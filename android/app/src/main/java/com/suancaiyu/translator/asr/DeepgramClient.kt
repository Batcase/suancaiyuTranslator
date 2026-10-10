package com.suancaiyu.translator.asr

import android.util.Log
import com.suancaiyu.translator.AsrProvider
import com.suancaiyu.translator.Languages
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Deepgram 流式识别。
 *
 * 关键参数（都是实测结论，照搬 Windows 端）：
 *   * `encoding=linear16&sample_rate=16000&channels=1` —— 直接吃裸 PCM16
 *   * `endpointing=500` —— 断句静音阈值。**300 会把日语句子切成碎片**
 *     （实测「…すごい」+「んですよ」分开，译文读不通），700 又会过度合并
 *   * `language=ja` —— 固定语种比自动判断更准更快
 *   * `model=nova-2` —— **日语不要用 nova-3**，实测 nova-3 每句丢句首
 *   * `vad_events=true` —— 服务端会额外推 SpeechStarted，便于知道"开始说话了"
 */
class DeepgramClient(
    private val apiKey: String,
    private val sourceLangCode: String,
    private val sampleRate: Int = 16000,
    private val model: String = AsrProvider.DEEPGRAM.model,
) : AsrClient {

    override val label = "Deepgram"

    private val timeline = AudioTimeline(sampleRate)
    private val closed = AtomicBoolean(false)
    private var socket: WebSocket? = null
    private var eventSink: ((AsrEvent) -> Unit)? = null
    private var lastSpeechAt = 0L

    private val client = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(0, TimeUnit.MILLISECONDS)
        .writeTimeout(15, TimeUnit.SECONDS)
        .pingInterval(10, TimeUnit.SECONDS)   // Deepgram 需要心跳保活
        .build()

    private fun url(): String {
        val lang = Languages.hintOf(sourceLangCode) ?: "multi"
        val q = buildString {
            append("model=").append(model)
            append("&encoding=linear16&sample_rate=").append(sampleRate).append("&channels=1")
            append("&interim_results=true")
            append("&punctuate=true&smart_format=true")
            append("&language=").append(lang)
            append("&endpointing=500")
            append("&vad_events=true")
        }
        return "wss://api.deepgram.com/v1/listen?$q"
    }

    override fun start(onEvent: (AsrEvent) -> Unit) {
        eventSink = onEvent
        val request = Request.Builder()
            .url(url())
            .addHeader("Authorization", "Token $apiKey")
            .build()

        socket = client.newWebSocket(request, object : WebSocketListener() {

            override fun onOpen(webSocket: WebSocket, response: Response) {
                Log.i(TAG, "Deepgram 已连接")
                emit(AsrEvent(AsrEvent.Kind.INFO, "Deepgram 已连接 (model=$model)"))
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                handleMessage(text)
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                if (closed.get()) return
                val code = response?.code ?: 0
                val msg = when (code) {
                    401 -> "Key 无效（401）"
                    403 -> "Key 被拒绝（403）"
                    0 -> "${t.javaClass.simpleName}: ${t.message}"
                    else -> "连接失败 HTTP $code"
                }
                Log.e(TAG, "Deepgram 失败: $msg", t)
                emit(AsrEvent(AsrEvent.Kind.ERROR, msg))
            }
        })
    }

    private fun handleMessage(text: String) {
        val root = parseJson(text) ?: return
        when (root.optString("type")) {
            "Results" -> {
                val alt = root.optJSONObject("channel")
                    ?.optJSONArray("alternatives")
                    ?.optJSONObject(0) ?: return
                val txt = alt.optString("transcript", "").trim()
                val isFinal = root.optBoolean("is_final", false)
                val speechFinal = root.optBoolean("speech_final", false)
                if (txt.isEmpty() && !speechFinal) return
                emit(
                    AsrEvent(
                        kind = if (isFinal) AsrEvent.Kind.FINAL else AsrEvent.Kind.PARTIAL,
                        text = txt,
                        utteranceEnd = speechFinal,
                        audioCapturedAtMs = timeline.now(),
                        language = root.optJSONObject("metadata")?.optString("language", "").orEmpty(),
                    )
                )
            }

            "UtteranceEnd" -> emit(
                AsrEvent(
                    AsrEvent.Kind.FINAL,
                    text = "",
                    utteranceEnd = true,
                    audioCapturedAtMs = timeline.now(),
                )
            )

            "SpeechStarted" -> lastSpeechAt = timeline.now()

            "Error" -> emit(
                AsrEvent(
                    AsrEvent.Kind.ERROR,
                    "${root.optString("variant")}: ${root.optString("description")}",
                )
            )
        }
    }

    override fun sendAudio(pcm: ByteArray, capturedAtMs: Long) {
        timeline.advance(pcm.size / 2, capturedAtMs)
        if (closed.get()) return
        socket?.send(okio.ByteString.of(*pcm))
    }

    private fun emit(event: AsrEvent) {
        eventSink?.invoke(event)
    }

    override fun close() {
        if (closed.getAndSet(true)) return
        try {
            socket?.send("{\"type\":\"CloseStream\"}")
            socket?.close(1000, "bye")
        } catch (t: Throwable) {
            Log.w(TAG, "关闭 Deepgram 异常（忽略）", t)
        }
        socket = null
        eventSink = null
    }

    companion object {
        private const val TAG = "Deepgram"
    }
}
