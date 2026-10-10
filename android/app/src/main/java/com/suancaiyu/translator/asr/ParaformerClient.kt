package com.suancaiyu.translator.asr

import android.util.Log
import com.suancaiyu.translator.AsrProvider
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

/**
 * 阿里云百炼 · Paraformer 实时语音识别（国内直连）。
 *
 * 协议流程（据官方文档，与 Windows 端实现一致）：
 *   1. 连接时在请求头带 `Authorization: Bearer <key>`（**握手阶段**就校验）
 *   2. 发 `run-task` 文本帧启动任务，等 `task-started` 后才能发音频
 *   3. 持续发**二进制**音频帧（单声道 PCM16）
 *   4. 发 `finish-task` 结束，等 `task-finished`
 *
 * 注意：官方说明仅华北2（北京）地域提供该服务。
 */
class ParaformerClient(
    private val apiKey: String,
    private val sourceLangCode: String,
    private val sampleRate: Int = 16000,
    private val workspaceId: String = "",
) : AsrClient {

    override val label = "阿里云 Paraformer"

    private val timeline = AudioTimeline(sampleRate)
    private val taskId = TaskId.next()
    private val closed = AtomicBoolean(false)
    private val started = AtomicBoolean(false)

    private var socket: WebSocket? = null
    private var eventSink: ((AsrEvent) -> Unit)? = null

    private val client: OkHttpClient = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(0, TimeUnit.MILLISECONDS)   // WebSocket 不能设读超时，否则会被掐断
        .writeTimeout(15, TimeUnit.SECONDS)
        .pingInterval(0, TimeUnit.MILLISECONDS)  // 服务端自带 heartbeat，别重复打扰
        .build()

    private fun endpoint(): String {
        val host = if (workspaceId.isNotBlank()) {
            "$workspaceId.cn-beijing.maas.aliyuncs.com"
        } else {
            "dashscope.aliyuncs.com"
        }
        return "wss://$host/api-ws/v1/inference"
    }

    override fun start(onEvent: (AsrEvent) -> Unit) {
        eventSink = onEvent
        val request = Request.Builder()
            .url(endpoint())
            .addHeader("Authorization", "Bearer $apiKey")
            .addHeader("user-agent", "SuanCaiYu-Translator/1.0")
            .build()

        socket = client.newWebSocket(request, object : WebSocketListener() {

            override fun onOpen(webSocket: WebSocket, response: Response) {
                Log.i(TAG, "WebSocket 已连接，发送 run-task")
                webSocket.send(runTaskPayload())
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                handleMessage(text)
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                if (closed.get()) return
                val code = response?.code ?: 0
                val msg = when {
                    code == 401 || code == 403 -> "Key 无效或没有权限（$code）"
                    else -> "${t.javaClass.simpleName}: ${t.message}"
                }
                Log.e(TAG, "WebSocket 失败: $msg", t)
                emit(AsrEvent(AsrEvent.Kind.ERROR, msg))
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                Log.i(TAG, "WebSocket 关闭 code=$code reason=$reason")
            }
        })
    }

    // ---------------------------------------------------------------

    private fun runTaskPayload(): String {
        val params = JSONObject().apply {
            put("format", "pcm")
            put("sample_rate", sampleRate)
            // 过滤"嗯、啊"：直播口语建议关掉，保留语气更自然
            put("disfluency_removal_enabled", false)
            // 语义断句准确率更高但延迟更大；直播字幕用 VAD 更合适
            put("semantic_punctuation_enabled", false)
            put("punctuation_prediction_enabled", true)
            // 心跳：持续发静音时保持连接不掉（直播常有长时间不说话）
            put("heartbeat", true)
            // 官方默认 1300ms 对直播太慢；日语实测 500 较合适
            put("max_sentence_silence", 500)
            // 语种提示：比让服务端猜更准（这是"提前告诉 AI 语言"的识别侧落点）
            com.suancaiyu.translator.Languages.hintOf(sourceLangCode)?.let {
                put("language_hints", JSONArray().put(it))
            }
        }
        val payload = JSONObject().apply {
            put("header", JSONObject().apply {
                put("action", "run-task")
                put("task_id", taskId)
                put("streaming", "duplex")
            })
            put("payload", JSONObject().apply {
                put("task_group", "audio")
                put("task", "asr")
                put("function", "recognition")
                put("model", AsrProvider.PARAFORMER.model)
                put("parameters", params)
                put("input", JSONObject())
            })
        }
        return payload.toString()
    }

    private fun handleMessage(text: String) {
        val root = parseJson(text) ?: return
        val header = root.optJSONObject("header") ?: return
        when (header.optString("event")) {
            "task-started" -> {
                started.set(true)
                Log.i(TAG, "task-started，可以发送音频")
                emit(AsrEvent(AsrEvent.Kind.INFO, "Paraformer 已连接"))
            }

            "result-generated" -> {
                val output = root.optJSONObject("payload")?.optJSONObject("output") ?: return
                val sentence = output.optJSONObject("sentence") ?: return
                if (sentence.optBoolean("heartbeat", false)) return   // 心跳包，跳过
                val txt = sentence.optString("text", "").trim()
                if (txt.isEmpty()) return

                // 用句子**结束**时间定位"说完的时刻"：偏保守（延迟算大不酸小），
                // 宁可保守也不要报喜不报忧
                val endMs = if (sentence.has("end_time") && !sentence.isNull("end_time")) {
                    sentence.optLong("end_time", -1L)
                } else -1L
                val at = if (endMs >= 0) timeline.timeAtOffsetMs(endMs) else timeline.now()

                val isEnd = sentence.optBoolean("sentence_end", false)
                emit(
                    AsrEvent(
                        kind = if (isEnd) AsrEvent.Kind.FINAL else AsrEvent.Kind.PARTIAL,
                        text = txt,
                        utteranceEnd = isEnd,
                        audioCapturedAtMs = at,
                    )
                )
            }

            "task-failed" -> {
                val code = header.optString("error_code", "")
                val msg = header.optString("error_message", "")
                val hint = if (msg.contains("timeout", true)) {
                    "（连接后 23 秒内没收到音频，或音频发送中断）"
                } else ""
                emit(AsrEvent(AsrEvent.Kind.ERROR, "Paraformer 任务失败 $code: $msg$hint"))
            }

            "task-finished" -> Log.i(TAG, "task-finished")
        }
    }

    /**
     * 发送音频。
     *
     * **必须无条件推进时间轴**（即使连接还没建好）——否则"连接未就绪"这段时间
     * 的音频不会计入，之后算延迟就会偏。这个坑在 Windows 端踩过。
     */
    override fun sendAudio(pcm: ByteArray, capturedAtMs: Long) {
        timeline.advance(pcm.size / 2, capturedAtMs)
        if (closed.get() || !started.get()) return
        socket?.send(okio.ByteString.of(*pcm))
    }

    private fun emit(event: AsrEvent) {
        eventSink?.invoke(event)
    }

    override fun close() {
        if (closed.getAndSet(true)) return
        try {
            socket?.send(
                JSONObject().apply {
                    put("header", JSONObject().apply {
                        put("action", "finish-task")
                        put("task_id", taskId)
                        put("streaming", "duplex")
                    })
                    put("payload", JSONObject().apply { put("input", JSONObject()) })
                }.toString()
            )
        } catch (t: Throwable) {
            Log.w(TAG, "发送 finish-task 失败（忽略）", t)
        }
        try {
            socket?.close(1000, "bye")
        } catch (t: Throwable) {
            Log.w(TAG, "关闭 WebSocket 异常（忽略）", t)
        }
        socket = null
        eventSink = null
    }

    companion object {
        private const val TAG = "Paraformer"
    }
}
