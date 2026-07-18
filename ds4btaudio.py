#!/usr/bin/env python3
"""ds4btaudio - DualShock 4 Bluetooth audio sink daemon for Linux.

Streams audio to the headphone jack of a Bluetooth-connected DualShock 4
without touching its BlueZ pairing or kernel (hid-playstation) input handling.

How it works:
  1. Creates a PulseAudio/PipeWire null sink ("DualShock 4 Headphones").
  2. Captures the sink's monitor with `parec` (32 kHz s16le stereo).
  3. Encodes the PCM to SBC (libsbc via ctypes): 16 blocks, 8 subbands,
     stereo, bitpool 50 -> 112-byte frames, the format the PS4 itself uses.
  4. Packs 4 SBC frames per HID output report 0x17 (CRC32-protected) and
     writes it to the controller's hidraw node every 16 ms, paced by the
     audio clock of the capture stream.

Protocol reference: https://www.psdevwiki.com/ps4/DS4-BT
Prior art: poconbhui/ds4drv branch `add-audio` (2016).
"""

import argparse
import ctypes
import ctypes.util
import logging
import os
import signal
import struct
import subprocess
import sys
import time
import zlib

log = logging.getLogger("ds4btaudio")

# ---------------------------------------------------------------------------
# SBC encoder (libsbc via ctypes)
# ---------------------------------------------------------------------------

# Constants from <sbc/sbc.h>
SBC_FREQ_32000 = 0x01
SBC_BLK_16 = 0x03
SBC_SB_8 = 0x01
SBC_MODE_STEREO = 0x02
SBC_AM_LOUDNESS = 0x00
SBC_LE = 0x00


class _SBCStruct(ctypes.Structure):
    """Mirror of sbc_t from <sbc/sbc.h> (stable ABI, libsbc >= 1.x)."""

    _fields_ = [
        ("flags", ctypes.c_ulong),
        ("frequency", ctypes.c_uint8),
        ("blocks", ctypes.c_uint8),
        ("subbands", ctypes.c_uint8),
        ("mode", ctypes.c_uint8),
        ("allocation", ctypes.c_uint8),
        ("bitpool", ctypes.c_uint8),
        ("endian", ctypes.c_uint8),
        ("priv", ctypes.c_void_p),
        ("priv_alloc_base", ctypes.c_void_p),
    ]


class SBCEncoder:
    """PCM s16le -> SBC encoder using libsbc, PS4 headset parameters."""

    def __init__(self, bitpool=50):
        libname = ctypes.util.find_library("sbc") or "libsbc.so.1"
        self._lib = ctypes.CDLL(libname)
        self._lib.sbc_init.argtypes = [ctypes.POINTER(_SBCStruct), ctypes.c_ulong]
        self._lib.sbc_encode.argtypes = [
            ctypes.POINTER(_SBCStruct),
            ctypes.c_void_p, ctypes.c_size_t,
            ctypes.c_void_p, ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_ssize_t),
        ]
        self._lib.sbc_encode.restype = ctypes.c_ssize_t
        self._lib.sbc_get_frame_length.argtypes = [ctypes.POINTER(_SBCStruct)]
        self._lib.sbc_get_frame_length.restype = ctypes.c_size_t
        self._lib.sbc_get_codesize.argtypes = [ctypes.POINTER(_SBCStruct)]
        self._lib.sbc_get_codesize.restype = ctypes.c_size_t
        self._lib.sbc_finish.argtypes = [ctypes.POINTER(_SBCStruct)]

        self._sbc = _SBCStruct()
        if self._lib.sbc_init(ctypes.byref(self._sbc), 0) != 0:
            raise RuntimeError("sbc_init() failed")
        self._sbc.frequency = SBC_FREQ_32000
        self._sbc.blocks = SBC_BLK_16
        self._sbc.subbands = SBC_SB_8
        self._sbc.mode = SBC_MODE_STEREO
        self._sbc.allocation = SBC_AM_LOUDNESS
        self._sbc.bitpool = bitpool
        self._sbc.endian = SBC_LE

        #: PCM bytes consumed per SBC frame (16 blocks * 8 subbands * 2 ch * 2 B)
        self.codesize = self._lib.sbc_get_codesize(ctypes.byref(self._sbc))
        #: SBC bytes produced per frame
        self.frame_length = self._lib.sbc_get_frame_length(ctypes.byref(self._sbc))

    def encode_frame(self, pcm: bytes) -> bytes:
        """Encode exactly one SBC frame from `codesize` bytes of PCM."""
        out = ctypes.create_string_buffer(self.frame_length)
        written = ctypes.c_ssize_t(0)
        consumed = self._lib.sbc_encode(
            ctypes.byref(self._sbc),
            pcm, len(pcm),
            out, self.frame_length,
            ctypes.byref(written),
        )
        if consumed != self.codesize or written.value != self.frame_length:
            raise RuntimeError(
                f"sbc_encode: consumed={consumed} written={written.value} "
                f"(expected {self.codesize}/{self.frame_length})"
            )
        return out.raw

    def close(self):
        self._lib.sbc_finish(ctypes.byref(self._sbc))


