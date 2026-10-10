package com.suancaiyu.translator

/**
 * 厂商注册表 —— 整个 App 的"配置中心"。
 *
 * 设计意图：加一家新厂商只改这个文件，不用碰界面和业务代码。
 * 与 Windows 端 `livesub/keystore.py` 里的 ASR_PROVIDERS / TRANSLATE_PROVIDERS
 * 一一对应，两边保持一致，方便对照排错。
 */

/** 识别引擎。 */
enum class AsrProvider(
    val id: String,
    val label: String,
    /** 需要哪个 Key；null 表示不需要（本地识别） */
    val keyName: String?,
    val baseUrl: String,
    val model: String,
    /** true = 流式（边说边出）；false = 整句提交（延迟更高） */
    val streaming: Boolean,
    val note: String,
) {
    PARAFORMER(
        id = "paraformer",
        label = "阿里云 Paraformer · 国内直连（推荐）",
        keyName = KeyName.DASHSCOPE,
        baseUrl = "wss://dashscope.aliyuncs.com/api-ws/v1/inference",
        model = "paraformer-realtime-v2",
        streaming = true,
        note = "中文/英文/日语/韩语/粤语等，专为直播会议设计",
    ),
    DEEPGRAM(
        id = "deepgram",
        label = "Deepgram · 海外",
        keyName = KeyName.DEEPGRAM,
        baseUrl = "wss://api.deepgram.com/v1/listen",
        model = "nova-2",
        streaming = true,
        note = "多语种；日语用 nova-2（nova-3 会丢句首）；国内网络有时连不上",
    ),
    SENSEVOICE(
        id = "sensevoice",
        label = "硅基流动 SenseVoice · 国内",
        keyName = KeyName.SILICONFLOW,
        baseUrl = "https://api.siliconflow.cn/v1/audio/transcriptions",
        model = "FunAudioLLM/SenseVoiceSmall",
        streaming = false,
        note = "中文/日语/英语/韩语，识别准；按整句提交，比流式慢一点",
    ),
    ;

    companion object {
        fun byId(id: String?): AsrProvider =
            entries.firstOrNull { it.id == id } ?: PARAFORMER

        /** 降级顺序：只保留配了 Key 的（识别的兜底由用户决定，不自动加本地） */
        fun chain(preferred: AsrProvider, hasKey: (String) -> Boolean): List<AsrProvider> {
            val order = buildList {
                add(preferred)
                entries.forEach { if (it !in this) add(it) }
            }
            return order.filter { it.keyName == null || hasKey(it.keyName) }
                .ifEmpty { listOf(PARAFORMER) }
        }
    }
}

/** 翻译引擎（都是 OpenAI 兼容的 /chat/completions）。 */
enum class MtProvider(
    val id: String,
    val label: String,
    val keyName: String,
    val baseUrl: String,
    val model: String,
    val note: String,
) {
    DEEPSEEK(
        id = "deepseek",
        label = "DeepSeek · 深度求索（推荐）",
        keyName = KeyName.DEEPSEEK,
        baseUrl = "https://api.deepseek.com",
        model = "deepseek-chat",
        note = "便宜、快、中文口语自然",
    ),
    QWEN(
        id = "qwen",
        label = "通义千问 · 阿里云百炼",
        keyName = KeyName.DASHSCOPE,
        baseUrl = "https://dashscope.aliyuncs.com/compatible-mode/v1",
        model = "qwen-plus",
        note = "和 Paraformer 用同一个 Key，不用再注册",
    ),
    KIMI(
        id = "kimi",
        label = "Kimi · 月之暗面",
        keyName = KeyName.MOONSHOT,
        baseUrl = "https://api.moonshot.cn/v1",
        model = "moonshot-v1-8k",
        note = "中文表达好",
    ),
    SILICONFLOW(
        id = "siliconflow",
        label = "硅基流动 SiliconFlow",
        keyName = KeyName.SILICONFLOW,
        baseUrl = "https://api.siliconflow.cn/v1",
        model = "Qwen/Qwen2.5-7B-Instruct",
        note = "一个 Key 能用多家模型",
    ),
    ZHIPU(
        id = "zhipu",
        label = "智谱 GLM",
        keyName = KeyName.ZHIPU,
        baseUrl = "https://open.bigmodel.cn/api/paas/v4",
        model = "glm-4-flash",
        note = "glm-4-flash 有免费额度",
    ),
    OPENAI(
        id = "openai",
        label = "OpenAI · 海外",
        keyName = KeyName.OPENAI,
        baseUrl = "https://api.openai.com/v1",
        model = "gpt-4o-mini",
        note = "国内通常连不上，仅备用/海外用户",
    ),
    ;

    companion object {
        fun byId(id: String?): MtProvider = entries.firstOrNull { it.id == id } ?: DEEPSEEK

        /** 降级顺序：只保留配了 Key 的 */
        fun chain(preferred: MtProvider, hasKey: (String) -> Boolean): List<MtProvider> {
            val order = buildList {
                add(preferred)
                entries.forEach { if (it !in this) add(it) }
            }
            return order.filter { hasKey(it.keyName) }
                .ifEmpty { listOf(preferred) }
        }
    }
}

/** Key 的标识（存 SharedPreferences 时用这些名字）。 */
object KeyName {
    const val DASHSCOPE = "dashscope"
    const val DEEPSEEK = "deepseek"
    const val DEEPGRAM = "deepgram"
    const val SILICONFLOW = "siliconflow"
    const val MOONSHOT = "moonshot"
    const val ZHIPU = "zhipu"
    const val OPENAI = "openai"

    /** 界面展示顺序 + 申请地址 */
    val ALL: List<Triple<String, String, String>> = listOf(
        Triple(DASHSCOPE, "阿里云 Paraformer", "bailian.console.aliyun.com"),
        Triple(DEEPGRAM, "Deepgram", "console.deepgram.com"),
        Triple(SILICONFLOW, "硅基流动", "cloud.siliconflow.cn"),
        Triple(DEEPSEEK, "DeepSeek", "platform.deepseek.com"),
        Triple(MOONSHOT, "Kimi", "platform.moonshot.cn"),
        Triple(ZHIPU, "智谱 GLM", "open.bigmodel.cn"),
        Triple(OPENAI, "OpenAI", "platform.openai.com"),
    )
}

/** 支持的源语言（PARAFORMER 的 language_hints 取值）。 */
object Languages {
    data class Item(val code: String, val display: String, val hint: String?)

    val ALL = listOf(
        Item("ja", "日语", "ja"),
        Item("en", "英语", "en"),
        Item("ko", "韩语", "ko"),
        Item("zh", "中文", "zh"),
        Item("yue", "粤语", "yue"),
        Item("auto", "自动识别", null),
    )

    val TARGETS = listOf("简体中文", "繁體中文", "English", "日本語")

    fun byCode(code: String?): Item = ALL.firstOrNull { it.code == code } ?: ALL.first()

    /** 给翻译 AI 看的名字（写"日语"比写"ja"更稳） */
    fun displayOf(code: String?): String = byCode(code).display

    fun hintOf(code: String?): String? = byCode(code).hint
}
