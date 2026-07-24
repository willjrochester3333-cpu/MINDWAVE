#!/usr/bin/env bash
# mindwave-pi5-all-in-one.sh
# =============================================================================
# The ENTIRE Raspberry Pi 5 MindWave display setup in one file. No repo
# clone, no separate config/service files — this one script writes
# everything it needs and installs itself.
#
# Talks to the laptop over Bluetooth Low Energy (BLE) — the Pi 5 advertises
# itself as a Nordic UART Service peripheral, exactly like the Pico's BLE
# firmware does, so mindwave_pico_bridge.py --link bluetooth needs NO
# changes to talk to this instead of a Pico.
#
# STEP 1 — fill in the two values below:
#   TARGET_USER  -> your Pi username (the one you set in Raspberry Pi Imager)
#   BLE_NAME     -> must match --ble-name on the laptop (default "MindWave")
#
# STEP 2 — pick ONE of these two ways to run it:
#
#   (A) Zero-touch, no SSH at all:
#       Right after flashing the SD card with Raspberry Pi Imager (card
#       still plugged into your laptop, not ejected yet), open the boot
#       drive and find firstrun.sh (some Imager versions put it at
#       bootfs/firstrun.sh). Open it in a text editor, and paste this
#       ENTIRE file's contents in, on their own line(s), directly ABOVE
#       the line near the end that looks like:
#           rm -f /boot/firstrun.sh
#       Save, eject, wire the hardware (see WIRING below), insert the
#       card into the Pi, and power on. Give it a few minutes — it needs
#       to download packages before its automatic first reboot. After
#       that, it's running for good, every boot, no SSH ever needed.
#
#   (B) Over SSH, after a normal Imager flash+boot:
#       Copy this file to the Pi (e.g. `scp mindwave-pi5-all-in-one.sh
#       yourpi:~/`), SSH in, then:
#           sudo bash mindwave-pi5-all-in-one.sh
#       Reboot when it finishes: sudo reboot
#
# Either way, on your laptop, run:
#     py mindwave_pico_bridge.py --link bluetooth
# exactly as you would for the Pico — this speaks the identical protocol.
# No pairing step needed; the laptop just scans for the advertised name.
#
# WIRING (Raspberry Pi 5, 40-pin header)
# -----------------------------------------------------------------------
# OLED (SSD1306, I2C1):      SDA->GPIO2 (pin 3)  SCL->GPIO3 (pin 5)
#                            VCC->3V3   (pin 1)  GND->GND   (pin 9)
# WS2812B / NeoPixel strip:  DIN->GPIO10/SPI0 MOSI (pin 19), ~330 ohm
#                            resistor in series is good practice.
#                            Use an external 5V supply for more than a
#                            few LEDs (15 LEDs can draw ~900mA) — share
#                            its ground with the Pi's GND. The Pi's own
#                            5V pin (2/4) is fine only for a short strip.
# Buzzer:                    Signal->GPIO17 (pin 11)  GND->GND (pin 14)
# Bluetooth needs no wiring — it's the Pi 5's onboard radio.
#
# NOTE on the LED strip: the Pi 5's RP1 I/O chip broke the traditional
# PWM+DMA method most WS2812B libraries rely on, so this drives it over
# SPI instead (adafruit-circuitpython-neopixel-spi). This is the one
# part that couldn't be tested on real Pi 5 hardware ahead of time — if
# something doesn't work, it's the first place to look.
#
# NOTE on running as root: registering a BLE GATT peripheral through
# BlueZ's D-Bus API needs elevated permissions that a regular user
# doesn't have by default on Raspberry Pi OS, unlike I2C/SPI/GPIO. So,
# unlike the earlier WiFi version of this script, everything here
# (package installs and the service) runs as root rather than
# TARGET_USER — simpler and more reliable than chasing D-Bus/polkit
# policy edits on a personal project.
#
# It's safe to re-run this script — re-running just re-applies the same
# steps and overwrites its own files with the same content.
# =============================================================================

# ── Fill these in ───────────────────────────────────────────────────────
TARGET_USER="pi"         # <-- your Pi username (just used for the install path)
BLE_NAME="MindWave"      # <-- must match --ble-name on the laptop (default "MindWave")
# ─────────────────────────────────────────────────────────────────────────

LOG_FILE="/boot/mindwave-install.log"
[ -d /boot/firmware ] && LOG_FILE="/boot/firmware/mindwave-install.log"

