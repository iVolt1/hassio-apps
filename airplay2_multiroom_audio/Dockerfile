# Standalone AirPlay 2 local-audio addon.
#
# Companion to the RAOP-only "AirPlay Local Audio" addon (see that
# Dockerfile's own header for why RAOP-only exists as a separate,
# simpler image). This one is the AirPlay 2 counterpart: real
# --with-airplay-2 support, a running nqptp PTP companion daemon per
# zone, and real avahi/D-Bus instead of tinysvcmdns.
#
# Converted directly from the manual build sequence (nqptp + shairport-
# sync, both tracking the `development` branch at HEAD, not a pinned
# tag/commit). That matches how this was validated by hand, but the AP1
# image's own header notes a real regression risk in doing this: an
# earlier unpinned build silently ignored -p/port= entirely. If that
# kind of regression shows up here, pin FFmpeg-tag-style to a known-good
# commit instead of tracking `development` HEAD.
#
# Depends on the separate Multiroom Audio addon (Chrisuthe) for its
# PulseAudio remap-sink topology, exactly like the AP1 addon — both
# connect to the same shared hassio_audio PulseAudio server. This image
# does not create sinks itself.
#
# IMPORTANT — sink selection: shairport-sync's PulseAudio backend has no
# `-d <sink>` CLI flag (that's ALSA-only; see the open feature request
# mikebrady/shairport-sync#1916). Each generated instance MUST select
# its sink via its config file's `pa { sink = "..."; }` block. If
# generate_airplay_services.sh (not part of this conversion — reused
# from the AP1 addon) still passes `-- -d <sink>`, every instance will
# silently fall back to the PulseAudio default sink instead of its
# intended zone. Update that script before relying on this image.
#
# IMPORTANT — nqptp + avahi/D-Bus are new requirements the AP1 image
# doesn't have: AP2 needs one running nqptp per zone (shared-memory
# interface name derived from the instance name, e.g. /nqptp-LivingRoom)
# and a real avahi-daemon backed by a live D-Bus system bus — not just
# the binaries installed. Getting `avahi-daemon` to start cleanly inside
# a bare container needs, in order: a D-Bus system bus up first
# (`mkdir -p /run/dbus`, a machine-id, `dbus-daemon --system --fork`),
# *then* `avahi-daemon --no-drop-root -D`. This Dockerfile installs the
# packages and directories for that; wiring it up as s6 longrun services
# (alongside whatever generates the per-zone shairport-sync + nqptp
# services) still needs to be written against this addon's actual rootfs
# layout — see the note near the bottom of this file.

ARG BUILD_FROM=ghcr.io/hassio-addons/base:stable

# Stage: Build nqptp — the PTP timing companion daemon AirPlay 2 needs.
# One instance runs per zone; shairport-sync talks to it over shared
# memory, not over the network, so it must be present and running
# locally, unlike anything in the AP1 image.
FROM ${BUILD_FROM} AS nqptp-builder
RUN apk add --no-cache \
    build-base \
    git \
    autoconf \
    automake \
    libtool \
    pkgconfig
WORKDIR /tmp
RUN git clone https://github.com/mikebrady/nqptp.git && \
    cd nqptp && \
    git switch development && \
    autoreconf -fi && \
    ./configure && \
    make -j$(nproc) && \
    make install DESTDIR=/tmp/nqptp-install

# Stage: Build Shairport-Sync with real AirPlay 2 support.
#
# Deliberately NOT --with-alsa: the source script installed
# libasound2-dev but never actually passed --with-alsa to ./configure,
# so it was dead weight there too — omitted here for the same reason
# the AP1 image omits ALSA entirely (see that Dockerfile's header: it
# keeps the class of ALSA-driver kernel hangs unreachable through this
# binary). Deliberately NOT --with-systemd-startup either: that installs
# systemd unit files, which are meaningless in an s6-overlay container —
# process supervision here is s6's job, same as the AP1 image.
FROM ${BUILD_FROM} AS alpine-builder
RUN apk add --no-cache \
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
    pulseaudio-dev \
    avahi-dev \
    libplist-dev \
    libsodium-dev \
    libgcrypt-dev \
    util-linux-dev \
    ffmpeg-dev \
    xxd \
    libplist-utils \
    ca-certificates

