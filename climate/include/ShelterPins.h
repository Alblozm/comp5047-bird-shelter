#pragma once

// Current physical wiring, verified on the real board on 2026-10-26:
// SHT31 at 0x45 and BH1750 at 0x23 both respond with this GPIO assignment.
// ESP32 can route I2C to either pin; these differ from the board's defaults.
namespace ShelterPins {
constexpr int SDA_GPIO = 22;
constexpr int SCL_GPIO = 21;
}
