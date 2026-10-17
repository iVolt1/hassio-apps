#!/bin/sh
# export_remap_sinks.sh
#
# Prints a PulseAudio startup-script snippet (for the audio plugin's
# custom.pa) that recreates every module-remap-sink currently loaded, with
# exactly the arguments it was created with, e.g. by the Multi-room Audio
# Controller.
#
# Usage (wherever `pactl` talks to the audio plugin's PulseAudio):
#     ./export_remap_sinks.sh > remap_sinks.pa
#     # review it, then append it to the audio plugin's custom.pa
#
# Notes on the output:
#   * .pa files take PulseAudio CLI commands (load-module ...), not
#     `pactl load-module ...`.
#   * The whole block sits between `.nofail` and `.fail`. Without that, a
#     single failing load-module (for example a master sink that is not
#     present yet) makes PulseAudio refuse to start at all.
#   * Remap sinks whose master is a Bluetooth sink are written as comments:
#     that master only exists while the speaker is connected, so the line
#     cannot succeed at PulseAudio startup.
#   * Duplicate modules (identical arguments) are written once.

pactl list modules | awk '
    /^Module #/                 { in_remap = 0 }
    /^[ \t]*Name: module-remap-sink[ \t]*$/ { in_remap = 1 }
    in_remap && /^[ \t]*Argument:/ {
        sub(/^[ \t]*Argument:[ \t]*/, "")
        in_remap = 0
        if (seen[$0]++) next
        master = ""
        if (match($0, /master=[^ ]+/)) master = substr($0, RSTART + 7, RLENGTH - 7)
        if (master ~ /^bluez_/)
            bt[++nbt] = "# Bluetooth master, not available at PulseAudio startup:\n# load-module module-remap-sink " $0
        else
            line[++n] = "load-module module-remap-sink " $0
    }
    END {
        print "# Remap sinks exported by export_remap_sinks.sh"
        print ".nofail"
        for (i = 1; i <= n; i++) print line[i]
        for (i = 1; i <= nbt; i++) print bt[i]
        print ".fail"
    }
'
