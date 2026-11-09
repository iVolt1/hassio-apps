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
  * with --load, first reloads each card in the card_options option with
    those module-alsa-card arguments (e.g. mmap=no tsched=no, the workaround
    for the Creative X-Fi snd_ctxfi mmap regression), then sets each card in
    the card_profiles option to its
    profile (a card's master sink only exists under the right profile), then
    makes PulseAudio match the options (remap sinks, then the combine_sinks
    built on them), using the same lines
    that go into custom.pa: loads configured sinks that are missing, reloads
    ones whose entry changed, and unloads ones it loaded earlier whose entry
    was removed

Usage: remap_sinks.py [--first-run] [--load] [--dry-run] [--reload-combine NAME[,NAME...]]
  --first-run: once per install (until /data/zones_opt_in exists). On a
  fresh install it unloads sinks a previous install of this add-on left in
  PulseAudio (they carry the airplay2_multiroom.managed property), so a
  reinstall starts clean. On an install from before zones were opt-in it
  turns every zone that would run today into an enabled_zones entry. From
  then on a zone runs only if it is in enabled_zones.
  --reload-combine: reload those combine sinks even if unchanged (the zone
  manager uses it when a member that had gone away, such as a Bluetooth
  speaker, is back: PulseAudio doesn't re-add it to the combine sink)
"""

import difflib
import fcntl
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

OPTIONS_FILE = Path(os.environ.get("RS_OPTIONS_FILE", "/data/options.json"))
CUSTOM_PA = Path(os.environ.get("RS_CUSTOM_PA", "/config/custom.pa"))
STATE_FILE = Path(os.environ.get("RS_STATE_FILE",
                                 str(CUSTOM_PA.with_name("remap_sinks_state.json"))))

COMBINE_STATE_FILE = Path(os.environ.get(
    "RS_COMBINE_STATE_FILE", str(CUSTOM_PA.with_name("combine_sinks_state.json"))))

# Every sink this add-on loads carries this property, so a fresh install can
# tell a previous install's leftovers from sinks something else loaded.
TAG = "airplay2_multiroom.managed=1"

# Exists once --first-run has run: zones are opt-in (enabled_zones).
OPT_IN_FILE = Path(os.environ.get("RS_OPT_IN_FILE", "/data/zones_opt_in"))
PORT_MAP_FILE = Path(os.environ.get("RS_PORT_MAP_FILE", "/config/shairport-sync/config/port_map.txt"))


def zone_is_on(options: dict, key: str) -> bool:
    """Whether a zone's AirPlay receiver should run. Zones are opt-in
    (enabled_zones) once --first-run has run, else on unless in
    disabled_zones (installs from before 0.138 until their first start)."""
    if OPT_IN_FILE.exists():
        return key in (options.get("enabled_zones") or [])
    return key not in (options.get("disabled_zones") or [])


def set_zone_on(options: dict, key: str, on: bool, old_key: str = None) -> bool:
    """Records a zone as on or off in the options (dropping old_key, the
    name before a rename). True if the options changed."""
    field = "enabled_zones" if OPT_IN_FILE.exists() else "disabled_zones"
    cur = [k for k in (options.get(field) or []) if isinstance(k, str)]
    new = [k for k in cur if k not in (key, old_key)]
    if on == (field == "enabled_zones"):
        new.append(key)
    if sorted(new) == sorted(cur):
        return False
    options[field] = new
    return True


def save_options(options: dict) -> tuple:
    """(ok, message): saves the options through the Supervisor, then
    /data/options.json (the Supervisor only rewrites it at start)."""
    token = os.environ.get("SUPERVISOR_TOKEN", "")
    if not token:
        return False, "no SUPERVISOR_TOKEN"
    req = urllib.request.Request(
        "http://supervisor/addons/self/options",
        data=json.dumps({"options": options}).encode(), method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            body = json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return False, f"Supervisor refused the options: {e}"
    except (OSError, ValueError) as e:
        return False, f"cannot reach the Supervisor: {e}"
    if body.get("result") != "ok":
        return False, f"Supervisor refused the options: {body.get('message') or body}"
    tmp = OPTIONS_FILE.with_name(OPTIONS_FILE.name + ".tmp")
    tmp.write_text(json.dumps(options))
    os.replace(tmp, OPTIONS_FILE)
    return True, "options saved"


def first_run() -> None:
    """See --first-run in the module docstring."""
    if OPT_IN_FILE.exists():
        return
    try:
        options = json.loads(OPTIONS_FILE.read_text())
    except (OSError, ValueError) as e:
        log(f"first run: cannot read {OPTIONS_FILE} ({e}); trying again next start")
        return
    try:
        ports = [l.split()[0] for l in PORT_MAP_FILE.read_text().splitlines() if l.split()]
    except OSError:
        ports = []
    if ports:
        # Upgrade: keep every zone that runs today running.
        off = set(options.get("disabled_zones") or [])
        options["enabled_zones"] = sorted({k for k in ports if k not in off}
                                          | set(options.get("enabled_zones") or []))
        options["disabled_zones"] = []
        ok, msg = save_options(options)
        if not ok:
            log(f"zones stay as they are for now ({msg}); trying again next start")
            return
        log("zones are opt-in from now on; kept on: " + (", ".join(options["enabled_zones"]) or "none"))
    else:
        # Fresh install: unload what a previous install left loaded.
        ok, out = pactl("list", "modules")
        if ok:
            mods, idx, name = [], None, None
            for line in out.splitlines():
                m = re.match(r"Module #(\d+)", line)
                if m:
                    idx, name = m.group(1), None
                    continue
                m = re.match(r"\s*Name:\s*(\S+)", line)
                if m:
                    name = m.group(1)
                    continue
                m = re.match(r"\s*Argument:\s*(.*)", line)
                if m and idx and name in ("module-remap-sink", "module-combine-sink") and TAG in m.group(1):
                    mods.append((name, idx, sink_name_of(m.group(1)) or "?"))
            # Combine sinks first: they sit on top of the remap sinks.
            for name, idx, sink in sorted(mods, key=lambda x: x[0] != "module-combine-sink"):
                ok2, err = pactl("unload-module", idx)
                log(f"fresh install: unloaded {sink}, left by a previous install" if ok2
                    else f"fresh install: unloading {sink} failed: {err}")
        log("fresh install: no zones run until you switch them on in the web panel")
    OPT_IN_FILE.parent.mkdir(parents=True, exist_ok=True)
    OPT_IN_FILE.write_text("1\n")


# sink_name -> module index, filled by loaded_remap_modules()
MODULE_IDX = {}
# combine sink_name -> (module index, argument string), filled likewise
COMBINE_MODS = {}

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

# channels: custom takes any two of the master's channels (left, right; the
# same one twice makes a mono zone). Abbreviations are for system names.
CHANNEL_ABBR = {
    "mono": "mono", "front-left": "fl", "front-right": "fr", "front-center": "fc",
    "rear-left": "rl", "rear-right": "rr", "rear-center": "rc", "lfe": "lfe",
    "side-left": "sl", "side-right": "sr", "front-left-of-center": "flc",
    "front-right-of-center": "frc",
}
CHANNEL_RE = re.compile(r"^(?:" + "|".join(map(re.escape, CHANNEL_ABBR)) + r"|aux\d{1,2}|top-[a-z-]+)$")


def channel_abbr(ch: str) -> str:
    return CHANNEL_ABBR.get(ch) or re.sub(r"[^a-z0-9]+", "", ch)


def master_channels(pair: str, left: str = "", right: str = ""):
    """master_channel_map for a channels choice, or None if invalid."""
    if pair == "custom":
        if CHANNEL_RE.match(left or "") and CHANNEL_RE.match(right or ""):
            return f"{left},{right}"
        return None
    return PAIRS.get(pair)

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


def system_name(master: str, pair: str, descs: dict, left: str = "", right: str = ""):
    """Default sink name: the master's description, cleaned up, plus the
    pair (for custom, the two channels' abbreviations). None if the master
    isn't known to PulseAudio."""
    desc = descs.get(master)
    if pair == "custom":
        suffix = (f"{channel_abbr(left)}_{channel_abbr(right)}"
                  if master_channels(pair, left, right) else None)
    else:
        suffix = PAIR_SUFFIX.get(pair)
    if not desc or not suffix:
        return None
    base = re.sub(r"[^A-Za-z0-9]+", "_", desc).strip("_")
    return f"{base}_{suffix}" if base else None


def entry_system_name(entry: dict, descs: dict):
    """system_name() for a remap_sinks option entry."""
    return system_name(str(entry.get("master", "")), str(entry.get("channels", "")), descs,
                       str(entry.get("left", "") or ""), str(entry.get("right", "") or ""))


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


def load_card_profiles() -> list:
    try:
        opts = json.loads(OPTIONS_FILE.read_text())
    except (OSError, ValueError):
        return []
    cp = opts.get("card_profiles") or []
    return cp if isinstance(cp, list) else []


def card_profiles_now():
    """{card name: (active profile, set of profile names)} from
    `pactl list cards`, or None if pactl can't be queried."""
    try:
        res = subprocess.run(["pactl", "list", "cards"], capture_output=True,
                             text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if res.returncode != 0:
        return None
    out, name, section = {}, None, None
    for line in res.stdout.splitlines():
        s = line.strip()
        if line.startswith("Card #"):
            name, section = None, None
        elif line.startswith("\t") and not line.startswith("\t\t"):
            section = None
            if s.startswith("Name:"):
                name = s[5:].strip()
                out[name] = ["", set()]
            elif s.startswith("Active Profile:") and name:
                out[name][0] = s[15:].strip()
            elif s == "Profiles:":
                section = "Profiles"
        elif section == "Profiles" and name and line.startswith("\t\t") \
                and not line.startswith("\t\t\t") and ": " in s:
            out[name][1].add(s.split(": ", 1)[0])
    return {k: (v[0], v[1]) for k, v in out.items()}


# module-alsa-card arguments card_options may set; the card's identity
# (device_id, name, card_name, ...) always comes from the loaded module.
CARD_OPTION_KEYS = {"mmap", "tsched", "tsched_buffer_size", "tsched_buffer_watermark",
                    "fixed_latency_range", "fragments", "fragment_size", "format", "rate",
                    "ignore_dB", "deferred_volume", "use_ucm", "avoid_resampling"}
MODARG_RE = re.compile(r'(\w+)=("([^"]*)"|\S+)')


def alsa_card_modules():
    """{card_name: (module index, [(key, raw value)])} for every loaded
    module-alsa-card, or None if pactl can't be queried. raw values keep
    their quotes, so the arguments can be passed back unchanged."""
    try:
        res = subprocess.run(["pactl", "list", "modules"], capture_output=True,
                             text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if res.returncode != 0:
        return None
    out, idx, is_card = {}, None, False
    for line in res.stdout.splitlines():
        if line.startswith("Module #"):
            idx, is_card = line[len("Module #"):].strip(), False
        elif re.match(r"\s*Name:\s*module-alsa-card\s*$", line):
            is_card = True
        elif is_card and re.match(r"\s*Argument:", line):
            args = [(m.group(1), m.group(2)) for m in MODARG_RE.finditer(line.split(":", 1)[1])]
            name = next((v.strip('"') for k, v in args if k == "card_name"), None)
            if name:
                out[name] = (idx, args)
            is_card = False
    return out


def apply_card_options() -> None:
    """Reloads each card in the card_options option whose module-alsa-card
    arguments don't already include those options, keeping its current
    profile. PulseAudio drops the remap sinks on that card with it; they are
    loaded again further on in this run."""
    try:
        wanted = json.loads(OPTIONS_FILE.read_text()).get("card_options") or []
    except (OSError, ValueError):
        return
    wanted = [e for e in wanted if isinstance(e, dict)] if isinstance(wanted, list) else []
    if not wanted:
        return
    mods = alsa_card_modules()
    if mods is None:
        log("pactl not available; not applying card options")
        return
    for e in wanted:
        card = str(e.get("card", "")).strip()
        opts = [(m.group(1), m.group(2)) for m in MODARG_RE.finditer(str(e.get("options", "")))]
        bad = [k for k, _ in opts if k not in CARD_OPTION_KEYS]
        if bad or not opts:
            log(f"card options: skipping {card}: options must be module-alsa-card settings such as "
                f"'mmap=no tsched=no' (not allowed: {', '.join(bad) or 'none given'})")
            continue
        if card not in mods:
            log(f"card options: no card {card} loaded (unplugged or renamed?); skipping")
            continue
        idx, args = mods[card]
        current = {k: v.strip('"') for k, v in args}
        if all(current.get(k) == v.strip('"') for k, v in opts):
            log(f"card options: {card} already has {' '.join(f'{k}={v}' for k, v in opts)}")
            continue
        new = dict(args)
        new.update(opts)
        new_args = " ".join(f"{k}={v}" for k, v in new.items())
        profile = (card_profiles_now() or {}).get(card, ("", set()))[0]
        ok, out = pactl("unload-module", idx)
        if not ok:
            log(f"card options: unloading {card} failed: {out}")
            continue
        ok, out = pactl("load-module", "module-alsa-card", new_args)
        if not ok:
            log(f"card options: loading {card} with {' '.join(f'{k}={v}' for k, v in opts)} "
                f"failed ({out}); loading it as it was")
            pactl("load-module", "module-alsa-card", " ".join(f"{k}={v}" for k, v in args))
        else:
            log(f"card options: reloaded {card} with {' '.join(f'{k}={v}' for k, v in opts)} "
                f"(module #{out})")
        if profile:
            pactl("set-card-profile", card, profile)


def apply_card_profiles() -> None:
    """Sets each card in the card_profiles option to its profile, if it
    isn't already. Cards not listed are left as they are."""
    wanted = [e for e in load_card_profiles() if isinstance(e, dict)]
    if not wanted:
        return
    now = card_profiles_now()
    if now is None:
        log("pactl not available; not setting card profiles")
        return
    for e in wanted:
        card, profile = str(e.get("card", "")).strip(), str(e.get("profile", "")).strip()
        if card not in now:
            log(f"card profile: no card {card} (unplugged or renamed?); skipping")
        elif profile not in now[card][1]:
            log(f"card profile: {card} has no profile {profile}; skipping")
        elif now[card][0] == profile:
            log(f"card profile: {card} already {profile}")
        else:
            res = subprocess.run(["pactl", "set-card-profile", card, profile],
                                 capture_output=True, text=True, timeout=15)
            if res.returncode == 0:
                log(f"card profile: set {card} to {profile} (was {now[card][0]})")
            else:
                log(f"card profile: setting {card} to {profile} failed: "
                    f"{res.stderr.strip() or res.returncode}")


def module_line(entry: dict, descs: dict = None):
    """The load-module line for one option entry, or None (with a log line
    saying why) if the entry is unusable. An entry without a name gets its
    system name."""
    name = str(entry.get("name", "") or "").strip()
    master = str(entry.get("master", "")).strip()
    pair = str(entry.get("channels", "")).strip()
    left = str(entry.get("left", "") or "").strip()
    right = str(entry.get("right", "") or "").strip()
    desc = str(entry.get("description", "") or "").strip()

    if not name:
        name = system_name(master, pair, descs or {}, left, right) or ""
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
    if pair not in PAIRS and pair != "custom":
        log(f"skipping {name}: channels must be one of {', '.join(PAIRS)}, custom; got {pair!r}")
        return None
    mcm = master_channels(pair, left, right)
    if not mcm:
        log(f"skipping {name}: channels custom needs left and right set to channel names "
            f"such as front-center or side-left (got {left!r}, {right!r})")
        return None

    args = [
        f"sink_name={name}",
        f"master={master}",
        "channels=2",
        f"master_channel_map={mcm}",
        "channel_map=front-left,front-right",
        "remix=no",
    ]
    if desc and ('"' in desc or "'" in desc):
        log(f"{name}: quotes are not allowed in description, ignoring it")
        desc = ""
    args.insert(1, f"sink_properties=\"device.description='{desc}' {TAG}\"" if desc
                else f"sink_properties={TAG}")
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
    out, kind, idx = [], None, None
    MODULE_IDX.clear()
    COMBINE_MODS.clear()
    for line in res.stdout.splitlines():
        if line.startswith("Module #"):
            kind, idx = None, line[len("Module #"):].strip()
        elif re.match(r"\s*Name:\s*module-remap-sink\s*$", line):
            kind = "remap"
        elif re.match(r"\s*Name:\s*module-combine-sink\s*$", line):
            kind = "combine"
        elif kind and re.match(r"\s*Argument:", line):
            args = line.split(":", 1)[1].strip()
            if kind == "remap":
                out.append(args)
                MODULE_IDX[sink_name_of(args)] = idx
            else:
                COMBINE_MODS[sink_name_of(args) or f"combined#{idx}"] = (idx, args)
            kind = None
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
    switched_off = []
    for entry in entries:
        line = module_line(entry if isinstance(entry, dict) else {}, descs)
        if line:
            name = sink_name_of(line)
            if any(n == name for n, _ in configured):
                log(f"skipping duplicate entry for {name}")
                continue
            if isinstance(entry, dict) and entry.get("enabled") is False:
                # Kept in the options but not loaded; if this add-on loaded it
                # earlier, apply() unloads it like a removed entry.
                switched_off.append(name)
                lines.append(f"# SWITCHED OFF: {line}")
                continue
            configured.append((name, line[len("load-module module-remap-sink "):]))
    if switched_off:
        log(f"switched off (not loaded): {', '.join(switched_off)}")
    names = {n for n, _ in configured}

    loaded = loaded_remap_modules()
    # Sinks this add-on loaded whose entry is gone (e.g. renamed) are about to
    # be unloaded: they neither count as unmanaged nor block their channels.
    state = load_state()
    leaving = {sink_name_of(a) for a in (loaded or [])
               if sink_name_of(a) not in names and sink_name_of(a) in state
               and same(a, state[sink_name_of(a)])}
    unmanaged = [a for a in (loaded or [])
                 if sink_name_of(a) not in names and sink_name_of(a) not in leaving]

    # Channels already in use: unmanaged sinks first, then configured ones
    # in option order, so the first entry to claim a pair keeps it.
    taken = [(sink_name_of(a), *claim(a)) for a in unmanaged]
    loadable = []
    for name, args in configured:
        master, chans = claim(args)
        if descs and master not in descs:
            # E.g. its card is set to a profile without this sink. Kept in the
            # options; it loads again once the master exists.
            log(f"NOT loading {name}: its master {master} doesn't exist now "
                "(card profile changed or card unplugged?)")
            lines.append(f"# NOT LOADED, master doesn't exist now: "
                         f"load-module module-remap-sink {args}")
            continue
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
    """Makes PulseAudio match the options. At start-up this runs before the
    zone manager starts; from the web panel, the zone manager follows the
    sink changes (stops removed zones, starts new ones)."""
    if loaded is None:
        log("pactl not available; not loading or unloading any sinks")
        return set()
    current = {sink_name_of(a): a for a in loaded}
    state = load_state()
    changed = set()   # remap sinks (re)loaded now; combine sinks using them reload

    # Entries removed from the options first, so a renamed sink's old copy is
    # gone before the new one loads. Usable-but-skipped entries are still
    # configured, so they keep their state and their sink.
    for name in [n for n in state if n not in configured]:
        if name not in current:
            log(f"{name} was removed from the options and is not loaded")
        elif same(current[name], state[name]):
            if unload(name):
                log(f"unloaded {name} (switched off or removed from the options)")
        else:
            log(f"{name} was removed from the options but is loaded with arguments "
                "this add-on did not set; leaving it")
        del state[name]

    for name, args in loadable:
        if name not in current:
            if load(name, args):
                state[name] = args
                changed.add(name)
        elif same(current[name], args):
            log(f"{name} already loaded")
            state[name] = args
        else:
            log(f"{name} differs from its option; reloading")
            if unload(name):
                changed.add(name)
                if load(name, args):
                    state[name] = args
                else:
                    log(f"restoring the previous {name}")
                    load(name, current[name])

    save_state(state)
    return changed


# -- combine sinks --------------------------------------------------------
def combine_configs(remap_names: set) -> list:
    """[(name, args, members)] for the usable combine_sinks entries (members
    is a comma-separated list of sink names, at least two)."""
    try:
        opts = json.loads(OPTIONS_FILE.read_text())
    except (OSError, ValueError):
        return []
    entries = opts.get("combine_sinks") or []
    out = []
    COMBINE_OFF.clear()
    for e in entries if isinstance(entries, list) else []:
        if not isinstance(e, dict):
            continue
        name = str(e.get("name", "")).strip()
        members = [m.strip() for m in str(e.get("sinks", "")).split(",") if m.strip()]
        desc = str(e.get("description", "") or "").strip()
        if not NAME_RE.match(name):
            log(f"combine sink: skipping invalid name {name!r} (letters, digits, _ . - only)")
            continue
        if name in remap_names or name in COMBINE_OFF or any(n == name for n, _, _ in out):
            log(f"combine sink: skipping {name}: the name is already used by another sink")
            continue
        bad = [m for m in members if not NAME_RE.match(m)]
        if bad or len(set(members)) < 2:
            log(f"combine sink: skipping {name}: sinks needs at least two different sink "
                f"names, comma-separated (got {e.get('sinks')!r})")
            continue
        args = f"sink_name={name} slaves={','.join(members)}"
        if desc and ('"' in desc or "'" in desc):
            log(f"combine sink {name}: quotes are not allowed in description, ignoring it")
            desc = ""
        args += (f" sink_properties=\"device.description='{desc}' {TAG}\"" if desc
                 else f" sink_properties={TAG}")
        if e.get("enabled") is False:
            # Kept in the options but not loaded; apply_combines() unloads it
            # if this add-on loaded it earlier.
            COMBINE_OFF[name] = args
            continue
        out.append((name, args, members))
    if COMBINE_OFF:
        log(f"combine sink(s) switched off (not loaded): {', '.join(COMBINE_OFF)}")
    return out


COMBINE_OFF = {}   # name -> args of switched-off combine_sinks entries


def combine_block(combos: list) -> list:
    """custom.pa lines for the combine sinks (after the remap sinks they
    use), plus unmanaged combine sinks as comments."""
    lines = [f"load-module module-combine-sink {a}" for _, a, _ in combos]
    lines += [f"# SWITCHED OFF: load-module module-combine-sink {a}" for a in COMBINE_OFF.values()]
    names = {n for n, _, _ in combos}
    mine = load_json(COMBINE_STATE_FILE)
    other = [a for n, (_, a) in COMBINE_MODS.items()
             if n not in names and n not in mine and n not in COMBINE_OFF]
    if other:
        lines += ["#", "# Combine sinks loaded now but not in the add-on options (left alone):"]
        lines += [f"# load-module module-combine-sink {a}" for a in other]
    return lines


def load_json(path: Path) -> dict:
    try:
        d = json.loads(path.read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def apply_combines(combos: list, changed: set, forced: set = frozenset()) -> None:
    """Loads each configured combine sink whose members all exist, reloads
    one whose entry changed or one of whose members was just (re)loaded
    (PulseAudio drops a member that goes away and doesn't take it back),
    and unloads combine sinks this add-on loaded whose entry was removed."""
    if loaded_remap_modules() is None:   # refresh COMBINE_MODS after the remap changes
        return
    sinks_now = set(sink_descriptions())
    cstate = load_json(COMBINE_STATE_FILE)
    names = {n for n, _, _ in combos}

    def unload_idx(name, idx):
        ok, out = pactl("unload-module", idx)
        if not ok:
            log(f"unloading combine sink {name} failed: {out}")
        return ok

    for name in [n for n in cstate if n not in names]:
        cur = COMBINE_MODS.get(name)
        if cur and same(cur[1], cstate[name]):
            if unload_idx(name, cur[0]):
                log(f"unloaded combine sink {name} " +
                    ("(switched off)" if name in COMBINE_OFF else "(removed from the options)"))
        elif cur:
            log(f"combine sink {name} was removed from the options but is loaded with "
                "arguments this add-on did not set; leaving it")
        del cstate[name]

    for name, args, members in combos:
        cur = COMBINE_MODS.get(name)
        missing = [m for m in members if m not in sinks_now]
        if missing:
            log(f"NOT loading combine sink {name}: member sink(s) {', '.join(missing)} "
                "don't exist now")
            if cur and name in cstate and same(cur[1], cstate[name]) and unload_idx(name, cur[0]):
                log(f"unloaded combine sink {name} until its members are back")
            continue
        touched = [m for m in members if m in changed]
        back = name in forced and not touched
        if cur and same(cur[1], args) and not touched and not back:
            log(f"combine sink {name} already loaded")
            cstate[name] = args
            continue
        if cur:
            why = ("differs from its option" if not same(cur[1], args) else
                   f"member(s) {', '.join(touched)} reloaded" if touched else "a member is back")
            log(f"combine sink {name}: {why}; reloading")
            if not unload_idx(name, cur[0]):
                continue
        ok, out = pactl("load-module", "module-combine-sink", args)
        if ok:
            log(f"loaded combine sink {name} (module #{out}): {', '.join(members)}")
            cstate[name] = args
        else:
            log(f"loading combine sink {name} failed: {out}")

    COMBINE_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = COMBINE_STATE_FILE.with_name(COMBINE_STATE_FILE.name + ".tmp")
    tmp.write_text(json.dumps(cstate, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, COMBINE_STATE_FILE)


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


CARD_VOLUME_STATE = Path(os.environ.get("RS_CARD_VOLUME_STATE", "/tmp/card_volume_done.json"))


def apply_card_volumes(combos) -> None:
    """Sets each sound card output this add-on plays through (the master of
    a remap sink, or a card output that is a combine sink member) to its
    0 dB point and unmutes it. Zones control their own volume (AirPlay
    volume, Max volume), so a lower card volume only lowers their ceiling.
    The 0 dB point is PulseAudio's Base Volume, not 100%: above it a card
    with a hardware mixer is boosted in software and can clip. Bluetooth
    sinks are left alone (their volume is the speaker's own). Each card is
    set once per appearance (its owner module), so a volume changed by hand
    later is kept until the card or PulseAudio comes back."""
    targets = {str(e.get("master", "")) for e in load_options()
               if isinstance(e, dict) and e.get("enabled") is not False}
    targets |= {m for _, _, members in combos for m in members}
    targets = {t for t in targets if t.startswith("alsa_output.")}
    if not targets:
        return
    ok, out = pactl("list", "sinks")
    if not ok:
        return
    sinks, cur = {}, None
    for line in out.splitlines():
        t = line.strip()
        if t.startswith("Name:"):
            cur = sinks.setdefault(t[5:].strip(), {})
        elif cur is None:
            continue
        elif t.startswith("Owner Module:"):
            cur["module"] = t.split(":", 1)[1].strip()
        elif t.startswith("Mute:"):
            cur["mute"] = t.endswith("yes")
        elif t.startswith("Volume:"):
            cur["volume"] = t[7:].strip()
            cur["raw"] = [int(v) for v in re.findall(r":\s*(\d+)\s*/", t)]
        elif t.startswith("Base Volume:"):
            m = re.match(r"Base Volume:\s*(\d+)", t)
            cur["base"] = int(m.group(1)) if m else 65536
    try:
        done = json.loads(CARD_VOLUME_STATE.read_text())
    except (OSError, ValueError):
        done = {}
    for name in sorted(targets):
        info = sinks.get(name)
        if not info or done.get(name) == info.get("module"):
            continue
        base = info.get("base", 65536)
        was = []
        if info.get("raw") and any(v != base for v in info["raw"]):
            pct = round(100 * max(info["raw"]) / 65536)
            was.append(f"was {pct}%")
            ok1, err = pactl("set-sink-volume", name, str(base))
            if not ok1:
                log(f"card volume: setting {name} failed: {err}")
                continue
        if info.get("mute"):
            was.append("was muted")
            pactl("set-sink-mute", name, "0")
        done[name] = info.get("module")
        if was:
            log(f"card volume: {name} set to its 0 dB point ({round(100 * base / 65536)}%)"
                f"{' and unmuted' if info.get('mute') else ''} ({', '.join(was)})")
    try:
        CARD_VOLUME_STATE.write_text(json.dumps(done))
    except OSError:
        pass


def main() -> int:
    # The web panel and the start-up hook may both run this; one at a time.
    lock = open(os.environ.get("RS_LOCK_FILE", "/tmp/remap_sinks.lock"), "w")
    fcntl.flock(lock, fcntl.LOCK_EX)
    dry_run = "--dry-run" in sys.argv[1:]
    load = "--load" in sys.argv[1:]
    if "--first-run" in sys.argv[1:] and not dry_run:
        first_run()
    if load and not dry_run:
        apply_card_options()
        apply_card_profiles()
    old = CUSTOM_PA.read_text() if CUSTOM_PA.exists() else ""
    block, loadable, loaded, configured = build_block(load_options(), old)
    combos = combine_configs(configured)
    block = block[:-1] + combine_block(combos) + block[-1:]
    new = splice(old, block)

    if load and not dry_run:
        changed = apply(loadable, loaded, configured)
        forced = set()
        argv = sys.argv[1:]
        for i, a in enumerate(argv):
            if a == "--reload-combine" and i + 1 < len(argv):
                forced |= {n.strip() for n in argv[i + 1].split(",") if n.strip()}
        apply_combines(combos, changed, forced)
        apply_card_volumes(combos)

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
