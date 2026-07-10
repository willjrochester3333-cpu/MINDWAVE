"""
mindwave_dashboard.py  ── ultra-smooth edition
================================================
Connects directly to ThinkGear Connector. NO OpenViBE needed.
REQUIREMENTS: matplotlib, numpy, scipy
"""

import numpy as np
import matplotlib
matplotlib.use("TkAgg")
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
from matplotlib.animation   import FuncAnimation
from matplotlib.ticker      import FixedLocator
from matplotlib.widgets     import Button
from mpl_toolkits.mplot3d   import Axes3D
import threading, time, math, random, socket, json
from collections import deque

try:
    from scipy.signal import butter, sosfilt, sosfilt_zi
    SCIPY_OK = True
except ImportError:
    SCIPY_OK = False
    print("scipy not found — filter buttons disabled. Install via Manage Packages.")

# ── Config ────────────────────────────────────────────────────────────────────
SAMPLE_RATE  = 512
WAVE_SECONDS = 4
WAVE_LEN     = SAMPLE_RATE * WAVE_SECONDS   # 2048
HIST_LEN     = 60
UPDATE_MS    = 50          # 20 fps
BRAIN_EVERY  = 3           # 3D redraws every Nth frame
BAND_EVERY   = 4           # band bars update every Nth frame
BAND_SMOOTH  = 0.35        # EMA factor for band bars — lower = smoother, higher = snappier
BRAIN_SMOOTH = 0.18        # EMA factor for brain node pulses (data-driven, see below)
FPS_EVERY    = 10          # frames between fps readout refreshes
TG_HOST      = "127.0.0.1"
TG_PORT      = 13854
# ──────────────────────────────────────────────────────────────────────────────

# ── Buffers — pre-allocated numpy arrays where possible ───────────────────────
raw_buf    = deque([0.0] * WAVE_LEN, maxlen=WAVE_LEN)
raw_np     = np.zeros(WAVE_LEN, dtype=np.float32)   # reused every frame
att_np     = np.full(HIST_LEN, np.nan, dtype=np.float32)
med_np     = np.full(HIST_LEN, np.nan, dtype=np.float32)
att_hist   = deque(maxlen=HIST_LEN)  # starts empty, fills as data arrives
med_hist   = deque(maxlen=HIST_LEN)  # starts empty, fills as data arrives

state = dict(
    attention=0, meditation=0, signal_q=0,
    delta=0, theta=0, low_alpha=0, high_alpha=0,
    low_beta=0, high_beta=0, low_gamma=0, mid_gamma=0,
    connected=False, sample_count=0,
)
# Snapshot struct — avoids dict copy inside animation loop
snap = dict(state)
lock = threading.Lock()

# ── Streaming bandpass filters with zi state ──────────────────────────────────
wave_mode = ["RAW"]

if SCIPY_OK:
    def make_bp(lo, hi, order=4):
        nyq = SAMPLE_RATE / 2.0
        sos = butter(order, [lo/nyq, hi/nyq], btype="band", output="sos")
        zi  = sosfilt_zi(sos) * 0      # start at zero
        return sos, zi

    FILTERS = {
        "ALPHA": make_bp(8,   12),
        "BETA":  make_bp(13,  30),
        "THETA": make_bp(4,    8),
        "DELTA": make_bp(0.5, 3.9),
        "GAMMA": make_bp(30,  49),
    }

# ── ThinkGear thread — lock held ONLY during state write ─────────────────────
def thinkgear_thread():
    handshake = json.dumps({"enableRawOutput": True, "format": "Json"}) + "\n"
    while True:
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
                # ── parse OUTSIDE the lock ────────────────────────────────
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
                    # ── write to state — lock held as briefly as possible ──
                    with lock:
                        if "poorSignalLevel" in data:
                            state["signal_q"] = max(0, 100 - int(data["poorSignalLevel"] / 2))
                        if "attention" in data and isinstance(data["attention"], (int,float)):
                            state["attention"] = int(data["attention"])
                            att_hist.append(state["attention"])
                        if "meditation" in data and isinstance(data["meditation"], (int,float)):
                            state["meditation"] = int(data["meditation"])
                            med_hist.append(state["meditation"])
                        if "rawEeg" in data:
                            raw_buf.append(float(data["rawEeg"]))
                            state["sample_count"] += 1
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

        except (ConnectionRefusedError, OSError):
            print("ThinkGear Connector not found — retrying in 5s...")
            with lock:
                state["connected"] = False
            time.sleep(5)
        except Exception as e:
            print(f"ThinkGear error: {e} — retrying in 3s...")
            time.sleep(3)
        finally:
            try: sock.close()
            except: pass

