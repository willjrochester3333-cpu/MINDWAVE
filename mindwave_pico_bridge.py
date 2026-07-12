"""
mindwave_pico_bridge.py  ── streams live focus/calm to a Raspberry Pi Pico
================================================
Connects to ThinkGear Connector (same approach as mindwave_dashboard.py
and mindwave_logger.py, NO OpenViBE needed) and forwards attention +
meditation over a USB cable to a Raspberry Pi Pico running pico/main.py,
which shows the numbers on a 128x64 SSD1306 OLED and color-codes a
WS2812B/NeoPixel strip by focus (attention) level: red = low, amber =
mid, green = high.

This is a separate, minimal app — mindwave_dashboard.py and
mindwave_logger.py are untouched.

REQUIREMENTS: pyserial  (pip install pyserial)
"""

import json
import socket
import threading
import time

import serial
import serial.tools.list_ports

# ── Config ────────────────────────────────────────────────────────────────────
TG_HOST    = "127.0.0.1"
TG_PORT    = 13854
BAUD_RATE  = 115200
SEND_EVERY = 0.5   # seconds between updates sent to the Pico
PICO_VID   = 0x2E8A  # Raspberry Pi Foundation's USB vendor ID
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
    th = threading.Thread(target=thinkgear_thread, daemon=True)
    th.start()
    print("Waiting for ThinkGear Connector on port 13854...")
    print("Do NOT open OpenViBE — it blocks the connection.")
    print("Put headset on and wait for blue light.\n")

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
