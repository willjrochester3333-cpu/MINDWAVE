# main.py — runs on the Raspberry Pi Pico
# ==========================================
# Reads "A:<attention>,M:<meditation>\n" lines over USB serial from
# mindwave_pico_bridge.py (running on the laptop) and shows them on a
# 128x32 SSD1306 OLED, plus color-codes a WS2812B/NeoPixel strip by
# blending focus and calm: green = focused (high attention), red =
# calm (high meditation), amber/yellow = a mix of both. Also shows a
# motivational quote (word-wrapped) and buzzes a buzzer for a few
# seconds whenever it receives a "Q:<text>" line, which
# mindwave_pico_bridge.py sends each time its study coach prints one.
#
# WIRING
# ------
# OLED (SSD1306, I2C):
#     SDA -> GPIO0  (physical pin 1)
#     SCL -> GPIO1  (physical pin 2)
#     VCC -> 3V3    (physical pin 36)
#     GND -> GND
#
# WS2812B / NeoPixel strip:
#     DIN -> GPIO28 (physical pin 34) — a ~330 ohm resistor in series is good practice
#     5V  -> VBUS (physical pin 40), or an external 5V supply for longer strips
#     GND -> GND (shared with the Pico)
#
# Buzzer (optional — buzzes once whenever a motivational quote appears
# in mindwave_pico_bridge.py's console):
#     Signal -> GPIO11 (physical pin 15)
#     GND    -> GND
#     A simple active buzzer module can wire directly to GPIO11. A bare
#     passive piezo will need driving through a transistor instead.
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
NUM_LEDS     = 15     # how many LEDs are on the strip
LED_PIN      = 28
BUZZER_PIN   = 11
BUZZ_MS      = 200    # how long the buzzer sounds for
QUOTE_DISPLAY_MS = 10000  # how long a motivational quote stays on screen
I2C_SDA_PIN  = 0
I2C_SCL_PIN  = 1
OLED_WIDTH   = 128
OLED_HEIGHT  = 32
DATA_TIMEOUT = 5000   # ms without a line before we show "no signal"
BRIGHTNESS   = 0.35   # 0..1, keeps the strip comfortable to look at
# ──────────────────────────────────────────────────────────────────────────────

i2c = I2C(0, sda=Pin(I2C_SDA_PIN), scl=Pin(I2C_SCL_PIN), freq=400000)
time.sleep(1)  # let the OLED finish powering up before we talk to it


def init_oled():
    """Scan the I2C bus and bring up the OLED if one answers.
    Returns None (instead of crashing) if nothing responds, so the LED
    strip still works even while the OLED wiring is being debugged."""
    addrs = i2c.scan()
    print("I2C devices found:", [hex(a) for a in addrs])
    addr = 0x3C if 0x3C in addrs else (0x3D if 0x3D in addrs else None)
    if addr is None:
        print("No SSD1306 responded on the I2C bus — check wiring: "
              "SDA=GPIO0, SCL=GPIO1, VCC=3V3, GND=GND.")
        return None
    try:
        return ssd1306.SSD1306_I2C(OLED_WIDTH, OLED_HEIGHT, i2c, addr=addr)
    except OSError as e:
        print("OLED found at", hex(addr), "but failed to initialize:", e)
        return None


oled = init_oled()

np = neopixel.NeoPixel(Pin(LED_PIN), NUM_LEDS)

buzzer = Pin(BUZZER_PIN, Pin.OUT)
buzzer.value(0)

stdin_poll = select.poll()
stdin_poll.register(sys.stdin, select.POLLIN)


def buzz():
    buzzer.value(1)
    time.sleep_ms(BUZZ_MS)
    buzzer.value(0)


def focus_color(attention, meditation):
    """Green = focused (attention), red = calm (meditation), blended
    when both are present, scaled by BRIGHTNESS."""
    attention = max(0, min(100, attention))
    meditation = max(0, min(100, meditation))
    r = int(255 * meditation / 100)
    g = int(255 * attention / 100)
    return (int(r * BRIGHTNESS), int(g * BRIGHTNESS), 0)


def show_reading(attention, meditation):
    # 128x32 only fits 4 rows of default 8px text, so keep it to two lines
    if oled:
        oled.fill(0)
        oled.text("Focus: {}".format(attention), 0, 0)
        oled.text("Calm:  {}".format(meditation), 0, 16)
        oled.show()

    color = focus_color(attention, meditation)
    for i in range(NUM_LEDS):
        np[i] = color
    np.write()


def wrap_text(text, width=16, max_lines=4):
    """Word-wrap text to fit the OLED's fixed 8px-per-char font."""
    words = text.split(" ")
    lines = []
    cur = ""
    for w in words:
        candidate = (cur + " " + w).strip()
        if len(candidate) <= width:
            cur = candidate
        else:
            if cur:
                lines.append(cur)
            cur = w[:width]  # a single very long word just gets hard-truncated
        if len(lines) >= max_lines:
            break
    if cur and len(lines) < max_lines:
        lines.append(cur)
    return lines[:max_lines]


def show_quote(text):
    if oled:
        oled.fill(0)
        for i, line in enumerate(wrap_text(text)):
            oled.text(line, 0, i * 8)
        oled.show()
    buzz()
    time.sleep_ms(max(0, QUOTE_DISPLAY_MS - BUZZ_MS))


def show_waiting(msg):
    if oled:
        oled.fill(0)
        oled.text("MindWave", 0, 0)
        oled.text(msg, 0, 16)
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


show_waiting("waiting...")
last_data_ms = time.ticks_ms()

showing_no_signal = False

while True:
    if stdin_poll.poll(500):
        line = sys.stdin.readline()
        if line.startswith("Q:"):
            show_quote(line[2:].strip())
            # a quote line is still proof the link is alive, so don't let the
            # time spent showing it push us into a false "no signal" state
            last_data_ms = time.ticks_ms()
            showing_no_signal = False
        else:
            parsed = parse_line(line)
            if parsed:
                last_data_ms = time.ticks_ms()
                showing_no_signal = False
                show_reading(*parsed)

    if not showing_no_signal and time.ticks_diff(time.ticks_ms(), last_data_ms) > DATA_TIMEOUT:
        showing_no_signal = True
        show_waiting("no signal")