# ── Palette ───────────────────────────────────────────────────────────────────
BG      = "#0d1117"
CARD    = "#161b22"
BORDER  = "#30363d"
ACCENT  = "#378ADD"
GREEN   = "#1D9E75"
PURPLE  = "#534AB7"
AMBER   = "#BA7517"
PINK    = "#D4537E"
TEXT    = "#e6edf3"
MUTED   = "#8b949e"

BAND_COLORS = [PURPLE, ACCENT, GREEN, "#27a870", AMBER, "#d9861e", PINK, "#b83460"]
BAND_LABELS = ["Delta","Theta","Lo α","Hi α","Lo β","Hi β","Lo γ","Mi γ"]
BAND_KEYS   = ["delta","theta","low_alpha","high_alpha",
               "low_beta","high_beta","low_gamma","mid_gamma"]

def log_band(v):
    """Convert raw band power to log10 scale for balanced display."""
    return math.log10(max(1.0, float(v))) * 20000

def log_bands(snap):
    """Return log-scaled band values for all 8 bands."""
    return [log_band(snap[k]) for k in BAND_KEYS]
MODE_COLORS = {"RAW":ACCENT,"ALPHA":GREEN,"BETA":AMBER,"THETA":PURPLE,"DELTA":PINK,"GAMMA":"#b83460"}

# ── rcParams ──────────────────────────────────────────────────────────────────
plt.rcParams.update({
    "figure.facecolor": BG,    "axes.facecolor":   CARD,
    "axes.edgecolor":   BORDER,"axes.labelcolor":  MUTED,
    "xtick.color":      MUTED, "ytick.color":      MUTED,
    "text.color":       TEXT,  "grid.color":       BORDER,
    "grid.linewidth":   0.4,   "font.family":      "monospace",
    "font.size":        9,     "axes.titlesize":   9,
    "axes.titlecolor":  MUTED, "axes.titleweight": "normal",
})

# ── Figure ────────────────────────────────────────────────────────────────────
fig = plt.figure(figsize=(15, 9), facecolor=BG)
fig.canvas.manager.set_window_title("MindWave EEG Dashboard")

# Layout: 5 rows, 4 cols
gs = gridspec.GridSpec(5, 4, figure=fig,
                       hspace=0.55, wspace=0.35,
                       left=0.06, right=0.97, top=0.93, bottom=0.09)

ax_wave    = fig.add_subplot(gs[0, :])
ax_brain   = fig.add_subplot(gs[1:3, :2], projection="3d")
ax_gravity = fig.add_subplot(gs[1:3, 2:])
ax_hist    = fig.add_subplot(gs[3, :2])
ax_band    = fig.add_subplot(gs[3, 2:])
ax_hbars   = fig.add_subplot(gs[4, :])

for ax in [ax_wave, ax_band, ax_hbars, ax_hist, ax_gravity]:
    ax.set_facecolor(CARD)
    for sp in ax.spines.values():
        sp.set_edgecolor(BORDER)

ax_brain.set_facecolor(BG)
for pane in [ax_brain.xaxis.pane, ax_brain.yaxis.pane, ax_brain.zaxis.pane]:
    pane.fill = False; pane.set_edgecolor(BORDER)
ax_brain.tick_params(colors=BG)
ax_brain.set_xlabel(""); ax_brain.set_ylabel(""); ax_brain.set_zlabel("")

# ── Metric cards ──────────────────────────────────────────────────────────────
for x, lbl in zip([0.13,0.30,0.50,0.70,0.88],
                  ["ATTENTION","MEDITATION","SIGNAL","SAMPLES","STATUS"]):
    fig.text(x, 0.972, lbl, ha="center", va="top", fontsize=8, color=MUTED)

