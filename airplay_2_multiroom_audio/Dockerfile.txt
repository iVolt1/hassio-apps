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

FROM ghcr.io/home-assistant/base:latest AS alpine-builder
RUN apk add --no-cache \
    build-base git autoconf automake libtool pkgconfig \
    libconfig-dev popt-dev openssl-dev soxr-dev pulseaudio-dev \
    avahi-dev libplist-dev libsodium-dev libgcrypt-dev \
    util-linux-dev ffmpeg-dev xxd libplist-util ca-certificates
WORKDIR /tmp
RUN git clone https://github.com/mikebrady/shairport-sync.git shairport-sync-ap2 && \
    cd shairport-sync-ap2 && \
    git switch development && \
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
        CFLAGS="-DCONFIG_FOR_LINUX" && \
    make -j$(nproc) && \
    strip shairport-sync && \
    make install DESTDIR=/tmp/shairport-ap2-install

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
    python3 py3-pip
COPY --from=nqptp-builder /tmp/nqptp-install/usr/local/bin/nqptp /usr/local/bin/nqptp
COPY --from=alpine-builder /tmp/shairport-ap2-install/usr/bin/shairport-sync /usr/bin/shairport-sync
RUN mkdir -p /config/shairport-sync /config/cast-bridge /media/music/castbridge /run/dbus
ENV S6_BEHAVIOUR_IF_STAGE2_FAILS=2
ENV S6_KEEP_ENV=1

WORKDIR /usr/sbin
COPY requirements.txt .
RUN pip install --break-system-packages --no-cache-dir -r requirements.txt

COPY generate_airplay2_services.sh /usr/sbin/generate_airplay2_services.sh
COPY run_stage2_hooks.sh /usr/sbin/run_stage2_hooks.sh
COPY cast_bridge_manager.py /usr/sbin/cast_bridge_manager.py
COPY cast_bridge_zone.py /usr/sbin/cast_bridge_zone.py
RUN chmod +x /usr/sbin/generate_airplay2_services.sh \
             /usr/sbin/run_stage2_hooks.sh
ENV S6_STAGE2_HOOK=/usr/sbin/run_stage2_hooks.sh
RUN rm -rf /usr/share/man /usr/share/doc /usr/share/info /var/cache/apk/* /tmp/* /usr/share/locale/*
LABEL io.hass.arch="amd64|aarch64" io.hass.name="AirPlay 2 Local Audio"
