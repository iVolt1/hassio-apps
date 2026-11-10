# Changelog

All notable changes to the AirPlay 2 Multiroom Audio add-on are listed here.
Newest first. Format follows [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

### Changed
- `ap1_bridge.py` and `ap1_test.py` take the device's AirPlay name instead of an IP
  (`ap1_bridge.py "BA 790 TA"`); it is looked up over mDNS (any case, part of the name
  is enough) and gives the address, port and settings.

### Fixed
- Version 0.151 (0.150 would read as 0.15).
- AirPlay 1 bridge (`ap1_bridge.py`): the network namespace check looked for a file
  `docker exec` doesn't see, so the bridge's AirPlay 2 receiver ran on the host side
  instead of with the other zones on the add-on's own IP, next to nqptp. It now asks
  `ip netns` and logs `network: netns ap2 …`.
- AirPlay 1 bridge: the device's clock (NTP over AirPlay 1) takes a while to settle
  after each new connection, so sync started a little off and improved. The connection
  is now kept open (idle) for `--linger` seconds (default 300) after a session, so the
  next session starts warm, and on a new connection the device joins after a
  `--warmup` (default 3000 ms; it skips that much audio). `--verbose` also shows
  cliairplay's resend / missing-packet lines.

### Fixed
- AirPlay 1 bridge: when the AirPlay 2 receiver drops audio that arrived too late, its
  play times jump ahead. The bridge used to catch up a frame per block and then restart
  the device's stream; it now fills the gap with the same amount of silence (or skips
  audio for a jump back) at once, as the AirPlay 2 rooms do, and stays in step. The
  10 s log line counts the gaps. Restarts only above a 2 s error.

### Added
- AirPlay 2 -> AirPlay 1 bridge, step 2a (run by hand, one device):
  `docker exec -it app_2c14ac29_airplay_2_multiroom_audio_beta python3 /usr/sbin/ap1_bridge.py <device ip> [--name "..."]`
  starts an AirPlay 2 receiver whose audio is passed on to the AirPlay 1 device with
  cliairplay, started and kept at the play times the receiver gets from the AirPlay 2
  group, so the device plays in step with your other rooms. It uses a second
  shairport-sync build, `shairport-sync-ap1bridge`, from the unmerged
  mikebrady/shairport-sync#2334 (timing headers on the pipe output), pinned to a
  commit; every other zone keeps the 5.6.2 build. Drift is corrected a frame at a
  time; a flush from the sender, or a large error, restarts the stream at the next
  block. Logs the sync error every 10 s.
- `ap1_test.py` exits quietly on Ctrl-C.

