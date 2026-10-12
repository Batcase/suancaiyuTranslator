package com.suancaiyu.translator.audio

import android.annotation.SuppressLint
import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.media.projection.MediaProjection
import android.util.Log
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.channels.BufferOverflow
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import java.util.concurrent.atomic.AtomicLong
import kotlin.math.sqrt

/**
 * 采集"手机正在播放的声音"。
 *
 * 两种模式
 * --------
 * * [Mode.PLAYBACK]（默认）—— AudioPlaybackCapture，内录别的 App 的声音，**不采麦克风**。
 *   需要 MediaProjection 授权。系统限制：被采集的 App 可以声明拒绝被录
 *   （带版权保护的影音一定会），这时只能改用麦克风模式。
 * * [Mode.MIC] —— 普通麦克风采集。作为兜底，需要 RECORD_AUDIO 权限。
 *
 * 输出格式固定 **16000Hz / 单声道 / PCM16**：云端识别直接吃这个格式，
 * 不需要额外编码。有些设备不支持直接以 16k 打开，此时退化成设备原生采样率
 * 并在回调里重采样。
 */
class AudioCaptureEngine(private val scope: CoroutineScope) {

    enum class Mode { PLAYBACK, MIC }

    /** 一帧音频 */
    data class Frame(val pcm: ByteArray, val capturedAtMs: Long, val rms: Float)

    private val _frames = MutableSharedFlow<Frame>(
        extraBufferCapacity = 128,
        // 订阅者跟不上时丢**最旧**的：直播字幕宁可少听一点，也不能越拖越久
        onBufferOverflow = BufferOverflow.DROP_OLDEST,
    )
    val frames: SharedFlow<Frame> = _frames

    private var record: AudioRecord? = null
    private var job: Job? = null

    @Volatile
    private var running = false

    private val dropped = AtomicLong(0)
    private val totalFrames = AtomicLong(0)
    private val totalBytes = AtomicLong(0)

    /** 最近一帧的响度（0~1）。用于诊断"到底采到声音没有"。 */
    @Volatile
    var lastRms: Float = 0f
        private set

    /** 采集启动至今出现过的最大响度 —— 长时间为 0 说明根本没采到声音 */
    @Volatile
    var peakRms: Float = 0f
        private set

    val frameCount: Long get() = totalFrames.get()
    val droppedCount: Long get() = dropped.get()
    val capturedBytes: Long get() = totalBytes.get()

    /** 实际使用的采样率（可能不是 16k） */
    var actualSampleRate: Int = TARGET_SAMPLE_RATE
        private set

    /** 是否在做重采样（true 表示设备和目标不一致） */
    var needsResample: Boolean = false
        private set

    // ---------------------------------------------------------------
    // 打开
    // ---------------------------------------------------------------

