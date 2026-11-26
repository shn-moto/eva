#!/usr/bin/env python3
"""Eva's hearing on the Station: the microphones' samples to the hub, nothing else.

The Station is hardware only (user, 2026-11-26): whatever the chips do themselves (the
ADCs' PGA, set by station-mics-init.sh) is fine, but once the sound is digital nothing more
is done with it here - no echo cancelling, no VAD, no cutting on its weak A53. Its own VAD
went deaf over music for whole songs. All of that runs on shn-linux now
(services/stt/ear_stream.py: WebRTC AEC3 + noise suppression, Silero VAD, the cut).

Capture: 5 words of 32 bits at 16 kHz from the mic board (snddaudio0) = the 7 microphones and
the amplifier taps, on one ADC clock: the canceller's reference stays sample-aligned with the
mics because both travel in the same frames. 320 KB/s over one TCP connection:
    {"v": 1, "sr": 16000, "words": 5, "block": 256}\\n
    then per block: 1 flag byte (1 music plays, 2 her voice plays, 4 test-set recording)
                    + 256 frames x 5 x int32, exactly as arecord gave them.
The flags are read here per block (the body sets them), so they are in step with the sound.
If the hub is away the sound is dropped (never queued for long) and the connection retried.
"""
import collections
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from core import profile  # noqa: E402

SR, WORDS, BLOCK = 16000, 5, 256
NBYTES = BLOCK * WORDS * 4
HUB = urllib.parse.urlparse(profile.service("stt")).hostname
PORT = int(os.environ.get("EVA_EAR_PORT", "8912"))
MUSIC_FLAG = "/run/eva/music"          # app/body.py: the brain holds it while music plays
SPEAKING_FLAG = "/run/eva/speaking"    # app/body.py: held while her voice plays
RECORD_FLAG = "/var/lib/eva/stt-record"
BACKLOG = int(1.0 * SR / BLOCK)        # blocks kept while the connection is (re)made: 1 s


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def flags():
    return (1 * os.path.exists(MUSIC_FLAG) + 2 * os.path.exists(SPEAKING_FLAG)
            + 4 * os.path.exists(RECORD_FLAG))


def capture(out, cond):
    proc = subprocess.Popen(["arecord", "-q", "-D", "hw:snddaudio0,0", "-c", str(WORDS), "-f", "S32_LE",
                             "-r", str(SR), "--period-size=" + str(BLOCK), "-t", "raw"],
                            stdout=subprocess.PIPE, bufsize=0)
    while True:
        raw = b""
        while len(raw) < NBYTES:
            b = proc.stdout.read(NBYTES - len(raw))
            if not b:
                log("capture ended")
                os._exit(1)
            raw += b
        with cond:
            out.append(bytes([flags()]) + raw)
            cond.notify()


def main():
    out, cond = collections.deque(maxlen=BACKLOG), threading.Condition()
    threading.Thread(target=capture, args=(out, cond), daemon=True).start()
    log(f"streaming to {HUB}:{PORT}")
    sent, t_log = 0, time.time()
    while True:
        try:
            s = socket.create_connection((HUB, PORT), timeout=5)
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            s.settimeout(10)
            s.sendall((json.dumps({"v": 1, "sr": SR, "words": WORDS, "block": BLOCK}) + "\n").encode())
            log("hub connected")
            with cond:
                out.clear()                    # what queued while away is stale
            while True:
                with cond:
                    while not out:
                        cond.wait(1)
                    items = list(out)
                    out.clear()
                s.sendall(b"".join(items))
                sent += len(items)
                if time.time() - t_log > 60:
                    log(f"sent {sent * BLOCK / SR:.0f} s of sound in the last minute")
                    sent, t_log = 0, time.time()
        except OSError as e:
            log(f"hub {HUB}:{PORT}: {e} - again in 2 s")
            try:
                s.close()
            except Exception:
                pass
            time.sleep(2)


if __name__ == "__main__":
    main()
