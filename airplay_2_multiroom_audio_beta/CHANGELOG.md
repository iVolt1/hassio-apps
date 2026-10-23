# Changelog

All notable changes to the AirPlay 2 Multiroom Audio add-on are listed here.
Newest first. Format follows [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

### Fixed
- Duplicate PulseAudio remap sinks (names ending in `.2`, `.3`) and the duplicate
  `airplay2-<name>.2` zones they produced. Two overlapping runs of the service
  generator could reload the same sink name, and PulseAudio silently renamed the loser.
  - The generator now takes a lock, so overlapping runs wait instead of racing.
  - A cleanup pass removes duplicates left behind by earlier runs, keeping one module per sink.
- Zones registering over and over (`AVAHI_CLIENT_S_COLLISION` / "needs a rename") because
  the add-on's avahi daemon listened on every host interface, including the hassio bridge,
  docker0 and the veth pairs. It is now restricted to the LAN interface
  (`AIRPLAY_INTERFACE`, default `enp5s0`) and uses its own mDNS host name, `airplay2-multiroom`.
- HDMI-backed zones that stayed silent after the receiver or TV was power-cycled. Master
  sinks are suspended and resumed at startup so PulseAudio reopens the ALSA device.

### Documentation
- Troubleshooting note: another add-on running its own `avahi-daemon` on the same host
  (for example the Music Assistant Local Audio add-on) makes both daemons collide. Turn the
  other one off, for Local Audio by setting its `server` option to a plain host or URL
  instead of `mdns:…`. This add-on should be the only avahi on the host.
