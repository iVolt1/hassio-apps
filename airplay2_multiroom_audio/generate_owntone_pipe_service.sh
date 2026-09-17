#!/bin/bash
#
# Creates a single, fixed AirPlay 2 receiver whose output is raw PCM
# written to a named pipe, for OwnTone (forked-daapd) to consume as a
# "pipe" input source — instead of a PulseAudio sink like the dynamic
# per-zone instances in generate_airplay2_services.sh.
#
# Deliberately kept as its own script/service rather than folded into
# that per-sink loop: it isn't tied to a PulseAudio remap sink at all,
# and keeping it separate leaves room to later drive it from its own
# addon config options (enable/disable, instance name, pipe path)
# without touching the dynamic generator.
#
# No metadata pipe is set up for now (raw audio only).

log_file="/config/shairport-sync/logs/generate_owntone_pipe_service.log"
mkdir -p "$(dirname "$log_file")"
echo "# OwnTone pipe AirPlay 2 service generated on $(date)" > "$log_file"

is_port_available() {
    ss -tuln | grep -q ":$1 " && return 1 || return 0
}

BASE_DIR="/etc/s6-overlay/s6-rc.d"
CONTENTS_DIR="${BASE_DIR}/user/contents.d"
CONFIG_DIR="/config/shairport-sync/config"
mkdir -p "${CONTENTS_DIR}" "${CONFIG_DIR}"

# Shares the same persisted port map generate_airplay2_services.sh uses,
# keyed by instance name, so this zone's AirPlay 2 device ID is just as
# stable across restarts as the dynamic per-sink ones.
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

# Overridable via env for now; addon-options-driven config can replace
# these later without changing anything else in this script.
OWNTONE_NAME="${OWNTONE_AIRPLAY_NAME:-OwnTone}"
OWNTONE_PIPE="${OWNTONE_PIPE_PATH:-/media/music/owntonepipe}"

service_dir="${BASE_DIR}/airplay2-${OWNTONE_NAME}"
player_log="/config/shairport-sync/logs/${OWNTONE_NAME}-ap2.log"
config_file="${CONFIG_DIR}/${OWNTONE_NAME}-ap2.conf"

mkdir -p "$(dirname "${OWNTONE_PIPE}")"
[ -p "${OWNTONE_PIPE}" ] || mkfifo "${OWNTONE_PIPE}"

if [ -d "$service_dir" ]; then
    echo "Service already exists: airplay2-${OWNTONE_NAME}" >> "$log_file"
    exit 0
fi

mkdir -p "${service_dir}" "${service_dir}/dependencies.d"
echo "longrun" > "${service_dir}/type"
# These one-time services are created by generate_airplay2_services.sh,
# which run_stage2_hooks.sh runs before this script.
touch "${service_dir}/dependencies.d/airplay2-dbus"
touch "${service_dir}/dependencies.d/airplay2-avahi"
touch "${service_dir}/dependencies.d/airplay2-nqptp"

# Fixed, out-of-range starting point so this singleton instance never
# collides with the dynamic per-sink range (5120+) in the other script.
PORT_BASE=5119
mapped_port="$(get_mapped_port "$OWNTONE_NAME")"
if [[ -n "$mapped_port" ]] && is_port_available "$mapped_port"; then
    current_port="$mapped_port"
    echo "Reusing persisted port ${current_port} for ${OWNTONE_NAME}" >> "$log_file"
else
    if [[ -n "$mapped_port" ]]; then
        echo "Persisted port ${mapped_port} for ${OWNTONE_NAME} is no longer free, reassigning" >> "$log_file"
    fi
    while ! is_port_available "$PORT_BASE"; do
        PORT_BASE=$((PORT_BASE + 1))
    done
    current_port=$PORT_BASE
    set_mapped_port "$OWNTONE_NAME" "$current_port"
    echo "Assigned new port ${current_port} for ${OWNTONE_NAME}" >> "$log_file"
fi

# Also fixed/out-of-range vs. both the AP2 (7001+) and AP1 (6001+) dynamic
# udp_port_base ranges, since a single instance doesn't need the
# probing loop the per-sink scripts use.
UDP_PORT_BASE=6500

cat > "${config_file}" <<EOF
general :
{
  name = "${OWNTONE_NAME}";
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
  name = "${OWNTONE_PIPE}";
};
EOF

cat > "${service_dir}/run" <<EOF
#!/usr/bin/with-contenv bashio
truncate -s 0 "${player_log}"
echo "\$(date) - Starting AirPlay 2 receiver: ${OWNTONE_NAME} -> pipe ${OWNTONE_PIPE} on port ${current_port}" >> "${player_log}"
exec shairport-sync \
    -a "${OWNTONE_NAME}" \
    -p ${current_port} \
    -c "${config_file}" \
    -vv \
    >> "${player_log}" 2>&1
EOF
chmod +x "${service_dir}/run"
touch "${CONTENTS_DIR}/airplay2-${OWNTONE_NAME}"

echo "Created: airplay2-${OWNTONE_NAME} -> pipe ${OWNTONE_PIPE} on port ${current_port}" >> "$log_file"
