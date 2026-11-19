#!/usr/bin/env python3
"""Line in -> an AirPlay receiver.

As a service (--service, started with the add-on): settings come from the
web panel's Line in section (/config/line-in/settings.json), and the state
and level go to /config/line-in/status.json for the panel; the AirPlay
receivers found on the network are listed in discovered.json.

By hand, for testing (stop the service's sending first: switch Line in off
in the panel):
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
  --gain DB          boost the input by DB before sending (default 0), with a
                     limiter so loud passages are turned down instead of
                     clipping; the threshold applies to the input before it
  --source NAME      PulseAudio source (default @DEFAULT_SOURCE@)
  --verbose          cliairplay --debug 4
Stop it with Ctrl-C.
"""

import argparse
import array
import json
import math
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ap1_bridge import RATE, BPF, Sender  # noqa: E402
from ap1_test import best_v4, local_nets, say  # noqa: E402

DIR = Path(os.environ.get("LINEIN_DIR", "/config/line-in"))
DEFAULTS = {"enabled": False, "target": "", "threshold_db": -50.0, "hold_s": 10.0,
            "gain_db": 0.0, "lead_ms": 500}

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


class Gain:
    """A fixed boost with a limiter: the gain drops at once when a chunk
    would clip and comes back slowly (about 6 dB a second)."""

    RELEASE = 10 ** (6 / 20 * CHUNK_MS / 1000)     # per chunk

    def __init__(self, db: float):
        self.target = 10 ** (db / 20)
        self.g = self.target

    def apply(self, data: bytes) -> bytes:
        a = array.array("h", data)
        peak = max(max(a), -min(a)) if a else 0
        self.g = min(self.target, self.g * self.RELEASE)
        if peak and peak * self.g > 32000:
            self.g = 32000 / peak
        if self.g == 1.0:
            return data
        g = self.g
        return array.array("h", [int(v * g) for v in a]).tobytes()


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    os.replace(tmp, path)


def friendly(service_name: str) -> str:
    """'6C5091CAB1CF@BA 790 TA._raop._tcp.local.' -> 'BA 790 TA'"""
    return service_name.split("@", 1)[-1].split("._raop")[0]


class Receivers:
    """The AirPlay receivers on the network (mDNS _raop._tcp), kept up to date;
    with write_to, their names are also written there whenever they change."""

    def __init__(self, write_to: Path | None = None):
        from zeroconf import ServiceBrowser, Zeroconf
        self.found = {}                 # friendly name -> (ip, port, txt)
        self.lock = threading.Lock()
        self.write_to = write_to
        self.pending = []
        self.zc = Zeroconf()
        self.nets = local_nets()
        outer = self

        class L:
            def add_service(self, zc, type_, name):
                outer.pending.append(("add", name))

            def update_service(self, zc, type_, name):
                outer.pending.append(("add", name))

            def remove_service(self, zc, type_, name):
                outer.pending.append(("remove", name))

        ServiceBrowser(self.zc, "_raop._tcp.local.", L())
        threading.Thread(target=self._resolve, daemon=True).start()

    def _resolve(self):
        while True:
            if not self.pending:
                time.sleep(0.2)
                continue
            op, name = self.pending.pop(0)
            if op == "remove":
                with self.lock:
                    changed = self.found.pop(friendly(name), None) is not None
            else:
                try:
                    info = self.zc.get_service_info("_raop._tcp.local.", name, timeout=1500)
                except Exception:
                    info = None
                if not info or not info.parsed_addresses():
                    continue
                txt = {k.decode(): (v.decode() if v else "") for k, v in info.properties.items()}
                ip = best_v4(info.parsed_addresses(), self.nets) or info.parsed_addresses()[0]
                txt["_addr"] = ip
                with self.lock:
                    new = (ip, info.port, txt)
                    changed = self.found.get(friendly(name)) != new
                    self.found[friendly(name)] = new
            if changed and self.write_to:
                with self.lock:
                    names = sorted(self.found, key=str.lower)
                write_json(self.write_to, names)

    def get(self, target: str):
        """(ip, port, txt) of the receiver called target (exactly, any case;
        else the only one whose name contains it; else at that IP), or None."""
        t = target.strip().lower()
        with self.lock:
            items = list(self.found.items())
        exact = [v for k, v in items if k.lower() == t]
        if exact:
            return exact[0]
        part = [v for k, v in items if t and t in k.lower()]
        if len(part) == 1:
            return part[0]
        at = [v for k, v in items if v[0] == target.strip()]
        return at[0] if at else None


