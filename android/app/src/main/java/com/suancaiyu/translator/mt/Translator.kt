package com.suancaiyu.translator.mt

import android.util.Log
import com.suancaiyu.translator.Languages
import com.suancaiyu.translator.MtProvider
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.TimeUnit

data class MtResult(val text: String, val ok: Boolean, val error: String = "", val elapsedMs: Long = 0)

/** 翻译器统一接口。 */
interface Translator {
    val label: String
    suspend fun translate(text: String, context: List<Pair<String, String>>): MtResult
    /** 预热：提前把语言背景告诉模型并建好连接 */
    fun warmup()
    fun close()
}

/**
 * 各家通用实现 —— 都是 OpenAI 兼容的 `/chat/completions`。
 *
 * 所以"接入一家新厂商"只需要在 Providers.kt 的 MtProvider 里加一个枚举值。
 */
class ChatTranslator(
    private val provider: MtProvider,
    private val apiKey: String,
    private val sourceLangCode: String,
    private val targetLang: String,
    private val sendLanguageHint: Boolean,
    private val timeoutSec: Long = 12,
) : Translator {

    override val label: String get() = provider.label.substringBefore(" · ")

    private val client = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        .readTimeout(timeoutSec, TimeUnit.SECONDS)
        .writeTimeout(timeoutSec, TimeUnit.SECONDS)
        .build()

    private val systemPrompt: String = buildSystemPrompt()

    private fun buildSystemPrompt(): String {
        val parts = mutableListOf(ROOT_PROMPT.replace("{target}", targetLang))
        val langHint = Languages.hintOf(sourceLangCode)
        if (sendLanguageHint && langHint != null) {
            // 这就是"提前告诉 AI 主要语言"的落点：写"主要语言"而不是"唯一语言"，
            // 否则它会把你英文术语、人名也硬按日语翻错
            parts += "【本次任务的语言背景】这场直播的主要语言是${Languages.displayOf(sourceLangCode)}" +
                "（识别引擎已按该语言识别）。请以该语言的理解优先；若句中确实夹有其它语言，按实际语言处理。"
        }
        return parts.joinToString("\n\n")
    }

    private fun hintPrompt(): String =
        "接下来你会收到一段直播语音的实时识别文本，需要翻译成$targetLang。\n\n" +
            "重要背景：这段直播的**主要语言是${Languages.displayOf(sourceLangCode)}**。" +
            "识别引擎已经按该语言识别，你收到的文本绝大多数就是它。\n\n" +
            "请据此处理：\n" +
            "1. 以该语言的理解优先。遇到看起来像英文单词或罗马字的部分，先考虑它是不是该语言里的" +
            "外来语、专有名词或口头禅，而不是直接当成英文去翻译。\n" +
            "2. 但也不要死板：如果确实出现了其它语言（英文术语、人名、歌词等），按它实际的语言理解即可。\n" +
            "3. 保持前后一致：同一个人名/术语在全场直播里始终用同一种译法。\n\n" +
            "收到后回复\"好\"，然后我们开始逐句翻译。"

    /**
     * 预热：单独发一条请求把语言背景告诉模型，并建好 HTTP 连接。
     *
     * 两个好处：模型第一句台词前就知道语种背景；顺带省掉建连的 300~600ms。
     * 失败不影响正常翻译（正常请求里也带着同样的语言背景）。
     */
    override fun warmup() {
        val hint = Languages.hintOf(sourceLangCode)
        if (!sendLanguageHint || hint == null) return
        try {
            post(
                JSONObject().apply {
                    put("model", provider.model)
                    put("messages", JSONArray().apply {
                        put(JSONObject().put("role", "system").put("content", systemPrompt))
                        put(JSONObject().put("role", "user").put("content", hintPrompt()))
                    })
                    put("temperature", 0)
                    put("max_tokens", 8)
                    put("stream", false)
                }
            )
            Log.i(TAG, "$label 已提前告知语言背景：${Languages.displayOf(sourceLangCode)}")
        } catch (t: Throwable) {
            Log.w(TAG, "$label 预热失败（不影响使用）: ${t.javaClass.simpleName}")
        }
    }

    override suspend fun translate(text: String, context: List<Pair<String, String>>): MtResult =
        withContext(Dispatchers.IO) {
            val started = System.currentTimeMillis()
            try {
                val messages = JSONArray().apply {
                    put(JSONObject().put("role", "system").put("content", systemPrompt))
                    context.takeLast(3).forEach { (src, dst) ->
                        put(JSONObject().put("role", "user").put("content", src))
                        put(JSONObject().put("role", "assistant").put("content", dst))
                    }
                    put(JSONObject().put("role", "user").put("content", text))
                }
                val payload = JSONObject().apply {
                    put("model", provider.model)
                    put("messages", messages)
                    put("temperature", 0.2)
                    put("max_tokens", 512)
                    put("stream", false)
                }
                val json = post(payload)
                val content = json.optJSONArray("choices")
                    ?.optJSONObject(0)
                    ?.optJSONObject("message")
                    ?.optString("content", "")
                    .orEmpty()
                MtResult(
                    text = clean(content),
                    ok = true,
                    elapsedMs = System.currentTimeMillis() - started,
                )
            } catch (t: Throwable) {
                val msg = t.message.orEmpty()
                val friendly = when {
                    msg.contains("401") -> "Key 无效（401）"
                    msg.contains("402") -> "余额不足（402）"
                    msg.contains("403") -> "没有权限（403）"
                    msg.contains("429") -> "额度用完或太频繁（429）"
                    msg.contains("timeout", true) -> "请求超时"
                    else -> "${t.javaClass.simpleName}: ${msg.take(80)}"
                }
                MtResult("", ok = false, error = friendly, elapsedMs = System.currentTimeMillis() - started)
            }
        }

    private fun post(payload: JSONObject): JSONObject {
        val request = Request.Builder()
            .url("${provider.baseUrl.trimEnd('/')}/chat/completions")
            .addHeader("Authorization", "Bearer $apiKey")
            .addHeader("Content-Type", "application/json")
            .post(payload.toString().toRequestBody(JSON_MEDIA))
            .build()
        client.newCall(request).execute().use { resp ->
            val body = resp.body?.string().orEmpty()
            if (!resp.isSuccessful) throw IllegalStateException("HTTP ${resp.code}: ${body.take(120)}")
            return JSONObject(body)
        }
    }

    override fun close() {
        try {
            client.dispatcher.executorService.shutdown()
            client.connectionPool.evictAll()
        } catch (t: Throwable) {
            Log.w(TAG, "关闭连接池异常（忽略）", t)
        }
    }

    private fun clean(raw: String): String {
        var t = raw.trim()
        t = t.replace(Regex("^(翻译[:：]|译文[:：]|Translation[:：])\\s*"), "")
        if (t.length >= 2 && t.first() == t.last() && t.first() in "\"'“”‘’「」") {
            t = t.substring(1, t.length - 1)
        }
        return t.trim()
    }

    companion object {
        private const val TAG = "ChatTranslator"
        private val JSON_MEDIA = "application/json; charset=utf-8".toMediaType()

        /** 与 Windows 端一致的基础提示词 */
        private val ROOT_PROMPT = """
            你是直播实时字幕的翻译引擎。把用户给你的外语句子翻译成{target}。

            规则：
            1. 只输出译文本身，不要任何解释、引号、前后缀或"翻译："字样。
            2. 保留口语感。直播里的话是随口说的，译文要像中国人顺口说的话，不要书面腔。
            3. 人名、游戏名、产品名、专有名词保留原文或通用译法，前后保持一致。
            4. 数字、时间、比分、装备名要准确。
            5. 如果句子明显没说完（被截断），就照现有内容译，不要补齐、不要猜。
            6. 语气词、笑声、脏话按中文习惯自然处理，不要过滤掉情绪。
            7. **输入来自语音识别，可能有同音字、片假名拼写或专有名词错误**。请结合上下文判断
               说话人真正想表达的意思，按最合理的理解翻译，不要把明显是识别错误的部分生硬直译成
               读不通的中文。但也不要凭空添加原文没有的信息。
        """.trimIndent()
    }
}

