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

## Features

- **Automatic discovery** — detects all PulseAudio output sinks at startup
- **Hot-plug support** — reacts to audio devices being connected or disconnected
- **Clean shutdown** — removes all remap sinks when the app stops
- **Persistent log** — records all module load/unload activity to `/app_configs/pulse_audio_stereo_pairs/remap_modules.log`

## Requirements

- PulseAudio must be running on the host before this app starts
- The app is `amd64` only

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