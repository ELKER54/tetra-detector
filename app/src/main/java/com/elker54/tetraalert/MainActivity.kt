package com.elker54.tetraalert

import android.Manifest
import android.app.Activity
import android.app.AlertDialog
import android.content.ClipData
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.net.Uri
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.text.InputType
import android.text.SpannableString
import android.text.Spanned
import android.text.style.AbsoluteSizeSpan
import android.text.style.ForegroundColorSpan
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.view.ViewGroup.LayoutParams.MATCH_PARENT
import android.view.ViewGroup.LayoutParams.WRAP_CONTENT
import android.view.WindowInsets
import android.widget.BaseAdapter
import android.widget.Button
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.ListView
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import androidx.core.content.FileProvider
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.zip.ZipInputStream

/** Phone screen, "Signal" design: Live, History, Pi and Settings tabs. */
class MainActivity : Activity() {

    private val main = Handler(Looper.getMainLooper())
    private val clock = SimpleDateFormat("HH:mm:ss", Locale.UK)
    private val dayClock = SimpleDateFormat("EEE d MMM HH:mm:ss", Locale.UK)
    private val hhmm = SimpleDateFormat("HH:mm", Locale.UK)
    private val listener: () -> Unit = { runOnUiThread { refresh() } }
    private val refreshers = mutableListOf<() -> Unit>()   // for the tab on screen
    private lateinit var content: FrameLayout
    private val tabButtons = mutableListOf<TextView>()
    private var tab = 0

    private val linkPerms = arrayOf(
        Manifest.permission.BLUETOOTH_SCAN,
        Manifest.permission.BLUETOOTH_CONNECT,
        Manifest.permission.POST_NOTIFICATIONS,
        Manifest.permission.ACCESS_FINE_LOCATION,
        Manifest.permission.ACCESS_COARSE_LOCATION,
    )

    private val tick = object : Runnable {
        override fun run() { refresh(); main.postDelayed(this, 5000) }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        CrashLog.install(this)
        Tones.init(this)
        window.decorView.setBackgroundColor(Ui.BG)

        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundColor(Ui.BG)
        }
        content = FrameLayout(this)
        root.addView(content, LinearLayout.LayoutParams(MATCH_PARENT, 0, 1f))
        root.addView(View(this).apply { setBackgroundColor(Ui.LINE) },
            LinearLayout.LayoutParams(MATCH_PARENT, Ui.dpi(this, 1)))
        val bar = LinearLayout(this).apply { setBackgroundColor(Ui.BAR) }
        listOf("Live", "History", "Pi", "Settings").forEachIndexed { i, name ->
            val b = TextView(this).apply {
                text = name
                gravity = Gravity.CENTER
                textSize = 13f
                setOnClickListener { show(i) }
            }
            tabButtons.add(b)
            bar.addView(b, LinearLayout.LayoutParams(0, Ui.dpi(this, 60), 1f))
        }
        root.addView(bar)

        // Android 15+ draws edge to edge: keep clear of the status and navigation bars.
        root.setOnApplyWindowInsetsListener { v, insets ->
            val bars = insets.getInsets(WindowInsets.Type.systemBars() or
                WindowInsets.Type.displayCutout())
            v.setPadding(bars.left, bars.top, bars.right, bars.bottom)
            insets
        }
        setContentView(root)
        show(savedInstanceState?.getInt("tab") ?: 0)