WORKDIR /tmp
RUN git clone https://github.com/mikebrady/shairport-sync.git shairport-sync-ap2 && \
    cd shairport-sync-ap2 && \
    git switch development && \
    autoreconf -fi && \
    ./configure \
        --sysconfdir=/etc \
        --with-soxr \
        --with-avahi \
        --with-ssl=openssl \
        --with-airplay-2 \
        --with-pulseaudio && \
    make -j$(nproc) && \
    strip shairport-sync && \
    make install DESTDIR=/tmp/shairport-ap2-install

# Final stage
FROM ${BUILD_FROM} AS base

# Runtime packages. Everything the AP1 image installs still applies
# (libconfig, popt, libpulse, soxr, openssl, iproute2, libgcc/libstdc++,
# bash) plus what AP2 specifically adds:
#   avahi, dbus         - real mDNS + the D-Bus system bus avahi-daemon
#                          needs to register with (AP1 uses
#                          --without-avahi --with-tinysvcmdns instead,
#                          so it needs neither)
#   libplist             - AirPlay 2 binary-plist parsing (SETUP
#                          messages, timing peer info, etc.)
#   libsodium, libgcrypt  - HAP pairing / transient pair-setup crypto
#   util-linux            - libuuid, used by the pairing code
#   ffmpeg-libs            - ALAC/AAC decode for AP2's realtime and
#                          buffered audio paths (the source script
#                          installed the full libavutil/avcodec/
#                          avformat-dev trio without a minimal custom
#                          build; this mirrors that rather than
#                          introducing the AP1 image's separate
#                          ffmpeglite stage — swap one in later if a
#                          smaller image matters more than fidelity to
#                          the original script)
RUN apk add -U -q --no-cache --no-progress \
    libconfig \
    popt \
    soxr \
    openssl \
    pulseaudio \
    pulseaudio-utils \
    avahi \
    dbus \
    libplist \
    libplist-utils \
    libsodium \
    libgcrypt \
    util-linux \
    ffmpeg-libs \
    iproute2 \
    libgcc \
    libstdc++ \
    bash \
    curl

# Copy nqptp
COPY --from=nqptp-builder /tmp/nqptp-install/usr/local/bin/nqptp /usr/local/bin/nqptp

# Copy Shairport-Sync (AirPlay 2 build)
COPY --from=alpine-builder /tmp/shairport-ap2-install/usr/bin/shairport-sync /usr/bin/shairport-sync

# Create runtime directories — /run/dbus is required before dbus-daemon
# will start; see the D-Bus/avahi startup note in the header.
RUN mkdir -p /config/shairport-sync /run/dbus

# Set up environment for s6-overlay
ENV S6_BEHAVIOUR_IF_STAGE2_FAILS=2
ENV S6_KEEP_ENV=1

# --- NOT YET WIRED UP ---
# The AP1 image only needs one s6 hook: generate_airplay_services.sh,
# which creates one shairport-sync longrun per zone. AP2 needs that
# PLUS three more things running continuously and in the right order —
# none of which existed in the AP1 image, so none of it is copied in
# here yet:
#   1. A dbus-daemon --system longrun (must be up before avahi-daemon)
#   2. An avahi-daemon --no-drop-root -D longrun (must be up before any
#      shairport-sync instance tries to register)
#   3. One nqptp longrun per zone (must be up before that zone's
#      shairport-sync instance starts, since it depends on nqptp's
#      shared-memory interface existing)
#
# generate_airplay_services.sh itself likely also needs updating for
# AP2 — beyond adding the nqptp service per zone, check whether it's
# still passing `-- -d <sink>` (see the sink-selection warning in the
# header) instead of writing a config file with `pa { sink = ...; }`.
#
# COPY generate_airplay_services.sh /usr/sbin/generate_airplay_services.sh
# RUN chmod +x /usr/sbin/generate_airplay_services.sh
# ENV S6_STAGE2_HOOK=/usr/sbin/generate_airplay_services.sh

# Cleanup to reduce image size
RUN rm -rf /usr/share/man /usr/share/doc /usr/share/info /var/cache/apk/* /tmp/* /usr/share/locale/*

LABEL \
    io.hass.arch="amd64|aarch64" \
    io.hass.name="AirPlay 2 Local Audio" \
    io.hass.description="AirPlay 2 receivers for local sound cards, one per physical output" \
    io.hass.type="addon" \
    io.hass.version="1.0" \
    maintainer="Maido Käära <m@maido.io>"
