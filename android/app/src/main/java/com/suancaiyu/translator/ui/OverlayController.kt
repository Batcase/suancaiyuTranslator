package com.suancaiyu.translator.ui

import android.content.Context
import android.content.Intent
import android.graphics.PixelFormat
import android.net.Uri
import android.os.Build
import android.provider.Settings
import android.util.Log
import android.view.Gravity
import android.view.MotionEvent
import android.view.View
import android.view.WindowManager
import com.suancaiyu.translator.data.SubtitleUiState

/**
 * 悬浮窗控制器 —— 把字幕挂到别的 App 前面。
 *
 * ## 曾经踩过的坑（导致"给了权限也不显示"）
 *
 * 旧实现在最前面做了 `if (!canDraw(context)) return false`，把
 * `Settings.canDrawOverlays()` 当成**闸门**。这个判断在某些 ROM / 分身 /
 * 多用户 / 授权状态不同步的情况下会返回 false，于是：
 *
 * * 窗口**永远不会被创建**
 * * 用户明明在系统设置里打开了"显示在其他应用上层"
 * * 界面却只显示一句"没有悬浮窗权限"，或者干脆什么都不显示
 *
 * **改为"先尝试添加，失败再判定权限"**：
 * `WindowManager.addView` 在真的没有权限时会抛 `SecurityException`，
 * 这是最权威的判据（系统自己检查的，不会和 ROM 的状态缓存不一致）。
 * `canDrawOverlays()` 只用来给用户提示，不再当闸门。
 *
 * ## 其他必须做对的点
 *
 * * 窗口类型必须 `TYPE_APPLICATION_OVERLAY`（Android 8 起 TYPE_PHONE 等已禁）
 * * 必须 `FLAG_NOT_FOCUSABLE`，否则会抢走直播 App 的输入焦点
 * * 鼠标穿透用 `FLAG_NOT_TOUCHABLE`（对应 Windows 端的 WS_EX_TRANSPARENT）
 * * **不要用 `FLAG_LAYOUT_NO_LIMITS`** —— 实测在有底部导航栏/手势条的设备上，
 *   它会让 `Gravity.BOTTOM + y` 的窗口被推到屏幕外，看起来就是"没显示"
 */
class OverlayController(private val context: Context) {

    private val windowManager =
        context.getSystemService(Context.WINDOW_SERVICE) as WindowManager
    private var view: SubtitleOverlayView? = null
    private var params: WindowManager.LayoutParams? = null

    /** 是否已添加 */
    val isShowing: Boolean get() = view != null

    /** 最近一次失败的原因（给界面显示，便于用户判断到底卡在哪） */
    var lastError: String = ""
        private set

