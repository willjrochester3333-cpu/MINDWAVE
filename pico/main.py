# main.py — runs on the Raspberry Pi Pico
# ==========================================
# Reads "A:<attention>,M:<meditation>\n" lines over USB serial from
# mindwave_pico_bridge.py (running on the laptop) and shows them on a
# 128x64 SSD1306 OLED, plus color-codes a WS2812B/NeoPixel strip by
# attention (focus) level: red = low focus, amber = mid, green = high.
#
# WIRING
# ------
# OLED (SSD1306, I2C):
#     SDA -> GPIO4  (physical pin 6)
#     SCL -> GPIO5  (physical pin 7)
#     VCC -> 3V3    (physical pin 36)
#     GND -> GND
#
# WS2812B / NeoPixel strip:
#     DIN -> GPIO16 (physical pin 21) — a ~330 ohm resistor in series is good practice
#     5V  -> VBUS (physical pin 40), or an external 5V supply for longer strips
#     GND -> GND (shared with the Pico)
#
# SETUP
# -----
# Copy this file AND ssd1306.py onto the Pico (e.g. with Thonny) as
# main.py and ssd1306.py so it runs automatically whenever the Pico is
# powered on or plugged into USB.

import sys
import select
import time
from machine import Pin, I2C
import neopixel
import ssd1306

# ── Config ──────────────────────────────────────────────────────────────────
NUM_LEDS     = 8      # how many LEDs are on the strip
LED_PIN      = 16
I2C_SDA_PIN  = 4
I2C_SCL_PIN  = 5
OLED_WIDTH   = 128
OLED_HEIGHT  = 64
DATA_TIMEOUT = 5000   # ms without a line before we show "no signal"
BRIGHTNESS   = 0.35   # 0..1, keeps the strip comfortable to look at
# ──────────────────────────────────────────────────────────────────────────────

i2c = I2C(0, sda=Pin(I2C_SDA_PIN), scl=Pin(I2C_SCL_PIN), freq=400000)
oled = ssd1306.SSD1306_I2C(OLED_WIDTH, OLED_HEIGHT, i2c)

np = neopixel.NeoPixel(Pin(LED_PIN), NUM_LEDS)

stdin_poll = select.poll()
stdin_poll.register(sys.stdin, select.POLLIN)


def focus_color(attention):
    """Red (low) -> amber (mid) -> green (high), scaled by BRIGHTNESS."""
    attention = max(0, min(100, attention))
    if attention < 50:
        t = attention / 50
        r, g, b = 255, int(160 * t), 0
    else:
        t = (attention - 50) / 50
        r, g, b = int(255 * (1 - t)), int(160 + 95 * t), 0
    return (int(r * BRIGHTNESS), int(g * BRIGHTNESS), int(b * BRIGHTNESS))


def show_reading(attention, meditation):
    oled.fill(0)
    oled.text("MindWave", 0, 0)
    oled.text("-" * 16, 0, 10)
    oled.text("Focus", 0, 26)
    oled.text(str(attention), 84, 26)
    oled.text("Calm", 0, 44)
    oled.text(str(meditation), 84, 44)
    oled.show()

    color = focus_color(attention)
    for i in range(NUM_LEDS):
        np[i] = color
    np.write()


def show_waiting(msg):
    oled.fill(0)
    oled.text("MindWave", 0, 0)
    oled.text("-" * 16, 0, 10)
    oled.text(msg, 0, 30)
    oled.show()
    for i in range(NUM_LEDS):
        np[i] = (0, 0, 20)  # dim blue = idle/no data yet
    np.write()


def parse_line(line):
    """Expects "A:65,M:42" — returns (attention, meditation) or None."""
    attention = meditation = None
    for part in line.strip().split(","):
        if part.startswith("A:"):
            attention = int(part[2:])
        elif part.startswith("M:"):
            meditation = int(part[2:])
    if attention is None or meditation is None:
        return None
    return attention, meditation


show_waiting("waiting for data...")
last_data_ms = time.ticks_ms()

showing_no_signal = False

while True:
    if stdin_poll.poll(500):
        line = sys.stdin.readline()
        parsed = parse_line(line)
        if parsed:
            last_data_ms = time.ticks_ms()
            showing_no_signal = False
            show_reading(*parsed)

    if not showing_no_signal and time.ticks_diff(time.ticks_ms(), last_data_ms) > DATA_TIMEOUT:
        showing_no_signal = True
        show_waiting("no signal")
