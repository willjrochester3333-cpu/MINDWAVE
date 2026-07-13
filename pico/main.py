# main.py — runs on the Raspberry Pi Pico (or Pico W / Pico 2 W)
# ==========================================
# Reads "A:<attention>,M:<meditation>\n" lines from mindwave_pico_bridge.py
# (running on the laptop) and shows them on a 128x32 SSD1306 OLED, plus
# color-codes a WS2812B/NeoPixel strip by blending focus and calm: green =
# focused (high attention), red = calm (high meditation), amber/yellow =
# a mix of both. Also shows a motivational quote (word-wrapped) and
# buzzes a buzzer for a few seconds whenever it receives a "Q:<text>"
# line, which mindwave_pico_bridge.py sends each time its study coach
# prints one.
#
# CONNECTION: checks for config files on the Pico in this order —
#   1. ble_secrets.py  -> Bluetooth (BLE), see ble_secrets.py.example
#   2. wifi_secrets.py -> WiFi, see wifi_secrets.py.example
#   3. neither present -> USB serial (the original, simplest option)
# Both wireless modes need a Pico W or Pico 2 W (the plain Pico has no
# wireless hardware). Delete a config file to fall back to the next
# option down the list.
#
# Bluetooth mode advertises as "MindWave" (a standard BLE UART/Nordic
# UART Service peripheral) and mindwave_pico_bridge.py --link bluetooth
# scans for and connects to it — no password or IP address needed.
#
# WiFi mode shows its progress on the OLED at every stage — "wifi N/20"
# while joining your network, then its own IP address once joined, then
# "link N" while reaching the laptop and completing a handshake (proves
# it's really talking to mindwave_pico_bridge.py, not just any listener
# on that port), and finally "waiting..." once genuinely connected. If
# it's stuck, whatever's on screen says exactly which stage failed.
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
# powered on or plugged into USB. Add ble_secrets.py or wifi_secrets.py
# (filled in from the matching .example file) for wireless.

import sys
import select
import time
from machine import Pin, I2C
import neopixel
import ssd1306

try:
    import ble_secrets
    BLE_ENABLED = True
except ImportError:
    BLE_ENABLED = False

try:
    import wifi_secrets
    WIFI_ENABLED = True
except ImportError:
    WIFI_ENABLED = False

if BLE_ENABLED:
    WIFI_ENABLED = False  # ble_secrets.py takes priority if both are present

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


# ── Link setup: USB serial by default, WiFi/Bluetooth if configured ──────────
HANDSHAKE = b"MWHELLO"

# ── Bluetooth (BLE) — Nordic UART Service, a standard/widely-supported
# pattern for serial-like data over BLE. RX = laptop writes to us; TX is
# kept for compatibility with generic "BLE UART" phone apps (useful for
# testing this independently of mindwave_pico_bridge.py) but unused by
# our own protocol, since data only ever flows laptop -> Pico.
if BLE_ENABLED:
    import bluetooth
    from micropython import const

    _IRQ_CENTRAL_CONNECT = const(1)
    _IRQ_CENTRAL_DISCONNECT = const(2)
    _IRQ_GATTS_WRITE = const(3)

    _UART_SERVICE_UUID = bluetooth.UUID("6E400001-B5A3-F393-E0A9-E50E24DCCA9E")
    _UART_TX_UUID = bluetooth.UUID("6E400003-B5A3-F393-E0A9-E50E24DCCA9E")
    _UART_RX_UUID = bluetooth.UUID("6E400002-B5A3-F393-E0A9-E50E24DCCA9E")
    _UART_TX = (_UART_TX_UUID, bluetooth.FLAG_NOTIFY)
    _UART_RX = (_UART_RX_UUID, bluetooth.FLAG_WRITE)
    _UART_SERVICE = (_UART_SERVICE_UUID, (_UART_TX, _UART_RX))

    class BLEUARTPeripheral:
        def __init__(self, name):
            self._ble = bluetooth.BLE()
            self._ble.active(True)
            self._ble.irq(self._irq)
            ((self._tx_handle, self._rx_handle),) = self._ble.gatts_register_services((_UART_SERVICE,))
            self._connections = set()
            self._rx_buffer = b""
            self._payload = self._advertising_payload(name)
            self._advertise()

        def _irq(self, event, data):
            if event == _IRQ_CENTRAL_CONNECT:
                conn_handle, _, _ = data
                self._connections.add(conn_handle)
            elif event == _IRQ_CENTRAL_DISCONNECT:
                conn_handle, _, _ = data
                self._connections.discard(conn_handle)
                self._advertise()
            elif event == _IRQ_GATTS_WRITE:
                conn_handle, value_handle = data
                if value_handle == self._rx_handle:
                    self._rx_buffer += self._ble.gatts_read(self._rx_handle)

        def any(self):
            return len(self._rx_buffer) > 0

        def read(self):
            buf = self._rx_buffer
            self._rx_buffer = b""
            return buf

        def is_connected(self):
            return len(self._connections) > 0

        def _advertising_payload(self, name):
            payload = bytearray()

            def _append(adv_type, value):
                nonlocal payload
                payload += bytes((len(value) + 1, adv_type)) + value

            _append(0x01, bytes([0x06]))       # flags: general discoverable, BLE-only
            _append(0x09, name.encode())        # complete local name
            _append(0x07, bytes(_UART_SERVICE_UUID))  # complete 128-bit service UUID list
            return payload

        def _advertise(self, interval_us=500000):
            self._ble.gap_advertise(interval_us, adv_data=self._payload)

