#!/bin/bash
# Audio notes for the Yandex Station 1 Debian (verified 2026-11-03):
#   Mics:     arecord -D hw:snddaudio0,0 -c 5 -f S32_LE -r 16000   (5 ch: mic array)
#   Speakers: aplay -D station_speakers <file>                     (48 kHz S32_LE stereo, dmix)
#   Volume:   amixer -c snddaudio1 sset 'Tweeters Master' <0-100%> (init boots quiet;
#             vendor max ≈ -20 dB)
# Verified: mic array records on all 5 channels; 440 Hz stereo tone played OK.
echo see comments in this file
