package com.elker54.tetraalert

import android.content.Intent
import android.content.pm.ApplicationInfo
import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.Paint
import android.graphics.RectF
import android.os.Handler
import android.os.Looper
import androidx.car.app.CarAppService
import androidx.car.app.CarContext
import androidx.car.app.CarToast
import androidx.car.app.Screen
import androidx.car.app.Session
import androidx.car.app.model.Action
import androidx.car.app.model.CarIcon
import androidx.car.app.model.Header
import androidx.car.app.model.ItemList
import androidx.car.app.model.ListTemplate
import androidx.car.app.model.Pane
import androidx.car.app.model.PaneTemplate
import androidx.car.app.model.Row
import androidx.car.app.model.Template
import androidx.car.app.model.Toggle
import androidx.core.graphics.drawable.IconCompat
import androidx.lifecycle.DefaultLifecycleObserver
import androidx.lifecycle.LifecycleOwner
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/** Android Auto entry point. Sideloaded debug build, so any host may connect. */
class AlertCarAppService : CarAppService() {

    override fun createHostValidator() =
        if (applicationInfo.flags and ApplicationInfo.FLAG_DEBUGGABLE != 0) {
            androidx.car.app.validation.HostValidator.ALLOW_ALL_HOSTS_VALIDATOR
        } else {
            androidx.car.app.validation.HostValidator.Builder(applicationContext)
                .addAllowedHosts(androidx.car.app.R.array.hosts_allowlist_sample)
                .build()
        }

    override fun onCreateSession(): Session = object : Session() {
        override fun onCreateScreen(intent: Intent): Screen = StatusScreen(carContext)
    }
}

/**
 * Car screen: a Pane with a drawn picture (strength meter + last 10 minutes),
 * or a plain list on hosts too old for Pane images (car API < 4).
 *
 * Android Auto allows 5 templates per task, and an update only counts as a
 * free "refresh" if the header title, number of rows and every row TITLE stay
 * the same. So titles here never change; everything live goes in row text.
 */
class StatusScreen(ctx: CarContext) : Screen(ctx) {

    private val main = Handler(Looper.getMainLooper())
    private val clock = SimpleDateFormat("HH:mm:ss", Locale.UK)
    private var pending = false
    private val usePane = ctx.carAppApiLevel >= 4

    // Hosts limit how often a template may refresh, so batch updates.
    private val listener: () -> Unit = {
        if (!pending) {
            pending = true
            main.postDelayed({ pending = false; invalidate() }, 1000)
        }
    }
    // "40 s ago" and the moving bars need a slow tick even with no news.
    private val tick = object : Runnable {
        override fun run() { listener(); main.postDelayed(this, 10_000) }
    }

    init {
        Tones.init(ctx)
        // Car screen opened with the link off (e.g. just after an app update): start it.
        if (DetectorState.conn == DetectorState.Conn.STOPPED && PiPresence.isPaired(ctx)) {
            try { DetectorService.start(ctx) } catch (e: Exception) { }
        }
        lifecycle.addObserver(object : DefaultLifecycleObserver {
            override fun onStart(owner: LifecycleOwner) {
                DetectorState.addListener(listener)
                main.postDelayed(tick, 10_000)
            }
            override fun onStop(owner: LifecycleOwner) {
                DetectorState.removeListener(listener)
                main.removeCallbacks(tick)
            }
        })
    }

    private fun lastAlert(): DetectorState.LastAlert? = DetectorState.lastAlert
        ?: AlertLog.lastAlert(carContext)?.let {
            DetectorState.LastAlert(it.tone, it.db, it.freqHz, it.atMillis)
        }

    /** One line: state, Pi temperature/load once known, and any warning. */
    private fun statusText(): String =
        (listOf(DetectorState.summary()) + listOfNotNull(DetectorState.piStats()) +
            DetectorState.piWarnings() +
            listOfNotNull(DetectorState.modeText(carContext),
                if (DetectorState.muted(carContext)) "MUTED" else null)).joinToString(" · ")

