# Changelog

All notable changes to the AirPlay 2 Multiroom Audio add-on are listed here.
Newest first. Format follows [Keep a Changelog](https://keepachangelog.com/).

## [2.0] - 2026-11-02

This release brings the beta channel's work since 1.01 to the stable add-on.

### Added
- **Own LAN IP for the zones** (`ap2_ip`, `ap2_gateway`, `ap2_mac`, `ap2_shim_ip`).
  avahi, nqptp and all zones run in a private network namespace on a macvlan with
  their own address, plus a host-side shim so apps on the host can reach them. This
  lets Music Assistant on the same host use AirPlay 2 with PTP timing instead of
  falling back to compatibility mode. If setup fails, the add-on starts on the host's
  address as before. Requires the new `NET_ADMIN`/`SYS_ADMIN` privileges and runs
  without AppArmor.
- **Zones follow PulseAudio while running.** A new zone manager starts and stops zones
  as remap sinks appear and disappear (event-driven, with a 30 s re-check), restarts a
  zone's shairport-sync if it exits, and restarts all zones after a PulseAudio restart.
  Returning zones keep their port and AirPlay 2 device ID.
- **Bluetooth speakers get a zone automatically**, named after the speaker, when their
  A2DP sink appears. No remap sink needed; a remap sink on top still takes precedence.
- **`log_level` option** (`v`, `vv`, `vvv`) for the per-zone shairport-sync logs.
- **Per-zone rename**: `name = "...";` in a zone's `<zone>-ap2.custom` file replaces the
  generated name instead of causing a fatal duplicate setting.
- shairport-sync statistics in each zone log while playing.
- Option descriptions in the add-on's Configuration tab.

### Changed
- **shairport-sync is pinned to the 5.6.2 release** (previously the development
  branch), built with a compatibility patch for Music Assistant's AirPlay 2 sender:
  - derives the audio format from the stream SETUP when the sender sends SSRC 0
    (see mikebrady/shairport-sync#2319);
  - compares flush sequence numbers in the sender's 16-bit space, so track changes
    within a session play instead of going silent;
  - adds ALAC 44.1 kHz/24-bit decoding and resampling.
  Apple senders are unaffected. The patch is `sps-ssrc0-fallback.patch`.
- With `ap2_ip` set, the zones are IPv4-only.
- shairport-sync is built with metadata and MQTT support (not configured by the add-on yet).
- On every start, generated zone configs (`*-ap2.conf`) and all logs are deleted.
  `*-ap2.custom` files, `port_map.txt` and `http_port_map.txt` are kept.

### Fixed
- Duplicate PulseAudio remap sinks (names ending `.2`, `.3`) and the duplicate zones
  they produced: the service generator now takes a lock, and a cleanup pass removes
  leftovers from earlier runs.
- Zones re-registering over and over (`AVAHI_CLIENT_S_COLLISION`): avahi is restricted to
  the LAN interface (from the default route, or `AIRPLAY_INTERFACE`) and uses its own
  host name, `airplay2-multiroom`.
- HDMI-backed zones staying silent after the receiver or TV was power-cycled: master
  sinks are suspended and resumed at startup.

## [1.01] - 2026-10-05

First public release: AirPlay 2 zones for PulseAudio remap sinks and Google Cast
devices, built on shairport-sync and nqptp.
