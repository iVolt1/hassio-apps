#!/usr/bin/env python3
"""Line inputs -> Music Assistant, as Sendspin sources.

Each sound-card input switched on in the web panel becomes a Sendspin client
with the source role (source@v1). Music Assistant's built-in Sendspin Source
plugin lists it under Live Inputs, and can play it on any player or group.

Per input this runs:
  - a ClientListener (WebSocket server + _sendspin._tcp mDNS advert), so
    Music Assistant finds the input and connects to it;
  - parec, capturing the input continuously (48 kHz / 16-bit / stereo) to
    measure its level;
  - line sense: the input reports signal "present" once its level stays above
    the threshold for a moment, and "absent" after it has been below it for a
    while. Music Assistant can start playback on a chosen player when the
    signal appears ("Automatically play line-in on" in the device's settings)
    and stops it a minute after the signal goes;
  - streaming: only when Music Assistant asks (server "start"), as PCM.

Pairing: unpaired access is on, so Music Assistant can approve the input
directly. Music Assistant can also pair with a code: the code is shown in the
web panel while a pairing attempt runs.

Files (/config/line-in):
  discovered.json  inputs found (pactl list sources, monitors left out)
  settings.json    {source: {"name", "threshold_db", "hold_s"}} (all optional)
  status.json      {source: {...state...}}, read by the web panel
  <id>/identity.json, <id>/pairing.json   per-input Sendspin identity/records
Inputs are opt-in: on only when "linein:<source>" is in enabled_zones.
"""

import array
import asyncio
import json
import logging
import math
import os
import re
import signal
import sys
import time
from pathlib import Path

from aiosendspin.client import ClientListener, PairingSupport, SendspinClient
from aiosendspin.models.player import SupportedAudioFormat
from aiosendspin.models.source import ClientHelloSourceFeatures, ClientHelloSourceSupport
from aiosendspin.models.types import AudioCodec, Roles, SignalState
from aiosendspin.noise import FileClientPairingStore, Identity, b64url_decode, b64url_encode

DIR = Path(os.environ.get("LINEIN_DIR", "/config/line-in"))
OPTIONS_FILE = Path(os.environ.get("LINEIN_OPTIONS_FILE", "/data/options.json"))
OPT_IN_FILE = Path(os.environ.get("LINEIN_OPT_IN_FILE", "/data/zones_opt_in"))
KEY_PREFIX = "linein:"
PORT_BASE = int(os.environ.get("LINEIN_PORT_BASE", "8940"))
RATE, CHANNELS, BPS = 48000, 2, 2
FRAME = CHANNELS * BPS
READ_MS = 20                          # parec read size
LEVEL_MS = 100                        # level window
DEFAULTS = {"threshold_db": -50.0, "hold_s": 10.0}
PRESENT_AFTER_S = 0.5                 # above the threshold this long: signal present
CAPTURE_LATENCY_US = 30_000           # parec buffering, roughly; backdates timestamps

logging.basicConfig(level=logging.INFO, format="%(asctime)s [line-in] %(message)s")
logging.getLogger("aiosendspin").setLevel(logging.WARNING)
logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
log = logging.getLogger("linein")


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    os.replace(tmp, path)


def input_id(source: str) -> str:
    """A file-name-safe id for a PulseAudio source name."""
    return re.sub(r"[^A-Za-z0-9_.-]", "_", source)


def enabled_sources() -> set:
    if not OPT_IN_FILE.exists():
        return set()
    opts = read_json(OPTIONS_FILE, {})
    return {k[len(KEY_PREFIX):] for k in (opts.get("enabled_zones") or [])
            if isinstance(k, str) and k.startswith(KEY_PREFIX)}


async def list_inputs() -> list:
    """[{source, description, spec}] for every capture source but monitors."""
    try:
        p = await asyncio.create_subprocess_exec(
            "pactl", "list", "sources", stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL)
        out = (await asyncio.wait_for(p.communicate(), 10))[0].decode(errors="replace")
    except (OSError, asyncio.TimeoutError):
        return []
    found, cur = [], None
    for line in out.splitlines():
        if line.startswith("Source #"):
            cur = {}
            found.append(cur)
        elif cur is not None:
            s = line.strip()
            for k, f in (("Name: ", "source"), ("Description: ", "description"),
                         ("Sample Specification: ", "spec"), ("Monitor of Sink: ", "monitor_of")):
                if s.startswith(k):
                    cur[f] = s[len(k):]
    return [{"source": c["source"], "description": c.get("description", c["source"]),
             "spec": c.get("spec", "")}
            for c in found if c.get("source") and not c["source"].endswith(".monitor")
            and c.get("monitor_of", "n/a") == "n/a"]


def assign_port(source: str, ports: dict) -> int:
    if source not in ports:
        used = set(ports.values())
        p = PORT_BASE
        while p in used:
            p += 1
        ports[source] = p
        write_json(DIR / "ports.json", ports)
    return ports[source]