    private fun alertLines(now: Long, oneLine: Boolean = false): List<String> {
        val a = lastAlert() ?: return listOf("None yet")
        // The Pane with the mode button only fits 3 lines of row text without scroll arrows.
        if (oneLine) return listOf("${AlertLog.toneName(a.tone)} %+.0f dB · %s · %.4f".format(
            Locale.UK, a.db, ago(now - a.atMillis), a.freqHz / 1e6))
        return listOf(
            "${AlertLog.toneName(a.tone)} %+.0f dB · %s".format(Locale.UK, a.db, ago(now - a.atMillis)),
            "${clock.format(Date(a.atMillis))} · %.4f MHz".format(Locale.UK, a.freqHz / 1e6))
    }

    private fun ago(ms: Long) = when {
        ms < 60_000 -> "${ms / 1000} s ago"
        ms < 3_600_000 -> "${ms / 60_000} min ago"
        else -> "${ms / 3_600_000} h ago"
    }

    private fun header(withMute: Boolean = false): Header {
        val h = Header.Builder()
            .setTitle("TETRA Alert")
            .setStartHeaderAction(Action.APP_ICON)
        if (withMute) {
            // "Saw one": the driver marks a police car, ambulance etc. for later tuning.
            h.addEndHeaderAction(Action.Builder()
                .setIcon(CarIcon.Builder(IconCompat.createWithBitmap(icon(R.drawable.ic_flag))).build())
                .setOnClickListener {
                    val saved = AlertLog.markSighting(carContext)
                    CarToast.makeText(carContext, if (saved) "Sighting saved" else "Already saved",
                        CarToast.LENGTH_SHORT).show()
                }
                .build())
            // Header actions must be icons; it's the only spot always on the right.
            val muted = DetectorState.muted(carContext)
            h.addEndHeaderAction(Action.Builder()
                .setIcon(CarIcon.Builder(IconCompat.createWithBitmap(
                    icon(if (muted) R.drawable.ic_sound_off else R.drawable.ic_sound_on))).build())
                .setOnClickListener { DetectorState.setMuted(carContext, !muted) }
                .build())
        }
        return h.build()
    }

    /** Vector icon as a bitmap: the host drew the resource version as a plain white square. */
    private fun icon(res: Int): Bitmap {
        val size = 96
        val bmp = Bitmap.createBitmap(size, size, Bitmap.Config.ARGB_8888)
        val d = carContext.getDrawable(res)!!.mutate()
        d.setBounds(8, 8, size - 8, size - 8)
        d.draw(Canvas(bmp))
        return bmp
    }

    /** Button text: what it's on now. Tap cycles Auto -> City -> Motorway -> Auto. */
    private fun modeButton() = when (DetectorState.modeSetting(carContext)) {
        0 -> "Auto: " + if (DetectorState.autoCity) "City" else "Motorway"
        1 -> "City (override)"
        else -> "Motorway (override)"
    }

    private fun cycleMode() =
        DetectorState.setModeSetting(carContext, (DetectorState.modeSetting(carContext) + 1) % 3)

    override fun onGetTemplate(): Template = if (usePane) pane() else list()

    private fun pane(): Template {
        val now = System.currentTimeMillis()
        val pane = Pane.Builder()
            // One row, two lines: the screen fits this, the picture and the mode button without scrolling.
            .addRow(Row.Builder().setTitle("Status").addText(statusText())
                .addText(alertLines(now, oneLine = true)[0]).build())   // no "Last:" so the MHz fits
            .addAction(Action.Builder()
                .setTitle(modeButton())
                .setOnClickListener { cycleMode() }
                .build())
            .setImage(CarIcon.Builder(IconCompat.createWithBitmap(
                CarPicture.draw(lastAlert(), AlertLog.since(carContext, now - TimelineView.WINDOW_MS), now,
                    Tuning.ms(carContext, Tuning.DIM_AFTER))))   // square: the host crops anything wider
                .build())
            .build()
        return PaneTemplate.Builder(pane).setHeader(header(withMute = true)).build()
    }