t_att  = fig.text(0.13, 0.946, "0",      ha="center", va="top", fontsize=22, color=ACCENT, fontweight="bold")
t_med  = fig.text(0.30, 0.946, "0",      ha="center", va="top", fontsize=22, color=GREEN,  fontweight="bold")
t_sig  = fig.text(0.50, 0.946, "0%",     ha="center", va="top", fontsize=22, color=TEXT,   fontweight="bold")
t_samp = fig.text(0.70, 0.946, "0",      ha="center", va="top", fontsize=22, color=TEXT,   fontweight="bold")
t_conn = fig.text(0.88, 0.946, "● WAIT", ha="center", va="top", fontsize=13, color=AMBER)

# Small performance readout, tucked in the bottom-right corner
t_fps = fig.text(0.965, 0.014, "", ha="right", va="bottom", fontsize=7, color=MUTED, fontfamily="monospace")

prev = dict(att=-1, med=-1, sig=-1, samp=-1, conn=None)

# ── Waveform ──────────────────────────────────────────────────────────────────
ax_wave.set_title("raw eeg  —  µV  |  use buttons below to filter", loc="left", pad=5)
ax_wave.set_xlim(0, WAVE_LEN - 1)
ax_wave.set_ylim(-220, 220)
ax_wave.grid(True, axis="y", alpha=0.35)
ax_wave.set_xticks([])
ax_wave.axhline(0, color=BORDER, linewidth=0.7, zorder=1)

X = np.arange(WAVE_LEN, dtype=np.float32)
Y0 = np.zeros(WAVE_LEN, dtype=np.float32)

wave_line, = ax_wave.plot(X, Y0, color=ACCENT, linewidth=0.75, zorder=3, animated=False)

def build_fills(y):
    yp = np.where(y >= 0, y, 0.0)
    yn = np.where(y <  0, y, 0.0)
    zeros = np.zeros(WAVE_LEN, dtype=np.float32)
    def make_poly(yv):
        top  = np.column_stack([X, yv])
        bot  = np.column_stack([X[::-1], zeros[::-1]])
        return np.vstack([top, bot])
    return make_poly(yp), make_poly(yn)

_p, _n = build_fills(Y0)
fill_pos_patch = ax_wave.fill(_p[:,0], _p[:,1], color=ACCENT, alpha=0.08, zorder=2, linewidth=0)[0]
fill_neg_patch = ax_wave.fill(_n[:,0], _n[:,1], color=PINK,   alpha=0.08, zorder=2, linewidth=0)[0]

def update_fill(patch, poly):
    from matplotlib.path import Path
    verts = np.vstack([poly, poly[0]])
    codes = [Path.MOVETO] + [Path.LINETO]*(len(poly)-1) + [Path.CLOSEPOLY]
    patch.get_path().vertices = verts
    patch.get_path().codes    = np.array(codes, dtype=np.uint8)

def apply_filter(raw, mode):
    if mode == "RAW" or not SCIPY_OK:
        return raw
    try:
        sos, zi = FILTERS[mode]
        out, new_zi = sosfilt(sos, raw, zi=zi.reshape(sos.shape[0], 2))
        FILTERS[mode] = (sos, new_zi.reshape(-1))
        return out.astype(np.float32)
    except Exception:
        return raw

# ── Filter buttons ────────────────────────────────────────────────────────────
if SCIPY_OK:
    fig.text(0.062, 0.073, "filter:", fontsize=8, color=MUTED, fontfamily="monospace")
    btn_defs = ["RAW","ALPHA","BETA","THETA","DELTA","GAMMA"]
    btn_refs = []
    for i, mode in enumerate(btn_defs):
        ax_b = fig.add_axes([0.105 + i*0.077, 0.055, 0.070, 0.030])
        col  = MODE_COLORS[mode]
        btn  = Button(ax_b, mode, color=CARD, hovercolor="#1f2937")
        btn.label.set_color(col); btn.label.set_fontsize(9)
        btn.label.set_fontfamily("monospace")
        for sp in ax_b.spines.values():
            sp.set_edgecolor(col); sp.set_linewidth(1.2)
        def make_cb(m):
            def cb(event):
                wave_mode[0] = m
                col = MODE_COLORS[m]
                wave_line.set_color(col)
                fill_pos_patch.set_facecolor(col)
                titles = {
                    "RAW":   "raw eeg  —  µV  |  all frequencies",
                    "ALPHA": "alpha  8–12 Hz  |  close eyes & relax → waves grow",
                    "BETA":  "beta  13–30 Hz  |  concentrate hard → amplitude rises",
                    "THETA": "theta  4–8 Hz   |  relax / let mind wander",
                    "DELTA": "delta  0.5–4 Hz |  slow background rhythm",
                    "GAMMA": "gamma  30–49 Hz |  peak focus / sensory binding",
                }
                ax_wave.set_title(titles[m], loc="left", pad=5)
                ax_wave.set_ylim(-80, 80) if m != "RAW" else ax_wave.set_ylim(-220, 220)
            return cb
        btn.on_clicked(make_cb(mode))
        btn_refs.append(btn)

