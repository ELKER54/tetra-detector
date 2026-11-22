package com.elker54.tetraalert

import android.annotation.SuppressLint
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothGatt
import android.bluetooth.BluetoothGattCallback
import android.bluetooth.BluetoothGattCharacteristic
import android.bluetooth.BluetoothGattDescriptor
import android.bluetooth.BluetoothManager
import android.bluetooth.BluetoothProfile
import android.bluetooth.BluetoothStatusCodes
import android.bluetooth.le.ScanCallback
import android.bluetooth.le.ScanFilter
import android.bluetooth.le.ScanResult
import android.bluetooth.le.ScanSettings
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.ParcelUuid

/**
 * Keeps the Bluetooth link to the Pi open in the background: finds it,
 * subscribes to alerts and heartbeats, reconnects on its own, plays alerts,
 * and warns once if protection is lost.
 */
@SuppressLint("MissingPermission")   // MainActivity gets the permissions before starting us
class DetectorService : Service() {

    companion object {
        @Volatile var instance: DetectorService? = null
        private const val NOTIF_ID = 1
        private const val CHANNEL = "link"
        private const val RESCAN_DELAY_MS = 6_000L   // Android limits how often scans can start
        private const val NO_SPEED_MS = 30_000L      // no speed reading this long counts as slow
        private const val PARKED_MPH = 3.0           // GPS drift when stood still stays under this
        private const val MOVING_MPH = 5.0           // above this, parked mute ends
        private const val FRESH_MS = 4_500L          // STRONG counts as ongoing this long after the
                                                     // last update (Pi sends at most every 2 s)
        private const val WANT_MTU = 247
        private const val MIN_FILES_MTU = 184         // 180 data bytes + frame byte + 3 ATT bytes
        private const val FILES_IDLE_MS = 20_000L     // no frame for this long: give up

        fun start(c: Context) = c.startForegroundService(Intent(c, DetectorService::class.java))
        fun stop(c: Context) = c.stopService(Intent(c, DetectorService::class.java))
    }

    private val main = Handler(Looper.getMainLooper())
    private var gatt: BluetoothGatt? = null
    private var scanning = false
    private var lastAlertSeq = -1
    private var lastGoodAt = 0L          // last heartbeat saying "detector running"
    private var lastAnyStatusAt = 0L
    private var hadProtection = false
    private var warned = false
    private var warnedAt = 0L
    // Repeat warnings only while Android Auto is connected: with the car off and
    // the phone walking away, one warning is enough.
    private var inCar = false
    private val carType by lazy { androidx.car.app.connection.CarConnection(this).type }
    private val carObserver = androidx.lifecycle.Observer<Int> {
        val was = inCar
        inCar = it == androidx.car.app.connection.CarConnection.CONNECTION_TYPE_PROJECTION
        // Journey over: a mute left on must not carry into the next drive.
        if (was && !inCar && DetectorState.muted(this)) DetectorState.setMuted(this, false)
    }

    // Repeat while STRONG and "Tetra clear" (fed by ALERT and LIVE).
    private var lastStrongAt = 0L       // newest STRONG alert or live update
    private var lastNonWeakAt = 0L      // newest medium or STRONG
    private var lastSoundAt = 0L        // newest alert pips we played
    private var strongEpisode = false   // reached STRONG, "clear" not yet said
    private val strength = object : Runnable {
        override fun run() {
            val now = System.currentTimeMillis()
            val running = DetectorState.conn == DetectorState.Conn.CONNECTED &&
                DetectorState.status?.state == Link.STATE_RUNNING
            val repeatMs = Tuning.ms(this@DetectorService, Tuning.REPEAT_STRONG)
            if (running && repeatMs > 0 &&
                now - lastStrongAt <= FRESH_MS && now - lastSoundAt >= repeatMs) {
                lastSoundAt = now
                if (!DetectorState.silenced(this@DetectorService)) Tones.repeat(2)
            }
            val clearMs = Tuning.ms(this@DetectorService, Tuning.CLEAR_AFTER)
            if (strongEpisode && running && now - lastNonWeakAt > clearMs) {
                strongEpisode = false
                if (clearMs > 0 && !DetectorState.silenced(this@DetectorService)) Tones.clear()
            }
            updateSpeedListening()
            // Parked or stationary, Android often stops sending locations: no reading = slow.
            if (speedListening && now - lastSpeedAt > NO_SPEED_MS) {
                motorwayCandidateSince = 0L
                if (slowSince == 0L) slowSince = now
                if (!DetectorState.autoCity) { DetectorState.autoCity = true; DetectorState.changed() }
            }
            val parkMs = Tuning.get(this@DetectorService, Tuning.PARK_MUTE) * 60_000L
            if (speedListening && parkMs > 0 && slowSince > 0 && now - slowSince >= parkMs &&
                !DetectorState.parked) {
                DetectorState.parked = true; DetectorState.changed()
            }
            if ((!speedListening || parkMs == 0L) && DetectorState.parked) {
                DetectorState.parked = false; DetectorState.changed()
            }
            main.postDelayed(this, 500)
        }
    }

