# iVolt1 Home Assistant Addons

A collection of Home Assistant addons for advanced audio and home automation.

[![Add this repository to your Home Assistant instance.](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2FiVolt1%2Fhassio_apps)

## Installation

The quickest way is the button above. To add the repository by hand:

1. Navigate to **Settings → Add-ons → Add-on Store**
2. Click the menu (⋮) in the top right and select **Repositories**
3. Add the following URL:
   ```
   https://github.com/iVolt1/hassio_apps
   ```
4. The addons will appear in the store under **iVolt1 Home Assistant Addons**

## Addons

### AirPlay 2 Multiroom Audio

Turns your PulseAudio-backed audio hardware (sound cards, USB DACs, HDMI outputs, Bluetooth speakers) **and** your Google Cast (Chromecast/Google Home) speakers into AirPlay 2 receivers. Play to them individually or grouped from an iPhone, iPad, or Mac, or use them as players in [Music Assistant](https://music-assistant.io).

- One AirPlay 2 zone per PulseAudio remap sink, built on [shairport-sync](https://github.com/mikebrady/shairport-sync) and nqptp.
- Cast devices on your network appear automatically as AirPlay 2 zones.
- Per-zone custom configuration, for example a latency offset to keep a Bluetooth speaker in sync with your other rooms.
- Bluetooth speaker and Music Assistant setup guides.

Requires `host_network` and PulseAudio-backed sound hardware. See the addon's own README for setup, troubleshooting, and the Advanced section.

## Support

Found a bug or have a question? Please [open an issue](https://github.com/iVolt1/hassio_apps/issues) and include the relevant zone log from `/config/shairport-sync/logs/` and the output of `pactl list sinks short`.