class LineIn:
    """Captures the default input and sends it while there is a signal."""

    def __init__(self, settings: dict, verbose=False, status_to: Path | None = None):
        self.s = settings
        self.verbose = verbose
        self.status_to = status_to
        self.sender = None
        self.parec = None
        self.above_since = None
        self.below_since = time.monotonic()
        self.retry_at = 0.0
        self.gain = Gain(settings["gain_db"]) if settings["gain_db"] else None
        self.peak_db = self.out_db = -120.0          # since the last report
        self.st_peak = self.st_out = -120.0          # since the last status
        self.last_report = self.last_status = time.monotonic()
        self.state, self.message, self.dest = "starting", "", None
        self.source = ""

    def start_capture(self, source="@DEFAULT_SOURCE@"):
        self.parec = subprocess.Popen(
            ["parec", "-d", source, "--format=s16le", f"--rate={RATE}", "--channels=2",
             "--raw", f"--latency-msec={CHUNK_MS}"],
            stdout=subprocess.PIPE)

    def close(self):
        if self.sender:
            self.sender.stop()
            self.sender = None
        if self.parec:
            self.parec.terminate()
            try:
                self.parec.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.parec.kill()
            self.parec = None

    def status(self, **extra):
        if not self.status_to:
            return
        write_json(self.status_to, {
            "state": self.state, "message": self.message,
            "level_db": round(self.st_peak, 1),
            "sent_db": round(self.st_out, 1) if self.gain and self.sender else None,
            "receiver": self.dest and f"{self.dest[0]}:{self.dest[1]}",
            "source": self.source, "time": time.time(), **extra})
        self.st_peak = self.st_out = -120.0
        self.last_status = time.monotonic()

    def chunk(self, dest):
        """Reads and handles one chunk; dest is (ip, port, txt) or None.
        False when capture has stopped."""
        data = self.parec.stdout.read(CHUNK)
        if not data:
            return False
        s, now = self.s, time.monotonic()
        db = level_db(data)
        out = self.gain.apply(data) if self.gain else data
        self.peak_db, self.st_peak = max(self.peak_db, db), max(self.st_peak, db)
        if self.gain:
            odb = level_db(out)
            self.out_db, self.st_out = max(self.out_db, odb), max(self.st_out, odb)
        always = s.get("always")
        if db >= s["threshold_db"]:
            self.below_since = None
            self.above_since = self.above_since or now
        else:
            self.above_since = None
            self.below_since = self.below_since or now
        present = always or (self.above_since is not None and now - self.above_since >= PRESENT_AFTER_S)
        gone = not always and self.below_since is not None and now - self.below_since >= s["hold_s"]

        if self.sender and (not self.sender.alive() or (dest and dest[:2] != self.dest[:2])):
            say("the receiver ended the session" if not self.sender.alive()
                else "the receiver moved; reconnecting")
            self.sender.stop()
            self.sender, self.retry_at = None, now + 2.0
        if self.sender is None and present and now >= self.retry_at:
            if dest is None:
                self.message = f"{s['target']!r} not found on the network"
            else:
                self.connect(dest, data if not self.gain else out, db, now)
                return True
        elif self.sender is not None and gone:
            say(f"no signal for {s['hold_s']:g} s: disconnecting")
            self.sender.stop()
            self.sender = None
        if self.sender is not None:
            try:
                self.sender.write(out)
                self.sender.proc.stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                pass        # noticed as not alive on the next chunk
        self.state = "sending" if self.sender else ("signal" if present else "no signal")
        if self.sender or not present:
            self.message = ""
        if now - self.last_report >= REPORT_S:
            sent = f" (sent at {self.out_db:.0f} dBFS)" if self.gain and self.sender else ""
            say(f"level: peak {self.peak_db:.0f} dBFS{sent} over {REPORT_S:g} s, {self.state}")
            self.peak_db, self.out_db, self.last_report = -120.0, -120.0, now
        if now - self.last_status >= 1.0:
            self.status()
        return True

    def connect(self, dest, out, db, now):
        ip, port, txt = dest
        say(f"signal at {db:.0f} dBFS: connecting to {self.s['target']} at {ip}:{port}")
        try:
            sender = Sender(ip, port, txt, self.verbose, raop_lead=int(self.s["lead_ms"]))
        except RuntimeError as e:
            say(f"could not connect: {e}")
            self.message, self.retry_at = f"could not connect: {e}", now + 5.0
            return
        if not sender.wait("connected", 10):
            say("no answer from the receiver")
            sender.stop()
            self.message, self.retry_at = "no answer from the receiver", now + 5.0
            return
        try:
            sender.write(out)               # the first pending sample ...
            sender.proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            sender.stop()
            return
        # ... is heard lead_ms after it was captured
        sender.send(f"START_UNIX_MS={int(time.time() * 1000 + self.s['lead_ms'] - CHUNK_MS)}",
                    "ACTION=START")
        self.sender, self.dest = sender, dest
        self.state, self.message = "sending", ""
        say("sending")


