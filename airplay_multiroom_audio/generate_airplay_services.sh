#!/bin/bash
#
# Creates one shairport-sync (RAOP / AirPlay 1) instance per remap-sink
# zone created by the SEPARATE sendspin-only addon (front_stereo/
# rear_stereo/side_stereo/center_sub/multichannel_stereo) — same sinks,
# same names, discovered the same way generate_sendspin_daemons.sh does
# (filtering pactl list sinks short for module-remap-sink). Both addons
# connect to the same shared hassio_audio PulseAudio server.
#
# This makes AirPlay coverage dependent on the sendspin addon's topology:
# masters with <=2 channels (e.g. USB DACs that don't need zone remapping)
# never get a remap sink from that addon, so they get no AirPlay instance
# here either — same logic, same exclusions, by design.
#
# Also makes this addon's startup order-dependent on the sendspin addon's
# remap-sink creation having already run. If that ever proves to be a
# real problem (e.g. on a cold boot with no guaranteed ordering), the fix
# is a short retry/wait loop here rather than assuming ordering.

echo "$(date) — generate_airplay_services.sh started" >> /tmp/airplay_gen_debug.log

log_file="/config/dbase_and_logs/shairport-sync/generate_airplay_services.log"
mkdir -p "$(dirname "$log_file")"
echo "# AirPlay services generated on $(date)" > "$log_file"

is_port_available() {
    ss -tuln | grep -q ":$1 " && return 1 || return 0
}

BASE_DIR="/etc/s6-overlay/s6-rc.d"
CONTENTS_DIR="${BASE_DIR}/user/contents.d"
CONFIG_DIR="/config/dbase_and_logs/shairport-sync/config"
mkdir -p "${CONTENTS_DIR}" "${CONFIG_DIR}"

# Explicit network interface for shairport-sync to advertise/bind on.
# Without this, "select interface(s) automatically" appears to have been
# choosing the internal Docker/hassio bridge (172.30.32.1) over the real
# LAN interface under host_network:true — RTSP negotiation succeeded but
# the client couldn't reach the internal address for the actual RTP audio
# stream, producing exactly "connects, no sound". Confirmed real LAN
# interface name from earlier avahi enumeration on this host: enp5s0.
# Override via env var if this ever needs to differ (different hardware,
# a rename, etc.) rather than editing this default blind.
AIRPLAY_INTERFACE="${AIRPLAY_INTERFACE:-enp5s0}"
echo "Using network interface: ${AIRPLAY_INTERFACE}" >> "$log_file"

PORT_BASE=5020
UDP_PORT_BASE=6001
while :; do
    is_port_available "$PORT_BASE" && break
    PORT_BASE=$((PORT_BASE + 1))
done
echo "Starting AirPlay port assignments at: $PORT_BASE" >> "$log_file"

# Target the SAME sinks the sendspin addon creates — its own remap-sink
# zones (front_stereo/rear_stereo/side_stereo/center_sub/multichannel_
# stereo), discovered exactly the way generate_sendspin_daemons.sh already
# does. Their names are already final and uniquely tagged by that addon's
# own naming scheme, so no naming logic is needed here — just use them
# directly.
#
# NOTE: this makes AirPlay instance creation dependent on the sendspin
# addon having already created its remap-sink topology. If this addon's
# service-generation hook runs before that topology exists (e.g. on a
# cold boot where startup order isn't guaranteed), this pass will find
# nothing and create zero instances. If that turns out to be a real
# problem, the fix is a short retry/wait loop here rather than assuming
# startup order.
#
# NOTE: masters with <=2 channels (e.g. the DragonFly S/PDIF DAC, the
# ELEGIANT USB DAC) never get a remap sink from the sendspin addon at
# all — it skips them entirely, matching remap_topology.py's behavior.
# Under "same sinks as sendspin", those two cards get no AirPlay instance
# either, by the same logic.
while read -r sink; do
    [[ -z "$sink" ]] && continue
    [[ "$sink" == sendspin_* ]] && continue

    friendly_name="$sink"
    service_dir="${BASE_DIR}/airplay-${friendly_name}"
    player_log="/config/dbase_and_logs/shairport-sync/${friendly_name}.log"
    config_file="${CONFIG_DIR}/${friendly_name}.conf"

    if [ -d "$service_dir" ]; then
        echo "Service already exists: airplay-${friendly_name}" >> "$log_file"
        PORT_BASE=$((PORT_BASE + 1))
        continue
    fi

    mkdir -p "${service_dir}"
    echo "longrun" > "${service_dir}/type"

    # Re-verify port availability for THIS zone specifically — checking
    # once at the top isn't enough; every subsequent assignment needs its
    # own check or a taken port silently crash-loops that instance.
    while ! is_port_available "$PORT_BASE"; do
        echo "Port $PORT_BASE unavailable, skipping" >> "$log_file"
        PORT_BASE=$((PORT_BASE + 1))
    done

    UDP_PORT_BASE=$((UDP_PORT_BASE + 10))
    current_port=$PORT_BASE
    current_sink=$sink
    current_name=$friendly_name
    current_log=$player_log
    current_udp_base=$UDP_PORT_BASE

    cat > "${config_file}" <<EOF
general :
{
  name = "${current_name}";
  port = ${current_port};
  interface = "${AIRPLAY_INTERFACE}";
  output_backend = "pa";
  udp_port_base = ${current_udp_base};
  audio_backend_buffer_desired_length_in_seconds = 0.5; 
};
sessioncontrol :
{
  allow_session_interruption = "yes";
};
pa :
{
  sink = "${current_sink}";
  application_name = "Shairport Sync";
  output_format = "S24_lE"; 
};
EOF

    cat > "${service_dir}/run" <<EOF
#!/usr/bin/with-contenv bashio

truncate -s 0 "${current_log}"

echo "\$(date) - Starting AirPlay (RAOP) receiver: ${current_name} for ${current_sink} on port ${current_port}" >> "${current_log}"

exec shairport-sync \
    -a "${current_name}" \
    -p ${current_port} \
    -c "${config_file}" \
	-vv \
    >> "${current_log}" 2>&1
EOF

    chmod +x "${service_dir}/run"
    touch "${CONTENTS_DIR}/airplay-${friendly_name}"

    echo "Created: airplay-${friendly_name} -> ${current_sink} on port ${current_port} (udp base ${current_udp_base})" >> "$log_file"
    PORT_BASE=$((PORT_BASE + 1))

done < <(pactl list sinks short | awk '/module-remap-sink/ {print $2}')

echo "$(date) — generate_airplay_services.sh ended" >> /tmp/airplay_gen_debug.log