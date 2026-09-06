# Standalone AirPlay (RAOP / AirPlay 1 only) local-audio addon.
#
# Deliberately RAOP-only, not AirPlay 2: Music Assistant's own docs state
# Shairport and AirPlay 2 are currently incompatible (Shairport lacks NTP
# timing support for AP2's mode, MA lacks PTP timing support for it), and
# MA's own AirPlay 2 grouping has open sync bugs. RAOP is also the mode
# with the actual track record here. This drops nqptp entirely (it exists
# only for AirPlay 2's PTP clock) and all AirPlay-2-only build deps
# (libplist, libsodium, dbus/HAP pairing).
#
# Depends on the separate Multiroom Audio addon (Chrisuthe) for its
# PulseAudio remap-sink topology — both addons connect to the same shared
# hassio_audio PulseAudio server. This addon creates one shairport-sync
# RAOP instance per remap-sink zone the Multiroom Audio addon has already
# created (see generate_airplay_services.sh). It does not create sinks
# itself, and has no coverage for masters that never get a remap sink
# from that addon (e.g. 2-channel devices it skips by design).
#
# Pinned to shairport-sync tag 4.3.7 — the same version Music Assistant's
# own AirPlay Receiver provider uses. An earlier unpinned build (whatever
# git HEAD happened to be at build time, later confirmed as 5.2.1)
# demonstrably ignored both the -p CLI flag and, apparently, the config
# file's port= setting: every instance kept trying to bind port 5000
# regardless of its assigned port, confirmed via `shairport-sync -v -p
# 5069` still reporting "rtsp listening port is 5000" in its own startup
# dump. Pinning to a known-working, widely-deployed release avoids
# debugging further regressions in an unpinned upstream HEAD.
#
# ALSA support is deliberately NOT compiled in. Every instance this addon
# generates hardcodes output_backend = "pa" — ALSA is only relevant for a
# standalone MA/shairport-sync install outside HAOS, which is out of
# scope here. Omitting --with-alsa isn't just a smaller binary: it means
# the class of ALSA-driver kernel hangs that can put a calling thread
# into an uninterruptible sleep simply cannot be reached through this
# binary at all, since no code path ever makes an ALSA ioctl.

# Base images now publish as multi-platform manifests, so a single
# unsuffixed tag resolves to the correct architecture automatically —
# no build.yaml / per-arch mapping needed. See:
# https://developers.home-assistant.io/blog/2026/04/02/builder-migration/
ARG BUILD_FROM=ghcr.io/hassio-addons/base:stable

# Stage: Build minimal FFmpeg for Shairport-Sync's --with-ffmpeg (ALAC/AAC
# decode support)
FROM alpine:3.20 AS ffmpeglite
ARG FFMPEG_BRANCH=release/7.1
RUN apk -U add \
    build-base \
    git \
    nasm \
    pkgconf
RUN mkdir -p /ffmpeg_sources /ffmpeg_build
WORKDIR /ffmpeg_sources
RUN git clone --depth=1 -b "$FFMPEG_BRANCH" https://github.com/FFmpeg/FFmpeg
WORKDIR /ffmpeg_sources/FFmpeg
RUN ./configure \
  --prefix="/ffmpeg_build" \
  --extra-libs="-lpthread -lm" \
  --ld="g++" \
  --disable-static \
  --enable-shared \
  --enable-gpl \
  --disable-programs \
  --disable-everything \
  --disable-doc \
  --disable-iconv \
  --disable-avdevice \
  --disable-avfilter \
  --disable-swscale \
  --disable-network \
  --disable-iamf \
  --disable-pixelutils \
  --disable-postproc \
  --enable-runtime-cpudetect \
  --disable-htmlpages \
  --disable-manpages \
  --disable-podpages \
  --disable-txtpages \
  --disable-debug \
  --enable-decoder=alac \
  --enable-decoder=aac
RUN make -j$(nproc) && make install

# Stage: Build Shairport-Sync, RAOP (AirPlay 1) only, PulseAudio backend
# only — no --with-airplay-2, no --with-alsa (see header note above), no
# --with-convolution (no impulse-response/room-correction feature is used
# by this addon, so libsndfile isn't needed either).
FROM ${BUILD_FROM} AS alpine-builder
RUN apk add --no-cache \
    pulseaudio-dev \
    build-base \
    git \
    autoconf \
    automake \
    libtool \
    pkgconfig \
    libconfig-dev \
    popt-dev \
    openssl-dev \
    soxr-dev \
    ca-certificates

