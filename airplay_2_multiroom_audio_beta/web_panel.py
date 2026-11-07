#!/usr/bin/env python3
"""
web_panel.py -- the add-on's web panel, shown in Home Assistant's sidebar
through Ingress: the running AirPlay 2 zones, sound cards, remap sinks
(with their system names) and whether each zone is running. Managed sinks can be renamed, new ones
created on a free pair, and unmanaged ones adopted. Sound-card and
Bluetooth zones can have their AirPlay name, latency offset and max volume
set; that restarts only that zone.

  GET  /            the page
  GET  /api/state   the same data as JSON
  GET  /api/debug/cards   raw `pactl list cards` output
  POST /api/apply   {"names": {"<option index>": "<new name>"}}: saves the
                    remap_sinks option through the Supervisor (so the
                    Configuration tab matches), then runs remap_sinks.py --load
                    and returns its log lines
  POST /api/create  {"master": ..., "channels": ...}: adds a sink on a free
                    pair, named with its system name, then the same as apply
  POST /api/adopt   {"name": ...}: adds an unmanaged sink to the options
                    under its current name, then the same as apply
  POST /api/profile {"card", "profile"}: sets a card's profile now, saves it
                    in card_profiles, re-points that card's remap_sinks
                    entries to its new sink where their pair still exists,
                    then the same as apply
  POST /api/zone/enabled {"key", "enabled"}: switches a sound-card or
                    Bluetooth zone off or on (disabled_zones option); the
                    zone manager stops or starts it within a few seconds
  POST /api/create_defaults {"master": ...}: adds one sink per free channel
                    pair of a card output, under their system names, then
                    the same as apply
  POST /api/combine/create {"name", "description", "sinks": [...]}: adds a
                    combine sink (two or more member sinks of any kind)
  POST /api/combine/remove {"index"}: removes a combine_sinks entry
  POST /api/combine/adopt  {"name"}: adds an unmanaged combine sink as is
                    (all three then the same as apply)
  POST /api/zone    {"key", "name", "offset_ms", "volume_max_db"}: writes
                    those (empty = not set) to a managed block in the zone's
                    <key>-ap2.custom file and restarts that zone's
                    shairport-sync; sink_zone_manager starts it again at once
  POST /api/bt_reset {}: power-cycles the host's Bluetooth adapter(s) through
                    BlueZ on the host bus (bt_reset.py); BlueZ clients then
                    reconnect their sinks

Python standard library only. Runs in the host network namespace even when
ap2_ip is set, so Ingress can reach it; it only talks to PulseAudio through
its socket, which works from either namespace.

Only Home Assistant's Ingress proxy (172.30.32.2) and localhost may connect:
the add-on uses host networking, so the port is also open on the LAN.
"""

import html
import json
import logging
import os
import re
import signal
import subprocess
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import bt_reset
import remap_sinks as rs

PORT = int(os.environ.get("WEB_PANEL_PORT", "8285"))
ALLOWED = set(os.environ.get("WEB_PANEL_ALLOWED", "172.30.32.2,127.0.0.1,::1").split(","))
OPTIONS_FILE = Path(os.environ.get("RS_OPTIONS_FILE", "/data/options.json"))
ZONE_CONFIG_DIR = Path(os.environ.get("SZM_CONFIG_DIR", "/config/shairport-sync/config"))
SUPERVISOR_URL = os.environ.get("SUPERVISOR_URL", "http://supervisor")
REMAP_SINKS = os.environ.get("REMAP_SINKS_SCRIPT", "/usr/sbin/remap_sinks.py")

log = logging.getLogger("web_panel")

PAIR_LABEL = {"front": "Front L/R", "rear": "Rear L/R", "side": "Side L/R",
              "center_lfe": "Center / LFE", "stereo": "Stereo"}
PAIR_NEEDS = {"front": {"front-left", "front-right"}, "rear": {"rear-left", "rear-right"},
              "side": {"side-left", "side-right"}, "center_lfe": {"front-center", "lfe"}}


# -- data ---------------------------------------------------------------
def list_sinks() -> list:
    """[{name, description, driver, channel_map}] for every sink."""
    try:
        res = subprocess.run(["pactl", "list", "sinks"], capture_output=True,
                             text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return []
    sinks, cur = [], None
    for line in res.stdout.splitlines() if res.returncode == 0 else []:
        if line.startswith("Sink #"):
            cur = {}
            sinks.append(cur)
            continue
        s = line.strip()
        if cur is None or ":" not in s:
            continue
        key, val = s.split(":", 1)
        if key in ("Name", "Description", "Driver", "Channel Map"):
            cur[key.lower().replace(" ", "_")] = val.strip()
    return sinks


PROFILE_RE = re.compile(r"^\s+(\S+): (.+?) \(sinks: (\d+), sources: (\d+), priority: (\d+)"
                        r"(?:, available: (\w+))?\)\s*$")


def pactl_text(*args) -> str:
    try:
        res = subprocess.run(["pactl", *args], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return res.stdout if res.returncode == 0 else ""


def list_cards(text: str = None) -> list:
    """[{name, driver, description, active, profiles: [{name, description,
    sinks, available}]}] from `pactl list cards`."""
    text = pactl_text("list", "cards") if text is None else text
    cards, cur, section = [], None, None
    for line in text.splitlines():
        if line.startswith("Card #"):
            cur = {"name": "", "driver": "", "description": "", "active": "", "profiles": []}
            cards.append(cur)
            section = None
            continue
        if cur is None or not line.strip():
            continue
        depth = len(line) - len(line.lstrip("\t"))
        s = line.strip()
        if depth == 1:
            section = None
            if s.startswith("Name:"):
                cur["name"] = s[5:].strip()
            elif s.startswith("Driver:"):
                cur["driver"] = s[7:].strip()
            elif s.startswith("Active Profile:"):
                cur["active"] = s[15:].strip()
            elif s in ("Properties:", "Profiles:", "Ports:"):
                section = s[:-1]
        elif depth == 2 and section == "Properties":
            m = re.match(r'(\S+) = "(.*)"$', s)
            if m and m.group(1) == "device.description":
                cur["description"] = m.group(2)
            elif m and m.group(1) == "alsa.card_name" and not cur["description"]:
                cur["description"] = m.group(2)
        elif depth == 2 and section == "Profiles":
            m = PROFILE_RE.match(line)
            if m:
                cur["profiles"].append({"name": m.group(1), "description": m.group(2),
                                        "sinks": int(m.group(3)),
                                        "available": m.group(6) != "no"})
    return cards


def card_outputs(card_name: str, sink_names) -> list:
    """The sinks that belong to a card, by PulseAudio's naming:
    alsa_card.X -> alsa_output.X.*, bluez_card.X -> bluez_sink.X.*"""
    for card_prefix, sink_prefix in (("alsa_card.", "alsa_output."), ("bluez_card.", "bluez_sink.")):
        if card_name.startswith(card_prefix):
            stem = sink_prefix + card_name[len(card_prefix):] + "."
            return sorted(n for n in sink_names if n.startswith(stem))
    return []


def channels_of(args: str):
    """(channels choice, left, right) a remap sink's arguments correspond
    to: a named pair (left/right empty), "custom" with its two master
    channels in order, or (None, "", "")."""
    mcm = (rs.arg_of(args, "master_channel_map") or rs.arg_of(args, "channel_map")
           or "front-left,front-right").split(",")
    for pair in ("front", "rear", "side", "center_lfe"):
        if mcm == rs.PAIRS[pair].split(","):
            return pair, "", ""
    if len(mcm) == 2 and all(rs.CHANNEL_RE.match(c) for c in mcm):
        return "custom", mcm[0], mcm[1]
    return None, "", ""


def needs_of(pair: str, left: str = "", right: str = "") -> set:
    if pair == "custom":
        return {left, right} - {""}
    return PAIR_NEEDS.get(pair, {"front-left", "front-right"})


def channels_label(pair: str, left: str = "", right: str = "") -> str:
    if pair == "custom":
        l, r = rs.channel_abbr(left).upper(), rs.channel_abbr(right).upper()
        return f"{l} (mono)" if left == right else f"{l} + {r}"
    return PAIR_LABEL.get(pair, pair)


def running_zones() -> set:
    """Sink names that have a shairport-sync running, from /proc."""
    names = set()
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            cmd = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode()
        except OSError:
            continue
        if "shairport-sync" in cmd:
            for m in re.finditer(r"([^\s/]+)-ap2\.conf", cmd):
                names.add(m.group(1))
    return names


CUSTOM_KEYS = {"name": "name", "audio_backend_latency_offset_in_seconds": "offset_s",
               "volume_max_db": "volume_max_db"}


def shairport_procs() -> list:
    """[(pid, argv)] for every running shairport-sync."""
    out = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        argv = [a.decode(errors="replace") for a in argv if a]
        if argv and os.path.basename(argv[0]) == "shairport-sync":
            out.append((int(pid), argv))
    return out


def proc_uptime(pid: int):
    """Seconds since the process started, or None."""
    try:
        start_ticks = int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19])
        uptime = float(Path("/proc/uptime").read_text().split()[0])
        return max(0, int(uptime - start_ticks / os.sysconf("SC_CLK_TCK")))
    except (OSError, ValueError, IndexError):
        return None