    // ---- City / Motorway from GPS speed (auto mode, only while Android Auto is connected)

    private var speedListening = false
    private var cityCandidateSince = 0L     // speed below the City threshold since then
    private var motorwayCandidateSince = 0L // speed above the Motorway threshold since then
    private var lastSpeedAt = 0L
    private var slowSince = 0L              // under PARKED_MPH (or no readings) since then

    private val speedListener = android.location.LocationListener { loc ->
        if (!loc.hasSpeed()) return@LocationListener
        val now = System.currentTimeMillis()
        val mph = loc.speed * 2.23694
        lastSpeedAt = now
        lastMph = mph
        // Parked auto-mute: slow since slowSince; moving again unmutes straight away.
        if (mph < PARKED_MPH) {
            if (slowSince == 0L) slowSince = now
        } else {
            slowSince = 0L
            if (mph > MOVING_MPH && DetectorState.parked) { DetectorState.parked = false; DetectorState.changed() }
        }
        when {
            mph < DetectorState.cityBelowMph(this) -> {
                motorwayCandidateSince = 0L
                if (cityCandidateSince == 0L) cityCandidateSince = now
                if (!DetectorState.autoCity && now - cityCandidateSince >= Tuning.ms(this, Tuning.MODE_HOLD)) {
                    DetectorState.autoCity = true; DetectorState.changed()
                }
            }
            mph > DetectorState.motorwayAboveMph(this) -> {
                cityCandidateSince = 0L
                if (motorwayCandidateSince == 0L) motorwayCandidateSince = now
                if (DetectorState.autoCity && now - motorwayCandidateSince >= Tuning.ms(this, Tuning.MODE_HOLD)) {
                    DetectorState.autoCity = false; DetectorState.changed()
                }
            }
            else -> { cityCandidateSince = 0L; motorwayCandidateSince = 0L }  // in between: stay
        }
    }

    /** GPS speed only while it can matter: auto mode, in the car, location allowed. */
    @android.annotation.SuppressLint("MissingPermission")
    private fun updateSpeedListening() {
        // "In the car": Android Auto connected, or the Pi linked (it only lives in the car).
        // Parked mute once failed to start in the car with Android Auto on; the Pi check
        // covers that whatever the cause.
        val car = inCar || DetectorState.conn == DetectorState.Conn.CONNECTED
        val want = car && AlertLog.hasLocation(this) &&
            (DetectorState.modeSetting(this) == 0 || Tuning.get(this, Tuning.PARK_MUTE) > 0)
        speedDiag(car)
        if (want == speedListening) return
        val lm = getSystemService(android.location.LocationManager::class.java)
        if (want) {
            // Try each provider until one accepts; remember why if none do.
            var err = "no location provider on"
            for (p in listOf(android.location.LocationManager.FUSED_PROVIDER,
                    android.location.LocationManager.GPS_PROVIDER)) {
                val on = try { lm.isProviderEnabled(p) } catch (e: Exception) { false }
                if (!on) continue
                try {
                    lm.requestLocationUpdates(p, 2000L, 0f, speedListener, mainLooper)
                    speedListening = true
                    speedProvider = p
                    lastSpeedAt = System.currentTimeMillis()
                    speedError = ""
                    return
                } catch (e: Exception) {
                    err = "$p refused: ${e.javaClass.simpleName}"
                }
            }
            speedError = err
        } else {
            lm.removeUpdates(speedListener)
            speedListening = false
            cityCandidateSince = 0L; motorwayCandidateSince = 0L; slowSince = 0L
        }
    }

