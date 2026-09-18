#!/usr/bin/env python3
"""
cast_bridge_manager.py — runs as a service INSIDE the AirPlay 2
Multiroom Audio addon's own container (see generate_airplay2_services.sh,
which creates the "airplay2-castbridge-manager" s6 service that execs
this script, depending on airplay2-dbus/airplay2-avahi/airplay2-nqptp).

This replaces what used to be a second, separate "Cast Bridge" addon.
That split existed because Cast device discovery/relay (pychromecast +
ffmpeg) has nothing to do with AirPlay 2/nqptp/avahi, so it seemed
natural to keep it in its own container — but nqptp's IPC to a
shairport-sync client is a POSIX shared-memory segment
(/nqptp-<name>), which is container-local even under host_network:true
(that only shares the network namespace, not /dev/shm). A
shairport-sync instance in a *different* container could never reach
this container's nqptp, so the separate Cast Bridge addon's own
AirPlay 2 receivers silently fell back to classic AirPlay 1 — its log
showed "NQPTP service not found." Splitting the work back out into a
file-based handoff between two containers (one creating AirPlay 2
receivers, the other doing Cast discovery/relay) fixed AirPlay 2 but
turned out to be its own headache: the two addons declared different
map types ("config:rw" vs "addon_config:rw"), which meant their
/config mounts were backed by two different host directories, so the
hand-off file silently never arrived where the other side looked for
it.

Running everything in one process, in one container, removes all of
that: this script discovers Cast devices, creates their AirPlay 2 pipe
receivers directly (shairport-sync subprocesses, using this
container's own already-working avahi + nqptp), and spawns the
ffmpeg/HTTP/Cast relay worker for each — no handoff file, no second
addon, no path-mismatch risk.

Each zone's shairport-sync RTSP port is persisted in the SAME
port_map.txt that generate_airplay2_services.sh's PulseAudio-sink
zones use (${CONFIG_DIR}/port_map.txt, "name port" per line) — reading
and writing that shared file here, in the same plain-text format the
shell script uses, means neither generator can ever hand out a port
the other one already owns. This script's own separate HTTP stream
ports are tracked in a small file of their own
(${CASTBRIDGE_DIR}/http_port_map.txt), since nothing else needs those.

Discovery happens once at this service's own startup (i.e. once per
addon restart), same as before. Add a new Cast device -> restart this
addon to pick it up.
"""

import json
import logging
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

import pychromecast

log = logging.getLogger("cast_bridge_manager")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [castbridge-manager] %(levelname)s %(message)s")

# Shared with generate_airplay2_services.sh's sink zones — same
# directory, same port_map.txt format, so RTSP ports never collide.
CONFIG_DIR = Path("/config/shairport-sync/config")
LOG_DIR = Path("/config/shairport-sync/logs")
PORT_MAP_FILE = CONFIG_DIR / "port_map.txt"

# This script's own state, not shared with the shell generator.
CASTBRIDGE_DIR = Path("/config/cast-bridge")
HTTP_PORT_MAP_FILE = CASTBRIDGE_DIR / "http_port_map.txt"

# Shared "media:rw" mount — same host directory the old separate Cast
# Bridge addon used, kept as-is since nothing about that path was
# actually part of the config-mismatch problem.
PIPE_DIR = Path("/media/music/castbridge")

AIRPLAY_INTERFACE = os.environ.get("AIRPLAY_INTERFACE", "enp5s0")
DISCOVERY_TIMEOUT_SECONDS = int(os.environ.get("CAST_DISCOVERY_TIMEOUT", "10"))

# Separate range from the sink zones' 5120+, so the two generators
# don't start out racing for the same starting port. Real safety still
# comes from is_port_available()/port_map.txt regardless.
SHAIRPORT_PORT_BASE = 5150
UDP_PORT_BASE = 6800
HTTP_PORT_BASE = 8090
SUPERVISE_INTERVAL_SECONDS = 10

# shairport-sync's own documented AirPlay 2 defaults (per its
# reference config: "Default is 48000 for AirPlay 2" / "Default is
# S32_LE for AirPlay 2").
PIPE_SAMPLE_RATE = 48000
PIPE_OUTPUT_FORMAT = "S32_LE"
FFMPEG_INPUT_FORMAT = "s32le"


def safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_") or "cast"


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


