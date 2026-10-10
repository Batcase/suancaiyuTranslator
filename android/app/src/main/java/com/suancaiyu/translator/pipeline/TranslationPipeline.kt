package com.suancaiyu.translator.pipeline

import android.util.Log
import com.suancaiyu.translator.AsrProvider
import com.suancaiyu.translator.Languages
import com.suancaiyu.translator.MtProvider
import com.suancaiyu.translator.Settings
import com.suancaiyu.translator.asr.AsrClient
import com.suancaiyu.translator.asr.AsrEvent
import com.suancaiyu.translator.asr.DeepgramClient
import com.suancaiyu.translator.asr.ParaformerClient
import com.suancaiyu.translator.asr.SenseVoiceClient
import com.suancaiyu.translator.data.RunState
import com.suancaiyu.translator.data.SubtitleStore
import com.suancaiyu.translator.mt.FallbackTranslator
import com.suancaiyu.translator.mt.Translator
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import java.util.concurrent.atomic.AtomicBoolean

/**
 * 编排层：把「内录 → 流式识别 → 翻译 → 悬浮字幕」串起来。
 *
 * 与 Windows 端 `pipeline.py` 同构，关键设计照搬（都是踩过坑才定下来的）：
 *
 *   1. **中间结果也翻译**，但做节流。只翻"定稿句子"的话用户要等 VAD 判定才看到字，
 *      观感是慢半拍。这里对还在生长的句子按间隔重翻，字幕跟着说话节奏往前爬。
 *   2. **译文异步回来必须校验 sid**，否则快速说话时会"上一句的译文贴到下一句"。
 *   3. **翻译请求单飞 + 合并**：任意时刻只有一个请求在飞，飞行期间只保留最新一个。
 *      既不会排队积压，也不会为中间结果反复烧钱。
 *   4. **忽略纯占位内容**（SenseVoice 会先发一个 "…"），避免把它当台词翻出去。
 */