# ── Transport buttons (pause / snapshot) ───────────────────────────────────────
def toggle_pause(event=None):
    if paused[0]:
        paused[0] = False
        ani.event_source.start()
        btn_pause.label.set_text("⏸ PAUSE")
        btn_pause.label.set_color(TEXT)
    else:
        paused[0] = True
        ani.event_source.stop()
        btn_pause.label.set_text("▶ RESUME")
        btn_pause.label.set_color(AMBER)
    fig.canvas.draw_idle()

def save_snapshot(event=None):
    fname = f"mindwave_snapshot_{time.strftime('%Y%m%d_%H%M%S')}.png"
    fig.savefig(fname, facecolor=BG, dpi=150)
    print(f"Saved snapshot → {fname}")

paused = [False]

ax_pause = fig.add_axes([0.60, 0.055, 0.105, 0.030])
btn_pause = Button(ax_pause, "⏸ PAUSE", color=CARD, hovercolor="#1f2937")
btn_pause.label.set_color(TEXT); btn_pause.label.set_fontsize(9)
btn_pause.label.set_fontfamily("monospace")
for sp in ax_pause.spines.values():
    sp.set_edgecolor(BORDER); sp.set_linewidth(1.2)
btn_pause.on_clicked(toggle_pause)

ax_save = fig.add_axes([0.75, 0.055, 0.135, 0.030])
btn_save = Button(ax_save, "💾 SNAPSHOT", color=CARD, hovercolor="#1f2937")
btn_save.label.set_color(TEXT); btn_save.label.set_fontsize(9)
btn_save.label.set_fontfamily("monospace")
for sp in ax_save.spines.values():
    sp.set_edgecolor(BORDER); sp.set_linewidth(1.2)
btn_save.on_clicked(save_snapshot)

def on_key(event):
    if event.key == "p":
        toggle_pause()
    elif event.key == "s":
        save_snapshot()

fig.canvas.mpl_connect("key_press_event", on_key)

# ── Band bar chart ─────────────────────────────────────────────────────────────
ax_band.set_title("band power spectrum", loc="left", pad=5)
bars = ax_band.bar(BAND_LABELS, [1]*8, color=BAND_COLORS, edgecolor=BG, linewidth=0.4)
ax_band.set_ylim(0, 110000)
ax_band.grid(True, axis="y", alpha=0.35)
ax_band.xaxis.set_major_locator(FixedLocator(range(8)))
ax_band.set_xticklabels(BAND_LABELS, fontsize=8)
ax_band.yaxis.set_major_formatter(plt.FuncFormatter(lambda v,_: f"10^{v/20000:.1f}" if v>0 else "0"))
band_ylim   = [110000]
prev_bvals  = [1.0] * 8   # also doubles as the EMA-smoothed running value

# ── Horizontal intensity bars ──────────────────────────────────────────────────
ax_hbars.set_title("band intensity", loc="left", pad=5)
ax_hbars.set_xlim(0, 1); ax_hbars.set_xticks([])
ax_hbars.set_ylim(-0.5, 7.5)
ax_hbars.yaxis.set_major_locator(FixedLocator(range(8)))
ax_hbars.set_yticklabels(BAND_LABELS[::-1], fontsize=8)
ax_hbars.grid(False)
hbars, hbar_txts = [], []
for i, (lbl, col) in enumerate(zip(BAND_LABELS[::-1], BAND_COLORS[::-1])):
    b = ax_hbars.barh(i, 0.01, color=col, height=0.58, alpha=0.85)
    hbars.append(b[0])
    t = ax_hbars.text(0.03, i, "0k", va="center", fontsize=8, color=TEXT)
    hbar_txts.append(t)

