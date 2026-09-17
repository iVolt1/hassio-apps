#!/usr/bin/env python3
"""
cast_bridge_zone.py — one zone's worker: raw PCM in from a FIFO
(written by this addon's own shairport-sync pipe-output AirPlay 2
receiver — see launcher.py), encoded by ffmpeg, served over HTTP, and
pushed to a Google Cast (Chromecast) device via pychromecast's
standard media LOAD flow.

Encoded as FLAC by default rather than MP3: FLAC has negligible
algorithmic encoding delay (no bit-reservoir/lookahead the way an
MP3/LAME encoder has), which matters here because it's the last
sizeable buffer between a volume change reaching shairport-sync's
pipe and it actually reaching the speaker — see STREAM_FORMAT below
if your Cast hardware doesn't take FLAC well and you need to fall
back to "wav" (also low-latency, more universally supported, larger
bandwidth) or "mp3" (smallest bandwidth, worst latency).

Why this exists instead of OwnTone's built-in Cast output: OwnTone's
cast.c falls back to Google's "Chrome Audio Mirroring" receiver app
(appId 85CDB22F) for any Cast target that doesn't support direct
media LOAD (in practice: Cast groups and stereo pairs). That app is a
screen-mirroring session, not meant for standalone continuous audio,
and closes itself after a few seconds on Google Home stereo-pair
groups. Casting a plain HTTP audio stream to the standard Default
Media Receiver app (which is what play_media() below does) avoids
that receiver entirely.

Run as its own process (see launcher.py) so one zone's ffmpeg/Cast
trouble can't take another zone down.
"""

import logging
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pychromecast

log = logging.getLogger("cast_bridge_zone")

# How long a client's chunk queue can lag behind before we drop it as
# stalled, rather than let it back up ffmpeg's reader thread forever.
# Kept small deliberately: at the old 200 chunks x 4096 bytes, a
# client that ever reads even slightly slower than realtime could
# silently accumulate up to ~800KB of backlog (tens of seconds at MP3
# bitrate) — audio already sitting in that queue plays out at
# whatever volume was in effect when it was queued, so a large ceiling
# here directly undermines volume-change responsiveness. This favors
# dropping old data over building backlog.
MAX_QUEUE_CHUNKS = 20
CHUNK_SIZE = 4096

# One-line format switch. FLAC has the least encoding latency of the
# three (no bit-reservoir/lookahead); WAV is uncompressed (also very
# low latency, larger bandwidth, safest compatibility bet); MP3 has
# the most encoding latency but smallest bandwidth. All are commonly
# supported by Cast's Default Media Receiver, but test on your actual
# hardware — fall back to "wav" if a device won't play "flac".
STREAM_FORMAT = "flac"
FORMAT_SPECS = {
    "flac": {"content_type": "audio/flac", "ext": "flac",
             "ffmpeg_args": ["-f", "flac", "-compression_level", "0"]},
    "wav": {"content_type": "audio/wav", "ext": "wav",
            "ffmpeg_args": ["-f", "wav"]},
    "mp3": {"content_type": "audio/mpeg", "ext": "mp3",
            "ffmpeg_args": ["-f", "mp3", "-b:a", "192k"]},
}

# How often the watchdog checks Cast playback state and re-issues
# play_media() if it's not actually playing.
WATCHDOG_INTERVAL_SECONDS = 10
# How long to tolerate a non-PLAYING state before treating it as
# stalled and reconnecting (Cast is briefly BUFFERING/IDLE between
# tracks in normal use, so this needs some slack).
STALL_TOLERANCE_SECONDS = 30


def local_ip_for_route_to(host: str) -> str:
    """Best-effort local IP address a peer at `host` would see us as.

    Doesn't actually send any packets (UDP connect() just picks a
    route) — used because under host_network:true the container's
    "own" IP is genuinely the host's LAN IP, and this is a portable
    way to find which interface/address that is without depending on
    interface names.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((host, 80))
        return s.getsockname()[0]
    finally:
        s.close()


class BroadcastStream:
    """Fans out one ffmpeg stdout stream to any number of HTTP clients.

    A single reader thread drains ffmpeg's stdout continuously (so
    ffmpeg is never left blocked on a full pipe just because no HTTP
    client happens to be connected right now) and copies each chunk
    into every currently-subscribed client queue.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._subscribers = []

    def subscribe(self):
        q = []
        cond = threading.Condition()
        entry = {"q": q, "cond": cond, "closed": False}
        with self._lock:
            self._subscribers.append(entry)
        return entry

    def unsubscribe(self, entry):
        with self._lock:
            if entry in self._subscribers:
                self._subscribers.remove(entry)

    def publish(self, chunk: bytes):
        with self._lock:
            subs = list(self._subscribers)
        for entry in subs:
            with entry["cond"]:
                if len(entry["q"]) >= MAX_QUEUE_CHUNKS:
                    # Slow/stalled client — drop the oldest data rather
                    # than let this reader thread block on it.
                    entry["q"].pop(0)
                entry["q"].append(chunk)
                entry["cond"].notify_all()

    def close_all(self):
        with self._lock:
            subs = list(self._subscribers)
        for entry in subs:
            with entry["cond"]:
                entry["closed"] = True
                entry["cond"].notify_all()


