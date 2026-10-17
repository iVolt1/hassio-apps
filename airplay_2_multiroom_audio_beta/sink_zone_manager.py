#!/usr/bin/env python3
"""
sink_zone_manager.py — one s6 service that owns every PulseAudio-sink
("remap sink") AirPlay 2 zone, the same way cast_bridge_manager.py owns
the Cast-backed ones.

Why this exists
---------------
generate_airplay2_services.sh used to create one s6 service directory
per remap sink. s6-rc compiles its service database once, at container
start (S6_STAGE2_HOOK), so a remap sink that appeared or disappeared
later -- a Bluetooth speaker reconnecting, a USB DAC being replugged, the
Multi-room Audio Controller recreating a sink -- could only be picked up
by restarting the whole addon. This process spawns and supervises the
shairport-sync instances itself, so zones can come and go at runtime.

What it does
------------
  * At startup, starts one shairport-sync per module-remap-sink, using
    exactly the config, port assignment, log file and command line the
    shell generator used to produce.
  * Watches `pactl subscribe` for sink new/remove events (debounced, with
    a periodic poll as a backstop) and reconciles the running zones with
    the sinks that exist:
      - sink disappeared -> stop that zone's shairport-sync (the zone
        leaves the AirPlay picker; its port stays reserved)
      - sink (re)appeared -> start the zone again on the SAME port and
        name, so its AirPlay 2 device ID, and therefore its Music
        Assistant player and settings, stay the same
  * Restarts a shairport-sync that exits unexpectedly, with backoff.

Live updates are OFF until the flag file below exists, so a new build can
first be run as a dry run: the log says what the manager WOULD do when the
sink set changes, without touching any zone.

    <CONFIG_DIR>/sink_zone_live      (create the empty file to enable)

What stays in generate_airplay2_services.sh: the one-time dbus / avahi /
nqptp / cast-bridge services and, before any of this starts, the master
sink bounce + remap sink recreate + duplicate cleanup. The stage-2 hook
runs before any service starts, so remap sinks are already in their
fresh state when this process first lists them.

Port assignments live in the same port_map.txt cast_bridge_manager.py
uses ("name port" per line). Both processes now start concurrently, so
every read-modify-write of that file happens under one shared flock.
"""

import fcntl
import logging
import os
import re
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path

log = logging.getLogger("sink_zone_manager")

CONFIG_DIR = Path(os.environ.get("SZM_CONFIG_DIR", "/config/shairport-sync/config"))
LOG_DIR = Path(os.environ.get("SZM_LOG_DIR", "/config/shairport-sync/logs"))
PORT_MAP_FILE = CONFIG_DIR / "port_map.txt"
PORT_MAP_LOCK_FILE = CONFIG_DIR / ".port_map.lock"
LIVE_FLAG_FILE = CONFIG_DIR / "sink_zone_live"

AIRPLAY_INTERFACE = os.environ.get("AIRPLAY_INTERFACE", "enp5s0")

# Same starting points the shell generator used. Cast zones use 5150+ /
# 6800+, so the two ranges only meet if there are a lot of zones, and
# port_map.txt plus is_port_available() catch that case anyway.
RTSP_PORT_BASE = 5120
UDP_PORT_BASE = 7011
UDP_PORT_STEP = 10  # shairport-sync's default udp_port_range is 10

SUPERVISE_INTERVAL_SECONDS = 5
POLL_INTERVAL_SECONDS = 30
EVENT_DEBOUNCE_SECONDS = 2.0
STOP_GRACE_SECONDS = 4.0
HEALTHY_AFTER_SECONDS = 60

DUPLICATE_SUFFIX_RE = re.compile(r"\.\d+$")


# -- shared port_map.txt ------------------------------------------------
class port_map_lock:
    """Exclusive flock shared with cast_bridge_manager.py."""

    def __enter__(self):
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        self._fh = open(PORT_MAP_LOCK_FILE, "w")
        fcntl.flock(self._fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self._fh, fcntl.LOCK_UN)
        self._fh.close()