# -- shared port_map.txt (bash-format "name port" lines) --------------
def load_name_port_map(path: Path) -> dict:
    if not path.exists():
        return {}
    m = {}
    for line in path.read_text().splitlines():
        parts = line.split()
        if len(parts) == 2:
            m[parts[0]] = int(parts[1])
    return m


def save_name_port_map(path: Path, m: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{name} {port}\n" for name, port in m.items()))


def get_or_assign_port(name: str, port_map: dict, base: int) -> int:
    """Reuses name's persisted port if still free; otherwise finds the
    next free port at/after base, persists it, and returns it. Mutates
    port_map in place."""
    existing = port_map.get(name)
    if existing is not None and is_port_available(existing):
        return existing
    port = base
    used = set(port_map.values())
    while port in used or not is_port_available(port):
        port += 1
    port_map[name] = port
    return port


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


def start_shairport(name: str, fifo: str, port: int, udp_port_base: int) -> subprocess.Popen:
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
  udp_port_base = {udp_port_base};
  mdns_backend = "avahi";
  audio_backend_buffer_desired_length_in_seconds = 0.1;
  audio_decoded_buffer_desired_length_in_seconds = 0.35;
  audio_backend_buffer_interpolation_threshold_in_seconds = 0.05;
}};
sessioncontrol :
{{
  allow_session_interruption = "yes";
}};
diagnostics :
{{
//  log_show_time_since_startup = "yes";
//  log_show_time_since_last_message = "no";
}};
pipe :
{{
  name = "{fifo}";
  output_rate = {PIPE_SAMPLE_RATE};
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
    LOG_DIR_CB = CASTBRIDGE_DIR / "logs"
    LOG_DIR_CB.mkdir(parents=True, exist_ok=True)
    log_fh = open(LOG_DIR_CB / f"{safe_name(zone['name'])}-bridge.log", "ab")
    return subprocess.Popen(
        [sys.executable, "/usr/sbin/cast_bridge_zone.py", json.dumps(zone)],
        stdout=log_fh, stderr=subprocess.STDOUT,
    )


def main():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CASTBRIDGE_DIR.mkdir(parents=True, exist_ok=True)
    PIPE_DIR.mkdir(parents=True, exist_ok=True)

    casts = discover_chromecasts()

    port_map = load_name_port_map(PORT_MAP_FILE)
    http_port_map = load_name_port_map(HTTP_PORT_MAP_FILE)

    zones = {}  # safe_name -> {"shairport": Popen, "bridge": Popen, "zone": dict, "sp_port": int, "udp_base": int}
    next_udp_base = UDP_PORT_BASE

    for cast in casts:
        friendly = cast.cast_info.friendly_name
        name = safe_name(friendly)

        sp_port = get_or_assign_port(name, port_map, SHAIRPORT_PORT_BASE)
        http_port = get_or_assign_port(name, http_port_map, HTTP_PORT_BASE)
        fifo = str(PIPE_DIR / f"{name}.pipe")

        # Each zone needs its own UDP port range (shairport-sync's AP2
        # timing/control/data channels) — reusing one across multiple
        # simultaneously-running receivers would conflict, the same
        # reason generate_airplay2_services.sh advances its own
        # UDP_PORT_BASE by 10 per sink zone.
        udp_base = next_udp_base
        next_udp_base += 10

        sp_proc = start_shairport(name, fifo, sp_port, udp_base)

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

        zones[name] = {"shairport": sp_proc, "bridge": bridge_proc, "zone": zone,
                        "sp_port": sp_port, "udp_base": udp_base}

    save_name_port_map(PORT_MAP_FILE, port_map)
    save_name_port_map(HTTP_PORT_MAP_FILE, http_port_map)

    if not zones:
        log.warning("No Cast devices to bridge. Idling — restart this addon after your "
                     "Cast devices are reachable on the network to pick them up.")

    log.info("All zones started (%d). Supervising.", len(zones))

    try:
        while True:
            time.sleep(SUPERVISE_INTERVAL_SECONDS)
            for name, entry in zones.items():
                if entry["shairport"].poll() is not None:
                    log.warning("shairport-sync for '%s' died, restarting", name)
                    entry["shairport"] = start_shairport(
                        name, entry["zone"]["fifo"], entry["sp_port"], entry["udp_base"])
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
