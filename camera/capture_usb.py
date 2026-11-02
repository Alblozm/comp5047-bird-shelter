"""Capture an actual JPEG from the DFR1154 bring-up firmware via USB.

Use the PlatformIO Python, which already supplies pyserial:
  ~/.platformio/penv/bin/python capture_usb.py --port /dev/cu.usbmodem1101
"""
import argparse
import json
import time
from datetime import datetime
from pathlib import Path

import serial


def capture(port, output):
    with serial.Serial(port, 115200, timeout=0.5, write_timeout=5) as device:
        deadline = time.monotonic() + 18
        ready = False
        while time.monotonic() < deadline:
            device.write(b"?\n")
            line = device.readline().decode("utf-8", errors="replace").strip()
            if line:
                print(line, flush=True)
            try:
                state = json.loads(line)
            except (ValueError, TypeError):
                continue
            if isinstance(state, dict) and state.get("camera_ready") is True:
                ready = True
                break
        if not ready:
            raise RuntimeError("Camera did not report ready; see serial diagnostics above")
        time.sleep(2)  # Allow automatic exposure to settle.
        device.reset_input_buffer()
        device.write(b"p\n")
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            line = device.readline().strip()
            if line.startswith(b"JPEG_BEGIN "):
                length = int(line.split()[1])
                break
            if line:
                print(line.decode("utf-8", errors="replace"), flush=True)
        else:
            raise RuntimeError("No JPEG header received")
        if not 100 <= length <= 2_000_000:
            raise RuntimeError(f"Invalid JPEG length: {length}")
        image = bytearray()
        deadline = time.monotonic() + 20
        while len(image) < length and time.monotonic() < deadline:
            image.extend(device.read(length - len(image)))
        if len(image) != length:
            raise RuntimeError(f"Incomplete JPEG: {len(image)}/{length} bytes")
        if not image.startswith(b"\xff\xd8") or not image.endswith(b"\xff\xd9"):
            raise RuntimeError("Invalid JPEG start/end markers")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(image)
        print(f"Saved real camera photo: {output} ({length} bytes)", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True)
    parser.add_argument("--output", type=Path, default=Path(__file__).parent / "captures" / (datetime.now().strftime("%Y%m%d-%H%M%S") + ".jpg"))
    args = parser.parse_args()
    capture(args.port, args.output)
