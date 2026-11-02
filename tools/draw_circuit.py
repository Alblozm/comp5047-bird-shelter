#!/usr/bin/env python3
"""Draw the Shelter circuit schematic and system diagram (schemdraw, IEEE symbols).

  pip install schemdraw matplotlib
  python tools/draw_circuit.py        -> docs/circuit/*.svg and *.png

Pin numbers match climate/src/shelter.cpp and app/README_使用说明.md.
"""
from pathlib import Path

import matplotlib
import schemdraw
import schemdraw.elements as elm

matplotlib.rcParams["font.family"] = ["Helvetica", "Arial", "DejaVu Sans"]
OUT = Path(__file__).resolve().parents[1] / "docs" / "circuit"
OUT.mkdir(parents=True, exist_ok=True)

INK, MUTED, POWER, SIGNAL, I2C = "#1f2a24", "#6b7a72", "#b3362a", "#1f5f8b", "#2f7a4f"


def pin(name, side, anchor=None, color=None):
    return elm.IcPin(name=name, side=side, anchorname=anchor or name.split()[0].replace("/", ""))


def line(d, points, color, width=1.6):
    for a, b in zip(points, points[1:]):
        d += elm.Line().at(a).to(b).color(color).linewidth(width)


def power(d, at, up=True):
    """3V3 flag or ground symbol attached to a pin end."""
    if up:
        d += elm.Line().at(at).up(.6).color(POWER)
        d += elm.Vdd().label("3V3", fontsize=9).color(POWER)
    else:
        d += elm.Line().at(at).down(.6).color(INK)
        d += elm.Ground().color(INK)


