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
from open_gopro.features.cohn_feature import CohnFeature
from open_gopro.network.ble.adapters.bleak_wrapper import BleakWrapperController
from open_gopro.network.wifi.controller import SsidState, WifiController

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

STATE = {"fresh": True, "state": "starting", "ble": False, "receiving": False, "wanted": False, "last_error": None,
         "log": collections.deque(maxlen=50)}


class NoWifi(WifiController):
    """The SDK builds a host Wi-Fi driver even for a BLE-only camera and fails without nmcli in the
    container (2026-11-28) - the hub's Wi-Fi is not used at all: the camera joins the home network."""

    def __init__(self, *a, **k):            # no interface lookup
        self._interface, self._password = None, None

    async def connect(self, ssid, password, timeout=15):
        return False

    async def disconnect(self):
        return True

    def current(self):
        return None, SsidState.DISCONNECTED

    def available_interfaces(self):
        return []

    def power(self, power):
        return False

    def is_on(self):
        return False


async def _pair(self, handle):
    """BlueZ's own pairing over D-Bus (bleak), not the SDK's bluetoothctl dialogue: it waits for a
    "#" prompt that BlueZ 5.7x prints as ">" and hung on every connect (2026-11-28). Already paired
    (the host keeps it) - nothing to do."""
    try:
        await handle.pair()
    except Exception as e:                  # already paired / the camera not in pairing mode
        log.info(f"pair: {e}")


BleakWrapperController.pair = _pair


async def _no_cohn(self):
    """COHN (the camera on the home network as a client) exists from HERO12 on: on a HERO11 the SDK
    waited 30 s for its status on every connect and gave up (2026-11-28). The live stream needs none."""


CohnFeature.wait_until_ready = _no_cohn


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
        body = json.dumps({k: (list(v) if k == "log" else v) for k, v in STATE.items() if k != "fresh"},
                          ensure_ascii=False).encode()
        self.send_response(200 if self.path.startswith("/health") else 404)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


async def step(what, coro, timeout=40):
    """One BLE step with a bound: the SDK waits for notifications a HERO11 does not always send."""
    try:
        return await asyncio.wait_for(coro, timeout)
    except asyncio.TimeoutError:
        raise RuntimeError(f"{what}: нет ответа за {timeout} с")


async def start_live(gp):
    """The two commands the SDK's start_stream sends, without its wait for a "starting" notification:
    the stream reached the receiver while that wait hung for good (2026-11-28). Whether it runs is
    the receiver's to say (session)."""
    if SSID:
        r = await step(f"Wi-Fi {SSID}", gp.access_point.connect(SSID, PASSWORD), 75)
        if not r.__class__.__name__.startswith("Success"):
            raise RuntimeError(f"Wi-Fi {SSID}: {r}")
        note(f"камера в сети {SSID}")
    await step("стоп", gp.ble_command.set_shutter(shutter=Toggle.DISABLE), 15)
    r = await step("режим трансляции", gp.ble_command.set_livestream_mode(
        url=RTMP, window_size=getattr(proto.EnumWindowSize, RES), lens=None, encode=False,  # not to the SD card
        minimum_bitrate=800, maximum_bitrate=3000, starting_bitrate=2000), 20)
    if not r.ok:
        raise RuntimeError(f"режим трансляции не принят: {r}")
    await asyncio.sleep(5)                              # the camera gets the stream ready
    await step("старт", gp.ble_command.set_shutter(shutter=Toggle.ENABLE), 15)
    note(f"трансляция запрошена на {RTMP}")


async def session():
    """One BLE connection: keep the stream at the receiver while the camera is wanted."""
    STATE["state"] = "connecting"
    async with WirelessGoPro(TARGET, interfaces={WirelessGoPro.Interface.BLE}, wifi_adapter=NoWifi) as gp:
        STATE["ble"] = True
        note("камера на связи по Bluetooth")
        missing_since = None
        if STATE.pop("fresh", False) and wanted():
            # the first connect after the service starts: the stream restarted on our network and
            # settings, not left as the phone app (or an older config) set it - a bad 5 GHz network was
            # kept that way (2026-11-28)
            note("первое подключение - перезапускаю трансляцию со своими настройками")
            await start_live(gp)
            missing_since = time.time() + 30
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
            backoff = min(30, backoff * 2)           # a camera put into pairing mode is caught within half a minute
        STATE["ble"] = False
        STATE["state"] = "retry"
        await asyncio.sleep(backoff)


if __name__ == "__main__":
    asyncio.run(main())
