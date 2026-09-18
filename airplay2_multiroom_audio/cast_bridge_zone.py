#!/usr/bin/env python3
"""
cast_bridge_zone.py — one zone's worker: raw PCM in from a FIFO
(written by this same container's own shairport-sync pipe-output
AirPlay 2 receiver — see cast_bridge_manager.py, which spawns both
that receiver and this worker per discovered Cast device), encoded by
ffmpeg, served over HTTP, and pushed to a Google Cast (Chromecast)
device via pychromecast's standard media LOAD flow.

Architecture note (read before changing STREAM_FORMAT): earlier
versions of this file ran ONE long-lived ffmpeg process and fanned its
encoded stdout out to every HTTP subscriber (a BroadcastStream of
already-encoded audio). That looked fine on paper but was broken for
any format with a stream-start-only header (FLAC's STREAMINFO block,
WAV's RIFF/fmt chunk): ffmpeg only ever writes that header once, at
the very start of the process, so only the FIRST subscriber ever saw
it. Every later reconnect — which the watchdog below triggers
routinely whenever Cast playback isn't actually PLAYING — subscribed
to the same already-running ffmpeg mid-stream and got headerless,
undecodable audio, which produced a connect/stall/reconnect loop that
looked like "no sound." Switching to MP3 (self-contained per-frame
headers) masked the symptom but real deployment logs showed the exact
same 30-second stall/reconnect cycle even with MP3 — so the header bug
was real but not the whole story; a plain curl of the HTTP stream
during an active AirPlay session then confirmed our own ffmpeg/HTTP
serving pipeline produces valid, audible audio, isolating the rest of
the problem to how the Cast receiver consumes an indefinite-length
HTTP response (most likely wanting either a known Content-Length or
chunked transfer framing, neither of which the old handler sent).

Current design fixes both issues at once:
  - PcmBroadcast is a persistent background thread that is the FIFO's
    sole, permanent reader. It never stops reading for lack of HTTP
    subscribers, and when shairport-sync's pipe writer closes (FIFO
    EOF), it just reopens the FIFO (which blocks until a new writer
    shows up) and keeps going, instead of exiting. It fans out RAW PCM
    to subscribers — no format-specific framing at this layer at all.
  - Every HTTP connection (including every reconnect) gets its OWN
    fresh, short-lived ffmpeg process, fed PCM from a brand-new
    subscription to the PCM broadcast by a small per-connection feeder
    thread. Because that ffmpeg process just started, its very first
    output bytes are always a fresh, valid format header — for WAV,
    FLAC, or MP3 alike. This is what makes WAV/FLAC actually safe to
    use now, not just MP3.
  - The HTTP response is sent with Transfer-Encoding: chunked (the
    correct HTTP/1.1 way to stream a body of unknown total length)
    instead of just closing the connection at EOF with no framing
    hint at all — addressing the other candidate cause from the curl
    investigation.

WAV is the default now per real-network testing: this network is
stable enough that WAV's larger bandwidth isn't a concern, and it
avoids MP3's encoder lookahead/bit-reservoir delay (the last sizeable
buffer between a volume change reaching shairport-sync's pipe and it
reaching the speaker). FLAC remains available (also low-delay) if
bandwidth ever becomes a concern; see FORMAT_SPECS below.

Why this exists instead of OwnTone's built-in Cast output: OwnTone's
cast.c falls back to Google's "Chrome Audio Mirroring" receiver app
(appId 85CDB22F) for any Cast target that doesn't support direct
media LOAD (in practice: Cast groups and stereo pairs). That app is a
screen-mirroring session, not meant for standalone continuous audio,
and closes itself after a few seconds on Google Home stereo-pair
groups. Casting a plain HTTP audio stream to the standard Default
Media Receiver app (which is what play_media() below does) avoids
that receiver entirely.

Run as its own process (see cast_bridge_manager.py) so one zone's
ffmpeg/Cast trouble can't take another zone down.
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

# How long a subscriber's chunk queue can lag behind before we drop
# its oldest data rather than let the publishing thread (the FIFO
# reader, or a per-connection ffmpeg stdout reader) block on a slow
# consumer forever. Kept small deliberately: a large ceiling here
# directly undermines volume-change responsiveness, since audio
# already sitting in a queue plays out at whatever volume was in
# effect when it was queued.
MAX_QUEUE_CHUNKS = 40
PCM_CHUNK_SIZE = 4096
ENCODED_CHUNK_SIZE = 4096

# See the module docstring's "Architecture note" for why WAV is now
# safe to use (each HTTP connection gets its own fresh ffmpeg process,
# so it always gets a fresh header) and why it's the default (stable
# network, avoids MP3's encoder delay).
STREAM_FORMAT = "wav"
FORMAT_SPECS = {
    # -sample_fmt s32 pins the FLAC encoder to full 32-bit passthrough
    # of shairport-sync's native AP2 S32_LE output — without it,
    # ffmpeg's flac encoder can silently pick a narrower sample
    # format, quietly discarding depth rather than actually failing.
    "flac": {"content_type": "audio/flac", "ext": "flac",
             "ffmpeg_args": ["-f", "flac", "-sample_fmt", "s32", "-compression_level", "0"]},
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


class Broadcast:
    """Fans out chunks of bytes (from whatever source `publish()` is fed
    by) to any number of subscribers, each with its own bounded queue.

    Generic over what the bytes actually are — used both for the raw
    PCM coming off the FIFO (PcmBroadcast, one instance per zone, long
    lived) and could equally carry encoded audio; kept as one small
    class rather than duplicating the subscribe/unsubscribe/publish
    bookkeeping.
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
                    # Slow/stalled subscriber — drop the oldest data
                    # rather than let the publisher block on it.
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

    @staticmethod
    def read_one(entry, timeout=5):
        """Blocks until a chunk is available, the subscriber is
        closed, or `timeout` elapses (returns None on timeout/closed
        so callers can re-check their own stop conditions)."""
        with entry["cond"]:
            while not entry["q"] and not entry["closed"]:
                entry["cond"].wait(timeout=timeout)
            if entry["closed"]:
                return None
            if not entry["q"]:
                return None
            return entry["q"].pop(0)


