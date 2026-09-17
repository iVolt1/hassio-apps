#!/usr/bin/env python3
"""
launcher.py — Cast Bridge addon entrypoint.

Dynamically discovers every Google Cast device on the LAN at addon
startup (via pychromecast's zeroconf-based discovery — no system
avahi-daemon involved) and, for each one found, spins up:

  1. its own dedicated shairport-sync AirPlay 2 "pipe" receiver,
     self-contained in THIS container, built with tinysvcmdns
     (in-process mDNS) instead of avahi, so this addon never runs a
     system avahi-daemon at all — see the Dockerfile for why that
     matters (the sibling AirPlay 2 Multiroom Audio addon's
     avahi-daemon hostname-collision saga).
  2. a cast_bridge_zone.py worker relaying that receiver's pipe audio
     to the discovered Cast device over HTTP + pychromecast's
     standard media LOAD flow (not OwnTone's fragile "Chrome Audio
     Mirroring" fallback).

No manual config needed: point an AirPlay 2 client (or Music
Assistant) at whichever Cast device's name shows up as an AirPlay
receiver, and audio flows through to that Chromecast.

Discovery + zone setup happens once at addon startup, not
continuously at runtime. If you add a new Cast device later, restart
the addon to pick it up. (Turning this into a live rescan-on-interval
loop is a small follow-up if you want it later — the pieces here
are already structured to support recomputing the zone set and
diffing against the running set of processes.)

Each zone's shairport-sync RTSP port and this addon's own HTTP
stream port are persisted to /config/cast-bridge/port_map.txt, keyed
by a sanitized version of the Cast device's friendly name, so both
stay stable across addon restarts — the same reasoning as the
sibling addon's port_map.txt (shairport-sync derives its AirPlay 2
device ID from instance name + port; changing port on every restart
would present as a brand-new AirPlay device to any client that
remembers it).
"""

import json
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pychromecast

log = logging.getLogger("cast_bridge_launcher")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [launcher] %(levelname)s %(message)s")

CONFIG_DIR = Path("/config/cast-bridge")
PORT_MAP_FILE = CONFIG_DIR / "port_map.txt"
PIPE_DIR = Path("/media/music/cast-bridge")
LOG_DIR = CONFIG_DIR / "logs"

AIRPLAY_INTERFACE = os.environ.get("AIRPLAY_INTERFACE", "enp5s0")
DISCOVERY_TIMEOUT_SECONDS = int(os.environ.get("CAST_DISCOVERY_TIMEOUT", "10"))
SHAIRPORT_PORT_BASE = 5200
HTTP_PORT_BASE = 8090
SUPERVISE_INTERVAL_SECONDS = 10


def safe_name(name: str) -> str:
    """Filesystem/port-map-key-safe version of a Cast device's friendly name."""
    return re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_") or "cast"


def load_port_map() -> dict:
    if not PORT_MAP_FILE.exists():
        return {}
    m = {}
    for line in PORT_MAP_FILE.read_text().splitlines():
        parts = line.split()
        if len(parts) == 3:
            m[parts[0]] = (int(parts[1]), int(parts[2]))
    return m


def save_port_map(m: dict):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    PORT_MAP_FILE.write_text("".join(f"{name} {sp} {hp}\n" for name, (sp, hp) in m.items()))


def discover_chromecasts():
    log.info("Discovering Cast devices (up to %ds)...", DISCOVERY_TIMEOUT_SECONDS)
    casts, browser = pychromecast.get_chromecasts(timeout=DISCOVERY_TIMEOUT_SECONDS)
    pychromecast.discovery.stop_discovery(browser)
    if casts:
        log.info("Found %d Cast device(s): %s", len(casts),
                  ", ".join(c.cast_info.friendly_name for c in casts))
    else:
        log.warning("No Cast devices found on this discovery pass.")
    return casts


def assign_ports(cast_names: list, port_map: dict) -> dict:
    """Ensures every name in cast_names has a persisted
    (shairport_port, http_port) pair, reusing any existing assignment
    so both the AirPlay 2 device ID and the bridge's stream URL stay
    stable across restarts. Stale entries for devices no longer seen
    are left in place harmlessly (small file, avoids needlessly
    reassigning a device's ports if it's just temporarily off the
    network)."""
    used_sp = {p[0] for p in port_map.values()}
    used_hp = {p[1] for p in port_map.values()}
    next_sp, next_hp = SHAIRPORT_PORT_BASE, HTTP_PORT_BASE
    for name in cast_names:
        if name in port_map:
            continue
        while next_sp in used_sp:
            next_sp += 1
        while next_hp in used_hp:
            next_hp += 1
        port_map[name] = (next_sp, next_hp)
        used_sp.add(next_sp)
        used_hp.add(next_hp)
    return port_map


# Matches shairport-sync's own documented AirPlay 2 defaults (per its
# reference config: "Default is 48000 for AirPlay 2" / "Default is
# S32_LE for AirPlay 2") rather than an earlier guess of 44100/24-bit
# based on the source files' own rate. That guess had it backwards:
# forcing 44100 would fight AP2's native buffered-audio decode rate
# and likely introduce a resample that wouldn't otherwise happen,
# instead of avoiding one. S32_LE is also unambiguous (exactly 4
# bytes/sample, no packing variant to get wrong) — safer than the
# S24_LE vs. S24_3LE distinction the previous 24-bit attempt risked.
PIPE_SAMPLE_RATE = 48000
PIPE_OUTPUT_FORMAT = "S32_LE"
FFMPEG_INPUT_FORMAT = "s32le"