def connect_wifi():
    # Each step gets its own on-screen status, and the whole thing is
    # wrapped in a try/except: if any of these calls raise instead of
    # just being slow, the OLED would otherwise freeze on the last
    # message it drew with no visible sign anything went wrong.
    try:
        show_waiting("wifi init")
        import network
        wlan = network.WLAN(network.STA_IF)
        show_waiting("wifi active")
        wlan.active(True)
        show_waiting("wifi join")
        wlan.connect(wifi_secrets.WIFI_SSID, wifi_secrets.WIFI_PASSWORD)
        print("Connecting to WiFi:", wifi_secrets.WIFI_SSID)
        for attempt in range(20):
            if wlan.isconnected():
                ip = wlan.ifconfig()[0]
                print("WiFi connected, IP:", ip)
                show_waiting(ip)  # IPv4 is at most 15 chars, fits the 16-char-wide line
                time.sleep_ms(1500)
                return wlan
            show_waiting("wifi {}/20".format(attempt + 1))
            time.sleep(1)
        print("WiFi connection timed out")
        return None
    except Exception as e:
        print("WiFi error:", e)
        show_waiting("wifi err")
        time.sleep_ms(1500)
        return None


def connect_to_laptop():
    """Open a TCP connection to the laptop AND verify it's actually the
    bridge script on the other end with a handshake — a bare connect()
    can report success on some network stacks even when nothing valid
    is really listening, which would otherwise show a false "waiting"
    state forever with no data ever arriving."""
    import socket
    attempt = 0
    while True:
        attempt += 1
        show_waiting("link {}".format(attempt))
        s = None
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(5)
            s.connect((wifi_secrets.LAPTOP_HOST, wifi_secrets.LAPTOP_PORT))
            s.sendall(HANDSHAKE)
            reply = s.recv(16)
            if reply != HANDSHAKE:
                raise OSError("bad handshake reply: {}".format(reply))
            s.settimeout(None)
            print("Connected to laptop at", wifi_secrets.LAPTOP_HOST)
            return s
        except OSError as e:
            print("Couldn't reach the laptop:", e, "- retrying in 3s...")
            if s is not None:
                try:
                    s.close()
                except Exception:
                    pass
            time.sleep(3)


_rx_buf = b""

def read_available_lines():
    """Return a list of complete text lines ready right now, from
    whichever transport is active (Bluetooth, WiFi socket, or USB serial)."""
    global _rx_buf, link_sock, stdin_poll
    lines = []

    if BLE_ENABLED:
        data = ble.read()
        if not data:
            return lines
        _rx_buf += data
        while b"\n" in _rx_buf:
            raw, _rx_buf = _rx_buf.split(b"\n", 1)
            lines.append(raw.decode("utf-8", "ignore"))
        return lines

    if not WIFI_ENABLED:
        lines.append(sys.stdin.readline())
        return lines

    try:
        data = link_sock.recv(256)
    except OSError:
        data = None
    if not data:
        print("Lost connection to laptop — reconnecting...")
        try:
            link_sock.close()
        except Exception:
            pass
        link_sock = connect_to_laptop()
        stdin_poll = select.poll()
        stdin_poll.register(link_sock, select.POLLIN)
        return lines
    _rx_buf += data
    while b"\n" in _rx_buf:
        raw, _rx_buf = _rx_buf.split(b"\n", 1)
        lines.append(raw.decode("utf-8", "ignore"))
    return lines


def has_data_ready():
    """Block up to ~500ms, return True if there's something to read.
    BLE data arrives via an IRQ callback in the background regardless of
    what the main loop is doing, so there's no file descriptor to poll —
    just wait a moment and check whether anything showed up."""
    if BLE_ENABLED:
        time.sleep_ms(500)
        return ble.any()
    return stdin_poll.poll(500)


if BLE_ENABLED:
    show_waiting("ble init")
    ble = BLEUARTPeripheral(ble_secrets.BLE_NAME)
    show_waiting("ble adv")
    link_sock = None
elif WIFI_ENABLED:
    show_waiting("connecting...")
    wlan = connect_wifi()
    while wlan is None:
        time.sleep(5)
        wlan = connect_wifi()
    link_sock = connect_to_laptop()
else:
    link_sock = None

if not BLE_ENABLED:
    stdin_poll = select.poll()
    stdin_poll.register(link_sock if WIFI_ENABLED else sys.stdin, select.POLLIN)

show_waiting("waiting...")
last_data_ms = time.ticks_ms()

showing_no_signal = False

while True:
    if has_data_ready():
        for line in read_available_lines():
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
