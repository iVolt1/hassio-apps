#!/usr/bin/env python3
"""AirPlay 1 (RAOP) sender test for the planned AirPlay 2 -> AirPlay 1 bridge.

Run it by hand in the add-on container:

    docker exec -it addon_2c14ac29_airplay_2_multiroom_audio_beta \
        python3 /usr/sbin/ap1_test.py 192.168.1.184

It streams a click track to the device with cliairplay --protocol raop,
anchored to the wall clock: a short 1 kHz beep at every whole second and a
higher 2 kHz beep every 5th second (so :00, :05, :10 ... of the clock). If
the device honours the start time, the beeps line up with a clock that
shows seconds (time.is, a phone clock).

Then it exercises what the bridge will need:
  - start at a chosen instant          (START_UNIX_MS + ACTION=START)
  - flush and re-anchor mid-stream     (ACTION=FLUSH, then a new START)
  - volume changes                      (VOLUME=)
  - stop                                (ACTION=STOP)
and prints every cliairplay status line with the local time, so the log
shows requested vs actual start times.

Options:
  --port N        RAOP port (default: from mDNS, else 5000)
  --lead MS       how far ahead of "now" each start is scheduled (default 500;
                  cliairplay's RAOP minimum is 200)
  --seconds N     total run time (default 30)
  --volume N      initial volume 0-100 (default 40)
  --debug N       cliairplay --debug level (default 3)
"""

import argparse
import math
import os
import struct
import subprocess
import sys
import tempfile
import threading
import time

RATE = 44100
CHANNELS = 2
BEEP_S = 0.06


def ts() -> str:
    t = time.time()
    return time.strftime("%H:%M:%S", time.localtime(t)) + f".{int(t * 1000) % 1000:03d}"


def say(msg: str) -> None:
    print(f"{ts()}  {msg}", flush=True)


def raop_service(ip: str, wait: float = 8.0):
    """(port, TXT dict) of the _raop._tcp service at ip, or (None, {}).
    ip may also be the device's AirPlay name (or part of it, any case);
    TXT then has its address as "_addr"."""
    try:
        from zeroconf import ServiceBrowser, Zeroconf
    except ImportError:
        return None, {}
    found = {}
    names = []      # service names seen; resolved below, outside zeroconf's thread

    class L:
        def add_service(self, zc, type_, name):
            names.append(name)

        def update_service(self, *a):
            pass

        def remove_service(self, *a):
            pass

    def matches(name, info):
        friendly = name.split("@", 1)[-1].split("._raop")[0]
        return (ip in info.parsed_addresses() or ip.lower() in friendly.lower()
                or ip.lower().replace(" ", "_") in friendly.lower())

    zc = Zeroconf()
    try:
        ServiceBrowser(zc, "_raop._tcp.local.", L())
        end = time.time() + wait
        done = set()
        while time.time() < end and "svc" not in found:
            for name in [n for n in names if n not in done]:
                done.add(name)
                try:
                    info = zc.get_service_info("_raop._tcp.local.", name, timeout=1500)
                except Exception:          # a slow or odd responder; skip it
                    info = None
                if info and matches(name, info):
                    txt = {k.decode(): (v.decode() if v else "") for k, v in info.properties.items()}
                    v4 = [x for x in info.parsed_addresses() if ":" not in x]
                    txt["_addr"] = (v4 or info.parsed_addresses())[0]
                    found["svc"] = (info.port, txt, name)
                    break
            time.sleep(0.1)
    finally:
        zc.close()
    if "svc" not in found:
        return None, {}
    port, txt, name = found["svc"]
    say(f"mDNS: {name} port {port} TXT {txt}")
    # "MAC@Friendly name._raop._tcp.local." -> "Friendly name"
    txt["_name"] = name.split("@", 1)[-1].split("._raop")[0]
    return port, txt