class PcmBroadcast(Broadcast):
    """Owns the FIFO. A single background thread is this FIFO's sole,
    permanent reader — started once and never stopped for lack of HTTP
    subscribers, so shairport-sync's pipe writer is never left blocked
    on a full/unread pipe.

    Handles the FIFO's EOF-on-writer-close behavior (a POSIX FIFO
    delivers a zero-length read once every writer has closed it) by
    reopening the FIFO — which blocks until a new writer opens it —
    and continuing, rather than treating that as a fatal condition.
    """

    def __init__(self, fifo_path: str, zone_name: str):
        super().__init__()
        self.fifo_path = fifo_path
        self.zone_name = zone_name
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if not os.path.exists(self.fifo_path):
            os.makedirs(os.path.dirname(self.fifo_path), exist_ok=True)
            os.mkfifo(self.fifo_path)
        self._thread = threading.Thread(target=self._run, daemon=True,
                                         name=f"pcm-fifo-{self.zone_name}")
        self._thread.start()

    def _run(self):
        while not self._stop.is_set():
            log.info("[%s] Opening FIFO for reading (blocks until a writer connects)",
                      self.zone_name)
            try:
                # O_RDONLY blocks until a writer opens the other end —
                # exactly the "wait for the next AirPlay session" state
                # we want between songs/sessions.
                fd = os.open(self.fifo_path, os.O_RDONLY)
            except OSError:
                log.exception("[%s] Failed to open FIFO, retrying shortly", self.zone_name)
                time.sleep(1)
                continue

            log.info("[%s] FIFO writer connected, reading PCM", self.zone_name)
            try:
                while not self._stop.is_set():
                    chunk = os.read(fd, PCM_CHUNK_SIZE)
                    if not chunk:
                        # Writer closed — normal at the end of an
                        # AirPlay session. Reopen and wait for the
                        # next one rather than exiting.
                        log.info("[%s] FIFO writer closed, reopening", self.zone_name)
                        break
                    self.publish(chunk)
            finally:
                os.close(fd)

    def stop(self):
        self._stop.set()
        self.close_all()


