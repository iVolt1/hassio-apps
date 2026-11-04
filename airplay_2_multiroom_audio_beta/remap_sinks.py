#!/usr/bin/env python3
"""
remap_sinks.py -- turns the add-on's `remap_sinks` option into PulseAudio
`load-module module-remap-sink ...` lines and writes them to custom.pa in
the add-on's config folder (/config inside the container,
/addon_configs/<slug>/ on the host).

That file uses the same syntax as the audio plugin's own /data/custom.pa,
which hassio_audio includes at startup. Copy the managed block there and
the sinks exist at boot even without this add-on.

It only ever unloads or reloads sinks it manages: ones in the options, and
ones it loaded itself earlier (recorded in remap_sinks_state.json next to
custom.pa). Sinks made by anything else are left alone. It:
  * rewrites only the block between the BEGIN/END markers, leaving any
    other text in custom.pa alone
  * lists remap sinks that are loaded right now but are not in the
    options (for example ones made by the Multi-room Audio Controller) as
    comments, so they can be copied into the options
  * logs a diff of what changed
  * skips (and comments out) a configured sink whose master channels are
    already used by another sink on the same master, configured or not
  * with --load, makes PulseAudio match the options, using the same lines
    that go into custom.pa: loads configured sinks that are missing, reloads
    ones whose entry changed, and unloads ones it loaded earlier whose entry
    was removed

Usage: remap_sinks.py [--load] [--dry-run]
"""

import difflib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

OPTIONS_FILE = Path(os.environ.get("RS_OPTIONS_FILE", "/data/options.json"))
CUSTOM_PA = Path(os.environ.get("RS_CUSTOM_PA", "/config/custom.pa"))
STATE_FILE = Path(os.environ.get("RS_STATE_FILE",
                                 str(CUSTOM_PA.with_name("remap_sinks_state.json"))))

# sink_name -> module index, filled by loaded_remap_modules()
MODULE_IDX = {}

BEGIN = "# >>> airplay2_multiroom_audio managed >>>"
END = "# <<< airplay2_multiroom_audio managed <<<"

# Which master channels each choice takes. The remap sink itself is always
# a plain stereo sink (front-left, front-right).
PAIRS = {
    "front": "front-left,front-right",
    "rear": "rear-left,rear-right",
    "side": "side-left,side-right",
    "center_lfe": "front-center,lfe",
    "stereo": "front-left,front-right",
}

# Suffix of a sink's system name, the same style Multi-room Audio Controller
# uses: "<master description>_<pair>", e.g. Creative_X-Fi_Analog_Surround_7_1_rl_rr
PAIR_SUFFIX = {
    "front": "fl_fr",
    "rear": "rl_rr",
    "side": "sl_sr",
    "center_lfe": "fc_lfe",
    "stereo": "fl_fr",
}

NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def sink_descriptions() -> dict:
    """sink name -> Description for every sink, or {} if pactl can't be
    queried."""
    try:
        res = subprocess.run(["pactl", "list", "sinks"], capture_output=True,
                             text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    out, name = {}, None
    for line in res.stdout.splitlines() if res.returncode == 0 else []:
        s = line.strip()
        if s.startswith("Name:"):
            name = s[5:].strip()
        elif s.startswith("Description:") and name:
            out[name] = s[12:].strip()
    return out


def system_name(master: str, pair: str, descs: dict):
    """Default sink name: the master's description, cleaned up, plus the
    pair. None if the master isn't known to PulseAudio."""
    desc = descs.get(master)
    if not desc or pair not in PAIR_SUFFIX:
        return None
    base = re.sub(r"[^A-Za-z0-9]+", "_", desc).strip("_")
    return f"{base}_{PAIR_SUFFIX[pair]}" if base else None


def log(msg: str) -> None:
    print(f"[remap_sinks] {msg}", flush=True)


def load_options() -> list:
    try:
        opts = json.loads(OPTIONS_FILE.read_text())
    except (OSError, ValueError) as e:
        log(f"cannot read {OPTIONS_FILE}: {e}")
        return []
    sinks = opts.get("remap_sinks") or []
    return sinks if isinstance(sinks, list) else []


def module_line(entry: dict, descs: dict = None):
    """The load-module line for one option entry, or None (with a log line
    saying why) if the entry is unusable. An entry without a name gets its
    system name."""
    name = str(entry.get("name", "") or "").strip()
    master = str(entry.get("master", "")).strip()
    pair = str(entry.get("channels", "")).strip()
    desc = str(entry.get("description", "") or "").strip()

    if not name:
        name = system_name(master, pair, descs or {}) or ""
        if not name:
            log(f"skipping unnamed entry on {master or '?'}: master not found in "
                "PulseAudio, so it has no system name; give it a name")
            return None
        log(f"unnamed entry on {master} ({pair}) uses its system name {name}")

    if not NAME_RE.match(name):
        log(f"skipping entry with invalid name {name!r} (letters, digits, _ . - only)")
        return None
    if not master or not NAME_RE.match(master):
        log(f"skipping {name}: invalid or missing master {master!r}")
        return None
    if pair not in PAIRS:
        log(f"skipping {name}: channels must be one of {', '.join(PAIRS)}, got {pair!r}")
        return None

    args = [
        f"sink_name={name}",
        f"master={master}",
        "channels=2",
        f"master_channel_map={PAIRS[pair]}",
        "channel_map=front-left,front-right",
        "remix=no",
    ]
    if desc:
        if '"' in desc or "'" in desc:
            log(f"{name}: quotes are not allowed in description, ignoring it")
        else:
            args.insert(1, f"sink_properties=\"device.description='{desc}'\"")
    return "load-module module-remap-sink " + " ".join(args)


def loaded_remap_modules():
    """[argument string] for every module-remap-sink loaded now, or None if
    pactl can't be queried (not an error: the file is still written)."""
    try:
        res = subprocess.run(["pactl", "list", "modules"], capture_output=True,
                             text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as e:
        log(f"pactl not available ({e}); not listing unmanaged sinks")
        return None
    if res.returncode != 0:
        log(f"pactl list modules exited {res.returncode}; not listing unmanaged sinks")
        return None
    out, is_remap, idx = [], False, None
    MODULE_IDX.clear()
    for line in res.stdout.splitlines():
        if line.startswith("Module #"):
            is_remap, idx = False, line[len("Module #"):].strip()
        elif re.match(r"\s*Name:\s*module-remap-sink\s*$", line):
            is_remap = True
        elif is_remap and re.match(r"\s*Argument:", line):
            args = line.split(":", 1)[1].strip()
            out.append(args)
            MODULE_IDX[sink_name_of(args)] = idx
            is_remap = False
    return out


def sink_name_of(args: str):
    m = re.search(r"(?:^|\s)sink_name=(\S+)", args)
    return m.group(1) if m else None


def arg_of(args: str, key: str):
    m = re.search(r"(?:^|\s)" + key + r"=(\S+)", args)
    return m.group(1) if m else None


def claim(args: str):
    """(master, set of master channels) a remap sink plays on."""
    chans = arg_of(args, "master_channel_map") or arg_of(args, "channel_map") \
        or "front-left,front-right"
    return arg_of(args, "master"), set(chans.split(","))


def old_unmanaged(old: str) -> list:
    """The unmanaged-sink comment lines from the current block, kept as they
    are when pactl can't be queried this time."""
    lines = old.splitlines()
    try:
        b = lines.index(BEGIN)
        e = lines.index(END, b)
    except ValueError:
        return []
    return [l for l in lines[b:e] if l.startswith("# load-module module-remap-sink ")]


def build_block(entries: list, old: str = ""):
    """(block lines, [(name, args)] to load). A configured sink whose master
    channels are already taken is commented out and not returned."""
    lines = [BEGIN,
             "# Generated from the add-on's remap_sinks option. Edits inside this",
             "# block are overwritten; put your own lines outside it."]
    configured = []
    descs = sink_descriptions()
    for entry in entries:
        line = module_line(entry if isinstance(entry, dict) else {}, descs)
        if line:
            name = sink_name_of(line)
            if any(n == name for n, _ in configured):
                log(f"skipping duplicate entry for {name}")
                continue
            configured.append((name, line[len("load-module module-remap-sink "):]))
    names = {n for n, _ in configured}

    loaded = loaded_remap_modules()
    unmanaged = [a for a in (loaded or []) if sink_name_of(a) not in names]

    # Channels already in use: unmanaged sinks first, then configured ones
    # in option order, so the first entry to claim a pair keeps it.
    taken = [(sink_name_of(a), *claim(a)) for a in unmanaged]
    loadable = []
    for name, args in configured:
        master, chans = claim(args)
        clash = next((o for o, m, c in taken if m == master and c & chans), None)
        if clash:
            log(f"NOT using {name}: channels {','.join(sorted(chans))} of {master} "
                f"are already used by {clash}")
            lines.append(f"# SKIPPED, same channels as {clash}: "
                         f"load-module module-remap-sink {args}")
            continue
        taken.append((name, master, chans))
        loadable.append((name, args))
        lines.append(f"load-module module-remap-sink {args}")
    log(f"{len(loadable)} configured remap sink(s) usable: "
        f"{', '.join(n for n, _ in loadable) or 'none'}")

    if loaded is None:
        kept = old_unmanaged(old)
        if kept:
            lines.append("#")
            lines.append("# Loaded now but not in the add-on options (left alone):")
            lines.extend(kept)
            log(f"kept {len(kept)} unmanaged line(s) from the previous run")
    elif unmanaged:
        lines.append("#")
        lines.append("# Loaded now but not in the add-on options (left alone):")
        for a in unmanaged:
            lines.append(f"# load-module module-remap-sink {a}")
        log(f"{len(unmanaged)} unmanaged remap sink(s) loaded: "
            + ", ".join(sink_name_of(a) or "?" for a in unmanaged))
    lines.append(END)
    return lines, loadable, loaded, names


def pactl(*args) -> tuple:
    """(ok, output) for one pactl call."""
    try:
        # One argv element per call argument: pactl joins its arguments with
        # spaces anyway, and this keeps the quoting in sink_properties intact.
        res = subprocess.run(["pactl", *args], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    if res.returncode == 0:
        return True, res.stdout.strip()
    return False, res.stderr.strip() or f"exit {res.returncode}"


def load(name: str, args: str) -> bool:
    ok, out = pactl("load-module", "module-remap-sink", args)
    log(f"loaded {name} (module #{out})" if ok else f"loading {name} failed: {out}")
    return ok


def unload(name: str) -> bool:
    idx = MODULE_IDX.get(name)
    ok, out = pactl("unload-module", idx) if idx else (False, "module not found")
    if not ok:
        log(f"unloading {name} failed: {out}")
    return ok


def same(a: str, b: str) -> bool:
    return a.split() == b.split()


def load_state() -> dict:
    try:
        st = json.loads(STATE_FILE.read_text())
        return st if isinstance(st, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(st: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_name(STATE_FILE.name + ".tmp")
    tmp.write_text(json.dumps(st, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, STATE_FILE)


def apply(loadable: list, loaded, configured: set) -> None:
    """Makes PulseAudio match the options. Runs before the zone manager
    starts, so zones only ever see the final set of sinks."""
    if loaded is None:
        log("pactl not available; not loading or unloading any sinks")
        return
    current = {sink_name_of(a): a for a in loaded}
    state = load_state()

    for name, args in loadable:
        if name not in current:
            if load(name, args):
                state[name] = args
        elif same(current[name], args):
            log(f"{name} already loaded")
            state[name] = args
        else:
            log(f"{name} differs from its option; reloading")
            if unload(name):
                if load(name, args):
                    state[name] = args
                else:
                    log(f"restoring the previous {name}")
                    load(name, current[name])

    # Entries removed from the options. Usable-but-skipped entries are still
    # configured, so they keep their state and their sink.
    for name in [n for n in state if n not in configured]:
        if name not in current:
            log(f"{name} was removed from the options and is not loaded")
        elif same(current[name], state[name]):
            if unload(name):
                log(f"unloaded {name} (removed from the options)")
        else:
            log(f"{name} was removed from the options but is loaded with arguments "
                "this add-on did not set; leaving it")
        del state[name]

    save_state(state)


def splice(old: str, block: list) -> str:
    """Replace the managed block in old, or append it if there is none."""
    lines = old.splitlines()
    try:
        b = lines.index(BEGIN)
        e = lines.index(END, b)
        new = lines[:b] + block + lines[e + 1:]
    except ValueError:
        new = lines + ([""] if lines and lines[-1].strip() else []) + block
    return "\n".join(new) + "\n"


def main() -> int:
    dry_run = "--dry-run" in sys.argv[1:]
    load = "--load" in sys.argv[1:]
    old = CUSTOM_PA.read_text() if CUSTOM_PA.exists() else ""
    block, loadable, loaded, configured = build_block(load_options(), old)
    new = splice(old, block)

    if load and not dry_run:
        apply(loadable, loaded, configured)

    if new == old:
        log(f"{CUSTOM_PA} unchanged")
        return 0
    for d in difflib.unified_diff(old.splitlines(), new.splitlines(),
                                  str(CUSTOM_PA) + " (old)", str(CUSTOM_PA) + " (new)",
                                  lineterm=""):
        log(d)
    if dry_run:
        log("dry run, not writing")
        return 0
    CUSTOM_PA.parent.mkdir(parents=True, exist_ok=True)
    tmp = CUSTOM_PA.with_name(CUSTOM_PA.name + ".tmp")
    tmp.write_text(new)
    os.replace(tmp, CUSTOM_PA)
    log(f"wrote {CUSTOM_PA}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