def click_block(first_sample: int, n: int) -> bytes:
    """n stereo s16le frames of the click track, where sample 0 is a whole
    second of the wall clock (the START instant)."""
    out = bytearray()
    beep = int(BEEP_S * RATE)
    for i in range(first_sample, first_sample + n):
        sec, pos = divmod(i, RATE)
        v = 0
        if pos < beep:
            f = 2000 if sec % 5 == 0 else 1000
            env = min(1.0, pos / 100, (beep - pos) / 100)   # no clicks at the edges
            v = int(12000 * env * math.sin(2 * math.pi * f * pos / RATE))
        out += struct.pack("<hh", v, v)
    return bytes(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("ip")
    ap.add_argument("--port", type=int)
    ap.add_argument("--lead", type=int, default=500)
    ap.add_argument("--seconds", type=int, default=30)
    ap.add_argument("--volume", type=int, default=40)
    ap.add_argument("--debug", type=int, default=3)
    a = ap.parse_args()

    port, txt = raop_service(a.ip)
    if not txt and not a.ip.replace(".", "").isdigit():
        say(f"no AirPlay 1 device called '{a.ip}' found over mDNS")
        return 1
    a.ip = txt.get("_addr", a.ip)
    port = a.port or port or 5000
    cmd = ["cliairplay", "--protocol", "raop", "--port", str(port),
           "--volume", str(a.volume), "--debug", str(a.debug)]
    for k in ("et", "md", "am", "pk", "pw", "cn"):
        if txt.get(k):
            cmd += [f"--{k}", txt[k]]
    tmp = tempfile.mkdtemp(prefix="ap1test-")
    pipe = os.path.join(tmp, "cmd")
    os.mkfifo(pipe)
    cmd += ["--cmdpipe", pipe, a.ip]
    say("starting: " + " ".join(cmd))
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=False)

    events = {}
    started_at = {}

    def reader(stream, tag):
        for raw in stream:
            line = raw.decode(errors="replace").rstrip()
            say(f"[{tag}] {line}")
            for key in ("connected", "audio", "started", "flushed", "stopped"):
                if f"[STATUS] {key}" in line:
                    if key == "started" and "at_unix_ms=" in line:
                        try:
                            req = int(line.split("requested_unix_ms=")[1].split()[0])
                            at = int(line.split("at_unix_ms=")[1].split()[0])
                            started_at["at"] = at
                            say(f"    start honoured {'exactly' if at == req else f'{at - req:+d} ms late'}")
                        except (IndexError, ValueError):
                            pass
                    events.setdefault(key, threading.Event()).set()

    for s, tag in ((proc.stdout, "out"), (proc.stderr, "err")):
        threading.Thread(target=reader, args=(s, tag), daemon=True).start()

    # Open the command pipe once cliairplay has opened its end (it exits
    # instead if it can't connect to the device).
    fd = None
    while fd is None:
        try:
            fd = os.open(pipe, os.O_WRONLY | os.O_NONBLOCK)
        except OSError:
            if proc.poll() is not None:
                time.sleep(0.2)   # let the reader threads print its last lines
                say(f"cliairplay exited with {proc.returncode} before taking commands")
                return 1
            time.sleep(0.05)
    os.set_blocking(fd, True)
    cmdfd = os.fdopen(fd, "w")

    def send(*lines):
        for l in lines:
            say(f"cmd> {l}")
            cmdfd.write(l + "\n")
        cmdfd.flush()

    def wait_for(key, timeout):
        ev = events.setdefault(key, threading.Event())
        ok = ev.wait(timeout)
        ev.clear()
        if not ok:
            say(f"(no [STATUS] {key} within {timeout}s)")
        return ok

    def anchor():
        """The next whole second at least --lead ms away, in unix ms."""
        return int(math.ceil((time.time() * 1000 + a.lead) / 1000.0) * 1000)

    def start():
        """Feed the first block, wait for cliairplay's [STATUS] audio, then
        START at the next whole second >= lead away. Returns the instant
        sample 0 plays (cliairplay moves an infeasible start later; the
        [STATUS] started ack says where)."""
        proc.stdin.write(click_block(0, RATE // 2))
        proc.stdin.flush()
        wait_for("audio", 10)
        t = anchor()
        started_at.clear()
        send(f"START_UNIX_MS={t}", "ACTION=START")
        say(f"beeps due on whole seconds from "
            f"{time.strftime('%H:%M:%S', time.localtime(t / 1000))}.000 (lead {a.lead} ms)")
        wait_for("started", 10)
        return started_at.get("at", t)

    t0 = start()
    sample = RATE // 2

    end = time.time() + a.seconds
    flush_at = time.time() + a.seconds * 0.4
    vol_at = time.time() + a.seconds * 0.7
    flushed = vol_done = False
    try:
        while time.time() < end and proc.poll() is None:
            # Stay ~1 s ahead of what plays now.
            playing = (time.time() * 1000 - t0) / 1000 * RATE
            if sample - playing < RATE:
                n = RATE // 10
                proc.stdin.write(click_block(sample, n))
                proc.stdin.flush()
                sample += n
                continue
            if not flushed and time.time() >= flush_at:
                flushed = True
                send("ACTION=FLUSH")
                wait_for("flushed", 5)
                t0 = start()
                sample = RATE // 2
                continue
            if not vol_done and time.time() >= vol_at:
                vol_done = True
                send(f"VOLUME={max(0, a.volume - 20)}")
                time.sleep(2)
                send(f"VOLUME={a.volume}")
                continue
            time.sleep(0.02)
    except BrokenPipeError:
        say("cliairplay closed its input")
    finally:
        try:
            send("ACTION=STOP")
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        say(f"cliairplay exited with {proc.returncode}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