class TranslationPipeline(
    private val settings: Settings,
    private val store: SubtitleStore,
) {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
    private val translatorLock = Mutex()
    private val stopped = AtomicBoolean(false)

    private var asr: AsrClient? = null
    private var translator: Translator? = null

    /** 翻译请求：容量 1 且 DROP_OLDEST 就是"单飞 + 合并" */
    private val mtQueue = Channel<MtJob>(capacity = 1, onBufferOverflow = kotlinx.coroutines.channels.BufferOverflow.DROP_OLDEST)

    private var currentSid = 0L
    private var currentSource = ""
    /** 上下文滑窗（原文→译文），让跨句人称/术语一致 */
    private val context = ArrayDeque<Pair<String, String>>()
    private var contextTurns = 3

    // 中间结果节流参数
    private var lastSpecText = ""
    private var lastSpecAt = 0L
    private val specIntervalMs = 350L
    private val specMinGrowth = 6

    private data class MtJob(
        val sid: Long,
        val text: String,
        val final: Boolean,
        val capturedAtMs: Long,
    )

    // ---------------------------------------------------------------

    fun start(): Boolean {
        if (stopped.get()) return false
        val srcLang = settings.sourceLang
        contextTurns = 3

        // ---- 识别：按降级链取第一家可用的 ----
        val asrChain = AsrProvider.chain(settings.asrProvider) { settings.hasKey(it) }
        val chosen = asrChain.firstOrNull() ?: run {
            store.bumpError("没有可用的识别引擎（请先填 Key）")
            store.setState(RunState.ERROR, "没有可用的识别引擎")
            return false
        }
        store.setActive(asr = chosen.label.substringBefore(" · "))
        asr = createAsr(chosen, srcLang)
        if (asr == null) {
            store.bumpError("无法创建识别客户端")
            return false
        }

        // ---- 翻译：多引擎降级 ----
        val mtChain = MtProvider.chain(settings.mtProvider) { settings.hasKey(it) }
        translator = FallbackTranslator(
            providers = mtChain,
            keyOf = { settings.key(it) },
            sourceLangCode = srcLang,
            targetLang = settings.targetLang,
            sendLanguageHint = settings.sendLanguageHint,
        ).also {
            store.setActive(mt = it.label)
            // 预热：提前告知语言背景 + 建连（丢到后台，不阻塞启动）
            scope.launch(Dispatchers.IO) { runCatching { it.warmup() } }
        }

        store.setState(RunState.RUNNING, "已连接 ${chosen.label.substringBefore(" · ")}")

        // ---- 翻译消费协程（单飞 + 合并） ----
        scope.launch { consumeTranslations() }

        // ---- 启动识别 ----
        asr?.start { event -> onAsrEvent(event) }
        return true
    }

    private fun createAsr(provider: AsrProvider, srcLang: String): AsrClient? {
        val key = provider.keyName?.let { settings.key(it) } ?: ""
        return when (provider) {
            AsrProvider.PARAFORMER -> ParaformerClient(key, srcLang)
            AsrProvider.DEEPGRAM -> DeepgramClient(key, srcLang)
            AsrProvider.SENSEVOICE -> SenseVoiceClient(key, scope)
        }
    }

    // ---------------------------------------------------------------
    // 识别事件处理
    // ---------------------------------------------------------------

    private fun onAsrEvent(event: AsrEvent) {
        when (event.kind) {
            AsrEvent.Kind.INFO -> Log.i(TAG, event.text)
            AsrEvent.Kind.ERROR -> {
                store.bumpError(event.text)
                store.setState(RunState.RUNNING, event.text.take(30))
            }

            AsrEvent.Kind.PARTIAL -> {
                val text = event.text.trim()
                if (text.isEmpty() || text == "…") return
                if (currentSource.isEmpty()) {
                    currentSid = store.beginSentence()
                }
                currentSource = text
                store.setSource(currentSid, text)
                maybeSpeculate(text, event.audioCapturedAtMs)
            }

            AsrEvent.Kind.FINAL -> {
                val text = event.text.trim()
                if (!event.utteranceEnd) {
                    // 这段音频定稿了，但句子可能没说完
                    if (text.isNotEmpty()) {
                        if (currentSource.isEmpty()) currentSid = store.beginSentence()
                        currentSource = text
                        store.setSource(currentSid, text)
                        maybeSpeculate(text, event.audioCapturedAtMs, force = true)
                    }
                    return
                }

                // 句子结束
                val finalText = text.ifEmpty { currentSource }
                if (finalText.isBlank()) {
                    if (currentSid != 0L) store.setSource(currentSid, "", final = true)
                    resetSentence()
                    return
                }
                if (currentSource.isEmpty()) currentSid = store.beginSentence()
                currentSource = finalText
                store.setSource(currentSid, finalText, final = true)

                // 定稿句：立刻翻译，并要求写进上下文
                submit(finalText, final = true, capturedAtMs = event.audioCapturedAtMs, sid = currentSid)
                resetSentence()
            }
        }
    }

    private fun resetSentence() {
        currentSource = ""
        lastSpecText = ""
        lastSpecAt = 0L
        currentSid = 0L
    }

    private fun maybeSpeculate(text: String, capturedAtMs: Long, force: Boolean = false) {
        val now = System.currentTimeMillis()
        val grew = text.length - lastSpecText.length >= specMinGrowth
        val firstTime = lastSpecText.isEmpty()
        if (!(force || firstTime || (grew && now - lastSpecAt >= specIntervalMs))) return
        lastSpecAt = now
        lastSpecText = text
        submit(text, final = false, capturedAtMs = capturedAtMs, sid = currentSid)
    }

    private fun submit(text: String, final: Boolean, capturedAtMs: Long, sid: Long) {
        if (text.isBlank() || sid == 0L) return
        // 容量 1 + DROP_OLDEST：新请求自动顶掉还没开始的旧请求
        mtQueue.trySend(MtJob(sid, text, final, capturedAtMs))
    }

    private suspend fun consumeTranslations() {
        for (job in mtQueue) {
            if (stopped.get()) break
            val t = translator ?: continue
            val t0 = System.currentTimeMillis()
            val result = translatorLock.withLock {
                t.translate(job.text, context.toList())
            }
            if (!result.ok) {
                store.bumpError(result.error)
                store.patchTranslation(job.sid, "（翻译失败: ${result.error}）", final = true, failed = true)
                continue
            }
            val e2e = if (job.capturedAtMs > 0) System.currentTimeMillis() - job.capturedAtMs else 0L
            val hit = store.patchTranslation(job.sid, result.text, final = job.final, e2eMs = e2e)
            // 只有命中的定稿句才进上下文（避免被顶掉的结果污染上下文）
            if (hit && job.final) {
                context.addLast(job.text to result.text)
                while (context.size > contextTurns * 3 + 4) context.removeFirst()
            }
            Log.d(TAG, "翻译 ${System.currentTimeMillis() - t0}ms -> ${result.text.take(30)}")
        }
    }

    // ---------------------------------------------------------------
    // 音频入口（由 Service 从采集引擎转发过来）
    // ---------------------------------------------------------------

    /**
     * 送入一帧音频。
     *
     * 直接从采集线程调用，**必须非阻塞**：内部只做"推进时间轴 + 入队"。
     * 时间轴无条件推进（即使连接还没建好），否则算延迟会偏——这个坑在 Windows 端踩过。
     */
    fun feedAudio(pcm: ByteArray, capturedAtMs: Long) {
        if (stopped.get()) return
        asr?.sendAudio(pcm, capturedAtMs)
    }

    // ---------------------------------------------------------------

    fun pause() {
        store.setState(RunState.PAUSED)
    }

    fun resume() {
        if (!stopped.get()) store.setState(RunState.RUNNING)
    }

    val isPaused: Boolean get() = store.state.value.state == RunState.PAUSED

    fun stop() {
        stopped.set(true)
        mtQueue.close()
        runCatching { asr?.close() }
        runCatching { translator?.close() }
        asr = null
        translator = null
        store.setState(RunState.IDLE, "")
    }

    companion object {
        private const val TAG = "Pipeline"
    }
}
