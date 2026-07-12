"""
mindwave_pico_bridge.py  ── streams live focus/calm to a Raspberry Pi Pico
================================================
Forwards attention + meditation over a USB cable to a Raspberry Pi Pico
running pico/main.py, which shows the numbers on a 128x32 SSD1306 OLED
and color-codes a WS2812B/NeoPixel strip by focus (attention) level:
red = low, amber = mid, green = high. pico/main.py never needs to
change — both data sources below feed it the same "A:xx,M:xx" format.

Two data sources, chosen with --source:

  thinkgear (default)
      Connects to ThinkGear Connector directly (same approach as
      mindwave_dashboard.py / mindwave_logger.py, NO OpenViBE needed)
      and uses NeuroSky's real eSense attention/meditation values.

  openvibe
      Connects to an OpenViBE Acquisition Server via Lab Streaming
      Layer (LSL) instead. OpenViBE doesn't calculate eSense
      attention/meditation itself — that's NeuroSky's proprietary
      algorithm — so this mode computes an approximate focus/calm
      proxy locally from raw EEG band power (beta dominance ~ focus,
      alpha dominance ~ calm). It's a reasonable stand-in, not the
      real thing.
      In OpenViBE, enable an LSL export of the EEG stream — either
      Acquisition Server's own LSL output option if your version has
      one, or a Designer scenario with an "LSL Export" box.

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

# ── OpenViBE thread — pulls raw EEG via LSL, estimates focus/calm locally ────
def openvibe_thread():
    try:
        import numpy as np
        from scipy.signal import welch
        from pylsl import resolve_byprop, StreamInlet
    except ImportError as e:
        print(f"--source openvibe needs extra packages: {e}")
        print("Install them with: pip install pylsl numpy scipy")
        return

    def band_power(freqs, power, lo, hi):
        mask = (freqs >= lo) & (freqs < hi)
        return float(power[mask].sum()) if mask.any() else 0.0

    while True:
        print("Looking for an OpenViBE EEG stream via LSL...")
        streams = resolve_byprop("type", "EEG", timeout=5)
        if not streams:
            print("No LSL EEG stream found. In OpenViBE, make sure Acquisition Server is "
                  "running with an LSL export enabled (or a Designer scenario with an "
                  "'LSL Export' box) — retrying in 5s...")
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
    parser = argparse.ArgumentParser(description="Stream live focus/calm to a Raspberry Pi Pico.")
    parser.add_argument("--source", choices=["thinkgear", "openvibe"], default="thinkgear",
                         help="Where to get EEG data from (default: thinkgear)")
    args = parser.parse_args()

    if args.source == "thinkgear":
        th = threading.Thread(target=thinkgear_thread, daemon=True)
        th.start()
        print("Waiting for ThinkGear Connector on port 13854...")
        print("Do NOT open OpenViBE — it blocks the connection.")
        print("Put headset on and wait for blue light.\n")
    else:
        th = threading.Thread(target=openvibe_thread, daemon=True)
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
