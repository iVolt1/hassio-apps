#!/bin/bash

# Guard against two overlapping invocations of this script racing on the
# same PulseAudio remap sinks. This is exactly what produced
# "HD_Audio_Generic_Digital_Surround_7_1_HDMI_2_rl_rr.2"-style duplicate
# sinks (and duplicate airplay2-<name>.2 zones/services): two runs each
# unload-then-reload the same sink name, and when their unload/reload
# pairs interleave, one run's reload lands on top of a sink name the
# other run's reload just claimed -- PulseAudio doesn't error on that,
# it silently auto-suffixes the loser ".2", so the failure mode is
# extra duplicate zones, not a crash. Restarting more than one thing
# that triggers this script at close to the same time (e.g. two addons,
# or the same addon restarted twice in quick succession before the
# first run exited) is exactly the trigger -- and this script's own log
# file made it worse: it's truncated fresh (>) on every run but written
# to with >> everywhere else, so two overlapping runs' output lands in
# the same log under a single header, looking like one run did
# everything twice. Block here until any other running instance
# finishes, rather than letting both proceed at once.
exec 200>/tmp/generate_airplay2_services.lock
if ! flock -w 120 200; then
    echo "$(date) — generate_airplay2_services.sh: another instance is still running after 120s, giving up" >> /tmp/airplay2_gen_debug.log
    exit 1
fi

echo "$(date) — generate_airplay2_services.sh started" >> /tmp/airplay2_gen_debug.log
log_file="/config/shairport-sync/logs/generate_airplay2_services.log"
mkdir -p "$(dirname "$log_file")"
echo "# AirPlay 2 services generated on $(date)" > "$log_file"

BASE_DIR="/etc/s6-overlay/s6-rc.d"
CONTENTS_DIR="${BASE_DIR}/user/contents.d"
CONFIG_DIR="/config/shairport-sync/config"
mkdir -p "${CONTENTS_DIR}" "${CONFIG_DIR}"

# Needed early (before the avahi service block below) so avahi-daemon can
# be restricted to this interface at startup — see the airplay2-avahi
# service comment for why that restriction matters.
AIRPLAY_INTERFACE="${AIRPLAY_INTERFACE:-enp5s0}"

# --- Bounce master sinks & recreate remap sinks before generating zone services ----------------
# HDMI (and some other ALSA-card) sinks don't resume cleanly on their own
# after the receiver is power-cycled or the cable is unplugged/replugged:
# PulseAudio's sink stays "RUNNING"/"IDLE" but the underlying ALSA PCM
# device never re-opens, so no audio actually reaches the hardware again
# until something forces PulseAudio to close and reopen that PCM device.
# This is the same class of fix as suspend_resume_sink() in the local_audio
# provider's pa_simple.py: pactl suspend-sink <name> 1 followed by
# suspend-sink <name> 0 forces PulseAudio to tear down and reopen the ALSA
# PCM device, which is what actually clears a stuck HDMI resume.
#
# Every zone this script generates is a module-remap-sink, though — a
# virtual routing sink with no ALSA backing of its own. Bouncing a remap
# sink does nothing to the real hardware behind it; only bouncing its
# underlying MASTER sink (the actual hw:... ALSA sink the remap was built
# on top of) resets the PCM device.
#
# Bouncing the master alone isn't enough, though: these remap sinks are
# created by the separate Multiroom Audio Controller addon, not by this
# one, and testing showed that bouncing the master here did not restore
# sound (see generate_airplay2_services.log: the bounce ran against the
# correct master, but audio still didn't play). That addon appears to
# recreate the remap sink itself once the master's state changes, on its
# own schedule — which races this script: either it hasn't recreated the
# sink yet by the time the loop below scans for zones, or it recreates it
# in a way that isn't actually attached to the freshly-reset master.
# Rather than depend on that addon's timing, this addon now captures each
# remap sink's exact module arguments (name, master, channels, channel
# map, etc.) and unloads + reloads it itself immediately after bouncing
# its master — so the remap sink is guaranteed to exist, under its
# correct name, freshly attached to the just-reset master, before any
# zone service generation below reads the sink list.
#
# This runs unconditionally, before any zone services are (re)created.
# That's safe here specifically because this whole script only runs via
# S6_STAGE2_HOOK — i.e. on a full container start/restart — which already
# tears down and respawns every shairport-sync-pa service regardless of
# anything done here. Bouncing/recreating the sinks a little earlier in
# that same restart window doesn't newly interrupt any zone beyond what
# the restart itself was already going to do.
get_owner_module() {
    # Given a sink name, print the module number that owns it.
    local sink_name="$1"
    pactl list sinks | awk -v name="$sink_name" '
        $0 ~ "Name: "name"$" { f=1 }
        f && /Owner Module:/ { print $3; exit }
    '
}