# ── Band web ──────────────────────────────────────────────────────────────────
ax_gravity.set_title("band web  —  mesh stretched toward dominant band", loc="left", pad=5)
ax_gravity.set_xlim(-1.15, 1.15)
ax_gravity.set_ylim(-1.15, 1.15)
ax_gravity.set_aspect("equal", adjustable="datalim")
ax_gravity.axis("off")
ax_gravity.set_facecolor(CARD)

W_N     = len(BAND_KEYS)
W_RINGS = 4
W_MAXR  = 0.82
W_PULL  = 0.85
W_SPRING = 0.045
W_DAMP   = 0.85

def w_anchor(i):
    a = (i / W_N) * math.pi * 2 - math.pi / 2
    return math.cos(a) * (W_MAXR * 1.15), math.sin(a) * (W_MAXR * 1.15)

w_nodes = []
w_grid  = {}
for r in range(1, W_RINGS + 1):
    rad = W_MAXR * (r / W_RINGS)
    for i in range(W_N):
        a  = (i / W_N) * math.pi * 2 - math.pi / 2
        hx = math.cos(a) * rad
        hy = math.sin(a) * rad
        w_grid[(r, i)] = len(w_nodes)
        w_nodes.append(dict(hx=hx, hy=hy, x=hx, y=hy, vx=0.0, vy=0.0,
                            ring=r, spoke=i, rad=rad))

W_CENTER = len(w_nodes)
w_nodes.append(dict(hx=0.0, hy=0.0, x=0.0, y=0.0, vx=0.0, vy=0.0,
                    ring=0, spoke=-1, rad=0.0))

def w_node(ring, spoke):
    if ring == 0:
        return w_nodes[W_CENTER]
    return w_nodes[w_grid[(ring, spoke)]]

w_edges = []
for r in range(1, W_RINGS + 1):
    for i in range(W_N):
        a = w_grid[(r, i)]
        b = w_grid[(r, (i + 1) % W_N)]
        w_edges.append((a, b, i))
for i in range(W_N):
    prev_idx = W_CENTER
    for r in range(1, W_RINGS + 1):
        cur = w_grid[(r, i)]
        w_edges.append((prev_idx, cur, i))
        prev_idx = cur

w_edge_lines = []
for a, b, spoke in w_edges:
    col = BAND_COLORS[spoke]
    ln, = ax_gravity.plot([w_nodes[a]["x"], w_nodes[b]["x"]],
                          [w_nodes[a]["y"], w_nodes[b]["y"]],
                          color=col, linewidth=0.6, alpha=0.2,
                          solid_capstyle="round", zorder=2)
    w_edge_lines.append(ln)

w_node_scatters = []
for i in range(W_N):
    ring_nodes = [w_node(r, i) for r in range(1, W_RINGS + 1)]
    xs = [n["x"] for n in ring_nodes]
    ys = [n["y"] for n in ring_nodes]
    sc = ax_gravity.scatter(xs, ys, s=12, c=BAND_COLORS[i],
                            alpha=0.6, zorder=4, edgecolors="none")
    w_node_scatters.append(sc)

w_hub = ax_gravity.scatter([0],[0], s=30, c=ACCENT, alpha=0.9, zorder=6, edgecolors="none")

w_label_txt = []
for i in range(W_N):
    ax_, ay_ = w_anchor(i)
    lx = ax_ * 1.12; ly = ay_ * 1.12
    txt = ax_gravity.text(lx, ly,
                          BAND_LABELS[i].replace("Lo ","L").replace("Hi ","H").replace("Mi ","M"),
                          ha="center", va="center", fontsize=7,
                          color=BAND_COLORS[i], fontfamily="monospace", zorder=7)
    w_label_txt.append(txt)

