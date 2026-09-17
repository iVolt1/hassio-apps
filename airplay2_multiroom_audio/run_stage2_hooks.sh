#!/bin/bash
#
# S6_STAGE2_HOOK only points to one script, so this just chains the
# generators: the dynamic per-PulseAudio-sink AirPlay 2 zones first
# (which also creates the one-time dbus/avahi/nqptp services
# everything else depends on), then the Cast Bridge pipe-output zones
# (one per Cast device the separate Cast Bridge addon has discovered
# — see generate_castbridge_pipe_services.sh for why those AirPlay 2
# receivers have to live here rather than in Cast Bridge's own
# container).
#
# The old fixed OwnTone pipe-output zone is gone — OwnTone's Cast
# bridging (and the AirPlay2->OwnTone pipe feeding it) is being
# replaced entirely by the AirPlay2-to-Cast-Bridge path above.
#
# Deliberately no `set -e`: generate_airplay2_services.sh ends in a
# `while read ... done < <(cmd)` loop, which always finishes via a
# failed `read` at EOF — that makes its own exit status non-zero on
# every normal run, regardless of whether anything actually went
# wrong. With `set -e` here, that non-zero status would abort this
# wrapper immediately and silently skip the remaining scripts every
# time.
/usr/sbin/generate_airplay2_services.sh
/usr/sbin/generate_castbridge_pipe_services.sh
