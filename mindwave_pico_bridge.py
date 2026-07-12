"""
mindwave_pico_bridge.py  ── streams live focus/calm to a Raspberry Pi Pico
================================================
Forwards attention + meditation over a USB cable to a Raspberry Pi Pico
running pico/main.py, which shows the numbers on a 128x32 SSD1306 OLED
and color-codes a WS2812B/NeoPixel strip by focus (attention) level:
red = low, amber = mid, green = high. pico/main.py never needs to
change — both data sources below feed it the same "A:xx,M:xx" format.

Three data sources, chosen with --source (openvibe is the default —
just run the script with no flags to use OpenViBE):

  openvibe (default)
      Connects to an OpenViBE Acquisition Server via Lab Streaming
      Layer (LSL). OpenViBE doesn't calculate eSense attention/
      meditation itself — that's NeuroSky's proprietary algorithm —
      so this mode computes an approximate focus/calm proxy locally
      from raw EEG band power (beta dominance ~ focus, alpha
      dominance ~ calm). It's a reasonable stand-in, not the real
      thing.
      IMPORTANT: Acquisition Server's own "Connection port: 1024" is
      NOT an LSL stream — it's OpenViBE's internal protocol for
      talking to Designer. To get an LSL stream out of it, open
      OpenViBE Designer and run (Play) a scenario with an "Acquisition
      Client" box (reading from Acquisition Server) feeding an "LSL
      Export" box. That box's settings are just a stream name (default
      "openvibeSignal") and a marker stream name — no "type" field.
      This script looks for a stream named "openvibeSignal" by
      default; pass --lsl-stream-name if you changed it, or it'll
      fall back to whatever LSL stream it can find.

  thinkgear
      Connects to ThinkGear Connector directly (same approach as
      mindwave_dashboard.py / mindwave_logger.py, NO OpenViBE needed)
      and uses NeuroSky's real eSense attention/meditation values.

  serial
      Connects straight to the headset's own Bluetooth serial port and
      parses NeuroSky's raw ThinkGear packet protocol by hand (sync
      bytes, checksums, payload codes) — no ThinkGear Connector, no
      OpenViBE, nothing else needs to be running. Also gives real
      eSense attention/meditation values, same as thinkgear mode.
      Pair the headset over Bluetooth first (PIN 0000) so it shows up
      as a serial/COM port — on Windows, Classic Bluetooth devices
      sometimes need a COM port added manually via Control Panel >
      Devices and Printers > (right-click the device) > Bluetooth
      Settings > COM Ports tab > Add, if one didn't appear on its own.

This is a separate, minimal app — mindwave_dashboard.py and
mindwave_logger.py are untouched.

REQUIREMENTS:
  pip install pyserial                 (always)
  pip install pylsl numpy scipy        (only for --source openvibe)
"""

import argparse
import json
import socket
import threading
import time
from collections import deque

import serial
import serial.tools.list_ports

# ── Config ────────────────────────────────────────────────────────────────────
TG_HOST     = "127.0.0.1"
TG_PORT     = 13854
BAUD_RATE   = 115200
SEND_EVERY  = 0.5   # seconds between updates sent to the Pico
PICO_VID    = 0x2E8A  # Raspberry Pi Foundation's USB vendor ID
LSL_WINDOW_SECONDS = 2.0   # how much raw EEG history to use for band power estimation
LSL_STREAM_NAME = "openvibeSignal"  # OpenViBE's LSL Export box default "Signal stream" name
# ──────────────────────────────────────────────────────────────────────────────

state = dict(attention=0, meditation=0, connected=False)
lock = threading.Lock()

# ── ThinkGear thread — same connection approach as the other apps ────────────
def thinkgear_thread():
    handshake = json.dumps({"enableRawOutput": False, "format": "Json"}) + "\n"
    while True:
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(10)
            sock.connect((TG_HOST, TG_PORT))
            sock.sendall(handshake.encode("utf-8"))
            sock.settimeout(None)
            print("ThinkGear Connector connected!")
            with lock:
                state["connected"] = True
            buf = ""
            while True:
                chunk = sock.recv(4096).decode("utf-8", errors="replace")
                if not chunk:
                    break
                buf += chunk
                lines_ready = []
                while "\r" in buf or "\n" in buf:
                    for d in ("\r\n", "\r", "\n"):
                        if d in buf:
                            line, buf = buf.split(d, 1)
                            break
                    line = line.strip()
                    if line:
                        lines_ready.append(line)

                for line in lines_ready:
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    with lock:
                        if "attention" in data and isinstance(data["attention"], (int, float)):
                            state["attention"] = int(data["attention"])
                        if "meditation" in data and isinstance(data["meditation"], (int, float)):
                            state["meditation"] = int(data["meditation"])

        except (ConnectionRefusedError, OSError):
            print("ThinkGear Connector not found — retrying in 5s...")
            with lock:
                state["connected"] = False
            time.sleep(5)
        except Exception as e:
            print(f"ThinkGear error: {e} — retrying in 3s...")
            time.sleep(3)
        finally:
            if sock is not None:
                try: sock.close()
                except Exception: pass

