#!/bin/bash
#
# S6_STAGE2_HOOK only points to one script, so this just chains the two
# generators: the dynamic per-PulseAudio-sink AirPlay 2 zones first
# (which also creates the one-time dbus/avahi/nqptp services everything
# else depends on), then the fixed OwnTone pipe-output zone.
#
# Deliberately no `set -e`: generate_airplay2_services.sh ends in a
# `while read ... done < <(cmd)` loop, which always finishes via a
# failed `read` at EOF — that makes its own exit status non-zero on
# every normal run, regardless of whether anything actually went
# wrong. With `set -e` here, that non-zero status would abort this
# wrapper immediately and silently skip the second script every time.
/usr/sbin/generate_airplay2_services.sh
/usr/sbin/generate_owntone_pipe_service.sh