w_dom_text = ax_gravity.text(0, -1.05, "pulling toward: —",
                              ha="center", va="center", fontsize=8,
                              color=MUTED, fontfamily="monospace", zorder=8)

# ── Att/Med history ────────────────────────────────────────────────────────────
ax_hist.set_title("attention  vs  meditation — 60s", loc="left", pad=5)
ax_hist.set_xlim(0, HIST_LEN - 1); ax_hist.set_ylim(-5, 105)
ax_hist.grid(True, alpha=0.35); ax_hist.set_xticks([])
ax_hist.axhline(65, color=GREEN, linewidth=0.6, linestyle=":", alpha=0.35, zorder=1)
ax_hist.axhline(40, color=AMBER, linewidth=0.6, linestyle=":", alpha=0.35, zorder=1)
att_line, = ax_hist.plot(att_np, color=ACCENT, linewidth=1.5, label="attention")
med_line, = ax_hist.plot(med_np, color=GREEN,  linewidth=1.5, label="meditation",
                          linestyle="--", dashes=(5,3))
ax_hist.legend(loc="upper left", fontsize=8,
               facecolor=CARD, edgecolor=BORDER, labelcolor=TEXT, framealpha=0.9)

# ── 3D brain ───────────────────────────────────────────────────────────────────
ax_brain.set_title("3d brain activity  —  nodes pulse with live band power", loc="left", pad=2)
ax_brain.set_xlim(-1.2,1.2); ax_brain.set_ylim(-1.2,1.2); ax_brain.set_zlim(-1.2,1.2)
ax_brain.set_box_aspect([1,1,1])

NODES = [
    ( 0.0,  0.6,  0.0, 0.22, PURPLE),
    ( 0.5,  0.1,  0.2, 0.15, ACCENT),
    (-0.5,  0.1,  0.2, 0.15, ACCENT),
    ( 0.3,  0.5,  0.3, 0.14, GREEN),
    (-0.3,  0.5,  0.3, 0.14, GREEN),
    ( 0.0,  0.4, -0.5, 0.18, PINK),
    ( 0.25, 0.0,  0.6, 0.12, AMBER),
    (-0.25, 0.0,  0.6, 0.12, AMBER),
]
NODE_BASE_S = np.array([n[3]*400 for n in NODES], dtype=float)
# Each node is wired to one EEG band, so its pulse reflects real signal power
# rather than arbitrary randomness (there are exactly 8 nodes and 8 bands).
NODE_BAND_IDX = list(range(len(NODES)))

u  = np.linspace(0, 2*np.pi, 24)
v2 = np.linspace(0, np.pi, 16)
ax_brain.plot_surface(
    0.85*np.outer(np.cos(u), np.sin(v2)),
    0.85*np.outer(np.sin(u), np.sin(v2)),
    0.75*np.outer(np.ones(24), np.cos(v2)),
    color=CARD, alpha=0.15, linewidth=0, antialiased=False)

for i in range(len(NODES)):
    for j in range(i+1, len(NODES)):
        dx=NODES[i][0]-NODES[j][0]; dy=NODES[i][1]-NODES[j][1]; dz=NODES[i][2]-NODES[j][2]
        if math.sqrt(dx*dx+dy*dy+dz*dz) < 1.0:
            ax_brain.plot([NODES[i][0],NODES[j][0]],[NODES[i][1],NODES[j][1]],
                          [NODES[i][2],NODES[j][2]], color=BORDER, linewidth=0.5, alpha=0.35)

node_sc, node_pulse_sc = [], []
node_pulse = np.zeros(len(NODES), dtype=float)

for nx,ny,nz,nr,nc in NODES:
    node_sc.append(ax_brain.scatter([nx],[ny],[nz], s=nr*400, c=nc, alpha=0.9, depthshade=True,  zorder=5))
    node_pulse_sc.append(ax_brain.scatter([nx],[ny],[nz], s=nr*400, c=nc, alpha=0.0, depthshade=False, zorder=4))

brain_azim, brain_frame = [0.0], [0]

# ── Animation ──────────────────────────────────────────────────────────────────
frame_n  = [0]
_perf    = dict(last=time.perf_counter(), ema_ms=float(UPDATE_MS))

