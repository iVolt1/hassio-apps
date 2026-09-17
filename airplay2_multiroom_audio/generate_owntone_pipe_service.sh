#!/bin/bash
#
# Creates one or more fixed AirPlay 2 receivers whose output is raw PCM
# written to a named pipe — for OwnTone (forked-daapd), or any future
# "Cast Bridge" addon FIFO consumer, to read as a pipe input source.
# Deliberately kept separate from generate_airplay2_services.sh's dynamic
# per-PulseAudio-sink loop: these zones aren't tied to a remap sink at
# all, and keeping this generator separate leaves room to eventually
# drive it from its own addon config options.
#
# No metadata pipe is set up for now (raw audio only).
#
# --- Config-driven, multi-zone (generalized from the single hardcoded
# OwnTone instance) ---------------------------------------------------
# Zones are read from a small config file, one
# "<name> <fifo_path> [output_rate] [output_format]" line per line
# (whitespace-separated, name has no spaces). rate/format are optional
# and default to 44100 / S16_LE (see below) if omitted. Blank lines
# and lines starting with # are ignored.
#
#   PIPE_ZONES_FILE="${CONFIG_DIR}/pipe_zones.txt"
#
# The output_rate/output_format defaults matter: shairport-sync's
# AirPlay 2 pipe backend otherwise defaults to 48000, which OwnTone's
# pipe input doesn't handle well — explicitly forcing 44100/S16_LE is
# what actually fixed OwnTone playback from this pipe. A future
# Cast Bridge zone that wants to match Chromecast's native 48000 rate
# (to skip ffmpeg resampling) can override this per-line, e.g.:
#   CastKitchen /media/music/castkitchenpipe 48000 S16_LE
#
# On first run (file doesn't exist yet), the file is seeded with a
# single default entry built from the legacy OWNTONE_AIRPLAY_NAME /
# OWNTONE_PIPE_PATH env vars (or their hardcoded defaults), so existing
# deployments keep working with zero config changes. To add more pipe
# zones later (e.g. one per future Cast Bridge target), just add more
# lines to pipe_zones.txt and restart the addon — each gets its own s6
# service, its own shairport-sync "pipe" backend config, and its own
# port persisted in the same port_map.txt used by
# generate_airplay2_services.sh, so device IDs stay stable across
# restarts exactly the same way the per-sink zones do.

log_file="/config/shairport-sync/logs/generate_owntone_pipe_service.log"
mkdir -p "$(dirname "$log_file")"
echo "# Pipe-output AirPlay 2 services generated on $(date)" > "$log_file"

is_port_available() {
    ss -tuln | grep -q ":$1 " && return 1 || return 0
}

BASE_DIR="/etc/s6-overlay/s6-rc.d"
CONTENTS_DIR="${BASE_DIR}/user/contents.d"
CONFIG_DIR="/config/shairport-sync/config"
mkdir -p "${CONTENTS_DIR}" "${CONFIG_DIR}"

# Shared with generate_airplay2_services.sh — same file, same format
# (name<space>port per line), so pipe zones and PulseAudio-sink zones
# never fight over a port a name has already been assigned.
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

PIPE_ZONES_FILE="${CONFIG_DIR}/pipe_zones.txt"
if [ ! -f "${PIPE_ZONES_FILE}" ]; then
    default_name="${OWNTONE_AIRPLAY_NAME:-OwnTone}"
    default_pipe="${OWNTONE_PIPE_PATH:-/media/music/owntonepipe}"
    cat > "${PIPE_ZONES_FILE}" <<EOF
# One pipe-output AirPlay 2 zone per line:
#   <name> <fifo_path> [output_rate] [output_format]
# rate/format default to 44100 / S16_LE if omitted.
# Blank lines and lines starting with # are ignored.
${default_name} ${default_pipe} 44100 S16_LE
EOF
    echo "Seeded default pipe zone: ${default_name} -> ${default_pipe}" >> "$log_file"
fi

# Separate, lower port range than generate_airplay2_services.sh's
# dynamic per-sink loop (which starts at 5120), so the two generators
# never race for the same starting port. Actual collisions are still
# guarded by is_port_available()/port_map.txt regardless.
PORT_BASE=5100
UDP_PORT_BASE=6500

zone_count=0
while read -r zone_name zone_pipe zone_rate zone_format; do
    [[ -z "$zone_name" ]] && continue
    [[ "$zone_name" == \#* ]] && continue
    [[ -z "$zone_pipe" ]] && { echo "Skipping malformed line for '${zone_name}' (no fifo path)" >> "$log_file"; continue; }
    zone_rate="${zone_rate:-44100}"
    zone_format="${zone_format:-S16_LE}"

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
done < <(grep -v '^\s*#' "${PIPE_ZONES_FILE}" | grep -v '^\s*$')

echo "Total pipe zones processed: ${zone_count}" >> "$log_file"
