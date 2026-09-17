#!/bin/bash
#
# Creates one pipe-output AirPlay 2 receiver per Cast Bridge target,
# in THIS container — which already has working avahi + nqptp (see
# generate_airplay2_services.sh) — instead of in the separate Cast
# Bridge addon's own container, which has neither. Running
# shairport-sync there was tried first and its own log showed exactly
# why it can't work: nqptp's IPC is a POSIX shared-memory segment
# (/nqptp-<name>), which is container-local — host_network:true only
# shares the network namespace, not /dev/shm — so a shairport-sync
# instance in a different container can never reach this addon's
# nqptp, no matter how the network is configured. Its own log showed:
#   "NQPTP service not found."
#   "Startup in Classic AirPlay (aka "AirPlay 1") mode. (AirPlay2 build.)"
# And Cast Bridge's container can't just run its own nqptp either —
# nqptp binds a fixed, host-wide port (319) and only one instance can
# ever own it per host; this addon already does.
#
# Cast Bridge (the separate addon) discovers Cast devices via
# pychromecast and writes the zones file below; this script turns
# that list into real AirPlay 2 receivers here. The FIFOs created
# below live under /media/music/castbridge/, which Cast Bridge's own
# container ALSO mounts — both addons map "media:rw" to the same
# shared Home Assistant media directory, so shairport-sync writing
# here and Cast Bridge's ffmpeg reading there is just two processes
# in two containers opening the same file on the same underlying
# host filesystem, no different from doing it within one container.
#
# Zones file format, one line per zone:
#   <name> <fifo_path> [output_rate] [output_format]
# rate/format default to 48000/S32_LE if omitted — shairport-sync's
# own documented AirPlay 2 defaults (per its reference config: quote
# from the ap2 build: "Default is 48000 for AirPlay 2" / "Default is
# S32_LE for AirPlay 2").
#
# IMPORTANT: this script only runs at THIS addon's own startup, same
# as the others. If Cast Bridge discovers a new Cast device after
# this addon is already running, its zones file gets updated but this
# addon won't create that device's receiver until it's restarted.
# Deliberately not automated (e.g. Cast Bridge triggering a Supervisor
# restart of this addon on every boot): that would also interrupt
# every other already-working AirPlay 2 zone here (Kitchen, BA_790,
# etc.) every time Cast Bridge restarts — worse than a manual restart
# on the rare occasion your Cast devices actually change.

log_file="/config/shairport-sync/logs/generate_castbridge_pipe_services.log"
mkdir -p "$(dirname "$log_file")"
echo "# Cast Bridge pipe AirPlay 2 services generated on $(date)" > "$log_file"

is_port_available() {
    ss -tuln | grep -q ":$1 " && return 1 || return 0
}

BASE_DIR="/etc/s6-overlay/s6-rc.d"
CONTENTS_DIR="${BASE_DIR}/user/contents.d"
CONFIG_DIR="/config/shairport-sync/config"
mkdir -p "${CONTENTS_DIR}" "${CONFIG_DIR}"

# Shared with generate_airplay2_services.sh — same file, same
# "name<space>port" format, so no generator can ever assign a port
# another one already owns for a different name.
PORT_MAP_FILE="${CONFIG_DIR}/port_map.txt"
touch "${PORT_MAP_FILE}"

get_mapped_port() {
    awk -v name="$1" '$1 == name { print $2; exit }' "${PORT_MAP_FILE}"
}

set_mapped_port() {
    local tmp
    tmp="$(mktemp)"
    awk -v name="$1" '$1 != name' "${PORT_MAP_FILE}" > "$tmp"
    echo "$1 $2" >> "$tmp"
    mv "$tmp" "${PORT_MAP_FILE}"
}

AIRPLAY_INTERFACE="${AIRPLAY_INTERFACE:-enp5s0}"

CASTBRIDGE_ZONES_FILE="${CONFIG_DIR}/castbridge_zones.txt"
touch "${CASTBRIDGE_ZONES_FILE}"

