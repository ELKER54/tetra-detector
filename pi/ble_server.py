#!/usr/bin/env python3
"""
ble_server.py - Bluetooth LE link from the Pi detector to the phone app

Listens for alerts and status from live.py (UDP on this Pi only) and passes
them to the phone as BLE notifications. Sends its own heartbeat every
HEARTBEAT_S seconds, so the phone can tell "Pi alive, detector stopped" from
"Pi gone".

Runs as root under systemd (tetra-ble.service): registering a GATT server with
BlueZ needs it. Uses the bless library (BlueZ over D-Bus).

No pairing: anyone in range could subscribe and see the same alerts. The only
writable thing is the shutdown request, which needs the PIN (see PIN_FILE) and
is replay-proof (tetra_link.py). Without a PIN file, shutdown is disabled.
"""

import asyncio
import hmac
import io
import json
import logging
import re
import secrets
import subprocess
import time
import zipfile
from pathlib import Path

import tetra_link as tl

HEARTBEAT_S = 3.0
LIVE_TIMEOUT_S = 15.0        # no status from live.py this long: report stalled
PIN_FILE = Path("/etc/tetra/pin")
MIN_ATTEMPT_GAP_S = 5.0      # slows down anyone guessing PINs
FRAME_GAP_S = 0.008          # pacing between data frames so BlueZ's queue keeps up
MAX_ZIP_BYTES = 20_000_000
RUN_NAME = re.compile(r"live_[A-Za-z0-9_-]{1,80}")
RUN_FILES = ("detections.csv", "health.csv", "activity.csv")

log = logging.getLogger("tetra-ble")


class Link:
    """State kept between live.py messages; no BLE in here so it can be tested."""

    def __init__(self):
        self.alert_seq = 0
        self.status_seq = 0
        self.nonce = secrets.randbits(32)
        self.last_attempt = 0.0
        self.files_block_until = 0.0
        self.last_live = 0.0
        self.live = {"state": tl.STATE_NOT_RUNNING, "temp_c": None,
                     "busy_pct": None, "flags_now": None}

    def on_packet(self, data, now=None):
        """Returns ("alert", bytes) for an alert, None otherwise."""
        now = time.time() if now is None else now
        try:
            msg = json.loads(data.decode())
        except (UnicodeDecodeError, ValueError):
            return None
        if msg.get("type") == "status":
            self.last_live = now
            for k in self.live:
                if k in msg:
                    self.live[k] = msg[k]
            return None
        if msg.get("type") == "live":
            self.last_live = now
            return ("live", tl.pack_live(msg["tone"], msg["db"], msg["freq_hz"]))
        if msg.get("type") == "alert":
            self.last_live = now
            self.alert_seq += 1
            return ("alert", tl.pack_alert(self.alert_seq, msg["tone"], msg["db"],
                                           msg["freq_hz"]))
        return None

    def heartbeat(self, now=None):
        now = time.time() if now is None else now
        state = self.live["state"]
        if self.last_live == 0.0:
            state = tl.STATE_NOT_RUNNING
        elif now - self.last_live > LIVE_TIMEOUT_S and state == tl.STATE_RUNNING:
            state = tl.STATE_STALLED
        self.status_seq += 1
        flags = (self.live["flags_now"] or 0) | (0x20 if wifi_on() else 0)
        return tl.pack_status(self.status_seq, state, self.live["temp_c"],
                              self.live["busy_pct"], flags, self.nonce)

    def check_command(self, data, pin, now=None):
        """"SD" or "WI" if data is a valid command for the current nonce, else None."""
        now = time.time() if now is None else now
        if not pin or now - self.last_attempt < MIN_ATTEMPT_GAP_S:
            return None
        self.last_attempt = now
        data = bytes(data)
        nonce = self.nonce
        self.nonce = secrets.randbits(32)          # every attempt burns the nonce
        for cmd, make in (("SD", tl.shutdown_token), ("WI", tl.wifi_on_token)):
            if hmac.compare_digest(data, make(pin, nonce)):
                return cmd
        return None

    def check_shutdown(self, data, pin, now=None):
        return self.check_command(data, pin, now) == "SD"

    def check_files(self, data, pin, now=None):
        """(cmd, arg) for a valid log request, else None. Only failures are
        slowed down, so a list followed by a download isn't."""
        now = time.time() if now is None else now
        data = bytes(data)
        if not pin or now < self.files_block_until or len(data) < 9:
            return None
        cmd, mac, arg_b = data[:1], data[1:9], data[9:]
        nonce = self.nonce
        self.nonce = secrets.randbits(32)
        try:
            arg = arg_b.decode()
        except UnicodeDecodeError:
            arg = None
        if cmd in (b"L", b"G") and arg is not None and \
                hmac.compare_digest(mac, tl.files_mac(pin, cmd, arg, nonce)):
            return cmd, arg
        self.files_block_until = now + MIN_ATTEMPT_GAP_S
        return None


