#!/usr/bin/env python3
"""
remap_sinks.py -- turns the add-on's `remap_sinks` option into PulseAudio
`load-module module-remap-sink ...` lines and writes them to custom.pa in
the add-on's config folder (/config inside the container,
/addon_configs/<slug>/ on the host).

That file uses the same syntax as the audio plugin's own /data/custom.pa,
which hassio_audio includes at startup. Copy the managed block there and
the sinks exist at boot even without this add-on.

Step 1: generate only. This script never loads or unloads PulseAudio
modules. It:
  * rewrites only the block between the BEGIN/END markers, leaving any
    other text in custom.pa alone
  * lists remap sinks that are loaded right now but are not in the
    options (for example ones made by the Multi-room Audio Controller) as
    comments, so they can be copied into the options
  * logs a diff of what changed

Usage: remap_sinks.py [--dry-run]
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

NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


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


def module_line(entry: dict):
    """The load-module line for one option entry, or None (with a log line
    saying why) if the entry is unusable."""
    name = str(entry.get("name", "")).strip()
    master = str(entry.get("master", "")).strip()
    pair = str(entry.get("channels", "")).strip()
    desc = str(entry.get("description", "") or "").strip()

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
    out, is_remap = [], False
    for line in res.stdout.splitlines():
        if line.startswith("Module #"):
            is_remap = False
        elif re.match(r"\s*Name:\s*module-remap-sink\s*$", line):
            is_remap = True
        elif is_remap and re.match(r"\s*Argument:", line):
            out.append(line.split(":", 1)[1].strip())
            is_remap = False
    return out


def sink_name_of(args: str):
    m = re.search(r"(?:^|\s)sink_name=(\S+)", args)
    return m.group(1) if m else None


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


def build_block(entries: list, old: str = "") -> list:
    lines = [BEGIN,
             "# Generated from the add-on's remap_sinks option. Edits inside this",
             "# block are overwritten; put your own lines outside it."]
    names = set()
    for entry in entries:
        line = module_line(entry if isinstance(entry, dict) else {})
        if line:
            names.add(sink_name_of(line))
            lines.append(line)
    log(f"{len(names)} configured remap sink(s): {', '.join(sorted(names)) or 'none'}")

    loaded = loaded_remap_modules()
    if loaded is None:
        kept = old_unmanaged(old)
        if kept:
            lines.append("#")
            lines.append("# Loaded now but not in the add-on options (left alone):")
            lines.extend(kept)
            log(f"kept {len(kept)} unmanaged line(s) from the previous run")
    else:
        unmanaged = [a for a in loaded if sink_name_of(a) not in names]
        if unmanaged:
            lines.append("#")
            lines.append("# Loaded now but not in the add-on options (left alone):")
            for a in unmanaged:
                lines.append(f"# load-module module-remap-sink {a}")
            log(f"{len(unmanaged)} unmanaged remap sink(s) loaded: "
                + ", ".join(sink_name_of(a) or "?" for a in unmanaged))
    lines.append(END)
    return lines


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
    old = CUSTOM_PA.read_text() if CUSTOM_PA.exists() else ""
    block = build_block(load_options(), old)
    new = splice(old, block)

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
