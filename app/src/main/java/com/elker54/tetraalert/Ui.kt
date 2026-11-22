package com.elker54.tetraalert

import android.content.Context
import android.graphics.Canvas
import android.graphics.Paint
import android.graphics.RectF
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.view.View

/** Colours and small helpers for the "Signal" look. */
object Ui {
    const val BG = 0xFF0B1220.toInt()
    const val CARD = 0xFF111A2C.toInt()
    const val LINE = 0xFF1E293B.toInt()
    const val BAR = 0xFF0E1628.toInt()
    const val TEXT = 0xFFE5E9F0.toInt()
    const val MUTED = 0xFF9AA6B8.toInt()
    const val ACCENT = 0xFF38BDF8.toInt()
    const val ACCENT_TEXT = 0xFF7DD3FC.toInt()
    const val WEAK = 0xFF475569.toInt()
    const val O1 = 0xFFFDBA74.toInt()
    const val O2 = 0xFFFB923C.toInt()
    const val O3 = 0xFFF97316.toInt()
    const val O4 = 0xFFEA580C.toInt()
    const val WARN = 0xFFF87171.toInt()
    val MONO: Typeface = Typeface.MONOSPACE
    val BOLD_MONO: Typeface = Typeface.create(Typeface.MONOSPACE, Typeface.BOLD)

    fun toneColour(tone: Int) = when (tone) { AlertLog.SIGHTING -> ACCENT; 0 -> WEAK; 1 -> O2; else -> O1 }
    fun toneText(tone: Int) = when (tone) { 0 -> MUTED; 1 -> O2; else -> O1 }

    fun dp(c: Context, v: Float) = v * c.resources.displayMetrics.density
    fun dpi(c: Context, v: Int) = (v * c.resources.displayMetrics.density + 0.5f).toInt()

    fun rounded(c: Context, colour: Int, radiusDp: Float, strokeColour: Int? = null) =
        GradientDrawable().apply {
            setColor(colour)
            cornerRadius = dp(c, radiusDp)
            strokeColour?.let { setStroke(dpi(c, 1), it) }
        }
}

/** Nine stacked segments: how strong the last alert was. Dims once it's old. */
class MeterView(c: Context) : View(c) {
    var db: Double? = null
    var stale = false
    private val paint = Paint(Paint.ANTI_ALIAS_FLAG)
    private val r = RectF()
    private val colours = intArrayOf(Ui.O1, Ui.O1, Ui.O2, Ui.O2, Ui.O3, Ui.O3, Ui.O4, Ui.O4, Ui.O4)

    override fun onDraw(canvas: Canvas) {
        val n = 9
        val gap = Ui.dp(context, 4f)
        val h = (height - gap * (n - 1)) / n
        val lit = db?.let { (it / 6.0).toInt().coerceIn(1, n) } ?: 0   // 6 dB a segment, 54+ fills it
        for (i in 0 until n) {
            val bottom = height - i * (h + gap)
            r.set(0f, bottom - h, width.toFloat(), bottom)
            paint.color = if (i < lit) colours[i] else Ui.LINE
            paint.alpha = if (i < lit && stale) 90 else 255
            canvas.drawRoundRect(r, Ui.dp(context, 4f), Ui.dp(context, 4f), paint)
        }
    }
}

/** Alerts in the last 10 minutes, 30 s to a bar, height by strength. */
class TimelineView(c: Context) : View(c) {
    var entries: List<AlertLog.Entry> = emptyList()
    var now = System.currentTimeMillis()
    private val paint = Paint(Paint.ANTI_ALIAS_FLAG)
    private val r = RectF()

    companion object {
        const val WINDOW_MS = 10 * 60_000L
        const val BUCKETS = 20
    }

    override fun onDraw(canvas: Canvas) {
        paint.color = Ui.CARD
        r.set(0f, 0f, width.toFloat(), height.toFloat())
        canvas.drawRoundRect(r, Ui.dp(context, 12f), Ui.dp(context, 12f), paint)

        val pad = Ui.dp(context, 10f)
        val gap = Ui.dp(context, 4f)
        val w = (width - 2 * pad - gap * (BUCKETS - 1)) / BUCKETS
        val maxH = height - 2 * pad
        val best = arrayOfNulls<AlertLog.Entry>(BUCKETS)
        val start = now - WINDOW_MS
        for (e in entries) {
            if (e.atMillis < start || e.atMillis > now) continue
            val b = ((e.atMillis - start) * BUCKETS / WINDOW_MS).toInt().coerceIn(0, BUCKETS - 1)
            if (best[b] == null || e.db > best[b]!!.db) best[b] = e
        }
        // Faint baseline so an empty chart still reads as a chart.
        paint.color = Ui.LINE
        canvas.drawRect(pad, height - pad - Ui.dp(context, 1f), width - pad, height - pad, paint)
        for (i in 0 until BUCKETS) {
            val e = best[i] ?: continue
            val frac = (e.db / 60.0).coerceIn(0.06, 1.0).toFloat()
            val x = pad + i * (w + gap)
            r.set(x, height - pad - maxH * frac, x + w, height - pad)
            paint.color = Ui.toneColour(e.tone)
            canvas.drawRoundRect(r, Ui.dp(context, 2f), Ui.dp(context, 2f), paint)
        }
    }
}