    /**
     * 打开内录。
     *
     * @param projection 已授权的 MediaProjection（由 Activity 拿到后传进来）
     * @param sampleRate 目标采样率，默认 16000
     * @return 是否成功
     */
    @SuppressLint("MissingPermission")
    fun openPlayback(projection: MediaProjection, sampleRate: Int = TARGET_SAMPLE_RATE): Boolean {
        // 按"用途"白名单匹配，而不是按 UID 限定包名 —— 这样看哪个直播 App
        // 都能采到，用户不用手动选包名。
        //
        // 为什么要列这么多 usage：
        //   用户反馈「**连蓝牙耳机就采不到声音**」。蓝牙（尤其 LE Audio / A2DP）
        //   的音频在部分 ROM 上会被标成 MEDIA 以外的 usage，只匹配
        //   MEDIA + GAME + UNKNOWN 就会一条都采不到。这里把"播放类"的 usage
        //   尽量列全，先兜住。
        //
        // 唯独**不加 USAGE_VOICE_COMMUNICATION**：通话音频涉及隐私，
        // 系统本来也不让回环采到，加了只会让配置更容易被整体拒绝。
        val fullSet = intArrayOf(
            AudioAttributes.USAGE_MEDIA,
            AudioAttributes.USAGE_GAME,
            AudioAttributes.USAGE_UNKNOWN,
            AudioAttributes.USAGE_ASSISTANCE_SONIFICATION,
            AudioAttributes.USAGE_ASSISTANCE_NAVIGATION_GUIDANCE,
            AudioAttributes.USAGE_ASSISTANCE_ACCESSIBILITY,
            AudioAttributes.USAGE_NOTIFICATION,
            AudioAttributes.USAGE_NOTIFICATION_EVENT,
            AudioAttributes.USAGE_NOTIFICATION_RINGTONE,
            AudioAttributes.USAGE_ALARM,
        )
        val safeSet = intArrayOf(
            AudioAttributes.USAGE_MEDIA,
            AudioAttributes.USAGE_GAME,
            AudioAttributes.USAGE_UNKNOWN,
        )

        // 分级尝试：个别 ROM 会因为某个 usage 拒绝整个配置，
        // 那就退回到最保守的三项。比"一次失败就放弃"稳妥得多。
        val attempts = listOf("全量用途" to fullSet, "常用用途" to safeSet)

        var lastError: Throwable? = null
        for ((label, usageSet) in attempts) {
            try {
                val builder = android.media.AudioPlaybackCaptureConfiguration.Builder(projection)
                for (u in usageSet) builder.addMatchingUsage(u)
                val config = builder.build()

                val rate = pickSampleRate(sampleRate)
                val format = AudioFormat.Builder()
                    .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                    .setSampleRate(rate)
                    .setChannelMask(AudioFormat.CHANNEL_IN_MONO)
                    .build()

                val minBuf = AudioRecord.getMinBufferSize(
                    rate, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT
                )
                if (minBuf <= 0) {
                    Log.e(TAG, "getMinBufferSize 返回 $minBuf，设备不支持该格式")
                    lastError = IllegalStateException("设备不支持 ${rate}Hz 单声道 PCM16")
                    continue
                }

                record = AudioRecord.Builder()
                    .setAudioFormat(format)
                    // 给 4 倍最小缓冲：太小会 underrun 丢数据，太大等于人为加延迟
                    .setBufferSizeInBytes(minBuf * 4)
                    .setAudioPlaybackCaptureConfig(config)
                    .build()

                actualSampleRate = rate
                needsResample = rate != sampleRate
                captureLabel = label
                Log.i(TAG, "内录已打开（$label，${rate}Hz，usage ${usageSet.size} 项）")
                startLoop()
                return true
            } catch (t: Throwable) {
                // 常见失败：SecurityException（投影授权失效）、
                // IllegalArgumentException（该 ROM 不接受某些 usage / 格式）
                Log.w(TAG, "内录打开失败（$label）：${t.javaClass.simpleName}: ${t.message}")
                lastError = t
                release()
            }
        }

        Log.e(TAG, "内录全部尝试失败", lastError)
        // 把失败原因留给上层显示 —— 不然界面只能看到一句"内录打开失败"，
        // 而真正有用的是 SecurityException 消息里写的"缺哪个权限"。
        playbackError = lastError?.let {
            "${it.javaClass.simpleName}${it.message?.let { m -> " - ${m.take(140)}" } ?: ""}"
        } ?: "未知原因"
        return false
    }

    /** 上一次 openPlayback 失败的原因（供界面显示） */
    @Volatile
    var playbackError: String = ""
        private set

    /** 供日志/界面排查用：实际生效的是哪档配置 */
    @Volatile
    var captureLabel: String = ""
        private set

    /** 打开麦克风采集（兜底模式）。 */
    @SuppressLint("MissingPermission")
    fun openMic(sampleRate: Int = TARGET_SAMPLE_RATE): Boolean {
        return try {
            val rate = pickSampleRate(sampleRate)
            val format = AudioFormat.Builder()
                .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                .setSampleRate(rate)
                .setChannelMask(AudioFormat.CHANNEL_IN_MONO)
                .build()
            val minBuf = AudioRecord.getMinBufferSize(
                rate, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT
            )
            if (minBuf <= 0) return false

            record = AudioRecord.Builder()
                .setAudioFormat(format)
                .setBufferSizeInBytes(minBuf * 4)
                .setAudioSource(MediaRecorder.AudioSource.MIC)
                .build()

            actualSampleRate = rate
            needsResample = rate != sampleRate
            startLoop()
            true
        } catch (t: Throwable) {
            Log.e(TAG, "打开麦克风失败", t)
            release()
            false
        }
    }

    /**
     * 选一个可用的采样率。
     *
     * 优先用目标值（16k）——系统会替我们重采样，比自己做省电也省延迟。
     * 但部分 ROM/设备会拒绝非原生采样率，这时退化成设备原生值。
     */
    private fun pickSampleRate(preferred: Int): Int {
        if (isRateSupported(preferred)) return preferred
        // 退化成常见的原生采样率
        for (fallback in intArrayOf(48000, 44100)) {
            if (isRateSupported(fallback)) {
                Log.w(TAG, "设备不支持 ${preferred}Hz，退化为 ${fallback}Hz（将本地重采样）")
                return fallback
            }
        }
        return preferred
    }

    private fun isRateSupported(rate: Int): Boolean = try {
        AudioRecord.getMinBufferSize(
            rate, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT
        ) > 0
    } catch (t: Throwable) {
        false
    }

