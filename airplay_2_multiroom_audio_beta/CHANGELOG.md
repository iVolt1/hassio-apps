# Changelog

All notable changes to the AirPlay 2 Multiroom Audio add-on are listed here.
Newest first. Format follows [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

### Added
- Web panel: sound-card and Bluetooth zones have an **Edit** form for AirPlay name, latency
  offset (ms) and max volume (dB). Saving writes them to a marked block in the zone's
  `<zone>-ap2.custom` file (an empty field leaves that setting out; your own lines outside
  the block are kept) and restarts only that zone, which the zone manager starts again at
  once with the new settings. A setting you already set by hand in that file is shown
  locked, since libconfig rejects a setting set twice. Cast Bridge zones are read-only.
- Web panel: **Zones** section listing every running AirPlay 2 receiver (sound-card,
  Bluetooth and Cast Bridge zones): name, type, what it plays to, RTSP port, overrides set
  in its `<zone>-ap2.custom` file (latency offset, max volume, custom name), whether it is
  playing, and uptime. Read from the running shairport-sync processes, so it shows what
  is actually running. `/api/state` includes it as `zones`.
- Web panel: **Create sink** — pick a sound card and one of its free channel pairs; the
  sink is added to `remap_sinks` under its system name (shown before you create it) and
  loaded at once, and its zone starts. Rename it afterwards like any other sink.
- Web panel: **Adopt** — an unmanaged remap sink (e.g. one made by the Multi-room Audio
  Controller) is added to `remap_sinks` under its current name, keeping its description,
  so its zone and Music Assistant player stay the same; it is reloaded once with this
  add-on's settings. Sinks whose channels can't be expressed in the options (such as
  `lfe,lfe`) show why they can't be adopted.
- Web panel: managed remap sinks can be renamed. Apply saves the `remap_sinks` option
  through the Supervisor (so the Configuration tab matches), then applies it at once with
  no add-on restart: the old sink is unloaded, the new one loaded, and the zone manager
  restarts the zone under its new name. The panel shows the result lines. Needs
  `hassio_api: true`, used only to save this add-on's own options.

### Fixed
- `remap_sinks`: a renamed sink was skipped as clashing with its own old copy. Sinks the
  add-on is about to unload no longer count as using their channels, and removals now run
  before loads. Runs of `remap_sinks.py` are serialized with a lock.
- Web panel in the Home Assistant sidebar ("AirPlay 2 Zones", via Ingress), read-only for
  now: sound cards with their used and free channel pairs, and every remap sink with its
  system name (with a copy button), card, channels, whether the add-on manages it, and
  whether its zone is running. `/api/state` returns the same data as JSON. Only Home
  Assistant's Ingress proxy and localhost may connect. Log: `web_panel.log`.
- `remap_sinks` entries may leave `name` empty. They then get a system name in the same
  style as the Multi-room Audio Controller: the card's description plus the pair
  (`fl_fr`, `rl_rr`, `sl_sr`, `fc_lfe`), e.g. `Creative_X_Fi_Analog_Surround_7_1_rl_rr`.
- `remap_sinks` now fully applies at every start: a sink whose entry changed is unloaded
  and reloaded with the new settings (the previous one is restored if the new one fails
  to load), and a sink this add-on loaded whose entry was removed is unloaded. The add-on
  records the sinks it manages in `remap_sinks_state.json` next to `custom.pa`, and never
  unloads or changes a sink it didn't create. This happens before zones start, so each
  zone keeps its port and Music Assistant player.
- `remap_sinks` entries that are not loaded yet are now loaded at add-on start, before
  zones are created, so each gets its AirPlay 2 zone straight away. An entry whose master
  channels are already used by another sink on the same card (configured or not) is not
  loaded, is commented out in `custom.pa` with the reason, and is logged. Sinks that are
  already loaded are never unloaded or changed.
- `remap_sinks` option. On every start the add-on writes these as `load-module
  module-remap-sink` lines to `custom.pa` in its config folder
  (`/addon_configs/<slug>/custom.pa`), inside a marked block; text outside the block is
  kept. Remap sinks that are loaded but not in the options are listed there as comments.
  Nothing is loaded into PulseAudio yet; copy the block into the audio plugin's
  `/data/custom.pa` to make the sinks permanent.
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
