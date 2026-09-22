#!/bin/bash
echo "$(date) — generate_airplay2_services.sh started" >> /tmp/airplay2_gen_debug.log
log_file="/config/shairport-sync/logs/generate_airplay2_services.log"
mkdir -p "$(dirname "$log_file")"
echo "# AirPlay 2 services generated on $(date)" > "$log_file"

is_port_available() {
    ss -tuln | grep -q ":$1 " && return 1 || return 0
}

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

# --- Persistent name -> port map ---------------------------------------
# /etc/s6-overlay/s6-rc.d does NOT survive a container restart, so the
# "service dir already exists, reuse its port" check below can never
# actually fire on a fresh boot. Meanwhile shairport-sync derives each
# zone's AirPlay 2 device ID from its instance name AND (per upstream
# PR #2286) its RTSP port when instances share an address. That means
# any time PORT_BASE's assignment depends on pactl's sink iteration
# order (which is not guaranteed stable — remap sinks are created
# dynamically by a separate addon), a zone's device ID can change
# across a restart, which Music Assistant sees as a brand-new player
# with default settings ("Streaming mode" resets to Automatic) plus an
# orphaned leftover entry for the old ID.
#
# Fix: persist the name -> port assignment to /config (this addon's
# persistent per-addon storage, kept across restarts) and reuse it
# forever, only falling forward to a new port for a given name if its
# previously-recorded port is actually taken by something else at
# generation time. This same file is also shared with
# cast_bridge_manager.py's Cast Bridge zones below, so no two zones —
# sink-backed or Cast-backed — can ever collide on a port.
PORT_MAP_FILE="${CONFIG_DIR}/port_map.txt"
touch "${PORT_MAP_FILE}"

get_mapped_port() {
    # Prints the persisted port for $1, or nothing if not yet assigned.
    awk -v name="$1" '$1 == name { print $2; exit }' "${PORT_MAP_FILE}"
}

set_mapped_port() {
    # Records name=$1 -> port=$2, replacing any prior entry for that name.
    local tmp
    tmp="$(mktemp)"
    awk -v name="$1" '$1 != name' "${PORT_MAP_FILE}" > "$tmp"
    echo "$1 $2" >> "$tmp"
    mv "$tmp" "${PORT_MAP_FILE}"
}

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

PORT_BASE=5120
UDP_PORT_BASE=7001
while :; do
    is_port_available "$PORT_BASE" && break
    PORT_BASE=$((PORT_BASE + 1))
done

while read -r sink; do
    [[ -z "$sink" ]] && continue
    [[ "$sink" == sendspin_* ]] && continue

    friendly_name="$sink"
    service_dir="${BASE_DIR}/airplay2-${friendly_name}"
    player_log="/config/shairport-sync/logs/${friendly_name}-ap2.log"
    config_file="${CONFIG_DIR}/${friendly_name}-ap2.conf"

    if [ -d "$service_dir" ]; then
        PORT_BASE=$((PORT_BASE + 1))
        continue
    fi

    mkdir -p "${service_dir}" "${service_dir}/dependencies.d"
    echo "longrun" > "${service_dir}/type"
    touch "${service_dir}/dependencies.d/airplay2-dbus"
    touch "${service_dir}/dependencies.d/airplay2-avahi"
    touch "${service_dir}/dependencies.d/airplay2-nqptp"

    # Reuse this zone's previously-assigned port if it's still free.
    # Only fall forward to a new port (and re-persist it) if the old
    # one is actually taken by something else right now — keeping the
    # port, and therefore the derived AirPlay 2 device ID, stable
    # across restarts instead of drifting with pactl's scan order.
    mapped_port="$(get_mapped_port "$friendly_name")"
    if [[ -n "$mapped_port" ]] && is_port_available "$mapped_port"; then
        current_port="$mapped_port"
        echo "Reusing persisted port ${current_port} for ${friendly_name}" >> "$log_file"
    else
        if [[ -n "$mapped_port" ]]; then
            echo "Persisted port ${mapped_port} for ${friendly_name} is no longer free, reassigning" >> "$log_file"
        fi
        while ! is_port_available "$PORT_BASE"; do
            PORT_BASE=$((PORT_BASE + 1))
        done
        current_port=$PORT_BASE
        set_mapped_port "$friendly_name" "$current_port"
        echo "Assigned new port ${current_port} for ${friendly_name}" >> "$log_file"
        PORT_BASE=$((PORT_BASE + 1))
    fi

    UDP_PORT_BASE=$((UDP_PORT_BASE + 10))
    current_sink=$sink
    current_name="${friendly_name//_/ }"
    current_log=$player_log
    current_udp_base=$UDP_PORT_BASE

    cat > "${config_file}" <<EOF
general :
{
  name = "${current_name}";
  port = ${current_port};
  interface = "${AIRPLAY_INTERFACE}";
  output_backend = "pulseaudio";
  udp_port_base = ${current_udp_base};
  mdns_backend = "avahi";
};
sessioncontrol :
{
  allow_session_interruption = "yes";
};
diagnostics :
{
  log_show_time_since_startup = "yes";
  log_show_time_since_last_message = "no";
};
pulseaudio :
{
  sink = "${current_sink}";
  application_name = "Shairport Sync";
};
EOF

    cat > "${service_dir}/run" <<EOF
#!/usr/bin/with-contenv bashio
truncate -s 0 "${current_log}"
echo "\$(date) - Starting AirPlay 2 receiver: ${current_name} for ${current_sink} on port ${current_port}" >> "${current_log}"
exec shairport-sync \
    -a "${current_name}" \
    -p ${current_port} \
    -c "${config_file}" \
    -vv \
    >> "${current_log}" 2>&1
EOF
    chmod +x "${service_dir}/run"
    touch "${CONTENTS_DIR}/airplay2-${friendly_name}"
done < <(pactl list sinks short | awk '/module-remap-sink/ {print $2}')
