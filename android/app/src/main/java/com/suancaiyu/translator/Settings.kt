package com.suancaiyu.translator

import android.content.Context
import android.content.SharedPreferences

/**
 * 本地设置与密钥存储。
 *
 * 为什么不用 DataStore / EncryptedSharedPreferences：
 *   * DataStore 要多引一个依赖，而这里的数据量极小（十来个字符串）
 *   * EncryptedSharedPreferences 已在 androidx.security 里被标记废弃，
 *     且它在部分国产 ROM 上有 KeyStore 兼容问题（会直接抛异常导致 App 起不来）
 *
 * 折中方案：普通设置用默认 SharedPreferences；**密钥单独放一个文件**，
 * 并在备份规则里排除（见 res/xml/backup_rules.xml），避免被云备份带走。
 */
class Settings(context: Context) {

    private val prefs: SharedPreferences =
        context.getSharedPreferences(PREF_NAME, Context.MODE_PRIVATE)

    /** 单独的密钥文件，不参与备份 */
    private val keys: SharedPreferences =
        context.getSharedPreferences(KEY_PREF_NAME, Context.MODE_PRIVATE)

    // ---------------- 密钥 ----------------

    fun key(name: String): String = keys.getString(name, "").orEmpty()

    fun setKey(name: String, value: String) {
        keys.edit().putString(name, value.trim()).apply()
    }

    fun hasKey(name: String?): Boolean = !name.isNullOrBlank() && key(name).isNotBlank()

    /** 至少有一家翻译 Key + 一家识别 Key（本地识别除外）才算配置完成 */
    fun isConfigured(): Boolean =
        MtProvider.entries.any { hasKey(it.keyName) } &&
            AsrProvider.entries.any { it.keyName == null || hasKey(it.keyName) }

    /** 脱敏展示 */
    fun maskedKey(name: String): String {
        val v = key(name)
        if (v.isBlank()) return "未配置"
        return if (v.length <= 10) "${v.first()}…${v.last()}" else "${v.take(6)}…${v.takeLast(4)}"
    }

    // ---------------- 引擎与语言 ----------------

    var asrProvider: AsrProvider
        get() = AsrProvider.byId(prefs.getString(KEY_ASR_PROVIDER, null))
        set(v) = prefs.edit().putString(KEY_ASR_PROVIDER, v.id).apply()

    var mtProvider: MtProvider
        get() = MtProvider.byId(prefs.getString(KEY_MT_PROVIDER, null))
        set(v) = prefs.edit().putString(KEY_MT_PROVIDER, v.id).apply()

    var sourceLang: String
        get() = prefs.getString(KEY_SOURCE_LANG, "ja") ?: "ja"
        set(v) = prefs.edit().putString(KEY_SOURCE_LANG, v).apply()

    var targetLang: String
        get() = prefs.getString(KEY_TARGET_LANG, "简体中文") ?: "简体中文"
        set(v) = prefs.edit().putString(KEY_TARGET_LANG, v).apply()

    /** 是否提前告知翻译 AI「主要语言」（对应 Windows 端的 send_language_hint） */
    var sendLanguageHint: Boolean
        get() = prefs.getBoolean(KEY_LANG_HINT, true)
        set(v) = prefs.edit().putBoolean(KEY_LANG_HINT, v).apply()

    // ---------------- 字幕窗 ----------------

    var overlayPanel: Boolean
        get() = prefs.getBoolean(KEY_OVERLAY_PANEL, false)
        set(v) = prefs.edit().putBoolean(KEY_OVERLAY_PANEL, v).apply()

    var overlayLocked: Boolean
        get() = prefs.getBoolean(KEY_OVERLAY_LOCKED, false)
        set(v) = prefs.edit().putBoolean(KEY_OVERLAY_LOCKED, v).apply()

    /** 字幕窗纵向位置（相对底边的偏移，px）；0 = 用默认值 */
    var overlayBottomOffset: Int
        get() = prefs.getInt(KEY_OVERLAY_OFFSET, 0)
        set(v) = prefs.edit().putInt(KEY_OVERLAY_OFFSET, v).apply()

    var maxLines: Int
        get() = prefs.getInt(KEY_MAX_LINES, 2)
        set(v) = prefs.edit().putInt(KEY_MAX_LINES, v.coerceIn(1, 4)).apply()

    companion object {
        private const val PREF_NAME = "settings"
        private const val KEY_PREF_NAME = "keys"   // 备份规则里已排除

        private const val KEY_ASR_PROVIDER = "asr_provider"
        private const val KEY_MT_PROVIDER = "mt_provider"
        private const val KEY_SOURCE_LANG = "source_lang"
        private const val KEY_TARGET_LANG = "target_lang"
        private const val KEY_LANG_HINT = "send_language_hint"
        private const val KEY_OVERLAY_PANEL = "overlay_panel"
        private const val KEY_OVERLAY_LOCKED = "overlay_locked"
        private const val KEY_OVERLAY_OFFSET = "overlay_bottom_offset"
        private const val KEY_MAX_LINES = "max_lines"
    }
}
