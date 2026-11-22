package com.elker54.tetraalert

import android.content.Context
import java.util.concurrent.CopyOnWriteArrayList

/** What the phone currently knows. Shared by the service, phone screen and car screen. */
object DetectorState {
    enum class Conn { STOPPED, SEARCHING, CONNECTED, OFFLINE }

    data class LastAlert(val tone: Int, val db: Double, val freqHz: Long, val atMillis: Long)

    @Volatile var conn = Conn.STOPPED
    @Volatile var status: Link.Status? = null
    @Volatile var lastAlert: LastAlert? = null
    @Volatile var note: String = ""

    private val listeners = CopyOnWriteArrayList<() -> Unit>()
    fun addListener(l: () -> Unit) = listeners.add(l)
    fun removeListener(l: () -> Unit) = listeners.remove(l)
    fun changed() = listeners.forEach { it() }

    // Settings, kept across restarts.
    private const val PREFS = "tetra"
    fun muted(c: Context) = c.getSharedPreferences(PREFS, 0).getBoolean("muted", false)
    fun setMuted(c: Context, v: Boolean) {
        c.getSharedPreferences(PREFS, 0).edit().putBoolean("muted", v).apply(); changed()
    }
    /** Snooze: alert sounds off until this time (not saved; a restart clears it). */
    @Volatile var snoozeUntil = 0L
    fun snoozed() = System.currentTimeMillis() < snoozeUntil
    fun snooze(ms: Long) { snoozeUntil = System.currentTimeMillis() + ms; changed() }
    /** Alert sounds (alerts, repeats, clear) are off: muted or snoozed. */
    fun silenced(c: Context) = muted(c) || snoozed() || parked

    /** Parked long enough (Tuning.PARK_MUTE) with Android Auto on: alerts silent until moving. */
    @Volatile var parked = false
    /** Speed / parked-mute status line for Settings (set by DetectorService). */
    @Volatile var speedInfo = "Detector link off"


    fun useMedia(c: Context) = c.getSharedPreferences(PREFS, 0).getBoolean("media", false)
    fun setUseMedia(c: Context, v: Boolean) {
        c.getSharedPreferences(PREFS, 0).edit().putBoolean("media", v).apply(); changed()
    }
    /** Weak alerts: 0 hidden (ignored by the phone; the Pi still logs them), 1 shown silently, 2 with sound. */
    fun weakMode(c: Context) = c.getSharedPreferences(PREFS, 0).getInt("weak_mode", 0)
    fun setWeakMode(c: Context, v: Int) {
        c.getSharedPreferences(PREFS, 0).edit().putInt("weak_mode", v).apply(); changed()
    }
    fun weakShown(c: Context) = weakMode(c) > 0
    fun weakText(c: Context) = when (weakMode(c)) { 0 -> "Hidden"; 1 -> "Shown, silent"; else -> "Shown with sound" }

    /** Drive mode setting: 0 auto by speed, 1 always City, 2 always Motorway. */
    fun modeSetting(c: Context) = c.getSharedPreferences(PREFS, 0).getInt("drive_mode", 0)
    fun setModeSetting(c: Context, v: Int) {
        c.getSharedPreferences(PREFS, 0).edit().putInt("drive_mode", v).apply(); changed()
    }
    /** Auto mode thresholds (mph). Equal = one switch point; a gap = stay as is in between. */
    fun cityBelowMph(c: Context) = c.getSharedPreferences(PREFS, 0).getInt("city_below_mph", 55)
    fun motorwayAboveMph(c: Context) = c.getSharedPreferences(PREFS, 0).getInt("motorway_above_mph", 55)
    fun setSpeedThresholds(c: Context, cityBelow: Int, motorwayAbove: Int) {
        c.getSharedPreferences(PREFS, 0).edit().putInt("city_below_mph", cityBelow)
            .putInt("motorway_above_mph", maxOf(cityBelow, motorwayAbove)).apply(); changed()
    }

    /** What auto mode has decided from GPS speed (set by DetectorService). */
    @Volatile var autoCity = true      // start slow: Motorway only after a minute over 65 mph
    fun city(c: Context) = when (modeSetting(c)) { 1 -> true; 2 -> false; else -> autoCity }
    fun modeText(c: Context) = if (parked) "Parked (muted)" else
        (if (city(c)) "City" else "Motorway") + (if (modeSetting(c) == 0) "" else " (fixed)")

    /**
     * Does this alert make a sound? STRONG always does (the override); medium
     * only in Motorway mode; weak only in Motorway mode with weak sound on.
     */
    fun audible(c: Context, tone: Int) = when (tone) {
        2 -> true
        1 -> when (Tuning.get(c, Tuning.MEDIUM_SOUND)) { 0 -> !city(c); 1 -> true; else -> false }
        else -> weakMode(c) == 2 && !city(c)
    }
    fun pin(c: Context) = c.getSharedPreferences(PREFS, 0).getString("pin", "") ?: ""
    fun setPin(c: Context, v: String) {
        c.getSharedPreferences(PREFS, 0).edit().putString("pin", v).apply()
    }

    fun summary(): String = when (conn) {
        Conn.STOPPED -> "Link off"
        Conn.SEARCHING -> "Looking for the Pi..."
        Conn.OFFLINE -> "DETECTOR OFFLINE"
        Conn.CONNECTED -> when (status?.state) {
            Link.STATE_RUNNING -> "Detector running"
            Link.STATE_NOT_RUNNING -> "Pi on, DETECTOR NOT RUNNING"
            Link.STATE_DONGLE_ERROR -> "Pi on, DONGLE ERROR"
            Link.STATE_STALLED -> "Pi on, DETECTOR STALLED"
            else -> "Connected, waiting for status"
        }
    }

    /** "67°C · 70%", or null before the Pi's first real reading (it sends 0 until then). */
    fun piStats(): String? {
        val s = status ?: return null
        if (conn != Conn.CONNECTED || s.tempC == 0) return null
        return "${s.tempC}°C · ${s.busyPct}%"
    }

    /** Warnings worth showing anywhere, or empty. */
    fun piWarnings(): List<String> {
        val s = status ?: return emptyList()
        return listOfNotNull(
            if (s.flagsNow and 0x10 != 0) "STORAGE FAILED" else null,
            if (s.flagsNow and 0x5 != 0) "POWER/HEAT LIMIT" else null,
            if (s.flagsNow and 0x40 != 0) "PI FALLING BEHIND" else null)
    }

    fun piDetails(): String {
        val s = status ?: return ""
        if (s.tempC == 0) return ""
        val warn = if (s.flagsNow and 0x5 != 0) "  POWER/HEAT LIMIT" else ""
        val disk = (if (s.flagsNow and 0x10 != 0) "  STORAGE FULL (alerts still work)" else "") +
            (if (s.flagsNow and 0x40 != 0) "  FALLING BEHIND" else "")
        val wifi = if (s.flagsNow and 0x20 != 0) ", Wi-Fi on" else ", Wi-Fi off"
        return "Pi ${s.tempC} C, load ${s.busyPct} %$wifi$warn$disk"
    }
}
