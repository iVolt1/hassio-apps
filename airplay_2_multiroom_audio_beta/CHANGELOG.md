# Changelog

All notable changes to the AirPlay 2 Multiroom Audio add-on are listed here.
Newest first. Format follows [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

### Added
- A zone's name can be set in its `<zone>-ap2.custom` file with `name = "...";`. It
  replaces the generated name instead of duplicating it, which shairport-sync rejects
  as a fatal "duplicate setting name". The name also changes the zone's AirPlay 2
  device ID, so Music Assistant sees it as a new player.

### Changed
- On every container start, all generated zone configs (`*-ap2.conf`) and all logs
  (`*.log` under `/config/shairport-sync/logs` and `/config/cast-bridge/logs`) are
  deleted. The `*-ap2.custom` overrides, `port_map.txt` and `http_port_map.txt` are kept.

### Fixed
- Duplicate PulseAudio remap sinks (names ending in `.2`, `.3`) and the duplicate
  `airplay2-<name>.2` zones they produced. Two overlapping runs of the service
  generator could reload the same sink name, and PulseAudio silently renamed the loser.
  - The generator now takes a lock, so overlapping runs wait instead of racing.
  - A cleanup pass removes duplicates left behind by earlier runs, keeping one module per sink.
- Zones registering over and over (`AVAHI_CLIENT_S_COLLISION` / "needs a rename") because
  the add-on's avahi daemon listened on every host interface, including the hassio bridge,
  docker0 and the veth pairs. It is now restricted to the LAN interface
  and uses its own mDNS host name, `airplay2-multiroom`. The interface is found from the
  default route, not hardcoded; `AIRPLAY_INTERFACE` overrides it, and if none is found
  avahi is left unrestricted.
- HDMI-backed zones that stayed silent after the receiver or TV was power-cycled. Master
  sinks are suspended and resumed at startup so PulseAudio reopens the ALSA device.

### Documentation
- Troubleshooting note: another add-on running its own `avahi-daemon` on the same host
  (for example the Music Assistant Local Audio add-on) makes both daemons collide. Turn the
  other one off, for Local Audio by setting its `server` option to a plain host or URL
  instead of `mdns:…`. This add-on should be the only avahi on the host.
