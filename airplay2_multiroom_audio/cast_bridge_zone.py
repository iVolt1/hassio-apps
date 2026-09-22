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
serving pipeline produces valid, audible audio on its own.

Current design:
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
  - The HTTP response is a plain, close-terminated body (Connection:
    close, no Content-Length, no Transfer-Encoding) — deliberately
    matching the exact framing the curl test proved works. An earlier
    revision of this file tried Transfer-Encoding: chunked here, but
    BaseHTTPRequestHandler answers as HTTP/1.0 unless protocol_version
    is explicitly raised, and chunked framing on an HTTP/1.0 response
    is invalid — a client that (correctly, for HTTP/1.0) does not
    chunk-decode it receives our literal "<hex-length>\r\n...\r\n"
    chunk framing as corrupted audio bytes instead. That is the
    suspected reason WAV kept cycling even after the fresh-ffmpeg-
    per-connection fix went in.

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
import select
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

# Hard ceiling on how long a single HTTP connection's ffmpeg can go
# without producing any output before we give up on it. Belt-and-
# suspenders against the same failure mode the has_writer check in
# CastZoneWorker.run() addresses at the source: without SOME bound
# here, a connection that started before any real PCM existed (or
# whose PCM source went quiet mid-stream) would block this handler
# thread — and its ffmpeg subprocess — forever, which is exactly how
# thousands of never-cleaned-up threads/processes accumulated into a
# multi-gigabyte leak in production.
STREAM_READ_TIMEOUT_SECONDS = 20

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
        # True only while a real AirPlay session is actively writing
        # to this FIFO. The watchdog in CastZoneWorker.run() uses this
        # to tell "nothing is playing right now" (expected, IDLE is
        # fine) apart from "something IS playing but Cast dropped it"
        # (an actual stall worth reconnecting for) — see run()'s
        # comment for why conflating the two caused a real memory
        # leak in production.
        self.has_writer = False

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
            self.has_writer = True
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
                self.has_writer = False
                os.close(fd)

    def stop(self):
        self._stop.set()
        self.has_writer = False
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
            self.send_header("Connection", "close")
            self.end_headers()

            try:
                last_data_time = time.time()
                while True:
                    ready, _, _ = select.select([proc.stdout], [], [], 1.0)
                    if not ready:
                        if proc.poll() is not None:
                            break
                        if time.time() - last_data_time > STREAM_READ_TIMEOUT_SECONDS:
                            # ffmpeg is alive but has produced nothing
                            # for too long (e.g. connected before any
                            # AirPlay session started, or the FIFO
                            # went quiet mid-stream). Give up on this
                            # connection rather than block this thread
                            # (and its ffmpeg process) forever — an
                            # earlier version of this handler had no
                            # such bound, which is how a run of
                            # doomed-from-the-start connections turned
                            # into a slow, unbounded resource leak.
                            log.warning("[%s] No stream data for %ds, giving up on this connection",
                                        zone_name, STREAM_READ_TIMEOUT_SECONDS)
                            break
                        continue
                    out = proc.stdout.read(ENCODED_CHUNK_SIZE)
                    if not out:
                        if proc.poll() is not None:
                            break
                        continue
                    last_data_time = time.time()
                    self.wfile.write(out)
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
        # Groups and stereo pairs (this is exactly what "MBR stereo pair"
        # and "Master bath pair" are) don't expose a stable per-target
        # host the way a single physical Chromecast does -- pychromecast's
        # own discovery leaves cast_info.host as the literal string
        # "unknown" for them, and cast_bridge_manager.py captured that
        # as-is into chromecast_host at its startup discovery.
        # get_chromecast_from_host() doesn't raise when given "unknown" as
        # the host -- it happily builds a Chromecast object pointed at it
        # -- so the failure only ever surfaced later, inside
        # update_status(), as "Chromecast unknown:8009 is connecting...",
        # every single watchdog cycle, forever: retrying _get_chromecast()
        # with the same persisted "unknown" host just repeats the same
        # broken attempt. Skip straight to the discovery fallback below
        # whenever the captured host isn't a real, usable value.
        if (self.chromecast_host and self.chromecast_host != "unknown"
                and self.chromecast_uuid):
            try:
                cast = pychromecast.get_chromecast_from_host(
                    (self.chromecast_host, self.chromecast_port,
                     self.chromecast_uuid, self.chromecast_model_name, self.chromecast_name),
                    timeout=10,
                )
                return cast
            except Exception:
                # exc_info=True here is deliberate and load-bearing, not
                # decoration: the bare "except Exception" below used to
                # swallow the real error entirely, which is exactly why a
                # run of consecutive failures against this same host
                # looked identical in the log whether the cause was a
                # stale/wrong IP (e.g. a stereo-pair group's active
                # "leader" address moving to a different paired speaker),
                # a timeout, or something else -- there was no way to
                # tell from the log alone.
                log.warning("[%s] Direct connect to %s failed, falling back to discovery",
                            self.name, self.chromecast_host, exc_info=True)

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
        had_writer = False
        last_healthy = time.time()

        while not self._stop.is_set():
            if cast is None:
                cast = self._get_chromecast()
                if cast is None:
                    log.warning("[%s] Cast device '%s' not found, retrying...",
                                self.name, self.chromecast_name or self.chromecast_host)
                    time.sleep(WATCHDOG_INTERVAL_SECONDS)
                    continue
                # get_chromecast_from_host()/get_chromecasts() only ever
                # *construct* a Chromecast object -- neither one starts its
                # SocketClient thread or connects it. Until something calls
                # cast.wait() (which lazily calls socket_client.start() the
                # first time), socket_client.host sits at its hardcoded
                # default of the literal string "unknown" and every call
                # through it raises "Chromecast unknown:8009 is
                # connecting...". This loop used to skip straight to
                # update_status() below without ever calling wait() first
                # -- since nothing had connected yet, every single
                # watchdog cycle failed with that exact error, forever,
                # for EVERY zone (this was never actually specific to Cast
                # groups/stereo pairs, despite that being the working
                # theory last round -- Living Room Soundbar, a plain
                # single device, hit the identical failure). Establish the
                # connection here, once, right after acquiring the cast
                # object, instead of implicitly relying on _play()'s own
                # cast.wait() call, which only happens much later and only
                # if a writer ever starts.
                try:
                    cast.wait(timeout=15)
                except Exception:
                    log.warning("[%s] Failed to connect to Cast device, retrying",
                                self.name, exc_info=True)
                    cast = None
                    time.sleep(WATCHDOG_INTERVAL_SECONDS)
                    continue
                # Don't play_media() here just because we found the
                # Cast device — wait for the loop below to see a real
                # AirPlay session start (writer_started). See that
                # comment for why calling play_media unconditionally,
                # whether or not anything was actually playing, is
                # what caused the memory leak this replaced.
                last_healthy = time.time()

            time.sleep(WATCHDOG_INTERVAL_SECONDS)

            has_writer = self._pcm.has_writer
            writer_started = has_writer and not had_writer
            had_writer = has_writer

            try:
                cast.media_controller.update_status()
                state = cast.media_controller.status.player_state
            except Exception:
                # exc_info=True is deliberate here too -- this is the
                # exact failure that repeated every ~10s, every cycle,
                # for the whole 'no sound for days' Cast Bridge outage,
                # with nothing in the log to say WHY update_status() kept
                # failing (stale connection, timeout, protocol error,
                # etc.). Without the real exception this is a guessing
                # game every time it recurs.
                log.warning("[%s] Lost connection to Cast device, reconnecting",
                            self.name, exc_info=True)
                cast = None
                continue

            if state in ("PLAYING", "BUFFERING"):
                last_healthy = time.time()
                continue

            if not has_writer:
                # No active AirPlay session on this zone right now —
                # IDLE is the expected resting state here, not a
                # stall. The previous version of this loop treated
                # "not PLAYING" as "broken, reconnect" unconditionally,
                # which meant it kept forcing a brand-new HTTP
                # connection and ffmpeg process every
                # STALL_TOLERANCE_SECONDS, forever, even when a zone
                # was simply idle all day with nobody AirPlaying to
                # it. Each of those connections then blocked forever
                # waiting on PCM that was never coming (nothing was
                # playing), and nothing ever cleaned them up — that's
                # the actual mechanism behind the multi-gigabyte
                # memory growth reported after ~a day of uptime.
                last_healthy = time.time()
                continue

            if writer_started or time.time() - last_healthy > STALL_TOLERANCE_SECONDS:
                log.info("[%s] Starting/resuming Cast playback (state=%s, writer_started=%s)",
                         self.name, state, writer_started)
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