get_module_args() {
    # Given a module number, print its exact load-time "Argument:" string
    # (e.g. "sink_name=Foo master=bar channels=2 channel_map=front-left,front-right"),
    # so the module can be unloaded and reloaded identically.
    local mod="$1"
    pactl list modules | awk -v mod="$mod" '
        $0 == "Module #"mod { f=1; next }
        f && /^Module #/ { exit }
        f && /Argument:/ {
            sub(/^[ \t]*Argument:[ \t]*/, "")
            print
            exit
        }
    '
}

bounce_sink() {
    local sink="$1"
    [[ -z "$sink" ]] && return
    pactl suspend-sink "$sink" 1
    sleep 0.5
    pactl suspend-sink "$sink" 0
}

# --- Deduplicate remap sinks left over from a prior overlapping run -----
# Before the flock fix above existed, two overlapping invocations of this
# script could each unload+reload the same sink name at the same time;
# PulseAudio doesn't error on that, it silently auto-suffixes the loser's
# sink name ".2", ".3", etc. Once such a duplicate module exists, it lingers
# in PulseAudio indefinitely (this addon restarting doesn't reset
# PulseAudio's own module state), and every subsequent run of the
# recreate loop below would just unload and reload it again using ITS OWN
# CAPTURED sink_name= argument -- which is the *original*, un-suffixed
# name -- so PulseAudio collides and re-suffixes it right back to ".2"
# every single time. That's why duplicates like "BA_790.2" kept coming
# back even after the flock fix: the flock stops NEW duplicates from
# being created by two racing runs, but does nothing to clean up ones
# that already exist, and the recreate loop has no way to tell "this is a
# duplicate of another module" from "this is a legitimately different
# zone" -- it just recreates whatever pactl currently lists, forever.
#
# Fix: before bouncing/recreating anything, group all module-remap-sink
# instances by the sink_name= they were ORIGINALLY loaded with (from
# their module arguments, not their current -- possibly auto-suffixed --
# pactl name). Any sink_name with more than one owning module is a
# duplicate: keep exactly one (preferring whichever module's current
# pactl name has no auto-suffix at all, i.e. exactly matches sink_name=)
# and unload the rest outright, without reloading them. Only then does
# the recreate loop below see one module per intended zone.
declare -A canonical_owner       # sink_name -> "module_id current_name" kept
declare -A duplicate_modules_str # sink_name -> "module_id:current_name " extras

