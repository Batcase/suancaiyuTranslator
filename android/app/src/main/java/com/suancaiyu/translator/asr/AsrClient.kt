package com.suancaiyu.translator.asr

import org.json.JSONObject
import java.util.concurrent.atomic.AtomicInteger

/**
 * 识别结果事件 —— 各引擎统一成这一种。
 *
 * 三个引擎的语义对应关系（照搬 Windows 端的经验，避免再踩一遍）：
 *   Paraformer: sentence_end=false -> PARTIAL；true -> FINAL(句尾)
 *   Deepgram  : is_final=false -> PARTIAL；speech_final=true -> FINAL(句尾)
 *   SenseVoice: 整句返回 -> FINAL(句尾)，中间只给一个占位 PARTIAL
 */
data class AsrEvent(
    val kind: Kind,
    val text: String = "",
    /** true = 这句话说完了（服务端 VAD 判定）；false = 只是这段音频定稿 */
    val utteranceEnd: Boolean = false,
    /** 这段音频**被播放**的时刻（用于算真实端到端延迟） */
    val audioCapturedAtMs: Long = 0L,
    val language: String = "",
) {
    enum class Kind { PARTIAL, FINAL, ERROR, INFO }
}

/** 识别客户端统一接口。 */
interface AsrClient {
    /** 送入一帧音频。可从任意线程调用，必须非阻塞。 */
    fun sendAudio(pcm: ByteArray, capturedAtMs: Long)

    /** 启动。回调都在后台线程，最终通过 [onEvent] 抛出。 */
    fun start(onEvent: (AsrEvent) -> Unit)

    /** 关闭连接并释放资源。 */
    fun close()

    /** 便于界面显示"当前实际在用哪家" */
    val label: String
}

/**
 * 音频时间轴 —— 把"识别结果"对回"这段声音真正被播放的时刻"。
 *
 * 为什么不能直接用 System.currentTimeMillis()：
 *   识别结果是**异步**回来的，网络往返 + 服务端处理可能几百毫秒。
 *   用"收到结果的时刻"当基准会把延迟算成 0，完全失真。
 *
 * 做法：发送音频时按采样数推进游标，并用每帧自带的 capturedAtMs 当锚点，
 * 这样即使音频是突发推送的（不是严格实时），换算依然准确。
 */
class AudioTimeline(private val sampleRate: Int = 16000) {

    private var sentSamples = 0L
    private var anchorAtMs = 0L
    private var anchorSamples = 0L
    private var startedAtMs = 0L

    @Synchronized
    fun advance(samples: Int, capturedAtMs: Long) {
        if (sentSamples == 0L) startedAtMs = capturedAtMs
        anchorAtMs = capturedAtMs
        anchorSamples = sentSamples
        sentSamples += samples
    }

    /** 当前已发送音频的末尾对应的"播放时刻" */
    @Synchronized
    fun now(): Long = anchorAtMs + (sentSamples - anchorSamples) * 1000L / sampleRate

    /**
     * 把服务端给的"相对音频起点的毫秒数"换算成绝对播放时刻。
     * Paraformer 的结果里带 begin_time / end_time，用它可以精确定位。
     */
    @Synchronized
    fun timeAtOffsetMs(offsetMs: Long): Long = startedAtMs + offsetMs

    @Synchronized
    fun reset() {
        sentSamples = 0
        anchorSamples = 0
        anchorAtMs = 0
        startedAtMs = 0
    }
}

/** 生成 UUID 形式 task_id（Paraformer 要求）。 */
internal object TaskId {
    private val counter = AtomicInteger(0)
    fun next(): String {
        val n = counter.incrementAndGet()
        val rnd = (Math.random() * 0xFFFFFFF).toLong()
        return "livesub-%08x-%04x-4%03x-%04x-%012x".format(
            System.currentTimeMillis() and 0xFFFFFFFFL,
            Thread.currentThread().id and 0xFFFF,
            (rnd shr 16) and 0xFFF,
            (rnd and 0x3FFF) or 0x8000,
            rnd * 7919 and 0xFFFFFFFFFFFFL,
        )
    }
}

/** 各引擎共用的 JSON 小工具（失败时不抛异常，返回空对象）。 */
internal fun parseJson(text: String): JSONObject? = try {
    JSONObject(text)
} catch (t: Throwable) {
    null
}