# ── Direct-serial thread — talks straight to the headset over Bluetooth ──────
# Hand-parses NeuroSky's raw ThinkGear serial packet protocol, bypassing
# ThinkGear Connector and OpenViBE entirely. Gives the real eSense
# attention/meditation values, just like thinkgear mode does.
SYNC_BYTE       = 0xAA
EXCODE          = 0x55
CODE_ATTENTION  = 0x04
CODE_MEDITATION = 0x05
HEADSET_BAUD    = 57600

class ThinkGearSerialParser:
    """Stateful byte-by-byte parser for the ThinkGear serial protocol."""

    def __init__(self):
        self._buf = bytearray()
        self._state = "sync1"
        self._plen = 0

    def feed(self, byte):
        """Feed one byte in. Returns (attention, meditation) — either may be
        None — whenever a complete, checksum-valid packet is parsed."""
        if self._state == "sync1":
            if byte == SYNC_BYTE:
                self._state = "sync2"
        elif self._state == "sync2":
            self._state = "sync1" if byte != SYNC_BYTE else "plen"
        elif self._state == "plen":
            if byte == SYNC_BYTE:      # still in sync sequence — stay here
                return None
            if byte > 170:             # invalid length
                self._state = "sync1"
                return None
            self._plen = byte
            self._buf = bytearray()
            self._state = "payload"
        elif self._state == "payload":
            self._buf.append(byte)
            if len(self._buf) == self._plen:
                self._state = "checksum"
        elif self._state == "checksum":
            expected = (~sum(self._buf)) & 0xFF
            self._state = "sync1"
            if byte == expected:
                return self._parse_payload(bytes(self._buf))
        return None

    def _parse_payload(self, payload):
        attention = meditation = None
        i = 0
        while i < len(payload):
            while i < len(payload) and payload[i] == EXCODE:
                i += 1
            if i >= len(payload):
                break
            code = payload[i]; i += 1
            if code >= 0x80:            # multi-byte row — skip its value
                if i >= len(payload):
                    break
                vlen = payload[i]; i += 1
                i += vlen
            else:                        # single-byte row
                if i >= len(payload):
                    break
                val = payload[i]; i += 1
                if code == CODE_ATTENTION:
                    attention = val
                elif code == CODE_MEDITATION:
                    meditation = val
        if attention is None and meditation is None:
            return None
        return attention, meditation

def find_headset_port():
    """Auto-detect the MindWave's own Bluetooth serial port (not the Pico's)."""
    keywords = ("mindwave", "rfcomm", "bluetooth")
    for p in serial.tools.list_ports.comports():
        desc = (p.description or "").lower()
        name = (p.name or "").lower()
        if any(k in desc or k in name for k in keywords):
            return p.device
    return None

def direct_serial_thread(headset_port=None):
    parser = ThinkGearSerialParser()
    while True:
        port = headset_port or find_headset_port()
        if not port:
            print("Couldn't auto-detect the headset's Bluetooth serial port.")
            print("Available serial ports:")
            for p in serial.tools.list_ports.comports():
                print(f"  {p.device}  ({p.description})")
            port = input("Enter the headset's COM port (e.g. COM6): ").strip()

        try:
            ser = serial.Serial(port, baudrate=HEADSET_BAUD, timeout=2)
            time.sleep(0.5)
            print(f"Connected to headset on {port}")
            with lock:
                state["connected"] = True
            while True:
                chunk = ser.read(32)
                for b in chunk:
                    result = parser.feed(b)
                    if result:
                        att, med = result
                        with lock:
                            if att is not None:
                                state["attention"] = att
                            if med is not None:
                                state["meditation"] = med
        except serial.SerialException as e:
            print(f"Headset serial error: {e} — retrying in 5s...")
            with lock:
                state["connected"] = False
            headset_port = None  # re-scan in case the port changed
            time.sleep(5)
        except Exception as e:
            print(f"Unexpected error reading headset: {e} — retrying in 3s...")
            time.sleep(3)

