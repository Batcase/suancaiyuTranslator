package com.suancaiyu.translator.ui

import android.content.Context
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.graphics.Typeface
import android.text.TextPaint
import android.util.AttributeSet
import android.util.TypedValue
import android.view.View
import com.suancaiyu.translator.data.SubtitleLine
import com.suancaiyu.translator.data.SubtitleUiState
import com.suancaiyu.translator.data.RunState

/**
 * 悬浮字幕视图。
 *
 * 为什么手写 onDraw 而不用 TextView 叠起来：
 *   直播画面明暗不定，纯色文字经常看不清。**描边文字**才是唯一可靠的做法，
 *   而 Android 的 TextView 不支持描边（只能加阴影，亮底上仍然发虚）。
 *   自定义 View 里用 Paint.Style.STROKE 先画一遍再画填充，效果与 Windows 端一致。
 *
 * 布局：顶部一条状态栏 + 下面若干行字幕（译文大字在上、原文小字在下）。
 */
class SubtitleOverlayView @JvmOverloads constructor(
    context: Context,
    attrs: AttributeSet? = null,
) : View(context, attrs) {

    private var uiState = SubtitleUiState()

    /** 是否画深色底板（默认不画：纯文字浮在画面上更像原生字幕） */
    var showPanel: Boolean = false
        set(value) {
            field = value
            invalidate()
        }

    var maxLines: Int = 2
        set(value) {
            field = value.coerceIn(1, 4)
            invalidate()
        }

    // ---- 画笔 ----
    private val fillPaint = TextPaint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.WHITE
        textSize = sp(24f)
        typeface = Typeface.create(Typeface.DEFAULT, Typeface.BOLD)
    }
    private val strokePaint = TextPaint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.BLACK            // 描边永远是黑的，亮暗底都清晰
        textSize = sp(24f)
        typeface = Typeface.create(Typeface.DEFAULT, Typeface.BOLD)
        style = Paint.Style.STROKE
        strokeWidth = sp(2.2f)
        strokeJoin = Paint.Join.ROUND
    }
    private val sourcePaint = TextPaint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.parseColor("#E8EAED")
        textSize = sp(14f)
        typeface = Typeface.DEFAULT
    }
    private val sourceStrokePaint = TextPaint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.BLACK
        textSize = sp(14f)
        typeface = Typeface.DEFAULT
        style = Paint.Style.STROKE
        strokeWidth = sp(1.6f)
        strokeJoin = Paint.Join.ROUND
    }
    private val statusPaint = TextPaint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.parseColor("#9AA0A6")
        textSize = sp(10f)
    }
    private val panelPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.parseColor("#B312161B")   // 半透明深色底板
    }
    private val barPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.parseColor("#D90E1116")
    }
    private val dividerPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.parseColor("#33FFFFFF")
        strokeWidth = 1f
    }

    private fun sp(v: Float): Float =
        TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_SP, v, resources.displayMetrics)

    // ---------------------------------------------------------------

    fun render(state: SubtitleUiState) {
        uiState = state
        invalidate()
    }

    /**
     * 量一下需要多高——供 WindowManager 调整窗口高度。
     * 悬浮窗是 WRAP_CONTENT 时直接用 onMeasure 的结果即可。
     */
    override fun onMeasure(widthMeasureSpec: Int, heightMeasureSpec: Int) {
        val width = MeasureSpec.getSize(widthMeasureSpec)
        val contentWidth = (width - paddingLeft - paddingRight - 24).coerceAtLeast(120)
        val height = barHeight() + contentHeight(contentWidth) + paddingTop + paddingBottom
        // setMeasuredDimension 要 Int：高度是浮点算出来的，必须取整
        setMeasuredDimension(width, height.toInt().coerceAtLeast(1))
    }

    private fun barHeight() = statusPaint.textSize * 2.1f

    private fun contentHeight(width: Int): Float {
        val lines = visibleLines()
        if (lines.isEmpty()) return statusPaint.textSize * 1.6f
        var h = 6f
        lines.forEach { line ->
            h += wrappedHeight(line.translated.ifEmpty { "…" }, fillPaint, width) + 2f
            if (line.source.isNotEmpty()) {
                h += wrappedHeight(line.source, sourcePaint, width) + 2f
            }
            h += 6f
        }
        return h
    }

    private fun wrappedHeight(text: String, paint: TextPaint, width: Int): Float {
        val lines = wrap(text, paint, width)
        return lines.size * (paint.descent() - paint.ascent() + 2f)
    }

    override fun onDraw(canvas: Canvas) {
        super.onDraw(canvas)
        val w = width.toFloat()
        val left = paddingLeft + 12f

        // ---- 状态栏 ----
        val barH = barHeight()
        canvas.drawRect(0f, 0f, w, barH, barPaint)
        val statusText = buildStatusText()
        canvas.drawText(statusText, left, barH / 2f - (statusPaint.descent() + statusPaint.ascent()) / 2f, statusPaint)
        canvas.drawLine(0f, barH, w, barH, dividerPaint)

        // ---- 内容 ----
        val lines = visibleLines()
        if (lines.isEmpty()) {
            val y = barH + statusPaint.textSize * 1.4f
            canvas.drawText("等待声音…（请确认直播正在播放）", left, y, statusPaint)
            return
        }

        val contentWidth = (width - left - paddingRight - 12f).toInt().coerceAtLeast(120)
        if (showPanel) {
            canvas.drawRect(0f, barH, w, height.toFloat(), panelPaint)
        }

        var y = barH + 6f
        val lastIndex = lines.lastIndex
        lines.forEachIndexed { i, line ->
            val isLive = (i == lastIndex) && !line.final

            // 译文：先描边再填充（顺序不能反，否则描边会盖住字心）
            val translated = line.translated.ifEmpty { if (isLive) "…" else "" }
            if (translated.isNotEmpty()) {
                val color = when {
                    line.failed -> Color.parseColor("#FF8A80")
                    isLive -> Color.parseColor("#FFE08A")   // 未定稿：暖黄，暗示还会变
                    else -> Color.WHITE
                }
                fillPaint.color = color
                drawOutlined(canvas, translated, left, y, fillPaint, strokePaint, contentWidth)
                y += wrappedHeight(translated, fillPaint, contentWidth)
            }

            // 原文：小字浅灰
            if (line.source.isNotEmpty()) {
                sourcePaint.color = if (isLive) Color.parseColor("#9AA0A6") else Color.parseColor("#E8EAED")
                drawOutlined(canvas, line.source, left, y, sourcePaint, sourceStrokePaint, contentWidth)
                y += wrappedHeight(line.source, sourcePaint, contentWidth)
            }
            y += 6f
        }
    }

    /** 一行文字先描边后填充，并支持自动折行。 */
    private fun drawOutlined(
        canvas: Canvas,
        text: String,
        x: Float,
        top: Float,
        fill: TextPaint,
        stroke: TextPaint,
        maxWidth: Int,
    ) {
        val rows = wrap(text, fill, maxWidth)
        var baseline = top - fill.ascent()
        val lineHeight = fill.descent() - fill.ascent() + 2f
        rows.forEach { row ->
            canvas.drawText(row, x, baseline, stroke)
            canvas.drawText(row, x, baseline, fill)
            baseline += lineHeight
        }
    }

    /** 中英混排的简单折行：中文按字断，英文尽量按词断。 */
    private fun wrap(text: String, paint: TextPaint, maxWidth: Int): List<String> {
        if (maxWidth <= 0) return listOf(text)
        if (paint.measureText(text) <= maxWidth) return listOf(text)
        val out = mutableListOf<String>()
        val sb = StringBuilder()
        var lastSpace = -1
        text.forEach { ch ->
            sb.append(ch)
            if (ch == ' ') lastSpace = sb.length - 1
            if (paint.measureText(sb.toString()) > maxWidth) {
                // 超宽了：英文回退到最近空格，中文直接断
                if (lastSpace > 0 && sb.length - lastSpace < 12) {
                    out.add(sb.substring(0, lastSpace))
                    val rest = sb.substring(lastSpace + 1)
                    sb.setLength(0)
                    sb.append(rest)
                } else {
                    val last = sb.last()
                    sb.setLength(sb.length - 1)
                    out.add(sb.toString())
                    sb.setLength(0)
                    sb.append(last)
                }
                lastSpace = -1
            }
        }
        if (sb.isNotEmpty()) out.add(sb.toString())
        return out
    }

    private fun visibleLines(): List<SubtitleLine> {
        val lines = uiState.lines
        if (lines.isEmpty()) return emptyList()
        return lines.takeLast(maxLines)
    }

    private fun buildStatusText(): String {
        val st = when (uiState.state) {
            RunState.IDLE -> "未启动"
            RunState.STARTING -> "正在启动…"
            RunState.RUNNING -> "识别中"
            RunState.PAUSED -> "已暂停"
            RunState.ERROR -> "出错了"
        }
        val bits = mutableListOf(st)
        if (uiState.activeAsr.isNotEmpty()) bits += uiState.activeAsr
        if (uiState.e2eMs > 0) bits += "${uiState.e2eMs}ms"
        if (uiState.errors > 0) bits += "错误×${uiState.errors}"
        if (uiState.detail.isNotEmpty()) bits += uiState.detail.take(24)
        return bits.joinToString("  ·  ")
    }

    companion object {
        const val TAG = "SubtitleOverlayView"
    }
}
