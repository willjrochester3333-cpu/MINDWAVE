"""
mindwave_logger.py  ── clean EEG data logger
================================================
Connects directly to ThinkGear Connector (same as mindwave_dashboard.py,
NO OpenViBE needed) and hands the readings back to you in a new, simple
form: a tidy CSV file, one row per reading, that opens in Excel/Sheets
or loads into any analysis tool.

This is a separate, minimal app — mindwave_dashboard.py is untouched.
REQUIREMENTS: none beyond the Python standard library (tkinter ships
with most Python installs).
"""

import csv
import json
import os
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
TG_HOST     = "127.0.0.1"
TG_PORT     = 13854
try:
    _base_dir = Path(__file__).resolve().parent
except NameError:
    # __file__ isn't set when the script is run via exec()/a REPL instead of
    # `python mindwave_logger.py` — fall back to the current directory.
    _base_dir = Path.cwd()
OUTPUT_DIR  = _base_dir / "eeg_logs"
REFRESH_MS  = 200

FIELDNAMES = [
    "timestamp", "attention", "meditation", "signal_quality_pct",
    "delta", "theta", "low_alpha", "high_alpha",
    "low_beta", "high_beta", "low_gamma", "mid_gamma",
]

# ── Palette (kept consistent with mindwave_dashboard.py) ──────────────────────
BG, CARD, BORDER = "#0d1117", "#161b22", "#30363d"
ACCENT, GREEN, AMBER, PINK = "#378ADD", "#1D9E75", "#BA7517", "#D4537E"
TEXT, MUTED = "#e6edf3", "#8b949e"

# ── Shared state ────────────────────────────────────────────────────────────
state = dict(
    attention=0, meditation=0, signal_q=0,
    delta=0, theta=0, low_alpha=0, high_alpha=0,
    low_beta=0, high_beta=0, low_gamma=0, mid_gamma=0,
    connected=False, packet_count=0,
)
lock = threading.Lock()

recording = dict(on=False, writer=None, fh=None, path=None, rows=0)

# ── ThinkGear thread — same connection approach as the dashboard ─────────────
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
                    got_power = False
                    with lock:
                        if "poorSignalLevel" in data:
                            state["signal_q"] = max(0, 100 - int(data["poorSignalLevel"] / 2))
                        if "attention" in data and isinstance(data["attention"], (int, float)):
                            state["attention"] = int(data["attention"])
                        if "meditation" in data and isinstance(data["meditation"], (int, float)):
                            state["meditation"] = int(data["meditation"])
                        if "eegPower" in data:
                            p = data["eegPower"]
                            state["delta"]      = p.get("delta",     state["delta"])
                            state["theta"]      = p.get("theta",     state["theta"])
                            state["low_alpha"]  = p.get("lowAlpha",  state["low_alpha"])
                            state["high_alpha"] = p.get("highAlpha", state["high_alpha"])
                            state["low_beta"]   = p.get("lowBeta",   state["low_beta"])
                            state["high_beta"]  = p.get("highBeta",  state["high_beta"])
                            state["low_gamma"]  = p.get("lowGamma",  state["low_gamma"])
                            state["mid_gamma"]  = p.get("midGamma",  state["mid_gamma"])
                            state["packet_count"] += 1
                            got_power = True
                    # _write_row() takes the lock itself, so it must run after
                    # this block releases it — lock is a plain Lock, not reentrant.
                    if got_power:
                        _write_row()

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

def _write_row():
    """Append one reading to the open CSV file, if recording is on."""
    with lock:
        if not recording["on"] or recording["writer"] is None:
            return
        recording["writer"].writerow({
            "timestamp":           datetime.now().isoformat(timespec="seconds"),
            "attention":           state["attention"],
            "meditation":          state["meditation"],
            "signal_quality_pct":  state["signal_q"],
            "delta":               state["delta"],
            "theta":               state["theta"],
            "low_alpha":           state["low_alpha"],
            "high_alpha":          state["high_alpha"],
            "low_beta":            state["low_beta"],
            "high_beta":           state["high_beta"],
            "low_gamma":           state["low_gamma"],
            "mid_gamma":           state["mid_gamma"],
        })
        recording["fh"].flush()
        recording["rows"] += 1

# ── UI ─────────────────────────────────────────────────────────────────────
root = tk.Tk()
root.title("MindWave — Data Logger")
root.configure(bg=BG)
root.geometry("420x480")
root.resizable(False, False)

def card(parent):
    return tk.Frame(parent, bg=CARD, highlightbackground=BORDER, highlightthickness=1)

header = tk.Frame(root, bg=BG)
header.pack(fill="x", padx=20, pady=(20, 10))
tk.Label(header, text="MindWave EEG", font=("Segoe UI", 18, "bold"),
          bg=BG, fg=TEXT).pack(anchor="w")
tk.Label(header, text="live capture · saved as CSV", font=("Segoe UI", 10),
          bg=BG, fg=MUTED).pack(anchor="w")

status_row = tk.Frame(root, bg=BG)
status_row.pack(fill="x", padx=20, pady=(0, 14))
status_dot = tk.Label(status_row, text="●", font=("Segoe UI", 12), bg=BG, fg=AMBER)
status_dot.pack(side="left")
status_txt = tk.Label(status_row, text="waiting for headset…", font=("Segoe UI", 10),
                       bg=BG, fg=MUTED)
