#!/usr/bin/env python3
"""MultiMic - record several USB mics at once, one file per mic, and line them up.

A window around multimic.py: tick your mics, watch the live levels, press Record,
clap, play, clap, press Stop. The takes are aligned for you and ready for Audacity.
"""
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import multimic as core
import volume_win

APP = "MultiMic"
RATE = 48000
CLIP = 0.985
# virtual / built-in inputs that are almost never what you want to record
HIDE = ("rode connect", "stream input", "virtual input", "system input",
        "microphone array", "stereo mix", "loopback")

# vertical meter geometry: -60 dB at the bottom .. 0 dB at the top
DB_MIN, METER_H, METER_CW = -60.0, 170, 92
BAR_X0, BAR_X1 = 6, 30
OUT_X0, OUT_X1 = 58, 82           # second meter: what is being recorded
GOOD_LO, GOOD_HI = -18.0, -6.0
STRIP_W = 170

# dark theme
BG, PANEL, FIELD = "#1e1f22", "#2b2d31", "#17181b"
FG, MUTED, BORDER, ACCENT = "#e6e6e6", "#9aa0a6", "#44474d", "#4c8dff"
BTN, BTN_HOVER = "#383b41", "#464a52"
WARN_FG, GATE_FG = "#ff6b6b", "#ffb74d"
# mic colours (pick one per strip; remembered)
PALETTE = ["#ef5350", "#ff9800", "#fdd835", "#66bb6a", "#26a69a",
           "#42a5f5", "#7e57c2", "#ab47bc", "#ec407a", "#bdbdbd"]


def resource(name):
    """Path of a bundled file, both when run from source and from the PyInstaller exe."""
    base = getattr(sys, "_MEIPASS", None) or Path(__file__).resolve().parent
    return Path(base) / name


def to_db(x):
    import math
    return DB_MIN if x <= 1e-6 else max(DB_MIN, 20 * math.log10(x))


def settings_file():
    base = os.environ.get("APPDATA") or str(Path.home())
    d = Path(base) / APP
    d.mkdir(parents=True, exist_ok=True)
    return d / "settings.json"


def load_settings():
    try:
        return json.loads(settings_file().read_text())
    except Exception:
        return {}


def save_settings(data):
    try:
        settings_file().write_text(json.dumps(data, indent=2))
    except Exception:
        pass


def safe_name(text, fallback):
    text = re.sub(r'[\\/:*?"<>|]+', "", text).strip().strip(".")
    return text or fallback


class Row:
    """One microphone line in the window."""

    def __init__(self, dev, on, name, color=PALETTE[5]):
        self.dev = dev
        self.color = color
        self.color_btn = None
        self.rate_lbl = None
        self.native_bits = None
        self.subtype = "PCM_24"
        self.on = tk.BooleanVar(value=on)
        self.name = tk.StringVar(value=name)
        self.canvas = None
        self.peak_lbl = None
        self.state_lbl = None
        self.hold = (0.0, 0.0)  # value, time
        self.error = ""
        self.vol = None          # volume_win.VolumeControl, or None if unavailable
        self.scale = None
        self.vol_lbl = None
        self.dragging = False
        self.updating = False    # True while the program (not the user) moves the slider
        self.job = None
        self.mute = tk.BooleanVar(value=False)   # records silence
        self.solo = tk.BooleanVar(value=False)   # every other mic records silence
        self.gate_off = False                    # the result of mute/solo for this mic
        self.frame = None
        self.gate_lbl = None
        self.mute_btn = self.solo_btn = None
        self.btn_default = ("", "")


