"""
tetra_link.py - the Pi-to-phone message format, shared by live.py and ble_server.py

live.py sends small UDP packets to ble_server.py on this Pi. ble_server.py turns
them into Bluetooth LE notifications for the phone. The phone app uses the
same UUIDs and byte layouts (see the Android app's Link.kt).

All multi-byte values are little-endian.

ALERT characteristic (notify, 9 bytes):
    u16 seq         increments per alert, so the phone can spot repeats
    u8  tone        0 weak, 1 medium, 2 STRONG
    i16 db_x10      strength above noise, dB x 10
    u32 freq_hz     uplink frequency in Hz

STATUS characteristic (notify, 10 bytes, sent every few seconds as a heartbeat):
    u16 seq
    u8  state       0 detector not running, 1 running, 2 dongle error, 3 stalled
    u8  temp_c      Pi CPU temperature (0 if unknown)
    u8  busy_pct    maths load (0 if unknown)
    u8  flags_now   live throttle bits from vcgencmd (bit0 under-voltage now,
                    bit1 CPU capped now, bit2 throttled now, bit3 temp limit now),
                    plus bit4 = results storage full or failing (alerts still work),
                    bit5 = Pi Wi-Fi radio on (set by ble_server.py),
                    bit6 = Pi falling behind (skipped dwells in the last 30 s)
    u32 nonce       current shutdown challenge (changes after every attempt)

CONTROL characteristic (write, 10 bytes): a PIN-protected command
    shutdown:    b"SD" + first 8 bytes of HMAC-SHA256(PIN, nonce as 4 LE bytes)
    Wi-Fi on:    b"WI" + first 8 bytes of HMAC-SHA256(PIN, b"WI" + nonce as 4 LE bytes)
A recorded request is useless later: the nonce changes after every attempt.
(The command is inside the HMAC for WI, so a WI request can't be relabelled SD.)

FILES characteristic (write + notify): download run logs, PIN-protected
    request (write):  cmd (1 byte) + mac (8 bytes) + arg (UTF-8)
        cmd b"L": list runs, arg empty
        cmd b"G": get one run's logs as a zip, arg = run folder name
        mac = first 8 bytes of HMAC-SHA256(PIN, cmd + arg + nonce as 4 LE bytes)
    reply (notifications): one header, then data frames in order
        header: b"H" + kind (1 byte: b"L" list JSON, b"G" zip, b"E" error text)
                + u32 total length + u32 CRC-32 of the whole payload
        data:   b"D" + up to FRAME_DATA bytes
    The Pi sends a fresh STATUS (new nonce) after every request.

LIVE characteristic (notify, 7 bytes): current strength of a busy channel,
    sent at most every LIVE_EVERY_S per channel while it stays active. Never a
    beep on its own: the phone uses it to repeat beeps while STRONG and to say
    "Tetra clear" once things go quiet.
    u8  tone        0 weak, 1 medium, 2 STRONG
    i16 db_x10
    u32 freq_hz

TIME characteristic (write, 8 bytes): the phone's clock on connect
    u64 epoch_ms    milliseconds since 1970 (UTC). The Pi 3B has no clock battery
                    and no internet in the car, so this is how it knows the time.
"""

import hashlib
import hmac

import json
import socket
import struct

SERVICE_UUID = "d11b6b94-3a64-46b5-8721-7f564f0815a0"
ALERT_UUID = "d11b6b95-3a64-46b5-8721-7f564f0815a0"
STATUS_UUID = "d11b6b96-3a64-46b5-8721-7f564f0815a0"
CONTROL_UUID = "d11b6b97-3a64-46b5-8721-7f564f0815a0"
TIME_UUID = "d11b6b98-3a64-46b5-8721-7f564f0815a0"
FILES_UUID = "d11b6b99-3a64-46b5-8721-7f564f0815a0"
LIVE_UUID = "d11b6b9a-3a64-46b5-8721-7f564f0815a0"
LIVE_EVERY_S = 2.0
FRAME_DATA = 180          # data bytes per notification; fits an MTU of 185 or more
# Short on purpose: a 128-bit UUID plus a long name won't fit one advert.
DEVICE_NAME = "TDET"