def make_stream_handler(pcm: PcmBroadcast, zone_name: str, format_spec: dict,
                         sample_format: str, sample_rate: int, channels: int):
    stream_path = f"/stream.{format_spec['ext']}"

    class StreamHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            log.debug("[%s] http: %s", zone_name, fmt % args)

        def do_GET(self):
            if self.path.rstrip("/") != stream_path:
                self.send_response(404)
                self.end_headers()
                return

            # Every connection — including every reconnect the
            # watchdog forces — gets its own brand-new ffmpeg process,
            # so its first output bytes are always a fresh, valid
            # format header. See the module docstring.
            cmd = [
                "ffmpeg",
                "-hide_banner", "-loglevel", "warning",
                "-fflags", "nobuffer", "-flags", "low_delay",
                "-f", sample_format,
                "-ar", str(sample_rate),
                "-ac", str(channels),
                "-i", "pipe:0",
                *format_spec["ffmpeg_args"],
                "pipe:1",
            ]
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE)

            pcm_entry = pcm.subscribe()
            stop_feeding = threading.Event()

            def feed():
                try:
                    while not stop_feeding.is_set():
                        chunk = Broadcast.read_one(pcm_entry, timeout=5)
                        if chunk is None:
                            if stop_feeding.is_set():
                                break
                            continue
                        try:
                            proc.stdin.write(chunk)
                        except (BrokenPipeError, OSError):
                            break
                finally:
                    try:
                        proc.stdin.close()
                    except OSError:
                        pass

            feeder = threading.Thread(target=feed, daemon=True,
                                       name=f"feed-{zone_name}")
            feeder.start()

            log.info("[%s] Cast device connected for stream (new ffmpeg pid=%s)",
                      zone_name, proc.pid)

            self.send_response(200)
            self.send_header("Content-Type", format_spec["content_type"])
            self.send_header("Cache-Control", "no-cache")
            # Length is unknown up front (this is a live, indefinite
            # stream) — chunked transfer framing is the correct
            # HTTP/1.1 way to signal that, rather than sending no
            # length hint at all and relying on the peer to treat
            # connection-close as end-of-body.
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()

            try:
                while True:
                    out = proc.stdout.read(ENCODED_CHUNK_SIZE)
                    if not out:
                        if proc.poll() is not None:
                            break
                        continue
                    self.wfile.write(b"%x\r\n" % len(out))
                    self.wfile.write(out)
                    self.wfile.write(b"\r\n")
                # Final chunk marker.
                self.wfile.write(b"0\r\n\r\n")
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                stop_feeding.set()
                pcm.unsubscribe(pcm_entry)
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                log.info("[%s] Cast device disconnected from stream (ffmpeg pid=%s stopped)",
                          zone_name, proc.pid)

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
        self.sample_rate = int(zone.get("sample_rate", 48000))
        self.channels = int(zone.get("channels", 2))
        # Must exactly match the byte layout shairport-sync's pipe
        # backend actually writes (see cast_bridge_manager.py's
        # PIPE_OUTPUT_FORMAT comment) — a mismatch here produces
        # garbled audio, not just a quality loss.
        self.sample_format = zone.get("sample_format", "s32le")
        self.format_spec = FORMAT_SPECS[zone.get("stream_format", STREAM_FORMAT)]

        self._pcm = PcmBroadcast(self.fifo, self.name)
        self._httpd = None
        self._stop = threading.Event()

    # -- HTTP server for this zone's stream ----------------------------
    def _start_http(self):
        handler = make_stream_handler(self._pcm, self.name, self.format_spec,
                                       self.sample_format, self.sample_rate, self.channels)
        self._httpd = ThreadingHTTPServer(("0.0.0.0", self.http_port), handler)
        threading.Thread(target=self._httpd.serve_forever, daemon=True,
                          name=f"http-{self.name}").start()
        log.info("[%s] Serving stream at :%d/stream.%s", self.name, self.http_port,
                  self.format_spec["ext"])

    # -- Chromecast discovery + play_media + watchdog -------------------
    def _get_chromecast(self):
        # Build the Chromecast object directly from the (host, port,
        # uuid, model_name, friendly_name) tuple cast_bridge_manager.py
        # captured at its startup discovery — no zeroconf involved at
        # all for this path. This deliberately avoids
        # get_listed_chromecasts(): that call hands back a Chromecast
        # object that keeps connecting asynchronously on a background
        # thread against the zeroconf instance it came with, and
        # immediately calling browser.stop_discovery() (as an earlier
        # version of this code did) killed that thread mid-connect —
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
        self._pcm.start()
        self._start_http()

        cast = None
        last_healthy = time.time()

        while not self._stop.is_set():
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
        self._pcm.stop()
        if self._httpd:
            self._httpd.shutdown()


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