status_txt.pack(side="left", padx=(6, 0))

stats_row = tk.Frame(root, bg=BG)
stats_row.pack(fill="x", padx=20)

def make_stat(parent, label, color):
    c = card(parent)
    c.pack(side="left", expand=True, fill="both", padx=6, ipady=10)
    tk.Label(c, text=label, font=("Segoe UI", 9), bg=CARD, fg=MUTED).pack(pady=(8, 2))
    val = tk.Label(c, text="0", font=("Segoe UI", 22, "bold"), bg=CARD, fg=color)
    val.pack(pady=(0, 8))
    return val

t_att = make_stat(stats_row, "ATTENTION", ACCENT)
t_med = make_stat(stats_row, "MEDITATION", GREEN)
t_sig = make_stat(stats_row, "SIGNAL", TEXT)

bands_card = card(root)
bands_card.pack(fill="x", padx=20, pady=16, ipady=8)
tk.Label(bands_card, text="BAND POWER (raw)", font=("Segoe UI", 9),
          bg=CARD, fg=MUTED).pack(anchor="w", padx=12, pady=(6, 4))
band_txt = tk.Label(bands_card, text="—", font=("Consolas", 9), justify="left",
                     bg=CARD, fg=TEXT)
band_txt.pack(anchor="w", padx=12, pady=(0, 6))

record_card = card(root)
record_card.pack(fill="x", padx=20, pady=(0, 14), ipady=10)

rec_state = {"on": False}

def toggle_recording():
    if not rec_state["on"]:
        OUTPUT_DIR.mkdir(exist_ok=True)
        fname = f"eeg_log_{time.strftime('%Y%m%d_%H%M%S')}.csv"
        path = OUTPUT_DIR / fname
        fh = open(path, "w", newline="", encoding="utf-8")
        writer = csv.DictWriter(fh, fieldnames=FIELDNAMES)
        writer.writeheader()
        with lock:
            recording.update(on=True, writer=writer, fh=fh, path=path, rows=0)
        rec_state["on"] = True
        btn_record.config(text="■  STOP RECORDING", bg=PINK)
    else:
        with lock:
            recording["on"] = False
            fh = recording["fh"]
            path = recording["path"]
            rows = recording["rows"]
            recording["writer"] = None
            recording["fh"] = None
        if fh:
            fh.close()
        rec_state["on"] = False
        btn_record.config(text="●  START RECORDING", bg=GREEN)
        if path:
            file_txt.config(text=f"Saved {rows} rows → {path.name}")

btn_record = tk.Button(record_card, text="●  START RECORDING", font=("Segoe UI", 11, "bold"),
                        bg=GREEN, fg="#0d1117", activebackground=GREEN, bd=0,
                        relief="flat", command=toggle_recording, cursor="hand2")
btn_record.pack(fill="x", padx=12, pady=(4, 8))

file_txt = tk.Label(record_card, text=f"Files are saved to {OUTPUT_DIR.name}/",
                     font=("Segoe UI", 8), bg=CARD, fg=MUTED, wraplength=360, justify="left")
file_txt.pack(anchor="w", padx=12)

def open_output_folder():
    OUTPUT_DIR.mkdir(exist_ok=True)
    if sys.platform.startswith("win"):
        os.startfile(OUTPUT_DIR)
    elif sys.platform == "darwin":
        subprocess.run(["open", str(OUTPUT_DIR)])
    else:
        subprocess.run(["xdg-open", str(OUTPUT_DIR)])

btn_folder = tk.Button(record_card, text="Open folder", font=("Segoe UI", 8),
                        bg=CARD, fg=MUTED, bd=0, relief="flat",
                        activebackground=CARD, command=open_output_folder, cursor="hand2")
btn_folder.pack(anchor="e", padx=12, pady=(0, 4))

footer = tk.Label(root, text="0 packets received", font=("Segoe UI", 8), bg=BG, fg=MUTED)
footer.pack(side="bottom", pady=(0, 12))

# ── Refresh loop ──────────────────────────────────────────────────────────────
def refresh():
    with lock:
        s = dict(state)
        rows = recording["rows"]

    t_att.config(text=str(s["attention"]))
    t_med.config(text=str(s["meditation"]))
    t_sig.config(text=f"{s['signal_q']}%")

    band_txt.config(text=(
        f"delta {s['delta']:<8} theta {s['theta']:<8} lowA {s['low_alpha']:<8} hiA {s['high_alpha']}\n"
        f"lowB  {s['low_beta']:<8} hiB   {s['high_beta']:<8} lowG {s['low_gamma']:<8} midG {s['mid_gamma']}"
    ))

    if s["connected"]:
        status_dot.config(fg=GREEN)
        status_txt.config(text="connected — live")
    else:
        status_dot.config(fg=AMBER)
        status_txt.config(text="waiting for headset…")

    footer_text = f"{s['packet_count']:,} packets received"
    if rec_state["on"]:
        footer_text += f"   ·   recording ({rows} rows)"
    footer.config(text=footer_text)

    root.after(REFRESH_MS, refresh)

# ── Start ──────────────────────────────────────────────────────────────────────
th = threading.Thread(target=thinkgear_thread, daemon=True)
th.start()
print("Waiting for ThinkGear Connector on port 13854...")
print("Do NOT open OpenViBE — it blocks the connection.")
print("Put headset on and wait for blue light.\n")

refresh()
root.mainloop()