def settings_now() -> dict:
    s = {**DEFAULTS, **read_json(DIR / "settings.json", {})}
    for k in ("threshold_db", "hold_s", "gain_db"):
        try:
            s[k] = float(s[k])
        except (TypeError, ValueError):
            s[k] = DEFAULTS[k]
    return s


def default_source() -> str:
    try:
        return subprocess.run(["pactl", "get-default-source"], capture_output=True,
                              text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def service():
    say("line in service started; settings in the web panel (Line in)")
    receivers = Receivers(DIR / "discovered.json")
    status_file = DIR / "status.json"
    running = {"on": True}

    def stop(*_):
        running["on"] = False
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    li, applied, last_check = None, None, 0.0
    while running["on"]:
        now = time.monotonic()
        if now - last_check >= 2.0 or li is None:
            s, last_check = settings_now(), now
            if s != applied:
                if li:
                    li.close()
                    li = None
                applied = s
                if s["enabled"] and s["target"].strip():
                    src = default_source()
                    say(f"line in on: {src or 'the default input'} -> {s['target']!r}, "
                        f"threshold {s['threshold_db']:g} dBFS, hold {s['hold_s']:g} s"
                        + (f", gain {s['gain_db']:g} dB" if s["gain_db"] else ""))
                    li = LineIn(s, status_to=status_file)
                    li.source = src
                    li.start_capture()
                    li.status()
                else:
                    say("line in off" if not s["enabled"] else "line in on, but no receiver chosen")
                    write_json(status_file, {"state": "off" if not s["enabled"] else "no receiver",
                                             "time": time.time()})
        if li is None:
            time.sleep(0.5)
            continue
        if not li.chunk(receivers.get(applied["target"])):
            say("capture stopped (is the default input still there?); starting again")
            li.close()
            li, applied = None, None
            time.sleep(2)
    if li:
        li.close()
    write_json(status_file, {"state": "off", "time": time.time()})


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target", nargs="?", help="AirPlay receiver name (or part of it) or IP")
    ap.add_argument("--service", action="store_true", help="run as the add-on's service")
    ap.add_argument("--threshold-db", type=float, default=DEFAULTS["threshold_db"])
    ap.add_argument("--hold", type=float, default=DEFAULTS["hold_s"])
    ap.add_argument("--lead", type=int, default=DEFAULTS["lead_ms"])
    ap.add_argument("--always", action="store_true")
    ap.add_argument("--gain", type=float, default=0.0)
    ap.add_argument("--source", default="@DEFAULT_SOURCE@")
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()
    if a.service:
        return service()
    if not a.target:
        ap.error("give a receiver name, or --service")

    receivers = Receivers()
    end = time.time() + 8
    while time.time() < end and receivers.get(a.target) is None:
        time.sleep(0.2)
    dest = receivers.get(a.target)
    if not dest:
        sys.exit(f"no AirPlay receiver matching {a.target!r} found (mDNS _raop._tcp)")
    say(f"line in ({a.source}) -> {a.target} at {dest[0]}:{dest[1]}; TXT {dest[2]}")
    say(f"sending while the level is above {a.threshold_db:g} dBFS"
        if not a.always else "sending all the time (--always)")
    if a.gain:
        say(f"boosting the input by {a.gain:g} dB (limited so it doesn't clip)")
    li = LineIn({"target": a.target, "threshold_db": a.threshold_db, "hold_s": a.hold,
                 "gain_db": a.gain, "lead_ms": a.lead, "always": a.always}, verbose=a.verbose)
    li.start_capture(a.source)

    def stop(*_):
        li.close()
        sys.exit(0)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while li.chunk(receivers.get(a.target) or dest):
        pass
    sys.exit("parec stopped (is the default input still there?)")


if __name__ == "__main__":
    main()
