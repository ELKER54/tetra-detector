package com.elker54.tetraalert

import android.Manifest
import android.app.Activity
import android.bluetooth.le.ScanFilter
import android.companion.AssociationInfo
import android.companion.AssociationRequest
import android.companion.BluetoothLeDeviceFilter
import android.companion.CompanionDeviceManager
import android.companion.CompanionDeviceService
import android.companion.DevicePresenceEvent
import android.companion.ObservingDevicePresenceRequest
import android.content.Context
import android.content.IntentSender
import android.content.pm.PackageManager
import android.os.Build
import android.os.ParcelUuid

/**
 * Auto-start: the Pi is "associated" with the phone once (system pop-up), then
 * Android wakes PiPresenceService whenever the Pi is in Bluetooth range and
 * lets it start the detector link from the background.
 */
object PiPresence {
    private const val PREFS = "tetra"
    private const val KEY = "association_id"

    fun associationId(c: Context) = c.getSharedPreferences(PREFS, 0).getInt(KEY, -1)
    fun isPaired(c: Context) = associationId(c) >= 0

    fun pair(activity: Activity, onResult: (String) -> Unit) {
        val cdm = activity.getSystemService(CompanionDeviceManager::class.java)
        val filter = BluetoothLeDeviceFilter.Builder()
            .setScanFilter(ScanFilter.Builder().setServiceUuid(ParcelUuid(Link.SERVICE)).build())
            .build()
        val request = AssociationRequest.Builder()
            .addDeviceFilter(filter)
            .setSingleDevice(true)
            .build()
        cdm.associate(request, activity.mainExecutor, object : CompanionDeviceManager.Callback() {
            override fun onAssociationPending(sender: IntentSender) {
                activity.startIntentSenderForResult(sender, 42, null, 0, 0, 0)
            }

            override fun onAssociationCreated(info: AssociationInfo) {
                activity.getSharedPreferences(PREFS, 0).edit().putInt(KEY, info.id).apply()
                val err = observe(activity, info)
                onResult(if (err == null)
                    "Pi paired. The link will now start by itself when the Pi is in range."
                else "Pi paired, but watching for it failed: $err")
            }

            override fun onFailure(error: CharSequence?) {
                onResult("Pairing failed: ${error ?: "unknown"}. Is the Pi on, and Location switched on?")
            }
        })
    }

    /** Ask Android to wake us when the Pi appears. Returns null, or an error to show. */
    private fun observe(c: Context, info: AssociationInfo): String? = try {
        val cdm = c.getSystemService(CompanionDeviceManager::class.java)
        if (Build.VERSION.SDK_INT >= 36) {
            cdm.startObservingDevicePresence(
                ObservingDevicePresenceRequest.Builder().setAssociationId(info.id).build())
        } else {
            @Suppress("DEPRECATION")
            info.deviceMacAddress?.let { cdm.startObservingDevicePresence(it.toString().uppercase()) }
        }
        null
    } catch (e: Exception) {
        "${e.javaClass.simpleName}: ${e.message}"
    }

    /** Re-arm on every app start (safe to repeat). Fixes pairings left half-done. */
    fun ensureObserving(c: Context): String? {
        val id = associationId(c)
        if (id < 0) return null
        val info = c.getSystemService(CompanionDeviceManager::class.java)
            .myAssociations.firstOrNull { it.id == id }
        if (info == null) {                       // removed in system settings
            c.getSharedPreferences(PREFS, 0).edit().remove(KEY).apply()
            return "The Pi pairing was removed. Pair again for auto-start."
        }
        return observe(c, info)
    }

    fun forget(c: Context) {
        val id = associationId(c)
        if (id >= 0) {
            try {
                c.getSystemService(CompanionDeviceManager::class.java).disassociate(id)
            } catch (_: Exception) {
            }
        }
        c.getSharedPreferences(PREFS, 0).edit().remove(KEY).apply()
    }

    fun haveBluetoothPermission(c: Context) =
        listOf(Manifest.permission.BLUETOOTH_SCAN, Manifest.permission.BLUETOOTH_CONNECT)
            .all { c.checkSelfPermission(it) == PackageManager.PERMISSION_GRANTED }
}

/** Woken by Android when the paired Pi comes into or leaves Bluetooth range. */
class PiPresenceService : CompanionDeviceService() {

    private fun appeared() {
        if (!PiPresence.haveBluetoothPermission(this)) return
        try {
            DetectorService.start(this)
            DetectorService.instance?.piBack()
        } catch (_: Exception) {
            // Shouldn't happen with the companion permissions, but never crash here.
        }
    }

    private fun disappeared() {
        DetectorService.instance?.piGone()
    }

    override fun onDevicePresenceEvent(event: DevicePresenceEvent) {
        when (event.event) {
            DevicePresenceEvent.EVENT_BLE_APPEARED, DevicePresenceEvent.EVENT_BT_CONNECTED -> appeared()
            DevicePresenceEvent.EVENT_BLE_DISAPPEARED, DevicePresenceEvent.EVENT_BT_DISCONNECTED -> disappeared()
        }
    }

    // Before Android 16 the system calls these instead.
    @Deprecated("Replaced by onDevicePresenceEvent on Android 16")
    override fun onDeviceAppeared(info: AssociationInfo) = appeared()

    @Deprecated("Replaced by onDevicePresenceEvent on Android 16")
    override fun onDeviceDisappeared(info: AssociationInfo) = disappeared()
}