    // ---------------------------------------------------------------
    // 读取循环
    // ---------------------------------------------------------------

    private fun startLoop() {
        val rec = record ?: return
        rec.startRecording()
        if (rec.recordingState != AudioRecord.RECORDSTATE_RECORDING) {
            Log.e(TAG, "startRecording 后状态不是 RECORDING，可能被系统拒绝")
            release()
            return
        }
        running = true

        // 20ms 一帧（16k 下 = 320 采样 = 640 字节）。
        // 更小（10ms）在移动网络下反而更差：包太多、TCP/TLS 开销占比上升。
        val frameSamples = actualSampleRate / 50
        job = scope.launch(Dispatchers.IO) {
            val buf = ShortArray(frameSamples)
            val native = if (needsResample) ShortArray(frameSamples) else buf
            while (isActive && running) {
                val read = rec.read(native, 0, native.size)
                if (read <= 0) {
                    // 负值是错误码，必须处理，否则会变成"界面在跑但没声音"
                    if (read < 0) {
                        Log.w(TAG, "AudioRecord.read 返回错误码 $read")
                        if (read == AudioRecord.ERROR_INVALID_OPERATION ||
                            read == AudioRecord.ERROR_DEAD_OBJECT
                        ) {
                            break
                        }
                    }
                    continue
                }
                val pcm = if (needsResample) {
                    Resampler.resampleTo16k(native, read, actualSampleRate)
                } else {
                    Shorts.toBytesLittleEndian(native, read)
                }
                if (pcm.isEmpty()) continue
                val rms = rmsOf(native, read)
                lastRms = rms
                if (rms > peakRms) peakRms = rms
                totalFrames.incrementAndGet()
                totalBytes.addAndGet(pcm.size.toLong())
                val emitted = _frames.tryEmit(Frame(pcm, System.currentTimeMillis(), rms))
                if (!emitted) dropped.incrementAndGet()
            }
        }
    }

    private fun rmsOf(data: ShortArray, len: Int): Float {
        if (len <= 0) return 0f
        var sum = 0.0
        for (i in 0 until len) {
            val v = data[i] / 32768.0
            sum += v * v
        }
        return sqrt(sum / len).toFloat()
    }

    // ---------------------------------------------------------------
    // 关闭与统计
    // ---------------------------------------------------------------

    fun release() {
        running = false
        job?.cancel()
        job = null
        record?.let { rec ->
            try {
                if (rec.recordingState == AudioRecord.RECORDSTATE_RECORDING) rec.stop()
            } catch (t: Throwable) {
                Log.w(TAG, "停止录制时异常（忽略）", t)
            }
            try {
                rec.release()
            } catch (t: Throwable) {
                Log.w(TAG, "释放 AudioRecord 异常（忽略）", t)
            }
        }
        record = null
    }

    val isRunning: Boolean get() = running

    fun stats(): String = "frames=${totalFrames.get()} dropped=${dropped.get()}"

    companion object {
        private const val TAG = "AudioCapture"
        const val TARGET_SAMPLE_RATE = 16000
    }
}

/** ShortArray <-> ByteArray（小端，PCM16）。 */
internal object Shorts {
    fun toBytesLittleEndian(src: ShortArray, len: Int): ByteArray {
        val out = ByteArray(len * 2)
        var i = 0
        var o = 0
        while (i < len) {
            val v = src[i].toInt()
            out[o] = (v and 0xFF).toByte()
            out[o + 1] = ((v shr 8) and 0xFF).toByte()
            o += 2
            i++
        }
        return out
    }
}

/**
 * 线性插值重采样。
 *
 * 人声识别不需要高阶滤波器，线性插值够用且极快（与 Windows 端同一套做法）。
 */
internal object Resampler {
    fun resampleTo16k(src: ShortArray, len: Int, srcRate: Int): ByteArray {
        if (srcRate == 16000 || len <= 0) return Shorts.toBytesLittleEndian(src, len)
        val outLen = (len.toLong() * 16000 / srcRate).toInt()
        if (outLen <= 0) return ByteArray(0)
        val out = ByteArray(outLen * 2)
        val step = (len - 1).toDouble() / maxOf(1, outLen - 1)
        var o = 0
        for (i in 0 until outLen) {
            val pos = i * step
            val i0 = pos.toInt().coerceIn(0, len - 1)
            val i1 = (i0 + 1).coerceAtMost(len - 1)
            val frac = pos - i0
            val sample = (src[i0] * (1 - frac) + src[i1] * frac).toInt()
                .coerceIn(-32768, 32767)
            out[o] = (sample and 0xFF).toByte()
            out[o + 1] = ((sample shr 8) and 0xFF).toByte()
            o += 2
        }
        return out
    }
}
