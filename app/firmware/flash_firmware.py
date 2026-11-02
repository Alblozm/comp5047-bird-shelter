#!/usr/bin/env python3
"""Flash the Shelter firmware without PlatformIO (macOS or Windows).

  python flash_firmware.py climate     FireBeetle 2 ESP32-E  -> shelter-1 firmware
  python flash_firmware.py camera      DFR1154 camera board
Close the Shelter console first: a serial port can only be used by one program.
Installs esptool with pip on first use.
"""
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "vendor"))
from serial.tools import list_ports  # noqa: E402

BOARDS = {"climate": ("shelter-climate.factory.bin", 0x1A86, "esp32"),
          "camera": ("shelter-camera.factory.bin", 0x303A, "esp32s3")}


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in BOARDS:
        sys.exit(__doc__)
    image, vid, chip = BOARDS[sys.argv[1]]
    port = sys.argv[2] if len(sys.argv) > 2 else next(
        (p.device for p in list_ports.comports() if p.vid == vid), None)
    if not port:
        sys.exit("没找到这块板子的 USB 串口。换一根数据线，或手动指定：python flash_firmware.py "
                 + sys.argv[1] + " COM5")
    try:
        import esptool  # noqa: F401
    except ImportError:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--user", "esptool"])
    print(f"烧录 {image} 到 {port} …")
    subprocess.check_call([sys.executable, "-m", "esptool", "--chip", chip, "--port", port,
                           "--baud", "460800", "write-flash", "0x0", str(HERE / image)])
    print("完成。拔插一次 USB，再启动控制台。")


if __name__ == "__main__":
    main()
