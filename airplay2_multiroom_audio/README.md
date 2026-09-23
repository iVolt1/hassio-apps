# AirPlay 2 Multiroom Audio

Turn your existing PulseAudio-backed audio hardware **and** your Google Cast (Chromecast/Google Home) speakers into AirPlay 2 receivers, so you can play to all of them — individually or grouped — from any AirPlay 2 source (iPhone, iPad, Mac).

This addon merges what used to be two separate things — AirPlay 2 receivers for local sound cards, and a bridge that relays AirPlay 2 audio to Google Cast devices — into a single Home Assistant addon.

## What it does

- Scans your system's PulseAudio sinks and creates an [AirPlay 2](https://github.com/mikebrady/shairport-sync) receiver (via `shairport-sync`) for each one, so each physical output (a zone, a room, a card's channel pair) shows up as its own AirPlay 2 speaker.
- Scans your network for Google Cast devices (speakers, speaker groups/stereo pairs, Cast-enabled soundbars) and creates a matching AirPlay 2 receiver for each one too, relaying the audio through to Cast — so devices that don't natively speak AirPlay 2 still show up as AirPlay 2 speakers.
- Skips any Cast device that's already advertising its own, real AirPlay 2 receiver (some TVs and soundbars have this built in) — it's detected automatically, not configured by name, so you never end up with two entries for the same physical device.
- All AirPlay 2 zones — sink-backed and Cast-backed alike — support AirPlay 2's native multi-room grouping, so you can select several speakers at once from your source device.
- This addon doesn't limit you to just the zones it creates: any other AirPlay 2 receiver already on your network (a HomePod, a smart TV, a soundbar with AirPlay 2 built in, another addon's receivers) shows up in your source's AirPlay picker right alongside this addon's zones, and can be grouped together with them the same way, since AirPlay 2 multi-room isn't specific to this addon — it's a standard feature of the protocol itself.

## Requirements

- A Home Assistant host with PulseAudio already set up and your sound hardware's sinks configured (see **Setting up a new zone** below).
- `host_network: true` — this addon needs real access to your LAN for AirPlay 2 (mDNS/Bonjour) and Cast device discovery to work.
- Google Cast devices must be on the same network/VLAN as the Home Assistant host; discovery relies on mDNS, which typically doesn't cross VLANs without an mDNS reflector.

## Setting up a new zone

**Sink-backed zones (local sound cards, USB DACs, HDMI, etc.):** this addon only creates an AirPlay 2 zone for a PulseAudio sink that's a `module-remap-sink` — and it names the zone after that remap sink's own name, so the remap sink's name *is* what shows up in your AirPlay picker. A raw hardware sink on its own (even a plain stereo card with nothing multi-channel about it) won't be picked up until it has a remap sink built on top of it — this applies even when the remap sink would just be a 1:1, all-channels passthrough of the card.

The recommended way to create these remap sinks is with the **Multi-room Audio Controller** addon, rather than creating them by hand with `pactl`. Add or rename a zone there, restart this addon afterward, and it'll pick up the new remap sink and create (or rename) the matching AirPlay 2 zone for it automatically.

**Cast-backed zones (Google/Nest speakers, Cast groups, Cast-enabled soundbars):** nothing to configure — every Cast device found on the network gets its own AirPlay 2 zone automatically, unless it's already got a native AirPlay 2 receiver of its own (see above).

## Restarting

Restart this addon whenever you:
- Add, rename, or remove a PulseAudio remap sink.
- Add or remove a Google Cast device on your network.

A restart re-scans both PulseAudio and the network and regenerates the AirPlay 2 zone list to match. Zone names and their AirPlay 2 ports are persisted across restarts, so a zone's identity in your AirPlay picker stays stable — it won't reappear as a "new" device each time.

If your AV receiver or a TV was power-cycled or had its HDMI cable unplugged, restarting this addon (or just waiting for its next scheduled restart) will also recover sinks that PulseAudio otherwise leaves silently stuck.

## Troubleshooting

Logs live under `/config/shairport-sync/logs/`:

- `generate_airplay2_services.log` — what happened on the most recent sink scan/zone generation pass (which sinks were found, which ports were assigned, any sinks that had to be bounced/recreated).
- `<zone name>-ap2.log` — each zone's own `shairport-sync` (AirPlay 2 receiver) log.
- `cast_bridge_manager.log` — Cast device discovery and the overall status of each Cast Bridge zone's shairport-sync/relay process pair.
- `<zone name>-bridge.log` (under `/config/cast-bridge/logs/`) — a Cast-backed zone's own relay log (its connection to the Cast device, playback start/stop).

**A zone doesn't show up in your AirPlay picker:**
- Sink-backed: confirm it's a `module-remap-sink` sink (`pactl list sinks short`) and check `generate_airplay2_services.log` for its name.
- Cast-backed: check `cast_bridge_manager.log` for whether it was found on the last scan, and whether it was excluded as already having its own AirPlay 2 receiver.

**No sound from a specific zone, but it shows up fine:** check that zone's own log (`-ap2.log` for sink zones, `-bridge.log` for Cast zones) for connection errors, then restart the addon.

**A zone plays briefly then goes silent, or never recovers after a receiver/TV is power-cycled:** restart the addon — this forces PulseAudio to reopen the underlying hardware device.

## Known limitations

- The iOS Spotify app's own in-app device picker initially shows these zones as classic AirPlay-1-only, regardless of what they actually support. If you hit this, use iOS's system AirPlay picker instead (tap the AirPlay icon, or the status-bar clock/Dynamic Island while something is playing) to select the zone as full AirPlay 2 at least once — after that first play, Spotify's own picker seems to pick up on its real AirPlay 2 capability and shows it correctly from then on.
- A brand-new Cast device or a renamed/removed one is only picked up on the next addon restart — there's no live rescanning while it's running.
- Grouping a Cast-backed zone together with a sink-backed (true AirPlay 2) zone likely won't play in tight sync. Sink-backed zones stay tightly synced with each other because they share AirPlay 2's own timing protocol (PTP, via nqptp) directly. A Cast-backed zone's audio, by contrast, is relayed through to the Cast device rather than played by an AirPlay 2 receiver itself, so it picks up its own separate buffering/latency along the way — it should stay in sync with other Cast-backed zones reasonably well, but not with the sink-backed ones. If you're grouping across both kinds regularly, placing AP2 (sink-backed) speakers and Cast-backed speakers in different, separated areas of the house helps mask any drift between them.
