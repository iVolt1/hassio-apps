#!/usr/bin/env python3
"""Line in -> an AirPlay receiver, step 1: run by hand.

    docker exec -it app_2c14ac29_airplay_2_multiroom_audio_beta \
        python3 /usr/sbin/linein_airplay.py "Kitchen"

Captures PulseAudio's default input (@DEFAULT_SOURCE@, the one set in
pavucontrol/HA's audio settings) and sends it over AirPlay 1 (RAOP, with
cliairplay, as the AirPlay 1 bridge does) to the receiver named on the
command line: meant for one of Music Assistant's AirPlay Receiver plugin
receivers, so MA plays the line in on that receiver's player like any other
AirPlay source.

It sends only while there is a signal: it connects once the input's level
stays above the threshold for a moment, and disconnects after the input has
been below it for --hold seconds, so the receiver (and MA's player) is free
again between uses. Every few seconds it logs the level and state.

Options:
  --threshold-db DB  signal threshold, dBFS (default -50)
  --hold S           seconds below the threshold before disconnecting (default 10)
  --lead MS          time between capture and play, ms (default 500)
  --always           send all the time, ignoring the level
  --source NAME      PulseAudio source (default @DEFAULT_SOURCE@)
  --verbose          cliairplay --debug 4
Stop it with Ctrl-C.
"""

import argparse
import array
import math
import os
import signal
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ap1_bridge import RATE, BPF, Sender  # noqa: E402
from ap1_test import raop_service, say  # noqa: E402

CHUNK_MS = 20
CHUNK = RATE * CHUNK_MS // 1000 * BPF
PRESENT_AFTER_S = 0.5
REPORT_S = 10.0


def level_db(data: bytes) -> float:
    a = array.array("h", data)
    if not a:
        return -120.0
    peak = max(max(a), -min(a))
    return 20 * math.log10(peak / 32768) if peak else -120.0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target", help="AirPlay receiver name (or part of it) or IP")
    ap.add_argument("--threshold-db", type=float, default=-50.0)
    ap.add_argument("--hold", type=float, default=10.0)
    ap.add_argument("--lead", type=int, default=500)
    ap.add_argument("--always", action="store_true")
    ap.add_argument("--source", default="@DEFAULT_SOURCE@")
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()

    port, txt = raop_service(a.target)
    if not port:
        sys.exit(f"no AirPlay receiver matching {a.target!r} found (mDNS _raop._tcp)")
    ip = txt["_addr"]
    say(f"line in ({a.source}) -> {a.target} at {ip}:{port}; TXT {txt}")
    say(f"sending while the level is above {a.threshold_db:g} dBFS"
        if not a.always else "sending all the time (--always)")

    parec = subprocess.Popen(
        ["parec", "-d", a.source, "--format=s16le", f"--rate={RATE}", "--channels=2",
         "--raw", f"--latency-msec={CHUNK_MS}"],
        stdout=subprocess.PIPE)
    sender = None
    above_since = None
    below_since = time.monotonic()
    peak_db, last_report, retry_at = -120.0, time.monotonic(), 0.0

    def stop(*_):
        if sender:
            sender.stop()
        parec.terminate()
        sys.exit(0)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    while True:
        data = parec.stdout.read(CHUNK)
        if not data:
            sys.exit("parec stopped (is the default input still there?)")
        now = time.monotonic()
        db = level_db(data)
        peak_db = max(peak_db, db)
        if db >= a.threshold_db:
            below_since = None
            above_since = above_since or now
        else:
            above_since = None
            below_since = below_since or now
        present = a.always or (above_since is not None and now - above_since >= PRESENT_AFTER_S)
        gone = not a.always and below_since is not None and now - below_since >= a.hold

        if sender and not sender.alive():
            say("the receiver ended the session")
            sender.stop()
            sender, retry_at = None, now + 2.0
        if sender is None and present and now >= retry_at:
            say(f"signal at {db:.0f} dBFS: connecting")
            try:
                sender = Sender(ip, port, txt, a.verbose, raop_lead=a.lead)
            except RuntimeError as e:
                say(f"could not connect: {e}")
                sender, retry_at = None, now + 5.0
                continue
            if not sender.wait("connected", 10):
                say("no answer from the receiver")
                sender.stop()
                sender, retry_at = None, now + 5.0
                continue
            try:
                sender.write(data)              # the first pending sample ...
                sender.proc.stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                continue
            # ... is heard --lead ms after it was captured
            sender.send(f"START_UNIX_MS={int(time.time() * 1000 + a.lead - CHUNK_MS)}",
                        "ACTION=START")
            say("sending")
            continue
        elif sender is not None and gone:
            say(f"no signal for {a.hold:g} s: disconnecting")
            sender.stop()
            sender = None
        if sender is not None:
            try:
                sender.write(data)
                sender.proc.stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                pass        # noticed as not alive on the next chunk
        if now - last_report >= REPORT_S:
            state = "sending" if sender else ("signal" if present else "no signal")
            say(f"level: peak {peak_db:.0f} dBFS over {REPORT_S:g} s, {state}")
            peak_db, last_report = -120.0, now


if __name__ == "__main__":
    main()
