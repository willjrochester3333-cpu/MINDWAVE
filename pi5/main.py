#!/usr/bin/env python3
"""
main.py — runs on a Raspberry Pi 5, standing in for the Pico
================================================
Connects to mindwave_pico_bridge.py --link wifi (running on your
laptop) over WiFi, and shows the same "A:<attention>,M:<meditation>"
readings on an SSD1306 OLED, color-codes a WS2812B/NeoPixel strip by
blending focus/calm the same way the Pico version does, and buzzes a
buzzer + shows motivational quotes on "Q:<text>" lines.

Uses the exact same wire protocol and handshake as pico/main.py's WiFi
mode, so mindwave_pico_bridge.py needs NO changes to talk to this
instead of a Pico — just run it with --link wifi as usual.

WIRING (Raspberry Pi 5, 40-pin header, fresh assignment — change the
CONFIG constants below if you wire it differently)
------------------------------------------------------------------
OLED (SSD1306, I2C1 — the Pi's standard/dedicated I2C bus):
    SDA -> GPIO2  (physical pin 3)
    SCL -> GPIO3  (physical pin 5)
    VCC -> 3V3    (physical pin 1)
    GND -> GND    (physical pin 9)

WS2812B / NeoPixel strip (driven over SPI0 — see NOTE below):
    DIN -> GPIO10 / SPI0 MOSI (physical pin 19) — a ~330 ohm resistor
           in series is good practice
    5V  -> an external 5V supply for anything more than a few LEDs;
           the Pi's 5V pin (physical pin 2/4) is fine for a short strip
    GND -> GND (shared with the Pi, and with the external supply if used)

Buzzer:
    Signal -> GPIO17 (physical pin 11)
    GND    -> GND

NOTE on the LED strip: the Pi 5's newer RP1 I/O chip broke the
traditional PWM+DMA method most Raspberry Pi WS2812B libraries
(rpi_ws281x and anything built on it) rely on. This script drives the
strip over SPI instead (via adafruit-circuitpython-neopixel-spi),
which sidesteps that problem — SPI's own clock does the precise bit
timing WS2812B needs instead of PWM/DMA. This is the least-tested part
of this file; if it doesn't work, that's the first place to look.

SETUP
-----
Easiest path — run the installer script (does everything below for you:
enables I2C/SPI, installs dependencies, sets up config.py, installs the
auto-start service):
    cd pi5
    chmod +x setup.sh
    ./setup.sh
    nano config.py    # fill in your laptop's IP address
    sudo reboot

That's it — main.py will now run automatically on every boot. See
setup.sh and mindwave-pi5.service in this folder for what it does.

Manual path, if you'd rather do it by hand:
    sudo raspi-config
      -> Interface Options -> I2C -> Enable
      -> Interface Options -> SPI -> Enable
    sudo reboot
    pip install luma.oled Pillow adafruit-blinka adafruit-circuitpython-neopixel-spi gpiozero rpi-lgpio
    cp config.py.example config.py
    nano config.py    # fill in your laptop's local IP address — check
                       # with `ip addr` / `hostname -I` on the Pi, and
                       # your laptop's own IP with `ipconfig` (Windows)
    python3 main.py

On your laptop, run mindwave_pico_bridge.py --link wifi exactly as you
would for the Pico — this script speaks the identical protocol.
"""

import socket
import time

try:
    import config
except ImportError:
    raise SystemExit(
        "config.py not found. Copy config.py.example to config.py in this "
        "folder and fill in your laptop's local IP address."
    )

# ── Config ────────────────────────────────────────────────────────────────────
NUM_LEDS     = 15     # how many LEDs are on the strip — match pico/main.py's value
BUZZER_PIN   = 17
BUZZ_S       = 0.2    # how long the buzzer sounds for
QUOTE_DISPLAY_S = 10.0  # how long a motivational quote stays on screen
OLED_I2C_ADDR = None   # None = auto-detect (tries 0x3C then 0x3D)
OLED_WIDTH   = 128
OLED_HEIGHT  = 32
DATA_TIMEOUT_S = 5.0   # seconds without a line before we show "no signal"
BRIGHTNESS   = 0.35    # 0..1, keeps the strip comfortable to look at
HANDSHAKE    = b"MWHELLO"
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


# ── Link: connects to mindwave_pico_bridge.py --link wifi, same protocol ─────
def connect_to_laptop():
    """Open a TCP connection to the laptop AND verify it's actually
    mindwave_pico_bridge.py with the same handshake pico/main.py uses."""
    attempt = 0
    while True:
        attempt += 1
        show_waiting(f"link {attempt}")
        s = None
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(5)
            s.connect((config.LAPTOP_HOST, config.LAPTOP_PORT))
            s.sendall(HANDSHAKE)
            reply = s.recv(16)
            if reply != HANDSHAKE:
                raise OSError(f"bad handshake reply: {reply!r}")
            s.settimeout(None)
            print(f"Connected to laptop at {config.LAPTOP_HOST}")
            return s
        except OSError as e:
            print(f"Couldn't reach the laptop: {e} — retrying in 3s...")
            if s is not None:
                try:
                    s.close()
                except Exception:
                    pass
            time.sleep(3)


def read_available_lines(link_sock, rx_buf):
    """Return (lines, updated_rx_buf, updated_link_sock). A recv() timeout
    just means nothing arrived in this ~500ms tick — perfectly normal, same
    as poll(500) returning nothing on the Pico — and must NOT be treated as
    a lost connection; only a real error or the peer cleanly closing
    (recv() returning b"") means we should reconnect."""
    try:
        data = link_sock.recv(256)
    except socket.timeout:
        return [], rx_buf, link_sock
    except OSError:
        data = None

    if not data:
        print("Lost connection to laptop — reconnecting...")
        try:
            link_sock.close()
        except Exception:
            pass
        new_sock = connect_to_laptop()
        new_sock.settimeout(0.5)
        return [], b"", new_sock

    rx_buf += data
    lines = []
    while b"\n" in rx_buf:
        raw, rx_buf = rx_buf.split(b"\n", 1)
        lines.append(raw.decode("utf-8", "ignore"))
    return lines, rx_buf, link_sock


def main():
    show_waiting("connecting...")
    link_sock = connect_to_laptop()
    link_sock.settimeout(0.5)  # so recv() doubles as our idle tick, like poll(500) on the Pico

    show_waiting("waiting...")
    rx_buf = b""
    last_data = time.monotonic()
    showing_no_signal = False

    while True:
        lines, rx_buf, link_sock = read_available_lines(link_sock, rx_buf)

        for line in lines:
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


if __name__ == "__main__":
    main()
