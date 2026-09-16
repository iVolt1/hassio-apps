#!/bin/bash
#
# AirPlay 2 counterpart to generate_airplay_services.sh (the AP1/RAOP
# addon's generator). Same remap-sink discovery and per-zone service
# generation logic, converted to shairport-sync's --with-airplay-2
# build. See that script's own header for the shared remap-sink/
# Multiroom Audio addon dependency notes — unchanged here.
#
# AP2 needs three things the AP1 script never had to worry about,
# running ONCE for the whole container rather than once per zone:
#   1. A D-Bus system bus (dbus-daemon)
#   2. A real avahi-daemon (AP1 uses --without-avahi --with-tinysvcmdns,
#      so needs neither)
#   3. nqptp — the PTP timing companion daemon. Despite what per-zone
#      log lines like "nqptp shared memory interface name derived from
#      name: /nqptp-LivingRoom" might suggest, nqptp itself is a SINGLE
#      system-wide process, not one per zone: it's the only thing that
#      can bind the privileged UDP 319/320 ports on the whole host (see
#      the "Address already in use" conflict this caused during earlier
#      cliairplay testing). Each shairport-sync instance just registers
#      its own named shared-memory interface with that one running
#      process — don't spawn a second nqptp per zone, it'll fail to bind
#      and every zone after the first will silently lose PTP sync.
#
# All three are created as their own s6 longrun services, once,
# idempotently, and every per-zone shairport-sync service gets explicit
# s6-rc dependencies.d entries on them — enforced start order, not a
# hopeful race.

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

# --- One-time, container-wide services: dbus -> avahi -> nqptp ---
#
# Each block is a no-op if the service dir already exists, so this is
# safe to leave in a script that otherwise only runs once at stage2
# anyway — belt and suspenders against this hook ever running twice.

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
    echo "Created: airplay2-dbus" >> "$log_file"
fi

AVAHI_SERVICE="${BASE_DIR}/airplay2-avahi"
if [ ! -d "$AVAHI_SERVICE" ]; then
    mkdir -p "$AVAHI_SERVICE" "${AVAHI_SERVICE}/dependencies.d"
    echo "longrun" > "${AVAHI_SERVICE}/type"
    touch "${AVAHI_SERVICE}/dependencies.d/airplay2-dbus"
    cat > "${AVAHI_SERVICE}/run" <<'EOF'
#!/usr/bin/with-contenv bashio
exec avahi-daemon --no-drop-root -D --no-chroot -s
EOF
    chmod +x "${AVAHI_SERVICE}/run"
    touch "${CONTENTS_DIR}/airplay2-avahi"
    echo "Created: airplay2-avahi (depends on airplay2-dbus)" >> "$log_file"
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
    echo "Created: airplay2-nqptp (single system-wide instance)" >> "$log_file"
fi

# Explicit network interface for shairport-sync to advertise/bind on —
# same fix, same reasoning, as the AP1 script: "select interface(s)
# automatically" picks the Docker/hassio bridge over the real LAN
# interface under host_network:true otherwise.
AIRPLAY_INTERFACE="${AIRPLAY_INTERFACE:-enp5s0}"
echo "Using network interface: ${AIRPLAY_INTERFACE}" >> "$log_file"

PORT_BASE=5120
UDP_PORT_BASE=7001
while :; do
    is_port_available "$PORT_BASE" && break
    PORT_BASE=$((PORT_BASE + 1))
done
echo "Starting AirPlay 2 port assignments at: $PORT_BASE" >> "$log_file"
# NOTE: deliberately a different port range from the AP1 script's
# 5020/6001 bases. If both the AP1 and AP2 addons ever run at the same
# time against the same remap-sink zones, they'd otherwise both claim
# the same starting ports for what would then be two competing AirPlay
# services (one RAOP, one AP2) advertised for the same physical output.
# Worth deciding deliberately whether that dual-advertise is wanted
# (offering both during a transition) or whether one addon should be
# disabled per zone — this script doesn't make that call for you.

while read -r sink; do
    [[ -z "$sink" ]] && continue
    [[ "$sink" == sendspin_* ]] && continue

    friendly_name="$sink"
    service_dir="${BASE_DIR}/airplay2-${friendly_name}"
    player_log="/config/shairport-sync/logs/${friendly_name}-ap2.log"
    config_file="${CONFIG_DIR}/${friendly_name}-ap2.conf"

    if [ -d "$service_dir" ]; then
        echo "Service already exists: airplay2-${friendly_name}" >> "$log_file"
        PORT_BASE=$((PORT_BASE + 1))
        continue
    fi

    mkdir -p "${service_dir}" "${service_dir}/dependencies.d"
    echo "longrun" > "${service_dir}/type"

    # This zone's shairport-sync instance must not start before dbus,
    # avahi, and nqptp are all up.
    touch "${service_dir}/dependencies.d/airplay2-dbus"
    touch "${service_dir}/dependencies.d/airplay2-avahi"
    touch "${service_dir}/dependencies.d/airplay2-nqptp"

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
  output_backend = "pulseaudio";
  udp_port_base = ${current_udp_base};
  mdns_backend = "avahi";
};
sessioncontrol :
{
  allow_session_interruption = "yes";
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

    echo "Created: airplay2-${friendly_name} -> ${current_sink} on port ${current_port} (udp base ${current_udp_base})" >> "$log_file"
    PORT_BASE=$((PORT_BASE + 1))

done < <(pactl list sinks short | awk '/module-remap-sink/ {print $2}')

echo "$(date) — generate_airplay2_services.sh ended" >> /tmp/airplay2_gen_debug.log
