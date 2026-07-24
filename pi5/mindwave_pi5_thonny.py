#!/usr/bin/env python3
"""
mindwave_pi5_thonny.py — everything in one file, meant to be run
directly in Thonny (or `python3 mindwave_pi5_thonny.py`) on the Pi 5.
No separate bash script, no separate config file.

The first time you run it, it installs everything it needs itself
(enables I2C/SPI/Bluetooth, installs packages) — that takes a few
minutes. After that it advertises itself over Bluetooth Low Energy
(BLE) as a Nordic UART Service peripheral, the exact same service the
Pico's BLE firmware uses, so mindwave_pico_bridge.py --link bluetooth
on your laptop needs NO changes to talk to this instead of a Pico. It
then shows "A:<attention>,M:<meditation>" readings on an SSD1306 OLED,
color-codes a WS2812B/NeoPixel strip by blending focus/calm, and
buzzes a buzzer + shows motivational quotes on "Q:<text>" lines.

HOW TO RUN THIS IN THONNY
--------------------------
1. Open a terminal on the Pi and launch Thonny WITH ROOT — needed
   because registering a BLE peripheral through BlueZ requires it:
       sudo thonny
2. Open this file (or paste its contents into a new one) and press
   Run (F5). First run takes a few minutes and needs the Pi to be on
   WiFi (to download packages) — after that, package installs are
   skipped automatically on future runs, so it starts in a second or
   two, and WiFi isn't needed anymore for BLE itself.
3. You should see "Advertising as 'MindWave' — waiting for the laptop
   to connect..." in Thonny's Shell.
4. On your laptop, run:  py mindwave_pico_bridge.py --link bluetooth
   No pairing needed — it just scans for the advertised name.

WIRING (Raspberry Pi 5, 40-pin header)
-----------------------------------------------------------------------
OLED (SSD1306, I2C1):      SDA->GPIO2 (pin 3)  SCL->GPIO3 (pin 5)
                           VCC->3V3   (pin 1)  GND->GND   (pin 9)
WS2812B / NeoPixel strip:  DIN->GPIO10/SPI0 MOSI (pin 19), ~330 ohm
                           resistor in series is good practice.
                           Use an external 5V supply for more than a
                           few LEDs (15 LEDs can draw ~900mA) — share
                           its ground with the Pi's GND. The Pi's own
                           5V pin (2/4) is fine only for a short strip.
Buzzer:                    Signal->GPIO17 (pin 11)  GND->GND (pin 14)
Bluetooth needs no wiring — it's the Pi 5's onboard radio.

NOTE on the LED strip: the Pi 5's RP1 I/O chip broke the traditional
PWM+DMA method most WS2812B libraries rely on, so this drives it over
SPI instead (adafruit-circuitpython-neopixel-spi). This, and the BLE
peripheral itself, are the two parts that couldn't be tested on real
Pi 5 hardware ahead of time — if something doesn't work, they're the
first place to look.

Want this running automatically on every boot instead of by hand in
Thonny? See mindwave-pi5-all-in-one.sh in this same folder — same
code, packaged to install itself as a systemd service.
"""

import os
import subprocess
import sys

# ── Config ────────────────────────────────────────────────────────────────────
BLE_NAME     = "MindWave"   # must match --ble-name on the laptop
NUM_LEDS     = 15     # how many LEDs are on the strip
BUZZER_PIN   = 17
BUZZ_S       = 0.2    # how long the buzzer sounds for
QUOTE_DISPLAY_S = 10.0  # how long a motivational quote stays on screen
OLED_I2C_ADDR = None   # None = auto-detect (tries 0x3C then 0x3D)
OLED_WIDTH   = 128
OLED_HEIGHT  = 32
DATA_TIMEOUT_S = 5.0   # seconds without a line before we show "no signal"
BRIGHTNESS   = 0.35    # 0..1, keeps the strip comfortable to look at
UART_SERVICE_UUID = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
UART_RX_CHAR_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"

# Set to True after your first successful run to skip the install step
# on future runs and start straight away — nothing below this line
# changes what it does, just how long it takes to get started.
SKIP_SETUP = False
# ──────────────────────────────────────────────────────────────────────────────


def _run(cmd):
    print(f"$ {' '.join(cmd)}", flush=True)
    try:
        result = subprocess.run(cmd)
    except FileNotFoundError:
        print(f"  ('{cmd[0]}' not found — continuing anyway)", flush=True)
        return
    if result.returncode != 0:
        print(f"  (exit code {result.returncode} — continuing anyway)", flush=True)