WORKDIR /tmp
COPY --from=ffmpeglite /ffmpeg_build /usr/local
RUN git clone --depth=1 --branch 4.3.7 https://github.com/mikebrady/shairport-sync.git shairport-sync-ap1 && \
    cd shairport-sync-ap1 && \
    autoreconf -fi && \
    PKG_CONFIG_PATH=/usr/local/lib/pkgconfig ./configure \
        --prefix=/usr \
        --with-pipe \
        --with-pulseaudio \
        --with-pa \
        --with-soxr \
        --with-ffmpeg \
        --with-metadata \
        --without-avahi \
        --without-dns-sd \
        --with-tinysvcmdns \
        --with-ssl=openssl \
        --with-stdout && \
    make -j$(nproc) && \
    strip shairport-sync && \
    make install DESTDIR=/tmp/shairport-ap1-install

# Final stage
FROM ${BUILD_FROM} AS base

# Runtime packages, pared to what this addon's own script and the
# shairport-sync binary it ships actually use:
#   libconfig, popt   - shairport-sync's .conf parsing / CLI arg parsing
#   libpulse          - required by the --with-pa/--with-pulseaudio build
#   soxr              - required by the --with-soxr build (resampling)
#   openssl           - required by the --with-ssl=openssl build
#   pulseaudio-utils  - not required by shairport-sync itself, but useful
#                        for anyone debugging sink state (pactl) inside
#                        the container; drop if you want a stricter image
#   iproute2          - provides `ss`, used by is_port_available() in
#                        generate_airplay_services.sh
#   libgcc/libstdc++  - runtime deps of the custom-built ffmpeg libs
#                        (built with --ld="g++")
#   bash              - the run scripts and generator script are bash
#
# Deliberately NOT installed (removed vs. the previous image): the full
# ALSA stack (nothing here ever selects an ALSA output_backend), the full
# PipeWire/WirePlumber stack (this addon is a pure PulseAudio *client* of
# the shared hassio_audio server — it has no business running its own
# local audio server), avahi/dbus/glib (the build uses
# --without-avahi --with-tinysvcmdns, so no real avahi daemon is needed),
# gnutls/mbedtls (the build uses --with-ssl=openssl, so these alternate
# TLS backends are never selected), busybox-openrc/openrc (this addon
# runs entirely under s6-overlay, not openrc), man-pages/mandoc (no use
# in a headless container), nano/htop (interactive debugging convenience,
# not a runtime requirement), gcompat (no glibc binary in this pipeline —
# everything is built from source against musl), and the apk `ffmpeg`/
# `ffmpeg-libs` packages (redundant with, and possibly conflicting with,
# the minimal custom-built ffmpeg libs already copied in below).
#
# Not fully verified against root/'s actual run scripts: `curl` is kept
# below as a debugging convenience (not required by the generator script
# or shairport-sync); `confuse` and `shadow` from the previous image were
# dropped as unrecognized requirements — re-add either if something in
# root/ actually needs them.
RUN apk add -U -q --no-cache --no-progress \
    libconfig \
    libpulse \
    popt \
    soxr \
    openssl \
    pulseaudio \
    pulseaudio-utils \
    iproute2 \
    libgcc \
    libstdc++ \
    bash \
    curl

COPY root/ /

# Copy Shairport-Sync (RAOP-only build)
COPY --from=alpine-builder /tmp/shairport-ap1-install/usr/bin/shairport-sync /usr/bin/shairport-sync

# Copy FFmpeg libraries shairport-sync was built against
COPY --from=ffmpeglite /ffmpeg_build/lib/*.so* /usr/lib/
RUN ldconfig /usr/lib 2>/dev/null || true

# Create runtime directories
RUN mkdir -p /config/dbase_and_logs/shairport-sync

# Set up environment for s6-overlay
ENV S6_BEHAVIOUR_IF_STAGE2_FAILS=2
ENV S6_KEEP_ENV=1
COPY generate_airplay_services.sh /usr/sbin/generate_airplay_services.sh
RUN chmod +x /usr/sbin/generate_airplay_services.sh
ENV S6_STAGE2_HOOK=/usr/sbin/generate_airplay_services.sh

# Cleanup to reduce image size
RUN rm -rf /usr/share/man /usr/share/doc /usr/share/info /var/cache/apk/* /tmp/* /usr/share/locale/*

LABEL \
    io.hass.arch="amd64|aarch64" \
    io.hass.name="AirPlay Local Audio" \
    io.hass.description="RAOP (AirPlay 1) receivers for local sound cards, one per physical output" \
    io.hass.type="addon" \
    io.hass.version="1.0" \
    maintainer="Maido Käära <m@maido.io>"
