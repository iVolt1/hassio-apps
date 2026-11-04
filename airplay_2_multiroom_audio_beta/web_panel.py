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
  POST /api/apply   {"names": {"<option index>": "<new name>"}}: saves the
                    remap_sinks option through the Supervisor (so the
                    Configuration tab matches), then runs remap_sinks.py --load
                    and returns its log lines
  POST /api/create  {"master": ..., "channels": ...}: adds a sink on a free
                    pair, named with its system name, then the same as apply
  POST /api/adopt   {"name": ...}: adds an unmanaged sink to the options
                    under its current name, then the same as apply
  POST /api/zone    {"key", "name", "offset_ms", "volume_max_db"}: writes
                    those (empty = not set) to a managed block in the zone's
                    <key>-ap2.custom file and restarts that zone's
                    shairport-sync; sink_zone_manager starts it again at once

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
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import remap_sinks as rs

PORT = int(os.environ.get("WEB_PANEL_PORT", "8285"))
ALLOWED = set(os.environ.get("WEB_PANEL_ALLOWED", "172.30.32.2,127.0.0.1,::1").split(","))
OPTIONS_FILE = Path(os.environ.get("RS_OPTIONS_FILE", "/data/options.json"))
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


def pair_of(args: str):
    """The channels choice a remap sink's arguments correspond to, or None."""
    _, chans = rs.claim(args)
    for pair, needs in PAIR_NEEDS.items():
        if chans == needs:
            return pair
    return None


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
        name = str(entry.get("name", "") or "").strip() or rs.system_name(master, pair, descs)
        if name:
            configured[name] = {"master": master, "pair": pair, "index": index}

    remaps, used = [], {}
    for args in loaded:
        name = rs.sink_name_of(args)
        master = rs.arg_of(args, "master") or ""
        pair = pair_of(args)
        used.setdefault(master, set()).update(rs.claim(args)[1])
        remaps.append({
            "name": name,
            "system_name": rs.system_name(master, pair, descs) if pair else None,
            "master": master,
            "master_description": descs.get(master, ""),
            "pair": pair,
            "channels": PAIR_LABEL.get(pair, rs.arg_of(args, "master_channel_map") or "?"),
            "managed": name in configured,
            "index": configured[name]["index"] if name in configured else None,
            "loaded": True,
            "zone_running": name in running,
        })
    for name, c in configured.items():
        if not any(r["name"] == name for r in remaps):
            remaps.append({
                "name": name,
                "system_name": rs.system_name(c["master"], c["pair"], descs),
                "master": c["master"], "master_description": descs.get(c["master"], ""),
                "pair": c["pair"], "channels": PAIR_LABEL.get(c["pair"], c["pair"]),
                "managed": True, "index": c["index"], "loaded": False, "zone_running": False,
            })
            if c["pair"] in PAIR_NEEDS:
                used.setdefault(c["master"], set()).update(PAIR_NEEDS[c["pair"]])

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
        pairs = [p for p, needs in PAIR_NEEDS.items() if needs <= chans]
        if not pairs and chans:
            pairs = ["stereo"]
        cards.append({
            "name": s["name"], "description": s.get("description", ""),
            "used": [p for p in pairs if (PAIR_NEEDS.get(p) or chans) & taken],
            "free": [p for p in pairs if not ((PAIR_NEEDS.get(p) or chans) & taken)],
        })
        cards[-1]["free_names"] = {p: rs.system_name(s["name"], p, descs) for p in cards[-1]["free"]}
    return {"zones": zones(), "cards": cards, "remap_sinks": remaps}


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


def change_and_apply(change) -> list:
    """Reads the options, lets change(entries, descs) edit the remap_sinks
    list in place and return what it did, then saves and applies. Returns the
    log lines to show."""
    try:
        options = json.loads(OPTIONS_FILE.read_text())
    except (OSError, ValueError) as e:
        raise ApplyError(f"cannot read {OPTIONS_FILE}: {e}")
    entries = options.setdefault("remap_sinks", [])
    descs = {s["name"]: s.get("description", "") for s in list_sinks() if "name" in s}

    lines = change(entries, descs)
    if not lines:
        return ["nothing changed"]

    resolved = [e.get("name") or rs.system_name(e.get("master", ""), e.get("channels", ""), descs)
                for e in entries]
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
        old = entry.get("name") or rs.system_name(entry.get("master", ""),
                                                   entry.get("channels", ""), descs)
        if new != old:
            entry["name"] = new
            lines.append(f"rename {old} -> {new}")
    return lines