while read -r current_name; do
    [[ -z "$current_name" ]] && continue
    owner_module="$(get_owner_module "$current_name")"
    [[ -z "$owner_module" ]] && continue
    args="$(get_module_args "$owner_module")"
    [[ -z "$args" ]] && continue
    sink_name="$(echo "$args" | grep -oE 'sink_name=[^ ]+' | cut -d= -f2)"
    [[ -z "$sink_name" ]] && continue

    if [[ -z "${canonical_owner[$sink_name]:-}" ]]; then
        canonical_owner["$sink_name"]="${owner_module} ${current_name}"
    else
        existing_module="${canonical_owner[$sink_name]%% *}"
        existing_name="${canonical_owner[$sink_name]#* }"
        if [[ "$current_name" == "$sink_name" && "$existing_name" != "$sink_name" ]]; then
            # This candidate is the unsuffixed one -- swap it in as the
            # keeper and demote the previous keeper to a duplicate.
            duplicate_modules_str["$sink_name"]+="${existing_module}:${existing_name} "
            canonical_owner["$sink_name"]="${owner_module} ${current_name}"
        else
            duplicate_modules_str["$sink_name"]+="${owner_module}:${current_name} "
        fi
    fi
done < <(pactl list sinks short | awk '/module-remap-sink/ {print $2}')

for sink_name in "${!duplicate_modules_str[@]}"; do
    for entry in ${duplicate_modules_str[$sink_name]}; do
        dup_module="${entry%%:*}"
        dup_name="${entry#*:}"
        echo "Removing duplicate remap sink ${dup_name} (module #${dup_module}), a leftover extra instance of ${sink_name}" >> "$log_file"
        pactl unload-module "$dup_module"
    done
done

declare -A bounced_masters
while read -r remap_sink; do
    [[ -z "$remap_sink" ]] && continue

    owner_module="$(get_owner_module "$remap_sink")"
    if [[ -z "$owner_module" ]]; then
        echo "Could not resolve owner module for remap sink ${remap_sink}, skipping bounce/recreate" >> "$log_file"
        continue
    fi

    remap_args="$(get_module_args "$owner_module")"
    if [[ -z "$remap_args" ]]; then
        echo "Could not resolve module arguments for ${remap_sink} (module #${owner_module}), skipping bounce/recreate" >> "$log_file"
        continue
    fi

    master_sink="$(echo "$remap_args" | grep -oE 'master=[^ ]+' | cut -d= -f2)"
    if [[ -z "$master_sink" ]]; then
        echo "Could not resolve master= for ${remap_sink} from its module arguments, skipping bounce/recreate" >> "$log_file"
        continue
    fi

    if [[ -z "${bounced_masters[$master_sink]:-}" ]]; then
        bounced_masters["$master_sink"]=1
        echo "Bouncing master sink ${master_sink} (underlies ${remap_sink}) before zone generation" >> "$log_file"
        bounce_sink "$master_sink"
    fi

    echo "Recreating remap sink ${remap_sink} (module #${owner_module}, args: ${remap_args})" >> "$log_file"
    pactl unload-module "$owner_module"
    sleep 0.2
    # Intentional word-splitting: remap_args is a captured
    # "key=value key=value ..." list and pactl load-module expects each
    # key=value as its own word, exactly as it was originally invoked.
    # shellcheck disable=SC2086
    pactl load-module module-remap-sink $remap_args >> "$log_file" 2>&1
done < <(pactl list sinks short | awk '/module-remap-sink/ {print $2}')

# --- Sink-backed zones are no longer generated here ---------------------
# This script used to create one s6 service directory per remap sink. s6-rc
# compiles its service database once, at container start, so a sink that
# appeared or disappeared later (a Bluetooth speaker reconnecting, a USB
# DAC being replugged) needed a full addon restart. sink_zone_manager.py
# (started below as the "airplay2-sink-zone-manager" service) now owns
# those zones the way cast_bridge_manager.py owns the Cast ones: it
# starts one shairport-sync per module-remap-sink, restarts it if it
# dies, and starts/stops zones as sinks come and go.
#
# The persistent name -> port map moved there too (same port_map.txt,
# same "name port" format, shared with cast_bridge_manager.py under a
# flock). Keeping each zone's RTSP port stable is what keeps its AirPlay 2
# device ID, and therefore its Music Assistant player and settings,
# stable: shairport-sync derives the ID from the zone name and its port.
#
# Everything ABOVE this point still runs here, once, before any service
# starts: the master-sink bounce, remap sink recreate and duplicate
# cleanup, so the manager always sees remap sinks in their fresh state.