def _setup():
    if os.geteuid() != 0:
        raise SystemExit(
            "This needs to run as root (BLE peripheral setup needs it). "
            "Close Thonny and reopen it with: sudo thonny"
        )
    print("== MindWave Pi 5 one-time setup — this takes a few minutes ==", flush=True)
    _run(["raspi-config", "nonint", "do_i2c", "0"])
    _run(["raspi-config", "nonint", "do_spi", "0"])
    _run(["rfkill", "unblock", "bluetooth"])
    _run(["apt-get", "update"])
    _run(["apt-get", "install", "-y", "bluez", "python3-pip", "python3-venv", "i2c-tools"])
    _run([sys.executable, "-m", "pip", "install", "--break-system-packages",
          "luma.oled", "Pillow", "adafruit-blinka",
          "adafruit-circuitpython-neopixel-spi", "gpiozero", "rpi-lgpio", "bless"])
    _run(["systemctl", "enable", "--now", "bluetooth"])
    print("== Setup done — starting the display ==", flush=True)
    print("(set SKIP_SETUP = True near the top of this file to skip this next time)", flush=True)


if not SKIP_SETUP:
    _setup()

import queue
import signal
import threading
import time
import asyncio
from bless import (
    BlessServer,
    GATTCharacteristicProperties,
    GATTAttributePermissions,
)


# ── OLED (SSD1306 over I2C, via luma.oled) ────────────────────────────────────
def init_oled():
    """Bring up the OLED. Returns None (instead of crashing) if it can't be
    found, so the LED strip/buzzer still work while OLED wiring is debugged."""
    try:
        from luma.core.interface.serial import i2c
        from luma.core.error import DeviceNotFoundError
        from luma.oled.device import ssd1306
    except ImportError as e:
        print(f"OLED libraries not installed ({e}) — continuing without it.")
        return None

    addrs_to_try = [OLED_I2C_ADDR] if OLED_I2C_ADDR else [0x3C, 0x3D]
    for addr in addrs_to_try:
        try:
            serial = i2c(port=1, address=addr)
            device = ssd1306(serial, width=OLED_WIDTH, height=OLED_HEIGHT)
            print(f"OLED found at {hex(addr)}")
            return device
        except DeviceNotFoundError:
            continue
        except Exception as e:
            print(f"OLED found at {hex(addr)} but failed to initialize: {e}")
            return None
    print("No SSD1306 responded on I2C1 — check wiring: SDA=GPIO2, SCL=GPIO3, "
          "VCC=3V3, GND=GND, and that I2C is enabled (raspi-config).")
    return None


oled = init_oled()


def oled_lines(*lines):
    """Clear and draw up to 4 lines of text (8px each, matches the Pico's
    16-char/4-line layout closely enough for the same short strings)."""
    if not oled:
        return
    from luma.core.render import canvas
    with canvas(oled) as draw:
        for i, line in enumerate(lines[:4]):
            draw.text((0, i * 8), line, fill="white")


# ── LED strip (WS2812B over SPI) ──────────────────────────────────────────────
def init_strip():
    try:
        import board
        import busio
        import neopixel_spi as neopixel
        spi = busio.SPI(board.SCK, MOSI=board.MOSI)
        pixels = neopixel.NeoPixel_SPI(spi, NUM_LEDS, pixel_order=neopixel.GRB, auto_write=False)
        print("LED strip initialized over SPI")
        return pixels
    except Exception as e:
        print(f"LED strip init failed ({e}) — continuing without it.")
        return None


strip = init_strip()


def set_strip_color(rgb):
    if not strip:
        return
    strip.fill(rgb)
    strip.show()


def set_strip_idle():
    set_strip_color((0, 0, 20))  # dim blue = idle/no data yet


# ── Buzzer ─────────────────────────────────────────────────────────────────────
def init_buzzer():
    try:
        from gpiozero import DigitalOutputDevice
        return DigitalOutputDevice(BUZZER_PIN)
    except Exception as e:
        print(f"Buzzer init failed ({e}) — continuing without it.")
        return None


buzzer = init_buzzer()


def buzz():
    if not buzzer:
        return
    buzzer.on()
    time.sleep(BUZZ_S)
    buzzer.off()


# ── Display helpers ────────────────────────────────────────────────────────────
def focus_color(attention, meditation):
    """Green = focused (attention), red = calm (meditation), blended
    when both are present, scaled by BRIGHTNESS."""
    attention = max(0, min(100, attention))
    meditation = max(0, min(100, meditation))
    r = int(255 * meditation / 100 * BRIGHTNESS)
    g = int(255 * attention / 100 * BRIGHTNESS)
    return (r, g, 0)


