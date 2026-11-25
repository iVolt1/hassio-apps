"""MQTT block for a zone's shairport-sync config (Home Assistant use).

The zone managers' s6 run scripts look up the Mosquitto broker through the
Supervisor (services: mqtt) before they start, and pass it in AP2_MQTT_*
environment variables; this module only turns those into a libconfig block.
The managers can't ask the Supervisor themselves: with ap2_ip set they run in
the zones' network namespace, which has no route to it.

Each zone publishes under airplay2/<zone>/ (artist, title, album, volume,
play_start, play_end, ... and the cover), with retain on, announces its
sensors to Home Assistant (MQTT discovery) and takes remote commands on
airplay2/<zone>/remote (play, pause, nextitem, previtem, volumeup, ...).
No broker, or the add-on's mqtt option off: no block, as before.
"""

import os
import re

TOPIC_PREFIX = "airplay2"


def _quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def topic_for(zone_name: str) -> str:
    """airplay2/<zone>, with anything but letters, digits, - and _ as _."""
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", zone_name).strip("_") or "zone"
    return f"{TOPIC_PREFIX}/{slug}"


def mqtt_block(zone_name: str) -> str:
    host = os.environ.get("AP2_MQTT_HOST", "")
    if not host:
        return ""
    lines = [
        '  enabled = "yes";',
        f"  hostname = {_quote(host)};",
        f"  port = {int(os.environ.get('AP2_MQTT_PORT') or 1883)};",
    ]
    user = os.environ.get("AP2_MQTT_USER", "")
    if user:
        lines.append(f"  username = {_quote(user)};")
        lines.append(f"  password = {_quote(os.environ.get('AP2_MQTT_PASSWORD', ''))};")
    lines += [
        f"  topic = {_quote(topic_for(zone_name))};",
        '  publish_parsed = "yes";',
        '  publish_cover = "yes";',
        '  publish_retain = "yes";',
        '  enable_autodiscovery = "yes";',
        '  enable_remote = "yes";',
    ]
    return "mqtt :\n{\n" + "\n".join(lines) + "\n};\n"
