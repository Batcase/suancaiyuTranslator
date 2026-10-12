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

    /** 识别降级链（只含配了 Key 的），失败时按顺序往下试 */
    private var asrChain: List<AsrProvider> = emptyList()
    private var asrIndex = 0
    /** 已经试过几家。用来区分"连接中失败"和"运行中偶发失败" */
    private var asrAttempts = 0
    /** 每一家的连接是否已经成功过（连上过就不再当成"需要换一家"） */
    private var asrConnected = false
    private val switchingAsr = AtomicBoolean(false)

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

    /**
     * 是否"等断句再整句翻"。
     *
     * true（默认，与 PC 端一致）：ASR 的中间结果**只更新屏幕上的原文**，
     *   译文等服务端判定这句说完再整句翻一次。
     *   好处：译文连贯、不会把同一句翻好几遍、省 token。
     *   （实测：快语速下翻译请求从 9 次降到 3 次，输入字符减少 56%）
     *
     * false：边说边翻，字幕出现更快，但快语速时会碎片化。
     */
    private val sentenceGated: Boolean = true

    /** 攒着等断句的整句（原文） */
    private var pending: String = ""
    private var pendingAt: Long = 0L
    private var pendingLastAt: Long = 0L
    private var currentCapturedAt: Long = 0L

    /** 攒够这么多字符就先翻（防止一口气说很久导致字幕一直空着） */
    private val maxPendingChars: Int = 60

    /** 静默这么久没有新内容就先翻（服务端不给断句信号时的兜底） */
    private val pendingTimeoutMs: Long = 1200

    // 注意：Kotlin 一个类只能有一个 companion object（本文件末尾已有一个），
    // 所以句末标点放在这里当普通属性，不再另开 companion。
    private val softBreak: Set<Char> = "。！？!?…‥".toSet()

    // ---------------------------------------------------------------

    fun start(): Boolean {
        if (stopped.get()) return false
        val srcLang = settings.sourceLang

        // ---- 识别：建立降级链，先试第一家 ----
        asrChain = AsrProvider.chain(settings.asrProvider) { settings.hasKey(it) }
        if (asrChain.isEmpty()) {
            store.bumpError("没有可用的识别引擎（请先填 Key）")
            store.setState(RunState.ERROR, "没有可用的识别引擎")
            return false
        }
        asrIndex = 0
        asrAttempts = 0
        if (!connectAsr()) return false

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

        // ---- 翻译消费协程（单飞 + 合并） ----
        scope.launch { consumeTranslations() }

        store.setState(RunState.RUNNING, "已连接 ${currentAsrLabel()}")
        return true
    }

    private fun currentAsrLabel(): String =
        asr?.label ?: asrChain.getOrNull(asrIndex)?.id.orEmpty()

    /** 连接识别链上的第 [asrIndex] 家。 */
    private fun connectAsr(): Boolean {
        val provider = asrChain.getOrNull(asrIndex)
        if (provider == null) {
            store.setState(RunState.ERROR, "所有识别引擎都连不上")
            return false
        }
        runCatching { asr?.close() }
        val client = createAsr(provider, settings.sourceLang)
        if (client == null) {
            store.bumpError("无法创建 ${provider.label} 客户端")
            return false
        }
        asr = client
        asrConnected = false
        asrAttempts += 1
        store.setActive(asr = provider.label.substringBefore(" · "))
        store.setState(RunState.RUNNING, "正在连接 ${provider.label.substringBefore(" · ")}…")
        Log.i(TAG, "尝试识别引擎 #$asrIndex: ${provider.id}")
        client.start { event -> onAsrEvent(event) }
        return true
    }

    /**
     * 识别出问题时换下一家。
     *
     * 只在**这家还没成功连上过**时才切换 —— 否则一次网络抖动就会把好用的引擎换掉。
     * 换过一轮（连最后一家也失败）就不再重试，避免无限循环烧 Key 额度。
     */
    private fun failoverAsr(reason: String) {
        if (stopped.get() || asrConnected) return
        if (!switchingAsr.compareAndSet(false, true)) return
        try {
            if (asrIndex + 1 >= asrChain.size) {
                Log.w(TAG, "识别链已走完，全部失败。最后一个原因：$reason")
                store.bumpError("识别引擎全部不可用：$reason")
                store.setState(RunState.ERROR, "识别不可用")
                return
            }
            asrIndex += 1
            val next = asrChain[asrIndex]
            Log.w(TAG, "识别失败（$reason），切换到 ${next.id}")
            store.setState(RunState.RUNNING, "已切换到 ${next.label.substringBefore(" · ")}")
            connectAsr()
        } finally {
            switchingAsr.set(false)
        }
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
            AsrEvent.Kind.INFO -> {
                // 连接成功（各客户端在握手完成时发这个），标记一下：
                // 之后再出错就不换引擎了，因为问题多半是偶发的网络抖动
                if (event.text.isNotEmpty()) {
                    asrConnected = true
                    Log.i(TAG, "识别已就绪：${event.text}")
                    store.setState(RunState.RUNNING, event.text.take(24))
                }
            }

            AsrEvent.Kind.ERROR -> {
                store.bumpError(event.text)
                if (!asrConnected) {
                    // 还没连上就失败（Key 无效 / 网络不通）→ 换下一家
                    failoverAsr(event.text)
                } else {
                    store.setState(RunState.RUNNING, event.text.take(30))
                }
            }

            AsrEvent.Kind.PARTIAL -> {
                val text = event.text.trim()
                if (text.isEmpty() || text == "…") return
                if (currentSource.isEmpty()) {
                    currentSid = store.beginSentence()
                }
                currentSource = text
                store.setSource(currentSid, text)
                if (sentenceGated) {
                    // 只说原文、不动翻译 —— 等断句（或兜底条件）再整句翻。
                    // 与 PC 端行为一致：解决快语速下"同一句被反复翻译、
                    // 且每次只看到半句话"造成的碎片化。
                    pending = text
                    pendingLastAt = System.currentTimeMillis()
                    maybeFlushPending()
                } else {
                    maybeSpeculate(text, event.audioCapturedAtMs)
                }
            }

            AsrEvent.Kind.FINAL -> {
                val text = event.text.trim()
                if (!event.utteranceEnd) {
                    // 这段音频定稿了，但句子可能没说完
                    if (text.isNotEmpty()) {
                        if (currentSource.isEmpty()) currentSid = store.beginSentence()
                        currentSource = text
                        store.setSource(currentSid, text)
                        if (sentenceGated) {
                            pending = text
                            pendingLastAt = System.currentTimeMillis()
                            maybeFlushPending()
                        } else {
                            maybeSpeculate(text, event.audioCapturedAtMs, force = true)
                        }
                    }
                    return
                }

                // 句子结束
                var finalText = text.ifEmpty { currentSource }
                // 缓冲区里是整句，比"最后一段"更完整
                if (sentenceGated && pending.isNotBlank()) finalText = pending.trim()
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
        pending = ""
        pendingAt = 0L
        pendingLastAt = 0L
    }

    /**
     * 判断攒着的这句要不要现在翻。
     *
     * 触发条件（先满足哪个算哪个）：
     *   1. 攒够 maxPendingChars —— 防止一口气说很久导致字幕一直空着
     *   2. 静默超过 pendingTimeoutMs —— 服务端没给断句信号时的兜底
     *   3. 标点软断句 —— 末尾是句末标点且已攒到一半容量
     *
     * 关于第 3 条：带 BGM 的直播音频动态范围常被压得极平（PC 端实测
     * P10~P90 仅 1dB），靠能量根本找不到停顿，只剩"攒够字符数硬切"，
     * 而那会切在句子中间。用标点断就自然多了。
     */
    private fun maybeFlushPending(force: Boolean = false) {
        val text = pending.trim()
        if (text.isEmpty()) return
        val now = System.currentTimeMillis()

        if (!force) {
            val tooLong = text.length >= maxPendingChars
            val quiet = if (pendingLastAt > 0) now - pendingLastAt else 0L
            val silentTooLong = quiet >= pendingTimeoutMs
            val punctBreak = text.length >= 12 &&
                text.last() in softBreak &&
                text.length >= maxPendingChars / 2
            if (!(tooLong || silentTooLong || punctBreak)) return
        }

        val sid = currentSid
        if (sid == 0L) return
        val captured = if (currentCapturedAt > 0) currentCapturedAt else now
        store.setSource(sid, text, final = true)
        submit(text, final = true, capturedAtMs = captured, sid = sid)
        pending = ""
        pendingAt = 0L
        pendingLastAt = 0L
        lastSpecText = text
    }

    /**
     * 由采集循环周期调用：负责"静默兜底"。
     *
     * 服务端迟迟不给 sentence_end 时（网络卡顿、或一句话说了很久），
     * 靠它把攒着的整句发出去，否则字幕会一直空着。
     *
     * 采集约 50 帧/秒，这里每次都调；真正的判断在 maybeFlushPending 内部
     * 按"字符数 / 静默时长 / 句末标点"决定要不要发，所以只是给个时机。
     */
    fun onTick() {
        if (!sentenceGated) return
        if (pending.isBlank()) return
        maybeFlushPending()
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
        // 记下"这段音频被播放的时刻"，攒着的那句等会儿提交时要用它算真实延迟
        if (capturedAtMs > 0) currentCapturedAt = capturedAtMs
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