UDP_ADDR = ("127.0.0.1", 47474)

STATE_NOT_RUNNING, STATE_RUNNING, STATE_DONGLE_ERROR, STATE_STALLED = 0, 1, 2, 3


def pack_alert(seq, tone, db, freq_hz):
    db_x10 = max(-32768, min(32767, int(round(db * 10))))
    return struct.pack("<HBhI", seq & 0xFFFF, tone, db_x10, int(freq_hz))


def pack_live(tone, db, freq_hz):
    db_x10 = max(-32768, min(32767, int(round(db * 10))))
    return struct.pack("<BhI", tone, db_x10, int(freq_hz))


def unpack_live(b):
    tone, db_x10, f = struct.unpack("<BhI", bytes(b))
    return {"tone": tone, "db": db_x10 / 10.0, "freq_hz": f}


def unpack_alert(b):
    seq, tone, db_x10, f = struct.unpack("<HBhI", bytes(b))
    return {"seq": seq, "tone": tone, "db": db_x10 / 10.0, "freq_hz": f}


def pack_status(seq, state, temp_c, busy_pct, flags_now, nonce=0):
    clamp = lambda v: max(0, min(255, int(round(v or 0))))
    return struct.pack("<HBBBBI", seq & 0xFFFF, state, clamp(temp_c),
                       clamp(busy_pct), int(flags_now or 0) & 0x7F, nonce & 0xFFFFFFFF)


def unpack_status(b):
    seq, state, t, busy, fl, nonce = struct.unpack("<HBBBBI", bytes(b))
    return {"seq": seq, "state": state, "temp_c": t, "busy_pct": busy,
            "flags_now": fl, "nonce": nonce}


def shutdown_token(pin, nonce):
    mac = hmac.new(pin.encode(), struct.pack("<I", nonce & 0xFFFFFFFF), hashlib.sha256)
    return b"SD" + mac.digest()[:8]


def files_mac(pin, cmd, arg, nonce):
    msg = cmd + arg.encode() + struct.pack("<I", nonce & 0xFFFFFFFF)
    return hmac.new(pin.encode(), msg, hashlib.sha256).digest()[:8]


def files_request(pin, cmd, arg, nonce):
    return cmd + files_mac(pin, cmd, arg, nonce) + arg.encode()


def files_frames(kind, payload):
    """Header then data frames for one reply."""
    import zlib
    out = [b"H" + kind + struct.pack("<II", len(payload), zlib.crc32(payload) & 0xFFFFFFFF)]
    for i in range(0, len(payload), FRAME_DATA):
        out.append(b"D" + payload[i:i + FRAME_DATA])
    return out


def pack_time(epoch_ms):
    return struct.pack("<Q", int(epoch_ms))


def unpack_time(b):
    return struct.unpack("<Q", bytes(b))[0]


def wifi_on_token(pin, nonce):
    mac = hmac.new(pin.encode(), b"WI" + struct.pack("<I", nonce & 0xFFFFFFFF),
                   hashlib.sha256)
    return b"WI" + mac.digest()[:8]


class Notifier:
    """Fire-and-forget UDP from live.py. Never blocks, never raises."""

    def __init__(self, addr=UDP_ADDR):
        self.addr = addr
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.setblocking(False)
        except OSError:
            self.sock = None

    def _send(self, msg):
        if self.sock is None:
            return
        try:
            self.sock.sendto(json.dumps(msg).encode(), self.addr)
        except OSError:
            pass                       # nobody listening: fine

    def alert(self, tone, db, freq_hz):
        self._send({"type": "alert", "tone": int(tone), "db": float(db),
                    "freq_hz": int(freq_hz)})

    def live(self, tone, db, freq_hz):
        self._send({"type": "live", "tone": int(tone), "db": float(db),
                    "freq_hz": int(freq_hz)})

    def status(self, state, temp_c=None, busy_pct=None, flags_now=None):
        self._send({"type": "status", "state": int(state), "temp_c": temp_c,
                    "busy_pct": busy_pct, "flags_now": flags_now})
