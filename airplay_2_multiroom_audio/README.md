# AirPlay 2 Multiroom Audio

Turn the audio outputs on your Home Assistant host (sound cards, USB DACs, HDMI, Bluetooth speakers) **and** your Google Cast speakers into AirPlay 2 receivers. Play to them one at a time or grouped from an iPhone, iPad or Mac, or use them as players in [Music Assistant](https://music-assistant.io), including synchronized multi-room playback with PTP timing.

## What it does

- **One AirPlay 2 zone per PulseAudio remap sink.** Each zone is a [shairport-sync](https://github.com/mikebrady/shairport-sync) receiver named after its remap sink, so each room or channel pair appears as its own AirPlay 2 speaker.
- **Bluetooth speakers become zones automatically.** A connected Bluetooth (A2DP) speaker gets its own zone, named after the speaker, with no remap sink needed.
- **Zones follow your hardware while running.** When a sink appears or disappears (a Bluetooth speaker connects or drops, a USB DAC is plugged in, a remap sink is recreated), its zone starts or stops within a few seconds. A returning zone keeps its port and AirPlay 2 device ID, so it comes back as the same player.
- **Google Cast speakers get AirPlay 2 zones too.** Every Cast speaker, group or stereo pair found on the network gets a zone that relays the audio to it. Cast devices that already have a real AirPlay 2 receiver of their own are skipped automatically.
- **Optional own LAN IP.** The zones can run on their own IP address, so Music Assistant on the same host can use AirPlay 2 with PTP timing. Without it, the two compete for the same PTP ports. See [Running on the same host as Music Assistant](#running-on-the-same-host-as-music-assistant).
- **Standard AirPlay 2 grouping.** Any other AirPlay 2 receiver on your network (HomePods, TVs, other add-ons) can be grouped with these zones from your source device.

## Requirements

- Home Assistant OS or Supervised, with PulseAudio sinks for your sound hardware (see [Setting up zones](#setting-up-zones)).
- A wired network connection for the host if you use the own-IP option (macvlan doesn't work over Wi-Fi).
- Cast devices on the same network/VLAN as the host. Discovery uses mDNS, which doesn't cross VLANs without a reflector.

**Permissions:** the add-on uses `host_network`, the `NET_ADMIN` and `SYS_ADMIN` privileges, and runs without AppArmor. The privileges are only used by the own-IP option, which creates a network namespace and macvlan interface; Home Assistant shows a lower security rating because of them.

## Configuration

| Option | Default | Purpose |
|---|---|---|
| `log_level` | `vv` | shairport-sync log detail per zone: `v`, `vv` or `vvv`. `vvv` is for debugging and grows logs quickly. |
| `ap2_ip` | empty | Own LAN address for the zones, with prefix, e.g. `192.168.1.60/24`. Empty = use the host's address. |
| `ap2_gateway` | empty | The gateway your host uses, e.g. `192.168.1.1`. |
| `ap2_mac` | empty | Fixed MAC for the own address, e.g. `02:aa:bb:cc:dd:01` (first byte `02` = locally administered). |
| `ap2_shim_ip` | empty | A second free LAN address, e.g. `192.168.1.61`, used so apps on the host can reach the zones. |

Restart the add-on after changing any option.

## Running on the same host as Music Assistant

AirPlay 2 timing (PTP) uses UDP ports 319 and 320. Music Assistant's AirPlay provider runs its own PTP clock on those ports, and so does this add-on's nqptp. With both on the host's address, whichever starts second can't bind them: Music Assistant then falls back to NTP timing, which these receivers reject for AirPlay 2, so you're limited to compatibility mode.

Setting `ap2_ip` solves this. At startup the add-on creates a private network namespace with a macvlan interface on your LAN, gives it that address, and runs avahi, nqptp and all the zones inside it. Each side then has its own ports 319/320.

1. Pick two free addresses on your LAN, outside your DHCP pool (or reserve them):
   - one for the zones (`ap2_ip`, with `/24` or whatever prefix your network uses),
   - one for the host-side shim (`ap2_shim_ip`).
2. Set `ap2_gateway` to the gateway your host uses (`ip route show default` on the host).
3. Optionally set `ap2_mac`.
4. Restart the add-on.

**Why the shim is needed:** a host cannot talk to its own macvlan interface directly. The add-on adds a second small macvlan on the host side with the shim address and routes the zones' address through it, so Music Assistant (and anything else on the host) can reach the zones and see their mDNS announcements. Traffic to the zones still leaves from the host's real address.

The zones are IPv4-only in this mode, so every sender (Music Assistant, iPhones) connects to them over IPv4.

**Checking it worked.** `generate_airplay2_services.log` should contain a line like:

```
ap2: netns ap2 up -- 192.168.1.60/24 on ap2mv (parent enp5s0), shim 192.168.1.61, host 192.168.1.117
```

If setup fails at any step (wrong gateway, an address already in use, missing privileges), the log shows `ap2: setup FAILED` with the reason and the add-on starts normally on the host's address instead.

## Setting up zones

**Sound cards, USB DACs and HDMI outputs.** The add-on creates a zone for every PulseAudio `module-remap-sink`, named after the remap sink. A raw hardware sink isn't picked up on its own; give it a remap sink, even a simple 1:1 passthrough. The easiest way to create and name remap sinks is the **Multi-room Audio Controller** add-on.

**Bluetooth speakers.** Pair and connect the speaker on the host (see [Pairing a Bluetooth speaker](#pairing-a-bluetooth-speaker)). As soon as PulseAudio shows its A2DP sink, a zone appears, named after the speaker. If you'd rather control the name or channel layout, create a remap sink on top of the Bluetooth sink; the remap sink then becomes the zone instead.

**Google Cast speakers.** Nothing to configure. Each Cast device found at startup gets a zone.

### When to restart

Sink-backed and Bluetooth zones are picked up while the add-on runs (it watches PulseAudio events and also re-checks every 30 seconds). Restart the add-on when you:

- add, rename or remove a **Cast** device (Cast discovery only runs at startup),
- change an add-on option or a zone's `.custom` file,
- want to recover a sound card that stays silent after a receiver or TV was power-cycled (startup suspends and resumes each card so PulseAudio reopens it).

Zone names and ports are stored in `port_map.txt` and survive restarts and updates, so each zone keeps the same AirPlay 2 identity.

## Using the zones in Music Assistant

Every zone appears in Music Assistant as an AirPlay player.

- **With `ap2_ip` set:** leave the streaming mode on **Automatic**, or choose **AirPlay 2 - PTP timing**. Zones play natively over AirPlay 2 with PTP, including synchronized groups, track changes within a session and 24-bit sources.
- **Without `ap2_ip` (MA on the same host):** set each player to **AirPlay 2 - compatibility mode** or **AirPlay 1 (RAOP)**.

To change it, open the player in Music Assistant, turn on **Show advanced settings**, open the **AirPlay** protocol and pick a **Streaming mode**.

Music Assistant may log `<zone> has not answered the server's PTP clock ... so it will not play any audio` for these zones. You can ignore it: nqptp follows Music Assistant's clock without sending replies, which that check doesn't expect, and playback works normally.

**About the bundled shairport-sync.** The add-on builds shairport-sync 5.6.2 with a small compatibility patch (`sps-ssrc0-fallback.patch`) for how Music Assistant's AirPlay 2 sender currently streams buffered audio. Without it, zones would stay silent, go silent after a track change, or play static on 24-bit sources. Apple senders are not affected by the patch. Details are in the patch file and the [CHANGELOG](CHANGELOG.md).

## Per-zone settings

Each sink-backed or Bluetooth zone has an override file:

```
/addon_configs/<add-on slug>/shairport-sync/config/<zone>-ap2.custom
```

- It's created on the first start after the zone appears, containing only comments, and is never overwritten.
- Its contents are included inside the zone's `general` block, so add plain `general` settings, each ending with a semicolon. Don't add a `general = { ... }` wrapper or other groups.
- `name = "...";` renames the zone. A renamed zone gets a new AirPlay 2 device ID, so Music Assistant sees it as a new player.
- Don't set `port`, `interface`, `output_backend`, `udp_port_base` or `mdns_backend`: the add-on writes those, and a duplicate setting stops the zone from starting.
- A syntax error stops only that zone; the error is in its `-ap2.log`.
- Restart the add-on after editing.

**Example: latency offset for a Bluetooth zone.** Bluetooth adds roughly 100–300 ms of delay that AirPlay doesn't account for, so a Bluetooth zone plays late in a group. To make a zone play 200 ms earlier:

```
audio_backend_latency_offset_in_seconds = -0.2;
```

To measure the delay, play a click track to the Bluetooth zone and a wired zone together, record both with your phone from an equal distance, and measure the gap between the clicks in an audio editor such as Audacity. Use that gap as a negative number of seconds and adjust until the clicks line up. With PTP timing, wired zones stay tightly in sync, so a Bluetooth offset is easier to hear and worth tuning.

## Pairing a Bluetooth speaker

Pairing is done in a shell on the Home Assistant host, not inside the add-on. The host needs a working Bluetooth adapter with BlueZ, and PulseAudio's Bluetooth modules (`pactl list modules short | grep blue`).

```bash
bluetoothctl
```

```
power on
agent on
default-agent
scan on
```

Put the speaker in pairing mode and make sure it isn't connected to a phone. When it appears as `[NEW] Device AA:BB:CC:DD:EE:FF Speaker Name`:

```
pair AA:BB:CC:DD:EE:FF
trust AA:BB:CC:DD:EE:FF
connect AA:BB:CC:DD:EE:FF
scan off
exit
```

`trust` lets the speaker reconnect by itself after it powers off or the host reboots, and the zone comes back automatically when it does. Check the result with:

```bash
bluetoothctl info AA:BB:CC:DD:EE:FF | grep -E "Paired|Trusted|Connected"
pactl list sinks short | grep bluez
```

**If it doesn't work:**

- **Not found in the scan:** put the speaker back in pairing mode and disconnect it from other devices.
- **`AuthenticationFailed` or a timeout:** `remove AA:BB:CC:DD:EE:FF`, then start again from `scan on`.
- **Pairs but won't connect:** PulseAudio's Bluetooth modules aren't loaded or can't reach BlueZ; check `journalctl -u bluetooth -b`.
- **Connected but no sink:** set the card to the A2DP profile: `pactl set-card-profile <card> a2dp-sink`.

## Logs and troubleshooting

Logs are in `/addon_configs/<add-on slug>/shairport-sync/logs/` and `.../cast-bridge/logs/`. They are cleared each time the add-on starts.

| Log | Contents |
|---|---|
| `generate_airplay2_services.log` | Startup: own-IP setup (`ap2:` lines), sink cleanup and recreation |
| `sink_zone_manager.log` | Zones starting, stopping and restarting as sinks come and go |
| `<zone>-ap2.log` | One zone's shairport-sync |
| `cast_bridge_manager.log` | Cast discovery and Cast zone status |
| `<zone>-bridge.log` | One Cast zone's relay |

**A zone doesn't appear in the AirPlay picker.** Check `pactl list sinks short` for its remap or Bluetooth sink, then `sink_zone_manager.log`. For Cast zones, check `cast_bridge_manager.log` and restart the add-on.

**Music Assistant can't reach the zones with `ap2_ip` set.** Confirm the `ap2: netns ap2 up` line. Then, on the host, `ip route get <ap2_ip address>` should show `dev ap2shim src <host IP>`.

**Zones keep renaming themselves, or registration collides.** Another add-on on the host is running its own avahi daemon with the same host name. This add-on should be the only avahi on the host; for example, set Music Assistant Local Audio's `server` option to a host or URL instead of `mdns:…`.

**Silent after a receiver or TV was power-cycled.** Restart the add-on.

When reporting a problem, include the zone's `-ap2.log` and the output of `pactl list sinks short`.

## Known limitations

- **Cast discovery runs only at startup.** New, renamed or removed Cast devices need a restart.
- **Cast zones don't sync tightly with sink zones.** Cast audio is relayed with its own buffering, so mixed groups can drift. Cast zones stay reasonably in sync with each other.
- **The Spotify app's own device picker** shows these zones as AirPlay 1. Use the iOS system AirPlay picker (Control Center, or tap the Dynamic Island while audio is playing) for AirPlay 2.
- **Spotify from an iPhone may stop after about 30 seconds** on some setups. This is under investigation; the log shows the iPhone ending the stream, not a receiver error. Music Assistant's own Spotify playback is not affected.
- **24-bit from Music Assistant** is only used on the first session after its AirPlay provider starts. Later sessions use 16-bit, because that's what the zones advertise.
- **Metadata and MQTT** are compiled into shairport-sync, but the add-on doesn't configure them yet.
