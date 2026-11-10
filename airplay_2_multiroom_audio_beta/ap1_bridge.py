#!/usr/bin/env python3
"""AirPlay 2 -> AirPlay 1 bridge, step 2a: one device, run by hand.

    docker exec -it app_2c14ac29_airplay_2_multiroom_audio_beta \
        python3 /usr/sbin/ap1_bridge.py 192.168.1.184 --name "Echo Dot AP2"

Starts an AirPlay 2 receiver ("--name") with shairport-sync-ap1bridge, a
build of mikebrady/shairport-sync#2334 whose pipe output puts a timing
header in front of every block of audio: the time its first frame must be
heard (CLOCK_MONOTONIC_RAW, ns). The bridge reads those blocks and feeds
them to cliairplay --protocol raop for the AirPlay 1 device, and tells
cliairplay to start the stream at exactly the first block's play time
(START_UNIX_MS = "the first pending sample is audible at this instant").
The device then plays in step with the AirPlay 2 group the receiver is in.

While playing it compares each block's play time with where the stream
has got to and keeps them together:
  - small differences (clock drift between the two time bases) are
    corrected by adding or dropping a single frame now and then;
  - a jump (the receiver dropped audio it got too late, so its play
    times leap ahead) by filling the gap with silence, or skipping, at once;
  - a very large one (over 2 s) by flushing and starting again at the next
    block.
A flush from the sender (seek, skip, pause) is passed on as a flush and a
new start; the end of a play session stops cliairplay, so the device is
free for other senders between sessions.

Every few seconds it logs the current sync error and the corrections made.

Options:
  --name NAME       AirPlay 2 player name (default "<device name> AP2")
  --port N          its RTSP port (default 5290)
  --udp-base N      its UDP port base (default 7290)
  --raop-port N     the device's RAOP port (default: from mDNS, else 5000)
  --min-lead MS     least time between a block arriving and its play time
                    for a start (default 300; cliairplay's RAOP minimum is 200)
  --verbose         shairport-sync -v and cliairplay --debug 4
Stop it with Ctrl-C.
"""

import argparse
import collections
import os
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ap1_test import raop_service, say  # noqa: E402

NETNS = "ap2"
HEADER = struct.Struct("<IHHIIQII")   # see "ADVANCED TOPICS/PipeOutput.md" in the PR
MAGIC = 0x48545053                    # "SPTH"
TIMED, FLUSH, STOP = 1, 2, 4
RATE, BPF = 44100, 4                  # S16_LE stereo, what cliairplay takes by default
DEADBAND_MS = 1.0                     # drift corrected a frame at a time beyond this
GAP_MS = 5.0                          # a jump beyond this is filled / skipped at once
RESYNC_MS = 2000.0                    # flush + start again beyond this


def mono_to_unix_ms(play_ns: int) -> float:
    """A CLOCK_MONOTONIC_RAW time (ns) as unix ms (what cliairplay takes)."""
    return (play_ns - time.clock_gettime_ns(time.CLOCK_MONOTONIC_RAW) + time.time_ns()) / 1e6


