#!/usr/bin/env python3
"""
dashboard_server.py — runs on the Raspberry Pi 5.

Hosts a live EEG dashboard as a website. Any device on the same
network can open http://<pi-ip>:8080 in a browser and see it; the
Pi's own touch display shows the same page in a kiosk browser.

Pressing "Connect" on the page connects THIS SERVER directly to
whatever OpenViBE LSL stream is broadcasting on the network — no
laptop-side bridge script involved. OpenViBE Acquisition Server +
Designer (with an LSL Export box) needs to be running on the laptop,
on the same network as the Pi, before you press Connect.

SETUP — see dashboard_all_in_one.sh in this folder for the one-file
installer that installs this, sets it to auto-start, and sets up the
touch display kiosk. Run this file directly only for manual testing:
    pip install flask pylsl numpy scipy
    python3 dashboard_server.py
"""

import json
import threading
import time
from collections import deque

from flask import Flask, Response, jsonify

PORT = 8080
LSL_STREAM_NAME = "openvibeSignal"  # OpenViBE's LSL Export box default "Signal stream" name
LSL_WINDOW_SECONDS = 2.0

app = Flask(__name__)

state_lock = threading.Lock()
state = {
    "connected": False,
    "attention": 0,
    "meditation": 0,
    "status": "Idle — press Connect to start.",
}
worker_started = False


def band_power(freqs, power, lo, hi):
    mask = (freqs >= lo) & (freqs < hi)
    return float(power[mask].sum()) if mask.any() else 0.0


def set_status(msg, connected=None):
    """Update the dashboard's status line AND print it (with flush=True so
    it shows up immediately in `journalctl -u mindwave-dashboard.service -f`
    instead of only being visible on the web page)."""
    with state_lock:
        state["status"] = msg
        if connected is not None:
            state["connected"] = connected
    print(msg, flush=True)


def lsl_worker():
    try:
        import numpy as np
        from scipy.signal import welch
        from pylsl import resolve_byprop, resolve_streams, StreamInlet
    except ImportError as e:
        set_status(f"Missing package: {e}. Install with: pip install pylsl numpy scipy")
        return

    while True:
        set_status(f"Looking for an LSL stream named '{LSL_STREAM_NAME}'...")
        streams = resolve_byprop("name", LSL_STREAM_NAME, timeout=5)
        if not streams:
            # fall back to whatever LSL stream is available, in case the
            # OpenViBE scenario used a different "Signal stream" name
            streams = resolve_streams(wait_time=2.0)
            if streams:
                set_status(f"No stream named '{LSL_STREAM_NAME}' — using '{streams[0].name()}' instead.")
        if not streams:
            set_status(
                "No LSL stream found. Make sure OpenViBE Designer is running a scenario with "
                "an LSL Export box (Play), not just Acquisition Server. Retrying...",
                connected=False,
            )
            time.sleep(5)
            continue

        inlet = StreamInlet(streams[0])
        info = inlet.info()
        fs = info.nominal_srate() or 128.0
        set_status(f"Connected to '{info.name()}' ({info.channel_count()} ch, {fs:.0f} Hz)", connected=True)

        window_len = max(int(fs * LSL_WINDOW_SECONDS), 32)
        buf = deque(maxlen=window_len)

        try:
            while True:
                sample, _ts = inlet.pull_sample(timeout=1.0)
                if sample is None:
                    continue
                buf.append(sample[0])  # channel 0 = raw EEG
                if len(buf) < window_len:
                    continue

                freqs, power = welch(np.array(buf), fs=fs, nperseg=min(256, len(buf)))
                theta = band_power(freqs, power, 4, 8)
                alpha = band_power(freqs, power, 8, 12)
                beta = band_power(freqs, power, 13, 30)
                total = theta + alpha + beta + 1e-9

                with state_lock:
                    state["attention"] = max(0, min(100, int(100 * beta / total)))
                    state["meditation"] = max(0, min(100, int(100 * alpha / total)))
        except Exception as e:
            set_status(f"Stream error ({e}) — reconnecting...", connected=False)
            time.sleep(3)


@app.route("/")
def index():
    return DASHBOARD_HTML


