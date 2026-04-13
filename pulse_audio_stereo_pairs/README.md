# Pulse Audio Stereo Pairs

A Home Assistant app that automatically creates PulseAudio stereo pair remap sinks for multi-channel audio devices. Each stereo pair appears as an independent output in Music Assistant and other audio applications.

## What It Does

Multi-channel sound cards (5.1, 7.1 surround) expose a single multi-channel PulseAudio sink. This app splits each card into individual stereo sinks — one per channel pair — so you can route audio independently to front, rear, side, and center/LFE speaker pairs.

For example, a 7.1 card becomes:

| Remap Sink | Channels |
|---|---|
| `Card_front_stereo` | Front left + right |
| `Card_rear_stereo` | Rear left + right |
| `Card_side_stereo` | Side left + right |
| `Card_center_sub` | Center + LFE |

S/PDIF and HDMI outputs get a single stereo remap sink each.

Native stereo devices (USB DACs etc.) are also exposed as remap sinks for consistency.

Example PulseAudio sinks and their stereo pair remap sinks shown using pactl.
```
➜  pactl list sinks short
0       alsa_output.usb-AudioQuest_AudioQuest_DragonFly_Black_v1.5_AQDFBL0120005724-00.analog-stereo    module-alsa-card.c      s24le 2ch 96000Hz       RUNNING
1       alsa_output.pci-0000_03_00.0.analog-surround-71 module-alsa-card.c      s32le 8ch 96000Hz       RUNNING
2       alsa_output.usb-C-Media_Electronics_Inc._USB2.0_High-Speed_True_HD_Audio-00.iec958-stereo       module-alsa-card.c      s32le 2ch 96000Hz       RUNNING
3       alsa_output.usb-0d8c_USB_Sound_Device-00.analog-surround-71     module-alsa-card.c      s16le 8ch 48000Hz       RUNNING
4       alsa_output.pci-0000_07_00.6.analog-surround-51 module-alsa-card.c      s32le 6ch 96000Hz       RUNNING
10    AudioQuest_DragonFly_Black_v15_front_stereo     module-remap-sink.c     s24le 2ch 96000Hz       IDLE
11    Creative_X_Fi_rear_stereo       module-remap-sink.c     s32le 2ch 96000Hz       IDLE
12    Creative_X_Fi_side_stereo       module-remap-sink.c     s32le 2ch 96000Hz       IDLE
13    Creative_X_Fi_front_stereo      module-remap-sink.c     s32le 2ch 96000Hz       IDLE
14    Creative_X_Fi_center_sub        module-remap-sink.c     s32le 2ch 96000Hz       IDLE
15    USB20_High_Speed_True_HD_Audio_iec958_stereo    module-remap-sink.c     s32le 2ch 96000Hz       IDLE
16    ICUSBAUDIO7D_rear_stereo        module-remap-sink.c     s16le 2ch 48000Hz       IDLE
17    ICUSBAUDIO7D_side_stereo        module-remap-sink.c     s16le 2ch 48000Hz       IDLE
18    ICUSBAUDIO7D_front_stereo       module-remap-sink.c     s16le 2ch 48000Hz       IDLE
19    ICUSBAUDIO7D_center_sub module-remap-sink.c     s16le 2ch 48000Hz       IDLE
20    HD_Audio_Generic_rear_stereo    module-remap-sink.c     s32le 2ch 96000Hz       IDLE
21    HD_Audio_Generic_front_stereo   module-remap-sink.c     s32le 2ch 96000Hz       IDLE
22    HD_Audio_Generic_center_sub     module-remap-sink.c     s32le 2ch 96000Hz       IDLE
➜
```

The higher than 44100 sample rates and higher than 16 bit depths above are achieved by modifying these settings in /etc/pulse/daemon.conf in the hassio_audio container and loading the new settings with `ha audio restart`. The settings changes are not persistent across reboots or container restarts so have to be reapplied if needed. These values have worked for a wide variety of DACs and may not work for all DACs:
```
 default-sample-format = s32le
 default-sample-rate = 96000
 alternate-sample-rate = 48000
``` 
## Features

- **Automatic discovery** — detects all PulseAudio output sinks at startup
- **Hot-plug support** — reacts to audio devices being connected or disconnected
- **Clean shutdown** — removes all remap sinks when the app stops
- **Persistent log** — records all module load/unload activity to `/app_configs/pulse_audio_stereo_pairs/remap_modules.log`

## Requirements

- PulseAudio must be running on the host before this app starts
- The app is `amd64` and `aarch64` only

## Installation

1. Add this repository to your Home Assistant app store
2. Install the app **Pulse Audio Stereo Pairs**
3. Start the app — stereo pairs will be created automatically

## Usage with Music Assistant

Once running, the stereo pair sinks appear automatically in the Music Assistant **Pulse Audio Out** provider as individual players.  Each player shows its native format (e.g. 96kHz/32-bit for the Creative X-Fi front stereo pair).

## Log

The app logs all activity to:
```
/app_configs/pulse_audio_stereo_pairs/remap_modules.log
```

This is useful for confirming which sinks were created and diagnosing any failures.

## Notes

- The app removes and recreates all remap sinks on each startup to ensure a clean state
- `module-suspend-on-idle` is disabled to keep sinks active at all times
- If a physical audio device is unplugged, its associated remap sinks are automatically removed
- Restartng the **Pulse Audio Out** provider in MA or restarting MA is necessary to reflect the changes when DACs are added or removed.
