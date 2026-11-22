package com.elker54.tetraalert

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/**
 * Installing an update kills the link, and auto-start only fires when the Pi
 * comes into range, which it already is. So restart the link straight after
 * an update if auto-start is set up (the companion pairing also lets us start
 * it from the background).
 */
class UpdateReceiver : BroadcastReceiver() {
    override fun onReceive(c: Context, intent: Intent) {
        if (intent.action != Intent.ACTION_MY_PACKAGE_REPLACED) return
        if (!PiPresence.isPaired(c)) return
        try {
            DetectorService.start(c)
        } catch (e: Exception) {
            // Android refused; the phone app or the Pi reappearing will start it.
        }
        PiPresence.ensureObserving(c)
    }
}
