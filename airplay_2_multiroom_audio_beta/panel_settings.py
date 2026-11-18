"""Settings the web panel manages, kept out of the add-on's Configuration tab.

The zones, sinks and card settings below are edited in the web panel, so they
are stored in /config/panel-settings.json instead of the add-on options (every
option in config.yaml's schema shows up in the Configuration tab).

Everything else still reads them from /data/options.json: at every start,
merge_into_options() copies them into that file (the Supervisor rewrites it at
start with only the schema's options), and save() writes both files.

Before 0.189 these were add-on options. The first start of 0.189 moves them
here: from /data/options.json if they are still in it, otherwise from the
options the Supervisor kept (GET /addons/self/info).
"""

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

PANEL_KEYS = ("remap_sinks", "card_profiles", "card_options", "combine_sinks",
              "disabled_zones", "enabled_zones")
SETTINGS_FILE = Path(os.environ.get("PANEL_SETTINGS_FILE", "/config/panel-settings.json"))
SUPERVISOR_URL = os.environ.get("SUPERVISOR_URL", "http://supervisor")


def _log(msg: str) -> None:
    print(f"[panel_settings] {msg}", flush=True)


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    os.replace(tmp, path)


def _supervisor(method: str, path: str, body=None):
    token = os.environ.get("SUPERVISOR_TOKEN", "")
    if not token:
        return None
    req = urllib.request.Request(
        f"{SUPERVISOR_URL}{path}", method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read() or b"{}")
    except (urllib.error.URLError, OSError, ValueError) as e:
        _log(f"Supervisor {method} {path}: {e}")
        return None


def panel_part(options: dict) -> dict:
    return {k: options.get(k) or [] for k in PANEL_KEYS}


def supervisor_part(options: dict) -> dict:
    return {k: v for k, v in options.items() if k not in PANEL_KEYS}


def load() -> dict:
    """The panel's settings, or {} if there is no settings file yet."""
    try:
        data = json.loads(SETTINGS_FILE.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def migrate(options: dict) -> dict:
    """First start without a settings file: takes the panel's settings from
    the options (or the Supervisor's stored copy), writes the file and drops
    them from the Supervisor's options. Returns the settings."""
    found = {k: options[k] for k in PANEL_KEYS if k in options}
    if not found:
        info = _supervisor("GET", "/addons/self/info")
        if not info or info.get("result") != "ok":
            # Don't write an empty file over settings we couldn't read.
            _log("could not read the Supervisor's stored options; trying again next start")
            return {}
        stored = (info.get("data") or {}).get("options") or {}
        found = {k: stored[k] for k in PANEL_KEYS if k in stored}
    settings = panel_part(found)
    _write_json(SETTINGS_FILE, settings)
    _log(f"panel settings moved to {SETTINGS_FILE}"
         + (f" ({', '.join(k for k in PANEL_KEYS if settings[k])})"
            if any(settings.values()) else " (none set)"))
    # Best effort: the Supervisor would otherwise keep warning about options
    # that are no longer in the schema.
    res = _supervisor("POST", "/addons/self/options", {"options": supervisor_part(options)})
    if res is not None and res.get("result") != "ok":
        _log(f"could not tidy the add-on options: {res.get('message') or res}")
    return settings


def merge_into_options(options_file: Path) -> bool:
    """At start: puts the panel's settings into /data/options.json, moving
    them to the settings file first if this is the first start since 0.189.
    False if the panel's settings couldn't be found (nothing is changed)."""
    try:
        options = json.loads(options_file.read_text())
    except (OSError, ValueError) as e:
        _log(f"cannot read {options_file}: {e}")
        return False
    if not SETTINGS_FILE.exists():
        migrate(options)
        if not SETTINGS_FILE.exists():
            return False
    merged = {**supervisor_part(options), **panel_part(load())}
    if merged != options:
        _write_json(options_file, merged)
    return True


def supervisor_changed(options: dict, options_file: Path) -> bool:
    """Whether options the panel is about to save also change the
    Configuration tab's options (which then go to the Supervisor too)."""
    try:
        before = json.loads(options_file.read_text())
    except (OSError, ValueError):
        return True
    return supervisor_part(options) != supervisor_part(before)


def save(options: dict, options_file: Path) -> None:
    """Writes the panel's settings to the settings file and all the options
    to /data/options.json (which the Supervisor only rewrites at start)."""
    _write_json(SETTINGS_FILE, panel_part(options))
    _write_json(options_file, options)