def show_reading(attention, meditation):
    oled_lines(f"Focus: {attention}", f"Calm:  {meditation}")
    set_strip_color(focus_color(attention, meditation))


def wrap_text(text, width=20, max_lines=4):
    """Word-wrap text for the OLED."""
    words = text.split(" ")
    lines, cur = [], ""
    for w in words:
        candidate = (cur + " " + w).strip()
        if len(candidate) <= width:
            cur = candidate
        else:
            if cur:
                lines.append(cur)
            cur = w[:width]
        if len(lines) >= max_lines:
            break
    if cur and len(lines) < max_lines:
        lines.append(cur)
    return lines[:max_lines]


def show_quote(text):
    oled_lines(*wrap_text(text))
    buzz()
    time.sleep(max(0.0, QUOTE_DISPLAY_S - BUZZ_S))


def show_waiting(msg):
    oled_lines("MindWave", msg)
    set_strip_idle()


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


# ── BLE peripheral (Nordic UART Service, same as pico/main.py's BLE mode) ────
# Incoming writes land on a background asyncio thread (bless is async-only);
# they're pushed onto this thread-safe queue as parsed lines, and the main
# loop below — running on the main thread — just reads from it, so all the
# OLED/LED/buzzer calls stay safely single-threaded.
_incoming_lines = queue.Queue()
_rx_buffer = bytearray()


def _handle_write(characteristic, value):
    global _rx_buffer
    characteristic.value = value
    _rx_buffer += bytes(value)
    while b"\n" in _rx_buffer:
        raw, _, rest = _rx_buffer.partition(b"\n")
        _rx_buffer = bytearray(rest)
        _incoming_lines.put(raw.decode("utf-8", "ignore"))


# Set once the server's up, so a Ctrl+C / stop can cleanly unregister the
# advertisement instead of leaving BlueZ thinking it's still active — that
# stale state is the most common cause of a later "failed to register
# advertisement" error on the next run.
_ble_loop = None
_ble_server = None


async def _run_ble_server():
    global _ble_server
    server = BlessServer(name=BLE_NAME)
    server.write_request_func = _handle_write
    await server.add_new_service(UART_SERVICE_UUID)
    await server.add_new_characteristic(
        UART_SERVICE_UUID,
        UART_RX_CHAR_UUID,
        GATTCharacteristicProperties.write,
        None,
        GATTAttributePermissions.writeable,
    )
    await server.start()
    _ble_server = server
    print(f"Advertising as '{BLE_NAME}' — waiting for the laptop to connect...", flush=True)
    while True:
        await asyncio.sleep(3600)  # server runs via D-Bus callbacks; just keep the loop alive


def start_ble_server():
    global _ble_loop
    _ble_loop = asyncio.new_event_loop()

    def runner():
        asyncio.set_event_loop(_ble_loop)
        _ble_loop.run_until_complete(_run_ble_server())

    threading.Thread(target=runner, daemon=True).start()


def stop_ble_server():
    if _ble_server is None or _ble_loop is None:
        return
    try:
        asyncio.run_coroutine_threadsafe(_ble_server.stop(), _ble_loop).result(timeout=5)
        print("BLE advertisement stopped cleanly.", flush=True)
    except Exception as e:
        print(f"Couldn't cleanly stop the BLE advertisement ({e}) — if the next "
              f"run fails with 'failed to register advertisement', run: "
              f"sudo systemctl restart bluetooth", flush=True)


def _handle_sigterm(signum, frame):
    # Not every "stop" arrives as Ctrl+C/SIGINT (Thonny's stop mechanism can
    # vary) — turn SIGTERM into the same KeyboardInterrupt path so shutdown
    # always cleans up the advertisement.
    raise KeyboardInterrupt()


signal.signal(signal.SIGTERM, _handle_sigterm)


def main():
    show_waiting("starting BLE...")
    start_ble_server()
    show_waiting("waiting...")

    last_data = time.monotonic()
    showing_no_signal = False

    try:
        while True:
            try:
                line = _incoming_lines.get(timeout=0.5)
            except queue.Empty:
                line = None

            if line is not None:
                if line.startswith("Q:"):
                    show_quote(line[2:].strip())
                    last_data = time.monotonic()
                    showing_no_signal = False
                else:
                    parsed = parse_line(line)
                    if parsed:
                        last_data = time.monotonic()
                        showing_no_signal = False
                        show_reading(*parsed)

            if not showing_no_signal and time.monotonic() - last_data > DATA_TIMEOUT_S:
                showing_no_signal = True
                show_waiting("no signal")
    except KeyboardInterrupt:
        print("Stopping...", flush=True)
    finally:
        stop_ble_server()


if __name__ == "__main__":
    main()