        PiPresence.ensureObserving(this)?.let { toast("Auto-start: $it") }
        CrashLog.lastAndClear(this)?.let {
            AlertDialog.Builder(this).setTitle("The app crashed last time")
                .setMessage("Please send this to Claude:\n\n$it")
                .setPositiveButton("OK", null).show()
        }
    }

    override fun onSaveInstanceState(out: Bundle) {
        super.onSaveInstanceState(out)
        out.putInt("tab", tab)
    }

    override fun onResume() {
        super.onResume()
        DetectorState.addListener(listener)
        main.post(tick)
    }

    override fun onPause() {
        DetectorState.removeListener(listener)
        main.removeCallbacks(tick)
        super.onPause()
    }

    private fun show(i: Int) {
        tab = i
        refreshers.clear()
        content.removeAllViews()
        content.addView(when (i) {
            0 -> liveTab(); 1 -> historyTab(); 2 -> piTab(); else -> settingsTab()
        })
        tabButtons.forEachIndexed { j, b ->
            b.setTextColor(if (j == i) Ui.ACCENT_TEXT else Ui.MUTED)
            b.typeface = if (j == i) android.graphics.Typeface.DEFAULT_BOLD
                else android.graphics.Typeface.DEFAULT
        }
        refresh()
    }

    private fun refresh() = refreshers.forEach { it() }

    // ================================================================ Live

    private fun liveTab(): View {
        val col = column(20, 16)

        // Status line: dot, state, and Pi temperature/load/Wi-Fi on the right.
        val dot = View(this)
        val state = text("", 15f, Ui.TEXT, bold = true)
        val pi = text("", 12f, Ui.MUTED).apply { typeface = Ui.MONO; gravity = Gravity.END }
        col.addView(LinearLayout(this).apply {
            gravity = Gravity.CENTER_VERTICAL
            addView(dot, LinearLayout.LayoutParams(Ui.dpi(context, 10), Ui.dpi(context, 10))
                .apply { marginEnd = Ui.dpi(context, 10) })
            addView(state, LinearLayout.LayoutParams(0, WRAP_CONTENT, 1f))
            addView(pi)
        })
        val note = text("", 13f, Ui.MUTED).apply { setPadding(0, Ui.dpi(context, 4), 0, 0) }
        col.addView(note)

        // Link off: one obvious way to start it.
        val startCard = button("Start detector link", filled = false) { startLink() }
        col.addView(startCard, lp(top = 12))

        // Meter and the last alert.
        val meter = MeterView(this)
        val head = label("LAST ALERT")
        val big = text("--", 56f, Ui.MUTED).apply { typeface = Ui.BOLD_MONO; includeFontPadding = false }
        val tone = text("", 18f, Ui.TEXT, bold = true)
        val freq = text("", 14f, Ui.MUTED).apply { typeface = Ui.MONO }
        val age = text("", 13f, Ui.MUTED)
        val info = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER_VERTICAL
            listOf(head, big, tone, freq, age).forEach { addView(it, lp(top = 4)) }
        }
        col.addView(LinearLayout(this).apply {
            addView(meter, LinearLayout.LayoutParams(Ui.dpi(context, 56), Ui.dpi(context, 230))
                .apply { marginEnd = Ui.dpi(context, 18) })
            addView(info, LinearLayout.LayoutParams(0, MATCH_PARENT, 1f))
        }, lp(top = 16))

        // Last 10 minutes.
        val historyLink = text("History", 13f, Ui.ACCENT_TEXT).apply {
            setPadding(Ui.dpi(context, 12), Ui.dpi(context, 10), 0, Ui.dpi(context, 10))
            setOnClickListener { show(1) }
        }
        col.addView(LinearLayout(this).apply {
            gravity = Gravity.CENTER_VERTICAL
            addView(label("LAST 10 MINUTES"), LinearLayout.LayoutParams(0, WRAP_CONTENT, 1f))
            addView(historyLink)
        }, lp(top = 18))
        val timeline = TimelineView(this)
        col.addView(timeline, LinearLayout.LayoutParams(MATCH_PARENT, Ui.dpi(this, 92)))
        val axisL = text("", 11f, Ui.MUTED).apply { typeface = Ui.MONO }
        val axisM = text("", 11f, Ui.MUTED).apply { typeface = Ui.MONO; gravity = Gravity.CENTER }
        val axisR = text("now", 11f, Ui.MUTED).apply { typeface = Ui.MONO; gravity = Gravity.END }
        col.addView(LinearLayout(this).apply {
            listOf(axisL, axisM, axisR).forEach { addView(it, LinearLayout.LayoutParams(0, WRAP_CONTENT, 1f)) }
        }, lp(top = 6))

        // Two quick settings, then the big mute button.
        val weak = valueRow(col, "Weak alerts") {
            DetectorState.setWeakMode(this, (DetectorState.weakMode(this) + 1) % 3)
        }
        val route = valueRow(col, "Sound route") { DetectorState.setUseMedia(this, !DetectorState.useMedia(this)) }
        // Quick override: Auto -> City -> Motorway -> Auto.
        val liveMode = valueRow(col, "City / Motorway") {
            DetectorState.setModeSetting(this, (DetectorState.modeSetting(this) + 1) % 3)
        }
        // Ground truth for tuning: tap when a police car, ambulance etc. is in sight.
        val snooze = button("Snooze", filled = false) {
            if (DetectorState.snoozed()) DetectorState.snooze(0)
            else DetectorState.snooze(Tuning.ms(this, Tuning.SNOOZE))
        }
        col.addView(LinearLayout(this).apply {
            addView(button("Saw one", filled = false) {
                toast(if (AlertLog.markSighting(this@MainActivity)) "Sighting saved with your location"
                      else "Already saved")
            }, LinearLayout.LayoutParams(0, Ui.dpi(context, 56), 1f).apply { marginEnd = Ui.dpi(context, 10) })
            addView(snooze, LinearLayout.LayoutParams(0, Ui.dpi(context, 56), 1f))
        }, lp(top = 20))
        val mute = button("", filled = true) { DetectorState.setMuted(this, !DetectorState.muted(this)) }
        col.addView(mute, lp(top = 12))

        refreshers.add {
            val conn = DetectorState.conn
            val s = DetectorState.status
            val running = conn == DetectorState.Conn.CONNECTED && s?.state == Link.STATE_RUNNING
            val bad = conn == DetectorState.Conn.OFFLINE ||
                (conn == DetectorState.Conn.CONNECTED && s != null && s.state != Link.STATE_RUNNING)
            dot.background = Ui.rounded(this, if (running) Ui.ACCENT else if (bad) Ui.WARN else Ui.MUTED, 5f)
            state.text = DetectorState.summary() + " · " + DetectorState.modeText(this)
            state.setTextColor(if (bad) Ui.WARN else Ui.TEXT)
            pi.text = s?.let {
                listOfNotNull(DetectorState.piStats(),
                    "WiFi " + if (it.flagsNow and 0x20 != 0) "on" else "off").joinToString(" · ")
            } ?: ""
            val warn = listOfNotNull(
                if (s != null && s.flagsNow and 0x5 != 0) "Pi power or heat limit" else null,
                if (s != null && s.flagsNow and 0x10 != 0) "Pi storage failed (alerts still work)" else null,
                if (s != null && s.flagsNow and 0x40 != 0) "Pi falling behind: skipping radio data" else null,
                DetectorState.note.ifEmpty { null })
            note.text = warn.joinToString("\n")
            note.visibility = if (warn.isEmpty()) View.GONE else View.VISIBLE
            startCard.visibility = if (conn == DetectorState.Conn.STOPPED) View.VISIBLE else View.GONE

            val now = System.currentTimeMillis()
            val a = DetectorState.lastAlert ?: AlertLog.lastAlert(this)?.let {
                DetectorState.LastAlert(it.tone, it.db, it.freqHz, it.atMillis)
            }
            val stale = a == null || now - a.atMillis > Tuning.ms(this, Tuning.DIM_AFTER)
            meter.db = a?.db
            meter.stale = stale
            meter.invalidate()
            if (a == null) {
                big.text = "--"; big.setTextColor(Ui.MUTED)
                tone.text = "No alerts yet"; freq.text = ""; age.text = ""
            } else {
                big.text = SpannableString("%+.0f dB".format(Locale.UK, a.db)).apply {
                    val cut = length - 3
                    setSpan(AbsoluteSizeSpan(22, true), cut, length, Spanned.SPAN_EXCLUSIVE_EXCLUSIVE)
                    setSpan(ForegroundColorSpan(Ui.MUTED), cut, length, Spanned.SPAN_EXCLUSIVE_EXCLUSIVE)
                }
                big.setTextColor(if (stale) Ui.MUTED else Ui.toneText(a.tone))
                tone.text = AlertLog.toneName(a.tone)
                freq.text = "%.4f MHz · %s".format(Locale.UK, a.freqHz / 1e6, clock.format(Date(a.atMillis)))
                age.text = ago(now - a.atMillis)
            }
            timeline.entries = AlertLog.since(this, now - TimelineView.WINDOW_MS)
            timeline.now = now
            timeline.invalidate()
            axisL.text = hhmm.format(Date(now - TimelineView.WINDOW_MS))
            axisM.text = hhmm.format(Date(now - TimelineView.WINDOW_MS / 2))

            weak.text = DetectorState.weakText(this)
            route.text = if (DetectorState.useMedia(this)) "Media" else "Navigation prompt"
            liveMode.text = when (DetectorState.modeSetting(this)) {
                0 -> "Auto (now ${if (DetectorState.autoCity) "City" else "Motorway"})"
                1 -> "City (override)"
                else -> "Motorway (override)"
            }
            val left = (DetectorState.snoozeUntil - System.currentTimeMillis() + 999) / 1000
            snooze.text = if (left > 0) "Snoozed ${left} s - tap to end"
                else "Snooze " + Tuning.SNOOZE.show(Tuning.get(this, Tuning.SNOOZE))
            val muted = DetectorState.muted(this)
            mute.text = if (muted) "Alerts MUTED - tap to unmute" else "Mute alerts"
            mute.background = Ui.rounded(this, if (muted) Ui.O3 else Ui.ACCENT, 14f)
        }
        return scroll(col)
    }

    private fun ago(ms: Long): String = when {
        ms < 60_000 -> "${ms / 1000} s ago"
        ms < 3_600_000 -> "${ms / 60_000} min ago"
        else -> "${ms / 3_600_000} h ago"
    }

    // ================================================================ History

    private fun historyTab(): View {
        val col = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(Ui.dpi(context, 20), Ui.dpi(context, 16), Ui.dpi(context, 20), 0)
        }
        val count = text("", 13f, Ui.MUTED)
        col.addView(LinearLayout(this).apply {
            gravity = Gravity.CENTER_VERTICAL
            addView(text("History", 22f, Ui.TEXT, bold = true), LinearLayout.LayoutParams(0, WRAP_CONTENT, 1f))
            addView(count)
        })
        val locWarn = text("", 13f, Ui.O2).apply {
            setPadding(0, Ui.dpi(context, 8), 0, 0)
            setOnClickListener { askLocation() }
        }
        col.addView(locWarn)
        col.addView(LinearLayout(this).apply {
            addView(button("Share as CSV", filled = false) { shareHistory() },
                LinearLayout.LayoutParams(0, Ui.dpi(context, 48), 1f).apply { marginEnd = Ui.dpi(context, 10) })
            addView(button("Clear", filled = false) { confirmClear() },
                LinearLayout.LayoutParams(0, Ui.dpi(context, 48), 1f))
        }, lp(top = 12))

        var items: List<AlertLog.Entry> = emptyList()
        val empty = text("No alerts saved yet. Each alert from the Pi lands here with where you were.",
            14f, Ui.MUTED).apply { setPadding(0, Ui.dpi(context, 24), 0, 0) }
        col.addView(empty)
        val adapter = object : BaseAdapter() {
            override fun getCount() = items.size
            override fun getItem(p: Int) = items[p]
            override fun getItemId(p: Int) = items[p].atMillis
            override fun getView(p: Int, old: View?, parent: ViewGroup): View =
                historyRow(items[p], old as? LinearLayout)
        }
        val list = ListView(this).apply {
            this.adapter = adapter
            divider = android.graphics.drawable.ColorDrawable(Ui.LINE)
            dividerHeight = Ui.dpi(context, 1)
            setSelector(android.R.color.transparent)
        }
        col.addView(list, LinearLayout.LayoutParams(MATCH_PARENT, 0, 1f).apply { topMargin = Ui.dpi(this@MainActivity, 8) })

        var shownSize = -1
        refreshers.add {
            val all = AlertLog.all(this)
            count.text = "${all.size} saved"
            locWarn.text = when {
                !AlertLog.hasLocation(this) -> "Locations are off, so alerts have no map. Tap to allow."
                !AlertLog.hasBackgroundLocation(this) ->
                    "Locations only save while the app is open. Tap and pick \"Allow all the time\"."
                else -> ""
            }
            locWarn.visibility = if (locWarn.text.isEmpty()) View.GONE else View.VISIBLE
            empty.visibility = if (all.isEmpty()) View.VISIBLE else View.GONE
            if (all.size != shownSize || all.firstOrNull() != items.firstOrNull()) {
                items = all; shownSize = all.size
                adapter.notifyDataSetChanged()
            }
        }
        return col
    }

    private fun historyRow(e: AlertLog.Entry, reuse: LinearLayout?): View {
        val row = reuse ?: LinearLayout(this).apply {
            gravity = Gravity.CENTER_VERTICAL
            minimumHeight = Ui.dpi(context, 64)
            setPadding(0, Ui.dpi(context, 8), 0, Ui.dpi(context, 8))
            val left = LinearLayout(context).apply { orientation = LinearLayout.VERTICAL }
            val top = LinearLayout(context).apply { gravity = Gravity.CENTER_VERTICAL }
            top.addView(text("", 11f, Ui.BG, bold = true).apply {
                setPadding(Ui.dpi(context, 7), Ui.dpi(context, 2), Ui.dpi(context, 7), Ui.dpi(context, 2))
            })
            top.addView(text("", 15f, Ui.TEXT, bold = true).apply { setPadding(Ui.dpi(context, 8), 0, 0, 0) })
            left.addView(top)
            left.addView(text("", 13f, Ui.MUTED).apply { typeface = Ui.MONO; setPadding(0, Ui.dpi(context, 3), 0, 0) })
            addView(left, LinearLayout.LayoutParams(0, WRAP_CONTENT, 1f))
            addView(text("", 14f, Ui.ACCENT_TEXT, bold = true).apply {
                gravity = Gravity.CENTER
                minWidth = Ui.dpi(context, 64)
                minHeight = Ui.dpi(context, 48)
            })
        }
        val left = row.getChildAt(0) as LinearLayout
        val top = left.getChildAt(0) as LinearLayout
        val badge = top.getChildAt(0) as TextView
        val time = top.getChildAt(1) as TextView
        val sub = left.getChildAt(1) as TextView
        val map = row.getChildAt(1) as TextView

        badge.text = AlertLog.toneName(e.tone).uppercase(Locale.UK)
        badge.background = Ui.rounded(this, Ui.toneColour(e.tone), 6f)
        val sighting = e.tone == AlertLog.SIGHTING
        badge.setTextColor(if (e.tone == 0) Ui.TEXT else Ui.BG)
        val today = SimpleDateFormat("yyyyMMdd", Locale.UK).let { it.format(Date()) == it.format(Date(e.atMillis)) }
        time.text = (if (today) clock else dayClock).format(Date(e.atMillis))
        sub.text = (if (sighting) "Your sighting"
            else "%+.0f dB · %.4f MHz".format(Locale.UK, e.db, e.freqHz / 1e6)) +
            (e.accM?.let { " · ±%.0f m".format(Locale.UK, it) } ?: "")
        if (e.lat != null && e.lon != null) {
            map.text = "Map"
            map.setTextColor(Ui.ACCENT_TEXT)
            map.setOnClickListener { openMap(e) }
        } else {
            map.text = "No fix"
            map.setTextColor(Ui.MUTED)
            map.setOnClickListener(null)
        }
        return row
    }

    private fun openMap(e: AlertLog.Entry) {
        val label = Uri.encode("${AlertLog.toneName(e.tone)} ${clock.format(Date(e.atMillis))}")
        val uri = Uri.parse("geo:${e.lat},${e.lon}?q=${e.lat},${e.lon}($label)")
        try {
            startActivity(Intent(Intent.ACTION_VIEW, uri))
        } catch (x: Exception) {
            toast("No maps app found")
        }
    }

    private fun shareHistory() {
        if (AlertLog.all(this).isEmpty()) { toast("Nothing to share yet"); return }
        share(listOf(AlertLog.exportCsv(this)), "Alert history")
    }

    private fun confirmClear() {
        AlertDialog.Builder(this)
            .setTitle("Clear the alert history?")
            .setMessage("This deletes every saved alert on the phone. The Pi's own logs aren't touched.")
            .setPositiveButton("Clear") { _, _ -> AlertLog.clear(this) }
            .setNegativeButton("Cancel", null)
            .show()
    }

    // ================================================================ Pi

    private fun piTab(): View {
        val col = column(20, 16)
        col.addView(text("Pi", 22f, Ui.TEXT, bold = true))
        val card = text("", 15f, Ui.TEXT).apply {
            background = Ui.rounded(context, Ui.CARD, 14f)
            setPadding(Ui.dpi(context, 16), Ui.dpi(context, 14), Ui.dpi(context, 16), Ui.dpi(context, 14))
            setLineSpacing(0f, 1.3f)
        }
        col.addView(card, lp(top = 12))

        col.addView(LinearLayout(this).apply {
            addView(button("Turn Wi-Fi on", filled = false) {
                toast(DetectorService.instance?.requestWifiOn() ?: "Detector link isn't running")
            }, LinearLayout.LayoutParams(0, Ui.dpi(context, 52), 1f).apply { marginEnd = Ui.dpi(context, 10) })
            addView(button("Shut down", filled = false) { confirmShutdown() },
                LinearLayout.LayoutParams(0, Ui.dpi(context, 52), 1f))
        }, lp(top = 12))

        col.addView(label("RUN LOGS"), lp(top = 24))
        col.addView(text("Download detections.csv and health.csv from the Pi over Bluetooth, then send them anywhere.",
            13f, Ui.MUTED), lp(top = 6))
        val progress = text("", 14f, Ui.ACCENT_TEXT).apply { typeface = Ui.MONO }
        val runs = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        col.addView(button("Get run list from the Pi", filled = false) { loadRuns(runs, progress) }, lp(top = 12))
        col.addView(progress, lp(top = 10))
        col.addView(runs, lp(top = 4))

        refreshers.add {
            val s = DetectorState.status
            val lines = mutableListOf(DetectorState.summary())
            if (s != null) {
                lines.add(if (s.tempC == 0) "Temperature and load: first reading due within a minute"
                    else "Temperature ${s.tempC} °C · load ${s.busyPct} %")
                lines.add("Wi-Fi " + if (s.flagsNow and 0x20 != 0) "on" else "off")
                if (s.flagsNow and 0x5 != 0) lines.add("Power or heat limit right now")
                if (s.flagsNow and 0x10 != 0) lines.add("Storage failed: alerts still work, logs aren't saving")
                if (s.flagsNow and 0x40 != 0) lines.add("Falling behind: skipping some radio data (too hot or slow)")
            }
            if (DetectorState.note.isNotEmpty()) lines.add(DetectorState.note)
            card.text = lines.joinToString("\n")
        }
        return scroll(col)
    }

    private fun loadRuns(box: LinearLayout, progress: TextView) {
        val svc = DetectorService.instance ?: run { toast("Detector link isn't running"); return }
        progress.text = "Asking the Pi..."
        val err = svc.requestFiles('L', "", { _, _ -> }) { _, payload, error ->
            if (payload == null) { progress.text = error; return@requestFiles }
            box.removeAllViews()
            val arr = try { org.json.JSONArray(String(payload)) } catch (x: Exception) {
                progress.text = "The Pi sent a list I couldn't read"; return@requestFiles
            }
            progress.text = if (arr.length() == 0) "No runs saved on the Pi" else "Tap a run to download it"
            for (i in 0 until arr.length()) {
                val o = arr.getJSONObject(i)
                val name = o.getString("name")
                box.addView(runRow(name, o.optLong("bytes")) { download(name, progress) })
            }
        }
        if (err != null) progress.text = err
    }

    private fun runRow(name: String, bytes: Long, onClick: () -> Unit) = LinearLayout(this).apply {
        gravity = Gravity.CENTER_VERTICAL
        minimumHeight = Ui.dpi(context, 56)
        setOnClickListener { onClick() }
        addView(text(runTitle(name), 15f, Ui.TEXT), LinearLayout.LayoutParams(0, WRAP_CONTENT, 1f))
        addView(text(size(bytes), 13f, Ui.MUTED).apply { typeface = Ui.MONO })
        addView(text("Get", 14f, Ui.ACCENT_TEXT, bold = true).apply {
            setPadding(Ui.dpi(context, 16), 0, 0, 0)
        })
    }

    /** live_car_20260101_120000 -> "car · Thu 1 Jan 12:00". */
    private fun runTitle(name: String): String {
        val m = Regex("live_(.*?)_?(\\d{8})_(\\d{6})$").find(name) ?: return name
        val d = SimpleDateFormat("yyyyMMddHHmmss", Locale.UK).parse(m.groupValues[2] + m.groupValues[3])
            ?: return name
        val tag = m.groupValues[1].ifEmpty { "run" }
        return "$tag · " + SimpleDateFormat("EEE d MMM HH:mm", Locale.UK).format(d)
    }

    private fun size(b: Long) = when {
        b < 1024 -> "$b B"
        b < 1024 * 1024 -> "${b / 1024} KB"
        else -> "%.1f MB".format(Locale.UK, b / 1048576.0)
    }

    private fun download(name: String, progress: TextView) {
        val svc = DetectorService.instance ?: run { toast("Detector link isn't running"); return }
        progress.text = "Zipping on the Pi..."
        val err = svc.requestFiles('G', name, { got, total ->
            progress.text = if (total > 0) "Downloading ${got * 100 / total}% (${size(got.toLong())})"
                else "Downloading..."
        }) { _, payload, error ->
            if (payload == null) { progress.text = error; return@requestFiles }
            val files = try { unzip(name, payload) } catch (x: Exception) {
                progress.text = "Couldn't open the download: ${x.message}"; return@requestFiles
            }
            progress.text = "Got ${files.size} files (${size(payload.size.toLong())} zipped)"
            if (files.isEmpty()) toast("That run has no log files") else share(files, runTitle(name))
        }
        if (err != null) progress.text = err
    }

    /** Pi zip -> share/<run>_detections.csv etc. Names are cleaned, never trusted as paths. */
    private fun unzip(run: String, zip: ByteArray): List<File> {
        val dir = File(cacheDir, "share").apply { mkdirs() }
        val out = mutableListOf<File>()
        ZipInputStream(zip.inputStream()).use { z ->
            while (true) {
                val e = z.nextEntry ?: break
                if (e.isDirectory) continue
                val base = File(e.name).name.replace(Regex("[^A-Za-z0-9._-]"), "_")
                val f = File(dir, "${run}_$base")
                f.outputStream().use { z.copyTo(it) }
                out.add(f)
            }
        }
        return out
    }

    private fun share(files: List<File>, title: String) {
        val uris = ArrayList(files.map { FileProvider.getUriForFile(this, "$packageName.files", it) })
        val send = Intent(if (uris.size == 1) Intent.ACTION_SEND else Intent.ACTION_SEND_MULTIPLE).apply {
            type = "text/csv"
            if (uris.size == 1) putExtra(Intent.EXTRA_STREAM, uris[0])
            else putParcelableArrayListExtra(Intent.EXTRA_STREAM, uris)
            putExtra(Intent.EXTRA_SUBJECT, "TETRA logs: $title")
            clipData = ClipData.newRawUri(null, uris[0]).apply { uris.drop(1).forEach { addItem(ClipData.Item(it)) } }
            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        }
        startActivity(Intent.createChooser(send, "Send $title"))
    }

    // ================================================================ Settings

    private fun settingsTab(): View {
        val col = column(20, 16)
        col.addView(text("Settings", 22f, Ui.TEXT, bold = true))

        col.addView(label("ALERTS"), lp(top = 20))
        val mute = valueRow(col, "Mute alerts") { DetectorState.setMuted(this, !DetectorState.muted(this)) }
        val weak = valueRow(col, "Weak alerts") {
            DetectorState.setWeakMode(this, (DetectorState.weakMode(this) + 1) % 3)
        }
        val route = valueRow(col, "Sound route") { DetectorState.setUseMedia(this, !DetectorState.useMedia(this)) }
        val mode = valueRow(col, "City / Motorway") {
            DetectorState.setModeSetting(this, (DetectorState.modeSetting(this) + 1) % 3)
        }
        val cityBelow = valueRow(col, "Auto: City below") { pickMph(true) }
        val motorwayAbove = valueRow(col, "Auto: Motorway above") { pickMph(false) }

        col.addView(label("TUNING"), lp(top = 24))
        val speedInfo = text("", 13f, Ui.MUTED).apply { setPadding(0, Ui.dpi(context, 6), 0, Ui.dpi(context, 6)) }
        col.addView(speedInfo)
        refreshers.add { speedInfo.text = "Speed check: " + DetectorState.speedInfo }
        for (item in Tuning.ALL) {
            val v = valueRow(col, item.label) { pickTuning(item) }
            refreshers.add { v.text = item.show(Tuning.get(this, item)) }
        }
        valueRow(col, "Reset tuning to defaults") {
            AlertDialog.Builder(this)
                .setTitle("Reset tuning?")
                .setMessage("Puts every setting in Tuning back to how it was set up.")
                .setPositiveButton("Reset") { _, _ -> Tuning.resetAll(this); refresh() }
                .setNegativeButton("Cancel", null)
                .show()
        }.text = ""

        col.addView(label("LINK"), lp(top = 24))
        val link = valueRow(col, "Detector link") {
            if (DetectorState.conn == DetectorState.Conn.STOPPED) startLink() else DetectorService.stop(this)
        }
        val auto = valueRow(col, "Auto-start when the Pi is near") {
            if (PiPresence.isPaired(this)) {
                PiPresence.forget(this); refresh(); toast("Pi forgotten. Auto-start off.")
            } else {
                toast("Pi must be on, and phone Location on, for pairing")
                PiPresence.pair(this) { msg -> runOnUiThread { refresh(); toast(msg) } }
            }
        }
        val pin = valueRow(col, "Pi PIN") { askPin() }

        col.addView(label("LOCATION"), lp(top = 24))
        val loc = valueRow(col, "Save where alerts happen") { askLocation() }

        col.addView(label("TEST SOUNDS"), lp(top = 24))
        valueRow(col, "Weak alert") { Tones.alert(0) }.text = "Play"
        valueRow(col, "Medium alert") { Tones.alert(1) }.text = "Play"
        valueRow(col, "STRONG alert") { Tones.alert(2) }.text = "Play"
        valueRow(col, "Detector offline warning") { Tones.offline() }.text = "Play"
        valueRow(col, "\"Tetra active\"") { Tones.active() }.text = "Play"
        valueRow(col, "\"Tetra clear\"") { Tones.clear() }.text = "Play"
        val voice = valueRow(col, "Voice") { pickVoice() }
        refreshers.add {
            val saved = Tones.savedVoice()
            voice.text = Tones.voices().firstOrNull { it.name == saved }?.let { Tones.voiceLabel(it) }
                ?: "Default UK"
        }

        val version = try { packageManager.getPackageInfo(packageName, 0).versionName } catch (x: Exception) { "?" }
        col.addView(text("TETRA Alert $version", 12f, Ui.MUTED), lp(top = 24))

        refreshers.add {
            mute.text = if (DetectorState.muted(this)) "On (clears when Android Auto disconnects)" else "Off"
            mode.text = when (DetectorState.modeSetting(this)) {
                0 -> "Auto (now ${if (DetectorState.autoCity) "City" else "Motorway"})"
                1 -> "City (override)"
                else -> "Motorway (override)"
            }
            cityBelow.text = "${DetectorState.cityBelowMph(this)} mph"
            motorwayAbove.text = "${DetectorState.motorwayAboveMph(this)} mph"
            weak.text = DetectorState.weakText(this)
            route.text = if (DetectorState.useMedia(this)) "Media" else "Navigation prompt"
            link.text = if (DetectorState.conn == DetectorState.Conn.STOPPED) "Off - tap to start" else "On - tap to stop"
            auto.text = if (PiPresence.isPaired(this)) "On" else "Off - tap to pair"
            pin.text = if (DetectorState.pin(this).isEmpty()) "Not set" else "Set"
            loc.text = when {
                AlertLog.hasBackgroundLocation(this) -> "All the time"
                AlertLog.hasLocation(this) -> "Only with app open"
                else -> "Off"
            }
        }
        return scroll(col)
    }

    // ================================================================ actions

    private fun startLink() {
        val missing = linkPerms.filter { checkSelfPermission(it) != PackageManager.PERMISSION_GRANTED }
        if (missing.isNotEmpty()) {
            requestPermissions(missing.toTypedArray(), REQ_LINK)
            return
        }
        DetectorService.start(this)
    }

    /** Foreground location first; Android only offers "all the time" after that, on its own page. */
    private fun askLocation() {
        if (!AlertLog.hasLocation(this)) {
            requestPermissions(arrayOf(Manifest.permission.ACCESS_FINE_LOCATION,
                Manifest.permission.ACCESS_COARSE_LOCATION), REQ_LOC)
        } else if (!AlertLog.hasBackgroundLocation(this)) {
            AlertDialog.Builder(this)
                .setTitle("Allow location all the time")
                .setMessage("Alerts mostly arrive with the screen off. On the next page pick " +
                    "\"Allow all the time\" so each one saves where you were.")
                .setPositiveButton("Continue") { _, _ ->
                    requestPermissions(arrayOf(Manifest.permission.ACCESS_BACKGROUND_LOCATION), REQ_BG)
                }
                .setNegativeButton("Not now", null)
                .show()
        } else {
            toast("Location is already allowed all the time")
        }
    }

    override fun onRequestPermissionsResult(code: Int, p: Array<out String>, res: IntArray) {
        super.onRequestPermissionsResult(code, p, res)
        when (code) {
            REQ_LINK -> {
                val btOk = listOf(Manifest.permission.BLUETOOTH_SCAN, Manifest.permission.BLUETOOTH_CONNECT)
                    .all { checkSelfPermission(it) == PackageManager.PERMISSION_GRANTED }
                if (btOk) DetectorService.start(this)
                else toast("Bluetooth permission is needed to reach the Pi")
                if (AlertLog.hasLocation(this) && !AlertLog.hasBackgroundLocation(this)) askLocation()
            }
            REQ_LOC -> if (AlertLog.hasLocation(this)) askLocation()
        }
        refresh()
    }

    private fun pickTuning(item: Tuning.Item) {
        val cur = Tuning.get(this, item)
        val labels = item.choices.map { item.show(it) + if (it == item.default) "  (default)" else "" }
        AlertDialog.Builder(this)
            .setTitle(item.label)
            .setSingleChoiceItems(labels.toTypedArray(), item.choices.indexOf(cur)) { d, i ->
                Tuning.set(this, item, item.choices[i]); d.dismiss(); refresh()
            }
            .setNegativeButton("Cancel", null)
            .show()
    }

    /** Auto mode speed thresholds; Motorway is never set below City. */
    private fun pickMph(city: Boolean) {
        val opts = (20..80 step 5).toList()
        val c = DetectorState.cityBelowMph(this)
        val m = DetectorState.motorwayAboveMph(this)
        AlertDialog.Builder(this)
            .setTitle(if (city) "City when below" else "Motorway when above")
            .setSingleChoiceItems(opts.map { "$it mph" }.toTypedArray(), opts.indexOf(if (city) c else m)) { d, i ->
                val v = opts[i]
                // Moving one past the other drags it along, so they never cross.
                if (city) DetectorState.setSpeedThresholds(this, v, maxOf(m, v))
                else DetectorState.setSpeedThresholds(this, minOf(c, v), v)
                d.dismiss(); refresh()
            }
            .setNegativeButton("Cancel", null)
            .show()
    }

    /** Pick a TTS voice; each tap previews it, OK keeps it. */
    private fun pickVoice() {
        val vs = Tones.voices()
        if (vs.isEmpty()) {
            toast("No voices available yet. Check Settings > Text-to-speech on the phone.")
            return
        }
        val before = Tones.savedVoice()
        val names = listOf("Default UK") + vs.map { Tones.voiceLabel(it) }
        var picked = vs.indexOfFirst { it.name == before } + 1
        AlertDialog.Builder(this)
            .setTitle("Voice")
            .setSingleChoiceItems(names.toTypedArray(), picked) { _, i ->
                picked = i
                Tones.chooseVoice(if (i == 0) null else vs[i - 1].name)
                Tones.active()                       // preview
            }
            .setPositiveButton("Use this") { _, _ -> refresh() }
            .setNegativeButton("Cancel") { _, _ -> Tones.chooseVoice(before); refresh() }
            .show()
    }

    private fun askPin() {
        val edit = EditText(this).apply {
            inputType = InputType.TYPE_CLASS_NUMBER or InputType.TYPE_NUMBER_VARIATION_PASSWORD
            hint = "Same PIN as on the Pi"
        }
        AlertDialog.Builder(this)
            .setTitle("Pi PIN")
            .setMessage("Used for shutdown, Wi-Fi on and log downloads.")
            .setView(edit)
            .setPositiveButton("Save") { _, _ ->
                DetectorState.setPin(this, edit.text.toString().trim())
                toast("PIN saved"); refresh()
            }
            .setNegativeButton("Cancel", null)
            .show()
    }

    private fun confirmShutdown() {
        AlertDialog.Builder(this)
            .setTitle("Shut down the Pi?")
            .setMessage("Detection stops until the Pi gets power again.")
            .setPositiveButton("Shut down") { _, _ ->
                toast(DetectorService.instance?.requestShutdown() ?: "Detector link isn't running")
            }
            .setNegativeButton("Cancel", null)
            .show()
    }

    // ================================================================ building blocks

    private fun column(hDp: Int, topDp: Int) = LinearLayout(this).apply {
        orientation = LinearLayout.VERTICAL
        setPadding(Ui.dpi(context, hDp), Ui.dpi(context, topDp), Ui.dpi(context, hDp), Ui.dpi(context, 20))
    }

    private fun scroll(v: View) = ScrollView(this).apply { addView(v); isFillViewport = true }

    private fun lp(top: Int = 0) = LinearLayout.LayoutParams(MATCH_PARENT, WRAP_CONTENT)
        .apply { topMargin = Ui.dpi(this@MainActivity, top) }

    private fun text(s: String, size: Float, colour: Int, bold: Boolean = false) = TextView(this).apply {
        text = s
        textSize = size
        setTextColor(colour)
        if (bold) typeface = android.graphics.Typeface.DEFAULT_BOLD
    }

    private fun label(s: String) = text(s, 12f, Ui.MUTED, bold = true).apply { letterSpacing = 0.12f }

    /** A full-width row: name on the left, current value on the right, tap to change. */
    private fun valueRow(parent: LinearLayout, name: String, onClick: () -> Unit): TextView {
        val value = text("", 14f, Ui.MUTED).apply { gravity = Gravity.END }
        parent.addView(LinearLayout(this).apply {
            gravity = Gravity.CENTER_VERTICAL
            minimumHeight = Ui.dpi(context, 52)
            setOnClickListener { onClick() }
            addView(text(name, 15f, Ui.TEXT), LinearLayout.LayoutParams(0, WRAP_CONTENT, 1f))
            addView(value)
        })
        parent.addView(View(this).apply { setBackgroundColor(Ui.LINE) },
            LinearLayout.LayoutParams(MATCH_PARENT, Ui.dpi(this, 1)))
        return value
    }

    private fun button(s: String, filled: Boolean, onClick: () -> Unit) = Button(this).apply {
        text = s
        isAllCaps = false
        textSize = 16f
        stateListAnimator = null
        setTextColor(if (filled) Ui.BG else Ui.TEXT)
        background = if (filled) Ui.rounded(context, Ui.ACCENT, 14f)
            else Ui.rounded(context, Color.TRANSPARENT, 14f, Ui.LINE)
        minHeight = Ui.dpi(context, 56)
        setOnClickListener { onClick() }
    }

    private fun toast(msg: String) = Toast.makeText(this, msg, Toast.LENGTH_LONG).show()

    companion object {
        private const val REQ_LINK = 1
        private const val REQ_LOC = 2
        private const val REQ_BG = 3
    }
}
