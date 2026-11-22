package com.elker54.tetraalert

import android.content.Context

/** Keeps the last crash so the app can show it on the next start (no logcat needed). */
object CrashLog {
    private const val PREFS = "tetra"
    private const val KEY = "last_crash"

    fun install(c: Context) {
        val app = c.applicationContext
        val previous = Thread.getDefaultUncaughtExceptionHandler()
        if (previous is Handler) return
        Thread.setDefaultUncaughtExceptionHandler(Handler(app, previous))
    }

    private class Handler(val app: Context, val previous: Thread.UncaughtExceptionHandler?) :
        Thread.UncaughtExceptionHandler {
        override fun uncaughtException(t: Thread, e: Throwable) {
            val text = e.stackTraceToString().lines().take(12).joinToString("\n")
            app.getSharedPreferences(PREFS, 0).edit().putString(KEY, text).commit()
            previous?.uncaughtException(t, e)
        }
    }

    fun lastAndClear(c: Context): String? {
        val p = c.getSharedPreferences(PREFS, 0)
        val s = p.getString(KEY, null) ?: return null
        p.edit().remove(KEY).apply()
        return s
    }
}