class Sender:
    """One cliairplay --protocol raop session (connect .. STOP)."""

    def __init__(self, ip, port, txt, verbose):
        self.events = collections.defaultdict(threading.Event)
        self.started = {}
        self.verbose = verbose
        self.connected_at = time.time()
        tmp = tempfile.mkdtemp(prefix="ap1bridge-")
        self.pipe = os.path.join(tmp, "cmd")
        os.mkfifo(self.pipe)
        cmd = ["cliairplay", "--protocol", "raop", "--port", str(port), "--volume", "100",
               "--debug", "4" if verbose else "2"]
        for k in ("et", "md", "am", "pk", "pw", "cn"):
            if txt.get(k):
                cmd += [f"--{k}", txt[k]]
        cmd += ["--cmdpipe", self.pipe, ip]
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE)
        for s in (self.proc.stdout, self.proc.stderr):
            threading.Thread(target=self._read, args=(s,), daemon=True).start()
        fd = None
        while fd is None:
            try:
                fd = os.open(self.pipe, os.O_WRONLY | os.O_NONBLOCK)
            except OSError:
                if self.proc.poll() is not None:
                    raise RuntimeError("cliairplay could not connect to the device")
                time.sleep(0.02)
        os.set_blocking(fd, True)
        self.cmd = os.fdopen(fd, "w")

    def _read(self, stream):
        for raw in stream:
            line = raw.decode(errors="replace").rstrip()
            if "playing elapsed_ms" in line:
                continue
            if ("[STATUS]" in line or "[ERROR]" in line or "error" in line.lower()
                    or (self.verbose and any(w in line.lower()
                                             for w in ("retr", "resend", "missing", "lost", "late")))):
                say(f"  cliairplay: {line}")
            for key in ("connected", "audio", "started", "flushed"):
                if f"[STATUS] {key}" in line:
                    if key == "started":
                        try:
                            self.started = {
                                "req": int(line.split("requested_unix_ms=")[1].split()[0]),
                                "at": int(line.split("at_unix_ms=")[1].split()[0])}
                        except (IndexError, ValueError):
                            self.started = {}
                    self.events[key].set()

    def alive(self):
        return self.proc.poll() is None

    def send(self, *lines):
        self.cmd.write("".join(l + "\n" for l in lines))
        self.cmd.flush()

    def wait(self, key, timeout):
        ok = self.events[key].wait(timeout)
        self.events[key].clear()
        return ok

    def write(self, data: bytes):
        self.proc.stdin.write(data)

    def stop(self):
        try:
            self.send("ACTION=STOP")
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()
        for f in (self.proc.stdin, self.cmd):
            try:
                f.close()
            except OSError:
                pass