def update(_):
    frame_n[0] += 1
    fn   = frame_n[0]
    mode = wave_mode[0]

    now = time.perf_counter()
    _perf["ema_ms"] = _perf["ema_ms"] * 0.9 + (now - _perf["last"]) * 1000.0 * 0.1
    _perf["last"] = now
    if fn % FPS_EVERY == 0:
        fps = 1000.0 / _perf["ema_ms"] if _perf["ema_ms"] > 0 else 0.0
        t_fps.set_text(f"{fps:4.1f} fps")

    with lock:
        np.copyto(raw_np, list(raw_buf))
        snap["attention"]    = state["attention"]
        snap["meditation"]   = state["meditation"]
        snap["signal_q"]     = state["signal_q"]
        snap["sample_count"] = state["sample_count"]
        snap["connected"]    = state["connected"]
        snap["delta"]        = state["delta"]
        snap["theta"]        = state["theta"]
        snap["low_alpha"]    = state["low_alpha"]
        snap["high_alpha"]   = state["high_alpha"]
        snap["low_beta"]     = state["low_beta"]
        snap["high_beta"]    = state["high_beta"]
        snap["low_gamma"]    = state["low_gamma"]
        snap["mid_gamma"]    = state["mid_gamma"]
        _att = list(att_hist); _med = list(med_hist)
        att_np[:] = np.nan; att_np[HIST_LEN-len(_att):] = _att
        med_np[:] = np.nan; med_np[HIST_LEN-len(_med):] = _med

    display = apply_filter(raw_np, mode)
    wave_line.set_ydata(display)
    pos_poly, neg_poly = build_fills(display)
    update_fill(fill_pos_patch, pos_poly)
    update_fill(fill_neg_patch, neg_poly)

    # Computed once per frame and shared by the band bars, the 3D brain pulse,
    # and the band web below — avoids recomputing the same 8 log10() calls thrice.
    raw_bvals = log_bands(snap)

    if fn % BAND_EVERY == 0:
        for i in range(8):
            prev_bvals[i] += (raw_bvals[i] - prev_bvals[i]) * BAND_SMOOTH
        mx = max(prev_bvals)
        new_ceil = max(mx * 1.15, band_ylim[0] * 0.97)
        if abs(new_ceil - band_ylim[0]) > band_ylim[0] * 0.05:
            band_ylim[0] = new_ceil
            ax_band.set_ylim(0, band_ylim[0])
        for bar, hb, txt, val in zip(bars, hbars, hbar_txts, prev_bvals):
            bar.set_height(val)
            hb.set_width(max(0.005, val / mx))
            txt.set_text(f"10^{val/20000:.1f}")

    att_line.set_ydata(att_np)
    med_line.set_ydata(med_np)

    att_v = snap["attention"]
    med_v = snap["meditation"]
    sig_v = snap["signal_q"]
    smp_v = snap["sample_count"]
    con_v = snap["connected"]

    if att_v != prev["att"]:
        t_att.set_text(str(att_v))
        t_att.set_color(GREEN if att_v>=65 else (AMBER if att_v>=40 else PINK))
        prev["att"] = att_v
    if med_v != prev["med"]:
        t_med.set_text(str(med_v))
        t_med.set_color(GREEN if med_v>=60 else (ACCENT if med_v>=35 else MUTED))
        prev["med"] = med_v
    if sig_v != prev["sig"]:
        t_sig.set_text(f"{sig_v}%")
        t_sig.set_color(GREEN if sig_v>=70 else (AMBER if sig_v>=40 else PINK))
        prev["sig"] = sig_v
    if smp_v != prev["samp"]:
        t_samp.set_text(f"{smp_v:,}"); prev["samp"] = smp_v
    if con_v != prev["conn"]:
        if con_v:
            t_conn.set_text("● LIVE"); t_conn.set_color(GREEN)
        else:
            t_conn.set_text("● WAIT"); t_conn.set_color(AMBER)
        prev["conn"] = con_v

    brain_frame[0] += 1
    if brain_frame[0] % BRAIN_EVERY == 0:
        brain_azim[0] = (brain_azim[0] + 1.2) % 360
        ax_brain.view_init(elev=20, azim=brain_azim[0])

        # Drive each node's pulse from its wired band's real power, normalized
        # against the current dominant band, and ease toward it — no randomness.
        node_vals  = np.array([raw_bvals[NODE_BAND_IDX[i]] for i in range(len(NODES))])
        node_peak  = node_vals.max() or 1.0
        targets    = node_vals / node_peak
        node_pulse[:] = node_pulse * (1.0 - BRAIN_SMOOTH) + targets * BRAIN_SMOOTH

        main_sizes  = (NODE_BASE_S * (1.0 + node_pulse * 0.35)).tolist()
        pulse_sizes = (NODE_BASE_S * (2.2 + node_pulse * 0.8)).tolist()
        pulse_alpha = (node_pulse * 0.18).tolist()
        for i, (sc, pu) in enumerate(zip(node_sc, node_pulse_sc)):
            sc.set_sizes([main_sizes[i]])
            pu.set_sizes([pulse_sizes[i]])
            pu.set_alpha(pulse_alpha[i])

    # ── Band web ───────────────────────────────────────────────────────────────
    total_w = sum(raw_bvals) or 1.0
    wnorm   = [v / total_w for v in raw_bvals]

    dom_i   = int(np.argmax(raw_bvals))
    dom_col = BAND_COLORS[dom_i]
    w_dom_text.set_text(f"pulling toward: {BAND_LABELS[dom_i]}")
    w_dom_text.set_color(dom_col)

    for nd in w_nodes:
        if nd["spoke"] < 0:
            nd["x"] = 0.0; nd["y"] = 0.0
            continue
        i  = nd["spoke"]
        w  = wnorm[i]
        ax_, ay_ = w_anchor(i)
        ring_frac = nd["ring"] / W_RINGS
        pull = w * W_PULL * ring_frac
        tx = nd["hx"] + (ax_ - nd["hx"]) * pull
        ty = nd["hy"] + (ay_ - nd["hy"]) * pull
        nd["vx"] += (tx - nd["x"]) * W_SPRING
        nd["vy"] += (ty - nd["y"]) * W_SPRING
        nd["vx"] *= W_DAMP
        nd["vy"] *= W_DAMP
        nd["x"]  += nd["vx"]
        nd["y"]  += nd["vy"]

    for (a, b, spoke), ln in zip(w_edges, w_edge_lines):
        na = w_nodes[a]; nb = w_nodes[b]
        ln.set_data([na["x"], nb["x"]], [na["y"], nb["y"]])
        w = wnorm[spoke]
        ln.set_alpha(0.10 + w * 0.70)
        ln.set_linewidth(0.4 + w * 2.6)

    for i, sc in enumerate(w_node_scatters):
        ring_nodes = [w_node(r, i) for r in range(1, W_RINGS + 1)]
        offs = np.array([[n["x"], n["y"]] for n in ring_nodes])
        sc.set_offsets(offs)
        w = wnorm[i]
        sizes = [8 + w * 40 * (n["ring"] / W_RINGS) for n in ring_nodes]
        sc.set_sizes(sizes)
        sc.set_alpha(0.35 + w * 0.6)

    w_hub.set_color(dom_col)

    for i, txt in enumerate(w_label_txt):
        if i == dom_i:
            txt.set_alpha(1.0); txt.set_fontweight("bold")
        else:
            txt.set_alpha(0.5 + wnorm[i] * 0.4); txt.set_fontweight("normal")

    return []

ani = FuncAnimation(fig, update, interval=UPDATE_MS, blit=False, cache_frame_data=False)

# ── Start ──────────────────────────────────────────────────────────────────────
th = threading.Thread(target=thinkgear_thread, daemon=True)
th.start()
print("Waiting for ThinkGear Connector on port 13854...")
print("Do NOT open OpenViBE — it blocks the connection.")
print("Put headset on and wait for blue light.\n")
if SCIPY_OK:
    print("Filter buttons: RAW | ALPHA (relax) | BETA (focus) | THETA | DELTA | GAMMA\n")
print("Keyboard shortcuts: 'p' pause/resume, 's' save snapshot PNG\n")

plt.suptitle("MindWave Mobile 2  —  Live EEG Dashboard",
             fontsize=13, color=TEXT, fontweight="normal", y=0.998)
plt.show()
