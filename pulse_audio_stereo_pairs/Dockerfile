# Pulse Audio Stereo Pair Addon
FROM ghcr.io/hassio-addons/base/amd64:stable

RUN apk add -U -q --no-cache --no-progress \
    pulseaudio \
    pulseaudio-utils \
    bash

COPY root/ /
RUN chmod +x /etc/s6-overlay/s6-rc.d/svc-pulse_audio_stereo_pairs/run
RUN chmod +x /etc/s6-overlay/s6-rc.d/svc-pulse_audio_stereo_pairs/finish

# Don't crash the container if a service fails
ENV S6_BEHAVIOUR_IF_STAGE2_FAILS=1

LABEL \
    io.hass.arch="amd64" \
    io.hass.name="Pulse Audio Stereo Pairs" \
    io.hass.description="Creates PulseAudio stereo pair remap sinks for multi-channel audio devices" \
    io.hass.type="addon" \
    io.hass.version="1.0"