### Added
- Groundwork for an AirPlay 2 -> AirPlay 1 bridge: the image now includes **cliairplay**
  (Music Assistant's AirPlay 1/2 sender, v0.5.9, built fully static) and a test tool,
  `ap1_test.py`, that streams a wall-clock click track to an AirPlay 1 device and
  exercises start-at-an-instant, flush/re-anchor, volume and stop. Run by hand:
  `docker exec -it app_2c14ac29_airplay_2_multiroom_audio_beta python3 /usr/sbin/ap1_test.py <device ip>`.
  Nothing runs it automatically.

### Changed
- Web panel: in a row's **More**, Delete sink / Remove combine sink / Adopt now come first
  (left), with Max volume (dB) as a fixed-width box after them, so the button is visible
  without scrolling right.

### Fixed
- Switching a card profile in the web panel could fail with `Request failed:
  SyntaxError: JSON.parse…` (a 502 from Home Assistant) and switch the card back: the
  new card output appearing made the zone manager's hotplug check (0.144) run the sink
  setup from the options before the panel had saved the new profile. The check now
  waits while the panel is changing cards and runs once it's done.

### Added
- **Card volume at start:** every sound card output a zone plays through (the master of
  a remap sink, or a card output in a combine sink) is set to its 0 dB point and
  unmuted when the add-on starts, after a PulseAudio restart, and when the card
  (re)appears, e.g. a USB adapter plugged in. The 0 dB point is PulseAudio's Base
  Volume (100% on most cards; lower on cards whose hardware mixer would otherwise be
  boosted). Zones keep their own volume control (AirPlay volume, Max volume). Each card
  is set once per appearance, so a volume you change by hand stays until the card or
  PulseAudio comes back. Bluetooth speakers are not touched. Logged as `card volume: …`.

### Changed
- Cast Bridge: discovery now **listens continuously** (one zeroconf instance for Cast
  devices and AirPlay receivers) instead of re-running a discovery pass on a timer
  (`CAST_REDISCOVER_SECONDS` is gone). A new Cast device is bridged about 5 seconds
  after it announces itself (the wait lets a device with native AirPlay 2 be recognised
  and left alone); a device that changes address gets its bridge restarted right away.
  No repeated mDNS query bursts. The bridges' own AirPlay names no longer count as
  "native AirPlay 2" for that check.

### Fixed
- Cast Bridge: Cast devices are now looked for again every 2 minutes in the background,
  not only when the add-on starts. A new device shows up in the web panel (and starts if
  its AirPlay box is ticked, or always on installs from before opt-in zones); a device
  that moved to a new IP address gets its bridge restarted with the new address. A
  device that drops off the network keeps its zone.

### Changed
- Version 0.141 (0.140 would read as 0.14, older than 0.139).
- Web panel: the first table is titled **AirPlay 2 receivers and Sinks**; the Create
  combine sink member group "Zones (remap sinks)" is now "Remap sinks"; the separate
  Create sink section is removed (possible remap sinks are rows in the first table).

- Web panel Zones table: **Player name** now shows the name the player has in AirPlay and
  Music Assistant (e.g. *Dining Room*), and editing it sets that name. For a sink this
  add-on owns, the sink is renamed to match with spaces and other characters as `_`
  (*Dining Room* -> `Dining_Room`); the exact spelling is kept as the zone's AirPlay name
  only when it isn't just the sink name with spaces. Bluetooth and unmanaged rows set the
  AirPlay name only. The separate AirPlay name field in **More** and the "AirPlay name:"
  note under the name are gone. A renamed zone keeps its AirPlay on/off.

- **Zones are opt-in.** A zone's AirPlay receiver runs only if it is in the new
  `enabled_zones` option (the web panel's AirPlay checkbox). On a fresh install
  nothing runs, Bluetooth speakers and Cast devices included, until you tick it. Cast
  devices found at start are listed in the Zones table, and their AirPlay checkbox starts
  or stops the bridge without restarting the add-on. Installs from before
  this version are migrated on their first start: every zone that ran is put in
  `enabled_zones` and `disabled_zones` is emptied (it's no longer used).
- Web panel Zones table: every **possible** remap sink is listed too, one row per free
  channel pair of each sound card (status *Not created*), so a fresh install shows all
  of them. Ticking **Sink** creates the sink (under the player name in the row, the
  system name unless you change it); ticking **AirPlay** creates it and starts its zone.
  Rows are grouped by card. Columns are now Player name, Channels, AirPlay, Sink,
  Offset, Status, System name, Card, Port.
- Sinks this add-on loads carry the PulseAudio property `airplay2_multiroom.managed=1`
  (this reloads every managed sink once, on the first start of this version). A fresh
  install unloads sinks with that property that a previous install left in
  PulseAudio, so uninstalling with *Delete data* and reinstalling starts clean.
- Web panel: the Zones and Remap sinks tables are merged into one **Zones** table with a
  row per remap sink, combine sink, Bluetooth zone and Cast zone. Columns: Player name
  (editable for managed sinks; renames the sink), Channels, AirPlay and Sink checkboxes,
  Offset (ms), Card, Port, Status, and the system name (with copy) on the far right.
  Ticking AirPlay ticks Sink; unticking Sink unticks AirPlay. Edit any number of rows and
  commit them with one **Apply** (or Discard). A row's **More** opens the AirPlay name
  override and Max volume (also saved with Apply) plus Delete / Remove / Adopt.
- Renaming a sink now keeps its zone's settings: its `-ap2.custom` file is copied to the
  new name, and `disabled_zones` and combine sink members follow the rename.

### Added
- Combine sink switch: a `combine_sinks` entry with `enabled: false` stays in the options
  but isn't loaded (if this add-on loaded it, it's unloaded, and its zone stops); it's
  marked `# SWITCHED OFF` in `custom.pa`. Web panel: Turn off / Turn on per managed
  combine sink in the Combine sinks table.
- Web panel: **Delete** button for managed remap sinks (click twice to confirm). The entry
  is removed from `remap_sinks`, its sink is unloaded and its zone stops, and its channel
  pair is free again; warns when combine sinks use it.
- Remap sink switch: a `remap_sinks` entry with `enabled: false` stays in the options
  but isn't loaded (if this add-on loaded it, it's unloaded, and its zone stops); it's
  marked `# SWITCHED OFF` in `custom.pa`, and its channel pair stays reserved. Web panel:
  Turn off / Turn on per managed sink in the Remap sinks table, warning when combine
  sinks use it.
- Zone switch: sound-card and Bluetooth zones can be switched off and on from the web
  panel (Turn off / Turn on in the Zones section). A switched-off zone's AirPlay receiver
  is stopped, so it disappears from AirPlay pickers and Music Assistant, while its sink,
  port, name and settings stay; turning it on brings the same player back. Stored in the
  new `disabled_zones` option; the zone manager acts within a few seconds of a change,
  and switched-off zones are listed as "Off".
- Web panel: **Bluetooth** section with a **Reset Bluetooth adapter** button that
  power-cycles the host's adapter(s) through BlueZ (`bt_reset.py`, via `dbus-send` on the
  host bus), for adapters that stay connected but stop passing audio after some hours.
  BlueZ clients such as Bluetooth Audio Manager reconnect the sinks afterwards.
- `bt_reset_on_start` option (default off): the same reset once at add-on start, before
  any zone starts.

