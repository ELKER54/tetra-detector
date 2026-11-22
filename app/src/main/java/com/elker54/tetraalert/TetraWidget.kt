package com.elker54.tetraalert

import android.app.PendingIntent
import android.appwidget.AppWidgetManager
import android.appwidget.AppWidgetProvider
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.widget.RemoteViews
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * Home-screen widget: the same picture as the car screen (meter + last 10
 * minutes), state, and the last alert. Pushed by the detector service; Android
 * never polls it (updatePeriodMillis 0), so it costs nothing when idle.
 */
class TetraWidget : AppWidgetProvider() {

    override fun onUpdate(c: Context, mgr: AppWidgetManager, ids: IntArray) = update(c, force = true)

    companion object {
        private const val MIN_GAP_MS = 60_000L        // status heartbeats arrive every 5 s
        private val main = Handler(Looper.getMainLooper())
        private var lastAt = 0L
        private var lastKey = ""
        private val clock = SimpleDateFormat("HH:mm:ss", Locale.UK)

        /** Redraws now if something that matters changed, else at most once a minute. */
        fun update(c: Context, force: Boolean = false) {
            val app = c.applicationContext
            val mgr = AppWidgetManager.getInstance(app)
            val ids = mgr.getAppWidgetIds(ComponentName(app, TetraWidget::class.java))
            if (ids.isEmpty()) return

            val a = DetectorState.lastAlert ?: AlertLog.lastAlert(app)?.let {
                DetectorState.LastAlert(it.tone, it.db, it.freqHz, it.atMillis)
            }
            // Alerts, connection changes and warnings go out straight away.
            val key = "${DetectorState.conn}|${DetectorState.status?.state}|${a?.atMillis}|" +
                DetectorState.piWarnings().joinToString()
            val nowUp = SystemClock.elapsedRealtime()
            if (!force && key == lastKey && nowUp - lastAt < MIN_GAP_MS) return
            lastKey = key
            lastAt = nowUp

            val now = System.currentTimeMillis()
            val v = RemoteViews(app.packageName, R.layout.widget)
            v.setImageViewBitmap(R.id.widget_picture,
                CarPicture.draw(a, AlertLog.since(app, now - TimelineView.WINDOW_MS), now,
                    Tuning.ms(app, Tuning.DIM_AFTER)))
            v.setTextViewText(R.id.widget_status,
                (listOf(DetectorState.summary()) + listOfNotNull(DetectorState.piStats()) +
                    DetectorState.piWarnings()).joinToString(" · "))
            if (a == null) {
                v.setTextViewText(R.id.widget_db, "--")
                v.setTextColor(R.id.widget_db, Ui.MUTED)
                v.setTextViewText(R.id.widget_detail, "No alerts yet")
            } else {
                v.setTextViewText(R.id.widget_db,
                    "%+.0f dB %s".format(Locale.UK, a.db, AlertLog.toneName(a.tone)))
                v.setTextColor(R.id.widget_db,
                    if (now - a.atMillis > Tuning.ms(app, Tuning.DIM_AFTER)) Ui.MUTED else Ui.toneText(a.tone))
                v.setTextViewText(R.id.widget_detail,
                    "%s · %.4f MHz".format(Locale.UK, clock.format(Date(a.atMillis)), a.freqHz / 1e6))
            }
            v.setOnClickPendingIntent(R.id.widget_root, PendingIntent.getActivity(app, 0,
                Intent(app, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE))
            mgr.updateAppWidget(ids, v)

            // A fresh alert: redraw once more when it turns old, so the meter dims.
            val staleAt = Tuning.ms(app, Tuning.DIM_AFTER) + 1000L
            if (a != null && now - a.atMillis < staleAt) {
                main.postDelayed({ update(app, force = true) }, staleAt - (now - a.atMillis))
            }
        }
    }
}
