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

shairport-sync's own discovery only announces sensors. publish_discovery()
adds to the same device Home Assistant buttons (volume up/down, play/pause,
next, previous), publishing to the zone's remote topic, and an image entity
showing the cover art from the zone's cover topic.
"""

import json
import logging
import os
import re
import threading

log = logging.getLogger(__name__)
# paho logs a harmless "Client is not connected" at ERROR when publish.multiple()
# disconnects; our own warning covers real failures.
logging.getLogger("paho").setLevel(logging.CRITICAL)

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


# (command, button name, icon): commands from shairport-sync's MQTT remote.
BUTTONS = (
    ("volumeup", "Volume up", "mdi:volume-plus"),
    ("volumedown", "Volume down", "mdi:volume-minus"),
    ("playpause", "Play/pause", "mdi:play-pause"),
    ("nextitem", "Next", "mdi:skip-next"),
    ("previtem", "Previous", "mdi:skip-previous"),
)
_published = set()


def device_id(zone_name: str) -> str:
    """The AirPlay device id shairport-sync derives from the zone name (FNV-1a
    of the name, locally administered, unicast; shairport.c), e.g.
    "c6:f2:ef:18:ba:dd". Its MQTT discovery uses it as the device identifier,
    so buttons with the same one join that device."""
    h = 1469598103934665603
    for b in zone_name.encode():
        h = ((h ^ b) * 1099511628211) & 0xFFFFFFFFFFFFFFFF
    i = h & 0xFFFFFFFFFFFF
    top = (((i >> 40) & 0xFE) | 0x02) << 40
    i = (i & 0xFFFFFFFFFF) | top
    return ":".join(f"{(i >> s) & 0xFF:02x}" for s in range(40, -8, -8))


def discovery_messages(zone_name: str):
    dev = device_id(zone_name)
    uid = "shairport-sync_" + dev.replace(":", "")
    topic = topic_for(zone_name)
    # Predictable entity ids for new entities (e.g. image.airplay2_dining_room_cover);
    # ones Home Assistant already has keep theirs.
    ent = "airplay2_" + topic.split("/", 1)[1].lower()
    device = {"identifiers": [dev], "name": zone_name}
    msgs = []
    for cmd, label, icon in BUTTONS:
        msgs.append({
            "topic": f"homeassistant/button/{uid}/{cmd}/config",
            "payload": json.dumps({
                "name": label,
                "unique_id": f"{uid}_{cmd}",
                "default_entity_id": f"button.{ent}_{cmd}",
                "command_topic": topic + "/remote",
                "payload_press": cmd,
                "icon": icon,
                "device": device,
            }),
            "retain": True,
        })
    # The cover art shairport-sync publishes (raw image bytes, retained).
    msgs.append({
        "topic": f"homeassistant/image/{uid}/cover/config",
        "payload": json.dumps({
            "name": "Cover",
            "unique_id": f"{uid}_cover",
            "default_entity_id": f"image.{ent}_cover",
            "image_topic": topic + "/cover",
            "content_type": "image/jpeg",
            "icon": "mdi:image-album",
            "device": device,
        }),
        "retain": True,
    })
    return msgs


def publish_discovery(zone_name: str) -> None:
    """Announce the zone's buttons and cover image (retained), once per zone
    name and run, in the background so a slow or missing broker never holds
    up a zone start."""
    host = os.environ.get("AP2_MQTT_HOST", "")
    if not host or zone_name in _published:
        return
    _published.add(zone_name)

    def run():
        try:
            from paho.mqtt import publish
            auth = None
            if os.environ.get("AP2_MQTT_USER"):
                auth = {"username": os.environ["AP2_MQTT_USER"],
                        "password": os.environ.get("AP2_MQTT_PASSWORD", "")}
            publish.multiple(discovery_messages(zone_name), hostname=host,
                             port=int(os.environ.get("AP2_MQTT_PORT") or 1883),
                             auth=auth, client_id="")
            log.info("MQTT: announced buttons and cover for %s", zone_name)
        except Exception as e:  # noqa: BLE001 -- never let this hurt the zone
            _published.discard(zone_name)
            log.warning("MQTT: could not announce buttons and cover for %s: %s", zone_name, e)

    threading.Thread(target=run, name=f"mqtt-discovery-{zone_name}", daemon=True).start()