### Changed
- `host_dbus: true`, so the add-on can reach BlueZ on the host bus (mounted read-only at
  `/run/dbus`). The add-on's own private D-Bus for avahi/shairport-sync moved to
  `/run/ap2-dbus` (`DBUS_SYSTEM_BUS_ADDRESS`) so the two don't collide.

### Fixed
- `card_options` option: extra `module-alsa-card` settings per sound card, e.g.
  `card: alsa_card.pci-0000_04_00.0`, `options: mmap=no tsched=no`. At every start (and
  after `ha audio restart`, via the zone manager) the card is reloaded with them, keeping
  its profile, before card profiles and sinks are applied; a card that already has them
  is left alone. `mmap=no tsched=no` works around the Creative X-Fi `snd_ctxfi` kernel
  regression where the card is RUNNING in PulseAudio but plays silence.

### Changed
- Zone logs are named after the zone's AirPlay name (`Workshop-ap2.log`, spaces as `_`)
  instead of its sink name, so the logs folder matches the Zones list. Renaming a zone
  starts a log under the new name and removes the old one (and the sink-named log from
  earlier versions), never touching a log another zone uses. Two zones with the same
  AirPlay name get `<name>__<sink>-ap2.log`. Config files stay keyed by sink name.

### Added
- Web panel: each sound card output with free channel pairs has a **Create default
  sinks** button that creates one remap sink per free pair (Front, Rear, Center/LFE, Side
  in the card's channel order; one Stereo sink on a 2-channel card), each under its
  system name, ready to be renamed. Pairs already in use are skipped.
- Web panel: **Combine sinks** section — a list of combine sinks with their members and
  whether their zone is running (Remove, or Adopt for one made elsewhere), and a form to
  create one: name, description, and checkboxes for any number of sinks, grouped as zones
  (remap sinks), sound card outputs and Bluetooth. It warns when a card's whole output and
  a zone on that same card are both picked (those speakers would play everything twice).
- Combine sinks come back by themselves when a missing member appears again, e.g. a
  Bluetooth speaker that was off: the zone manager (re)loads the combine sink, since
  PulseAudio drops a member that goes away and doesn't take it back.
  `remap_sinks.py --reload-combine NAME` forces that reload.
- Combine sinks: a `combine_sinks` option (name, comma-separated member `sinks`,
  optional description) loads PulseAudio `module-combine-sink` sinks after the remap
  sinks, and each gets its own AirPlay 2 zone — one AirPlay target that plays in several
  rooms, handy for senders that can't group zones. A combine sink is reloaded when one of
  its members is reloaded (PulseAudio drops a member that goes away), isn't loaded while a
  member is missing, and is unloaded when removed from the options (only ones this add-on
  loaded). After `ha audio restart` the zone manager reloads them too.
- Custom channel pairs: `remap_sinks` entries can use `channels: custom` with `left` and
  `right` set to any two channels of the card (e.g. `front-center` + `side-left`; the same
  channel twice makes a mono zone). System names use the channel abbreviations
  (`..._fc_sl`, `..._lfe_lfe`). The clash check, profile switches and `custom.pa` all
  handle them per channel.
- Web panel: **Create sink** has a **Custom…** choice with Left and Right lists of the
  card's free channels; each card shows its free channels; sinks with any two-channel
  map (such as an `lfe,lfe` sink) can now be adopted.

### Fixed
- Web panel: switching a card's profile no longer renames entries that use their system
  name (which comes from the old sink's description): they keep their name, now stored
  explicitly, so their zone and Music Assistant player stay the same.
- Card profiles: a `card_profiles` option (card + profile) set at every start before the
  remap sinks load; cards not listed keep the profile PulseAudio gives them.
- Web panel: each sound card's **Profile** can be changed. After a confirm step listing the
  card's zones, the profile is switched at once and saved in `card_profiles`, and the card's
  `remap_sinks` entries are moved to its new output sink where their channel pair still
  exists, so those zones come back under the same name and Music Assistant player. Entries
  whose pair the new profile lacks stay in the options and return when you switch back.
- `remap_sinks`: an entry whose master sink doesn't exist right now (e.g. its card is on
  another profile, or unplugged) is not loaded and is marked in `custom.pa`, instead of
  failing in PulseAudio.
- Web panel: the Sound cards section now lists PulseAudio's cards (including ones set to
  Off) with their active profile and the profiles they offer (unplugged ones marked), and
  each card's output sink(s) with used and free channel pairs. Read-only for now.
  `/api/state` includes it as `pa_cards`; `/api/debug/cards` returns the raw
  `pactl list cards` output.
- Web panel: sound-card and Bluetooth zones have an **Edit** form for AirPlay name, latency
  offset (ms) and max volume (dB). Saving writes them to a marked block in the zone's
  `<zone>-ap2.custom` file (an empty field leaves that setting out; your own lines outside
  the block are kept) and restarts only that zone, which the zone manager starts again at
  once with the new settings. The form shows the values in effect, including ones you set
  by hand in that file; on save, such a hand-set line is commented out (marked "moved to
  the web panel block") and its value moves into the block, since libconfig rejects a
  setting set twice. Cast Bridge zones are read-only.
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