def runs_dir():
    """The results stick if mounted, else the runs folder next to this script."""
    stick = Path("/mnt/tetra/runs")
    return stick if stick.is_dir() else Path(__file__).resolve().parent / "runs"


def list_runs(base, limit=30):
    """JSON list of the newest runs: name and size of their log files."""
    out = []
    try:
        runs = sorted((p for p in base.iterdir()
                       if p.is_dir() and RUN_NAME.fullmatch(p.name)), reverse=True)
    except OSError:
        runs = []
    for p in runs[:limit]:
        size = 0
        for f in RUN_FILES:
            try:
                size += (p / f).stat().st_size
            except OSError:
                pass
        if size:                      # skip empty folders from failed starts
            out.append({"name": p.name, "bytes": size})
    return json.dumps(out).encode()


def zip_run(base, name):
    """The run's CSVs in one zip. Raises ValueError with a message to show."""
    if not RUN_NAME.fullmatch(name):
        raise ValueError("bad run name")
    run = base / name
    if not run.is_dir():
        raise ValueError("run not found")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in RUN_FILES:
            p = run / f
            if p.is_file():
                # A power cut can leave zero bytes at the end; they only add noise.
                z.writestr(f"{name}/{f}", p.read_bytes().rstrip(b"\x00"))
    data = buf.getvalue()
    if len(data) > MAX_ZIP_BYTES:
        raise ValueError("run too big to send over Bluetooth")
    return data


def wifi_on():
    """True if the Wi-Fi radio isn't blocked (rfkill soft or hard)."""
    for d in Path("/sys/class/rfkill").glob("rfkill*"):
        try:
            if (d / "type").read_text().strip() != "wlan":
                continue
            return (d / "soft").read_text().strip() == "0" and \
                (d / "hard").read_text().strip() == "0"
        except OSError:
            continue
    return False


# Accept phone times only in this window: a sanity check, not security.
TIME_MIN = 1767225600            # 2026-01-01 UTC
TIME_MAX = 4102444800            # 2100-01-01 UTC
TIME_SET_IF_OFF_BY_S = 2.0


