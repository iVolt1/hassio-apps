#!/usr/bin/env python3
"""
bt_reset.py -- power-cycle the host's Bluetooth adapter(s) through BlueZ on
the HOST system bus, the same as `bluetoothctl power off; power on`.

Fixes adapters that stay connected to their A2DP sinks but stop passing audio
after some hours; BlueZ clients (e.g. the Bluetooth Audio Manager app)
reconnect the sinks once the adapter is powered again.

The host bus comes from `host_dbus: true` (Supervisor bind-mounts the host's
/run/dbus here). This add-on's own private bus for avahi lives at
/run/ap2-dbus instead, so the host bus is always addressed explicitly.

  bt_reset.py            reset every adapter (hci0, hci1, ...)
  bt_reset.py hci0       reset only hci0

Used by the web panel's Reset Bluetooth button and, with the
bt_reset_on_start option, once at add-on start. Standard library only;
needs dbus-send.
"""

import os
import re
import subprocess
import sys
import time

HOST_BUS = os.environ.get("HOST_DBUS_ADDRESS", "unix:path=/run/dbus/system_bus_socket")
OFF_SECONDS = 2


def _send(*args, timeout=10) -> subprocess.CompletedProcess:
    return subprocess.run(["dbus-send", f"--bus={HOST_BUS}", "--print-reply",
                           "--dest=org.bluez", *args],
                          capture_output=True, text=True, timeout=timeout)


def adapters() -> list:
    """Adapter object paths (/org/bluez/hciN) from BlueZ's ObjectManager."""
    r = _send("/", "org.freedesktop.DBus.ObjectManager.GetManagedObjects")
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip() or "dbus-send failed")
    # Device paths are /org/bluez/hciN/dev_...; the closing quote keeps only adapters.
    return sorted(set(re.findall(r'object path "(/org/bluez/hci\d+)"', r.stdout)))


def _set_powered(path: str, on: bool) -> None:
    r = _send(path, "org.freedesktop.DBus.Properties.Set", "string:org.bluez.Adapter1",
              "string:Powered", f"variant:boolean:{'true' if on else 'false'}")
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip() or "dbus-send failed")


def _powered(path: str):
    r = _send(path, "org.freedesktop.DBus.Properties.Get", "string:org.bluez.Adapter1",
              "string:Powered")
    m = re.search(r"boolean (true|false)", r.stdout)
    return None if not m else m.group(1) == "true"


def reset(only: str = "") -> list:
    """Power-cycle the adapter(s); returns log lines. Raises if none could be reset."""
    if not os.path.exists(HOST_BUS.split("unix:path=", 1)[-1]):
        raise RuntimeError(f"host D-Bus not available ({HOST_BUS}) - is host_dbus enabled?")
    paths = adapters()
    if only:
        paths = [p for p in paths if p.rsplit("/", 1)[-1] == only]
    if not paths:
        raise RuntimeError(f"no Bluetooth adapter{' ' + only if only else ''} found in BlueZ")
    lines, failed = [], 0
    for p in paths:
        name = p.rsplit("/", 1)[-1]
        try:
            _set_powered(p, False)
            lines.append(f"{name}: powered off")
            time.sleep(OFF_SECONDS)
            _set_powered(p, True)
            time.sleep(1)
            lines.append(f"{name}: powered on (Powered={_powered(p)})")
        except Exception as e:  # keep going with the other adapters
            failed += 1
            lines.append(f"{name}: reset failed: {e}")
    if failed == len(paths):
        raise RuntimeError("; ".join(lines))
    lines.append("Bluetooth sinks reconnect through their BlueZ client in a few seconds.")
    return lines


if __name__ == "__main__":
    try:
        for line in reset(sys.argv[1] if len(sys.argv) > 1 else ""):
            print(f"bt_reset: {line}")
    except Exception as e:
        print(f"bt_reset: {e}", file=sys.stderr)
        sys.exit(1)
