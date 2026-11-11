#!/usr/bin/env python3
"""AirPlay 1 bridge manager: one ap1_bridge.py per AirPlay 1 device that is
switched on in the web panel.

Runs as an s6 service in the AirPlay 2 zones' network namespace (like the
Cast Bridge manager). It:
  - listens for AirPlay 1 (_raop._tcp) devices on the network, leaving out
    devices that also have AirPlay 2 (an _airplay._tcp service at the same
    address) and this add-on's own receivers (our addresses);
  - writes what it found to /config/ap1-bridge/discovered.json, so the
    panel can list them;
  - starts ap1_bridge.py for each device whose key is in the enabled_zones
    option (the panel's AirPlay checkbox), with that device's settings from
    /config/ap1-bridge/settings.json (name, offset, buffer, raop_lead, all
    optional), and stops it when it is switched off;
  - restarts a bridge when its settings or the device's address change, or
    when it exits;
  - writes each bridge's state to /config/ap1-bridge/status.json and its
    output to /config/shairport-sync/logs/<player name>-ap1.log.
"""

import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cast_bridge_manager import (  # noqa: E402
    PORT_MAP_FILE, get_or_assign_port, load_name_port_map, lock_port_map,
    safe_name, save_name_port_map)
from ap1_test import best_v4, local_nets  # noqa: E402

AP1_DIR = Path(os.environ.get("AP1_DIR", "/config/ap1-bridge"))
DISCOVERED_FILE = AP1_DIR / "discovered.json"
SETTINGS_FILE = AP1_DIR / "settings.json"
STATUS_FILE = AP1_DIR / "status.json"
LOG_DIR = Path(os.environ.get("AP1_LOG_DIR", "/config/shairport-sync/logs"))
OPTIONS_FILE = Path(os.environ.get("AP1_OPTIONS_FILE", "/data/options.json"))
OPT_IN_FILE = Path(os.environ.get("AP1_OPT_IN_FILE", "/data/zones_opt_in"))
BRIDGE = "/usr/sbin/ap1_bridge.py"
PORT_BASE = 5150          # RTSP ports come from the shared port map
UDP_BASE = 7700           # 10 UDP ports per bridge from here
DEFAULTS = {"offset": 0, "buffer": 1.0, "raop_lead": 500}
RESTART_DELAY = 10

logging.basicConfig(level=logging.INFO, format="%(asctime)s [ap1-bridge-manager] %(message)s",
                    force=True)   # cast_bridge_manager (imported above) set its own
log = logging.getLogger("ap1_bridge_manager")


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    os.replace(tmp, path)


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def enabled_keys() -> set:
    """AirPlay 1 bridges are opt-in: on only when listed in enabled_zones."""
    if not OPT_IN_FILE.exists():
        return set()
    opts = read_json(OPTIONS_FILE, {})
    return {str(k) for k in (opts.get("enabled_zones") or [])}


def own_addresses() -> set:
    """This add-on's IPv4 addresses: the zones' namespace and the host's."""
    addrs = set()
    for cmd in (["ip", "-o", "-4", "addr"], ["nsenter", "-t", "1", "-n", "ip", "-o", "-4", "addr"]):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.TimeoutExpired):
            continue
        addrs |= set(re.findall(r"inet (\d+\.\d+\.\d+\.\d+)/", out))
    return addrs


class Discovery:
    """Keeps listening for _raop._tcp and _airplay._tcp services. Service
    details are resolved in the main loop, not in zeroconf's callbacks."""

    def __init__(self):
        from zeroconf import ServiceBrowser, Zeroconf
        self.zc = Zeroconf()
        self.lock = threading.Lock()
        self.pending = {"_raop._tcp.local.": set(), "_airplay._tcp.local.": set()}
        self.raop = {}        # service name -> (address, port, txt)
        self.airplay = {}     # service name -> set of addresses
        self.nets, self.nets_at = None, 0.0   # our networks, to pick a device's LAN address
        outer = self

        class L:
            def add_service(self, zc, type_, name):
                with outer.lock:
                    outer.pending[type_].add(name)

            update_service = add_service

            def remove_service(self, zc, type_, name):
                with outer.lock:
                    outer.pending[type_].discard(name)
                    (outer.raop if type_.startswith("_raop") else outer.airplay).pop(name, None)

        for t in self.pending:
            ServiceBrowser(self.zc, t, L())

    def resolve(self):
        with self.lock:
            todo = [(t, n) for t, names in self.pending.items() for n in names]
            for names in self.pending.values():
                names.clear()
        for t, n in todo:
            try:
                info = self.zc.get_service_info(t, n, timeout=1500)
            except Exception:
                info = None
            if not info:
                continue
            addrs = [a for a in info.parsed_addresses() if ":" not in a]
            if t.startswith("_raop"):
                if addrs:
                    if self.nets is None or time.time() - self.nets_at > 300:
                        self.nets, self.nets_at = local_nets(), time.time()
                    txt = {k.decode(): (v.decode() if v else "") for k, v in info.properties.items()}
                    self.raop[n] = (best_v4(addrs, self.nets), info.port, txt)
            else:
                self.airplay[n] = set(addrs)

    def devices(self, own: set) -> dict:
        """key -> {"key", "name", "address", "port"} for AirPlay 1-only devices."""
        ap2_addrs = set().union(*self.airplay.values()) if self.airplay else set()
        out = {}
        for n, (addr, port, txt) in self.raop.items():
            if addr in own or addr in ap2_addrs:
                continue
            name = n.split("@", 1)[-1].split("._raop")[0]
            key = safe_name(name)
            out[key] = {"key": key, "name": name, "address": addr, "port": port,
                        "model": txt.get("am", "")}
        return out