    private var speedProvider = ""
    private var speedError = ""
    private var lastMph = -1.0

    /** One line for Settings so a parked-mute problem can be seen, not guessed. */
    private fun speedDiag(car: Boolean) {
        val now = System.currentTimeMillis()
        val parkMs = Tuning.get(this, Tuning.PARK_MUTE) * 60_000L
        DetectorState.speedInfo = when {
            !car -> "Off: not in the car (Android Auto ${if (inCar) "on" else "off"}, Pi not linked)"
            !AlertLog.hasLocation(this) -> "Off: location not allowed"
            speedError.isNotEmpty() -> "Off: $speedError"
            !speedListening -> "Off: not needed (auto mode and parked mute both off)"
            DetectorState.parked -> "Parked, muted until over ${MOVING_MPH.toInt()} mph"
            else -> {
                val age = (now - lastSpeedAt) / 1000
                val spd = if (lastMph < 0) "no reading yet" else "%.0f mph %d s ago".format(lastMph, age)
                val left = if (parkMs > 0 && slowSince > 0) " · mutes in ${(parkMs - (now - slowSince)).coerceAtLeast(0) / 1000} s" else ""
                "$speedProvider: $spd$left"
            }
        }
    }

    private fun heard(tone: Int, now: Long) {
        if (tone >= 2) { lastStrongAt = now; strongEpisode = true }
        if (tone >= 1) lastNonWeakAt = now
    }
    private var announced = false        // "Tetra active" said since protection began
    private var mtu = 23