def streaming_pids() -> set:
    """PIDs that have a PulseAudio stream open (i.e. are playing)."""
    try:
        res = subprocess.run(["pactl", "list", "sink-inputs"], capture_output=True,
                             text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return set()
    return {int(m) for m in re.findall(r'application\.process\.id = "(\d+)"', res.stdout)}


def conf_value(text: str, key: str):
    """An uncommented `key = value;` from a libconfig file; a quoted value is
    unescaped the same way sink_zone_manager reads a custom name."""
    m = re.search(r'^\s*' + re.escape(key) + r'\s*=\s*(?:"((?:[^"\\]|\\.)*)"|([^";]*?))\s*;',
                  text, re.M)
    if not m:
        return None
    if m.group(1) is not None:
        return re.sub(r'\\(.)', r'\1', m.group(1))
    return m.group(2).strip()


def read_custom(path: Path, part: str = "all") -> dict:
    """The overrides this panel knows about, from a zone's -ap2.custom file
    (uncommented lines only). part: "all", "managed" (the panel's block) or
    "hand" (everything outside it)."""
    try:
        text = path.read_text()
    except OSError:
        return {}
    if part == "hand":
        text = strip_block(text)
    elif part == "managed":
        lines = text.splitlines()
        try:
            text = "\n".join(lines[lines.index(ZBEGIN):lines.index(ZEND, lines.index(ZBEGIN))])
        except ValueError:
            text = ""
    return {short: conf_value(text, key) for key, short in CUSTOM_KEYS.items()
            if conf_value(text, key) is not None}


def zones() -> list:
    """Every running shairport-sync, with what it plays to."""
    playing = streaming_pids()
    out = []
    for pid, argv in shairport_procs():
        opt = {argv[i]: argv[i + 1] for i in range(len(argv) - 1) if argv[i] in ("-a", "-p", "-c")}
        conf = Path(opt.get("-c", ""))
        try:
            text = conf.read_text()
        except OSError:
            text = ""
        key = conf.name[:-len("-ap2.conf")] if conf.name.endswith("-ap2.conf") else conf.name
        backend = conf_value(text, "output_backend") or "?"
        sink = conf_value(text.split("pulseaudio :", 1)[1], "sink") if "pulseaudio :" in text else None
        if backend == "pipe":
            kind, target = "cast", "Cast Bridge"
        elif sink and sink.startswith("bluez_"):
            kind, target = "bluetooth", sink
        else:
            kind, target = "sink", sink or "?"
        custom_file = conf.with_name(f"{key}-ap2.custom")
        out.append({
            "key": key, "name": opt.get("-a", key), "port": opt.get("-p"), "pid": pid,
            "kind": kind, "target": target, "uptime_s": proc_uptime(pid),
            "playing": pid in playing if kind != "cast" else None,
            "editable": kind != "cast" and custom_file.exists(),
            "custom_file": str(custom_file),
            "overrides": read_custom(custom_file) if kind != "cast" else {},
            "managed_overrides": read_custom(custom_file, "managed") if kind != "cast" else {},
            "hand_overrides": read_custom(custom_file, "hand") if kind != "cast" else {},
        })
    out.sort(key=lambda z: (z["kind"] == "cast", z["name"].lower()))
    return out


def state() -> dict:
    sinks = list_sinks()
    descs = {s["name"]: s.get("description", "") for s in sinks if "name" in s}
    loaded = rs.loaded_remap_modules() or []
    running = running_zones()

    configured = {}
    for index, entry in enumerate(rs.load_options()):
        if not isinstance(entry, dict):
            continue
        master = str(entry.get("master", "")).strip()
        pair = str(entry.get("channels", "")).strip()
        left, right = str(entry.get("left", "") or ""), str(entry.get("right", "") or "")
        name = str(entry.get("name", "") or "").strip() or rs.entry_system_name(entry, descs)
        if name:
            configured[name] = {"master": master, "pair": pair, "left": left, "right": right,
                                "index": index}

    remaps, used = [], {}
    for args in loaded:
        name = rs.sink_name_of(args)
        master = rs.arg_of(args, "master") or ""
        pair, left, right = channels_of(args)
        used.setdefault(master, set()).update(rs.claim(args)[1])
        remaps.append({
            "name": name,
            "system_name": rs.system_name(master, pair, descs, left, right) if pair else None,
            "master": master,
            "master_description": descs.get(master, ""),
            "pair": pair, "left": left, "right": right,
            "channels": channels_label(pair, left, right) if pair else
                        (rs.arg_of(args, "master_channel_map") or "?"),
            "managed": name in configured,
            "index": configured[name]["index"] if name in configured else None,
            "loaded": True,
            "zone_running": name in running,
        })
    for name, c in configured.items():
        if not any(r["name"] == name for r in remaps):
            remaps.append({
                "name": name,
                "system_name": rs.system_name(c["master"], c["pair"], descs, c["left"], c["right"]),
                "master": c["master"], "master_description": descs.get(c["master"], ""),
                "pair": c["pair"], "left": c["left"], "right": c["right"],
                "channels": channels_label(c["pair"], c["left"], c["right"]),
                "managed": True, "index": c["index"], "loaded": False, "zone_running": False,
            })
            used.setdefault(c["master"], set()).update(needs_of(c["pair"], c["left"], c["right"]))

    card_names = {s["name"] for s in sinks if "alsa" in s.get("driver", "") and "name" in s}
    for r in remaps:
        if r["managed"]:
            continue
        if not r["pair"]:
            r["adopt_note"] = f"channels {r['channels']} can't be set in the options"
        elif r["master"] not in card_names:
            r["adopt_note"] = "its master is not a sound card"
        r["adoptable"] = "adopt_note" not in r

    # Option order, then unmanaged sinks, so a renamed sink stays in place.
    remaps.sort(key=lambda r: (r["index"] is None, r["index"] or 0, r["name"] or ""))

    cards = []
    for s in sinks:
        if "alsa" not in s.get("driver", "") or "name" not in s:
            continue
        chans = {c.strip() for c in s.get("channel_map", "").split(",") if c.strip()}
        taken = used.get(s["name"], set())
        order = [c.strip() for c in s.get("channel_map", "").split(",") if c.strip()]
        pairs = sorted((p for p, needs in PAIR_NEEDS.items() if needs <= chans),
                       key=lambda p: min(order.index(c) for c in PAIR_NEEDS[p]))
        if not pairs and chans:
            pairs = ["stereo"]
        cards.append({
            "name": s["name"], "description": s.get("description", ""),
            "used": [p for p in pairs if (PAIR_NEEDS.get(p) or chans) & taken],
            "free": [p for p in pairs if not ((PAIR_NEEDS.get(p) or chans) & taken)],
        })
        cards[-1]["free_names"] = {p: rs.system_name(s["name"], p, descs) for p in cards[-1]["free"]}
        cards[-1]["free_channels"] = [c for c in order if c not in taken and rs.CHANNEL_RE.match(c)]
        sn = rs.system_name(s["name"], "front", descs)
        cards[-1]["name_base"] = sn[:-len("_fl_fr")] if sn else ""
    # Combine sinks: configured ones, then unmanaged ones loaded now.
    # rs.COMBINE_MODS was filled by rs.loaded_remap_modules() above.
    sink_names = {s["name"] for s in sinks if "name" in s}
    combines = []
    for index, entry in enumerate(load_combine_options()):
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name", "")).strip()
        members = [m.strip() for m in str(entry.get("sinks", "")).split(",") if m.strip()]
        combines.append({"name": name, "members": members, "index": index, "managed": True,
                         "description": str(entry.get("description", "") or ""),
                         "loaded": name in rs.COMBINE_MODS,
                         "missing": [m for m in members if m not in sink_names],
                         "zone_running": name in running})
    for name, (_, args) in rs.COMBINE_MODS.items():
        if not any(c["name"] == name for c in combines):
            slaves = rs.arg_of(args, "slaves") or ""
            m = re.search(r"device\.description='([^'\"]*)'", args)
            combines.append({"name": name, "members": [x for x in slaves.split(",") if x],
                             "index": None, "managed": False, "loaded": True,
                             "description": m.group(1) if m else "", "missing": [],
                             "zone_running": name in running, "adoptable": bool(slaves)})
    choices = []
    for s in sinks:
        drv, n = s.get("driver", ""), s.get("name", "")
        if not n or "combine" in drv or n.endswith(".monitor"):
            continue
        kind = "remap" if "remap" in drv else "bluetooth" if n.startswith("bluez_") else \
               "card" if "alsa" in drv else "other"
        master = next((r["master"] for r in remaps if r["name"] == n), "")
        choices.append({"name": n, "description": s.get("description", ""), "kind": kind,
                        "master": master})

    pa_cards = list_cards()
    for c in pa_cards:
        c["outputs"] = card_outputs(c["name"], [s["name"] for s in cards])
    zlist = zones()
    for z in zlist:
        z["enabled"] = True
    running_keys = {z["key"] for z in zlist}
    for key in sorted(load_disabled_zones()):
        if key in running_keys:   # switched off a moment ago; still stopping
            continue
        ov = read_custom(ZONE_CONFIG_DIR / f"{key}-ap2.custom")
        zlist.append({"key": key, "name": ov.get("name") or key.replace("_", " "), "port": None,
                      "pid": None, "kind": "sink" if key in sink_names else "bluetooth",
                      "target": key, "uptime_s": None, "playing": None, "editable": False,
                      "overrides": ov, "managed_overrides": {}, "hand_overrides": {},
                      "enabled": False})

    return {"zones": zlist, "pa_cards": pa_cards, "cards": cards, "remap_sinks": remaps,
            "combine_sinks": combines, "sink_choices": choices}


# -- apply ----------------------------------------------------------------
class ApplyError(Exception):
    pass


def save_options(options: dict) -> None:
    """Saves the add-on's whole options through the Supervisor (validated
    against the schema there), then writes /data/options.json, which the
    Supervisor otherwise only rewrites when the add-on starts."""
    token = os.environ.get("SUPERVISOR_TOKEN", "")
    if not token:
        raise ApplyError("no SUPERVISOR_TOKEN (hassio_api missing from config.yaml?)")
    req = urllib.request.Request(
        f"{SUPERVISOR_URL}/addons/self/options",
        data=json.dumps({"options": options}).encode(), method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            body = json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read() or b"{}")
        except ValueError:
            body = {}
        raise ApplyError(f"Supervisor refused the options: {body.get('message') or e}")
    except (OSError, ValueError) as e:
        raise ApplyError(f"cannot reach the Supervisor: {e}")
    if body.get("result") != "ok":
        raise ApplyError(f"Supervisor refused the options: {body.get('message') or body}")
    tmp = OPTIONS_FILE.with_name(OPTIONS_FILE.name + ".tmp")
    tmp.write_text(json.dumps(options))
    os.replace(tmp, OPTIONS_FILE)


def change_and_apply(change, edit_options=None) -> list:
    """Reads the options, lets change(entries, descs) edit the remap_sinks
    list in place and return what it did (and edit_options(options), if
    given, edit the rest), then saves and applies. Returns the log lines to
    show."""
    try:
        options = json.loads(OPTIONS_FILE.read_text())
    except (OSError, ValueError) as e:
        raise ApplyError(f"cannot read {OPTIONS_FILE}: {e}")
    entries = options.setdefault("remap_sinks", [])
    descs = {s["name"]: s.get("description", "") for s in list_sinks() if "name" in s}

    lines = change(entries, descs)
    if edit_options:
        lines = edit_options(options) + lines
    if not lines:
        return ["nothing changed"]

    resolved = [e.get("name") or rs.entry_system_name(e, descs) for e in entries]
    dupes = {n for n in resolved if n and resolved.count(n) > 1}
    if dupes:
        raise ApplyError(f"name used twice: {', '.join(sorted(dupes))}")

    save_options(options)
    lines.append("options saved")
    try:
        res = subprocess.run(["python3", "-I", REMAP_SINKS, "--load"], capture_output=True,
                             text=True, timeout=90)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ApplyError(f"options saved, but applying failed: {e}")
    lines += [l for l in res.stdout.splitlines() + res.stderr.splitlines()
              if not l.startswith(("[remap_sinks] +", "[remap_sinks] -", "[remap_sinks] @",
                                   "[remap_sinks]  "))]
    return lines


def apply_names(names: dict) -> list:
    """Renames option entries ({index: new name})."""
    return change_and_apply(lambda entries, descs: _rename(entries, descs, names))


def _rename(entries: list, descs: dict, names: dict) -> list:
    lines = []
    for key, new in names.items():
        try:
            i = int(key)
            entry = entries[i]
        except (ValueError, IndexError, TypeError):
            raise ApplyError(f"no remap_sinks entry #{key}; reload the page")
        new = str(new).strip()
        if not rs.NAME_RE.match(new):
            raise ApplyError(f"'{new}': letters, digits, _ . - only")
        old = entry.get("name") or rs.entry_system_name(entry, descs)
        if new != old:
            entry["name"] = new
            lines.append(f"rename {old} -> {new}")
    return lines


def card_sink_prefix(card: str) -> str:
    for card_prefix, sink_prefix in (("alsa_card.", "alsa_output."), ("bluez_card.", "bluez_sink.")):
        if card.startswith(card_prefix):
            return sink_prefix + card[len(card_prefix):] + "."
    return "\0"   # matches no sink


def set_profile(card: str, profile: str) -> list:
    """Sets a card's profile now, keeps it in card_profiles, and re-points
    the card's remap_sinks entries to its new output sink where their pair
    still exists. Entries whose pair is gone stay in the options (pointing at
    the old sink), so switching back brings them back."""
    pc = next((c for c in list_cards() if c["name"] == card), None)
    if not pc:
        raise ApplyError(f"no card {card}; reload the page")
    prof = next((p for p in pc["profiles"] if p["name"] == profile), None)
    if not prof:
        raise ApplyError(f"{card} has no profile {profile}")
    old_profile = pc["active"]
    # Descriptions before the switch: an entry using its system name gets it
    # from its master's description, and that sink is about to go away.
    pre_descs = {s["name"]: s.get("description", "") for s in list_sinks() if "name" in s}
    lines = []
    if old_profile != profile:
        res = subprocess.run(["pactl", "set-card-profile", card, profile],
                             capture_output=True, text=True, timeout=15)
        if res.returncode != 0:
            raise ApplyError(f"setting the profile failed: {res.stderr.strip() or res.returncode}")
        lines.append(f"set {pc['description'] or card} to {prof['description']} (was {old_profile})")
    else:
        lines.append(f"{pc['description'] or card} is already {prof['description']}; saving it")

    # The new output sink(s) appear right after the switch; wait briefly.
    prefix = card_sink_prefix(card)
    outputs = []
    for _ in range(20):
        outputs = [s for s in list_sinks() if s.get("name", "").startswith(prefix)
                   and "remap" not in s.get("driver", "")]
        if outputs or prof["sinks"] == 0:
            break
        time.sleep(0.15)
    chans = {s["name"]: {c.strip() for c in s.get("channel_map", "").split(",") if c.strip()}
             for s in outputs}

    def edit_options(options):
        cps = [e for e in (options.get("card_profiles") or [])
               if isinstance(e, dict) and e.get("card") != card]
        cps.append({"card": card, "profile": profile})
        options["card_profiles"] = cps
        return []

    def change(entries, descs):
        out = []
        for e in entries:
            master = str(e.get("master", ""))
            if not master.startswith(prefix):
                continue
            pair = str(e.get("channels", ""))
            l, r = str(e.get("left", "") or ""), str(e.get("right", "") or "")
            needs = needs_of(pair, l, r)
            name = e.get("name") or rs.entry_system_name(e, pre_descs) or "?"
            fit = next((n for n, c in chans.items() if needs <= c), None)
            if fit and fit != master:
                e["master"] = fit
                if not e.get("name") and name != "?":
                    # Keep the zone's name (and MA player): the new master's
                    # description would give it a different system name.
                    e["name"] = name
                out.append(f"{name}: moved to {fit}")
            elif fit:
                out.append(f"{name}: unchanged")
            else:
                out.append(f"{name}: stops — the new profile has no {channels_label(pair, l, r)}"
                           " (kept in the options; switching back restores it)")
        return out or ["no remap sinks on this card"]
    try:
        return lines + change_and_apply(change, edit_options)
    except ApplyError as e:
        raise ApplyError(f"{e} (the profile was already switched live: {lines[0]})")


def load_disabled_zones() -> set:
    try:
        keys = json.loads(OPTIONS_FILE.read_text()).get("disabled_zones") or []
    except (OSError, ValueError):
        return set()
    return {str(k) for k in keys} if isinstance(keys, list) else set()


def set_zone_enabled(key: str, enabled: bool) -> list:
    """Switches a zone off (stops its AirPlay receiver, keeps its sink, port
    and settings) or back on, by saving the disabled_zones option. The zone
    manager notices the change and acts within a few seconds."""
    key = str(key).strip()
    z = next((z for z in state()["zones"] if z["key"] == key), None)
    if not z:
        raise ApplyError(f"no zone {key}; reload the page")
    if z["kind"] == "cast":
        raise ApplyError("Cast Bridge zones can't be switched here")
    try:
        options = json.loads(OPTIONS_FILE.read_text())
    except (OSError, ValueError) as e:
        raise ApplyError(f"cannot read {OPTIONS_FILE}: {e}")
    off = [k for k in (options.get("disabled_zones") or []) if k != key]
    if not enabled:
        off.append(key)
    if sorted(off) == sorted(options.get("disabled_zones") or []):
        return [f"{z['name']} is already {'on' if enabled else 'off'}"]
    options["disabled_zones"] = off
    save_options(options)
    return [f"switched {z['name']} {'on' if enabled else 'off'}; options saved",
            "the zone manager " + ("starts" if enabled else "stops") + " it within a few seconds" +
            ("" if enabled else " (its sink, port and settings stay)")]


def create_defaults(master: str) -> list:
    """Adds one sink per free channel pair of a card output (Front, Rear,
    Center/LFE, Side, in the card's order; a 2-channel card gets one Stereo
    sink), each under its system name, ready to be renamed."""
    card = next((c for c in state()["cards"] if c["name"] == master), None)
    if not card:
        raise ApplyError(f"no sound card output {master}; reload the page")
    todo = [(p, card["free_names"].get(p)) for p in card["free"]]
    if not todo:
        raise ApplyError(f"{card['description']} has no free channel pairs")
    if any(not n for _, n in todo):
        raise ApplyError("cannot make system names for this card; add sinks in the Configuration tab")

    def change(entries, descs):
        for pair, name in todo:
            entries.append({"name": name, "master": master, "channels": pair})
        return [f"create {name} ({PAIR_LABEL[pair]})" for pair, name in todo]
    return change_and_apply(change)


def create_sink(master: str, pair: str, left: str = "", right: str = "") -> list:
    """Adds a sink on a free pair (or, for custom, two free channels) of a
    card, named with its system name."""
    card = next((c for c in state()["cards"] if c["name"] == master), None)
    if not card:
        raise ApplyError(f"no sound card {master}; reload the page")
    if pair == "custom":
        for ch in (left, right):
            if ch not in card["free_channels"]:
                raise ApplyError(f"channel {ch or '(none)'} of {card['description']} is not free")
        descs = {s["name"]: s.get("description", "") for s in list_sinks() if "name" in s}
        name = rs.system_name(master, "custom", descs, left, right)
    else:
        if pair not in card["free"]:
            raise ApplyError(f"{PAIR_LABEL.get(pair, pair)} of {card['description']} is not free")
        name = card["free_names"].get(pair)
    if not name:
        raise ApplyError("cannot make a system name for this card; add it in the Configuration tab")

    def change(entries, descs):
        entry = {"name": name, "master": master, "channels": pair}
        if pair == "custom":
            entry.update(left=left, right=right)
        entries.append(entry)
        return [f"create {name} ({card['description']}, {channels_label(pair, left, right)})"]
    return change_and_apply(change)


def load_combine_options() -> list:
    try:
        entries = json.loads(OPTIONS_FILE.read_text()).get("combine_sinks") or []
    except (OSError, ValueError):
        return []
    return entries if isinstance(entries, list) else []


def create_combine(name: str, description: str, members: list) -> list:
    """Adds a combine sink of two or more existing sinks of any kind."""
    st = state()
    name = name.strip()
    if not rs.NAME_RE.match(name):
        raise ApplyError("name: letters, digits, _ . - only")
    taken = {c["name"] for c in st["sink_choices"]} | {r["name"] for r in st["remap_sinks"]} | \
            {c["name"] for c in st["combine_sinks"]}
    if name in taken:
        raise ApplyError(f"{name} is already the name of a sink")
    members = list(dict.fromkeys(str(m).strip() for m in members if str(m).strip()))
    if len(members) < 2:
        raise ApplyError("pick at least two sinks")
    known = {c["name"] for c in st["sink_choices"]}
    unknown = [m for m in members if m not in known]
    if unknown:
        raise ApplyError(f"no such sink: {', '.join(unknown)}; reload the page")
    description = description.strip()
    if '"' in description or "'" in description:
        raise ApplyError("description: no quotes")

    def edit_options(options):
        entry = {"name": name, "sinks": ",".join(members)}
        if description:
            entry["description"] = description
        options.setdefault("combine_sinks", []).append(entry)
        return [f"create combine sink {name}: {', '.join(members)}"]
    return change_and_apply(lambda entries, descs: [], edit_options)


def remove_combine(index) -> list:
    def edit_options(options):
        entries = options.get("combine_sinks") or []
        try:
            entry = entries.pop(int(index))
        except (ValueError, IndexError, TypeError):
            raise ApplyError("no such combine sink; reload the page")
        return [f"remove combine sink {entry.get('name')}"]
    return change_and_apply(lambda entries, descs: [], edit_options)


def adopt_combine(name: str) -> list:
    c = next((c for c in state()["combine_sinks"] if c["name"] == name and not c["managed"]), None)
    if not c or not c.get("adoptable"):
        raise ApplyError(f"{name} is not an unmanaged combine sink; reload the page")

    def edit_options(options):
        entry = {"name": name, "sinks": ",".join(c["members"])}
        if c["description"]:
            entry["description"] = c["description"]
        options.setdefault("combine_sinks", []).append(entry)
        return [f"adopt combine sink {name}: {', '.join(c['members'])}"]
    return change_and_apply(lambda entries, descs: [], edit_options)


def adopt_sink(name: str) -> list:
    """Adds an unmanaged sink to the options under its current name, so its
    zone and Music Assistant player stay the same."""
    row = next((r for r in state()["remap_sinks"] if r["name"] == name), None)
    if not row or row["managed"]:
        raise ApplyError(f"{name} is not an unmanaged sink; reload the page")
    if not row.get("adoptable"):
        raise ApplyError(f"{name} can't be adopted: {row.get('adopt_note')}")
    args = next((a for a in rs.loaded_remap_modules() or [] if rs.sink_name_of(a) == name), "")
    m = re.search(r"device\.description='([^'\"]*)'", args)

    def change(entries, descs):
        entry = {"name": name, "master": row["master"], "channels": row["pair"]}
        if row["pair"] == "custom":
            entry.update(left=row["left"], right=row["right"])
        if m and m.group(1):
            entry["description"] = m.group(1)
        entries.append(entry)
        return [f"adopt {name} ({row['master_description']}, {row['channels']})"]
    return change_and_apply(change)


ZBEGIN = "// >>> web panel managed >>>"
ZEND = "// <<< web panel managed <<<"


def strip_block(text: str) -> str:
    lines = text.splitlines()
    try:
        b = lines.index(ZBEGIN)
        e = lines.index(ZEND, b)
        lines = lines[:b] + lines[e + 1:]
    except ValueError:
        pass
    return "\n".join(lines) + ("\n" if lines else "")


def save_zone(key: str, name: str, offset_ms: str, volume_max_db: str) -> list:
    """Writes the zone's overrides to the managed block of its -ap2.custom
    file (an empty value leaves that setting out), then restarts the zone."""
    zone = next((z for z in zones() if z["key"] == key), None)
    if not zone:
        raise ApplyError(f"zone {key} is not running; reload the page")
    if not zone["editable"]:
        raise ApplyError(f"{zone['name']} can't be edited here (Cast Bridge zone)")

    settings = {}
    name = name.strip()
    if name:
        if len(name) > 60 or any(ord(c) < 32 for c in name):
            raise ApplyError("name: up to 60 characters, no control characters")
        settings["name"] = '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if str(offset_ms).strip():
        try:
            ms = int(str(offset_ms).strip())
        except ValueError:
            raise ApplyError("latency offset: whole milliseconds, e.g. -120")
        if not -2000 <= ms <= 2000:
            raise ApplyError("latency offset: between -2000 and 2000 ms")
        settings["audio_backend_latency_offset_in_seconds"] = f"{ms / 1000:.3f}"
    if str(volume_max_db).strip():
        try:
            db = float(str(volume_max_db).strip())
        except ValueError:
            raise ApplyError("max volume: dB, e.g. -6")
        if not -96 <= db <= 0:
            raise ApplyError("max volume: between -96 and 0 dB")
        settings["volume_max_db"] = f"{db:.1f}"

    path = Path(zone["custom_file"])
    try:
        old = path.read_text()
    except OSError as e:
        raise ApplyError(f"cannot read {path}: {e}")
    # The form shows every setting it manages with its value in effect, so a
    # line for one of them set by hand outside the block is taken over: it is
    # commented out (kept for reference) and the value lives in the block.
    # libconfig rejects a setting that is set twice.
    moved, kept = [], []
    for line in strip_block(old).splitlines():
        key = next((k for k in CUSTOM_KEYS
                    if re.match(r"\s*" + re.escape(k) + r"\s*=", line)), None)
        if key:
            moved.append(key)
            kept.append(f"// moved to the web panel block: {line.strip()}")
        else:
            kept.append(line)
    rest = "\n".join(kept) + ("\n" if kept else "")
    block = [ZBEGIN] + [f"{k} = {v};" for k, v in settings.items()] + [ZEND] if settings else []
    new = rest.rstrip("\n") + ("\n" if rest.strip() else "") + "\n".join(block) + ("\n" if block else "")
    if new == old:
        lines = [f"{path.name} unchanged"]
    else:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(new)
        os.replace(tmp, path)
        lines = [f"wrote {path.name}: " + (", ".join(f"{k} = {v}" for k, v in settings.items())
                                           or "no overrides")]
        if moved:
            lines.append(f"took over {', '.join(moved)} set by hand (that line is now commented out)")
    try:
        os.kill(zone["pid"], signal.SIGTERM)
    except OSError as e:
        raise ApplyError(f"saved, but restarting the zone failed: {e}")
    lines.append(f"restarting {zone['name']} (stopped pid {zone['pid']}); "
                 "the zone manager starts it again within a few seconds")
    if (name and name != zone["name"]) or (not name and "name" in zone["overrides"]):
        lines.append("the AirPlay name changed, so Music Assistant will see a new player")
    return lines


# -- page -----------------------------------------------------------------
CSS = """
body{margin:0;background:#111318;color:#e6e8eb;font:14px system-ui,sans-serif}
header{padding:20px 32px;border-bottom:1px solid #2a2f38}
header b{font-size:18px;font-weight:600}
main{max-width:1280px;margin:0 auto;padding:28px 32px;display:flex;flex-direction:column;gap:28px}
h1,h2{margin:0;font-weight:600}h1{font-size:22px}h2{font-size:20px}
p.sub{margin:4px 0 0;color:#9aa3ae}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:16px;margin-top:14px}
.card{border:1px solid #2a2f38;border-radius:12px;background:#1a1d24;padding:16px;display:flex;flex-direction:column;gap:8px}
.mono{font-family:ui-monospace,monospace;font-size:12px;color:#9aa3ae;overflow-wrap:anywhere}
.box{overflow-x:auto;border:1px solid #2a2f38;border-radius:12px;background:#1a1d24;margin-top:14px}
table{width:100%;min-width:900px;border-collapse:collapse}
th{text-align:left;color:#9aa3ae;font-size:12px;text-transform:uppercase;letter-spacing:.04em;padding:12px 16px;font-weight:600}
td{padding:12px 16px;border-top:1px solid #2a2f38;color:#c9d1db}
td.sys{white-space:nowrap}
td.name{font-family:ui-monospace,monospace;font-size:13px;color:#e6e8eb}
button.copy{width:32px;height:32px;border:1px solid #353b46;border-radius:6px;background:transparent;color:#b8c0ca;cursor:pointer;vertical-align:middle;margin-left:6px}
.warn{color:#f0b26a}
input.nm{width:100%;min-width:340px;min-height:40px;padding:0 10px;border-radius:8px;border:1px solid #353b46;background:#111318;color:#e6e8eb;font:13px ui-monospace,monospace;box-sizing:border-box}
input.nm.changed{border-color:#4a6a96}
.bar{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:12px;margin-top:14px}
.btn{min-height:44px;padding:0 18px;border-radius:8px;font:600 14px system-ui,sans-serif;cursor:pointer}
.btn.pri{border:0;background:#3d8bfd;color:#0b1220}.btn.sec{border:1px solid #353b46;background:transparent;color:#e6e8eb;font-weight:500}
.btn:disabled{opacity:.5;cursor:default}
.kgroup{flex:1 1 240px;min-width:0}.kgroup h3{margin:0 0 6px;font-size:12px;color:#9aa3ae;font-weight:500;text-transform:uppercase;letter-spacing:.04em}
form.create .kgroup label{display:flex;gap:10px;align-items:flex-start;padding:8px 0;margin:0;border-top:1px solid #23272f;cursor:pointer;font-size:14px;color:#e6e8eb;font-weight:400}
.kgroup input{width:18px;height:18px;margin-top:2px;flex:none}
#kwarn{color:#f0b26a;font-size:13px}
pre#result,pre#zresult,pre#cresult,pre#kresult{margin:14px 0 0;border:1px solid #2a2f38;border-radius:12px;background:#0d0f13;padding:14px 16px;font:12px/1.7 ui-monospace,monospace;color:#b8c0ca;white-space:pre-wrap}
pre#result.err,pre#zresult.err,pre#cresult.err,pre#kresult.err{color:#f0b26a}
.btn.sm{min-height:36px;padding:0 12px;font-size:13px;margin-left:10px}
form.create{display:flex;flex-wrap:wrap;align-items:flex-end;gap:12px;border:1px dashed #353b46;border-radius:12px;padding:16px;margin-top:14px}
form.create label,form.create .lbl{display:block;font-size:12px;color:#9aa3ae;font-weight:500;margin-bottom:6px}
form.create select{min-height:44px;padding:0 12px;border-radius:8px;border:1px solid #353b46;background:#111318;color:#e6e8eb;font:14px system-ui,sans-serif}
tr.zedit td{background:#161a21}
form.zform{display:flex;flex-wrap:wrap;align-items:flex-end;gap:12px}
form.zform label{display:block;font-size:12px;color:#9aa3ae;font-weight:500;margin-bottom:6px}
form.zform input{min-height:40px;padding:0 10px;border-radius:8px;border:1px solid #353b46;background:#111318;color:#e6e8eb;font:14px system-ui,sans-serif;box-sizing:border-box;width:100%}
form.create .preview{min-height:44px;display:flex;align-items:center;padding:0 12px;border-radius:8px;background:#0d0f13;font:13px ui-monospace,monospace;color:#c9d1db;overflow-wrap:anywhere}
"""

COPY_ICON = ('<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
             'stroke-width="2" aria-hidden="true"><rect x="9" y="9" width="12" height="12" rx="2">'
             '</rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path></svg>')

SCRIPT = """
const inputs = [...document.querySelectorAll('input.nm')];
const applyBtn = document.getElementById('apply'), discardBtn = document.getElementById('discard');
const changed = () => inputs.filter(i => i.value.trim() !== i.defaultValue);
const refresh = () => { inputs.forEach(i => i.classList.toggle('changed', i.value.trim() !== i.defaultValue));
  const n = changed().length; if (applyBtn) { applyBtn.disabled = !n; discardBtn.disabled = !n; } };
inputs.forEach(i => i.addEventListener('input', refresh));
if (discardBtn) discardBtn.addEventListener('click', () => { inputs.forEach(i => i.value = i.defaultValue); refresh(); });
const allButtons = () => document.querySelectorAll('button.btn');
async function post(url, body, outId = 'result', delay = 3000) {
  const out = document.getElementById(outId);
  allButtons().forEach(b => b.disabled = true);
  out.hidden = false; out.className = ''; out.textContent = 'Applying...';
  out.scrollIntoView({block: 'nearest'});
  try {
    const r = await fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'},
                                body: JSON.stringify(body)});
    const j = await r.json();
    out.textContent = (j.lines || []).join('\\n') + (j.error ? '\\n' + j.error : '');
    if (j.ok) { out.textContent += `\\n\\nReloading in ${delay / 1000} s...`; setTimeout(() => location.reload(), delay); return; }
    out.className = 'err';
  } catch (e) { out.className = 'err'; out.textContent = 'Request failed: ' + e; }
  allButtons().forEach(b => b.disabled = false); refresh(); cardChanged();
}
if (applyBtn) applyBtn.addEventListener('click', () => {
  const names = {}; changed().forEach(i => names[i.dataset.index] = i.value.trim());
  post('api/apply', {names});
});
document.querySelectorAll('select.cprof').forEach(sel => {
  const card = sel.closest('.card'), setBtn = card.querySelector('button.cset'), box = card.querySelector('.cconfirm');
  const sync = () => { setBtn.disabled = sel.value === sel.dataset.active; if (setBtn.disabled) box.hidden = true; };
  sel.addEventListener('change', sync);
  setBtn.addEventListener('click', () => {
    const aff = JSON.parse(sel.dataset.affected), prof = sel.selectedOptions[0].textContent;
    box.querySelector('.ctext').textContent = aff.length
      ? `Switching to ${prof} restarts this card's zones: ${aff.join(', ')}. Zones whose channel pair the new profile lacks stop until you switch back.`
      : `Switch to ${prof}? No remap sinks use this card.`;
    box.hidden = false;
  });
  box.querySelector('button.cno').addEventListener('click', () => { sel.value = sel.dataset.active; sync(); });
  box.querySelector('button.cgo').addEventListener('click', () =>
    post('api/profile', {card: sel.dataset.card, profile: sel.value}, 'cresult', 6000));
  sync();
});
document.querySelectorAll('button.zedit-btn').forEach(b => b.addEventListener('click', () => {
  const row = document.getElementById(b.dataset.target); row.hidden = !row.hidden;
  if (!row.hidden) row.querySelector('input').focus();
}));
document.querySelectorAll('button.zcancel').forEach(b => b.addEventListener('click', () => {
  const row = b.closest('tr'); row.querySelectorAll('input').forEach(i => i.value = i.defaultValue); row.hidden = true;
}));
document.querySelectorAll('button.zsave').forEach(b => b.addEventListener('click', () => {
  const f = b.closest('form');
  const v = n => f.elements[n].disabled ? '' : f.elements[n].value;
  post('api/zone', {key: b.dataset.key, name: v('name'), offset_ms: v('offset_ms'),
                    volume_max_db: v('volume_max_db')}, 'zresult', 8000);
}));
const kName = document.getElementById('k_name'), kCreate = document.getElementById('kcreate'),
      kWarn = document.getElementById('kwarn');
const kChecked = () => [...document.querySelectorAll('input.kmem:checked')];
function kSync() {
  if (!kCreate) return;
  const sel = kChecked(), cards = new Set(sel.filter(c => c.dataset.kind === 'card').map(c => c.value));
  const dup = sel.filter(c => c.dataset.kind === 'remap' && cards.has(c.dataset.master)).map(c => c.value);
  kWarn.hidden = !dup.length;
  kWarn.textContent = dup.length ? `${dup.join(', ')} ${dup.length > 1 ? 'are' : 'is'} on a card whose whole output is also picked: those speakers would play everything twice.` : '';
  kCreate.disabled = sel.length < 2 || !/^[A-Za-z0-9_.-]+$/.test(kName.value.trim());
}
document.querySelectorAll('input.kmem').forEach(c => c.addEventListener('change', kSync));
if (kName) { kName.addEventListener('input', kSync); kSync();
  kCreate.addEventListener('click', () => post('api/combine/create', {name: kName.value.trim(),
    description: document.getElementById('k_desc').value, sinks: kChecked().map(c => c.value)}, 'kresult')); }
const btBtn = document.getElementById('btreset');
if (btBtn) btBtn.addEventListener('click', () => post('api/bt_reset', {}, 'btresult', 10000));
document.querySelectorAll('button.zswitch').forEach(b => b.addEventListener('click', () =>
  post('api/zone/enabled', {key: b.dataset.key, enabled: b.dataset.on === '1'}, 'zresult', 8000)));
document.querySelectorAll('button.cdef').forEach(b => b.addEventListener('click', () =>
  post('api/create_defaults', {master: b.dataset.master}, 'cresult')));
document.querySelectorAll('button.kremove').forEach(b => b.addEventListener('click', () => {
  if (b.dataset.armed) { post('api/combine/remove', {index: Number(b.dataset.index)}, 'kresult'); return; }
  b.dataset.armed = '1'; b.textContent = 'Click again to remove';
  setTimeout(() => { delete b.dataset.armed; b.textContent = 'Remove'; }, 4000);
}));
document.querySelectorAll('button.kadopt').forEach(b => b.addEventListener('click', () =>
  post('api/combine/adopt', {name: b.dataset.name}, 'kresult')));
document.querySelectorAll('button.adopt').forEach(b => b.addEventListener('click', () =>
  post('api/adopt', {name: b.dataset.name})));
const cardSel = document.getElementById('cc_card'), pairSel = document.getElementById('cc_pair'),
      preview = document.getElementById('cc_preview'), createBtn = document.getElementById('create');
function cardChanged() {
  if (!cardSel) return;
  const free = JSON.parse(cardSel.selectedOptions[0].dataset.free);
  const keep = pairSel.value;
  pairSel.innerHTML = '';
  Object.entries(free).forEach(([p, v]) => { const o = new Option(v.label, p); o.dataset.name = v.name || ''; pairSel.add(o); });
  if ([...pairSel.options].some(o => o.value === keep)) pairSel.value = keep;
  pairChanged();
}
const custom = document.getElementById('cc_custom'),
      leftSel = document.getElementById('cc_left'), rightSel = document.getElementById('cc_right');
const abbr = c => (custom ? JSON.parse(custom.dataset.abbr)[c] : '') || c.replace(/[^a-z0-9]/g, '');
function pairChanged() {
  const o = pairSel.selectedOptions[0];
  const isCustom = !!o && o.value === 'custom';
  custom.hidden = !isCustom; custom.style.display = isCustom ? 'flex' : 'none';
  if (isCustom) {
    const card = cardSel.selectedOptions[0], chans = JSON.parse(card.dataset.chans);
    [leftSel, rightSel].forEach((s, i) => { const keep = s.value; s.innerHTML = '';
      chans.forEach(c => s.add(new Option(`${c} (${abbr(c).toUpperCase()})`, c)));
      s.value = chans.includes(keep) ? keep : chans[Math.min(i, chans.length - 1)]; });
    const base = card.dataset.base;
    preview.textContent = base ? `${base}_${abbr(leftSel.value)}_${abbr(rightSel.value)}` : '(no system name)';
    createBtn.disabled = !base;
    return;
  }
  preview.textContent = o ? (o.dataset.name || '(no system name)') : '';
  createBtn.disabled = !o || !o.dataset.name;
}
if (leftSel) { leftSel.addEventListener('change', pairChanged); rightSel.addEventListener('change', pairChanged); }
if (cardSel) { cardSel.addEventListener('change', cardChanged); pairSel.addEventListener('change', pairChanged);
  createBtn.addEventListener('click', () => post('api/create', {master: cardSel.value, channels: pairSel.value,
    left: pairSel.value === 'custom' ? leftSel.value : '', right: pairSel.value === 'custom' ? rightSel.value : ''}));
  cardChanged(); }
refresh();
document.querySelectorAll('button.copy').forEach(b => b.addEventListener('click', () => {
  const t = b.dataset.text;
  (navigator.clipboard ? navigator.clipboard.writeText(t) : Promise.reject())
    .catch(() => { const r = document.createRange(); r.selectNode(b.previousElementSibling);
                   getSelection().removeAllRanges(); getSelection().addRange(r); });
}));
"""


def render(st: dict) -> str:
    e = html.escape
    by_sink = {c["name"]: c for c in st["cards"]}

    def output_html(o):
        """One card output: sink name, pairs in use / free, free channels, and
        the Create default sinks button."""
        fc = ", ".join(rs.channel_abbr(c).upper() for c in o["free_channels"])
        return (f'<div class="mono" style="margin-top:4px">{e(o["name"])}</div>'
                f'<div>In use: {e(", ".join(PAIR_LABEL[p] for p in o["used"]) or "none")}'
                f' — Free: {e(", ".join(PAIR_LABEL[p] for p in o["free"]) or "none")}</div>'
                f'<div class="sub" style="font-size:12px">Free channels: {e(fc or "none")}</div>'
                + (f'<button class="btn sec sm cdef" type="button" data-master="{e(o["name"])}" '
                   f'style="margin:6px 0 0;align-self:flex-start">Create default sinks '
                   f'({e(", ".join(PAIR_LABEL[p] for p in o["free"]))})</button>' if o["free"] else ""))
    affected = {}
    for pc in st["pa_cards"]:
        pre = card_sink_prefix(pc["name"])
        affected[pc["name"]] = [r["name"] + ("" if r["managed"] else " (unmanaged: will be lost)")
                                for r in st["remap_sinks"] if r["master"].startswith(pre)]
    shown = set()
    card_html = []
    for i, pc in enumerate(st["pa_cards"]):
        opts = "".join(
            f'<option value="{e(p["name"])}"{" selected" if p["name"] == pc["active"] else ""}>'
            f'{e(p["description"])}{"" if p["available"] else " (unplugged)"}</option>'
            for p in pc["profiles"])
        outs = []
        for sink in pc["outputs"]:
            shown.add(sink)
            outs.append(output_html(by_sink[sink]))
        card_html.append(
            f'<div class="card"><div style="font-weight:600">{e(pc["description"] or pc["name"])}</div>'
            f'<div class="mono">{e(pc["name"])}</div>'
            f'<label for="cp_{i}" class="sub" style="font-size:12px;margin-top:4px">Profile</label>'
            f'<div style="display:flex;gap:8px"><select id="cp_{i}" class="cprof" data-card="{e(pc["name"])}" '
            f'data-active="{e(pc["active"])}" data-affected="{e(json.dumps(affected.get(pc["name"], [])))}" '
            f'style="flex:1;min-width:0;min-height:40px;padding:0 10px;border-radius:8px;'
            f'border:1px solid #353b46;background:#111318;color:#e6e8eb">{opts}</select>'
            f'<button class="btn sec sm cset" type="button" data-for="cp_{i}" disabled style="margin:0">Set</button></div>'
            f'<div class="cconfirm" id="cc_{i}" hidden style="border:1px solid #5a4a2a;border-radius:8px;padding:10px;'
            f'background:#221d14;font-size:13px"><div class="ctext"></div>'
            f'<div style="display:flex;gap:8px;margin-top:8px"><button class="btn pri sm cgo" type="button" '
            f'data-for="cp_{i}" style="margin:0">Switch profile</button><button class="btn sec sm cno" '
            f'type="button" style="margin:0">Cancel</button></div></div>'
            + ("".join(outs) or '<div class="sub">No output with this profile</div>') + '</div>')
    # Sound-card sinks no PulseAudio card claims (unusual) still get shown.
    for c in st["cards"]:
        if c["name"] not in shown:
            card_html.append(f'<div class="card"><div style="font-weight:600">{e(c["description"])}</div>'
                             + output_html(c) + '</div>')
    cards = "".join(card_html)

    rows = []
    for r in st["remap_sinks"]:
        sysn = (f'<span class="mono">{e(r["system_name"])}</span>'
                f'<button class="copy" aria-label="Copy system name" data-text="{e(r["system_name"])}">'
                f'{COPY_ICON}</button>') if r["system_name"] else '<span class="mono">—</span>'
        if not r["loaded"]:
            st_txt = '<span class="warn">Configured, not loaded (see log)</span>'
        else:
            st_txt = ("Zone running" if r["zone_running"] else "No zone running") + \
                     ("" if r["managed"] else " · unmanaged")
            if not r["managed"]:
                st_txt += (f'<button class="btn sec sm adopt" data-name="{e(r["name"])}">Adopt</button>'
                           if r.get("adoptable") else
                           f'<br><span class="sub" style="font-size:12px">Can\'t adopt: {e(r.get("adopt_note", ""))}</span>')
        if r["index"] is not None:
            name_cell = (f'<td><input class="nm" data-index="{r["index"]}" value="{e(r["name"])}" '
                         f'aria-label="Sink name for {e(r["system_name"] or r["name"])}"></td>')
        else:
            name_cell = f'<td class="name">{e(r["name"] or "?")}</td>'
        rows.append(f'<tr>{name_cell}<td class="sys">{sysn}</td>'
                    f'<td>{e(r["master_description"] or r["master"])}</td>'
                    f'<td>{e(r["channels"])}</td><td>{st_txt}</td></tr>')

    def ago(s):
        if s is None:
            return "?"
        return f"{s // 86400}d {s % 86400 // 3600}h" if s >= 86400 else \
               f"{s // 3600}h {s % 3600 // 60}m" if s >= 3600 else f"{s // 60}m {s % 60}s"

    zrows = []
    for z in st["zones"]:
        if not z.get("enabled", True):
            status = "Off"
        else:
            status = ("Playing" if z["playing"] else "Idle") if z["playing"] is not None else "Running"
        ov = z["overrides"]
        extras = []
        if "offset_s" in ov:
            try:
                extras.append(f'offset {round(float(ov["offset_s"]) * 1000):+d} ms')
            except ValueError:
                extras.append(f'offset {ov["offset_s"]} s')
        if "volume_max_db" in ov:
            extras.append(f'max {ov["volume_max_db"]} dB')
        if "name" in ov:
            extras.append("custom name")
        kind = {"sink": "Sound card", "bluetooth": "Bluetooth", "cast": "Cast Bridge"}[z["kind"]]
        rid = f"ze_{len(zrows)}"
        edit_btn = (f'<button class="btn sec sm zedit-btn" data-target="{rid}">Edit</button>'
                    if z["editable"] else "")
        if z["kind"] != "cast":
            on = z.get("enabled", True)
            edit_btn += (f'<button class="btn sec sm zswitch" data-key="{e(z["key"])}" '
                         f'data-on="{"0" if on else "1"}">{"Turn off" if on else "Turn on"}</button>')
        up = f' · up {e(ago(z["uptime_s"]))}' if z.get("enabled", True) else ""
        zrows.append(f'<tr{"" if z.get("enabled", True) else " style=\"opacity:.6\""}>'
                     f'<td style="font-weight:600;color:#e6e8eb">{e(z["name"])}</td>'
                     f'<td>{e(kind)}</td><td class="sys"><span class="mono">{e(z["target"])}</span></td>'
                     f'<td>{e(z["port"] or "—")}</td><td>{e(", ".join(extras) or "—")}</td>'
                     f'<td>{e(status)}{up}{edit_btn}</td></tr>')
        if z["editable"]:
            mo = z["overrides"]   # the values in effect, set here or by hand

            def field(fid, fname, label, short, value, mode=""):
                return (f'<label for="{fid}">{label}</label><input id="{fid}" name="{fname}"{mode} '
                        f'value="{e(value)}" placeholder="{e(z["name"]) if fname == "name" else "0"}">')
            try:
                off = str(round(float(mo["offset_s"]) * 1000)) if "offset_s" in mo else ""
            except ValueError:
                off = ""
            zrows.append(
                f'<tr class="zedit" id="{rid}" hidden><td colspan="6"><form class="zform" onsubmit="return false">'
                f'<div style="flex:2 1 240px">{field(rid + "_n", "name", "AirPlay name (empty = default)", "name", mo.get("name", ""))}</div>'
                f'<div style="flex:1 1 140px">{field(rid + "_o", "offset_ms", "Latency offset (ms)", "offset_s", off, " inputmode=numeric")}</div>'
                f'<div style="flex:1 1 140px">{field(rid + "_v", "volume_max_db", "Max volume (dB)", "volume_max_db", mo.get("volume_max_db", ""), " inputmode=decimal")}</div>'
                f'<button class="btn pri zsave" type="button" data-key="{e(z["key"])}">Save &amp; restart zone</button>'
                f'<button class="btn sec zcancel" type="button">Cancel</button></form>'
                f'<p class="sub" style="margin:10px 0 0;font-size:12px">Changing the name makes Music Assistant see a new player.</p>'
                f'</td></tr>')
    zones_html = (f'<div class="box"><table><thead><tr><th>Zone</th><th>Type</th><th>Plays to</th>'
                  f'<th>Port</th><th>Overrides</th><th>Status</th></tr></thead>'
                  f'<tbody>{"".join(zrows)}</tbody></table></div><pre id="zresult" hidden></pre>') if zrows else \
                 '<p class="sub" style="margin-top:14px">No zones running.</p>'

    creatable = [c for c in st["cards"] if c["free"] or c["free_channels"]]
    if creatable:
        def free_opts(c):
            d = {p: {"label": PAIR_LABEL[p], "name": c["free_names"].get(p)} for p in c["free"]}
            if c["free_channels"]:
                d["custom"] = {"label": "Custom…", "name": ""}
            return d
        opts = "".join(
            f'<option value="{e(c["name"])}" data-free="' + e(json.dumps(free_opts(c)))
            + f'" data-chans="{e(json.dumps(c["free_channels"]))}" data-base="{e(c["name_base"])}">'
            f'{e(c["description"] or c["name"])}</option>' for c in creatable)
        create_form = f"""<form class="create" onsubmit="return false">
<div style="flex:1 1 260px"><label for="cc_card">Card</label><select id="cc_card" style="width:100%">{opts}</select></div>
<div style="flex:1 1 180px"><label for="cc_pair">Channels (free only)</label><select id="cc_pair" style="width:100%"></select></div>
<div id="cc_custom" hidden style="flex:2 1 300px;display:none;gap:12px" data-abbr="{e(json.dumps(rs.CHANNEL_ABBR))}">
<div style="flex:1"><label for="cc_left">Left</label><select id="cc_left" style="width:100%"></select></div>
<div style="flex:1"><label for="cc_right">Right</label><select id="cc_right" style="width:100%"></select></div></div>
<div style="flex:2 1 320px"><span class="lbl">Will be created as</span><span class="preview" id="cc_preview"></span></div>
<button class="btn pri" id="create" type="button">Create sink</button></form>"""
    else:
        create_form = '<p class="sub" style="margin-top:14px">No free channels left on any sound card.</p>'

    # Zones are known by their sink name; other sinks by their description.
    label = {c["name"]: (c["name"] if c["kind"] == "remap" else c["description"] or c["name"])
             for c in st["sink_choices"]}
    krows = []
    for c in st["combine_sinks"]:
        mem = ", ".join(e(label.get(m, m)) + (' <span class="warn">(missing)</span>' if m in c["missing"] else "")
                        for m in c["members"])
        if not c["loaded"]:
            kst = '<span class="warn">Not loaded (a member is missing; see log)</span>'
        else:
            kst = "Zone running" if c["zone_running"] else "No zone running"
        if c["managed"]:
            act = f'<button class="btn sec sm kremove" data-index="{c["index"]}">Remove</button>'
        elif c.get("adoptable"):
            kst += " · unmanaged"
            act = f'<button class="btn sec sm kadopt" data-name="{e(c["name"])}">Adopt</button>'
        else:
            kst += " · unmanaged"
            act = ""
        krows.append(f'<tr><td class="name">{e(c["name"])}<div class="sub" style="font-family:system-ui;font-size:12px">'
                     f'{e(c["description"])}</div></td><td>{mem}</td><td>{kst}{act}</td></tr>')
    ktable = (f'<div class="box"><table><thead><tr><th>Name</th><th>Members</th><th>State</th></tr></thead>'
              f'<tbody>{"".join(krows)}</tbody></table></div>') if krows else \
             '<p class="sub" style="margin-top:14px">No combine sinks yet.</p>'
    groups = []
    for kind, title in (("remap", "Zones (remap sinks)"), ("card", "Sound card outputs"),
                        ("bluetooth", "Bluetooth"), ("other", "Other sinks")):
        items = [c for c in st["sink_choices"] if c["kind"] == kind]
        if items:
            groups.append(f'<div class="kgroup"><h3>{title}</h3>' + "".join(
                f'<label><input type="checkbox" class="kmem" value="{e(c["name"])}" data-kind="{kind}" '
                f'data-master="{e(c["master"])}"><span>{e(label[c["name"]])}'
                + (f'<span class="mono" style="display:block">{e(c["name"])}</span>' if kind != "remap" else "")
                + '</span></label>'
                for c in items) + '</div>')
    combine_html = f"""{ktable}<pre id="kresult" hidden></pre>
<form class="create" onsubmit="return false" style="flex-direction:column;flex-wrap:nowrap;align-items:stretch">
<div style="display:flex;flex-wrap:wrap;gap:12px">
<div style="flex:1 1 260px"><label for="k_name">Name</label><input id="k_name" class="nm" style="min-width:0" placeholder="e.g. Outdoor"></div>
<div style="flex:2 1 320px"><label for="k_desc">Description (optional)</label><input id="k_desc" class="nm" style="min-width:0;font-family:system-ui" placeholder="e.g. Back Yard + Workshop"></div></div>
<div><span class="lbl">Sinks to combine (two or more)</span><div style="display:flex;flex-wrap:wrap;gap:20px">{''.join(groups)}</div></div>
<div id="kwarn" hidden></div>
<div><button class="btn pri" id="kcreate" type="button" disabled>Create combine sink</button></div></form>"""

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AirPlay 2 Multiroom Audio</title><style>{CSS}</style></head><body>
<header><b>AirPlay 2 Multiroom Audio</b></header>
<main>
<section><h1>Zones</h1><p class="sub">Every running AirPlay 2 receiver.</p>{zones_html}</section>
<section><h2>Bluetooth</h2><p class="sub">Power-cycle the Bluetooth adapter, for when Bluetooth speakers stay connected but stop playing. Their zones drop for a few seconds and reconnect.</p>
<button class="btn sec" id="btreset" type="button">Reset Bluetooth adapter</button><pre id="btresult" hidden></pre></section>
<section><h2>Sound cards</h2><p class="sub">Free pairs can become new zones. Changing a profile restarts that card's zones.</p>
<div class="cards">{cards or '<p class="sub">No sound cards found.</p>'}</div><pre id="cresult" hidden></pre></section>
<section><h2>Remap sinks</h2><p class="sub">Rename a managed sink and Apply. The system name stays as a reference.</p>
<div class="box"><table><thead><tr><th>Sink name</th><th>System name</th><th>Card</th>
<th>Channels</th><th>State</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>
<div class="bar"><p class="sub" style="margin:0">Renaming a sink renames its zone, so Music Assistant sees it as a new player.</p>
<div style="display:flex;gap:10px"><button class="btn sec" id="discard" disabled>Discard changes</button>
<button class="btn pri" id="apply" disabled>Apply</button></div></div>
<pre id="result" hidden></pre>{create_form}</section>
<section><h2>Combine sinks</h2><p class="sub">One AirPlay zone that plays to several sinks at once — handy for senders that can't group zones. Members on different sound cards are kept in sync by resampling.</p>
{combine_html}</section>
</main><script>{SCRIPT}</script></body></html>"""


# -- server ---------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: str, ctype: str):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        ip = self.client_address[0]
        if ip not in ALLOWED:
            log.warning("refused %s from %s", self.path, ip)
            self._send(403, "Forbidden\n", "text/plain")
            return
        # Ingress forwards the path after its own prefix, so "/" and "" are
        # the page; every link the page uses is relative.
        path = self.path.split("?", 1)[0].rstrip("/")
        try:
            if path in ("", "/index.html"):
                self._send(200, render(state()), "text/html; charset=utf-8")
            elif path == "/api/debug/cards":
                self._send(200, pactl_text("list", "cards") or "(pactl list cards returned nothing)\n",
                           "text/plain; charset=utf-8")
            elif path == "/api/state":
                self._send(200, json.dumps(state(), indent=2), "application/json")
            else:
                self._send(404, "Not found\n", "text/plain")
        except Exception:  # keep serving; the traceback goes to the log
            log.exception("error serving %s", self.path)
            self._send(500, "Internal error, see web_panel.log\n", "text/plain")

    def do_POST(self):
        ip = self.client_address[0]
        if ip not in ALLOWED:
            log.warning("refused POST %s from %s", self.path, ip)
            self._send(403, "Forbidden\n", "text/plain")
            return
        path = self.path.split("?", 1)[0].rstrip("/")
        if path not in ("/api/apply", "/api/create", "/api/adopt", "/api/zone", "/api/profile",
                        "/api/combine/create", "/api/combine/remove", "/api/combine/adopt",
                        "/api/create_defaults", "/api/bt_reset", "/api/zone/enabled"):
            self._send(404, "Not found\n", "text/plain")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(min(length, 65536)) or b"{}")
            if not isinstance(body, dict):
                raise ApplyError("bad request")
            log.info("%s requested: %s", path, body)
            if path == "/api/apply":
                names = body.get("names") or {}
                if not isinstance(names, dict):
                    raise ApplyError("bad request")
                lines = apply_names(names)
            elif path == "/api/create":
                lines = create_sink(str(body.get("master", "")), str(body.get("channels", "")),
                                    str(body.get("left", "") or ""), str(body.get("right", "") or ""))
            elif path == "/api/zone/enabled":
                lines = set_zone_enabled(str(body.get("key", "")), bool(body.get("enabled")))
            elif path == "/api/create_defaults":
                lines = create_defaults(str(body.get("master", "")))
            elif path == "/api/combine/create":
                sinks_ = body.get("sinks") or []
                if not isinstance(sinks_, list):
                    raise ApplyError("bad request")
                lines = create_combine(str(body.get("name", "")), str(body.get("description", "")), sinks_)
            elif path == "/api/combine/remove":
                lines = remove_combine(body.get("index"))
            elif path == "/api/combine/adopt":
                lines = adopt_combine(str(body.get("name", "")))
            elif path == "/api/profile":
                lines = set_profile(str(body.get("card", "")), str(body.get("profile", "")))
            elif path == "/api/zone":
                lines = save_zone(str(body.get("key", "")), str(body.get("name", "")),
                                  str(body.get("offset_ms", "")), str(body.get("volume_max_db", "")))
            elif path == "/api/bt_reset":
                try:
                    lines = bt_reset.reset()
                except RuntimeError as e:
                    raise ApplyError(str(e))
            else:
                lines = adopt_sink(str(body.get("name", "")))
            for l in lines:
                log.info("apply: %s", l)
            result = {"ok": True, "lines": lines}
        except ApplyError as e:
            log.warning("apply failed: %s", e)
            result = {"ok": False, "lines": [], "error": str(e)}
        except Exception as e:  # keep serving; the traceback goes to the log
            log.exception("apply crashed")
            result = {"ok": False, "lines": [], "error": f"internal error: {e}"}
        self._send(200, json.dumps(result), "application/json")

    def log_message(self, fmt, *args):
        log.info("%s %s", self.client_address[0], fmt % args)


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    log.info("listening on port %d, allowing %s", PORT, ", ".join(sorted(ALLOWED)))
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