# --- One-time, container-wide services: dbus -> avahi -> nqptp -> castbridge-manager ---
DBUS_SERVICE="${BASE_DIR}/airplay2-dbus"
if [ ! -d "$DBUS_SERVICE" ]; then
    mkdir -p "$DBUS_SERVICE"
    echo "longrun" > "${DBUS_SERVICE}/type"
    cat > "${DBUS_SERVICE}/run" <<'EOF'
#!/usr/bin/with-contenv bashio
mkdir -p /run/dbus
[ -f /etc/machine-id ] || dbus-uuidgen > /etc/machine-id
exec dbus-daemon --system --nofork --nopidfile
EOF
    chmod +x "${DBUS_SERVICE}/run"
    touch "${CONTENTS_DIR}/airplay2-dbus"
fi

AVAHI_SERVICE="${BASE_DIR}/airplay2-avahi"
if [ ! -d "$AVAHI_SERVICE" ]; then
    mkdir -p "$AVAHI_SERVICE" "${AVAHI_SERVICE}/dependencies.d"
    echo "longrun" > "${AVAHI_SERVICE}/type"
    touch "${AVAHI_SERVICE}/dependencies.d/airplay2-dbus"
    cat > "${AVAHI_SERVICE}/run" <<EOF
#!/usr/bin/with-contenv bashio
# Deliberately no -D: that flag daemonizes/forks, which s6 (expecting a
# foreground process) interprets as an immediate crash-and-restart, while
# the actual backgrounded child from the prior attempt stays alive holding
# the PID file, producing an infinite "Daemon already running" loop.
#
# Restrict avahi-daemon to the real LAN interface. Under host_network:true
# this container sees every interface on the host, not just ${AIRPLAY_INTERFACE}
# — the hassio bridge, docker0, and one veth pair per other addon container
# on this host. Left unrestricted, avahi-daemon listens and publishes on
# all of them, and multicast reflected across the hassio bridge between
# veth peers can make avahi see its own announcement echoed back as if
# from a rival host, producing a perpetual, never-resolving "needs a
# rename" collision loop against its own name.
#
# Also give this daemon its own explicit mDNS hostname. Left unset,
# avahi-daemon defaults to the machine's system hostname — and under
# host_network:true, any OTHER addon on this host that also runs its
# own private avahi-daemon (e.g. the separate AirPlay 1 Multiroom Audio
# addon) inherits that exact same default. Two independent avahi-daemons
# both claiming the identical hostname on the same interface fight
# forever: AVAHI_CLIENT_S_COLLISION is a client/hostname-level state, so
# it resets EVERY entry group that daemon is tracking, not just one
# service — which is why previously-stable zones (not just a newly
# added one) can start cycling through "needs a rename" once a second
# daemon with the same hostname shows up.
awk -v iface="${AIRPLAY_INTERFACE}" '
    /^allow-interfaces=/ { next }
    /^host-name=/ { next }
    /^\[server\]/ { print; print "allow-interfaces=" iface; print "host-name=airplay2-multiroom"; next }
    { print }
' /etc/avahi/avahi-daemon.conf > /tmp/avahi-daemon.conf.new && \
    mv /tmp/avahi-daemon.conf.new /etc/avahi/avahi-daemon.conf
exec avahi-daemon --no-drop-root --no-chroot -s
EOF
    chmod +x "${AVAHI_SERVICE}/run"
    touch "${CONTENTS_DIR}/airplay2-avahi"
fi

NQPTP_SERVICE="${BASE_DIR}/airplay2-nqptp"
if [ ! -d "$NQPTP_SERVICE" ]; then
    mkdir -p "$NQPTP_SERVICE"
    echo "longrun" > "${NQPTP_SERVICE}/type"
    cat > "${NQPTP_SERVICE}/run" <<'EOF'