def make_stream_handler(broadcast: BroadcastStream, zone_name: str, format_spec: dict):
    stream_path = f"/stream.{format_spec['ext']}"

    class StreamHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            log.debug("[%s] http: %s", zone_name, fmt % args)

        def do_GET(self):
            if self.path.rstrip("/") != stream_path:
                self.send_response(404)
                self.end_headers()
                return

            self.send_response(200)
            self.send_header("Content-Type", format_spec["content_type"])
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()

            entry = broadcast.subscribe()
            log.info("[%s] Cast device connected for stream", zone_name)
            try:
                while True:
                    with entry["cond"]:
                        while not entry["q"] and not entry["closed"]:
                            entry["cond"].wait(timeout=5)
                        if entry["closed"]:
                            break
                        chunk = entry["q"].pop(0)
                    self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                broadcast.unsubscribe(entry)
                log.info("[%s] Cast device disconnected from stream", zone_name)

    return StreamHandler


class CastZoneWorker:
    def __init__(self, zone: dict):
        self.name = zone["name"]
        self.fifo = zone["fifo"]
        self.chromecast_name = zone.get("chromecast_name")
        self.chromecast_host = zone.get("chromecast_host")
        self.chromecast_port = zone.get("chromecast_port", 8009)
        self.chromecast_uuid = zone.get("chromecast_uuid")
        self.chromecast_model_name = zone.get("chromecast_model_name", "")
        self.http_port = int(zone["http_port"])
        self.sample_rate = int(zone.get("sample_rate", 44100))
        self.channels = int(zone.get("channels", 2))
        self.format_spec = FORMAT_SPECS[zone.get("stream_format", STREAM_FORMAT)]

        self._ffmpeg = None
        self._broadcast = BroadcastStream()
        self._httpd = None
        self._stop = threading.Event()

    # -- ffmpeg: raw PCM from the FIFO -> continuous encoded audio on stdout --
    def _start_ffmpeg(self):
        if not os.path.exists(self.fifo):
            os.makedirs(os.path.dirname(self.fifo), exist_ok=True)
            os.mkfifo(self.fifo)

        cmd = [
            "ffmpeg",
            "-hide_banner", "-loglevel", "warning",
            "-fflags", "nobuffer", "-flags", "low_delay",
            "-f", "s16le",
            "-ar", str(self.sample_rate),
            "-ac", str(self.channels),
            "-i", self.fifo,
            *self.format_spec["ffmpeg_args"],
            "pipe:1",
        ]
        log.info("[%s] Starting ffmpeg: %s", self.name, " ".join(cmd))
        # ffmpeg blocks reading the FIFO until shairport-sync opens it
        # for writing — that's normal FIFO behavior, not a hang.
        self._ffmpeg = subprocess.Popen(cmd, stdout=subprocess.PIPE, stdin=subprocess.DEVNULL)

        def pump():
            assert self._ffmpeg and self._ffmpeg.stdout
            while not self._stop.is_set():
                chunk = self._ffmpeg.stdout.read(CHUNK_SIZE)
                if not chunk:
                    break
                self._broadcast.publish(chunk)
            log.warning("[%s] ffmpeg stdout closed", self.name)

        threading.Thread(target=pump, daemon=True, name=f"ffmpeg-pump-{self.name}").start()

    def _ffmpeg_alive(self) -> bool:
        return self._ffmpeg is not None and self._ffmpeg.poll() is None

    # -- HTTP server for this zone's stream ----------------------------
    def _start_http(self):
        handler = make_stream_handler(self._broadcast, self.name, self.format_spec)
        self._httpd = ThreadingHTTPServer(("0.0.0.0", self.http_port), handler)
        threading.Thread(target=self._httpd.serve_forever, daemon=True,
                          name=f"http-{self.name}").start()
        log.info("[%s] Serving stream at :%d/stream.%s", self.name, self.http_port,
                  self.format_spec["ext"])

    # -- Chromecast discovery + play_media + watchdog -------------------
    def _get_chromecast(self):
        # Build the Chromecast object directly from the (host, port,
        # uuid, model_name, friendly_name) tuple launcher.py captured
        # at its startup discovery — no zeroconf involved at all for
        # this path. This deliberately avoids get_listed_chromecasts():
        # that call hands back a Chromecast object that keeps
        # connecting asynchronously on a background thread against the
        # zeroconf instance it came with, and immediately calling
        # browser.stop_discovery() (as an earlier version of this code
        # did) killed that thread mid-connect —
        # "AssertionError: Zeroconf instance loop must be running" —
        # which is what was actually causing every play attempt to
        # time out.
        if self.chromecast_host and self.chromecast_uuid:
            try:
                cast = pychromecast.get_chromecast_from_host(
                    (self.chromecast_host, self.chromecast_port,
                     self.chromecast_uuid, self.chromecast_model_name, self.chromecast_name),
                    timeout=10,
                )
                return cast
            except Exception:
                log.warning("[%s] Direct connect to %s failed, falling back to discovery",
                            self.name, self.chromecast_host)

        # Fallback: a full, blocking discovery scan. get_chromecasts()
        # manages its own zeroconf instance's lifecycle correctly
        # (unlike get_listed_chromecasts + an immediate stop_discovery),
        # so it's safe to use here even though it's slower.
        if self.chromecast_name:
            casts, browser = pychromecast.get_chromecasts(timeout=10)
            browser.stop_discovery()
            for c in casts:
                if c.cast_info.friendly_name == self.chromecast_name:
                    return c

        return None

    def _stream_url(self) -> str:
        target_for_route = self.chromecast_host or "8.8.8.8"
        ip = local_ip_for_route_to(target_for_route)
        return f"http://{ip}:{self.http_port}/stream.{self.format_spec['ext']}"

    def _play(self, cast) -> bool:
        try:
            cast.wait(timeout=15)
            mc = cast.media_controller
            mc.play_media(self._stream_url(), self.format_spec["content_type"],
                           stream_type="LIVE", title=self.name)
            mc.block_until_active(timeout=15)
            log.info("[%s] play_media issued to %s", self.name, cast.name)
            return True
        except Exception:
            log.exception("[%s] Failed to start playback on Cast device", self.name)
            return False

    def run(self):
        self._start_ffmpeg()
        self._start_http()

        cast = None
        last_healthy = time.time()

        while not self._stop.is_set():
            if not self._ffmpeg_alive():
                log.warning("[%s] ffmpeg died, restarting", self.name)
                self._start_ffmpeg()

            if cast is None:
                cast = self._get_chromecast()
                if cast is None:
                    log.warning("[%s] Cast device '%s' not found, retrying...",
                                self.name, self.chromecast_name or self.chromecast_host)
                    time.sleep(WATCHDOG_INTERVAL_SECONDS)
                    continue
                if not self._play(cast):
                    cast = None
                    time.sleep(WATCHDOG_INTERVAL_SECONDS)
                    continue
                last_healthy = time.time()

            time.sleep(WATCHDOG_INTERVAL_SECONDS)

            try:
                cast.media_controller.update_status()
                state = cast.media_controller.status.player_state
            except Exception:
                log.warning("[%s] Lost connection to Cast device, reconnecting", self.name)
                cast = None
                continue

            if state in ("PLAYING", "BUFFERING"):
                last_healthy = time.time()
            elif time.time() - last_healthy > STALL_TOLERANCE_SECONDS:
                log.warning("[%s] Playback stalled (state=%s), re-issuing play_media",
                            self.name, state)
                if self._play(cast):
                    last_healthy = time.time()
                else:
                    cast = None

    def stop(self):
        self._stop.set()
        self._broadcast.close_all()
        if self._httpd:
            self._httpd.shutdown()
        if self._ffmpeg:
            self._ffmpeg.terminate()


def main():
    import json
    zone = json.loads(sys.argv[1])
    logging.basicConfig(
        level=logging.INFO,
        format=f"%(asctime)s [{zone['name']}] %(levelname)s %(message)s",
    )
    worker = CastZoneWorker(zone)
    try:
        worker.run()
    except KeyboardInterrupt:
        worker.stop()


if __name__ == "__main__":
    main()
