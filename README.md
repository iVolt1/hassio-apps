# iVolt1 Home Assistant Add-ons

Home Assistant add-ons for multi-room audio.

[![Add this repository to your Home Assistant instance.](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2FiVolt1%2Fhassio-apps)

## Installation

The quickest way is the button above. To add the repository by hand:

1. Go to **Settings → Add-ons → Add-on Store**.
2. Open the menu (⋮) in the top right and select **Repositories**.
3. Add this URL:
   ```
   https://github.com/iVolt1/hassio-apps
   ```
4. The add-ons appear in the store under **iVolt1 Home Assistant Apps**.

## Add-ons

### [AirPlay 2 Multiroom Audio](airplay_2_multiroom_audio)

Turns the audio outputs on your Home Assistant host (sound cards, USB DACs, HDMI, Bluetooth speakers) **and** your Google Cast speakers into AirPlay 2 receivers. Play to them one at a time or grouped from an iPhone, iPad or Mac, or use them as players in [Music Assistant](https://music-assistant.io), including synchronized multi-room playback with PTP timing.

- One AirPlay 2 zone per PulseAudio remap sink, built on [shairport-sync](https://github.com/mikebrady/shairport-sync) and nqptp. Zones start and stop as sinks come and go.
- Connected Bluetooth speakers get a zone automatically.
- Google Cast devices on your network appear as AirPlay 2 zones.
- Optional own LAN IP, so Music Assistant on the same host can use AirPlay 2 with PTP timing.
- Per-zone settings, such as a latency offset to keep a Bluetooth speaker in sync with your other rooms.

See the add-on's [README](airplay_2_multiroom_audio/README.md) for setup and troubleshooting, and its [CHANGELOG](airplay_2_multiroom_audio/CHANGELOG.md) for what's new.

#### Stable and beta

| Add-on in the store | Use it for |
|---|---|
| **AirPlay 2 Multiroom Audio** | Stable releases. Recommended. |
| **AirPlay 2 Multiroom Audio Beta** | New changes before they reach stable. May be less reliable. |

Install one or the other, not both: they would advertise the same zones on your network. To switch, copy `port_map.txt`, `http_port_map.txt` and your `*-ap2.custom` files from one add-on's `addon_configs/<slug>/shairport-sync/config/` folder to the other's before starting it, so your zones keep their identity in Music Assistant.

## Support

Found a bug or have a question? [Open an issue](https://github.com/iVolt1/hassio-apps/issues) and include:

- the add-on version,
- the zone's log from `addon_configs/<slug>/shairport-sync/logs/<zone>-ap2.log`,
- the output of `pactl list sinks short`.
