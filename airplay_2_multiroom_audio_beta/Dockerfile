FROM ghcr.io/home-assistant/base:latest AS nqptp-builder
RUN apk add --no-cache build-base git autoconf automake libtool pkgconfig
WORKDIR /tmp
RUN git clone https://github.com/mikebrady/nqptp.git && \
    cd nqptp && \
    git switch development && \
    sed -i 's|#include <linux/if_packet.h>|#include <netpacket/packet.h>|' nqptp-utilities.c && \
    autoreconf -fi && \
    CFLAGS="-DCONFIG_FOR_LINUX" ./configure && \
    make -j$(nproc) && \
    make install DESTDIR=/tmp/nqptp-install

# cliairplay (Music Assistant's AirPlay 1/2 sender), for the AirPlay 1 bridge.
# Built fully static on Debian: its prebuilt libraop libraries are glibc
# builds, and a static binary runs on this Alpine image without gcompat.
# Only libraop's own submodules are needed (its prebuilt targets/), not the
# OpenSSL/BoringSSL sources nested below them.
FROM debian:bookworm-slim AS cliairplay-builder
ARG CLIAIRPLAY_REF=v0.5.12
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential git ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /tmp
# Patch: CLIAIRPLAY_RAOP_LEAD_MS sets the RAOP lead (fixed 2000 ms upstream).
COPY cliairplay-raop-lead.patch /tmp/cliairplay-raop-lead.patch
RUN git clone --depth 1 --branch ${CLIAIRPLAY_REF} https://github.com/music-assistant/airplay-cli.git && \
    cd airplay-cli && \
    git apply /tmp/cliairplay-raop-lead.patch && \
    git submodule update --init --depth 1 libraop && \
    git -C libraop submodule update --init --depth 1 && \
    make -j$(nproc) STATIC=1 EXTRA_LDFLAGS=-static VERSION=${CLIAIRPLAY_REF#v} && \
    cp bin/cliairplay-linux-* /tmp/cliairplay && \
    /tmp/cliairplay --check

FROM ghcr.io/home-assistant/base:latest AS alpine-builder
RUN apk add --no-cache \
    build-base git autoconf automake libtool pkgconfig \
    libconfig-dev popt-dev openssl-dev soxr-dev pulseaudio-dev \
    avahi-dev libplist-dev libsodium-dev libgcrypt-dev \
    util-linux-dev ffmpeg-dev xxd libplist-util ca-certificates mosquitto-dev
WORKDIR /tmp
# Pinned to the 5.6.3 release, plus a local patch: for senders that send SSRC 0 on
# buffered AirPlay 2 packets (Music Assistant's cliairplay), take the audio format from
# the stream SETUP's audioFormat instead. See mikebrady/shairport-sync#2319.
COPY sps-ssrc0-fallback.patch /tmp/sps-ssrc0-fallback.patch
RUN git clone https://github.com/mikebrady/shairport-sync.git shairport-sync-ap2 && \
#RUN git clone --depth 1 --branch 5.6.3 https://github.com/mikebrady/shairport-sync.git shairport-sync-ap2 && \
    cd shairport-sync-ap2 && \
    git switch development && \
    #git apply /tmp/sps-ssrc0-fallback.patch && \
    autoreconf -fi && \
    ./configure \
        --prefix=/usr \
        --sysconfdir=/etc \
        --with-soxr \
        --with-avahi \
        --with-ssl=openssl \
        --with-airplay-2 \
        --with-pulseaudio \
        --with-pipe \
        --with-metadata \
        --with-mqtt-client \
        CFLAGS="-DCONFIG_FOR_LINUX" && \
    make -j$(nproc) && \
    strip shairport-sync && \
    make install DESTDIR=/tmp/shairport-ap2-install

# A second shairport-sync, used only by the AirPlay 1 bridge: mariusdkm's PR
# branch adding timing headers to the pipe backend (mikebrady/shairport-sync#2334,
# not merged yet; the header format may still change). Pinned to a commit. Every
# other zone keeps using the 5.6.2 build above.
FROM alpine-builder AS sps-pipe-builder
ARG SPS_PIPE_REPO=https://github.com/mariusdkm/shairport-sync.git
ARG SPS_PIPE_COMMIT=ce6ad19ee0976d5e43698d289990aefed226a057
RUN git clone --branch ap2/s2-pipe-timing-headers ${SPS_PIPE_REPO} shairport-sync-pipe && \
    cd shairport-sync-pipe && \
    git checkout ${SPS_PIPE_COMMIT} && \
    git apply /tmp/sps-ssrc0-fallback.patch && \
    autoreconf -fi && \
    ./configure \
        --prefix=/usr \
        --sysconfdir=/etc \
        --with-soxr \
        --with-avahi \
        --with-ssl=openssl \
        --with-airplay-2 \
        --with-pipe \
        --with-metadata \
        CFLAGS="-DCONFIG_FOR_LINUX" && \
    make -j$(nproc) && \
    strip shairport-sync && \
    cp shairport-sync /tmp/shairport-sync-ap1bridge

FROM ghcr.io/home-assistant/base:latest AS base
# python3/py3-pip/ffmpeg (the real binary, not just ffmpeg-libs) added
# for cast_bridge_manager.py + cast_bridge_zone.py — Cast device
# discovery/relay used to live in a separate "Cast Bridge" addon, now
# folded into this one container (see generate_airplay2_services.sh
# and run_stage2_hooks.sh for why: nqptp's IPC can't cross containers,
# and the two addons' config mounts didn't actually resolve to the
# same host path either, so keeping this as one addon avoids both
# problems at the root).
RUN apk add -U -q --no-cache --no-progress \
    libconfig popt soxr openssl pulseaudio pulseaudio-utils \
    avahi dbus libplist libplist-util libsodium libgcrypt \
    util-linux ffmpeg-libs ffmpeg iproute2 libgcc libstdc++ bash curl \
    python3 py3-pip mosquitto-libs
COPY --from=nqptp-builder /tmp/nqptp-install/usr/local/bin/nqptp /usr/local/bin/nqptp
COPY --from=alpine-builder /tmp/shairport-ap2-install/usr/bin/shairport-sync /usr/bin/shairport-sync
COPY --from=cliairplay-builder /tmp/cliairplay /usr/bin/cliairplay
COPY --from=sps-pipe-builder /tmp/shairport-sync-ap1bridge /usr/bin/shairport-sync-ap1bridge
RUN mkdir -p /config/shairport-sync /config/cast-bridge /media/music/castbridge /run/dbus
ENV S6_BEHAVIOUR_IF_STAGE2_FAILS=2
ENV S6_KEEP_ENV=1
# The add-on's private bus (avahi, shairport-sync); /run/dbus is the host bus (host_dbus).
ENV DBUS_SYSTEM_BUS_ADDRESS=unix:path=/run/ap2-dbus/system_bus_socket

WORKDIR /usr/sbin
COPY requirements.txt .
RUN pip install --break-system-packages --no-cache-dir -r requirements.txt

COPY generate_airplay2_services.sh /usr/sbin/generate_airplay2_services.sh
COPY run_stage2_hooks.sh /usr/sbin/run_stage2_hooks.sh
COPY cast_bridge_manager.py /usr/sbin/cast_bridge_manager.py
COPY cast_bridge_zone.py /usr/sbin/cast_bridge_zone.py
COPY sink_zone_manager.py /usr/sbin/sink_zone_manager.py
COPY remap_sinks.py /usr/sbin/remap_sinks.py
COPY web_panel.py /usr/sbin/web_panel.py
COPY bt_reset.py /usr/sbin/bt_reset.py
COPY ap1_test.py /usr/sbin/ap1_test.py
COPY ap1_bridge.py /usr/sbin/ap1_bridge.py
COPY ap1_bridge_manager.py /usr/sbin/ap1_bridge_manager.py
RUN chmod +x /usr/sbin/generate_airplay2_services.sh \
             /usr/sbin/run_stage2_hooks.sh
ENV S6_STAGE2_HOOK=/usr/sbin/run_stage2_hooks.sh
RUN rm -rf /usr/share/man /usr/share/doc /usr/share/info /var/cache/apk/* /tmp/* /usr/share/locale/*
LABEL io.hass.arch="amd64|aarch64" io.hass.name="AirPlay 2 Local Audio"