# ── OpenViBE thread — pulls raw EEG via LSL, estimates focus/calm locally ────
def openvibe_thread(stream_name=None):
    try:
        import numpy as np
        from scipy.signal import welch
        from pylsl import resolve_byprop, resolve_streams, StreamInlet
    except ImportError as e:
        print(f"--source openvibe needs extra packages: {e}")
        print("Install them with: pip install pylsl numpy scipy")
        return

    stream_name = stream_name or LSL_STREAM_NAME

    def band_power(freqs, power, lo, hi):
        mask = (freqs >= lo) & (freqs < hi)
        return float(power[mask].sum()) if mask.any() else 0.0

    while True:
        print(f"Looking for an LSL stream named '{stream_name}'...")
        streams = resolve_byprop("name", stream_name, timeout=5)
        if not streams:
            # fall back to whatever LSL stream is available, in case the
            # OpenViBE scenario used a different "Signal stream" name
            streams = resolve_streams(wait_time=2.0)
            if streams:
                print(f"No stream named '{stream_name}' — using '{streams[0].name()}' instead.")
        if not streams:
            print("No LSL stream found at all. In OpenViBE Designer, make sure a scenario "
                  "with an 'LSL Export' box is running (Play), not just Acquisition Server "
                  "— retrying in 5s...")
            with lock:
                state["connected"] = False
            time.sleep(5)
            continue

        inlet = StreamInlet(streams[0])
        info = inlet.info()
        fs = info.nominal_srate() or 128.0
        print(f"Connected to LSL stream '{info.name()}' "
              f"({info.channel_count()} channel(s), {fs:.0f} Hz)")
        with lock:
            state["connected"] = True

        window_len = max(int(fs * LSL_WINDOW_SECONDS), 32)
        buf = deque(maxlen=window_len)

        try:
            while True:
                sample, _timestamp = inlet.pull_sample(timeout=1.0)
                if sample is None:
                    continue
                buf.append(sample[0])  # channel 0 = raw EEG
                if len(buf) < window_len:
                    continue

                freqs, power = welch(np.array(buf), fs=fs, nperseg=min(256, len(buf)))
                theta = band_power(freqs, power, 4, 8)
                alpha = band_power(freqs, power, 8, 12)
                beta  = band_power(freqs, power, 13, 30)
                total = theta + alpha + beta + 1e-9

                with lock:
                    state["attention"]  = max(0, min(100, int(100 * beta  / total)))
                    state["meditation"] = max(0, min(100, int(100 * alpha / total)))
        except Exception as e:
            print(f"OpenViBE/LSL stream error: {e} — reconnecting...")
            with lock:
                state["connected"] = False
            time.sleep(3)

# ── Pico serial link ──────────────────────────────────────────────────────────
def find_pico_port():
    """Look for a USB serial port that looks like a Raspberry Pi Pico."""
    for p in serial.tools.list_ports.comports():
        if p.vid == PICO_VID:
            return p.device
    return None

def connect_serial(preferred_port=None):
    """Open the Pico's serial port, prompting for one if it can't be found."""
    while True:
        port = preferred_port or find_pico_port()
        if not port:
            print("Couldn't auto-detect a Pico. Available serial ports:")
            for p in serial.tools.list_ports.comports():
                print(f"  {p.device}  ({p.description})")
            port = input("Enter the Pico's COM port (e.g. COM5): ").strip()
        try:
            ser = serial.Serial(port, BAUD_RATE, timeout=1)
            print(f"Connected to Pico on {port}")
            return ser, port
        except serial.SerialException as e:
            print(f"Couldn't open {port}: {e} — retrying in 3s...")
            preferred_port = None
            time.sleep(3)

def main():
    arg_parser = argparse.ArgumentParser(description="Stream live focus/calm to a Raspberry Pi Pico.")
    arg_parser.add_argument("--source", choices=["thinkgear", "serial", "openvibe"], default="openvibe",
                             help="Where to get EEG data from (default: openvibe)")
    arg_parser.add_argument("--headset-port", default=None,
                             help="Headset's Bluetooth COM port for --source serial "
                                  "(auto-detected if omitted)")
    arg_parser.add_argument("--lsl-stream-name", default=LSL_STREAM_NAME,
                             help=f"LSL stream name for --source openvibe "
                                  f"(default: '{LSL_STREAM_NAME}', matching OpenViBE's "
                                  f"LSL Export box default)")
    args = arg_parser.parse_args()

    if args.source == "thinkgear":
        th = threading.Thread(target=thinkgear_thread, daemon=True)
        th.start()
        print("Waiting for ThinkGear Connector on port 13854...")
        print("Do NOT open OpenViBE — it blocks the connection.")
        print("Put headset on and wait for blue light.\n")
    elif args.source == "serial":
        th = threading.Thread(target=direct_serial_thread, args=(args.headset_port,), daemon=True)
        th.start()
        print("Connecting straight to the headset over Bluetooth serial.")
        print("Do NOT run ThinkGear Connector or OpenViBE at the same time — "
              "they'll grab the Bluetooth port first.\n")
    else:
        th = threading.Thread(target=openvibe_thread, args=(args.lsl_stream_name,), daemon=True)
        th.start()
        print("Using OpenViBE via LSL. Focus/calm are an approximate proxy computed from")
        print("raw EEG band power, not NeuroSky's real eSense attention/meditation.\n")

    ser, port = connect_serial()

    try:
        while True:
            with lock:
                att, med = state["attention"], state["meditation"]
            try:
                ser.write(f"A:{att},M:{med}\n".encode("ascii"))
                print(f"\r→ Pico  focus={att:3d}  calm={med:3d}   ", end="", flush=True)
            except serial.SerialException as e:
                print(f"\nLost connection to Pico ({e}) — reconnecting...")
                ser, port = connect_serial(port)
            time.sleep(SEND_EVERY)
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        ser.close()

if __name__ == "__main__":
    main()
