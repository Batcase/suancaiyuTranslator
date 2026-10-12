package com.suancaiyu.translator.ui

import android.app.Activity
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.media.projection.MediaProjectionManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.Settings as SysSettings
import android.text.InputType
import android.util.Log
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.widget.Button
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.lifecycleScope
import androidx.lifecycle.repeatOnLifecycle
import com.suancaiyu.translator.AsrProvider
import com.suancaiyu.translator.KeyName
import com.suancaiyu.translator.Languages
import com.suancaiyu.translator.MtProvider
import com.suancaiyu.translator.R
import com.suancaiyu.translator.Settings
import com.suancaiyu.translator.data.RunState
import com.suancaiyu.translator.data.SubtitleUiState
import com.suancaiyu.translator.service.TranslatorService
import kotlinx.coroutines.launch

/**
 * 主界面：配置 + 内嵌字幕预览 + 启动/停止。
 *
 * 为什么用代码建布局而不写 XML：
 *   这个界面的控件较多且需要按"是否已配置"动态显隐，用 XML 要配一堆 id 和
 *   findViewById，代码里直接构造更直观、改动也更集中（少一个出错面）。
 *
 * 权限流程（Android 的硬性要求，顺序不能乱）：
 *   1. 通知权限（Android 13+，可选但建议）
 *   2. 悬浮窗权限 —— **无法用对话框申请**，只能跳系统设置页
 *   3. 麦克风权限（仅麦克风模式需要）
 *   4. MediaProjection 授权 —— 系统弹窗，拿到的 resultCode/data 要立刻交给前台服务
 */
class MainActivity : AppCompatActivity() {

    private lateinit var settings: Settings
    private lateinit var overlayPreview: SubtitleOverlayView
    private lateinit var statusText: TextView
    private lateinit var routeText: TextView
    private lateinit var startButton: Button
    private lateinit var stopButton: Button
    private lateinit var overlayWarn: TextView
    private lateinit var setupPanel: LinearLayout
    /** Key 输入框：keyName -> EditText（在 buildSetupPanel 里填） */
    private val keyFields: MutableMap<String, EditText> = mutableMapOf()

    private val projectionLauncher = registerForActivityResult(
        androidx.activity.result.contract.ActivityResultContracts.StartActivityForResult()
    ) { result ->
        if (result.resultCode == Activity.RESULT_OK && result.data != null) {
            startupNote = ""
            startServiceWithProjection(result.resultCode, result.data!!)
        } else {
            // ★ 不要只弹个一闪而过的 toast：用户会以为"点了没反应"。
            //   记下来并**持久显示**在状态行上。
            startupNote = "已取消录制授权，内录无法启动（可改用麦克风模式）"
            Log.w(TAG, startupNote)
            toast(startupNote)
            refreshUi()
        }
    }

    /** 麦克风权限申请：授权后直接进麦克风模式 */
    private val micPermissionLauncher = registerForActivityResult(
        androidx.activity.result.contract.ActivityResultContracts.RequestPermission()
    ) { granted ->
        if (granted) {
            startServiceWithMic()
        } else {
            toast("没有录音权限，麦克风模式用不了")
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        settings = Settings(this)
        setContentView(buildRoot())
        requestNotificationPermissionIfNeeded()
        refreshUi()
    }

    override fun onResume() {
        super.onResume()
        // 从系统设置页回来后要重新检查悬浮窗权限
        refreshOverlayWarning()
    }

    // ===============================================================
    // 界面构建
    // ===============================================================

    private fun buildRoot(): View {
        val scroll = ScrollView(this).apply {
            setBackgroundColor(Color.parseColor("#15181C"))
            isFillViewport = true
        }
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(18), dp(18), dp(18), dp(24))
        }
        scroll.addView(root)

        // 标题
        root.addView(text(getString(R.string.app_name), 22f, "#E8EAED", bold = true))
        root.addView(text(getString(R.string.app_subtitle), 12f, "#9AA0A6").also {
            it.setPadding(0, dp(2), 0, dp(12))
        })

        // 悬浮窗权限警告（没有权限时字幕挂不上去）
        overlayWarn = text(getString(R.string.main_overlay_permission), 12f, "#FFB020").apply {
            visibility = View.GONE
            setPadding(0, 0, 0, dp(8))
            setOnClickListener { openOverlaySettings() }
        }
        root.addView(overlayWarn)

