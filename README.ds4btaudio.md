# ds4btaudio

Use a Bluetooth-connected DualShock 4 as an audio sink on Linux: plug earbuds
into the controller's 3.5 mm jack and route any audio to them, PS4-style.

Unlike the historical ds4drv approach, this daemon **coexists with BlueZ and
the kernel `hid-playstation` driver**: pairing, input (gamepad/touchpad/motion),
LED and rumble keep working untouched. The daemon only *writes* audio/volume
HID output reports to the controller's `/dev/hidrawX` node.

## How it works

1. Creates a PulseAudio/PipeWire null sink `ds4_headphones`
   ("DualShock 4 Headphones") at 32 kHz stereo.
2. Captures the sink monitor with `parec`.
3. Encodes PCM to SBC with libsbc (16 blocks, 8 subbands, stereo,
   bitpool 50 → 112-byte frames) — the exact format the PS4 uses.
4. Packs 4 SBC frames (448 B) into HID output report `0x17` with a CRC32
   trailer and writes it to the hidraw node every 16 ms (62.5 reports/s),
   paced by the audio capture clock.

Headphone volume is set once at startup via output report `0x11`, using only
the volume-enable flag bits so the kernel driver's LED/rumble state is never
clobbered.

## Requirements

* Bluetooth-paired DualShock 4 (v1 `054C:05C4` or v2 `054C:09CC`)
* PipeWire (with pipewire-pulse) or PulseAudio
* `libsbc1`, `pulseaudio-utils` (parec/pactl) — both are already present on
  any system with Bluetooth audio support
* Python 3 (stdlib only)
* Write access to the controller's hidraw node (granted to the seated user
  by the standard `uaccess` udev ACL on desktop distros)

No compilation, no extra Python packages, no root.

## Usage

    ./ds4btaudio.py                 # stream until the controller disconnects
    ./ds4btaudio.py --wait          # keep waiting/reconnecting (daemon mode)
    ./ds4btaudio.py --volume 85     # louder headphone amp (0-100, default 70)
    ./ds4btaudio.py --stats 5       # log report throughput every 5 s

Then select **DualShock 4 Headphones** as output in your mixer, or move a
single app's stream to it.

### As a user service

    mkdir -p ~/.config/systemd/user
    cp systemd/ds4btaudio.service ~/.config/systemd/user/
    systemctl --user daemon-reload
    systemctl --user enable --now ds4btaudio

The sink appears whenever a DS4 is connected; it goes away (and the service
keeps waiting) when the controller sleeps.

## Notes & limitations

* Streaming keeps the controller radio busy: expect faster battery drain.
  Stop the service (or remove the sink as default) when not in use.
* Microphone capture (headset TRRS mic) is not implemented.
* The controller's built-in speaker is addressable via the same protocol
  (volume byte 24 / audio header routing) but is not exposed yet.
* Latency is roughly the Pulse capture latency (~20 ms) + one report (16 ms)
  + controller buffering — fine for music/video, noticeable for rhythm games.

## Credits

* Protocol reverse engineering: https://www.psdevwiki.com/ps4/DS4-BT and
  https://eleccelerator.com/wiki/index.php?title=DualShock_4
* Prior art: poconbhui's `add-audio` branch of chrippa/ds4drv (2016), which
  proved the report-0x17 hidraw approach and whose SBC parameters this
  rewrite inherits.
