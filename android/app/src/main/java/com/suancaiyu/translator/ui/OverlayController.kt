package com.suancaiyu.translator.ui

import android.content.Context
import android.graphics.PixelFormat
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
 * 几个必须做对的点（写错任何一个都会导致"看不见"或"挡操作"）：
 *   * 窗口类型必须是 **TYPE_APPLICATION_OVERLAY**（Android 8 起 TYPE_PHONE 等已被禁）
 *   * 必须加 **FLAG_NOT_FOCUSABLE**，否则会抢走直播 App 的输入焦点，用户没法打字发弹幕
 *   * 鼠标穿透用 **FLAG_NOT_TOUCHABLE**（对应 Windows 端的 WS_EX_TRANSPARENT）
 *   * 需要 SYSTEM_ALERT_WINDOW 权限，而它**无法用运行时对话框申请**，
 *     只能跳系统设置页让用户手动开，再用 Settings.canDrawOverlays() 回查
 */
class OverlayController(private val context: Context) {

    private val windowManager =
        context.getSystemService(Context.WINDOW_SERVICE) as WindowManager
    private var view: SubtitleOverlayView? = null
    private var params: WindowManager.LayoutParams? = null

    /** 是否已添加 */
    val isShowing: Boolean get() = view != null

    companion object {
        private const val TAG = "OverlayController"

        /** 悬浮窗权限是否已授予 */
        fun canDraw(context: Context): Boolean =
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) Settings.canDrawOverlays(context)
            else true
    }

    fun show(showPanel: Boolean, maxLines: Int, bottomOffsetPx: Int, locked: Boolean): Boolean {
        if (!canDraw(context)) {
            Log.w(TAG, "没有悬浮窗权限")
            return false
        }
        if (view != null) {
            // 已经在了：更新样式即可
            view?.showPanel = showPanel
            view?.maxLines = maxLines
            return true
        }
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
                    WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS or
                    WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN or
                    WindowManager.LayoutParams.FLAG_NOT_TOUCH_MODAL,
                PixelFormat.TRANSLUCENT,
            ).apply {
                gravity = Gravity.BOTTOM or Gravity.CENTER_HORIZONTAL
                y = if (bottomOffsetPx > 0) bottomOffsetPx else defaultBottomOffset()
                // 默认穿透：字幕不该挡直播操作
                if (!locked) flags = flags or WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE
            }

            windowManager.addView(v, p)
            attachDrag(v, p)
            view = v
            params = p
            Log.i(TAG, "悬浮窗已添加")
            true
        } catch (t: Throwable) {
            // 常见失败：权限其实没给全、或 ROM 拦截
            Log.e(TAG, "添加悬浮窗失败", t)
            view = null
            params = null
            false
        }
    }

    private fun defaultBottomOffset(): Int {
        // 默认贴在底边往上 8% 处，避开直播 App 自己的控制条
        val h = context.resources.displayMetrics.heightPixels
        return (h * 0.08f).toInt()
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
        runCatching { windowManager.updateViewLayout(v, p) }
            .onFailure { Log.w(TAG, "更新悬浮窗样式失败", it) }
    }

    fun setLocked(locked: Boolean) = setClickThrough(!locked)

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
