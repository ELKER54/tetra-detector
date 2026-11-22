package com.elker54.tetraalert

import android.content.Context

/**
 * Phone-side timings and choices the driver can change in Settings > Tuning.
 * Each item: a fixed list of choices (0 often means "off"/"never"), a default,
 * and how to show a value. Pi-side tuning (strength bands etc.) is separate.
 */
object Tuning {
    class Item(val key: String, val label: String, val default: Int,
               val choices: List<Int>, val show: (Int) -> String)

    private fun secs(v: Int) = if (v < 60) "$v s" else if (v % 60 == 0) "${v / 60} min" else "$v s"

    val REPEAT_STRONG = Item("t_repeat_strong_s", "Repeat while STRONG", 4,
        listOf(2, 3, 4, 6, 8, 10, 0)) { if (it == 0) "Off" else "Every $it s" }
    val CLEAR_AFTER = Item("clear_after_s", "Say \"Tetra clear\"", 20,
        listOf(10, 20, 30, 60, 0)) { if (it == 0) "Off" else "After $it s quiet" }
    val SNOOZE = Item("t_snooze_s", "Snooze length", 30,
        listOf(15, 30, 60, 120, 300)) { secs(it) }
    val MODE_HOLD = Item("t_mode_hold_s", "City / Motorway switch delay", 60,
        listOf(15, 30, 60, 120)) { secs(it) }
    /** 0 Motorway only, 1 both modes, 2 never. */
    val MEDIUM_SOUND = Item("t_medium_sound", "Medium alerts sound in", 0,
        listOf(0, 1, 2)) { when (it) { 0 -> "Motorway only"; 1 -> "City and Motorway"; else -> "Never" } }
    val OFFLINE_AFTER = Item("t_offline_after_s", "Warn \"detector offline\" after", 15,
        listOf(10, 15, 30, 60)) { secs(it) }
    val OFFLINE_REPEAT = Item("t_offline_repeat_s", "Repeat offline warning", 120,
        listOf(60, 120, 300, 0)) { if (it == 0) "Never" else "Every ${secs(it)}" }
    val DIM_AFTER = Item("t_dim_after_s", "Meter dims after", 30,
        listOf(15, 30, 60)) { secs(it) }
    val GONE_STOP = Item("t_gone_stop_min", "Give up when the Pi leaves", 5,
        listOf(2, 5, 10, 30)) { "$it min" }

    val PARK_MUTE = Item("t_park_mute_min", "Mute when parked for", 5,
        listOf(2, 5, 10, 15, 0)) { if (it == 0) "Off" else "$it min" }

    val ALL = listOf(PARK_MUTE, REPEAT_STRONG, CLEAR_AFTER, MEDIUM_SOUND, SNOOZE, MODE_HOLD,
        DIM_AFTER, OFFLINE_AFTER, OFFLINE_REPEAT, GONE_STOP)

    private const val PREFS = "tetra"

    fun get(c: Context, i: Item) = c.getSharedPreferences(PREFS, 0).getInt(i.key, i.default)

    fun set(c: Context, i: Item, v: Int) {
        c.getSharedPreferences(PREFS, 0).edit().putInt(i.key, v).apply()
        DetectorState.changed()
    }

    fun ms(c: Context, i: Item) = get(c, i) * 1000L

    fun resetAll(c: Context) {
        c.getSharedPreferences(PREFS, 0).edit().apply { ALL.forEach { remove(it.key) } }.apply()
        DetectorState.changed()
    }
}