/**
 * 多翻译引擎的自动降级。
 *
 * 云端服务会宕机（千问出现过整晚不可用）。只配一家时宕机字幕就彻底没了，
 * 所以这里在某家失败时自动轮到下一个已配 Key 的引擎，并记住哪家坏了
 * （避免每句都去撞一次已挂掉的服务）。
 */
class FallbackTranslator(
    private val chain: List<ChatTranslator>,
) : Translator {

    constructor(providers: List<MtProvider>, keyOf: (String) -> String, sourceLangCode: String,
                targetLang: String, sendLanguageHint: Boolean) : this(
        providers.map { ChatTranslator(it, keyOf(it.keyName), sourceLangCode, targetLang, sendLanguageHint) }
    )

    private val failures = IntArray(chain.size)
    private var activeIndex = 0

    override val label: String get() = chain.getOrNull(activeIndex)?.label ?: "无可用引擎"

    /** 当前实际在用的引擎（用于界面显示，降级后能看出来换了） */
    val activeLabel: String get() = label

    override fun warmup() {
        // 每一家都预热：既建立语言预期，也顺便探一次可用性
        chain.forEach { it.warmup() }
    }

    override suspend fun translate(text: String, context: List<Pair<String, String>>): MtResult {
        val order = buildList {
            add(activeIndex)
            chain.indices.forEach { if (it != activeIndex) add(it) }
        }
        var last: MtResult? = null
        for (idx in order) {
            val t = chain.getOrNull(idx) ?: continue
            if (failures[idx] >= DEAD_AFTER) continue
            val r = t.translate(text, context)
            if (r.ok) {
                if (idx != activeIndex) Log.i(TAG, "已切换到 ${t.label}")
                activeIndex = idx
                failures[idx] = 0
                return r
            }
            last = r
            failures[idx] += 1
            Log.w(TAG, "${t.label} 失败：${r.error}")
        }
        // 全都失败：清空计数再试第一家，别让字幕彻底空掉
        failures.fill(0)
        chain.firstOrNull()?.let {
            val r = it.translate(text, context)
            if (r.ok) {
                activeIndex = 0
                return r
            }
            last = r
        }
        return last ?: MtResult("", ok = false, error = "所有翻译引擎均不可用")
    }

    override fun close() {
        chain.forEach { it.close() }
    }

    companion object {
        private const val TAG = "FallbackTranslator"
        /** 连续失败几次就判定这家已挂，本轮不再尝试 */
        private const val DEAD_AFTER = 2
    }
}
