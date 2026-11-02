# DFR1154 camera hardware bring-up

Independent PlatformIO project using the existing course platform 55.03.311.
The camera pins and OV3660 adjustments follow DFRobot's official timed-camera
example: https://wiki.dfrobot.com/dfr1154/docs/21371

This adaptation sends a VGA JPEG over native USB Serial/JTAG, so the first test
does not require WiFi credentials or an SD card. It is not the final WiFi firmware.
Do not attach the mister, external power or other peripherals to this board.

```
~/.platformio/penv/bin/pio run -t upload --upload-port /dev/cu.usbmodem1101
~/.platformio/penv/bin/python capture_usb.py --port /dev/cu.usbmodem1101
```

Validation: the real board must report camera_ready=true, then produce a JPEG
that decodes and shows the scene currently in front of the lens. Compilation
alone does not verify camera operation. See VERIFICATION.md for actual results.
