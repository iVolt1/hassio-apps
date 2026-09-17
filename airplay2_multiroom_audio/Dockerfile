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
RUN apk add -U -q --no-cache --no-progress \
    libconfig popt soxr openssl pulseaudio pulseaudio-utils \
    avahi dbus libplist libplist-util libsodium libgcrypt \
    util-linux ffmpeg-libs iproute2 libgcc libstdc++ bash curl
COPY --from=nqptp-builder /tmp/nqptp-install/usr/local/bin/nqptp /usr/local/bin/nqptp
COPY --from=alpine-builder /tmp/shairport-ap2-install/usr/bin/shairport-sync /usr/bin/shairport-sync
RUN mkdir -p /config/shairport-sync /run/dbus
ENV S6_BEHAVIOUR_IF_STAGE2_FAILS=2
ENV S6_KEEP_ENV=1
COPY generate_airplay2_services.sh /usr/sbin/generate_airplay2_services.sh
COPY generate_owntone_pipe_service.sh /usr/sbin/generate_owntone_pipe_service.sh
COPY run_stage2_hooks.sh /usr/sbin/run_stage2_hooks.sh
RUN chmod +x /usr/sbin/generate_airplay2_services.sh \
             /usr/sbin/generate_owntone_pipe_service.sh \
             /usr/sbin/run_stage2_hooks.sh
ENV S6_STAGE2_HOOK=/usr/sbin/run_stage2_hooks.sh
RUN rm -rf /usr/share/man /usr/share/doc /usr/share/info /var/cache/apk/* /tmp/* /usr/share/locale/*
LABEL io.hass.arch="amd64|aarch64" io.hass.name="AirPlay 2 Local Audio"
