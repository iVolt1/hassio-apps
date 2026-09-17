#!/bin/bash
#
# S6_STAGE2_HOOK only points to one script, so this just chains the two
# generators: the dynamic per-PulseAudio-sink AirPlay 2 zones first
# (which also creates the one-time dbus/avahi/nqptp services everything
# else depends on), then the fixed OwnTone pipe-output zone.
set -e
/usr/sbin/generate_airplay2_services.sh
/usr/sbin/generate_owntone_pipe_service.sh