    companion object {
        private const val TAG = "OverlayController"

        /** 悬浮窗权限是否已授予（**仅供参考**，不作为能否添加窗口的判据） */
        fun canDraw(context: Context): Boolean = try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
                Settings.canDrawOverlays(context)
            } else {
                true
            }
        } catch (t: Throwable) {
            // 个别 ROM 上这个方法本身会抛，保守返回 false（但不会再挡住添加尝试）
            Log.w(TAG, "canDrawOverlays 调用异常", t)
            false
        }

        /** 跳到系统的"显示在其他应用上层"授权页 */
        fun openPermissionSettings(context: Context) {
            val intents = listOf(
                Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION, Uri.parse("package:${context.packageName}")),
                Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION),
                Intent(Settings.ACTION_MANAGE_APPLICATIONS_SETTINGS),
            )
            for (i in intents) {
                i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                try {
                    context.startActivity(i)
                    return
                } catch (t: Throwable) {
                    Log.w(TAG, "打开授权页失败，换下一个", t)
                }
            }
            Log.e(TAG, "无法打开悬浮窗授权页，请手动到系统设置里开启")
        }
    }

    fun show(showPanel: Boolean, maxLines: Int, bottomOffsetPx: Int, locked: Boolean): Boolean {
        lastError = ""

        if (view != null) {
            // 已经在了：更新样式即可
            view?.showPanel = showPanel
            view?.maxLines = maxLines
            return true
        }

        val granted = canDraw(context)
        Log.i(TAG, "尝试显示悬浮窗：canDrawOverlays=$granted  locked=$locked")

        return try {
            val v = SubtitleOverlayView(context).apply {
                this.showPanel = showPanel
                this.maxLines = maxLines
            }
            val p = WindowManager.LayoutParams(
                WindowManager.LayoutParams.MATCH_PARENT,
                WindowManager.LayoutParams.WRAP_CONTENT,
                // Android 8+ 必须用 OVERLAY 类型
                WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY,
                WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or
                    WindowManager.LayoutParams.FLAG_NOT_TOUCH_MODAL,
                PixelFormat.TRANSLUCENT,
            ).apply {
                gravity = Gravity.BOTTOM or Gravity.CENTER_HORIZONTAL
                y = if (bottomOffsetPx > 0) bottomOffsetPx else defaultBottomOffset()
                // 默认穿透：字幕不该挡直播操作
                if (!locked) flags = flags or WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE
            }

            // ★ 这里才是真正的权限判据：没权限时系统会抛 SecurityException
            windowManager.addView(v, p)
            attachDrag(v, p)
            view = v
            params = p
            Log.i(TAG, "悬浮窗已添加（canDrawOverlays=$granted）")

            // 添加之后自检一次：`addView` 不抛异常**不代表窗口真的看得见**。
            // 个别 ROM 会接受窗口但把它放在屏幕外。把实测结果记下来，
            // 出问题时能在界面/日志里看到确切原因，而不是只有"没显示"。
            v.post {
                try {
                    val vis = v.visibility == View.VISIBLE
                    val w = v.width
                    val h = v.height
                    Log.i(TAG, "悬浮窗自检：visible=$vis size=${w}x$h alpha=${v.alpha} isShown=${v.isShown}")
                    if (w <= 0 || h <= 0) {
                        lastError = "悬浮窗尺寸异常（${w}x$h），可能被系统裁剪"
                        Log.w(TAG, lastError)
                    } else if (!v.isShown) {
                        lastError = "悬浮窗已创建但被系统隐藏（该 ROM 可能限制后台悬浮窗）"
                        Log.w(TAG, lastError)
                    }
                } catch (t: Throwable) {
                    Log.w(TAG, "悬浮窗自检异常", t)
                }
            }
            true
        } catch (e: SecurityException) {
            // 真正的权限问题。注意提示里要带上系统设置里的名字，否则用户找不到
            lastError = "系统拒绝了悬浮窗（请在「设置 → 应用 → 显示在其他应用上层」里开启）"
            Log.e(TAG, "添加悬浮窗被拒绝：$lastError", e)
            view = null
            params = null
            false
        } catch (t: Throwable) {
            // 其他失败：ROM 拦截、窗口类型不被允许等
            lastError = "添加悬浮窗失败：${t.javaClass.simpleName}"
            Log.e(TAG, "添加悬浮窗失败", t)
            view = null
            params = null
            false
        }
    }

    private fun defaultBottomOffset(): Int {
        // 默认贴在底边往上 10% 处，避开直播 App 自己的控制条与系统手势条
        val h = context.resources.displayMetrics.heightPixels
        return (h * 0.10f).toInt()
    }

    /** 未锁定时可拖动，松手记住位置。 */
    private fun attachDrag(v: View, p: WindowManager.LayoutParams) {
        var downY = 0f
        var startY = 0
        v.setOnTouchListener { _, event ->
            when (event.action) {
                MotionEvent.ACTION_DOWN -> {
                    downY = event.rawY
                    startY = p.y
                    true
                }
                MotionEvent.ACTION_MOVE -> {
                    p.y = (startY + (event.rawY - downY)).toInt().coerceAtLeast(0)
                    runCatching { windowManager.updateViewLayout(v, p) }
                    true
                }
                else -> false
            }
        }
    }

    /** 切换鼠标穿透。 */
    fun setClickThrough(clickThrough: Boolean) {
        val p = params ?: return
        val v = view ?: return
        p.flags = if (clickThrough) {
            p.flags or WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE
        } else {
            p.flags and WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE.inv()
        }
        updateSafely(v, p)
    }

    fun setLocked(locked: Boolean) = setClickThrough(!locked)

    /**
     * 重排窗口（例如用户改了字号/行数）。
     * 用 `updateViewLayout` 而不是"先移除再添加"——后者会闪一下。
     */
    fun refresh() {
        val v = view ?: return
        val p = params ?: return
        p.y = if (p.y > 0) p.y else defaultBottomOffset()
        updateSafely(v, p)
    }

    private fun updateSafely(v: View, p: WindowManager.LayoutParams) {
        try {
            windowManager.updateViewLayout(v, p)
        } catch (t: Throwable) {
            Log.w(TAG, "更新悬浮窗失败（可能是权限被撤了）", t)
            lastError = "更新悬浮窗失败：${t.javaClass.simpleName}"
        }
    }

    fun currentBottomOffset(): Int = params?.y ?: 0

    fun render(state: SubtitleUiState) {
        view?.render(state)
    }

    fun hide() {
        val v = view ?: return
        try {
            windowManager.removeView(v)
        } catch (t: Throwable) {
            Log.w(TAG, "移除悬浮窗失败（忽略）", t)
        }
        view = null
        params = null
    }
}