    // Log download in progress, if any.
    private var files: Link.Reassembler? = null
    private var filesProgress: ((Int, Int) -> Unit)? = null
    private var filesDone: ((Char, ByteArray?, String?) -> Unit)? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        instance = this
        carType.observeForever(carObserver)
        DetectorState.autoCity = true
        CrashLog.install(this)
        Tones.init(this)
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(
            NotificationChannel(CHANNEL, "Detector link", NotificationManager.IMPORTANCE_LOW))
        startForeground(NOTIF_ID, notification(), ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE)
        DetectorState.addListener(::refreshNotification)
        DetectorState.addListener(widgetListener)
        set(DetectorState.Conn.SEARCHING)
        startScan()
        main.postDelayed(watchdog, 2000)
        main.postDelayed(strength, 500)
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int) = START_STICKY

    override fun onDestroy() {
        if (files != null) filesFinish(null, "Detector link stopped")
        carType.removeObserver(carObserver)
        inCar = false
        updateSpeedListening()
        main.removeCallbacksAndMessages(null)
        stopScan()
        gatt?.close()
        gatt = null
        instance = null
        DetectorState.removeListener(::refreshNotification)
        DetectorState.removeListener(widgetListener)
        DetectorState.status = null
        set(DetectorState.Conn.STOPPED)
        TetraWidget.update(this, force = true)
        super.onDestroy()
    }

    // ------------------------------------------------------------ state

    private fun set(c: DetectorState.Conn, note: String = DetectorState.note) {
        DetectorState.conn = c
        DetectorState.note = note
        DetectorState.changed()
    }

    private fun notification(): Notification {
        val open = PendingIntent.getActivity(this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE)
        return Notification.Builder(this, CHANNEL)
            .setSmallIcon(R.drawable.ic_app)
            .setContentTitle("TETRA Alert")
            .setContentText(DetectorState.summary())
            .setOngoing(true)
            .setContentIntent(open)
            .build()
    }

    private val widgetListener: () -> Unit = { main.post { TetraWidget.update(this) } }

    private fun refreshNotification() {
        main.post {
            getSystemService(NotificationManager::class.java).notify(NOTIF_ID, notification())
        }
    }

    private val watchdog = object : Runnable {
        override fun run() {
            val now = System.currentTimeMillis()
            // Connected but silent: the link is dead even if Android hasn't noticed.
            val lostMs = Tuning.ms(this@DetectorService, Tuning.OFFLINE_AFTER)
            val rewarnMs = Tuning.ms(this@DetectorService, Tuning.OFFLINE_REPEAT)
            if (gatt != null && lastAnyStatusAt > 0 && now - lastAnyStatusAt > lostMs) {
                dropAndRescan("no heartbeat for ${lostMs / 1000} s")
            }
            // One warning is easy to miss over music, so repeat it while protection is lost.
            if (hadProtection && now - lastGoodAt > lostMs &&
                (!warned || (inCar && rewarnMs > 0 && now - warnedAt > rewarnMs))) {
                warned = true
                warnedAt = now
                announced = false            // say "Tetra active" again when it comes back
                if (DetectorState.conn != DetectorState.Conn.CONNECTED) {
                    set(DetectorState.Conn.OFFLINE)
                }
                if (!DetectorState.muted(this@DetectorService)) Tones.offline()
            }
            main.postDelayed(this, 2000)
        }
    }

    // ------------------------------------------------------------ scanning

    private val scanCb = object : ScanCallback() {
        override fun onScanResult(callbackType: Int, result: ScanResult) {
            if (gatt != null) return
            stopScan()
            set(DetectorState.Conn.SEARCHING, "Found the Pi, connecting...")
            gatt = result.device.connectGatt(this@DetectorService, false, gattCb,
                BluetoothDevice.TRANSPORT_LE)
        }

        override fun onScanFailed(errorCode: Int) {
            scanning = false
            set(DetectorState.Conn.SEARCHING, "Scan failed (code $errorCode), retrying")
            main.postDelayed({ startScan() }, RESCAN_DELAY_MS)
        }
    }

    private fun startScan() {
        if (scanning || gatt != null || instance == null) return
        val bt = getSystemService(BluetoothManager::class.java).adapter
        if (bt == null || !bt.isEnabled) {
            set(DetectorState.Conn.SEARCHING, "Phone Bluetooth is off")
            main.postDelayed({ startScan() }, RESCAN_DELAY_MS)
            return
        }
        val filter = ScanFilter.Builder().setServiceUuid(ParcelUuid(Link.SERVICE)).build()
        val settings = ScanSettings.Builder().setScanMode(ScanSettings.SCAN_MODE_LOW_LATENCY).build()
        bt.bluetoothLeScanner?.startScan(listOf(filter), settings, scanCb)
        scanning = true
    }

    private fun stopScan() {
        if (!scanning) return
        getSystemService(BluetoothManager::class.java).adapter?.bluetoothLeScanner?.stopScan(scanCb)
        scanning = false
    }

    private fun dropAndRescan(why: String) {
        if (files != null) filesFinish(null, "Link to the Pi dropped")
        gatt?.close()
        gatt = null
        lastAnyStatusAt = 0
        DetectorState.status = null
        set(if (warned) DetectorState.Conn.OFFLINE else DetectorState.Conn.SEARCHING, why)
        main.postDelayed({ startScan() }, RESCAN_DELAY_MS)
    }

    // ------------------------------------------------------------ GATT

    private val gattCb = object : BluetoothGattCallback() {
        override fun onConnectionStateChange(g: BluetoothGatt, status: Int, newState: Int) {
            main.post {
                if (newState == BluetoothProfile.STATE_CONNECTED) {
                    set(DetectorState.Conn.SEARCHING, "Connected, setting up...")
                    mtu = 23
                    // Bigger packets for log downloads; carry on either way.
                    if (!g.requestMtu(WANT_MTU)) g.discoverServices()
                } else if (newState == BluetoothProfile.STATE_DISCONNECTED) {
                    if (g == gatt) dropAndRescan("Disconnected")
                    else g.close()
                }
            }
        }

        override fun onMtuChanged(g: BluetoothGatt, newMtu: Int, status: Int) {
            main.post {
                if (status == BluetoothGatt.GATT_SUCCESS) mtu = newMtu
                g.discoverServices()
            }
        }

        override fun onServicesDiscovered(g: BluetoothGatt, status: Int) {
            main.post {
                val svc = g.getService(Link.SERVICE)
                if (svc == null) {
                    dropAndRescan("Pi found but the detector service is missing")
                    return@post
                }
                subscribe(g, svc.getCharacteristic(Link.ALERT))
            }
        }

        // One GATT write at a time: alerts, then status, then logs (older Pis have none).
        override fun onDescriptorWrite(g: BluetoothGatt, d: BluetoothGattDescriptor, status: Int) {
            main.post {
                val svc = g.getService(Link.SERVICE) ?: return@post
                // Optional ones in order; an older Pi simply lacks them.
                val optional = listOf(Link.FILES, Link.LIVE).mapNotNull { svc.getCharacteristic(it) }
                val next = when (val u = d.characteristic.uuid) {
                    Link.ALERT -> svc.getCharacteristic(Link.STATUS)
                    Link.STATUS -> optional.firstOrNull()
                    else -> optional.getOrNull(optional.indexOfFirst { it.uuid == u } + 1)
                }
                if (next != null) subscribe(g, next) else ready(g)
            }
        }

        override fun onCharacteristicChanged(g: BluetoothGatt, c: BluetoothGattCharacteristic,
                                             value: ByteArray) {
            main.post {
                when (c.uuid) {
                    Link.FILES -> onFilesFrame(value)
                    Link.LIVE -> Link.parseLive(value)?.let { heard(it.tone, System.currentTimeMillis()) }
                    else -> onValue(c.uuid, value)
                }
            }
        }

        override fun onCharacteristicWrite(g: BluetoothGatt, c: BluetoothGattCharacteristic,
                                           status: Int) {
            if (c.uuid == Link.CONTROL) {
                main.post {
                    set(DetectorState.conn, if (status == BluetoothGatt.GATT_SUCCESS)
                        "Request sent to the Pi" else "Request to the Pi failed ($status)")
                }
            }
        }
    }

    private fun ready(g: BluetoothGatt) {
        set(DetectorState.Conn.CONNECTED, "")
        sendTime(g)
    }

    /** Give the Pi our clock; it sets its own if it's more than 2 s out. */
    private fun sendTime(g: BluetoothGatt) {
        val c = g.getService(Link.SERVICE)?.getCharacteristic(Link.TIME) ?: return  // older Pi
        write(g, c, Link.packTime(System.currentTimeMillis()))
    }

    /** Android allows one GATT operation at a time; if one is still going, try again shortly. */
    private fun write(g: BluetoothGatt, c: BluetoothGattCharacteristic, value: ByteArray,
                      tries: Int = 12) {
        val r = g.writeCharacteristic(c, value, BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT)
        if (r != BluetoothStatusCodes.SUCCESS && tries > 1 && g == gatt) {
            main.postDelayed({ if (g == gatt) write(g, c, value, tries - 1) }, 250)
        }
    }

    private fun subscribe(g: BluetoothGatt, c: BluetoothGattCharacteristic?) {
        if (c == null) {
            dropAndRescan("Pi service incomplete")
            return
        }
        g.setCharacteristicNotification(c, true)
        g.writeDescriptor(c.getDescriptor(Link.CCCD), BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE)
    }

    private fun onValue(uuid: java.util.UUID, value: ByteArray) {
        val now = System.currentTimeMillis()
        if (uuid == Link.STATUS) {
            val s = Link.parseStatus(value) ?: return
            DetectorState.status = s
            lastAnyStatusAt = now
            if (s.state == Link.STATE_RUNNING) {
                lastGoodAt = now
                hadProtection = true
                warned = false
                if (!announced) {
                    announced = true
                    if (!DetectorState.muted(this)) Tones.active()
                }
            }
            set(DetectorState.Conn.CONNECTED, "")
        } else if (uuid == Link.ALERT) {
            val a = Link.parseAlert(value) ?: return
            if (a.seq == lastAlertSeq || a.seq == 0) return       // 0 = initial placeholder
            lastAlertSeq = a.seq
            if (a.tone == 0 && !DetectorState.weakShown(this)) return   // hidden; the Pi keeps it
            DetectorState.lastAlert = DetectorState.LastAlert(a.tone, a.db, a.freqHz, now)
            val audible = DetectorState.audible(this, a.tone)
            heard(a.tone, now)
            if (audible) lastSoundAt = now      // the next repeat waits a full 4 s
            if (audible && !DetectorState.silenced(this)) Tones.alert(a.tone)
            AlertLog.record(this, a.tone, a.db, a.freqHz, now)
            DetectorState.changed()
        }
    }

    private val stopIfStillGone = Runnable {
        if (DetectorState.conn != DetectorState.Conn.CONNECTED) stopSelf()
    }

    /** Android says the paired Pi left range (car off): stop soon unless it returns. */
    fun piGone() {
        main.removeCallbacks(stopIfStillGone)
        main.postDelayed(stopIfStillGone, Tuning.get(this, Tuning.GONE_STOP) * 60_000L)
    }

    fun piBack() = main.removeCallbacks(stopIfStillGone)

    /** Ask the Pi to switch its Wi-Fi back on (it turns it off away from home). */
    fun requestWifiOn(): String {
        val g = gatt ?: return "Not connected to the Pi"
        val s = DetectorState.status ?: return "No status from the Pi yet"
        val pin = DetectorState.pin(this)
        if (pin.isEmpty()) return "Set the PIN in the phone app first"
        val c = g.getService(Link.SERVICE)?.getCharacteristic(Link.CONTROL)
            ?: return "This Pi has no control channel"
        write(g, c, Link.wifiOnToken(pin, s.nonce))
        return "Asked the Pi to turn Wi-Fi on. Give it 30 s to join your network."
    }

    /** Returns a message for the user; the Pi checks the PIN itself. */
    fun requestShutdown(): String {
        val g = gatt ?: return "Not connected to the Pi"
        val s = DetectorState.status ?: return "No status from the Pi yet"
        val pin = DetectorState.pin(this)
        if (pin.isEmpty()) return "Set the shutdown PIN in the phone app first"
        val c = g.getService(Link.SERVICE)?.getCharacteristic(Link.CONTROL)
            ?: return "This Pi has no shutdown control"
        write(g, c, Link.shutdownToken(pin, s.nonce))
        hadProtection = false          // an intended shutdown shouldn't sound the offline warning
        announced = false
        return "Shutting down the Pi. Wait 20 s before cutting the power."
    }

    // ------------------------------------------------------------ log download

    private val filesTimeout = Runnable { filesFinish(null, "The Pi stopped sending. Try again.") }

    /**
     * Ask the Pi for its run list (cmd 'L') or one run's logs as a zip (cmd 'G').
     * Returns an error to show straight away, or null if the request went out;
     * then onDone gets (kind, payload, null) or (' ', null, error).
     */
    fun requestFiles(cmd: Char, arg: String, onProgress: (Int, Int) -> Unit,
                     onDone: (Char, ByteArray?, String?) -> Unit): String? {
        val g = gatt ?: return "Not connected to the Pi"
        val s = DetectorState.status ?: return "No status from the Pi yet"
        val pin = DetectorState.pin(this)
        if (pin.isEmpty()) return "Set the PIN in Settings first"
        val c = g.getService(Link.SERVICE)?.getCharacteristic(Link.FILES)
            ?: return "The Pi needs the new ble_server.py for log downloads"
        if (mtu < MIN_FILES_MTU) return "Bluetooth packet size too small ($mtu). Stop and start the link."
        if (files != null) return "A download is already running"
        files = Link.Reassembler()
        filesProgress = onProgress
        filesDone = onDone
        main.postDelayed(filesTimeout, FILES_IDLE_MS)
        write(g, c, Link.filesRequest(pin, cmd, arg, s.nonce))
        return null
    }

    private fun onFilesFrame(f: ByteArray) {
        val r = files ?: return
        main.removeCallbacks(filesTimeout)
        when (r.add(f)) {
            Link.Reassembler.Step.IGNORED, Link.Reassembler.Step.PROGRESS -> {
                filesProgress?.invoke(r.received, r.total)
                main.postDelayed(filesTimeout, FILES_IDLE_MS)
            }
            Link.Reassembler.Step.DONE ->
                if (r.kind == 'E') filesFinish(null, String(r.payload))
                else filesFinish(r, null)
            Link.Reassembler.Step.BAD -> filesFinish(null, "Download damaged in transit. Try again.")
        }
    }

    private fun filesFinish(r: Link.Reassembler?, error: String?) {
        main.removeCallbacks(filesTimeout)
        val cb = filesDone
        files = null; filesProgress = null; filesDone = null
        if (r != null) cb?.invoke(r.kind, r.payload, null) else cb?.invoke(' ', null, error)
    }
}