class Bridge:
    def __init__(self, dev: dict, settings: dict, rtsp_port: int, udp_base: int):
        self.dev, self.settings = dev, settings
        self.name = settings.get("name") or dev["name"]
        self.state, self.since = "starting", time.time()
        self.port = rtsp_port
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.log_path = LOG_DIR / f"{safe_name(self.name)}-ap1.log"
        cmd = ["python3", "-u", BRIDGE, dev["address"], "--name", self.name,
               "--port", str(rtsp_port), "--udp-base", str(udp_base),
               "--raop-port", str(dev["port"]),
               "--raop-lead", str(int(settings.get("raop_lead", DEFAULTS["raop_lead"]))),
               "--buffer", str(float(settings.get("buffer", DEFAULTS["buffer"]))),
               "--offset", str(int(settings.get("offset", DEFAULTS["offset"])))]
        log.info("starting bridge '%s' -> %s:%s (%s)", self.name, dev["address"], dev["port"],
                 " ".join(cmd[3:]))
        self.logf = open(self.log_path, "a", buffering=1)
        self.logf.write(f"{time.ctime()} - Starting AirPlay 1 bridge: {self.name} "
                        f"-> {dev['address']}:{dev['port']} on port {rtsp_port}\n")
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, bufsize=1)
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.proc.stdout:
            self.logf.write(line)
            low = line.lower()
            for word, state in (("started in step", "playing"), ("session ended", "idle"),
                                ("bridging to", "ready"), ("connecting to", "connecting"),
                                ("waiting to start", "waiting"), ("idle: closing", "ready")):
                if word in low:
                    self.state, self.since = state, time.time()
                    break

    def alive(self):
        return self.proc.poll() is None

    def stop(self):
        if self.alive():
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.logf.write(f"{time.ctime()} - bridge stopped\n")
        self.logf.close()


def main():
    AP1_DIR.mkdir(parents=True, exist_ok=True)
    disc = Discovery()
    bridges = {}          # key -> Bridge
    udp_slots = {}        # key -> UDP base, for this run
    last_exit = {}        # key -> time a bridge exited on its own
    published = None
    own = own_addresses()
    own_checked = time.time()
    log.info("listening for AirPlay 1 devices (own addresses: %s)", ", ".join(sorted(own)) or "?")
    running = True

    def stop(*_):
        nonlocal running
        running = False
    signal.signal(signal.SIGTERM, stop)

    while running:
        time.sleep(2)
        disc.resolve()
        if time.time() - own_checked > 60:
            own, own_checked = own_addresses(), time.time()
        devices = disc.devices(own)
        if devices != published:
            new = set(devices) - set(published or {})
            if new:
                log.info("AirPlay 1 device(s) found: %s",
                         ", ".join(f"{devices[k]['name']} ({devices[k]['address']})" for k in new))
            write_json(DISCOVERED_FILE, list(devices.values()))
            published = devices
        settings = read_json(SETTINGS_FILE, {})
        wanted = {k for k in enabled_keys() & set(devices)
                  if not settings.get(k, {}).get("hidden")}   # removed in the panel

        for key in list(bridges):
            b = bridges[key]
            s = settings.get(key, {})
            reason = None
            if key not in wanted:
                reason = "switched off" if key in devices else "device gone"
            elif s != b.settings:
                reason = "settings changed"
            elif devices[key]["address"] != b.dev["address"] or devices[key]["port"] != b.dev["port"]:
                reason = "device address changed"
            elif not b.alive():
                log.warning("bridge '%s' exited (%s); restarting in %d s", b.name,
                            b.proc.returncode, RESTART_DELAY)
                last_exit[key] = time.time()
                reason = "exited"
            if reason:
                if reason != "exited":
                    log.info("stopping bridge '%s' (%s)", b.name, reason)
                b.stop()
                del bridges[key]

        for key in sorted(wanted - set(bridges)):
            if time.time() - last_exit.get(key, 0) < RESTART_DELAY:
                continue
            fh = lock_port_map()
            try:
                port_map = load_name_port_map(PORT_MAP_FILE)
                port = get_or_assign_port(f"ap1_{key}", port_map, PORT_BASE)
                save_name_port_map(PORT_MAP_FILE, port_map)
            finally:
                fh.close()
            udp = udp_slots.setdefault(key, UDP_BASE + 10 * len(udp_slots))
            try:
                bridges[key] = Bridge(devices[key], settings.get(key, {}), port, udp)
            except OSError as e:
                log.error("cannot start bridge for %s: %s", key, e)
                last_exit[key] = time.time()

        write_json(STATUS_FILE, {k: {"state": b.state, "since": b.since, "port": b.port,
                                     "pid": b.proc.pid, "name": b.name,
                                     "log": b.log_path.name}
                                 for k, b in bridges.items()})

    for b in bridges.values():
        b.stop()
    write_json(STATUS_FILE, {})


if __name__ == "__main__":
    main()