#!/usr/bin/with-contenv bashio
exec nqptp
EOF
    chmod +x "${NQPTP_SERVICE}/run"
    touch "${CONTENTS_DIR}/airplay2-nqptp"
fi

# cast_bridge_manager.py used to be a separate addon in its own
# container. That meant Cast device discovery and this container's
# nqptp/avahi could never actually reach each other (nqptp's IPC is a
# container-local POSIX shared-memory segment, not something
# host_network:true shares), so Cast Bridge's own shairport-sync
# instances silently fell back to AirPlay 1. Running it here instead —
# in the same container as nqptp/avahi/dbus — fixes that at the root,
# and also removes the need for the two addons to hand a zones file
# back and forth across a shared /config mount: this one process
# discovers Cast devices AND spawns their AirPlay 2 receivers directly,
# no intermediate file or second addon involved.
CASTBRIDGE_MANAGER_SERVICE="${BASE_DIR}/airplay2-castbridge-manager"
if [ ! -d "$CASTBRIDGE_MANAGER_SERVICE" ]; then
    mkdir -p "$CASTBRIDGE_MANAGER_SERVICE" "${CASTBRIDGE_MANAGER_SERVICE}/dependencies.d"
    echo "longrun" > "${CASTBRIDGE_MANAGER_SERVICE}/type"
    touch "${CASTBRIDGE_MANAGER_SERVICE}/dependencies.d/airplay2-dbus"
    touch "${CASTBRIDGE_MANAGER_SERVICE}/dependencies.d/airplay2-avahi"
    touch "${CASTBRIDGE_MANAGER_SERVICE}/dependencies.d/airplay2-nqptp"
    cat > "${CASTBRIDGE_MANAGER_SERVICE}/run" <<'EOF'
#!/usr/bin/with-contenv bashio
mkdir -p /config/cast-bridge/logs
exec python3 /usr/sbin/cast_bridge_manager.py >> /config/cast-bridge/logs/cast_bridge_manager.log 2>&1
EOF
    chmod +x "${CASTBRIDGE_MANAGER_SERVICE}/run"
    touch "${CONTENTS_DIR}/airplay2-castbridge-manager"
fi

# One manager for every PulseAudio-sink zone (see the comment above).
SINK_ZONE_MANAGER_SERVICE="${BASE_DIR}/airplay2-sink-zone-manager"
if [ ! -d "$SINK_ZONE_MANAGER_SERVICE" ]; then
    mkdir -p "$SINK_ZONE_MANAGER_SERVICE" "${SINK_ZONE_MANAGER_SERVICE}/dependencies.d"
    echo "longrun" > "${SINK_ZONE_MANAGER_SERVICE}/type"
    touch "${SINK_ZONE_MANAGER_SERVICE}/dependencies.d/airplay2-dbus"
    touch "${SINK_ZONE_MANAGER_SERVICE}/dependencies.d/airplay2-avahi"
    touch "${SINK_ZONE_MANAGER_SERVICE}/dependencies.d/airplay2-nqptp"
    # Unquoted heredoc on purpose: AIRPLAY_INTERFACE is baked in here, the
    # same way it was baked into each zone's generated config before.
    cat > "${SINK_ZONE_MANAGER_SERVICE}/run" <<EOF
#!/usr/bin/with-contenv bashio
export AIRPLAY_INTERFACE="${AIRPLAY_INTERFACE}"
mkdir -p /config/shairport-sync/logs
exec python3 /usr/sbin/sink_zone_manager.py >> /config/shairport-sync/logs/sink_zone_manager.log 2>&1
EOF
    chmod +x "${SINK_ZONE_MANAGER_SERVICE}/run"
    touch "${CONTENTS_DIR}/airplay2-sink-zone-manager"
fi