def clock_update(data, now=None, run=subprocess.run):
    """Set the system clock from the phone's time if it's sane and off by > 2 s.
    Returns how far the clock moved (s), or None if nothing was done."""
    now = time.time() if now is None else now
    try:
        phone = tl.unpack_time(data) / 1000.0
    except Exception:
        return None
    if not TIME_MIN <= phone <= TIME_MAX:
        return None
    jump = phone - now
    if abs(jump) <= TIME_SET_IF_OFF_BY_S:
        return None
    run(["date", "-u", "-s", f"@{phone:.3f}"], check=False,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return jump


def read_pin():
    try:
        return PIN_FILE.read_text().strip() or None
    except OSError:
        return None


class UdpIn(asyncio.DatagramProtocol):
    def __init__(self, on_packet):
        self.on_packet = on_packet

    def datagram_received(self, data, addr):
        self.on_packet(data)


async def main():
    from bless import (BlessServer, GATTAttributePermissions,
                       GATTCharacteristicProperties)

    link = Link()
    loop = asyncio.get_running_loop()
    server = BlessServer(name=tl.DEVICE_NAME, loop=loop)
    server.read_request_func = lambda ch, **kw: ch.value
    pin = read_pin()
    log.info("shutdown button %s", "enabled" if pin else "disabled (no PIN file)")

    sending = {"busy": False}

    async def send_reply(kind, payload):
        sending["busy"] = True
        try:
            for frame in tl.files_frames(kind, payload):
                send(tl.FILES_UUID, frame)
                await asyncio.sleep(FRAME_GAP_S)
        finally:
            sending["busy"] = False

    def on_files(value):
        req = link.check_files(value, pin)
        send(tl.STATUS_UUID, link.heartbeat())      # fresh nonce for the next request
        if req is None:
            log.warning("log request refused")
            kind, payload = b"E", b"Refused: check the PIN in the app matches the Pi"
        elif sending["busy"]:
            kind, payload = b"E", b"Busy sending, try again in a moment"
        else:
            cmd, arg = req
            try:
                kind, payload = (b"L", list_runs(runs_dir())) if cmd == b"L" \
                    else (b"G", zip_run(runs_dir(), arg))
                log.info("log request %s %s: %d bytes", cmd, arg, len(payload))
            except (ValueError, OSError) as exc:
                kind, payload = b"E", str(exc).encode()
        loop.create_task(send_reply(kind, payload))

    def on_write(ch, value, **kw):
        uuid = str(ch.uuid).lower()
        if uuid == tl.FILES_UUID:
            on_files(value)
            return
        if uuid == tl.TIME_UUID:
            jump = clock_update(value)
            if jump is not None:
                log.warning("clock set from phone (moved %+.0f s)", jump)
            return
        if uuid != tl.CONTROL_UUID:
            return
        cmd = link.check_command(value, pin)
        if cmd == "SD":
            log.warning("valid shutdown request: powering off")
            send(tl.STATUS_UUID, link.heartbeat())
            subprocess.Popen(["systemctl", "poweroff"])
        elif cmd == "WI":
            log.warning("valid Wi-Fi on request")
            subprocess.Popen(["rfkill", "unblock", "wifi"])
        else:
            log.warning("control request refused")
    server.write_request_func = on_write

    props = GATTCharacteristicProperties.read | GATTCharacteristicProperties.notify
    perms = GATTAttributePermissions.readable
    await server.add_new_service(tl.SERVICE_UUID)
    await server.add_new_characteristic(tl.SERVICE_UUID, tl.ALERT_UUID, props,
                                        bytearray(tl.pack_alert(0, 0, 0, 0)), perms)
    await server.add_new_characteristic(tl.SERVICE_UUID, tl.STATUS_UUID, props,
                                        bytearray(link.heartbeat()), perms)
    await server.add_new_characteristic(tl.SERVICE_UUID, tl.LIVE_UUID, props,
                                        bytearray(tl.pack_live(0, 0, 0)), perms)
    await server.add_new_characteristic(tl.SERVICE_UUID, tl.CONTROL_UUID,
                                        GATTCharacteristicProperties.write,
                                        None, GATTAttributePermissions.writeable)
    await server.add_new_characteristic(tl.SERVICE_UUID, tl.TIME_UUID,
                                        GATTCharacteristicProperties.write,
                                        None, GATTAttributePermissions.writeable)
    await server.add_new_characteristic(
        tl.SERVICE_UUID, tl.FILES_UUID,
        GATTCharacteristicProperties.write | GATTCharacteristicProperties.notify,
        None, GATTAttributePermissions.readable | GATTAttributePermissions.writeable)
    await server.start()
    log.info("advertising as %s", tl.DEVICE_NAME)

    def send(uuid, payload):
        server.get_characteristic(uuid).value = bytearray(payload)
        server.update_value(tl.SERVICE_UUID, uuid)

    def on_packet(data):
        out = link.on_packet(data)
        if out and out[0] == "live":
            send(tl.LIVE_UUID, out[1])
        elif out:
            send(tl.ALERT_UUID, out[1])
            log.info("alert %s", tl.unpack_alert(out[1]))

    await loop.create_datagram_endpoint(lambda: UdpIn(on_packet),
                                        local_addr=tl.UDP_ADDR)
    while True:
        send(tl.STATUS_UUID, link.heartbeat())
        await asyncio.sleep(HEARTBEAT_S)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    asyncio.run(main())
