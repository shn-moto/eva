#!/usr/bin/env python3
"""A GoPro kept live for Eva's eyes, without the phone app: over Bluetooth (the official Open GoPro SDK)
the camera is joined to the home Wi-Fi and told to live-stream RTMP to the hub's receiver (eva-rtmp),
which gives it to the eyes as RTSP. The SDK keeps a BLE keep-alive going, so the camera does not
power itself off.

    the truth is the receiver: no frames on its path for RESTART_AFTER s -> the stream is (re)started
    the switch is the eyes' camera: off on the admin page «Глаза» -> the stream is stopped
    GET /health (127.0.0.1:8914)   {"state", "ble", "receiving", "wanted", "last_error", "log": [...]}

    GOPRO_RTMP       rtmp://<hub>:1935/live/gopro - where the camera streams to (the hub's LAN address)
    GOPRO_WIFI_SSID  the home network the camera joins (one Quik already saved needs no password)
    GOPRO_WIFI_PASSWORD  only for a network new to the camera
    GOPRO_TARGET     the last 4 digits of the camera's name (several GoPros), empty = the first one
    GOPRO_RESOLUTION 480 | 720 | 1080 (720)

The first connection needs the camera in pairing mode (Connections -> Connect Device -> GoPro Quik):
the pairing is kept by the host's BlueZ, later connections need nothing.
(2026-11-28: HERO11 Black; the camera's own Wi-Fi took one client only and was switched on by Quik
alone, its live stream over the home Wi-Fi has neither problem.)"""
import asyncio
import collections
import http.server
import json
import logging
import os
import threading
import time
import urllib.parse
import urllib.request

from open_gopro import WirelessGoPro
from open_gopro.models import proto, streaming
from open_gopro.models.constants import Toggle

RTMP = os.environ.get("GOPRO_RTMP", "rtmp://127.0.0.1:1935/live/gopro")
PATH = urllib.parse.urlparse(RTMP).path.lstrip("/")                 # live/gopro
SSID = os.environ.get("GOPRO_WIFI_SSID", "")
PASSWORD = os.environ.get("GOPRO_WIFI_PASSWORD", "")
TARGET = os.environ.get("GOPRO_TARGET") or None
RES = {"480": "WINDOW_SIZE_480", "720": "WINDOW_SIZE_720", "1080": "WINDOW_SIZE_1080"}[os.environ.get("GOPRO_RESOLUTION", "720")]
EYES_CONF = os.environ.get("EYES_CONF", "/eyes/eyes.json")
RECEIVER = os.environ.get("RTMP_API", "http://127.0.0.1:9997")
RESTART_AFTER = 15            # s without the stream at the receiver before it is started again
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("gopro")
for noisy in ("bleak", "open_gopro"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

STATE = {"state": "starting", "ble": False, "receiving": False, "wanted": False, "last_error": None,
         "log": collections.deque(maxlen=50)}


def note(msg, error=False):
    (log.error if error else log.info)(msg)
    STATE["log"].appendleft({"t": time.strftime("%d.%m %H:%M:%S"), "level": "error" if error else "info", "msg": msg})
    if error:
        STATE["last_error"] = msg


def wanted():
    """The eyes' camera reading this stream is on (admin «Глаза»)."""
    try:
        with open(EYES_CONF, encoding="utf-8") as f:
            cams = json.load(f).get("cameras", [])
    except (OSError, ValueError):
        return False
    return any(c.get("on", True) and c.get("url", "").rstrip("/").endswith("/" + PATH) for c in cams)


def receiving():
    try:
        with urllib.request.urlopen(f"{RECEIVER}/v3/paths/get/{PATH}", timeout=3) as r:
            return bool(json.loads(r.read()).get("ready"))
    except Exception:
        return False


class Health(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps(dict(STATE, log=list(STATE["log"])), ensure_ascii=False).encode()
        self.send_response(200 if self.path.startswith("/health") else 404)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


async def start_live(gp):
    if SSID:
        r = await gp.access_point.connect(SSID, PASSWORD)
        if not r.__class__.__name__.startswith("Success"):
            raise RuntimeError(f"Wi-Fi {SSID}: {r}")
        note(f"камера в сети {SSID}")
    if gp.streaming.is_streaming:                      # the SDK still holds a stream that died
        await gp.streaming.stop_active_stream()
    r = await gp.streaming.start_stream(streaming.StreamType.LIVE, streaming.LivestreamOptions(
        url=RTMP, resolution=getattr(proto.EnumWindowSize, RES), encode=False,   # encode: not to the SD card
        minimum_bitrate=800, maximum_bitrate=3000, starting_bitrate=2000))
    if not r.__class__.__name__.startswith("Success"):
        raise RuntimeError(f"трансляция не запустилась: {r}")
    note(f"трансляция запущена на {RTMP}")


async def session():
    """One BLE connection: keep the stream at the receiver while the camera is wanted."""
    STATE["state"] = "connecting"
    async with WirelessGoPro(TARGET, interfaces={WirelessGoPro.Interface.BLE}) as gp:
        STATE["ble"] = True
        note("камера на связи по Bluetooth")
        missing_since = None
        while gp.is_ble_connected:
            STATE["wanted"], STATE["receiving"] = wanted(), receiving()
            if not STATE["wanted"]:
                if gp.streaming.is_streaming or STATE["receiving"]:
                    # the shutter ends a live stream whoever started it (the SDK's own stop knows only its own)
                    await gp.ble_command.set_shutter(shutter=Toggle.DISABLE)
                    note("камера выключена в «Глазах» - трансляция остановлена")
                return                                  # BLE released: the camera may sleep
            if STATE["receiving"]:
                missing_since = None
                STATE["state"] = "streaming"
            else:
                missing_since = missing_since or time.time()
                STATE["state"] = "waiting"
                if time.time() - missing_since >= RESTART_AFTER:
                    note("приёмник не получает поток - запускаю трансляцию")
                    await gp.ble_command.set_shutter(shutter=Toggle.DISABLE)     # a stream stuck half-way
                    await asyncio.sleep(3)
                    await start_live(gp)
                    missing_since = time.time() + 30    # give it time to reach the receiver
            await asyncio.sleep(5)
        note("Bluetooth-связь с камерой потеряна", error=True)


async def main():
    threading.Thread(target=http.server.ThreadingHTTPServer(("127.0.0.1", 8914), Health).serve_forever,
                     daemon=True).start()
    backoff = 5
    while True:
        STATE["wanted"], STATE["receiving"] = wanted(), receiving()
        if not STATE["wanted"]:
            STATE.update(state="off", ble=False)
            await asyncio.sleep(10)
            continue
        try:
            await session()
            backoff = 5
        except Exception as e:                          # camera away / asleep / out of range: retry
            note(f"{type(e).__name__}: {e}"[:300], error=True)
            backoff = min(120, backoff * 2)
        STATE["ble"] = False
        STATE["state"] = "retry"
        await asyncio.sleep(backoff)


if __name__ == "__main__":
    asyncio.run(main())