class LineIn:
    """One input: a Sendspin source client, its listener, capture and line sense."""

    def __init__(self, source: str, name: str, port: int, settings: dict):
        self.source, self.name, self.port = source, name, port
        self.threshold = float(settings.get("threshold_db", DEFAULTS["threshold_db"]))
        self.hold_s = float(settings.get("hold_s", DEFAULTS["hold_s"]))
        self.dir = DIR / input_id(source)
        self.client = None
        self.listener = None
        self.capture = None
        self.tasks = []
        self.level_db = None
        self.signal = SignalState.ABSENT
        self.above_since = None
        self.below_since = time.monotonic()
        self.sent = (None, None)           # (connection token, signal) last reported
        self.server = None
        self.streaming = False
        self.pairing_code = None
        self.error = None

    # -- set-up ----------------------------------------------------------------------
    async def start(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        idf = self.dir / "identity.json"
        data = read_json(idf, {})
        if data.get("private_key"):
            identity = Identity.from_private_bytes(b64url_decode(data["private_key"]))
        else:
            identity = Identity.generate()
            write_json(idf, {"private_key": b64url_encode(identity.private_bytes)})
        store = await FileClientPairingStore.open(self.dir / "pairing.json")
        cfg = await store.get_pairing_config()
        if not cfg.unpaired_access_enabled:
            from dataclasses import replace
            await store.store_pairing_config(replace(cfg, unpaired_access_enabled=True))
        self.client = SendspinClient(
            identity=identity, client_name=self.name, roles=[Roles.SOURCE],
            pairing_store=store,
            source_support=ClientHelloSourceSupport(
                features=ClientHelloSourceFeatures(line_sense=True)),
            pairing_support=PairingSupport(pairing_code_display=self._show_code))
        self.client.add_server_command_listener(self._on_command)
        self.client.add_disconnect_listener(self._on_disconnect)
        self.client.add_pairing_abort_listener(
            lambda reason: log.warning("%s: pairing failed (%s)", self.name, reason.value))
        self.listener = ClientListener(
            client_id=identity.peer_id, on_connection=self._on_connection,
            port=self.port, client_name=self.name)
        await self.listener.start()
        self.tasks.append(asyncio.create_task(self._capture_loop()))
        log.info("%s: listening on port %d for Music Assistant (input %s)",
                 self.name, self.port, self.source)

    async def stop(self) -> None:
        for t in self.tasks:
            t.cancel()
        await self._end_stream()
        if self.client is not None:
            try:
                await self.client.disconnect()
            except Exception:  # noqa: BLE001
                pass
        if self.listener is not None:
            await self.listener.stop()
        log.info("%s: stopped", self.name)

    # -- connection --------------------------------------------------------------------
    async def _show_code(self, code, *, grouped=None):
        self.pairing_code = grouped
        if grouped:
            log.info("%s: pairing code %s (enter it in Music Assistant)", self.name, grouped)

    async def _on_connection(self, ws) -> None:
        task = asyncio.create_task(self.client.attach_websocket(ws))
        await asyncio.sleep(0)
        try:
            await task
        except Exception as e:  # noqa: BLE001
            log.warning("%s: connection ended: %s", self.name, e)

    def _on_disconnect(self) -> None:
        if self.server:
            log.info("%s: Music Assistant disconnected", self.name)
        self.server, self.streaming, self.capture = None, False, None
        self.sent = (None, None)

    def _on_command(self, payload) -> None:
        src = getattr(payload, "source", None)
        if src is not None:
            asyncio.create_task(self._handle_source_command(src.command))

    async def _handle_source_command(self, command: str) -> None:
        if command == "start":
            try:
                self.capture = self.client.create_source_capture(SupportedAudioFormat(
                    codec=AudioCodec.PCM, channels=CHANNELS, sample_rate=RATE, bit_depth=BPS * 8))
                await self.capture.start()
                self.streaming = True
                log.info("%s: streaming to Music Assistant", self.name)
            except Exception as e:  # noqa: BLE001
                log.warning("%s: could not start streaming: %s", self.name, e)
                self.capture, self.streaming = None, False
        elif command == "stop":
            await self._end_stream()
            log.info("%s: streaming stopped", self.name)

    async def _end_stream(self) -> None:
        cap, self.capture, self.streaming = self.capture, None, False
        if cap is not None:
            try:
                await cap.stop()
            except Exception:  # noqa: BLE001
                pass

    # -- capture and line sense ------------------------------------------------------------
    async def _capture_loop(self) -> None:
        chunk = RATE * READ_MS // 1000 * FRAME
        while True:
            proc = None
            try:
                proc = await asyncio.create_subprocess_exec(
                    "parec", f"--device={self.source}", "--format=s16le", f"--rate={RATE}",
                    f"--channels={CHANNELS}", "--latency-msec=20", "--raw",
                    "--client-name=AirPlay 2 Multiroom Audio line-in",
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                self.error = None
                peak, n = 0, 0
                while True:
                    data = await proc.stdout.readexactly(chunk)
                    a = array.array("h", data)
                    peak = max(peak, max(a), -min(a))
                    n += READ_MS
                    if n >= LEVEL_MS:
                        self._level(peak)
                        peak, n = 0, 0
                    await self._report_signal()
                    if self.streaming and self.capture is not None:
                        try:
                            await self.capture.feed(
                                data, self.client.now_us() - READ_MS * 1000 - CAPTURE_LATENCY_US)
                        except Exception as e:  # noqa: BLE001
                            log.info("%s: stream ended (%s)", self.name, e)
                            self.capture, self.streaming = None, False
            except asyncio.CancelledError:
                if proc and proc.returncode is None:
                    proc.kill()
                raise
            except (asyncio.IncompleteReadError, OSError) as e:
                err = ""
                if proc is not None:
                    if proc.returncode is None:
                        proc.kill()
                    err = (await proc.stderr.read()).decode(errors="replace").strip()
                self.error = f"capture stopped: {err or e}"
                log.warning("%s: %s; retrying in 5 s", self.name, self.error)
                await asyncio.sleep(5)

    def _level(self, peak: int) -> None:
        self.level_db = 20 * math.log10(peak / 32768) if peak else -96.0
        now = time.monotonic()
        if self.level_db >= self.threshold:
            self.below_since = None
            self.above_since = self.above_since or now
            if self.signal is SignalState.ABSENT and now - self.above_since >= PRESENT_AFTER_S:
                self.signal = SignalState.PRESENT
                log.info("%s: signal present (%.0f dBFS)", self.name, self.level_db)
        else:
            self.above_since = None
            self.below_since = self.below_since or now
            if self.signal is SignalState.PRESENT and now - self.below_since >= self.hold_s:
                self.signal = SignalState.ABSENT
                log.info("%s: signal absent (below %.0f dBFS for %.0f s)",
                         self.name, self.threshold, self.hold_s)

    async def _report_signal(self) -> None:
        c = self.client
        info = c.server_info if c is not None and c.connected else None
        if info is None:
            return
        if self.server != info.name:
            self.server = info.name
            log.info("%s: connected to %s", self.name, info.name)
        token = id(info)
        if self.sent == (token, self.signal):
            return
        try:
            await c._admitted_connection.send_source_signal(self.signal)  # noqa: SLF001
            self.sent = (token, self.signal)
        except Exception:  # noqa: BLE001
            pass        # role not active yet (unapproved, or still activating)

    def status(self) -> dict:
        c = self.client
        paired = None
        if c is not None and c.connected and c.noise_psk is not None:
            paired = getattr(c.noise_psk.category, "value", "") == "long_term"
        connected = bool(c is not None and c.connected)
        state = ("streaming" if self.streaming else "connected" if connected and self.sent[0]
                 else "waiting_approval" if connected else "listening")
        return {"name": self.name, "port": self.port, "state": state,
                "server": self.server if connected else None, "paired": paired,
                "signal": self.signal.value, "level_db": None if self.level_db is None
                else round(self.level_db), "pairing_code": self.pairing_code,
                "error": self.error, "client_id": c.identity.peer_id if c else None}


async def main() -> None:
    DIR.mkdir(parents=True, exist_ok=True)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    running: dict = {}
    ports = read_json(DIR / "ports.json", {})
    last_found = None
    while not stop.is_set():
        inputs = await list_inputs()
        names = {i["source"]: i["description"] for i in inputs}
        if inputs != last_found:
            write_json(DIR / "discovered.json", inputs)
            if last_found is None or {i["source"] for i in inputs} != {i["source"] for i in last_found}:
                log.info("inputs: %s", ", ".join(i["description"] for i in inputs) or "none")
            last_found = inputs
        settings = read_json(DIR / "settings.json", {})
        want = {s for s in enabled_sources() if s in names}
        for s in list(running):
            li = running[s]
            st = settings.get(s, {})
            name = st.get("name") or names.get(s, s)
            changed = (name != li.name
                       or float(st.get("threshold_db", DEFAULTS["threshold_db"])) != li.threshold
                       or float(st.get("hold_s", DEFAULTS["hold_s"])) != li.hold_s)
            if s not in want or changed:
                log.info("%s: %s", li.name, "switched off" if s not in want else "settings changed")
                await li.stop()
                del running[s]
        for s in want - set(running):
            st = settings.get(s, {})
            li = LineIn(s, st.get("name") or names[s], assign_port(s, ports), st)
            try:
                await li.start()
                running[s] = li
            except Exception as e:  # noqa: BLE001
                log.warning("%s: could not start: %s", li.name, e)
                await li.stop()
        write_json(DIR / "status.json", {s: li.status() for s, li in running.items()})
        try:
            await asyncio.wait_for(stop.wait(), 2)
        except asyncio.TimeoutError:
            pass
    for li in running.values():
        await li.stop()
    write_json(DIR / "status.json", {})


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