@app.route("/connect", methods=["POST"])
def connect():
    global worker_started
    with state_lock:
        already = worker_started
        worker_started = True
    if not already:
        threading.Thread(target=lsl_worker, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/events")
def events():
    def gen():
        last = None
        while True:
            with state_lock:
                snapshot = dict(state)
            payload = json.dumps(snapshot)
            if payload != last:
                yield f"data: {payload}\n\n"
                last = payload
            else:
                yield ": keep-alive\n\n"
            time.sleep(0.3)
    return Response(gen(), mimetype="text/event-stream")


DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<title>MindWave Dashboard</title>
<style>
  :root {
    --bg: #0d1117; --surface: #161b22; --surface-2: #1c232c; --border: #30363d;
    --text: #e6edf3; --muted: #8b949e; --accent: #5b9fe0; --good: #3ecf98; --warn: #e0a94a;
  }
  * { box-sizing: border-box; }
  html, body {
    margin: 0; padding: 0; background: var(--bg); color: var(--text); height: 100%;
    font-family: -apple-system, "Segoe UI", system-ui, sans-serif;
  }
  .wrap { max-width: 900px; margin: 0 auto; padding: 24px 20px 60px; }
  header { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; margin-bottom: 20px; }
  h1 { font-size: 26px; margin: 0; }
  .status-pill {
    display: inline-flex; align-items: center; gap: 8px; font-size: 13px; padding: 6px 12px;
    border-radius: 999px; border: 1px solid var(--border); background: var(--surface); color: var(--muted);
  }
  .status-pill .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--muted); }
  .status-pill.live { color: var(--good); }
  .status-pill.live .dot { background: var(--good); box-shadow: 0 0 0 4px rgba(62,207,152,0.25); }
  .connect-btn {
    font-size: 20px; font-weight: 600; padding: 18px 30px; border: none; border-radius: 12px;
    background: linear-gradient(180deg, #6badea, var(--accent)); color: #06121f; cursor: pointer;
    box-shadow: 0 6px 0 #3f7fb8, 0 10px 24px rgba(0,0,0,0.35); width: 100%; margin-bottom: 22px;
  }
  .connect-btn:active { transform: translateY(4px); box-shadow: 0 2px 0 #3f7fb8; }
  .connect-btn:disabled { opacity: 0.6; }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
  .card { background: var(--surface); border: 1px solid var(--border); border-radius: 14px; padding: 20px; }
  .card .label { font-size: 12px; letter-spacing: 0.08em; text-transform: uppercase; color: var(--muted); margin-bottom: 10px; }
  .card .value { font-size: 56px; font-weight: 700; font-variant-numeric: tabular-nums; line-height: 1; }
  .bar-track { height: 12px; border-radius: 999px; background: var(--surface-2); margin-top: 14px; overflow: hidden; }
  .bar-fill { height: 100%; border-radius: 999px; width: 0%; transition: width 0.4s ease; }
  #attention-fill { background: linear-gradient(90deg, var(--accent), var(--good)); }
  #meditation-fill { background: linear-gradient(90deg, var(--warn), var(--accent)); }
  .status-line { text-align: center; color: var(--muted); font-size: 13px; margin-top: 18px; min-height: 18px; }
  @media (max-width: 480px) {
    .grid { grid-template-columns: 1fr; }
    .card .value { font-size: 46px; }
  }
</style>
</head>
<body>
  <div class="wrap">
    <header>
      <h1>MindWave Dashboard</h1>
      <div id="status-pill" class="status-pill"><span class="dot"></span><span id="status-pill-text">Idle</span></div>
    </header>
    <button id="connect-btn" class="connect-btn">Connect to EEG Stream</button>
    <div class="grid">
      <div class="card">
        <div class="label">Attention</div>
        <div class="value" id="attention-value">0</div>
        <div class="bar-track"><div class="bar-fill" id="attention-fill"></div></div>
      </div>
      <div class="card">
        <div class="label">Meditation</div>
        <div class="value" id="meditation-value">0</div>
        <div class="bar-track"><div class="bar-fill" id="meditation-fill"></div></div>
      </div>
    </div>
    <div class="status-line" id="status-line">Press Connect to start pulling live data.</div>
  </div>
<script>
  var btn = document.getElementById('connect-btn');
  var pill = document.getElementById('status-pill');
  var pillText = document.getElementById('status-pill-text');
  var statusLine = document.getElementById('status-line');
  var attVal = document.getElementById('attention-value');
  var medVal = document.getElementById('meditation-value');
  var attFill = document.getElementById('attention-fill');
  var medFill = document.getElementById('meditation-fill');

  btn.addEventListener('click', function () {
    btn.disabled = true;
    btn.textContent = 'Connecting...';
    fetch('/connect', { method: 'POST' });
  });

  var es = new EventSource('/events');
  es.onmessage = function (e) {
    var data = JSON.parse(e.data);
    attVal.textContent = data.attention;
    medVal.textContent = data.meditation;
    attFill.style.width = data.attention + '%';
    medFill.style.width = data.meditation + '%';
    statusLine.textContent = data.status;
    if (data.connected) {
      pill.className = 'status-pill live';
      pillText.textContent = 'Live';
      btn.textContent = 'Connected';
    } else {
      pill.className = 'status-pill';
      pillText.textContent = 'Idle';
      if (btn.disabled) btn.textContent = 'Reconnecting...';
    }
  };
</script>
</body>
</html>
"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, threaded=True)