def start_shairport(name: str, fifo: str, port: int, sample_rate: int = PIPE_SAMPLE_RATE) -> subprocess.Popen:
    PIPE_DIR.mkdir(parents=True, exist_ok=True)
    if not os.path.exists(fifo):
        os.mkfifo(fifo)

    config_file = CONFIG_DIR / f"{name}-ap2.conf"
    log_file = LOG_DIR / f"{name}-ap2.log"
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    config_file.write_text(f"""\
general :
{{
  name = "{name}";
  port = {port};
  interface = "{AIRPLAY_INTERFACE}";
  output_backend = "pipe";
  mdns_backend = "tinysvcmdns";
  // Tightened from shairport-sync's defaults (0.35s / 1.0s) to cut
  // volume-change response latency through this pipe->ffmpeg->Cast
  // chain: audio already sitting in these buffers when the volume
  // changes plays out at the OLD level until it drains, so the
  // whole chain only feels as responsive as its slowest buffer.
  // audio_decoded_buffer_desired_length_in_seconds matters most —
  // it's the AirPlay 2 "Buffered Audio" mode decode buffer, which
  // is what most AP2 clients (including MA's) actually negotiate.
  audio_backend_buffer_desired_length_in_seconds = 0.1;
  audio_decoded_buffer_desired_length_in_seconds = 0.35;
  audio_backend_buffer_interpolation_threshold_in_seconds = 0.05;
}};
sessioncontrol :
{{
  allow_session_interruption = "yes";
}};
// Diagnostic settings. These are for diagnostic and debugging only.
// Normally you should leave them commented out.
diagnostics =
{{
//	log_show_time_since_startup = "yes"; // set this to yes if you want the time since startup in the debug message -- seconds down to nanoseconds
//	log_show_time_since_last_message = "no"; // set this to yes if you want the time since the last debug message in the debug message -- seconds down to nanoseconds
}};
pipe :
{{
  name = "{fifo}";
  output_rate = {sample_rate};
  output_format = "{PIPE_OUTPUT_FORMAT}";
}};
""")

    log.info("Starting shairport-sync for '%s' on port %d -> %s", name, port, fifo)
    log_fh = open(log_file, "ab")
    return subprocess.Popen(
        ["shairport-sync", "-a", name, "-p", str(port), "-c", str(config_file), "-vv"],
        stdout=log_fh, stderr=subprocess.STDOUT,
    )


def start_bridge_worker(zone: dict) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "/opt/cast-bridge/cast_bridge_zone.py", json.dumps(zone)]
    )


def main():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    PIPE_DIR.mkdir(parents=True, exist_ok=True)

    # Deliberately does NOT start its own nqptp. nqptp binds a fixed,
    # host-wide OS port (319, the PTP event port) and is meant to run
    # as a single instance per host, shared by every AirPlay 2
    # (shairport-sync --with-airplay-2) receiver on that host — not
    # one per container. If the AirPlay 2 Multiroom Audio addon (or
    # any other shairport-sync-with-AP2 addon) is running on this
    # host, its nqptp is already up and this addon's shairport-sync
    # instances use that one, since host_network:true means every
    # host_network container shares the same network namespace/ports.
    # Trying to start a second nqptp here just fails immediately
    # ("Address in use") and loops forever pointlessly.
    #
    # If NO other AirPlay 2 addon is running on this host, there is
    # currently no nqptp at all and these AirPlay 2 receivers' PTP
    # timing will not work — that's a real gap to revisit (e.g.
    # detecting the missing nqptp and starting exactly one, addon
    # coordination aside) if you ever run this addon standalone.

    casts = discover_chromecasts()

    port_map = load_port_map()
    names = [safe_name(c.cast_info.friendly_name) for c in casts]
    port_map = assign_ports(names, port_map)
    save_port_map(port_map)

    zones = {}  # safe_name -> {"shairport": Popen, "bridge": Popen, "zone": dict, "sp_port": int}

    for cast in casts:
        friendly = cast.cast_info.friendly_name
        name = safe_name(friendly)
        sp_port, http_port = port_map[name]
        fifo = str(PIPE_DIR / f"{name}.pipe")

        sp_proc = start_shairport(name, fifo, sp_port)

        zone = {
            "name": friendly,
            "fifo": fifo,
            "chromecast_name": friendly,
            "chromecast_host": cast.cast_info.host,
            "chromecast_port": cast.cast_info.port,
            "chromecast_uuid": str(cast.cast_info.uuid),
            "chromecast_model_name": cast.cast_info.model_name,
            "http_port": http_port,
            "sample_rate": PIPE_SAMPLE_RATE,
            "sample_format": FFMPEG_INPUT_FORMAT,
            "channels": 2,
        }
        bridge_proc = start_bridge_worker(zone)

        zones[name] = {"shairport": sp_proc, "bridge": bridge_proc, "zone": zone, "sp_port": sp_port}

    if not zones:
        log.warning("No Cast devices to bridge. Idling — restart the addon after your "
                     "Cast devices are reachable on the network to pick them up.")

    log.info("All zones started (%d). Supervising.", len(zones))

    try:
        while True:
            time.sleep(SUPERVISE_INTERVAL_SECONDS)
            for name, entry in zones.items():
                if entry["shairport"].poll() is not None:
                    log.warning("shairport-sync for '%s' died, restarting", name)
                    entry["shairport"] = start_shairport(name, entry["zone"]["fifo"], entry["sp_port"])
                if entry["bridge"].poll() is not None:
                    log.warning("bridge worker for '%s' died, restarting", name)
                    entry["bridge"] = start_bridge_worker(entry["zone"])
    except KeyboardInterrupt:
        pass
    finally:
        for entry in zones.values():
            entry["shairport"].terminate()
            entry["bridge"].terminate()


if __name__ == "__main__":
    main()
