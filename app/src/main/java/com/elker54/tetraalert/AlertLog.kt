package com.elker54.tetraalert

import android.annotation.SuppressLint
import android.content.Context
import android.content.pm.PackageManager
import android.location.Location
import android.location.LocationManager
import android.os.CancellationSignal
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import java.io.File
import java.util.Locale

/**
 * Every alert from the Pi, with where the phone was. One line per alert in a
 * small text file, newest last, trimmed to the last MAX entries.
 */
object AlertLog {
    data class Entry(val atMillis: Long, val tone: Int, val db: Double, val freqHz: Long,
                     val lat: Double?, val lon: Double?, val accM: Float?)

    /** tone value for "I saw one" marks from the driver (no radio data). */
    const val SIGHTING = -1
    private const val SIGHTING_DEBOUNCE_MS = 3000L
    private var lastSighting = 0L

    private const val FILE = "alerts.txt"
    private const val MAX = 2000
    private const val FRESH_FIX_MS = 15_000L   // a fix this recent is good enough as is
    private const val FIX_WAIT_MS = 12_000L    // then give up and use whatever we have

    private val lock = Any()
    private var cache: MutableList<Entry>? = null

    fun format(e: Entry): String = listOf(
        e.atMillis.toString(), e.tone.toString(), "%.1f".format(Locale.UK, e.db), e.freqHz.toString(),
        e.lat?.let { "%.6f".format(Locale.UK, it) } ?: "",
        e.lon?.let { "%.6f".format(Locale.UK, it) } ?: "",
        e.accM?.let { "%.0f".format(Locale.UK, it) } ?: "",
    ).joinToString(",")

    fun parse(line: String): Entry? {
        val p = line.split(",")
        if (p.size < 7) return null
        return try {
            Entry(p[0].toLong(), p[1].toInt(), p[2].toDouble(), p[3].toLong(),
                p[4].toDoubleOrNull(), p[5].toDoubleOrNull(), p[6].toFloatOrNull())
        } catch (e: NumberFormatException) { null }
    }

    private fun file(c: Context) = File(c.filesDir, FILE)

    /** Newest first. */
    private fun shown(c: Context, e: Entry) = e.tone != 0 || DetectorState.weakShown(c)

    /** Newest first; weak ones left out while weak alerts are hidden. */
    fun all(c: Context): List<Entry> =
        synchronized(lock) { load(c).asReversed().filter { shown(c, it) } }

    /** Radio alerts only (no sightings), oldest first. */
    fun since(c: Context, fromMillis: Long): List<Entry> =
        synchronized(lock) { load(c).filter { it.atMillis >= fromMillis && it.tone >= 0 && shown(c, it) } }

    /** Newest radio alert, skipping sightings. */
    fun lastAlert(c: Context): Entry? =
        synchronized(lock) { load(c).lastOrNull { it.tone >= 0 && shown(c, it) } }

    /** Driver saw a police car, ambulance etc. Returns false for a double tap. */
    fun markSighting(c: Context): Boolean {
        val now = System.currentTimeMillis()
        if (now - lastSighting < SIGHTING_DEBOUNCE_MS) return false
        lastSighting = now
        record(c, SIGHTING, 0.0, 0L, now)
        return true
    }

    private fun load(c: Context): MutableList<Entry> {
        cache?.let { return it }
        val list = try {
            file(c).readLines().mapNotNull(::parse).toMutableList()
        } catch (e: Exception) { mutableListOf() }
        cache = list
        return list
    }

    private fun add(c: Context, e: Entry) {
        synchronized(lock) {
            val list = load(c)
            list.add(e)
            if (list.size > MAX + 200) {          // rewrite now and then rather than every alert
                list.subList(0, list.size - MAX).clear()
                file(c).writeText(list.joinToString("") { format(it) + "\n" })
            } else {
                file(c).appendText(format(e) + "\n")
            }
        }
        DetectorState.changed()
    }

    fun clear(c: Context) {
        synchronized(lock) { cache = mutableListOf(); file(c).delete() }
        DetectorState.changed()
    }

    /** History as a CSV for sharing. */
    fun exportCsv(c: Context): File {
        val fmt = java.text.SimpleDateFormat("yyyy-MM-dd HH:mm:ss", Locale.UK)
        val out = File(File(c.cacheDir, "share").apply { mkdirs() }, "alert_history.csv")
        out.writeText("time,strength,db,freq_mhz,lat,lon,accuracy_m\n" +
            synchronized(lock) { load(c).toList() }.joinToString("") {
                val radio = it.tone >= 0
                listOf(fmt.format(java.util.Date(it.atMillis)), toneName(it.tone),
                    if (radio) "%.1f".format(Locale.UK, it.db) else "",
                    if (radio) "%.4f".format(Locale.UK, it.freqHz / 1e6) else "",
                    it.lat?.toString() ?: "", it.lon?.toString() ?: "",
                    it.accM?.let { a -> "%.0f".format(Locale.UK, a) } ?: "").joinToString(",") + "\n"
            })
        return out
    }

    fun toneName(t: Int) = when (t) { SIGHTING -> "saw one"; 0 -> "weak"; 1 -> "medium"; else -> "STRONG" }

    fun hasLocation(c: Context) =
        c.checkSelfPermission(android.Manifest.permission.ACCESS_FINE_LOCATION) ==
            PackageManager.PERMISSION_GRANTED

    fun hasBackgroundLocation(c: Context) = hasLocation(c) &&
        c.checkSelfPermission(android.Manifest.permission.ACCESS_BACKGROUND_LOCATION) ==
            PackageManager.PERMISSION_GRANTED

    /**
     * Saves the alert straight away if there's a recent fix, otherwise asks for
     * one and saves when it comes (or after FIX_WAIT_MS with the best we have).
     * Never delays the sound: the caller has already played it.
     */
    @SuppressLint("MissingPermission")
    fun record(c: Context, tone: Int, db: Double, freqHz: Long, atMillis: Long) {
        fun save(l: Location?) = add(c, Entry(atMillis, tone, db, freqHz,
            l?.latitude, l?.longitude, l?.let { if (it.hasAccuracy()) it.accuracy else null }))

        if (!hasLocation(c)) { save(null); return }
        val lm = c.getSystemService(LocationManager::class.java)
        val providers = listOf(LocationManager.FUSED_PROVIDER, LocationManager.GPS_PROVIDER,
            LocationManager.NETWORK_PROVIDER).filter {
            try { lm.isProviderEnabled(it) } catch (e: Exception) { false }
        }
        val last = providers.mapNotNull {
            try { lm.getLastKnownLocation(it) } catch (e: Exception) { null }
        }.maxByOrNull { it.elapsedRealtimeNanos }
        val ageMs = last?.let { (SystemClock.elapsedRealtimeNanos() - it.elapsedRealtimeNanos) / 1_000_000 }
        if (providers.isEmpty() || (ageMs != null && ageMs < FRESH_FIX_MS)) { save(last); return }

        val main = Handler(Looper.getMainLooper())
        val cancel = CancellationSignal()
        var done = false
        val finish = { l: Location? ->
            main.post {
                if (!done) {
                    done = true
                    cancel.cancel()
                    save(l ?: last)
                }
            }
        }
        main.postDelayed({ finish(null) }, FIX_WAIT_MS)
        try {
            lm.getCurrentLocation(providers.first(), cancel, c.mainExecutor) { finish(it) }
        } catch (e: Exception) {
            finish(null)
        }
    }
}