    private fun list(): Template {
        val now = System.currentTimeMillis()
        val items = ItemList.Builder()
        items.addItem(Row.Builder().setTitle("Status").addText(statusText()).build())
        val alert = Row.Builder().setTitle("Last alert")
        alertLines(now).forEach { alert.addText(it) }
        items.addItem(alert.build())

        val muted = DetectorState.muted(carContext)
        items.addItem(Row.Builder()
            .setTitle("Mute alerts")
            .setToggle(Toggle.Builder { DetectorState.setMuted(carContext, it) }
                .setChecked(muted).build())
            .build())

        val stopped = DetectorState.conn == DetectorState.Conn.STOPPED
        items.addItem(Row.Builder()
            .setTitle("Detector link")
            .addText(if (stopped) "Off - tap to start" else "On")
            .setOnClickListener { if (stopped) startLink() }
            .build())
        items.addItem(Row.Builder()
            .setTitle("City / Motorway")
            .addText(modeButton())
            .setOnClickListener { cycleMode() }
            .build())
        items.addItem(Row.Builder()
            .setTitle("Test alert sound")
            .setOnClickListener { Tones.alert(1) }
            .build())

        return ListTemplate.Builder()
            .setSingleList(items.build())
            .setHeader(header())
            .build()
    }

    private fun startLink() {
        try {
            DetectorService.start(carContext)
        } catch (e: Exception) {
            // Android may refuse to start it from here; the phone app always can.
            CarToast.makeText(carContext, "Start the link from the phone app",
                CarToast.LENGTH_LONG).show()
        }
    }
}

/** The car picture: strength meter on the left, last 10 minutes of alerts on the right. */
object CarPicture {
    // 384 px square, RGB_565: about 290 KB, well inside what one Binder call can carry.
    // Default 384 px square (widget). RGB_565 keeps it well inside one Binder call.
    private const val S = 384

    fun draw(a: DetectorState.LastAlert?, entries: List<AlertLog.Entry>, now: Long,
             dimMs: Long = 30_000L, W: Int = S, H: Int = S): Bitmap {
        val bmp = Bitmap.createBitmap(W, H, Bitmap.Config.RGB_565)
        val c = Canvas(bmp)
        val p = Paint(Paint.ANTI_ALIAS_FLAG)
        val r = RectF()
        c.drawColor(Ui.BG)

        val pad = 22f
        val meterW = 60f
        val gap = 18f
        val h = H - 2 * pad

        // Meter: 9 segments, 6 dB each, dimmed once the alert is over 30 s old.
        val n = 9
        val sg = 4f
        val segH = (h - sg * (n - 1)) / n
        val colours = intArrayOf(Ui.O1, Ui.O1, Ui.O2, Ui.O2, Ui.O3, Ui.O3, Ui.O4, Ui.O4, Ui.O4)
        val lit = a?.let { (it.db / 6.0).toInt().coerceIn(1, n) } ?: 0
        val stale = a == null || now - a.atMillis > dimMs
        for (i in 0 until n) {
            val bottom = H - pad - i * (segH + sg)
            r.set(pad, bottom - segH, pad + meterW, bottom)
            p.color = if (i < lit) colours[i] else Ui.LINE
            if (i < lit && stale) p.alpha = 90
            c.drawRoundRect(r, 5f, 5f, p)
        }

        // Timeline card.
        val left = pad + meterW + gap
        r.set(left, pad, W - pad, H - pad)
        p.color = Ui.CARD
        c.drawRoundRect(r, 10f, 10f, p)
        val ip = 10f
        val buckets = TimelineView.BUCKETS
        val bg = 3f
        val bw = (W - pad - left - 2 * ip - bg * (buckets - 1)) / buckets
        val base = H - pad - ip
        val maxH = h - 2 * ip
        p.color = Ui.LINE
        c.drawRect(left + ip, base - 2f, W - pad - ip, base, p)
        val best = arrayOfNulls<AlertLog.Entry>(buckets)
        val start = now - TimelineView.WINDOW_MS
        for (e in entries) {
            if (e.atMillis < start || e.atMillis > now) continue
            val b = ((e.atMillis - start) * buckets / TimelineView.WINDOW_MS).toInt().coerceIn(0, buckets - 1)
            if (best[b] == null || e.db > best[b]!!.db) best[b] = e
        }
        for (i in 0 until buckets) {
            val e = best[i] ?: continue
            val frac = (e.db / 60.0).coerceIn(0.06, 1.0).toFloat()
            val x = left + ip + i * (bw + bg)
            r.set(x, base - maxH * frac, x + bw, base)
            p.color = Ui.toneColour(e.tone)
            c.drawRoundRect(r, 2f, 2f, p)
        }
        return bmp
    }
}