def load_port_map() -> dict:
    m = {}
    if PORT_MAP_FILE.exists():
        for line in PORT_MAP_FILE.read_text().splitlines():
            parts = line.split()
            if len(parts) == 2:
                try:
                    m[parts[0]] = int(parts[1])
                except ValueError:
                    pass
    return m


def save_port_map(m: dict) -> None:
    tmp = PORT_MAP_FILE.with_name(PORT_MAP_FILE.name + ".tmp")
    tmp.write_text("".join(f"{name} {port}\n" for name, port in m.items()))
    os.replace(tmp, PORT_MAP_FILE)


def is_port_available(port: int) -> bool:
    for socktype in (socket.SOCK_STREAM, socket.SOCK_DGRAM):
        s = socket.socket(socket.AF_INET, socktype)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("0.0.0.0", port))
        except OSError:
            return False
        finally:
            s.close()
    return True


def assign_rtsp_port(name: str) -> int:
    """Returns name's persisted RTSP port if it is still free and not
    claimed by a different name in port_map.txt; otherwise picks the next
    free, unclaimed port, persists it, and returns it.

    shairport-sync derives a zone's AirPlay 2 device ID from its name AND
    its RTSP port, so keeping the port stable keeps the device ID stable
    (Music Assistant would otherwise see a brand-new player with default
    settings). The "claimed by a different name" check reads the map
    itself, not just the OS's bind state, because at startup every
    shairport-sync is down and every port looks free.
    """
    with port_map_lock():
        port_map = load_port_map()
        others = {k: v for k, v in port_map.items() if k != name}
        existing = port_map.get(name)
        if existing is not None:
            if existing in others.values():
                log.warning("Persisted port %d for '%s' is also claimed by another "
                            "zone in port_map.txt, reassigning", existing, name)
            elif is_port_available(existing):
                log.info("Reusing persisted port %d for '%s'", existing, name)
                return existing
            else:
                log.warning("Persisted port %d for '%s' is no longer free, reassigning",
                            existing, name)
        port = RTSP_PORT_BASE
        used = set(others.values())
        while port in used or not is_port_available(port):
            port += 1
        port_map[name] = port
        save_port_map(port_map)
        log.info("Assigned new port %d for '%s'", port, name)
        return port


