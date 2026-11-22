package com.elker54.tetraalert

import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.util.UUID
import javax.crypto.Mac
import javax.crypto.spec.SecretKeySpec

/** Byte formats shared with the Pi. Must match tetra_link.py exactly. */
object Link {
    val SERVICE: UUID = UUID.fromString("d11b6b94-3a64-46b5-8721-7f564f0815a0")
    val ALERT: UUID = UUID.fromString("d11b6b95-3a64-46b5-8721-7f564f0815a0")
    val STATUS: UUID = UUID.fromString("d11b6b96-3a64-46b5-8721-7f564f0815a0")
    val CONTROL: UUID = UUID.fromString("d11b6b97-3a64-46b5-8721-7f564f0815a0")
    val TIME: UUID = UUID.fromString("d11b6b98-3a64-46b5-8721-7f564f0815a0")
    val FILES: UUID = UUID.fromString("d11b6b99-3a64-46b5-8721-7f564f0815a0")
    val LIVE: UUID = UUID.fromString("d11b6b9a-3a64-46b5-8721-7f564f0815a0")
    val CCCD: UUID = UUID.fromString("00002902-0000-1000-8000-00805f9b34fb")

    const val STATE_NOT_RUNNING = 0
    const val STATE_RUNNING = 1
    const val STATE_DONGLE_ERROR = 2
    const val STATE_STALLED = 3

    data class Alert(val seq: Int, val tone: Int, val db: Double, val freqHz: Long)
    data class Status(val seq: Int, val state: Int, val tempC: Int, val busyPct: Int,
                      val flagsNow: Int, val nonce: Long)

    private fun le(b: ByteArray) = ByteBuffer.wrap(b).order(ByteOrder.LITTLE_ENDIAN)

    fun parseAlert(b: ByteArray): Alert? {
        if (b.size < 9) return null
        val x = le(b)
        return Alert(x.short.toInt() and 0xFFFF, x.get().toInt() and 0xFF,
            x.short / 10.0, x.int.toLong() and 0xFFFFFFFFL)
    }

    /** Current strength of a busy channel (u8 tone, i16 dB x10, u32 Hz). Never a beep by itself. */
    data class Live(val tone: Int, val db: Double, val freqHz: Long)

    fun parseLive(b: ByteArray): Live? {
        if (b.size < 7) return null
        val x = le(b)
        return Live(x.get().toInt() and 0xFF, x.short / 10.0, x.int.toLong() and 0xFFFFFFFFL)
    }

    fun parseStatus(b: ByteArray): Status? {
        if (b.size < 6) return null
        val x = le(b)
        val seq = x.short.toInt() and 0xFFFF
        val state = x.get().toInt() and 0xFF
        val temp = x.get().toInt() and 0xFF
        val busy = x.get().toInt() and 0xFF
        val flags = x.get().toInt() and 0xFF
        val nonce = if (b.size >= 10) x.int.toLong() and 0xFFFFFFFFL else 0L
        return Status(seq, state, temp, busy, flags, nonce)
    }

    /** "SD" + first 8 bytes of HMAC-SHA256(pin, nonce LE). Test: pin 1234,
     *  nonce 0x01020304 gives 53444d11952d0f1f4226. */
    fun shutdownToken(pin: String, nonce: Long): ByteArray {
        val mac = Mac.getInstance("HmacSHA256")
        mac.init(SecretKeySpec(pin.toByteArray(Charsets.UTF_8), "HmacSHA256"))
        val n = le(ByteArray(4)).putInt(nonce.toInt()).array()
        return byteArrayOf('S'.code.toByte(), 'D'.code.toByte()) + mac.doFinal(n).copyOf(8)
    }

    /** Phone clock for the Pi (it has no clock battery and no internet in the car). */
    fun packTime(epochMs: Long): ByteArray = le(ByteArray(8)).putLong(epochMs).array()

    /** "WI" + first 8 bytes of HMAC-SHA256(pin, "WI" + nonce LE). Test: pin 1234,
     *  nonce 0x01020304 gives 574914c6908309120d73. */
    fun wifiOnToken(pin: String, nonce: Long): ByteArray {
        val mac = Mac.getInstance("HmacSHA256")
        mac.init(SecretKeySpec(pin.toByteArray(Charsets.UTF_8), "HmacSHA256"))
        val cmd = byteArrayOf('W'.code.toByte(), 'I'.code.toByte())
        val n = le(ByteArray(4)).putInt(nonce.toInt()).array()
        return cmd + mac.doFinal(cmd + n).copyOf(8)
    }

    /** Log request: cmd + first 8 bytes of HMAC-SHA256(pin, cmd + arg + nonce LE) + arg.
     *  Test: pin 1234, "G", "live_car_1", nonce 0x01020304 gives mac fbb19d47aad8d7f7. */
    fun filesRequest(pin: String, cmd: Char, arg: String, nonce: Long): ByteArray {
        val mac = Mac.getInstance("HmacSHA256")
        mac.init(SecretKeySpec(pin.toByteArray(Charsets.UTF_8), "HmacSHA256"))
        val c = byteArrayOf(cmd.code.toByte())
        val a = arg.toByteArray(Charsets.UTF_8)
        val n = le(ByteArray(4)).putInt(nonce.toInt()).array()
        return c + mac.doFinal(c + a + n).copyOf(8) + a
    }

    /** Puts the Pi's reply frames back together: one "H" header, then "D" data frames. */
    class Reassembler {
        enum class Step { IGNORED, PROGRESS, DONE, BAD }
        var kind = ' '; private set
        var total = 0; private set
        private var crc = 0L
        private var buf: java.io.ByteArrayOutputStream? = null
        val received get() = buf?.size() ?: 0
        val payload: ByteArray get() = buf?.toByteArray() ?: ByteArray(0)

        fun add(f: ByteArray): Step {
            if (f.isEmpty()) return Step.IGNORED
            when (f[0].toInt().toChar()) {
                'H' -> {
                    if (f.size < 10) return Step.BAD
                    val x = le(f)
                    x.get()
                    kind = x.get().toInt().toChar()
                    total = x.int
                    crc = x.int.toLong() and 0xFFFFFFFFL
                    buf = java.io.ByteArrayOutputStream(maxOf(total, 0))
                }
                'D' -> {
                    val b = buf ?: return Step.IGNORED
                    b.write(f, 1, f.size - 1)
                }
                else -> return Step.IGNORED
            }
            val b = buf!!
            if (b.size() < total) return Step.PROGRESS
            val c = java.util.zip.CRC32().apply { update(b.toByteArray()) }.value
            return if (b.size() == total && c == crc) Step.DONE else Step.BAD
        }
    }
}
