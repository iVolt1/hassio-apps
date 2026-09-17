# As of Supervisor 2026.04.0, build.yaml / BUILD_FROM is no longer
# read or passed as a build-arg — hardcode the same multi-arch base
# image the AirPlay 2 Multiroom Audio addon uses.
#
# No nqptp build stage here, deliberately: nqptp binds a fixed,
# host-wide OS port (319) and is meant to run as a single instance per
# host shared by every AirPlay-2-capable shairport-sync instance, not
# one per container. This addon relies on the AirPlay 2 Multiroom
# Audio addon's (or any other AP2 shairport-sync addon's) nqptp
# already running host-wide under host_network:true — see launcher.py.
FROM ghcr.io/home-assistant/base:latest AS shairport-builder
RUN apk add --no-cache \
    build-base git autoconf automake libtool pkgconfig \
    libconfig-dev popt-dev openssl-dev soxr-dev \
    libplist-dev libsodium-dev libgcrypt-dev \
    util-linux-dev ffmpeg-dev xxd libplist-util ca-certificates
WORKDIR /tmp
# Deliberately --with-tinysvcmdns instead of --with-avahi: tinysvcmdns
# is a small self-contained, in-process mDNS responder bundled with
# shairport-sync's own source — no system avahi-daemon involved at
# all. That keeps this addon completely out of the avahi
# hostname-collision bug class hit building the sibling AirPlay 2
# Multiroom Audio addon (two independent avahi-daemons on the same
# host_network:true host fighting over the same default mDNS
# hostname). No --with-pulseaudio either: this addon's receivers only
# ever output to a pipe, never a PulseAudio sink.
RUN git clone https://github.com/mikebrady/shairport-sync.git shairport-sync-ap2 && \
    cd shairport-sync-ap2 && \
    git switch development && \
    autoreconf -fi && \
    ./configure \
        --prefix=/usr \
        --sysconfdir=/etc \
        --with-soxr \
        --with-tinysvcmdns \
        --with-ssl=openssl \
        --with-airplay-2 \
        --with-pipe \
        CFLAGS="-DCONFIG_FOR_LINUX" && \
    make -j$(nproc) && \
    strip shairport-sync && \
    make install DESTDIR=/tmp/shairport-ap2-install

FROM ghcr.io/home-assistant/base:latest
# No avahi, no dbus, no pulseaudio here at all — this addon needs
# neither a system mDNS daemon (shairport-sync uses tinysvcmdns;
# pychromecast's discovery uses the pure-Python zeroconf package) nor
# an audio server (pipe output only).
RUN apk add -U -q --no-cache --no-progress \
    libconfig popt soxr openssl libplist libplist-util libsodium libgcrypt \
    util-linux ffmpeg-libs ffmpeg iproute2 libgcc libstdc++ bash curl \
    python3 py3-pip

COPY --from=shairport-builder /tmp/shairport-ap2-install/usr/bin/shairport-sync /usr/bin/shairport-sync

WORKDIR /opt/cast-bridge
COPY requirements.txt .
RUN pip install --break-system-packages --no-cache-dir -r requirements.txt

COPY launcher.py cast_bridge_zone.py ./

RUN mkdir -p /config/cast-bridge /media/music/cast-bridge

# Single static s6 service. launcher.py itself does Cast discovery,
# port persistence, and spawns/supervises one shairport-sync +
# one cast_bridge_zone.py pair per discovered Cast device — no
# S6_STAGE2_HOOK generator step needed, unlike the sibling addon,
# since there's no PulseAudio-sink hardware enumeration involved here.
COPY rootfs/ /
RUN chmod +x /etc/s6-overlay/s6-rc.d/cast-bridge/run

RUN rm -rf /usr/share/man /usr/share/doc /usr/share/info /var/cache/apk/* /tmp/* /usr/share/locale/*
LABEL io.hass.arch="amd64|aarch64" io.hass.name="Cast Bridge"