# Separate starting port from the dynamic per-sink zones (5120+), so
# the two generators don't race for the same starting port. Real
# safety still comes from is_port_available()/port_map.txt regardless.
PORT_BASE=5150
UDP_PORT_BASE=6800

zone_count=0
while read -r zone_name zone_pipe zone_rate zone_format; do
    [[ -z "$zone_name" ]] && continue
    [[ "$zone_name" == \#* ]] && continue
    [[ -z "$zone_pipe" ]] && { echo "Skipping malformed line for '${zone_name}' (no fifo path)" >> "$log_file"; continue; }
    zone_rate="${zone_rate:-48000}"
    zone_format="${zone_format:-S32_LE}"

    zone_count=$((zone_count + 1))
    service_dir="${BASE_DIR}/airplay2-${zone_name}"
    player_log="/config/shairport-sync/logs/${zone_name}-ap2.log"
    config_file="${CONFIG_DIR}/${zone_name}-ap2.conf"

    mkdir -p "$(dirname "${zone_pipe}")"
    [ -p "${zone_pipe}" ] || mkfifo "${zone_pipe}"

    if [ -d "$service_dir" ]; then
        echo "Service already exists: airplay2-${zone_name}" >> "$log_file"
        continue
    fi

    mkdir -p "${service_dir}" "${service_dir}/dependencies.d"
    echo "longrun" > "${service_dir}/type"
    touch "${service_dir}/dependencies.d/airplay2-dbus"
    touch "${service_dir}/dependencies.d/airplay2-avahi"
    touch "${service_dir}/dependencies.d/airplay2-nqptp"

    mapped_port="$(get_mapped_port "$zone_name")"
    if [[ -n "$mapped_port" ]] && is_port_available "$mapped_port"; then
        current_port="$mapped_port"
        echo "Reusing persisted port ${current_port} for ${zone_name}" >> "$log_file"
    else
        if [[ -n "$mapped_port" ]]; then
            echo "Persisted port ${mapped_port} for ${zone_name} is no longer free, reassigning" >> "$log_file"
        fi
        while ! is_port_available "$PORT_BASE"; do
            PORT_BASE=$((PORT_BASE + 1))
        done
        current_port=$PORT_BASE
        set_mapped_port "$zone_name" "$current_port"
        echo "Assigned new port ${current_port} for ${zone_name}" >> "$log_file"
    fi
    PORT_BASE=$((PORT_BASE + 1))
    UDP_PORT_BASE=$((UDP_PORT_BASE + 10))

    cat > "${config_file}" <<EOF
general :
{
  name = "${zone_name}";
  port = ${current_port};
  interface = "${AIRPLAY_INTERFACE}";
  output_backend = "pipe";
  udp_port_base = ${UDP_PORT_BASE};
  mdns_backend = "avahi";
  audio_backend_buffer_desired_length_in_seconds = 0.1;
  audio_decoded_buffer_desired_length_in_seconds = 0.35;
  audio_backend_buffer_interpolation_threshold_in_seconds = 0.05;
};
sessioncontrol :
{
  allow_session_interruption = "yes";
};
pipe :
{
  name = "${zone_pipe}";
  output_rate = ${zone_rate};
  output_format = "${zone_format}";
};
EOF

    cat > "${service_dir}/run" <<EOF
#!/usr/bin/with-contenv bashio
truncate -s 0 "${player_log}"
echo "\$(date) - Starting AirPlay 2 receiver: ${zone_name} -> pipe ${zone_pipe} on port ${current_port}" >> "${player_log}"
exec shairport-sync \
    -a "${zone_name}" \
    -p ${current_port} \
    -c "${config_file}" \
    -vv \
    >> "${player_log}" 2>&1
EOF
    chmod +x "${service_dir}/run"
    touch "${CONTENTS_DIR}/airplay2-${zone_name}"

    echo "Created: airplay2-${zone_name} -> pipe ${zone_pipe} on port ${current_port} (${zone_rate} ${zone_format})" >> "$log_file"
done < <(grep -v '^\s*#' "${CASTBRIDGE_ZONES_FILE}" | grep -v '^\s*$')

echo "Total Cast Bridge zones processed: ${zone_count}" >> "$log_file"
