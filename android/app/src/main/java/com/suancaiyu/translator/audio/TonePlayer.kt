package com.suancaiyu.translator.audio

import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioTrack
import android.util.Log
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlin.math.PI
import kotlin.math.sin

/**
 * 自测音源 —— 直接播放一段正弦波。
 *
 * 为什么需要它：内录失败最难查的表现是"界面在跑但没字幕"，而原因可能有三类
 * ——没声音在放、被采集的 App 拒绝录制、采集配置不对。用这个能**确定性地**
 * 造出一段已知响度的声音，把"没声音"这个变量排除掉，剩下的就是真问题。
 *
 * 也用于开发期验证 AudioPlaybackCapture 是否真的通了。
 */
object TonePlayer {

    private const val TAG = "TonePlayer"

    /**
     * 播放内置的日语样本（`res/raw/ja_sample.wav`）。
     *
     * 用来做**确定性的端到端自测**：样本本身是一段已知内容的日语，
     * 播出来后会被内录采到 → 走完整识别→翻译链路 → 能直接看识别结果对不对。
     * 这比播放正弦波强得多（正弦波只能证明"采到音频了"，证明不了识别链路）。
     *
     * 模拟器上通常没有 TTS 引擎，所以自带一段样本是最可靠的验证手段。
     */
    suspend fun playAsset(context: android.content.Context, resId: Int): Boolean =
        withContext(Dispatchers.IO) {
            var player: android.media.MediaPlayer? = null
            return@withContext try {
                val afd = context.resources.openRawResourceFd(resId)
                    ?: return@withContext false
                player = android.media.MediaPlayer().apply {
                    setAudioAttributes(
                        AudioAttributes.Builder()
                            // 必须是 USAGE_MEDIA，否则内录的 usage 白名单匹配不到
                            .setUsage(AudioAttributes.USAGE_MEDIA)
                            .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                            .build()
                    )
                    setDataSource(afd.fileDescriptor, afd.startOffset, afd.length)
                    prepare()
                }
                afd.close()
                player.start()
                Log.i(TAG, "开始播放内置日语样本（${player.duration}ms）")
                val dur = player.duration
                kotlinx.coroutines.delay(dur.toLong() + 500)
                true
            } catch (t: Throwable) {
                Log.e(TAG, "播放内置样本失败", t)
                false
            } finally {
                runCatching {
                    player?.stop()
                    player?.release()
                }
            }
        }

    /**
     * 播放一段正弦波，音量可指定。
     *
     * @param freqHz 频率（440 = 标准音 A）
     * @param seconds 时长
     * @param amplitude 振幅 0~1
     */
    suspend fun play(
        freqHz: Double = 440.0,
        seconds: Double = 3.0,
        amplitude: Double = 0.6,
        sampleRate: Int = 44100,
    ): Boolean = withContext(Dispatchers.IO) {
        var track: AudioTrack? = null
        return@withContext try {
            val totalSamples = (sampleRate * seconds).toInt()
            val buf = ShortArray(totalSamples)
            for (i in 0 until totalSamples) {
                // 加淡入淡出，避免爆音（爆音会让 RMS 读数失真）
                val fade = minOf(1.0, i / (sampleRate * 0.05), (totalSamples - i) / (sampleRate * 0.05))
                buf[i] = (sin(2 * PI * freqHz * i / sampleRate) * amplitude * fade * 32767).toInt().toShort()
            }

            val minBuf = AudioTrack.getMinBufferSize(
                sampleRate, AudioFormat.CHANNEL_OUT_MONO, AudioFormat.ENCODING_PCM_16BIT
            )
            track = AudioTrack.Builder()
                .setAudioAttributes(
                    AudioAttributes.Builder()
                        // 关键：用 USAGE_MEDIA，才会被内录的 usage 白名单匹配到
                        .setUsage(AudioAttributes.USAGE_MEDIA)
                        .setContentType(AudioAttributes.CONTENT_TYPE_MUSIC)
                        .build()
                )
                .setAudioFormat(
                    AudioFormat.Builder()
                        .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                        .setSampleRate(sampleRate)
                        .setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
                        .build()
                )
                .setBufferSizeInBytes(maxOf(minBuf, buf.size * 2))
                .setTransferMode(AudioTrack.MODE_STATIC)
                .build()

            track.write(buf, 0, buf.size)
            track.setVolume(1.0f)
            track.play()
            Log.i(TAG, "开始播放测试音：${freqHz}Hz ${seconds}s 振幅=$amplitude")
            kotlinx.coroutines.delay((seconds * 1000).toLong() + 200)
            true
        } catch (t: Throwable) {
            Log.e(TAG, "播放测试音失败", t)
            false
        } finally {
            runCatching {
                track?.stop()
                track?.release()
            }
        }
    }
}