# ---------------------------------------------------------------------------
# DualShock 4 hidraw audio device
# ---------------------------------------------------------------------------

DS4_VENDOR = 0x054C
DS4_PRODUCTS = (0x05C4, 0x09CC)  # CUH-ZCT1 (v1), CUH-ZCT2 (v2)
BT_BUS = 0x0005

# Audio report 0x17 carries 4 SBC frames (448 bytes) in a 452-byte payload
AUDIO_REPORT_ID = 0x17
AUDIO_FRAMES_PER_REPORT = 4
AUDIO_PAYLOAD_SIZE = 452
AUDIO_HEADER = 0x24  # 4 SBC frames, headphone routing

# Output report 0x11 flag bits (report byte 3)
FLAG_VOLUME_L = 0x10
FLAG_VOLUME_R = 0x20
FLAG_VOLUME_MIC = 0x40
FLAG_VOLUME_SPEAKER = 0x80


def bt_crc(report: bytes) -> bytes:
    """CRC32 of a DS4 Bluetooth output report (prefixed with HIDP 0xA2)."""
    return struct.pack("<I", zlib.crc32(b"\xa2" + report) & 0xFFFFFFFF)


def find_ds4_hidraw():
    """Find a Bluetooth-connected DS4 hidraw node via sysfs.

    Returns (hidraw_path, uniq_mac) or None.
    """
    base = "/sys/class/hidraw"
    if not os.path.isdir(base):
        return None
    for name in sorted(os.listdir(base)):
        uevent_path = os.path.join(base, name, "device", "uevent")
        try:
            with open(uevent_path) as f:
                uevent = dict(
                    line.strip().split("=", 1) for line in f if "=" in line
                )
        except OSError:
            continue
        hid_id = uevent.get("HID_ID", "")  # e.g. 0005:0000054C:000005C4
        parts = hid_id.split(":")
        if len(parts) != 3:
            continue
        bus, vid, pid = (int(p, 16) for p in parts)
        if bus == BT_BUS and vid == DS4_VENDOR and pid in DS4_PRODUCTS:
            return "/dev/" + name, uevent.get("HID_UNIQ", "")
    return None


class DS4AudioDevice:
    """Writes DS4 Bluetooth audio/volume output reports to a hidraw node."""

    def __init__(self, hidraw_path):
        self.path = hidraw_path
        self.fd = os.open(hidraw_path, os.O_WRONLY)
        self.frame_number = 0

    def _write_report(self, report: bytes):
        os.write(self.fd, report)

    def set_volume(self, headphone_l, headphone_r, mic=0, speaker=0):
        """Send output report 0x11 setting only the volume fields.

        The flags byte enables *only* the volume fields so the kernel
        driver's LED/rumble state is left untouched.
        """
        pkt = bytearray(78)
        pkt[0] = 0x11
        pkt[1] = 0xC0  # HID + CRC, default poll interval
        pkt[3] = (
            FLAG_VOLUME_L | FLAG_VOLUME_R | FLAG_VOLUME_MIC | FLAG_VOLUME_SPEAKER
        )
        pkt[21] = min(headphone_l, 100)
        pkt[22] = min(headphone_r, 100)
        pkt[23] = min(mic, 100)
        pkt[24] = min(speaker, 100)
        pkt[74:78] = bt_crc(bytes(pkt[:74]))
        self._write_report(bytes(pkt))

    def send_audio(self, sbc_data: bytes):
        """Send one 0x17 audio report containing 4 SBC frames (448 bytes)."""
        self.frame_number = (self.frame_number + AUDIO_FRAMES_PER_REPORT) & 0xFFFF
        report = (
            bytes((AUDIO_REPORT_ID, 0x40, 0xA0))
            + struct.pack("<H", self.frame_number)
            + bytes((AUDIO_HEADER,))
            + sbc_data
            + bytes(AUDIO_PAYLOAD_SIZE - len(sbc_data))
        )
        self._write_report(report + bt_crc(report))

    def close(self):
        os.close(self.fd)