def schematic():
    with schemdraw.Drawing(show=False) as d:
        d.config(fontsize=10, unit=2, lw=1.4, color=INK)

        # ---------------- main controller ----------------
        # Pin order is chosen so that no two wires cross. schemdraw stacks left/right
        # pins from the bottom up, so the list runs bottom -> top.
        mcu = elm.Ic(pins=[
            pin("GND", "right", "GND"), pin("VCC ≈4.7 V", "right", "VCC"),
            pin("GPIO25 · D2", "right", "D2"), pin("GPIO26 · D3", "right", "D3"),
            pin("GPIO34 · A2", "right", "A2"), pin("GPIO22 · SCL", "right", "SCL"),
            pin("GPIO21 · SDA", "right", "SDA"), pin("3V3", "right", "V33"),
            pin("USB-C", "left", "USB")],
            size=(7.6, 14), pinspacing=1.6, edgepadH=1.2, leadlen=.8)
        d += mcu.at((0, 0)).anchor("center").theta(0).label("FireBeetle 2\nESP32-E\n(DFR0654)", loc="center", fontsize=12)
        A = mcu.absanchors

        bank = elm.Ic(pins=[pin("5 V", "right", "OUT")], size=(3.2, 1.6), leadlen=.5)
        d += bank.at((A["USB"][0] - 1.4, A["USB"][1])).anchor("OUT").theta(0).label("USB power\nbank", loc="center", fontsize=9)
        line(d, [bank.absanchors["OUT"], A["USB"]], POWER)
        power(d, A["V33"], up=True)
        line(d, [A["GND"], (A["GND"][0] + .8, A["GND"][1])], INK)
        power(d, (A["GND"][0] + .8, A["GND"][1]), up=False)

        X = 15.5  # x of the peripheral pin ends

        def device(name, pins, top, h):
            ic = elm.Ic(pins=[pin(n, "left", a) for n, a in pins], size=(4.6, h), pinspacing=.9,
                        edgepadH=.5, leadlen=.7)
            d.add(ic.at((X + 3.0, top - h / 2)).anchor("center").theta(0).label(name, loc="top", fontsize=9))
            return ic.absanchors

        def tie_power(anc):
            line(d, [anc["VCC"], (anc["VCC"][0] - .9, anc["VCC"][1])], POWER)
            power(d, (anc["VCC"][0] - .9, anc["VCC"][1]), up=True)
            line(d, [anc["GND"], (anc["GND"][0] - .5, anc["GND"][1])], INK)
            power(d, (anc["GND"][0] - .5, anc["GND"][1]), up=False)

        sht = device("SHT31-F (SEN0334) · temperature / humidity · I²C 0x45",
                     [("GND", "GND"), ("SCL", "SCL"), ("SDA", "SDA"), ("VCC", "VCC")], 15.4, 3.6)
        bh = device("BH1750 (SEN0097) · ambient light · I²C 0x23",
                    [("GND", "GND"), ("ADDR  n.c.", "ADDR"), ("SCL", "SCL"), ("SDA", "SDA"), ("VCC", "VCC")], 9.8, 4.4)
        wl = device("SEN0508 · non-contact water level · OUT low = water",
                    [("GND", "GND"), ("OUT", "OUT"), ("VCC", "VCC")], -3.2, 2.8)
        for anc in (sht, bh, wl):
            tie_power(anc)

        # I2C bus: SDA and SCL rails with a tap (dot) to each sensor
        sda_x, scl_x = 8.6, 9.6
        line(d, [A["SDA"], (sda_x, A["SDA"][1]), (sda_x, sht["SDA"][1]), sht["SDA"]], I2C)
        line(d, [A["SCL"], (scl_x, A["SCL"][1]), (scl_x, sht["SCL"][1]), sht["SCL"]], I2C)
        line(d, [(sda_x, bh["SDA"][1]), bh["SDA"]], I2C)
        line(d, [(scl_x, bh["SCL"][1]), bh["SCL"]], I2C)
        d += elm.Dot().at((sda_x, bh["SDA"][1])).color(I2C)
        d += elm.Dot().at((scl_x, bh["SCL"][1])).color(I2C)
        d += elm.Label().at((sda_x - .25, (A["SDA"][1] + bh["SDA"][1]) / 2)).label("I²C bus", halign="right", color=I2C, fontsize=9)

        # FSR divider: 3V3 - FSR - node - 10k - GND, node -> GPIO34 (straight across)
        node = (12.8, A["A2"][1])
        line(d, [A["A2"], node], SIGNAL)
        d += elm.Dot().at(node).color(SIGNAL)
        d += elm.ResistorVar().at(node).up(2.4).label("FSR 402\nperch pressure", loc="bottom", fontsize=9).color(INK)
        d += elm.Vdd().label("3V3", fontsize=9).color(POWER)
        d += elm.Resistor().at(node).down(2.4).label("10 kΩ", loc="bottom", fontsize=9).color(INK)
        d += elm.Ground().color(INK)
        d += elm.Label().at((node[0] + .35, node[1] + .45)).label("more force → higher ADC", halign="left",
                                                                 fontsize=8, color=MUTED)

        # water level: lane on the right so it never crosses the mister wiring
        line(d, [A["D3"], (11.8, A["D3"][1]), (11.8, wl["OUT"][1]), wl["OUT"]], SIGNAL)

        # mister: Grove driver + ultrasonic transducer
        mist = elm.Ic(pins=[pin("GND", "left", "GND"), pin("NC", "left", "NC"), pin("VCC", "left", "VCC"),
                            pin("SIG / EN", "left", "SIG"), pin("", "right", "OUT2"), pin("", "right", "OUT1")],
                      size=(4.6, 4.4), pinspacing=.9, edgepadH=.5, leadlen=.7)
        d += mist.at((X + 3.0, -10.0)).anchor("center").theta(0).label(
            "Grove Water Atomization driver · 5 V, max 2 W", loc="top", fontsize=9)
        M = mist.absanchors
        line(d, [A["D2"], (11.2, A["D2"][1]), (11.2, M["SIG"][1]), M["SIG"]], SIGNAL)
        line(d, [A["VCC"], (10.6, A["VCC"][1]), (10.6, M["VCC"][1]), M["VCC"]], POWER)
        line(d, [M["GND"], (M["GND"][0] - .5, M["GND"][1])], INK)
        power(d, (M["GND"][0] - .5, M["GND"][1]), up=False)
        transducer = elm.Ic(pins=[pin("", "left", "IN2"), pin("", "left", "IN1")], size=(2.8, 1.8), pinspacing=.9,
                            edgepadH=.45, leadlen=.5)
        d += transducer.at((M["OUT1"][0] + 1.0, M["OUT1"][1])).anchor("IN1").theta(0).label(
            "Ultrasonic\ntransducer", loc="center", fontsize=8)
        T = transducer.absanchors
        line(d, [M["OUT1"], T["IN1"]], INK)
        line(d, [M["OUT2"], T["IN2"]], INK)
        d += elm.Label().at(((M["OUT1"][0] + T["IN1"][0]) / 2 + 1.9, T["IN2"][1] - 1.25)).label(
            "≈65 V peak · keep dry", fontsize=8, color=MUTED)

        # camera node: separate board, no wires to the FireBeetle
        cam = elm.Ic(pins=[pin("USB-C", "left", "USB")], size=(7.6, 3.6), leadlen=.8)
        d += cam.at((0, -15.4)).anchor("center").theta(0).label("DFR1154\nESP32-S3 AI Camera", loc="center", fontsize=11)
        bank2 = elm.Ic(pins=[pin("5 V", "right", "OUT")], size=(3.2, 1.6), leadlen=.5)
        d += bank2.at((cam.absanchors["USB"][0] - 1.4, cam.absanchors["USB"][1])).anchor("OUT").theta(0).label(
            "USB power\nbank", loc="center", fontsize=9)
        line(d, [bank2.absanchors["OUT"], cam.absanchors["USB"]], POWER)
        d += elm.Label().at((5.4, -15.4)).label(
            "Camera board: no wired connection to the FireBeetle.\n"
            "Both boards talk to the laptop console over WiFi (USB while debugging).",
            halign="left", fontsize=8.5, color=MUTED)

        # title and legend
        d += elm.Label().at((-11.0, 19.6)).label("Urban Bird Microclimate Shelter — circuit schematic", halign="left", fontsize=15)
        d += elm.Label().at((-11.0, 18.6)).label(
            "COMP5047 · rev 2 · 29 Oct 2026 · 3.3 V logic · pins: GPIO number · board silkscreen label",
            halign="left", fontsize=9, color=MUTED)
        lx, ly = -11.0, -19.4
        for i, (text, color) in enumerate([("power (3V3 / 5 V)", POWER), ("I²C data", I2C),
                                            ("digital / analog signal", SIGNAL), ("ground / other", INK)]):
            line(d, [(lx + i * 5.2, ly), (lx + i * 5.2 + .9, ly)], color, 2.4)
            d += elm.Label().at((lx + i * 5.2 + 1.1, ly)).label(text, halign="left", fontsize=8.5)
        d += elm.Label().at((lx + 21.4, ly)).label("● = connection · crossing lines without ● are not connected",
                                                   halign="left", fontsize=8.5, color=MUTED)
        for ext in ("svg", "png"):
            d.save(str(OUT / f"shelter_schematic.{ext}"), dpi=200)


if __name__ == "__main__":
    schematic()
    print("saved to", OUT)