(
  set -e

  INSTALL_DIR="/root/mindwave-pi5"

  echo "== MindWave Pi 5 all-in-one setup (Bluetooth) =="
  echo "Install dir: $INSTALL_DIR   BLE name: $BLE_NAME"

  echo "-- Waiting for network (needed to download packages) --"
  for i in $(seq 1 60); do
    ping -c1 -W2 8.8.8.8 >/dev/null 2>&1 && break
    sleep 2
  done

  echo "-- Enabling I2C and SPI --"
  raspi-config nonint do_i2c 0
  raspi-config nonint do_spi 0

  echo "-- Enabling Bluetooth --"
  rfkill unblock bluetooth || true
  apt-get update
  apt-get install -y bluez

  echo "-- Installing system packages --"
  apt-get install -y python3-pip python3-venv i2c-tools

  echo "-- Installing Python packages --"
  pip install --break-system-packages \
      luma.oled Pillow adafruit-blinka adafruit-circuitpython-neopixel-spi gpiozero rpi-lgpio bless

  systemctl enable --now bluetooth

  echo "-- Writing $INSTALL_DIR/main.py --"
  mkdir -p "$INSTALL_DIR"
  cat > "$INSTALL_DIR/main.py" <<'PYEOF'
#!/usr/bin/env python3
"""
main.py — runs on a Raspberry Pi 5, standing in for the Pico
================================================
Advertises itself over Bluetooth Low Energy (BLE) as a Nordic UART
Service peripheral — the exact same service the Pico's BLE firmware
uses — so mindwave_pico_bridge.py --link bluetooth needs NO changes
to talk to this instead of a Pico. Shows the same
"A:<attention>,M:<meditation>" readings on an SSD1306 OLED,
color-codes a WS2812B/NeoPixel strip by blending focus/calm the same
way the Pico version does, and buzzes a buzzer + shows motivational
quotes on "Q:<text>" lines.

Generated by mindwave-pi5-all-in-one.sh — BLE_NAME below was filled in
from that script's BLE_NAME setting.
"""

import queue
import signal
import threading
import time

try:
    import asyncio
    from bless import (
        BlessServer,
        GATTCharacteristicProperties,
        GATTAttributePermissions,
    )
except ImportError as e:
    raise SystemExit(f"bless not installed ({e}). pip install bless")

# ── Config ────────────────────────────────────────────────────────────────────
BLE_NAME     = "BLE_NAME_PLACEHOLDER"   # must match --ble-name on the laptop
UART_SERVICE_UUID = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
UART_RX_CHAR_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
NUM_LEDS     = 15     # how many LEDs are on the strip — match pico/main.py's value
BUZZER_PIN   = 17
BUZZ_S       = 0.2    # how long the buzzer sounds for
QUOTE_DISPLAY_S = 10.0  # how long a motivational quote stays on screen
OLED_I2C_ADDR = None   # None = auto-detect (tries 0x3C then 0x3D)
OLED_WIDTH   = 128
OLED_HEIGHT  = 32
DATA_TIMEOUT_S = 5.0   # seconds without a line before we show "no signal"
BRIGHTNESS   = 0.35    # 0..1, keeps the strip comfortable to look at
# ──────────────────────────────────────────────────────────────────────────────


# ── OLED (SSD1306 over I2C, via luma.oled) ────────────────────────────────────
def init_oled():
    """Bring up the OLED. Returns None (instead of crashing) if it can't be
    found — or if the luma packages aren't even installed — so the LED
    strip/buzzer still work while OLED wiring/setup is debugged."""
    try:
        from luma.core.interface.serial import i2c
        from luma.core.error import DeviceNotFoundError
        from luma.oled.device import ssd1306
    except ImportError as e:
        print(f"OLED libraries not installed ({e}) — continuing without it. "
              f"pip install luma.oled Pillow")
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


# ── Display helpers (mirrors pico/main.py's show_reading/show_quote/show_waiting) ─
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
    """Word-wrap text for the OLED. Pi 5's default PIL font is a little
    narrower than the Pico's fixed 8px font, so 20 chars/line fits."""
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
# loop below — running on the main thread, same as before — just reads from
# it, so all the OLED/LED/buzzer calls stay safely single-threaded.
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


# Set once the server's up, so a shutdown can cleanly unregister the
# advertisement instead of leaving BlueZ thinking it's still active — that
# stale state is the most common cause of a later "failed to register
# advertisement" error, e.g. after `systemctl restart mindwave-pi5.service`.
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
              f"start fails with 'failed to register advertisement', run: "
              f"sudo systemctl restart bluetooth", flush=True)


def _handle_sigterm(signum, frame):
    # systemctl stop/restart sends SIGTERM, not SIGINT — turn it into the
    # same KeyboardInterrupt path so shutdown always cleans up the
    # advertisement, whether stopped via systemd, Thonny, or Ctrl+C.
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
PYEOF

  sed -i "s/BLE_NAME_PLACEHOLDER/$BLE_NAME/" "$INSTALL_DIR/main.py"

  echo "-- Installing the auto-start service --"
  cat > /etc/systemd/system/mindwave-pi5.service <<EOF
[Unit]
Description=MindWave Pi 5 display (OLED + LED strip + buzzer, over BLE)
After=bluetooth.target
Wants=bluetooth.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 $INSTALL_DIR/main.py
WorkingDirectory=$INSTALL_DIR
User=root
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

  systemctl daemon-reload
  systemctl enable --now mindwave-pi5.service

  echo "== Done =="
  echo "Check it's running:   sudo systemctl status mindwave-pi5.service"
  echo "Watch live output:    journalctl -u mindwave-pi5.service -f"
) > "$LOG_FILE" 2>&1 || echo "MindWave install hit an error — see $LOG_FILE"