class App:
    def __init__(self, root):
        self.root = root
        self.ui_q = queue.Queue()
        self.settings = load_settings()
        self.rows = []
        self.monitors = {}
        self.recs = []
        self.finished = []         # frozen copies of the last take's recorders, used when saving
        self.recording = False
        self.busy = False
        self.t0 = 0.0
        self.take_dir = None      # last saved take (for the Open button)
        self.cur_dir = None       # take being recorded
        self.sd = self.sf = None

        root.title("MultiMic - multi-microphone recorder")
        self._set_icon()
        root.geometry(f"980x{max(620, min(900, root.winfo_screenheight() - 90))}")
        root.minsize(940, 560)
        self._dark_theme()
        self._build()
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        try:
            import sounddevice as sd
            import soundfile as sf
            self.sd, self.sf = sd, sf
        except Exception as e:
            messagebox.showerror(APP, f"Audio libraries are missing:\n{e}\n\n"
                                      "Run:  py -m pip install -r requirements.txt")
            return
        self.refresh_devices()
        self.tick()

    def _dark_theme(self):
        root = self.root
        root.configure(bg=BG)
        style = ttk.Style()
        style.theme_use("clam")
        style.configure(".", background=BG, foreground=FG, fieldbackground=FIELD, bordercolor=BORDER,
                        lightcolor=BORDER, darkcolor=BORDER, troughcolor=FIELD, focuscolor=BG,
                        insertcolor=FG, selectbackground=ACCENT, selectforeground="white")
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=FG)
        style.configure("Hint.TLabel", foreground=MUTED)
        style.configure("TLabelframe", background=BG, bordercolor=BORDER)
        style.configure("TLabelframe.Label", background=BG, foreground=MUTED)
        style.configure("TButton", background=BTN, foreground=FG, bordercolor=BORDER, padding=(8, 3))
        style.map("TButton", background=[("disabled", PANEL), ("pressed", ACCENT), ("active", BTN_HOVER)],
                  foreground=[("disabled", "#6b6f76")])
        style.configure("Big.TButton", font=("Segoe UI", 12, "bold"), padding=8)
        style.configure("TMenubutton", background=BTN, foreground=FG, bordercolor=BORDER,
                        arrowcolor=FG, padding=(10, 3))
        style.map("TMenubutton", background=[("disabled", PANEL), ("active", BTN_HOVER)],
                  foreground=[("disabled", "#6b6f76")])
        style.configure("TCheckbutton", background=BG, foreground=FG, indicatorbackground=FIELD,
                        indicatorforeground=FG)
        style.map("TCheckbutton", background=[("active", BG)],
                  indicatorbackground=[("selected", ACCENT), ("!selected", FIELD)],
                  indicatorforeground=[("selected", "white")])
        style.configure("TEntry", fieldbackground=FIELD, foreground=FG, insertcolor=FG, bordercolor=BORDER)
        style.map("TEntry", fieldbackground=[("readonly", PANEL)])
        style.configure("TCombobox", fieldbackground=FIELD, background=BTN, foreground=FG,
                        arrowcolor=FG, bordercolor=BORDER, selectbackground=FIELD, selectforeground=FG)
        style.map("TCombobox", fieldbackground=[("readonly", FIELD), ("disabled", PANEL)],
                  foreground=[("disabled", "#6b6f76")], background=[("active", BTN_HOVER)])
        root.option_add("*TCombobox*Listbox.background", PANEL)
        root.option_add("*TCombobox*Listbox.foreground", FG)
        root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        root.option_add("*TCombobox*Listbox.selectForeground", "white")
        style.configure("TScale", background=BTN, troughcolor=FIELD, bordercolor=BORDER)
        style.configure("Vertical.TScale", background=BTN, troughcolor=FIELD, bordercolor=BORDER)
        style.configure("TScrollbar", background=BTN, troughcolor=FIELD, bordercolor=BORDER, arrowcolor=FG)
        style.configure("Strip.TFrame", background=PANEL)
        style.configure("Strip.TLabel", background=PANEL, foreground=FG)
        style.configure("StripHint.TLabel", background=PANEL, foreground=MUTED)
        # tk (non-ttk) widgets and menus
        for opt, val in (("Menu.background", PANEL), ("Menu.foreground", FG),
                         ("Menu.activeBackground", ACCENT), ("Menu.activeForeground", "white"),
                         ("Menu.disabledForeground", "#6b6f76"), ("Menu.selectColor", ACCENT),
                         ("Menu.borderWidth", 0), ("Menu.relief", "flat")):
            root.option_add("*" + opt, val)
        try:                                   # dark title bar on Windows 10/11
            if sys.platform == "win32":
                import ctypes
                root.update_idletasks()
                hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
                on = ctypes.c_int(1)
                for attr in (20, 19):
                    ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(on), 4)
        except Exception:
            pass

    def _set_icon(self):
        try:
            if sys.platform == "win32":
                self.root.iconbitmap(default=str(resource("multimic.ico")))
            else:
                self._icon = tk.PhotoImage(file=str(resource("multimic.png")))
                self.root.iconphoto(True, self._icon)
        except Exception:
            pass                                    # a missing icon must never stop the app

    # ------------------------------------------------------------------ layout
    def _build(self):
        pad = {"padx": 10, "pady": 4}
        top = ttk.Frame(self.root)
        top.pack(fill="x", **pad)
        ttk.Label(top, text="1. Choose your microphones", font=("Segoe UI", 11, "bold")).pack(side="left")
        self.add_btn = ttk.Menubutton(top, text="Available Mics", direction="below")
        self.add_btn.pack(side="left", padx=14)
        self.add_menu = tk.Menu(self.add_btn, tearoff=False)
        self.add_btn.configure(menu=self.add_menu)
        ttk.Button(top, text="Refresh", command=self.refresh_devices).pack(side="right")
        ttk.Button(top, text="Reset peaks", command=self.reset_peaks).pack(side="right", padx=4)
        ttk.Button(top, text="Windows sound settings",
                   command=self.open_sound_settings).pack(side="right", padx=4)

        self.mic_frame = tk.Frame(self.root, bg=BG, highlightthickness=1, highlightbackground=BORDER,
                                  padx=4, pady=4)
        self.mic_frame.pack(fill="x", **pad)

        hint = ttk.Frame(self.root)
        hint.pack(fill="x", padx=10)
        ttk.Label(hint, style="Hint.TLabel", wraplength=920, justify="left", text=(
            "Tick mics in Available Mics (top left). Drag a strip's colour bar to reorder, click it to pick a "
            "colour. Reach the green zone, never the red end, or lower the volume slider (locked while "
            "recording). In = what the mic hears, Rec = what is recorded (Peak and CLIPPING follow Rec). "
            "M = record silence, S = solo (the others record silence); both work during a take.")
        ).pack(side="left")
        opt = ttk.LabelFrame(self.root, text="2. Options", padding=6)
        opt.pack(fill="x", **pad)
        r = ttk.Frame(opt)
        r.pack(fill="x")
        ttk.Label(r, text="Save takes in:").pack(side="left")
        default_dir = str(Path.home() / "Documents" / "MultiMic takes")
        self.out_dir = tk.StringVar(value=self.settings.get("out_dir", default_dir))
        ttk.Entry(r, textvariable=self.out_dir).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(r, text="Browse...", command=self.browse).pack(side="left")
        self.auto_align = tk.BooleanVar(value=self.settings.get("auto_align", True))
        self.end_clap = tk.BooleanVar(value=self.settings.get("end_clap", True))
        r2 = ttk.Frame(opt)
        r2.pack(fill="x", pady=(6, 0))
        ttk.Checkbutton(r2, text="Line the mics up when I stop (needs a clap at the start)",
                        variable=self.auto_align).pack(side="left")
        ttk.Checkbutton(r2, text="Use a clap at the end to fix clock drift",
                        variable=self.end_clap).pack(side="left", padx=18)

        r3 = ttk.Frame(opt)
        r3.pack(fill="x", pady=(6, 0))
        ttk.Label(r3, text="Save files as:").pack(side="left")
        self.out_rate = tk.StringVar(value=str(self.settings.get("out_rate", 48000)))
        self.out_depth = tk.StringVar(value=self.settings.get("out_depth", "24-bit"))
        if self.out_depth.get() not in core.SUBTYPES:
            self.out_depth.set("24-bit")
        self.rate_cb = ttk.Combobox(r3, textvariable=self.out_rate, width=8, state="readonly",
                                    values=[str(r) for r in core.OUT_RATES])
        self.rate_cb.pack(side="left", padx=(8, 2))
        ttk.Label(r3, text="Hz").pack(side="left")
        self.depth_cb = ttk.Combobox(r3, textvariable=self.out_depth, width=12, state="readonly",
                                     values=list(core.SUBTYPES))
        self.depth_cb.pack(side="left", padx=(14, 2))
        ttk.Label(r3, style="Hint.TLabel", text="  This is the format of the aligned copies. Your originals stay as recorded.").pack(side="left")
        for cb in (self.rate_cb, self.depth_cb):
            cb.bind("<<ComboboxSelected>>", lambda e: self.on_format_changed())

        rec = ttk.LabelFrame(self.root, text="3. Record", padding=6)
        rec.pack(fill="x", **pad)
        self.btn = ttk.Button(rec, text="●  Record", style="Big.TButton", command=self.toggle_record)
        self.btn.pack(side="left")
        self.timer = ttk.Label(rec, text="00:00", font=("Consolas", 18))
        self.timer.pack(side="left", padx=16)
        self.open_btn = ttk.Button(rec, text="Open last take folder", command=self.open_take, state="disabled")
        self.open_btn.pack(side="right")
        self.status = ttk.Label(self.root, wraplength=920, justify="left", font=("Segoe UI", 10, "bold"))
        self.status.pack(fill="x", padx=10, pady=2)
        self.set_status("Ready. Add your mics above, check the levels, then press Record.")

        logf = ttk.Frame(self.root)
        logf.pack(fill="both", expand=True, **pad)
        self.log = tk.Text(logf, height=5, wrap="word", state="disabled", font=("Consolas", 10),
                           background=FIELD, foreground=FG, insertbackground=FG,
                           relief="flat", highlightthickness=1, highlightbackground=BORDER)
        sb = ttk.Scrollbar(logf, command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        self.log.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.log.tag_configure("warn", foreground=WARN_FG)
        self.log.tag_configure("ok", foreground="#6fcf7f")

    def set_status(self, text):
        self.status.configure(text=text)

    def write(self, text, tag=None):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n", tag)
        self.log.see("end")
        self.log.configure(state="disabled")

    # ----------------------------------------------------------------- devices
    def refresh_devices(self):
        if self.recording or self.busy:
            return
        self.stop_monitor()
        for w in self.mic_frame.winfo_children():
            w.destroy()
        saved = self.settings.get("mics", {})
        order = self.settings.get("order", [])
        self.rows = []
        self.empty_lbl = None
        try:
            devs = core.list_input_devices(self.sd)
        except Exception as e:
            self.write(f"Could not list devices: {e}", "warn")
            devs = []
        shown = list(devs)                      # every microphone is listed in Available Mics
        # keep the arrangement the user chose; new devices go on the right
        shown.sort(key=lambda d: order.index(d["name"]) if d["name"] in order else len(order))
        for n, d in enumerate(shown):
            s = saved.get(d["name"])
            looks_rode = "nt-usb" in d["name"].lower()
            on = s["on"] if s else looks_rode
            default = f"mic{n + 1}"
            color = (s or {}).get("color") or PALETTE[n % len(PALETTE)]
            row = Row(d, on, (s or {}).get("name", default), color)
            self._build_strip(row)
            self.rows.append(row)
        self.reflow()
        self.apply_gates()
        self.start_monitor()

    def _build_strip(self, row):
        """One mixer-style channel: arrange controls, name, vertical meter + volume, M / S."""
        f = tk.Frame(self.mic_frame, bg=PANEL, highlightthickness=2, highlightbackground=row.color,
                     highlightcolor=row.color, padx=6, pady=4)
        row.frame = f
        ttk.Frame(f, width=STRIP_W, height=1, style="Strip.TFrame").pack()   # minimum width

        head = ttk.Frame(f, style="Strip.TFrame")
        head.pack(fill="x")
        ttk.Button(head, text="◀", width=3, command=lambda r=row: self.nudge(r, -1)).pack(side="left")
        ttk.Button(head, text="▶", width=3, command=lambda r=row: self.nudge(r, 1)).pack(side="right")
        # colour bar: click = pick this mic's colour, press and drag = move the strip
        row.color_btn = tk.Label(head, text="▾", bg=row.color, fg="#111111", cursor="hand2",
                                 font=("Segoe UI", 9, "bold"))
        row.color_btn.pack(side="left", fill="both", expand=True, padx=6, pady=1)
        row.color_btn.bind("<ButtonPress-1>", lambda e, r=row: self._color_press(r, e))
        row.color_btn.bind("<ButtonRelease-1>", lambda e, r=row: self._color_release(r, e))

        ttk.Frame(f, height=6, style="Strip.TFrame").pack()
        namebox = ttk.Frame(f, width=STRIP_W, height=62, style="Strip.TFrame")             # 4 lines, same height in every strip
        namebox.pack_propagate(False)
        namebox.pack(fill="x")
        ttk.Label(namebox, text=row.dev["name"], wraplength=STRIP_W - 8, justify="left",
                  style="StripHint.TLabel").pack(anchor="nw")
        ttk.Entry(f, textvariable=row.name, width=14, justify="center").pack(fill="x", pady=(4, 2))
        row.rate_lbl = ttk.Label(f, text="", font=("Segoe UI", 8), style="StripHint.TLabel")
        row.rate_lbl.pack()

        cap = ttk.Frame(f, style="Strip.TFrame")
        cap.pack(fill="x")
        ttk.Label(cap, text="In", style="StripHint.TLabel").pack(side="left", padx=(8, 0))
        ttk.Label(cap, text="Rec", style="StripHint.TLabel").pack(side="left", padx=(22, 0))
        ttk.Label(cap, text="Volume", style="StripHint.TLabel").pack(side="right")
        mid = ttk.Frame(f, style="Strip.TFrame")
        mid.pack(pady=2)
        row.canvas = tk.Canvas(mid, width=METER_CW, height=METER_H, highlightthickness=0,
                               background=PANEL)
        row.canvas.pack(side="left")
        row.vol = volume_win.find_capture_volume(row.dev["name"])
        nf = volume_win.native_format(row.dev["name"])          # (rate, bits, is_float) from Windows
        row.native_bits = nf[1] if nf else None
        row.subtype = core.subtype_for_bits(nf[1], nf[2]) if nf else "PCM_24"
        row.scale = ttk.Scale(mid, from_=100, to=0, orient="vertical", length=METER_H,
                              command=lambda v, r=row: self.on_volume(r, v))
        row.scale.pack(side="left", padx=(6, 6))
        row.scale.bind("<ButtonPress-1>", lambda e, r=row: setattr(r, "dragging", True))
        row.scale.bind("<ButtonRelease-1>", lambda e, r=row: setattr(r, "dragging", False))

        row.peak_lbl = ttk.Label(f, text="", font=("Consolas", 10), style="Strip.TLabel")
        row.peak_lbl.pack()
        row.vol_lbl = ttk.Label(f, text="", font=("Consolas", 10), style="Strip.TLabel")
        row.vol_lbl.pack()
        row.state_lbl = ttk.Label(f, text="", foreground=WARN_FG, font=("Segoe UI", 9, "bold"),
                              style="Strip.TLabel")
        row.state_lbl.pack()
        row.gate_lbl = ttk.Label(f, text="", foreground=GATE_FG, font=("Segoe UI", 9, "bold"),
                              style="Strip.TLabel")
        row.gate_lbl.pack()

        btns = ttk.Frame(f, style="Strip.TFrame")
        btns.pack(pady=(4, 2))
        bopts = dict(width=4, font=("Segoe UI", 11, "bold"), bg=BTN, fg=FG, activebackground=BTN_HOVER,
                     activeforeground=FG, bd=1, highlightthickness=0, relief="raised")
        row.mute_btn = tk.Button(btns, text="M", command=lambda r=row: self.toggle_mute(r), **bopts)
        row.solo_btn = tk.Button(btns, text="S", command=lambda r=row: self.toggle_solo(r), **bopts)
        row.mute_btn.pack(side="left", padx=3)
        row.solo_btn.pack(side="left", padx=3)
        row.btn_default = (row.mute_btn.cget("bg"), row.mute_btn.cget("fg"))
        self.style_buttons(row)

        if row.vol is None:
            row.scale.state(["disabled"])
            row.vol_lbl.configure(text="volume: n/a")
            self.write(f"Input volume slider unavailable for {row.dev['name']}: "
                       f"{volume_win.LAST_ERROR or 'unknown reason'}", "warn")
        else:
            self.sync_volume(row)

    def on_tick_mic(self, row=None):
        if (self.recording or self.busy) and row is not None:
            row.on.set(not row.on.get())           # mics can't be added or removed mid-take
            return
        if row is not None and row.on.get():
            vis = self.visible_rows()
            if vis and vis[-1] is not row:         # a newly added mic goes on the right
                self.rows.remove(row)
                self.rows.insert(self.rows.index(vis[-1]) + 1, row)
        for r in self.rows:
            if not r.on.get() and (r.mute.get() or r.solo.get()):
                r.mute.set(False)
                r.solo.set(False)
                self.style_buttons(r)
        self.reflow()
        self.apply_gates()
        self.start_monitor(keep=True)
        self.save()

    # ------------------------------------------------------------ arrangement
    def visible_rows(self):
        return [r for r in self.rows if r.on.get()]

    def reflow(self):
        i = 0
        for row in self.rows:
            if row.on.get():
                row.frame.grid(row=0, column=i, padx=4, pady=2, sticky="n")
                i += 1
            else:
                row.frame.grid_remove()
        if getattr(self, "empty_lbl", None) is not None:
            self.empty_lbl.destroy()
            self.empty_lbl = None
        if i == 0:
            self.empty_lbl = ttk.Label(
                self.mic_frame, padding=20,
                text="No microphones selected. Open Available Mics (top left) and tick the mics you want, or plug them in and press Refresh.")
            self.empty_lbl.grid(row=0, column=0)
        self.rebuild_add_menu()

    def rebuild_add_menu(self):
        self.add_menu.delete(0, "end")
        locked = self.recording or self.busy
        stable = sorted(self.rows, key=lambda r: (any(h in r.dev["name"].lower() for h in HIDE),
                                                  r.dev["index"]))
        for r in stable:
            self.add_menu.add_checkbutton(
                label=r.dev["name"], variable=r.on, command=lambda r=r: self.on_tick_mic(r),
                state="disabled" if locked else "normal")
        n_on = len(self.visible_rows())
        self.add_btn.configure(state="disabled" if (locked or not self.rows) else "normal",
                               text=f"Available Mics ({n_on} recording)" if self.rows else "Available Mics")

    def add_row(self, row):
        """Tick a mic from the Available Mics list (used by the menu and by tests)."""
        row.on.set(True)
        self.on_tick_mic(row)

    def move_row(self, row, idx):
        """Move a shown strip to position idx among the shown strips."""
        if self.recording or self.busy or row not in self.rows:
            return
        vis = self.visible_rows()
        idx = max(0, min(idx, len(vis) - 1))
        if row in vis and vis.index(row) == idx:
            return
        self.rows.remove(row)
        vis = [r for r in vis if r is not row]
        if idx < len(vis):
            pos = self.rows.index(vis[idx])
        else:
            pos = self.rows.index(vis[-1]) + 1 if vis else len(self.rows)
        self.rows.insert(pos, row)
        self.reflow()
        self.save()

    def nudge(self, row, step):
        vis = self.visible_rows()
        if row in vis:
            self.move_row(row, vis.index(row) + step)

    def _drag_start(self, row, e):
        if self.recording or self.busy:
            return
        self._drag_row = row
        self.root.configure(cursor="fleur")

    def _color_press(self, row, e):
        row._press = (e.x_root, e.y_root)
        self._drag_start(row, e)

    def _color_release(self, row, e):
        x0, y0 = getattr(row, "_press", (e.x_root, e.y_root))
        if abs(e.x_root - x0) < 6 and abs(e.y_root - y0) < 6:      # a click, not a drag
            self._drag_row = None
            self.root.configure(cursor="")
            self.pick_color(row)
        else:
            self._drag_end(row, e)

    def pick_color(self, row):
        """Small popup of colour swatches under the strip's colour bar."""
        pop = tk.Toplevel(self.root)
        pop.overrideredirect(True)
        pop.configure(bg=BORDER, padx=1, pady=1)
        inner = tk.Frame(pop, bg=PANEL, padx=6, pady=6)
        inner.pack()

        def choose(c):
            pop.destroy()
            self.set_color(row, c)
        for n, c in enumerate(PALETTE):
            sw = tk.Label(inner, bg=c, width=3, height=1, cursor="hand2",
                          highlightthickness=2, highlightbackground=FG if c == row.color else PANEL)
            sw.grid(row=n // 5, column=n % 5, padx=2, pady=2)
            sw.bind("<Button-1>", lambda e, c=c: choose(c))
        pop.update_idletasks()
        b = row.color_btn
        pop.geometry(f"+{b.winfo_rootx()}+{b.winfo_rooty() + b.winfo_height() + 2}")

        def outside(e):
            w = pop.winfo_containing(e.x_root, e.y_root)
            if w is None or not str(w).startswith(str(pop)):
                pop.destroy()
        pop.bind("<Button-1>", outside)
        pop.bind("<Escape>", lambda e: pop.destroy())
        pop.grab_set()
        pop.focus_set()
        row.popup = pop

    def set_color(self, row, color):
        row.color = color
        row.color_btn.configure(bg=color)
        row.frame.configure(highlightbackground=color, highlightcolor=color)
        self.save()

    def _drag_end(self, row, e):
        if getattr(self, "_drag_row", None) is not row:
            return
        self._drag_row = None
        self.root.configure(cursor="")
        others = [r for r in self.visible_rows() if r is not row]
        idx = sum(1 for r in others if r.frame.winfo_rootx() + r.frame.winfo_width() / 2 < e.x_root)
        self.move_row(row, idx)

    # -------------------------------------------------------------- mute / solo
    def effective_off(self, row):
        """True if this mic should record silence right now."""
        if not row.on.get():
            return False
        soloing = any(r.solo.get() and r.on.get() for r in self.rows)
        return row.mute.get() or (soloing and not row.solo.get())

    def why_off(self, row):
        return "muted" if row.mute.get() else "silenced by solo"

    def apply_gates(self):
        """Push mute/solo to the recorders; returns the rows whose state changed."""
        changed = []
        for row in self.rows:
            off = self.effective_off(row)
            if off != row.gate_off:
                changed.append(row)
            row.gate_off = off
            for rec in list(self.recs) + list(self.monitors.values()):
                if rec.index == row.dev["index"]:
                    rec.gate_off = off
        return changed

    def style_buttons(self, row):
        d_bg, d_fg = row.btn_default
        if row.mute.get():
            row.mute_btn.configure(bg="#d32f2f", fg="white", relief="sunken")
        else:
            row.mute_btn.configure(bg=d_bg, fg=d_fg, relief="raised")
        if row.solo.get():
            row.solo_btn.configure(bg="#f5a623", fg="black", relief="sunken")
        else:
            row.solo_btn.configure(bg=d_bg, fg=d_fg, relief="raised")

    def toggle_mute(self, row):
        if self.busy:
            return
        row.mute.set(not row.mute.get())
        self._gate_changed()

    def toggle_solo(self, row):
        if self.busy:
            return
        row.solo.set(not row.solo.get())
        self._gate_changed()

    def _gate_changed(self):
        changed = self.apply_gates()
        for r in self.rows:
            self.style_buttons(r)
        if self.recording:                        # leave a trail of what happened when
            t = int(time.perf_counter() - self.t0)
            for r in changed:
                what = f"silenced ({self.why_off(r)})" if r.gate_off else "back on"
                self.write(f"[{t // 60:02d}:{t % 60:02d}] {safe_name(r.name.get(), 'mic')}: {what}")

    # ----------------------------------------------------------------- volume
    def sync_volume(self, row):
        """Show the mic's current Windows volume on its slider."""
        if row.vol is None or row.dragging or row.job is not None:
            return
        try:
            v = row.vol.get() * 100
        except Exception:
            return
        if abs(row.scale.get() - v) >= 1 or not row.vol_lbl.cget("text"):
            row.updating = True
            row.scale.set(v)
            row.updating = False
        row.vol_lbl.configure(text=f"Volume {v:.0f} %")

    def on_volume(self, row, value):
        if row.updating or row.vol is None:
            return
        v = float(value)
        row.vol_lbl.configure(text=f"Volume {v:.0f} %")
        if row.job is not None:                      # apply shortly after the slider stops moving
            self.root.after_cancel(row.job)
        row.job = self.root.after(80, lambda: self._apply_volume(row, v))

    def _apply_volume(self, row, v):
        row.job = None
        try:
            row.vol.set(v / 100)
        except Exception as e:
            row.error = "volume failed"
            self.write(f"Could not change the volume of {row.dev['name']}: {e}", "warn")

    def fmt(self):
        """(sample rate, soundfile subtype) chosen for the saved files. Main thread only."""
        try:
            rate = int(self.out_rate.get())
        except ValueError:
            rate = 48000
        return rate, core.SUBTYPES.get(self.out_depth.get(), "PCM_24")

    def on_format_changed(self):
        self.rate_cb.selection_clear()
        self.depth_cb.selection_clear()
        for row in self.rows:                   # the mics keep recording in their own format; only the
            rec = self.monitors.get(row.dev["index"])        # aligned copies change
            if rec is not None and row.on.get():
                row.rate_lbl.configure(text=self.rate_text(rec, self.fmt()[0]))
        self.save()

    def set_sliders_enabled(self, enabled):
        for row in self.rows:
            if row.vol is not None and row.scale is not None:
                row.scale.state(["!disabled"] if enabled else ["disabled"])
        for cb in (self.rate_cb, self.depth_cb):
            cb.configure(state="readonly" if enabled else "disabled")

    @staticmethod
    def open_hint(name, err):
        text = f"{name} {err}".lower()
        if any(k in text for k in ("headset", "hands-free", "bluetooth", "handsfree")) or "9992" in text:
            return ("  This looks like a Bluetooth headset. Its microphone only works in a low-quality "
                    "phone-call mode and Windows may refuse to share it.")
        return "  Another program (such as RØDE Connect) may be using it."

    @staticmethod
    def rate_text(rec, wanted):
        khz = f"{rec.rate / 1000:g} kHz"
        bits = {"PCM_16": "16-bit", "PCM_24": "24-bit", "FLOAT": "32-bit float"}.get(
            getattr(rec, "file_subtype", ""), "")
        if bits:
            khz += f", {bits}"
        if rec.rate != wanted:
            khz += f" > {wanted / 1000:g} on save"
        return khz + rec.api_note.replace("(using the ", "(").replace(" driver)", ")")

    # --------------------------------------------------------------- monitoring
    def checked_rows(self):
        return [r for r in self.rows if r.on.get()]

    def stop_monitor(self):
        for rec in self.monitors.values():
            try:
                rec.stop()
            except Exception:
                pass
        self.monitors = {}

    def start_monitor(self, keep=False):
        """Open the level meters for the ticked mics. keep=True leaves streams that are already open
        alone (only opening new ones and closing unticked ones), so nothing is closed and reopened
        needlessly; a Bluetooth headset drops out when that happens."""
        if self.recording or self.busy or not self.sd:
            return
        if not keep:
            self.stop_monitor()
        for row in self.rows:
            idx = row.dev["index"]
            if keep and idx in self.monitors:
                if row.on.get():
                    continue                                   # already open, leave it
                try:
                    self.monitors.pop(idx).stop()
                except Exception:
                    pass
                continue
            row.error = ""
            row.dead_warned = False
            if not row.on.get():
                continue
            try:
                rec = core.MicRecorder(self.sd, self.sf, row.dev["index"], "meter", None, None, 0,
                                       row.subtype)            # the mic's own rate and bit depth
                rec.start()
                self.monitors[row.dev["index"]] = rec
                row.rate_lbl.configure(text=self.rate_text(rec, self.fmt()[0]))
            except Exception as e:
                row.error = "can't open"
                row.rate_lbl.configure(text="")
                row.on.set(False)
                self.write(f"Could not open {row.dev['name']}: {e}\n" + self.open_hint(row.dev["name"], e),
                           "warn")

    def reset_peaks(self):
        for rec in list(self.monitors.values()) + list(self.recs):
            rec.max_peak = 0.0
            rec.disp_peak = 0.0

    # -------------------------------------------------------------------- tick
    def active(self, row):
        idx = row.dev["index"]
        for rec in self.recs:
            if rec.index == idx:
                return rec
        return self.monitors.get(idx)

    def tick(self):
        try:
            while True:
                self.ui_q.get_nowait()()
        except queue.Empty:
            pass
        now = time.time()
        for row in self.rows:
            if not row.on.get():
                continue
            rec = self.active(row)
            self.draw_meter(row, rec, now)
        self._ticks = getattr(self, "_ticks", 0) + 1
        if self._ticks % 20 == 0:
            for row in self.visible_rows():
                self.sync_volume(row)
        if self.recording:
            t = int(time.perf_counter() - self.t0)
            self.timer.configure(text=f"{t // 60:02d}:{t % 60:02d}")
        self._tick_job = self.root.after(50, self.tick)

    def draw_meter(self, row, rec, now):
        c = row.canvas
        c.delete("all")
        H = METER_H

        def y(db):
            return H * (1 - (db - DB_MIN) / -DB_MIN)

        def bar(x0, x1):
            c.create_rectangle(x0, 0, x1, H, fill="#111317", outline="")
            c.create_rectangle(x0, y(GOOD_HI), x1, y(GOOD_LO), fill="#2f4f2f", outline="")

        bar(BAR_X0, BAR_X1)
        bar(OUT_X0, OUT_X1)
        for db in (0, -6, -12, -18, -30, -45, -60):
            yy = min(max(y(db), 1), H - 1)
            c.create_line(BAR_X1 + 1, yy, BAR_X1 + 5, yy, fill="#80848b")
            c.create_text(BAR_X1 + 6, min(max(yy, 6), H - 6), text=str(db), anchor="w",
                          fill="#80848b", font=("Segoe UI", 7))
        gate = "MUTED" if row.mute.get() else "silenced by solo"
        row.gate_lbl.configure(text=gate if row.gate_off else "")
        if rec is None:
            row.peak_lbl.configure(text="")
            row.state_lbl.configure(text=row.error)
            return
        # left meter (In): what the mic hears, always live
        db = to_db(rec.peak)
        color = "#3cb043" if db < GOOD_HI else ("#f5a623" if db < -1.0 else "#e53935")
        if db > DB_MIN:
            c.create_rectangle(BAR_X0 + 2, y(db), BAR_X1 - 2, H - 2, fill=color, outline="")
        # right meter (Rec): what goes into the file (silent when muted / not soloed).
        # The peak-hold line, the Peak reading and the CLIPPING warning all follow this one.
        out = rec.out_peak
        odb = to_db(out)
        ocolor = "#3cb043" if odb < GOOD_HI else ("#f5a623" if odb < -1.0 else "#e53935")
        if odb > DB_MIN:
            c.create_rectangle(OUT_X0 + 2, y(odb), OUT_X1 - 2, H - 2, fill=ocolor, outline="")
        if odb >= to_db(row.hold[0]) or now - row.hold[1] > 2.0:    # peak-hold tick
            row.hold = (out, now)
        if row.hold[0] > 0:
            hy = y(to_db(row.hold[0]))
            c.create_line(OUT_X0, hy, OUT_X1, hy, fill="white", width=2)
        if row.gate_off:
            c.create_text((OUT_X0 + OUT_X1) // 2, H // 2, text="OFF", fill="#bbbbbb",
                          font=("Segoe UI", 8, "bold"))
        dead = rec.frames == 0 and rec.t_start and time.perf_counter() - rec.t_start > 2.0
        if dead and not getattr(row, "dead_warned", False):
            row.dead_warned = True
            self.write(f"{safe_name(row.name.get(), 'mic')} ({row.dev['name']}): no audio is arriving from "
                       "this device. It may be a Bluetooth headset, disabled or muted in Windows, or "
                       "in use by another program. Untick it in Available Mics if you don't need it.", "warn")
        mx = to_db(rec.disp_peak)
        row.peak_lbl.configure(text=f"Peak {mx:5.1f} dB" if rec.disp_peak > 0 else "")
        row.state_lbl.configure(text="NO AUDIO!" if dead else ("CLIPPING!" if rec.disp_peak >= CLIP else row.error))

    # ------------------------------------------------------------------ record
    def toggle_record(self):
        if self.busy:
            return
        if self.recording:
            self.stop_record()
        else:
            self.start_record()

    def start_record(self):
        rows = self.checked_rows()
        if not rows:
            messagebox.showinfo(APP, "Add at least one microphone first (Available Mics list, top left).")
            return
        names, used = [], set()
        for n, row in enumerate(rows):
            nm = safe_name(row.name.get(), f"mic{n + 1}")
            base, k = nm, 2
            while nm.lower() in used:
                nm, k = f"{base}{k}", k + 1
            used.add(nm.lower())
            names.append(nm)
        silent = [(r, nm) for r, nm in zip(rows, names) if self.effective_off(r)]
        if silent:
            lines = "\n".join(f"  •  {nm}: {self.why_off(r)}" for r, nm in silent)
            if not messagebox.askokcancel(
                    APP, "These mics will record SILENCE:\n\n" + lines +
                    "\n\nIf that is not what you want, press Cancel and turn off M / S first.\n"
                    "Tip: keep every mic un-muted for the first clap, so the mics can be lined up.\n\n"
                    "Start recording anyway?", icon="warning"):
                return
        outdir = Path(self.out_dir.get()) / datetime.now().strftime("take_%Y%m%d_%H%M%S")
        try:
            outdir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            messagebox.showerror(APP, f"Can't create the folder:\n{outdir}\n\n{e}")
            return
        recs, fresh = [], []
        try:
            for row, nm in zip(rows, names):
                rec = self.monitors.pop(row.dev["index"], None)      # reuse the open meter stream
                if rec is None:
                    rec = core.MicRecorder(self.sd, self.sf, row.dev["index"], nm, None, outdir, 0,
                                           row.subtype)
                    fresh.append(rec)
                else:
                    rec.prepare(outdir, nm)
                recs.append(rec)
        except Exception as e:
            for r in recs:
                try:
                    if r in fresh:
                        r.stream.close()
                        r.file.close()
                    else:
                        r.end()
                        self.monitors[r.index] = r
                except Exception:
                    pass
            messagebox.showerror(APP, f"Could not start recording:\n{e}\n\n"
                                 + self.open_hint(row.dev["name"], e).strip())
            return
        self.recs, self.cur_dir = recs, outdir
        self.t0 = time.perf_counter()
        for row in self.rows:
            row.gate_off = False
        self.apply_gates()                              # mute / solo as they are right now
        for r in recs:
            if r in fresh:
                r.gain = 0.0 if r.gate_off else 1.0     # start closed if muted: no fade-in click
                r.start()
            else:
                r.go()                                  # the open stream starts the take at its next buffer
        for row, r in zip(rows, recs):
            row.rate_lbl.configure(text=self.rate_text(r, self.fmt()[0]))
        self.recording = True
        self.set_sliders_enabled(False)
        self.rebuild_add_menu()
        self.btn.configure(text="■  Stop")
        self.open_btn.configure(state="disabled")
        self.set_status("RECORDING.  Stay quiet a moment, then clap ONCE near all the mics. "
                        "Play. At the end clap once more, stay quiet for 2 seconds, then press Stop.")
        self.write(f"Recording to {outdir}")
        self.save()

    def stop_record(self):
        """Capture ends the instant Stop is pressed; then we ask whether to keep the take."""
        self.recording = False
        self.busy = True
        self.btn.configure(state="disabled", text="Stopping...")
        self.set_status("Stopped. Choose whether to save the recording.")
        threading.Thread(target=self._stop_streams, daemon=True).start()

    def _post(self, fn):
        self.ui_q.put(fn)

    def _stop_streams(self):
        """Finish each mic's file; the devices stay open (meters keep running) so a Bluetooth
        headset is not switched off and on again."""
        snaps = []
        for r in self.recs:
            try:
                snaps.append(r.end())
            except Exception:
                pass
        self.finished = snaps
        self._post(self._after_stop)

    def _after_stop(self):
        for r in self.recs:
            self.monitors[r.index] = r
        self.recs = []
        self._ask_and_finish()

    def _ask_and_finish(self):
        choice = self.ask_save()
        if choice == "save":
            self.btn.configure(text="Saving...")
            self.set_status("Saving and lining up the recordings...")
            rate, subtype = self.fmt()
            opts = (self.auto_align.get(), self.end_clap.get(), rate, subtype)   # Tk variables: main thread
            threading.Thread(target=self._finish_safe, args=(opts,), daemon=True).start()
        else:
            self.btn.configure(text="Deleting...")
            self.set_status("Deleting the recording...")
            threading.Thread(target=self._discard, daemon=True).start()

    def recording_length(self):
        return max((r.frames / r.rate for r in self.finished), default=0)

    def ask_save(self):
        """Modal 'save this recording?' question. Returns 'save' or 'discard'.
        Closing the window or pressing Escape saves, so nothing is lost by accident."""
        secs = int(self.recording_length())
        while True:
            dlg = tk.Toplevel(self.root, bg=BG)
            dlg.title("Save this recording?")
            dlg.transient(self.root)
            dlg.resizable(False, False)
            result = {"v": "save"}
            ttk.Label(dlg, padding=(20, 18, 20, 8), justify="left", font=("Segoe UI", 11), text=(
                f"Recording stopped.  Length: {secs // 60:02d}:{secs % 60:02d}\n\n"
                "Do you want to save this recording?\n"
                "Discarding permanently deletes its files.")).pack()
            bar = ttk.Frame(dlg, padding=(20, 8, 20, 18))
            bar.pack(fill="x")

            def pick(v, d=dlg, r=result):
                r["v"] = v
                d.destroy()
            ttk.Button(bar, text="Discard", command=lambda: pick("discard")).pack(side="left")
            save_btn = ttk.Button(bar, text="Save recording", style="Big.TButton",
                                  command=lambda: pick("save"))
            save_btn.pack(side="right")
            dlg.protocol("WM_DELETE_WINDOW", lambda: pick("save"))
            dlg.bind("<Escape>", lambda e: pick("save"))
            dlg.bind("<Return>", lambda e: pick("save"))
            dlg.update_idletasks()
            x = self.root.winfo_rootx() + (self.root.winfo_width() - dlg.winfo_width()) // 2
            y = self.root.winfo_rooty() + 120
            dlg.geometry(f"+{max(x, 0)}+{max(y, 0)}")
            dlg.grab_set()
            save_btn.focus_set()
            self.root.wait_window(dlg)
            if result["v"] == "save":
                return "save"
            if messagebox.askyesno(APP, "Delete this recording permanently?\nThis cannot be undone.",
                                   icon="warning", default="no"):
                return "discard"
            # they changed their mind: ask again

    def _discard(self):
        shutil.rmtree(self.cur_dir, ignore_errors=True)
        if self.cur_dir.exists():
            msg = f"Could not delete all the files. The folder is: {self.cur_dir}"
            self._post(lambda: self._done(error=msg))
        else:
            self._post(lambda: self._done(discarded=True))

    def _finish_safe(self, opts):
        try:
            result = self._finish(*opts)
            self._post(lambda: self._done(**result))
        except Exception as e:                       # never leave the window stuck on "Saving..."
            self._post(lambda: self._done(error=f"Something went wrong while saving: {e}"))

    def _finish(self, auto_align, end_clap, rate, subtype):
        """Write take.json, check levels, line the mics up. The streams are already stopped."""
        recs, outdir = self.finished, self.cur_dir
        summary = core.write_take_meta(outdir, recs, rate, subtype)
        lines = []
        depth = {v: k for k, v in core.SUBTYPES.items()}[subtype]
        lines.append((f"Originals: as recorded. Aligned copies: {rate} Hz, {depth}.", "ok"))
        for s in summary:
            warn = None
            muted = s.get("muted", 0.0)
            whole = muted >= 0.9 * max(s["seconds"], 0.1)
            if s["seconds"] < 0.5:
                lines.append((f"{s['name']}: recorded NO AUDIO. The device opened but never sent any sound "
                              "(a Bluetooth headset, a device muted or disabled in Windows, or one that "
                              "another program is holding). It is left out of the lining up.", "warn"))
                continue
            if whole:
                lines.append((f"{s['name']}: {s['seconds']:.0f} s, muted for the whole take "
                              "(the file is silent)", "ok"))
                continue
            if s["peak"] >= CLIP:
                warn = ("CLIPPED. Lower this mic's volume and, if it was only the clap "
                        "that clipped, clap more softly or from further away.")
            elif s["peak"] < 0.1:
                warn = "very quiet (peaks below -20 dB). Raise this mic's volume a little."
            note = f", muted for {muted:.0f} s" if muted >= 0.5 else ""
            lines.append((f"{s['name']}: {s['seconds']:.0f} s{note}, peak {to_db(s['peak']):.1f} dB"
                          + (f"  -  {warn}" if warn else ""), "warn" if warn else "ok"))
            for w in s["warnings"]:
                lines.append((f"   driver warning: {w}", "warn"))
            if s["capture_rate"] != rate:
                extra = (f" (this mic records up to {s['capture_rate'] // 2} Hz, so nothing above that)"
                         if s["capture_rate"] < rate else "")
                lines.append((f"   recorded at {s['capture_rate']} Hz; converted to {rate} Hz in the "
                              f"copies{extra}", "ok"))
        report, err, dest = [], None, None
        if auto_align and len(recs) >= 2:
            try:
                dest, rep = core.align_folder(outdir, end_clap, 15.0, rate, subtype)
                report = core.format_report(rep)
            except SystemExit as e:
                err = str(e.code)
            except Exception as e:
                err = f"Aligning failed: {e}"
        converted = None
        if dest is None:                                          # not aligned: still give converted copies
            try:
                converted, notes = core.export_folder(outdir, rate, subtype)
                lines += [(n, "warn") for n in notes]
            except Exception as e:
                lines.append((f"Could not write the converted copies: {e}", "warn"))
        return {"lines": lines, "report": report, "err": err, "dest": dest, "converted": converted}

    def _done(self, lines=(), report=(), err=None, dest=None, error=None, discarded=False, converted=None):
        self.busy = False
        self.recs = []
        self.finished = []
        self.set_sliders_enabled(True)
        self.rebuild_add_menu()
        self.btn.configure(state="normal", text="●  Record")
        self.timer.configure(text="00:00")
        if error:
            self.set_status(error)
            self.write(error, "warn")
            self.start_monitor(keep=True)
            return
        if discarded:
            self.write("Recording discarded.")
            self.set_status("Recording discarded. Ready for the next take.")
            self.start_monitor(keep=True)
            return
        self.take_dir = self.cur_dir
        for text, tag in lines:
            self.write(text, tag)
        if err:
            self.write(f"Could not line up the mics: {err}", "warn")
            self.write("Your recordings are saved; you can still import them into Audacity. "
                       "Try recording again with a clear clap at the start that every mic hears.", "warn")
            if converted:
                self.write(f"Copies in your chosen format (not lined up): {converted}", "ok")
            self.set_status("Saved, but the mics could not be lined up. See the notes below.")
        elif dest:
            self.write("Lined up:", "ok")
            for l in report:
                self.write("  " + l)
            self.write(f"Aligned files: {dest}", "ok")
            self.set_status("Done. Import the files from the 'aligned' folder into Audacity "
                            "(File > Import > Audio).")
        else:
            if converted:
                self.write(f"Copies in your chosen format: {converted}", "ok")
            self.set_status(f"Saved to {self.take_dir}")
        self.open_btn.configure(state="normal")
        self.start_monitor(keep=True)

    # ------------------------------------------------------------------- misc
    def browse(self):
        d = filedialog.askdirectory(initialdir=self.out_dir.get() or str(Path.home()))
        if d:
            self.out_dir.set(d)

    def open_take(self):
        if not self.take_dir:
            return
        target = self.take_dir / "aligned"
        target = target if target.exists() else self.take_dir
        self.open_path(target)

    @staticmethod
    def open_path(path):
        try:
            if sys.platform == "win32":
                os.startfile(str(path))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except Exception:
            pass

    def open_sound_settings(self):
        try:
            if sys.platform == "win32":
                os.startfile("ms-settings:sound")
            else:
                messagebox.showinfo(APP, "Open your system sound settings.")
        except Exception:
            pass

    def save(self):
        mics = {r.dev["name"]: {"on": r.on.get(), "name": r.name.get(), "color": r.color} for r in self.rows}
        old = self.settings.get("mics", {})
        old.update(mics)
        names_now = [r.dev["name"] for r in self.rows]
        order = names_now + [n for n in self.settings.get("order", []) if n not in names_now]
        self.settings.update({"mics": old, "order": order, "out_dir": self.out_dir.get(),
                              "auto_align": self.auto_align.get(), "end_clap": self.end_clap.get(),
                              "out_rate": self.fmt()[0], "out_depth": self.out_depth.get()})
        save_settings(self.settings)

    def on_close(self):
        if self.busy:
            messagebox.showinfo(APP, "Still working on your recording. Please wait a moment, "
                                     "then close the window.")
            return
        if self.recording:
            self.recording = False
            snaps = []
            for r in self.recs:
                try:
                    snaps.append(r.end())
                except Exception:
                    pass
                self.monitors[r.index] = r                  # closed below with the other devices
            self.recs, self.finished = [], snaps
            if self.ask_save() == "save":
                try:
                    self._finish(self.auto_align.get(), self.end_clap.get(), *self.fmt())
                except Exception as e:
                    messagebox.showerror(APP, f"Saving failed: {e}\nThe files are in: {self.cur_dir}")
            else:
                shutil.rmtree(self.cur_dir, ignore_errors=True)
        self.save()
        self.stop_monitor()
        try:
            self.root.after_cancel(self._tick_job)
        except Exception:
            pass
        self.root.destroy()


def main():
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
        try:   # own taskbar identity so Windows shows our icon, not Python's
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("MultiMic.Recorder")
        except Exception:
            pass
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