        // ---- 内嵌字幕预览（复用与悬浮窗同一个 View，所见即所得）----
        val previewCard = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundColor(Color.parseColor("#1E2227"))
            addView(text("字幕预览", 13f, "#9AA0A6").also { it.setPadding(dp(12), dp(10), 0, dp(4)) })
        }
        overlayPreview = SubtitleOverlayView(this).apply {
            maxLines = settings.maxLines
            showPanel = true
        }
        previewCard.addView(
            overlayPreview,
            LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT
            )
        )
        root.addView(previewCard, matchWidthBottom(dp(12)))

        // ---- 启动 / 停止 ----
        val buttons = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
        startButton = primaryButton(getString(R.string.main_start)) { onStartClicked() }
        stopButton = dangerButton(getString(R.string.main_stop)) { onStopClicked() }
        buttons.addView(startButton, weight())
        buttons.addView(stopButton, weight(start = dp(10)))
        root.addView(buttons)
        root.addView(text(getString(R.string.main_projection_first), 11f, "#9AA0A6").also {
            it.setPadding(0, dp(8), 0, dp(6))
        })

        // ---- 麦克风模式（内录采不到声音时的兜底）----
        //
        // 什么时候需要：被采集的 App 声明了不允许录（DRM 内容）、
        // 或者**换了输出设备**（典型就是插上蓝牙耳机）导致回环拿不到数据。
        // 这时对着喇叭用麦克风录也能用，音质差但总比没字幕强。
        root.addView(
            secondaryButton("改用麦克风模式（内录没声音时用）") { onStartMicClicked() }
        )
        root.addView(text("内录采不到声音时试试这个：音质差一些，但蓝牙耳机场景能用", 11f, "#6B7280").also {
            it.setPadding(0, dp(4), 0, dp(14))
        })

        // ---- 状态与路由信息 ----
        statusText = text(getString(R.string.main_idle), 14f, "#9AA0A6", bold = true)
        root.addView(statusText)
        routeText = text("", 11f, "#9AA0A6").apply { setPadding(0, dp(4), 0, dp(14)) }
        root.addView(routeText)

        // ---- 配置面板（首次使用展开，之后可折叠）----
        root.addView(divider())
        setupPanel = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        buildSetupPanel(setupPanel)
        root.addView(setupPanel)

        root.addView(text(getString(R.string.note_capture_policy), 11f, "#6B7280").also {
            it.setPadding(0, dp(16), 0, 0)
        })

        return scroll
    }

    private fun buildSetupPanel(parent: LinearLayout) {
        parent.addView(sectionTitle("① 翻译 Key"))
        parent.addView(hint("DeepSeek / 通义千问 / Kimi 任填一家即可，多填可自动降级"))
        MtProvider.entries.forEach { p ->
            addKeyRow(parent, p.keyName, p.label.substringBefore(" · "))
        }

        parent.addView(sectionTitle("② 语音识别 Key"))
        parent.addView(hint("推荐阿里云 Paraformer（国内直连）；多填一家宕机不中断"))
        AsrProvider.entries.filter { it.keyName != null }.forEach { p ->
            addKeyRow(parent, p.keyName!!, p.label.substringBefore(" · "))
        }

        parent.addView(sectionTitle("③ 翻译方向"))
        val langs = Languages.ALL.joinToString(" / ") { it.display }
        parent.addView(hint("源语言：$langs"))
        val langRow = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
        Languages.ALL.forEach { item ->
            langRow.addView(chip(item.display, settings.sourceLang == item.code) {
                settings.sourceLang = item.code
                refreshUi()
            }, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f).also {
                it.rightMargin = dp(6)
            })
        }
        parent.addView(langRow)

        val save = secondaryButton("保存配置") {
            saveKeys()
            toast(getString(R.string.settings_saved))
            refreshUi()
        }
        parent.addView(save, matchWidthTop(dp(12)))
    }

    private fun addKeyRow(parent: LinearLayout, keyName: String, label: String) {
        val row = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            setPadding(0, dp(6), 0, 0)
        }
        row.addView(
            text(label, 12f, if (settings.hasKey(keyName)) "#3DD68C" else "#9AA0A6"),
            LinearLayout.LayoutParams(dp(84), ViewGroup.LayoutParams.WRAP_CONTENT)
        )
        val input = EditText(this).apply {
            hint = if (settings.hasKey(keyName)) settings.maskedKey(keyName) else "粘贴 Key"
            setText(settings.key(keyName))
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD
            textSize = 12f
            setTextColor(Color.parseColor("#E8EAED"))
            setHintTextColor(Color.parseColor("#6B7280"))
            setBackgroundColor(Color.parseColor("#0F1216"))
            setPadding(dp(8), dp(8), dp(8), dp(8))
        }
        keyFields[keyName] = input
        row.addView(input, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
        parent.addView(row)
    }

    private fun saveKeys() {
        keyFields.forEach { (name, field) ->
            val v = field.text.toString().trim()
            if (v.isNotEmpty()) settings.setKey(name, v)
        }
    }

    // ===============================================================
    // 启动 / 停止
    // ===============================================================

    private fun onStartClicked() {
        saveKeys()

        if (!settings.isConfigured()) {
            toast("请先填写 Key：至少一家翻译 + 一家识别")
            return
        }
        // ★ 刻意不在这里用 canDrawOverlays() 拦路。
        //
        // 旧实现在这里 `if (!canDraw) { 提示 + return }`，结果是：只要那个判断
        // 返回 false（某些 ROM/分身/多用户下即使授权了也会返回 false），
        // **整个翻译都启动不了**，用户看到的就是"给了权限也没用"。
        //
        // 现在改成：没授权就提示一次并给个跳转入口，但**照样启动**。
        // 真的没有权限时，OverlayController 里的 addView 会抛
        // SecurityException，那时再把准确原因显示到界面上。
        if (!OverlayController.canDraw(this)) {
            toast("没检测到悬浮窗权限，将先启动翻译；看不到字幕再点上方提示去授权")
        }

        // 启动内录（系统会弹"是否允许录制/投射"）
        val mgr = getSystemService(Context.MEDIA_PROJECTION_SERVICE) as MediaProjectionManager
        try {
            projectionLauncher.launch(mgr.createScreenCaptureIntent())
            // 说明白下一步该干什么 —— 不给提示的话用户会以为没反应
            startButton.postDelayed({ toast("请在系统弹窗里选「立即开始」") }, 900)
        } catch (t: Throwable) {
            // 个别 ROM 上这个 Intent 可能因为缺少某个组件/权限而抛异常，
            // 表现为"点了没任何反应"。把原因直接告诉用户。
            toast("无法打开录制授权弹窗：${t.javaClass.simpleName}，请改用麦克风模式")
            Log.e(TAG, "createScreenCaptureIntent 启动失败", t)
        }
    }

    /** 供日志过滤用 */
    private val TAG = "MainActivity"

    private fun startServiceWithProjection(resultCode: Int, data: Intent) {
        val intent = Intent(this, TranslatorService::class.java).apply {
            action = TranslatorService.ACTION_START
            putExtra(TranslatorService.EXTRA_RESULT_CODE, resultCode)
            putExtra(TranslatorService.EXTRA_RESULT_DATA, data)
            putExtra(TranslatorService.EXTRA_USE_MIC, false)
        }
        ContextCompat.startForegroundService(this, intent)
        toast("已启动，字幕会挂在屏幕下方")
        startButton.postDelayed({ refreshUi() }, 800)
    }

    /**
     * 麦克风模式：不依赖 MediaProjection，直接录麦克风。
     *
     * 用途：内录采不到声音时的兜底。典型场景是被采集的 App 禁止录制
     * （DRM 内容），或者**换了输出设备**（插蓝牙耳机）导致回环拿不到数据。
     */
    private fun onStartMicClicked() {
        saveKeys()
        if (!settings.isConfigured()) {
            toast("请先填写 Key：至少一家翻译 + 一家识别")
            return
        }
        val perm = android.Manifest.permission.RECORD_AUDIO
        if (ContextCompat.checkSelfPermission(this, perm) != PackageManager.PERMISSION_GRANTED) {
            micPermissionLauncher.launch(perm)
            return
        }
        startServiceWithMic()
    }

    private fun startServiceWithMic() {
        startupNote = ""
        val intent = Intent(this, TranslatorService::class.java).apply {
            action = TranslatorService.ACTION_START
            putExtra(TranslatorService.EXTRA_USE_MIC, true)
        }
        ContextCompat.startForegroundService(this, intent)
        toast("麦克风模式已启动（对着喇叭/耳机听筒录）")
        startButton.postDelayed({ refreshUi() }, 800)
    }

    private fun onStopClicked() {
        startService(
            Intent(this, TranslatorService::class.java).setAction(TranslatorService.ACTION_STOP)
        )
        startButton.postDelayed({ refreshUi() }, 400)
    }

    private fun openOverlaySettings() {
        val intent = Intent(
            SysSettings.ACTION_MANAGE_OVERLAY_PERMISSION,
            Uri.parse("package:$packageName")
        )
        runCatching { startActivity(intent) }
            .onFailure { toast("无法打开权限设置页，请手动到系统设置里开启") }
    }

    private fun requestNotificationPermissionIfNeeded() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) return
        val perm = android.Manifest.permission.POST_NOTIFICATIONS
        if (ContextCompat.checkSelfPermission(this, perm) != PackageManager.PERMISSION_GRANTED) {
            ActivityCompat.requestPermissions(this, arrayOf(perm), 1001)
        }
    }

    // ===============================================================
    // 状态刷新
    // ===============================================================

    private var collecting = false

    /**
     * 启动过程中的说明/失败原因，**持久显示**在状态行。
     *
     * 为什么需要：之前失败原因只在 toast 里一闪而过（或被服务里的
     * `setState(IDLE, "")` 清掉），用户看到的就是"点了没反应 + 未启动"，
     * 完全无从判断。现在把原因留住并显示出来。
     */
    private var startupNote: String = ""

    private fun refreshUi() {
        routeText.text = buildRouteText()
        refreshOverlayWarning()

        val running = TranslatorService.running
        startButton.isEnabled = !running
        stopButton.isEnabled = running
        if (!running) {
            // 没在跑：优先显示最近一次启动失败/被取消的原因，而不是干巴巴一句"未启动"。
            // 用户反馈"没有任何提示、只显示未启动"就是这个原因。
            statusText.text = startupNote.ifEmpty { getString(R.string.main_idle) }
            statusText.setTextColor(
                Color.parseColor(if (startupNote.isNotEmpty()) "#FFB020" else "#9AA0A6")
            )
        }

        // 订阅字幕状态。
        //
        // store 现在是**进程级单例**（首次访问就创建），不再是"服务起来之前为 null"。
        // 旧代码写的是 `val store = TranslatorService.store; if (store != null && !collecting)`，
        // 而 Activity 首次 refreshUi 时服务还没启动 → store 为 null → collecting 一直是 false
        // → 之后即使服务起来了也**再也不订阅**，界面永远显示"未启动"。
        // 这个坑就是用户反馈的"App 内未生效"。
        if (!collecting) {
            collecting = true
            lifecycleScope.launch {
                repeatOnLifecycle(Lifecycle.State.STARTED) {
                    TranslatorService.store.state.collect { s -> renderState(s) }
                }
            }
        }
    }

    private fun renderState(s: SubtitleUiState) {
        overlayPreview.maxLines = settings.maxLines
        overlayPreview.render(s)
        statusText.text = when (s.state) {
            RunState.IDLE -> getString(R.string.main_idle)
            RunState.STARTING -> getString(R.string.state_starting)
            RunState.RUNNING -> buildString {
                append(getString(R.string.state_running))
                if (s.activeAsr.isNotEmpty()) append(" · ${s.activeAsr}")
                if (s.e2eMs > 0) append(" · ${s.e2eMs}ms")
                if (s.errors > 0) append(" · 错误×${s.errors}")
            }
            RunState.PAUSED -> getString(R.string.state_paused)
            RunState.ERROR -> "出错了：${s.detail}"
        }
        statusText.setTextColor(
            Color.parseColor(
                when {
                    s.state == RunState.ERROR -> "#FF6B6B"
                    s.errors > 0 -> "#FFB020"
                    s.state == RunState.RUNNING -> "#3DD68C"
                    else -> "#9AA0A6"
                }
            )
        )
        overlayWarn.visibility = if (OverlayController.canDraw(this)) View.GONE else View.VISIBLE
    }

    private fun refreshOverlayWarning() {
        // 提示条只在"系统说没权限"时显示，但**任何情况下都保持可点**——
        // 有些 ROM 上 canDrawOverlays() 会误报已授权，用户仍需要一个入口去检查。
        overlayWarn.visibility = if (OverlayController.canDraw(this)) View.GONE else View.VISIBLE
    }

    private fun buildRouteText(): String {
        val asr = AsrProvider.chain(settings.asrProvider) { settings.hasKey(it) }
            .joinToString(" → ") { it.label.substringBefore(" · ") }
        val mt = MtProvider.chain(settings.mtProvider) { settings.hasKey(it) }
            .joinToString(" → ") { it.label.substringBefore(" · ") }
        val src = Languages.displayOf(settings.sourceLang)
        return "识别：$asr\n翻译：$mt\n语言：$src → ${settings.targetLang}"
    }

    // ===============================================================
    // 小工具
    // ===============================================================

    private fun dp(v: Int): Int = (v * resources.displayMetrics.density).toInt()

    private fun text(s: String, sizeSp: Float, color: String, bold: Boolean = false): TextView =
        TextView(this).apply {
            text = s
            textSize = sizeSp
            setTextColor(Color.parseColor(color))
            if (bold) setTypeface(typeface, android.graphics.Typeface.BOLD)
            includeFontPadding = false
        }

    private fun hint(s: String) = text(s, 11f, "#6B7280").also { it.setPadding(0, dp(2), 0, dp(6)) }

    private fun sectionTitle(s: String) = text(s, 14f, "#E8EAED", bold = true).also {
        it.setPadding(0, dp(18), 0, dp(6))
    }

    private fun divider() = View(this).apply {
        setBackgroundColor(Color.parseColor("#33FFFFFF"))
        layoutParams = LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, 1
        ).also { it.topMargin = dp(6) }
    }

    private fun primaryButton(label: String, onClick: () -> Unit) =
        Button(this).apply {
            text = label
            textSize = 15f
            setTextColor(Color.WHITE)
            setBackgroundColor(Color.parseColor("#4C8DFF"))
            setOnClickListener { onClick() }
            setPadding(0, dp(12), 0, dp(12))
        }

    private fun secondaryButton(label: String, onClick: () -> Unit) =
        Button(this).apply {
            text = label
            textSize = 13f
            setTextColor(Color.parseColor("#E8EAED"))
            setBackgroundColor(Color.parseColor("#1E2227"))
            setOnClickListener { onClick() }
        }

    private fun dangerButton(label: String, onClick: () -> Unit) =
        Button(this).apply {
            text = label
            textSize = 15f
            setTextColor(Color.parseColor("#FF6B6B"))
            setBackgroundColor(Color.parseColor("#3A2224"))
            setOnClickListener { onClick() }
            setPadding(0, dp(12), 0, dp(12))
        }

    private fun chip(label: String, selected: Boolean, onClick: () -> Unit) =
        TextView(this).apply {
            text = label
            textSize = 12f
            gravity = Gravity.CENTER
            setPadding(0, dp(8), 0, dp(8))
            setTextColor(if (selected) Color.WHITE else Color.parseColor("#9AA0A6"))
            setBackgroundColor(Color.parseColor(if (selected) "#4C8DFF" else "#1E2227"))
            setOnClickListener { onClick() }
        }

    private fun weight(start: Int = 0) = LinearLayout.LayoutParams(
        0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f
    ).also { it.leftMargin = start }

    private fun matchWidthBottom(bottom: Int) = LinearLayout.LayoutParams(
        ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT
    ).also { it.bottomMargin = bottom }

    private fun matchWidthTop(top: Int) = LinearLayout.LayoutParams(
        ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT
    ).also { it.topMargin = top }

    private fun toast(msg: String) = Toast.makeText(this, msg, Toast.LENGTH_SHORT).show()

    override fun onRequestPermissionsResult(
        requestCode: Int,
        permissions: Array<out String>,
        grantResults: IntArray,
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == 1001) {
            val granted = grantResults.isNotEmpty() && grantResults[0] == PackageManager.PERMISSION_GRANTED
            if (!granted) toast("没有通知权限，服务在后台可能被系统限制")
        }
    }
}