# -- PulseAudio ---------------------------------------------------------
def list_remap_sinks():
    """Names of the module-remap-sink sinks that should have a zone, in
    pactl's order, or None if pactl could not be queried (callers must
    NOT treat None as "no sinks", or a PulseAudio hiccup would tear every
    zone down)."""
    try:
        res = subprocess.run(["pactl", "list", "sinks", "short"],
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("pactl list sinks failed: %s", e)
        return None
    if res.returncode != 0:
        log.warning("pactl list sinks exited %d: %s", res.returncode, res.stderr.strip())
        return None

    names = []
    for line in res.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 3 and "module-remap-sink" in parts[2]:
            names.append(parts[1])
    return names


def bounce_and_recreate_remaps(wanted):
    """After a PulseAudio restart a master sink can look fine (IDLE) while its
    ALSA device never reopens, so nothing is audible. Same fix as the addon's
    boot-time step: suspend/resume each master sink once, then reload each
    remap module with its original arguments. `wanted` is the list of zone
    sink names; other remap sinks are left alone."""
    try:
        res = subprocess.run(["pactl", "list", "modules"], capture_output=True,
                             text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("pactl list modules failed: %s", e)
        return
    if res.returncode != 0:
        log.warning("pactl list modules exited %d: %s", res.returncode, res.stderr.strip())
        return

    mods, idx, is_remap = [], None, False
    for line in res.stdout.splitlines():
        m = re.match(r"Module #(\d+)", line)
        if m:
            idx, is_remap = m.group(1), False
        elif re.match(r"\s*Name:\s*module-remap-sink\s*$", line):
            is_remap = True
        elif is_remap and re.match(r"\s*Argument:", line):
            mods.append((idx, line.split("Argument:", 1)[1].strip()))
            is_remap = False

    def run(*cmd):
        r = subprocess.run(["pactl", *cmd], capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            log.warning("pactl %s failed: %s", " ".join(cmd[:2]), r.stderr.strip())
        return r.returncode == 0

    bounced = set()
    for idx, args in mods:
        sink = re.search(r"sink_name=(\S+)", args)
        master = re.search(r"master=(\S+)", args)
        if not sink or sink.group(1) not in wanted:
            continue
        try:
            if master and master.group(1) not in bounced:
                bounced.add(master.group(1))
                log.info("Bouncing master sink %s (underlies %s)", master.group(1), sink.group(1))
                run("suspend-sink", master.group(1), "1")
                time.sleep(0.5)
                run("suspend-sink", master.group(1), "0")
            log.info("Recreating remap sink %s (module #%s)", sink.group(1), idx)
            run("unload-module", idx)
            time.sleep(0.2)
            run("load-module", "module-remap-sink", args)
        except (OSError, subprocess.TimeoutExpired) as e:
            log.warning("Bounce/recreate of %s failed: %s", sink.group(1), e)


_warned_skips = set()


def zone_sinks(names):
    """Filters remap sink names down to the ones that get a zone."""
    keep = []
    for n in names:
        if n.startswith("sendspin_"):
            continue
        if DUPLICATE_SUFFIX_RE.search(n) and DUPLICATE_SUFFIX_RE.sub("", n) in names:
            if n not in _warned_skips:
                _warned_skips.add(n)
                log.warning("Ignoring '%s': looks like an auto-suffixed duplicate of '%s' "
                            "(restart the addon to have the duplicate cleaned up)",
                            n, DUPLICATE_SUFFIX_RE.sub("", n))
            continue
        keep.append(n)
    return keep


# -- one zone -----------------------------------------------------------
class Zone:
    def __init__(self, sink: str, rtsp_port: int, udp_base: int):
        self.sink = sink
        self.display_name = sink.replace("_", " ")
        self.rtsp_port = rtsp_port
        self.udp_base = udp_base
        self.config_file = CONFIG_DIR / f"{sink}-ap2.conf"
        self.custom_file = CONFIG_DIR / f"{sink}-ap2.custom"
        self.log_file = LOG_DIR / f"{sink}-ap2.log"
        self.proc = None
        self.started_at = 0.0
        self.failures = 0
        self.dead_noted = False
        self.next_restart_at = 0.0

    def _ensure_custom_file(self):
        # Per-zone user overrides, spliced into the general block via
        # libconfig's @include. Never overwritten once it exists. It must
        # always exist: a missing @include target stops shairport-sync
        # from parsing its config.
        if not self.custom_file.exists():
            self.custom_file.write_text(
                "// Per-zone shairport-sync overrides, included inside general = { ... }\n"
                "// Example:\n"
                "// audio_backend_latency_offset_in_seconds = -0.15;\n")
            log.info("Created %s", self.custom_file)

    def _write_config(self):
        self.config_file.write_text(f"""\
general :
{{
  name = "{self.display_name}";
  port = {self.rtsp_port};
  interface = "{AIRPLAY_INTERFACE}";
  output_backend = "pulseaudio";
  udp_port_base = {self.udp_base};
  mdns_backend = "avahi";
  @include "{self.custom_file}"
}};
sessioncontrol :
{{
  allow_session_interruption = "yes";
}};
diagnostics :
{{
  log_show_time_since_startup = "yes";
  log_show_time_since_last_message = "no";
}};
pulseaudio :
{{
  sink = "{self.sink}";
  application_name = "Shairport Sync";
}};
""")

    def start(self, fresh: bool = True):
        """fresh=True truncates the zone log (a new session of the zone);
        fresh=False appends, so the output of a crash is not wiped by the
        restart that follows it."""
        self._ensure_custom_file()
        self._write_config()
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        if fresh:
            open(self.log_file, "wb").close()
        with open(self.log_file, "ab") as fh:
            fh.write(f"{time.strftime('%a %b %d %H:%M:%S %Z %Y')} - Starting AirPlay 2 receiver: "
                     f"{self.display_name} for {self.sink} on port {self.rtsp_port}\n".encode())
            fh.flush()
            self.proc = subprocess.Popen(
                ["shairport-sync", "-a", self.display_name, "-p", str(self.rtsp_port),
                 "-c", str(self.config_file), "-vv"],
                stdout=fh, stderr=subprocess.STDOUT)
        self.started_at = time.time()
        self.dead_noted = False
        log.info("Started zone '%s' (sink %s, port %d, udp %d, pid %d)",
                 self.display_name, self.sink, self.rtsp_port, self.udp_base, self.proc.pid)

    def terminate(self):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()

    def wait_or_kill(self, deadline: float):
        p = self.proc
        if p is None:
            return
        try:
            p.wait(timeout=max(0.1, deadline - time.time()))
        except subprocess.TimeoutExpired:
            log.warning("shairport-sync for '%s' did not exit, killing", self.display_name)
            p.kill()
            p.wait()
        self.proc = None

    def stop(self):
        self.terminate()
        self.wait_or_kill(time.time() + STOP_GRACE_SECONDS)

    def supervise(self, now: float):
        p = self.proc
        if p is None:
            return
        if p.poll() is None:
            if now - self.started_at > HEALTHY_AFTER_SECONDS:
                self.failures = 0
            return
        if not self.dead_noted:
            self.dead_noted = True
            self.failures += 1
            delay = 0 if self.failures == 1 else min(60, 5 * 2 ** (self.failures - 2))
            self.next_restart_at = now + delay
            log.warning("shairport-sync for '%s' exited with code %s, restarting in %ds "
                        "(failure %d)", self.display_name, p.returncode, delay, self.failures)
        if now >= self.next_restart_at:
            self.start(fresh=False)


# -- the manager --------------------------------------------------------
class Manager:
    def __init__(self):
        self.zones = {}        # sink name -> Zone (running)
        self.udp_bases = {}    # sink name -> udp base, kept across stop/start
        self.booted = False
        self.last_diff_logged = None
        self.stop_event = threading.Event()
        self.change_event = threading.Event()
        self.last_sink_event = 0.0
        self.pactl_subscribe = None
        self.pulse_restarted = False   # set when the PulseAudio connection dropped
        self.recover = set()           # zones to restart when their sink returns

    def _udp_base_for(self, sink: str) -> int:
        if sink not in self.udp_bases:
            used = set(self.udp_bases.values())
            base = UDP_PORT_BASE
            while base in used:
                base += UDP_PORT_STEP
            self.udp_bases[sink] = base
        return self.udp_bases[sink]

    def _start_zone(self, sink: str):
        if re.search(r"\s", sink):
            log.warning("Skipping sink '%s': whitespace in the name breaks port_map.txt", sink)
            return
        zone = Zone(sink, assign_rtsp_port(sink), self._udp_base_for(sink))
        zone.start(fresh=True)
        self.zones[sink] = zone

    def reconcile(self):
        names = list_remap_sinks()
        if names is None:
            return
        desired = zone_sinks(names)
        add = [s for s in desired if s not in self.zones]
        remove = [s for s in self.zones if s not in desired]

        if self.pulse_restarted:
            self.pulse_restarted = False
            if self.booted:
                # Running shairport-sync processes still hold connections to the
                # old PulseAudio server and make no sound. Restart them all;
                # this is recovery, so it does not depend on the live flag.
                log.warning("PulseAudio was restarted: restarting %d zone(s)", len(self.zones))
                self.recover |= set(self.zones)
                for s in list(self.zones):
                    self.zones.pop(s).stop()
                self.stop_event.wait(2)   # let PulseAudio finish loading its cards
                bounce_and_recreate_remaps(desired)
                names = list_remap_sinks() or names
                desired = zone_sinks(names)
                add = [s for s in desired if s not in self.zones]
                remove = []
        if self.recover:
            back = [s for s in add if s in self.recover]
            for s in back:
                log.info("Restarting zone '%s' after the PulseAudio restart", s)
                self._start_zone(s)
                add.remove(s)
            self.recover -= set(back)

        if not self.booted:
            self.booted = True
            if not desired:
                log.warning("No remap sinks found at startup. Watching for new ones.")
            for s in add:
                self._start_zone(s)
            log.info("Started %d zone(s). Supervising%s.", len(self.zones),
                     "" if LIVE_FLAG_FILE.exists() else
                     f" (live sink updates are OFF: create {LIVE_FLAG_FILE} to enable)")
            return

        if not add and not remove:
            self.last_diff_logged = None
            return

        diff = (tuple(add), tuple(remove))
        if not LIVE_FLAG_FILE.exists():
            if diff != self.last_diff_logged:
                self.last_diff_logged = diff
                log.info("DRY RUN: sink set changed, would start %s and stop %s. "
                         "Create %s to apply sink changes live.", add or "nothing",
                         remove or "nothing", LIVE_FLAG_FILE)
            return

        self.last_diff_logged = None
        log.info("Sink set changed: starting %s, stopping %s", add or "nothing", remove or "nothing")
        for s in remove:
            self.zones.pop(s).stop()
            log.info("Stopped zone for '%s' (sink is gone; its port stays reserved)", s)
        for s in add:
            self._start_zone(s)

    def supervise(self):
        now = time.time()
        for zone in self.zones.values():
            zone.supervise(now)

    # -- pactl subscribe watcher ----------------------------------------
    def _watch_events(self):
        while not self.stop_event.is_set():
            try:
                self.pactl_subscribe = subprocess.Popen(
                    ["pactl", "subscribe"], stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, text=True)
            except OSError as e:
                log.warning("Could not start 'pactl subscribe': %s", e)
                self.stop_event.wait(5)
                continue
            for line in self.pactl_subscribe.stdout:
                if "on sink #" in line and ("'new'" in line or "'remove'" in line):
                    self.last_sink_event = time.time()
                    self.change_event.set()
            self.pactl_subscribe.wait()
            if not self.stop_event.is_set():
                log.warning("'pactl subscribe' exited, restarting it in 2s")
                self.pulse_restarted = True
                self.stop_event.wait(2)
                # Events may have been missed meanwhile.
                self.last_sink_event = time.time()
                self.change_event.set()

    def shutdown(self):
        log.info("Shutting down, stopping %d zone(s)", len(self.zones))
        if self.pactl_subscribe is not None and self.pactl_subscribe.poll() is None:
            self.pactl_subscribe.terminate()
        for z in self.zones.values():
            z.terminate()
        deadline = time.time() + STOP_GRACE_SECONDS
        for z in self.zones.values():
            z.wait_or_kill(deadline)

    def run(self):
        threading.Thread(target=self._watch_events, daemon=True, name="pactl-subscribe").start()
        last_reconcile = last_supervise = 0.0
        self.reconcile()
        last_reconcile = time.time()
        try:
            while not self.stop_event.is_set():
                now = time.time()
                if self.change_event.is_set() and now - self.last_sink_event >= EVENT_DEBOUNCE_SECONDS:
                    self.change_event.clear()
                    self.reconcile()
                    last_reconcile = time.time()
                elif now - last_reconcile >= (POLL_INTERVAL_SECONDS if self.booted
                                              else SUPERVISE_INTERVAL_SECONDS):
                    # Backstop poll; before the first successful pactl
                    # query (PulseAudio not up yet) it retries faster.
                    self.reconcile()
                    last_reconcile = time.time()
                if now - last_supervise >= SUPERVISE_INTERVAL_SECONDS:
                    self.supervise()
                    last_supervise = time.time()
                self.stop_event.wait(0.5)
        finally:
            self.shutdown()


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [sink-zones] %(levelname)s %(message)s")
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    mgr = Manager()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: mgr.stop_event.set())

    log.info("sink_zone_manager starting (interface %s)", AIRPLAY_INTERFACE)
    mgr.run()


if __name__ == "__main__":
    main()
