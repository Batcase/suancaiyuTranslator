package com.suancaiyu.translator.asr

import android.util.Log
import com.suancaiyu.translator.AsrProvider
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.MultipartBody
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import java.io.ByteArrayOutputStream
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

/**
 * 硅基流动 · SenseVoice —— **整句**识别（国内直连）。
 *
 * 与流式引擎的区别：它没有流式接口，只能一次提交一段完整音频，
 * 所以这里自己做了"静音断句"：
 *   攒够 [maxUtteranceMs] 或静音超过 [silenceEndMs] → 把这段发出去。
 * 因此端到端会多出"等待说完 + 一次 HTTP 往返"的时间（约 1~2 秒），
 * 但识别质量好、国内稳定，适合作为 Paraformer 的备用。
 *
 * 实现上直接组 WAV 字节（44 字节头 + PCM），不依赖 MediaCodec，
 * 保证任何设备上都能用。
 */
class SenseVoiceClient(
    private val apiKey: String,
    private val scope: CoroutineScope,
    private val sampleRate: Int = 16000,
    private val silenceEndMs: Long = 500,
    private val maxUtteranceMs: Long = 6000,
    private val silenceRms: Float = 0.004f,
) : AsrClient {

    override val label = "硅基流动 SenseVoice"

    private val timeline = AudioTimeline(sampleRate)
    private val closed = AtomicBoolean(false)
    private var eventSink: ((AsrEvent) -> Unit)? = null
    private var loop: Job? = null

    private val buffer = ByteArrayOutputStream()
    private var bufferStartMs = 0L
    private var lastVoiceAtMs = 0L
    private var firstFrameAtMs = 0L
    private var speechMs = 0L

    private val client = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(30, TimeUnit.SECONDS)
        .writeTimeout(30, TimeUnit.SECONDS)
        .build()

    override fun start(onEvent: (AsrEvent) -> Unit) {
        eventSink = onEvent
        emit(AsrEvent(AsrEvent.Kind.INFO, "SenseVoice 已就绪（整句识别）"))
        loop = scope.launch(Dispatchers.IO) {
            while (isActive && !closed.get()) {
                try {
                    kotlinx.coroutines.delay(120)
                    maybeFlush()
                } catch (t: Throwable) {
                    if (isActive) Log.w(TAG, "整句识别循环异常", t)
                }
            }
        }
    }

    @Synchronized
    override fun sendAudio(pcm: ByteArray, capturedAtMs: Long) {
        if (closed.get()) return
        timeline.advance(pcm.size / 2, capturedAtMs)
        if (firstFrameAtMs == 0L) firstFrameAtMs = capturedAtMs
        if (bufferStartMs == 0L) bufferStartMs = capturedAtMs

        val rms = rmsOf(pcm)
        val now = System.currentTimeMillis()
        if (rms >= silenceRms) {
            lastVoiceAtMs = now
            speechMs += pcm.size / 2 * 1000L / sampleRate
        }
        buffer.write(pcm)
    }

    @Synchronized
    private fun maybeFlush() {
        if (buffer.size() == 0) return
        val audioMs = buffer.size().toLong() / 2 * 1000 / sampleRate
        val quietMs = if (lastVoiceAtMs == 0L) audioMs else System.currentTimeMillis() - lastVoiceAtMs
        val shouldFlush = (quietMs >= silenceEndMs && speechMs >= 300) || audioMs >= maxUtteranceMs
        if (!shouldFlush) return

        val pcm = buffer.toByteArray()
        val startedAt = bufferStartMs
        reset()
        // 尽量没人说话就别浪费一次请求（也避免模型在静音上乱编）
        if (speechMs < 300 && audioMs < 800) return
        transcribe(pcm, startedAt)
    }

    @Synchronized
    private fun reset() {
        buffer.reset()
        bufferStartMs = 0L
        lastVoiceAtMs = 0L
        speechMs = 0L
        firstFrameAtMs = 0L
    }

    private fun transcribe(pcm: ByteArray, startedAtMs: Long) {
        // 先给界面一个"在处理"的信号，避免用户以为卡住了
        emit(AsrEvent(AsrEvent.Kind.PARTIAL, "…", audioCapturedAtMs = startedAtMs))

        val wav = wavOf(pcm, sampleRate)
        val body = MultipartBody.Builder()
            .setType(MultipartBody.FORM)
            .addFormDataPart(
                "file", "audio.wav",
                wav.toRequestBody("audio/wav".toMediaType())
            )
            .addFormDataPart("model", AsrProvider.SENSEVOICE.model)
            .build()

        val request = Request.Builder()
            .url(AsrProvider.SENSEVOICE.baseUrl)
            .addHeader("Authorization", "Bearer $apiKey")
            .post(body)
            .build()

        try {
            client.newCall(request).execute().use { resp ->
                val text = resp.body?.string().orEmpty()
                if (!resp.isSuccessful) {
                    val msg = when (resp.code) {
                        401, 403 -> "Key 无效或没有权限（${resp.code}）"
                        429 -> "请求过于频繁或额度用完（429）"
                        else -> "HTTP ${resp.code}: ${text.take(120)}"
                    }
                    emit(AsrEvent(AsrEvent.Kind.ERROR, msg))
                    return
                }
                val json = parseJson(text)
                val transcript = json?.optString("text", "").orEmpty().trim()
                if (transcript.isNotEmpty()) {
                    emit(
                        AsrEvent(
                            kind = AsrEvent.Kind.FINAL,
                            text = transcript,
                            utteranceEnd = true,
                            audioCapturedAtMs = startedAtMs,
                        )
                    )
                }
            }
        } catch (t: Throwable) {
            Log.e(TAG, "转写失败", t)
            emit(AsrEvent(AsrEvent.Kind.ERROR, "转写失败: ${t.javaClass.simpleName}"))
        }
    }

    private fun rmsOf(pcm: ByteArray): Float {
        if (pcm.size < 2) return 0f
        var sum = 0.0
        var i = 0
        while (i + 1 < pcm.size) {
            val v = ((pcm[i + 1].toInt() shl 8) or (pcm[i].toInt() and 0xFF)).toShort() / 32768.0
            sum += v * v
            i += 2
        }
        return kotlin.math.sqrt(sum / (pcm.size / 2)).toFloat()
    }

    /** 组一个最小 WAV（PCM16 单声道）。 */
    private fun wavOf(pcm: ByteArray, rate: Int): ByteArray {
        val out = ByteArrayOutputStream(44 + pcm.size)
        val dataLen = pcm.size
        val byteRate = rate * 2
        out.write("RIFF".toByteArray())
        out.write(intLE(36 + dataLen))
        out.write("WAVE".toByteArray())
        out.write("fmt ".toByteArray())
        out.write(intLE(16))
        out.write(shortLE(1))               // PCM
        out.write(shortLE(1))               // 单声道
        out.write(intLE(rate))
        out.write(intLE(byteRate))
        out.write(shortLE(2))               // blockAlign
        out.write(shortLE(16))              // bitsPerSample
        out.write("data".toByteArray())
        out.write(intLE(dataLen))
        out.write(pcm)
        return out.toByteArray()
    }

    private fun intLE(v: Int) = byteArrayOf(
        (v and 0xFF).toByte(), ((v shr 8) and 0xFF).toByte(),
        ((v shr 16) and 0xFF).toByte(), ((v shr 24) and 0xFF).toByte(),
    )

    private fun shortLE(v: Int) = byteArrayOf((v and 0xFF).toByte(), ((v shr 8) and 0xFF).toByte())

    private fun emit(event: AsrEvent) {
        eventSink?.invoke(event)
    }

    override fun close() {
        if (closed.getAndSet(true)) return
        loop?.cancel()
        loop = null
        try {
            maybeFlush()
        } catch (t: Throwable) {
            Log.w(TAG, "关闭时冲刷失败（忽略）", t)
        }
        eventSink = null
    }

    companion object {
        private const val TAG = "SenseVoice"
    }
}