def create_sink(master: str, pair: str) -> list:
    """Adds a sink on a free pair of a card, named with its system name."""
    card = next((c for c in state()["cards"] if c["name"] == master), None)
    if not card:
        raise ApplyError(f"no sound card {master}; reload the page")
    if pair not in card["free"]:
        raise ApplyError(f"{PAIR_LABEL.get(pair, pair)} of {card['description']} is not free")
    name = card["free_names"].get(pair)
    if not name:
        raise ApplyError("cannot make a system name for this card; add it in the Configuration tab")

    def change(entries, descs):
        entries.append({"name": name, "master": master, "channels": pair})
        return [f"create {name} ({card['description']}, {PAIR_LABEL[pair]})"]
    return change_and_apply(change)


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
    rest = strip_block(old)
    hand = [k for k in settings if conf_value(rest, k) is not None]
    if hand:
        raise ApplyError(f"{', '.join(hand)} is already set by hand in {path}; "
                         "remove it there first (libconfig rejects a setting set twice)")
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
pre#result,pre#zresult{margin:14px 0 0;border:1px solid #2a2f38;border-radius:12px;background:#0d0f13;padding:14px 16px;font:12px/1.7 ui-monospace,monospace;color:#b8c0ca;white-space:pre-wrap}
pre#result.err,pre#zresult.err{color:#f0b26a}
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
function pairChanged() {
  const o = pairSel.selectedOptions[0];
  preview.textContent = o ? (o.dataset.name || '(no system name)') : '';
  createBtn.disabled = !o || !o.dataset.name;
}
if (cardSel) { cardSel.addEventListener('change', cardChanged); pairSel.addEventListener('change', pairChanged);
  createBtn.addEventListener('click', () => post('api/create', {master: cardSel.value, channels: pairSel.value}));
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
    cards = "".join(
        f'<div class="card"><div style="font-weight:600">{e(c["description"])}</div>'
        f'<div class="mono">{e(c["name"])}</div>'
        f'<div>In use: {e(", ".join(PAIR_LABEL[p] for p in c["used"]) or "none")}'
        f' — Free: {e(", ".join(PAIR_LABEL[p] for p in c["free"]) or "none")}</div></div>'
        for c in st["cards"])

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
        zrows.append(f'<tr><td style="font-weight:600;color:#e6e8eb">{e(z["name"])}</td>'
                     f'<td>{e(kind)}</td><td class="sys"><span class="mono">{e(z["target"])}</span></td>'
                     f'<td>{e(z["port"] or "?")}</td><td>{e(", ".join(extras) or "—")}</td>'
                     f'<td>{e(status)} · up {e(ago(z["uptime_s"]))}{edit_btn}</td></tr>')
        if z["editable"]:
            mo, ho = z["managed_overrides"], z["hand_overrides"]

            def field(fid, fname, label, short, value, mode=""):
                if short in ho:
                    return (f'<label for="{fid}">{label}</label><input id="{fid}" name="{fname}" disabled '
                            f'value="" placeholder="set by hand: {e(ho[short])}" title="Set by hand in the zone\'s .custom file; edit or remove it there">')
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

    creatable = [c for c in st["cards"] if c["free"]]
    if creatable:
        opts = "".join(
            f'<option value="{e(c["name"])}" data-free="'
            + e(json.dumps({p: {"label": PAIR_LABEL[p], "name": c["free_names"].get(p)} for p in c["free"]}))
            + f'">{e(c["description"] or c["name"])}</option>' for c in creatable)
        create_form = f"""<form class="create" onsubmit="return false">
<div style="flex:1 1 260px"><label for="cc_card">Card</label><select id="cc_card" style="width:100%">{opts}</select></div>
<div style="flex:1 1 180px"><label for="cc_pair">Channels (free pairs only)</label><select id="cc_pair" style="width:100%"></select></div>
<div style="flex:2 1 320px"><span class="lbl">Will be created as</span><span class="preview" id="cc_preview"></span></div>
<button class="btn pri" id="create" type="button">Create sink</button></form>"""
    else:
        create_form = '<p class="sub" style="margin-top:14px">No free channel pairs left on any sound card.</p>'

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AirPlay 2 Multiroom Audio</title><style>{CSS}</style></head><body>
<header><b>AirPlay 2 Multiroom Audio</b></header>
<main>
<section><h1>Zones</h1><p class="sub">Every running AirPlay 2 receiver.</p>{zones_html}</section>
<section><h2>Sound cards</h2><p class="sub">Free pairs can become new zones.</p>
<div class="cards">{cards or '<p class="sub">No sound cards found.</p>'}</div></section>
<section><h2>Remap sinks</h2><p class="sub">Rename a managed sink and Apply. The system name stays as a reference.</p>
<div class="box"><table><thead><tr><th>Sink name</th><th>System name</th><th>Card</th>
<th>Channels</th><th>State</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>
<div class="bar"><p class="sub" style="margin:0">Renaming a sink renames its zone, so Music Assistant sees it as a new player.</p>
<div style="display:flex;gap:10px"><button class="btn sec" id="discard" disabled>Discard changes</button>
<button class="btn pri" id="apply" disabled>Apply</button></div></div>
<pre id="result" hidden></pre>{create_form}</section>
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
        if path not in ("/api/apply", "/api/create", "/api/adopt", "/api/zone"):
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
                lines = create_sink(str(body.get("master", "")), str(body.get("channels", "")))
            elif path == "/api/zone":
                lines = save_zone(str(body.get("key", "")), str(body.get("name", "")),
                                  str(body.get("offset_ms", "")), str(body.get("volume_max_db", "")))
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