# ---------------------------------------------------------------------------
# PulseAudio/PipeWire virtual sink + capture
# ---------------------------------------------------------------------------

class PulseSink:
    """A null sink whose monitor is captured with parec at the SBC format."""

    SAMPLE_RATE = 32000

    def __init__(self, name="ds4_headphones", description="DualShock 4 Headphones"):
        self.name = name
        self.module_id = subprocess.check_output(
            [
                "pactl", "load-module", "module-null-sink",
                f"sink_name={name}",
                f"rate={self.SAMPLE_RATE}",
                "channels=2",
                "sink_properties=device.description='"
                + description
                + "' device.icon_name='audio-headphones'",
            ],
            text=True,
        ).strip()
        self.parec = None

    def start_capture(self):
        self.parec = subprocess.Popen(
            [
                "parec",
                "--device", f"{self.name}.monitor",
                "--format=s16le",
                f"--rate={self.SAMPLE_RATE}",
                "--channels=2",
                "--latency-msec=20",
                "--raw",
            ],
            stdout=subprocess.PIPE,
        )
        return self.parec.stdout

    def read_exact(self, n):
        """Read exactly n bytes from the capture stream (or b'' on EOF)."""
        buf = b""
        while len(buf) < n:
            chunk = self.parec.stdout.read(n - len(buf))
            if not chunk:
                return b""
            buf += chunk
        return buf

    def close(self):
        if self.parec:
            self.parec.terminate()
            try:
                self.parec.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.parec.kill()
            self.parec = None
        subprocess.call(["pactl", "unload-module", self.module_id])


# ---------------------------------------------------------------------------
# Main streaming loop
# ---------------------------------------------------------------------------

def stream(device: DS4AudioDevice, volume: int, stats_interval: float = 0):
    encoder = SBCEncoder()
    pcm_per_report = encoder.codesize * AUDIO_FRAMES_PER_REPORT
    sbc_per_report = encoder.frame_length * AUDIO_FRAMES_PER_REPORT
    assert sbc_per_report == 448, f"unexpected SBC frame size {encoder.frame_length}"

    log.info(
        "SBC: frame=%dB codesize=%dB -> %d frames / %d PCM bytes per report",
        encoder.frame_length, encoder.codesize,
        AUDIO_FRAMES_PER_REPORT, pcm_per_report,
    )

    sink = PulseSink()
    try:
        sink.start_capture()
        device.set_volume(volume, volume)
        log.info("Streaming to %s (headphone volume %d/100)", device.path, volume)

        reports = 0
        t0 = time.monotonic()
        while True:
            pcm = sink.read_exact(pcm_per_report)
            if not pcm:
                log.warning("Capture stream ended")
                break
            sbc = b"".join(
                encoder.encode_frame(pcm[i:i + encoder.codesize])
                for i in range(0, len(pcm), encoder.codesize)
            )
            device.send_audio(sbc)
            reports += 1
            if stats_interval and reports % int(stats_interval * 62.5) == 0:
                elapsed = time.monotonic() - t0
                log.info(
                    "%d reports in %.1fs (%.1f/s, expect 62.5/s)",
                    reports, elapsed, reports / elapsed,
                )
    finally:
        sink.close()
        encoder.close()


def main():
    parser = argparse.ArgumentParser(
        description="DualShock 4 Bluetooth headphone audio daemon"
    )
    parser.add_argument(
        "--volume", type=int, default=70,
        help="headphone hardware volume 0-100 (default: 70)",
    )
    parser.add_argument(
        "--wait", action="store_true",
        help="wait for a controller instead of exiting when none is found",
    )
    parser.add_argument(
        "--stats", type=float, default=0, metavar="SECONDS",
        help="log throughput stats every SECONDS",
    )
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))

    while True:
        found = find_ds4_hidraw()
        if not found:
            if not args.wait:
                log.error("No Bluetooth DualShock 4 found")
                return 1
            time.sleep(2)
            continue

        hidraw_path, mac = found
        log.info("Found DualShock 4 %s at %s", mac, hidraw_path)
        try:
            device = DS4AudioDevice(hidraw_path)
        except OSError as err:
            log.error("Cannot open %s: %s", hidraw_path, err)
            if not args.wait:
                return 1
            time.sleep(2)
            continue

        try:
            stream(device, args.volume, args.stats)
        except OSError as err:
            log.warning("Device error (disconnected?): %s", err)
        finally:
            device.close()

        if not args.wait:
            return 0
        log.info("Waiting for controller to reappear...")
        time.sleep(2)


if __name__ == "__main__":
    sys.exit(main())