class Bridge:
    def __init__(self, a, raop_port, txt):
        self.a, self.raop_port, self.txt = a, raop_port, txt
        self.sender = None
        self.queue = collections.deque()   # (play_ns or None, audio) waiting for a start
        self.state = "idle"                # idle | syncing | playing
        self.idle_since = None             # session ended, connection kept open (linger)
        self.start_ms = 0.0                # unix ms at which fed frame 0 plays
        self.fed = 0                       # frames given to cliairplay since the start
        self.min_lead = a.min_lead
        self.skip = 0                      # frames still to skip (a backwards jump)
        self.stats = {"added": 0, "dropped": 0, "gaps": 0, "gap_ms": 0.0,
                      "resyncs": 0, "err": 0.0, "lead": 0.0}
        self.last_report = time.time()

    # -- records from shairport-sync -------------------------------------------------
    def record(self, flags, frames, play_ns, audio):
        if flags & (FLUSH | STOP):
            self.queue.clear()
            if self.sender and self.state == "playing":
                if flags & STOP:
                    say(f"session ended: AirPlay 1 connection kept open for {self.a.linger} s")
                    self.sender.send("ACTION=FLUSH")
                    self.sender.wait("flushed", 5)
                    self.state, self.idle_since = "idle", time.time()
                else:
                    say("flush from the sender")
                    self.sender.send("ACTION=FLUSH")
                    self.sender.wait("flushed", 5)
                    self.state = "syncing"
            elif flags & STOP and self.sender and self.state == "syncing":
                self.sender.send("ACTION=FLUSH")
                self.sender.wait("flushed", 5)
                self.state, self.idle_since = "idle", time.time()
            if not frames:
                return
        if self.state == "playing":
            self.play(flags, frames, play_ns, audio)
        else:
            self.sync(flags, play_ns, audio)

    def sync(self, flags, play_ns, audio):
        """Collect blocks until one can be started on time, then start there."""
        if not (flags & TIMED) and not self.queue:
            return                          # silence before anything timed: skip
        self.queue.append((play_ns if flags & TIMED else None, audio))
        self.idle_since = None
        if self.sender is None or not self.sender.alive():
            say("audio arriving: connecting to the AirPlay 1 device")
            try:
                self.sender = Sender(self.a.ip, self.raop_port, self.txt, self.a.verbose)
            except RuntimeError as e:
                say(f"  {e}; dropping this session's audio")
                self.queue.clear()
                self.sender = None
                return
            self.state = "syncing"
        if not self.sender.events["connected"].is_set():
            return
        # Drop blocks that would play too soon to be started on time, or
        # before the device's clock has had time to sync to ours after a new
        # connection (AirPlay 1 timing settles over the first exchanges).
        now = time.time() * 1000
        not_before = max(now + self.min_lead, self.sender.connected_at * 1000 + self.a.warmup)
        while self.queue:
            p, _ = self.queue[0]
            if p is not None and mono_to_unix_ms(p) >= not_before:
                break
            self.queue.popleft()
        if not self.queue:
            return
        first_ns = self.queue[0][0]
        self.start_ms = mono_to_unix_ms(first_ns)
        self.fed = 0
        self.skip = 0
        for _, data in self.queue:
            self.sender.write(data)
            self.fed += len(data) // BPF
        self.queue.clear()
        self.sender.proc.stdin.flush()
        self.sender.wait("audio", 0.5)    # sent once a packet is buffered: at once
        t = round(self.start_ms)
        self.sender.send(f"START_UNIX_MS={t}", "ACTION=START")
        self.sender.wait("started", 5)
        st = self.sender.started
        lead = self.start_ms - time.time() * 1000
        if st and st["at"] > st["req"] + 2:
            # Moved later than asked: it would be out of step by that much.
            self.min_lead = min(self.min_lead + 100, 1500)
            say(f"start moved {st['at'] - st['req']} ms later than asked; starting again "
                f"(lead now {self.min_lead} ms)")
            self.stats["resyncs"] += 1
            self.sender.send("ACTION=FLUSH")
            self.sender.wait("flushed", 5)
            return
        self.start_ms += (t - self.start_ms)  # what cliairplay was told
        self.state = "playing"
        say(f"started in step: first block plays at {t} (in {lead:.0f} ms)")

    def tick(self):
        """Closes the AirPlay 1 connection after --linger seconds idle."""
        if self.sender and self.idle_since and time.time() - self.idle_since > self.a.linger:
            say("idle: closing the AirPlay 1 connection")
            self.sender.stop()
            self.sender, self.idle_since = None, None

    def play(self, flags, frames, play_ns, audio):
        """Pass a block on, keeping the stream where the play times say it should be."""
        if flags & TIMED:
            # Frames still to be skipped play no time: this block starts that much earlier.
            expected = self.start_ms + (self.fed - self.skip) * 1000.0 / RATE
            actual = mono_to_unix_ms(play_ns)
            err = actual - expected          # > 0: we're early; < 0: we're late
            self.stats["err"] = err
            self.stats["lead"] = actual - time.time() * 1000
            if abs(err) > RESYNC_MS:
                say(f"out of step by {err:+.1f} ms: flushing and starting again")
                self.stats["resyncs"] += 1
                self.sender.send("ACTION=FLUSH")
                self.sender.wait("flushed", 5)
                self.state = "syncing"
                self.sync(flags, play_ns, audio)
                return
            if abs(err) > GAP_MS:
                # The receiver skipped audio it got too late (play time jumped
                # ahead): fill the gap with silence, as the AirPlay 2 rooms
                # play nothing for it. A jump back: skip that much audio.
                n = round(abs(err) * RATE / 1000)
                self.stats["gaps"] += 1
                self.stats["gap_ms"] += abs(err)
                say(f"receiver timeline jumped {err:+.1f} ms: "
                    f"{'filling the gap with silence' if err > 0 else 'skipping that much audio'}"
                    " to stay in step")
                if err > 0:
                    audio = b"\0" * (n * BPF) + audio
                else:
                    self.skip += n
            elif err > DEADBAND_MS and frames:
                audio = audio[:BPF] + audio   # one frame added: the rest plays a frame later
                self.stats["added"] += 1
            elif err < -DEADBAND_MS and frames > 1:
                audio = audio[BPF:]           # one frame dropped: the rest plays a frame earlier
                self.stats["dropped"] += 1
        if self.skip and audio:
            k = min(self.skip, len(audio) // BPF)
            audio = audio[k * BPF:]
            self.skip -= k
        try:
            self.sender.write(audio)
            self.fed += len(audio) // BPF
        except (BrokenPipeError, OSError):
            say("cliairplay went away; starting again with the next block")
            self.sender, self.state = None, "idle"
        if time.time() - self.last_report >= 10:
            self.last_report = time.time()
            s = self.stats
            say(f"in step: error {s['err']:+.2f} ms, lead {s['lead']:.0f} ms, "
                f"frames added {s['added']} dropped {s['dropped']}, "
                f"gaps {s['gaps']} ({s['gap_ms']:.0f} ms), restarts {s['resyncs']}")


def read_exact(f, n):
    buf = b""
    while len(buf) < n:
        chunk = f.read(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("ip")
    ap.add_argument("--name")
    ap.add_argument("--port", type=int, default=5290)
    ap.add_argument("--udp-base", type=int, default=7290)
    ap.add_argument("--raop-port", type=int)
    ap.add_argument("--min-lead", type=int, default=300)
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--linger", type=int, default=300,
                    help="seconds to keep the AirPlay 1 connection open after a session ends")
    ap.add_argument("--warmup", type=int, default=3000,
                    help="ms after a new connection before the device joins")
    a = ap.parse_args()

    # The add-on's AirPlay 2 receivers live in the "ap2" network namespace
    # (their own IP); run there too when it exists.
    def ip_out(*args):
        try:
            return subprocess.run(["ip", *args], capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.TimeoutExpired):
            return ""
    have_ns = NETNS in ip_out("netns", "list").split()
    in_ns = ip_out("netns", "identify", str(os.getpid())).strip() == NETNS
    if have_ns and not in_ns:
        os.execvp("ip", ["ip", "netns", "exec", NETNS, sys.executable, *sys.argv])
    if in_ns:
        os.environ["AP1_BRIDGE_IN_NS"] = "1"
    say(f"network: {'netns ' + NETNS + ' (with the other AirPlay 2 zones)' if in_ns else 'host'}")

    port, txt = raop_service(a.ip)
    raop_port = a.raop_port or port or 5000
    name = a.name or (txt.get("_name") or f"AirPlay 1 {a.ip}") + " AP2"

    work = tempfile.mkdtemp(prefix="ap1bridge-sps-")
    fifo = os.path.join(work, "audio")
    os.mkfifo(fifo)
    iface = 'interface = "ap2mv";' if os.environ.get("AP1_BRIDGE_IN_NS") else ""
    conf = os.path.join(work, "sps.conf")
    with open(conf, "w") as f:
        f.write(f"""general = {{
  name = "{name}";
  port = {a.port};
  udp_port_base = {a.udp_base};
  {iface}
  output_backend = "pipe";
  mdns_backend = "avahi";
}};
sessioncontrol = {{ allow_session_interruption = "yes"; }};
pipe = {{
  name = "{fifo}";
  timing_headers = "yes";
  output_rate = {RATE};
  output_format = "S16_LE";
}};
""")
    sps = subprocess.Popen(["shairport-sync-ap1bridge", "-c", conf] + (["-v"] if a.verbose else []),
                           stdout=None if a.verbose else subprocess.DEVNULL,
                           stderr=None if a.verbose else subprocess.DEVNULL)
    say(f"AirPlay 2 receiver '{name}' on port {a.port}, bridging to {a.ip}:{raop_port} "
        "(AirPlay 1). Play to it, grouped with your other rooms. Ctrl-C to stop.")

    bridge = Bridge(a, raop_port, txt)
    lock = threading.Lock()

    def idle_timer():
        while True:
            time.sleep(1)
            with lock:
                bridge.tick()
    threading.Thread(target=idle_timer, daemon=True).start()
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        while sps.poll() is None:
            with open(fifo, "rb", buffering=0) as f:
                while True:
                    h = read_exact(f, HEADER.size)
                    if h is None:
                        break                          # writer closed; reopen
                    magic, ver, flags, frames, rtp, play_ns, rate, bpf = HEADER.unpack(h)
                    if magic != MAGIC or ver != 1:
                        say("pipe out of step (bad header); resynchronising")
                        while True:                     # search for the next magic
                            b = read_exact(f, 1)
                            if b is None:
                                break
                            h = h[1:] + b
                            if struct.unpack("<I", h[:4])[0] == MAGIC:
                                break
                        continue
                    if frames and (rate != RATE or bpf != BPF):
                        say(f"unexpected format {rate} Hz, {bpf} bytes/frame; stopping")
                        return 1
                    audio = read_exact(f, frames * bpf) if frames else b""
                    if audio is None:
                        break
                    with lock:
                        bridge.record(flags, frames, play_ns, audio)
    except KeyboardInterrupt:
        pass
    finally:
        if bridge.sender:
            bridge.sender.stop()
        sps.terminate()
        try:
            sps.wait(timeout=5)
        except subprocess.TimeoutExpired:
            sps.kill()
        say("bridge stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
