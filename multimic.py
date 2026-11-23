#!/usr/bin/env python3
"""multimic - record several separate USB microphones at once, one WAV per mic,
then line the takes up afterwards.

Commands
  list                      show input devices
  record                    record the chosen devices into one take folder
  align  <take-folder>      line the files up using a clap at the start
                            (and optionally one at the end to fix drift)

Why align? Each USB mic has its own clock, so they start a few ms apart and
slowly drift. A loud clap near all mics at the start (and end) of the take gives
a shared marker that `align` uses to fix both.
"""
import argparse
import json
import queue
import sys
import threading
import time
from datetime import datetime
from pathlib import Path


# --------------------------------------------------------------------- list
def list_input_devices(sd, show_all=False):
    """Input devices as dicts. On Windows only WASAPI ones unless show_all."""
    apis = sd.query_hostapis()
    out = []
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] < 1:
            continue
        api = apis[d["hostapi"]]["name"]
        if not show_all and sys.platform == "win32" and "WASAPI" not in api:
            continue
        out.append({"index": i, "name": d["name"], "api": api,
                    "channels": d["max_input_channels"], "rate": int(d["default_samplerate"])})
    return out


def _sd():
    try:
        import sounddevice as sd
        return sd
    except Exception as e:  # missing package or PortAudio
        sys.exit(f"Could not load sounddevice ({e}).\nRun: pip install -r requirements.txt")


def cmd_list(args):
    sd = _sd()
    apis = sd.query_hostapis()
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] < 1:
            continue
        api = apis[d["hostapi"]]["name"]
        if not args.all and sys.platform == "win32" and "WASAPI" not in api:
            continue
        print(f"{i:>3}  {d['name']}  [{api}]  in:{d['max_input_channels']}  "
              f"default {int(d['default_samplerate'])} Hz")
    if not args.all and sys.platform == "win32":
        print("\n(showing WASAPI devices only; use --all to see every driver)")


# ------------------------------------------------------------------- record
# Output formats offered to the user (label -> soundfile subtype)
SUBTYPES = {"16-bit": "PCM_16", "24-bit": "PCM_24", "32-bit float": "FLOAT"}
OUT_RATES = [22050, 32000, 44100, 48000, 88200, 96000]
# Rates to try when a device refuses the one we asked for (e.g. a Bluetooth headset mic)
CAPTURE_RATES = [48000, 44100, 32000, 24000, 22050, 16000, 11025, 8000]


def subtype_for_bits(bits, is_float=False):
    """The WAV bit depth that keeps a device's own format exactly: 16 or 24 bit PCM, else 32-bit float."""
    if bits is None:
        return "PCM_24"
    if bits <= 16:
        return "PCM_16"
    if bits <= 24:
        return "PCM_24"
    return "FLOAT"


def _alt_devices(sd, index):
    """Same-named input devices on other host APIs (e.g. MME) as a fallback if WASAPI refuses."""
    try:
        me = sd.query_devices(index)
        key = me["name"].lower()[:28]
        out = []
        for i, d in enumerate(sd.query_devices()):
            if (i != index and d["max_input_channels"] >= 1 and d["hostapi"] != me["hostapi"]
                    and d["name"].lower().startswith(key)):
                out.append(i)
        return out
    except Exception:
        return []


class MicRecorder:
    """One input stream -> one WAV file, written by a background thread."""

    def __init__(self, sd, sf, index, name, rate, outdir, channel, subtype="PCM_24"):
        """`rate` is the rate we would like (None = the device's own rate). If the device cannot do it, the nearest one it
        accepts is used instead; the real capture rate is then self.rate (and is converted to the
        wanted rate when the take is aligned / exported)."""
        self.sd, self.sf = sd, sf
        self.index, self.name, self.rate = index, name, rate
        self.file_subtype = subtype              # bit depth of the original file (the mic's own)
        self.wanted_rate = rate
        self.channel = channel  # which input channel to keep (0-based)
        info = sd.query_devices(index)
        self.dev_name = info["name"]
        self.n_in = max(1, min(info["max_input_channels"], 2))
        self.path = (outdir / f"{name}.wav") if outdir else None   # outdir=None: meter only
        self.q = queue.Queue()
        self.frames = 0
        self.peak = 0.0          # decaying, for meters
        self.max_peak = 0.0      # highest sample seen, for clip warnings (what the mic heard)
        self.rec_peak = 0.0      # highest sample written to the file (or that would be, when only metering)
        self.disp_peak = 0.0     # like rec_peak but resettable from the window's Reset peaks button
        self.out_peak = 0.0      # decaying, for meters: what is going into the file right now
        self.gate_off = False    # True = record silence (mute, or silenced by another mic's solo)
        self.gain = 1.0          # current fade state of the gate, 1 = open, 0 = closed
        self.muted_frames = 0    # how long the file has been silenced
        self.t_start = None       # perf_counter when the stream was started (to spot a dead device)
        self._pending = False     # a recording was prepared with prepare(); the callback starts it
        self._rec = False         # True while audio is being queued for the file
        self.first_cb = None      # perf_counter at first callback
        self.first_adc = None     # estimated perf_counter of first sample
        self.last_cb = None
        self.status_msgs = []
        self.stop_flag = threading.Event()
        self.api_note = ""
        self.stream = self._open_stream(rate)
        self.file = (sf.SoundFile(str(self.path), "w", samplerate=self.rate, channels=1,
                                  subtype=self.file_subtype) if self.path else None)
        self._rec = self.file is not None
        self.thread = threading.Thread(target=self._writer, daemon=True)

    def _cb(self, indata, frames, t, status):
        now = time.perf_counter()
        if self._pending:                        # a take starts exactly here, in the audio thread
            self._pending = False
            self.first_cb = None
            self.frames = 0
            self.status_msgs = []
            self.muted_frames = 0
            self.max_peak = self.rec_peak = self.disp_peak = self.out_peak = 0.0
            self.gain = 0.0 if self.gate_off else 1.0      # start closed if muted: no fade-in click
            self._rec = True
        if self.first_cb is None:
            self.first_cb = now
            # time between the first sample being captured and this callback
            self.first_adc = now - (t.currentTime - t.inputBufferAdcTime)
        self.last_cb = now
        if status:
            self.status_msgs.append(str(status))
        ch = min(self.channel, indata.shape[1] - 1)
        mono = indata[:, ch].copy()
        self.frames += frames
        p = float(abs(mono).max()) if frames else 0.0
        self.peak = max(self.peak * 0.8, p) if p < self.peak else p
        self.max_peak = max(self.max_peak, p)
        out = self._gate(mono)                   # what would go into the file (meters use it too)
        if frames:
            q = float(abs(out).max())
            self.rec_peak = max(self.rec_peak, q)
            self.disp_peak = max(self.disp_peak, q)
            self.out_peak = max(self.out_peak * 0.8, q) if q < self.out_peak else q
        if self._rec:
            self.q.put(out)

    def _open_stream(self, wanted):
        """Open the device, trying other sample rates / channel counts / drivers if needed."""
        sd = self.sd
        errors = []
        devices = [self.index]
        tried_alt = False
        while devices:
            dev = devices.pop(0)
            try:
                d = sd.query_devices(dev)
                api = sd.query_hostapis(d["hostapi"])["name"]
            except Exception as e:
                errors.append(str(e))
                continue
            extra = None
            if sys.platform == "win32" and "WASAPI" in api:
                extra = sd.WasapiSettings(auto_convert=True)   # lets other rates work in shared mode
            n_max = max(1, min(d["max_input_channels"], 2))
            rates = []
            for r in ([wanted] if wanted else []) + [int(d["default_samplerate"])] + CAPTURE_RATES:
                if r not in rates:
                    rates.append(r)
            for ch in ([n_max] + ([1] if n_max > 1 else [])):
                for r in rates:
                    try:
                        st = sd.InputStream(device=dev, channels=ch, samplerate=r, dtype="float32",
                                            callback=self._cb, extra_settings=extra)
                    except Exception as e:
                        errors.append(f"{r} Hz via {api}: {e}")
                        continue
                    self.rate, self.n_in = r, ch
                    self.api_note = "" if dev == self.index else f" (using the {api} driver)"
                    return st
            if not tried_alt:                      # the main driver refused everything: try the others
                tried_alt = True
                devices += _alt_devices(sd, self.index)
        last = errors[0] if errors else "unknown error"
        raise RuntimeError(f"{last}\n(also tried other sample rates and drivers)")

    def _gate(self, x):
        """Apply mute/solo: fade to silence (or back) over 10 ms, then hold. Length never changes,
        so every track stays the same length and in sync."""
        import numpy as np
        target = 0.0 if self.gate_off else 1.0
        if self.gain == target:
            if target == 1.0:
                return x
            self.muted_frames += len(x)
            return np.zeros_like(x)
        step = 1.0 / (0.010 * self.rate)
        ramp = np.arange(1, len(x) + 1, dtype=np.float32) * step
        g = np.minimum(self.gain + ramp, 1.0) if target > self.gain else np.maximum(self.gain - ramp, 0.0)
        self.gain = float(g[-1]) if len(g) else self.gain
        if target == 0.0:
            self.muted_frames += len(x)
        return (x * g).astype(x.dtype)

    def _writer(self):
        while not (self.stop_flag.is_set() and self.q.empty()):
            try:
                self.file.write(self.q.get(timeout=0.2))
            except queue.Empty:
                pass
        self.file.close()

    def start(self):
        self.t_start = time.perf_counter()
        if self.file is not None:
            self.thread.start()
        self.stream.start()

    def stop(self):
        """Close the device (and finish the file, if one is being written)."""
        self.stream.stop()
        self.stream.close()
        self.stop_flag.set()
        if self.file is not None and self.thread.is_alive():
            self.thread.join()

    # A stream that is already open (and showing meters) can start and finish takes without being
    # closed and reopened. That matters for Bluetooth headsets, which drop out when reopened.
    def prepare(self, outdir, name):
        """Create the WAV for a new take on this already-open stream. Call go() to start it."""
        self.name = name
        self.path = Path(outdir) / f"{name}.wav"
        self.q = queue.Queue()
        self.stop_flag = threading.Event()
        self.file = self.sf.SoundFile(str(self.path), "w", samplerate=self.rate, channels=1,
                                      subtype=self.file_subtype)
        self.thread = threading.Thread(target=self._writer, daemon=True)
        self.thread.start()

    def go(self):
        self._pending = True

    def end(self):
        """Finish the take but leave the device open (back to metering only).
        Returns a frozen copy of what the take needs: name, rate, frames, timing, peaks..."""
        from types import SimpleNamespace
        self._rec = False
        self._pending = False
        snap = SimpleNamespace(
            name=self.name, index=self.index, dev_name=self.dev_name, rate=self.rate,
            file_subtype=self.file_subtype,
            frames=self.frames, first_adc=self.first_adc, first_cb=self.first_cb, last_cb=self.last_cb,
            status_msgs=list(self.status_msgs), muted_frames=self.muted_frames,
            max_peak=self.max_peak, rec_peak=self.rec_peak, path=self.path)
        if self.file is not None:
            self.stop_flag.set()
            self.thread.join()                     # the writer drains the queue, then closes the file
            self.file = None
        return snap


def _db(x):
    import math
    return -99.0 if x <= 1e-5 else 20 * math.log10(x)


def write_take_meta(outdir, recs, rate, subtype=None):
    """Save take.json next to the WAVs and return a short summary per mic. `rate` / `subtype` are
    the format the files will be saved in; each mic may have captured at its own rate."""
    meta = {"output_rate": rate, "output_bit_depth": subtype, "tracks": []}
    base = min((r.first_adc for r in recs if r.first_adc), default=0)
    summary = []
    for r in recs:
        dur = (r.last_cb - r.first_cb) if r.first_cb else 0
        meta["tracks"].append({
            "name": r.name, "device_index": r.index, "device": r.dev_name,
            "frames": r.frames, "capture_rate": r.rate,
            "original_format": getattr(r, "file_subtype", "PCM_24"),
            "start_offset_s": (r.first_adc - base) if r.first_adc else None,
            "measured_rate_hz": (r.frames / dur) if dur > 1 else None,
            "driver_warnings": sorted(set(r.status_msgs))[:5],
            "muted_seconds": round(getattr(r, "muted_frames", 0) / r.rate, 2),
        })
        summary.append({"name": r.name, "seconds": r.frames / r.rate, "capture_rate": r.rate,
                        "heard_peak": r.max_peak,                       # what the mic heard
                        "peak": getattr(r, "rec_peak", r.max_peak),     # what is in the file
                        "muted": getattr(r, "muted_frames", 0) / r.rate,
                        "warnings": sorted(set(r.status_msgs))[:3]})
    (Path(outdir) / "take.json").write_text(json.dumps(meta, indent=2))
    return summary


def cmd_record(args):
    sd = _sd()
    import soundfile as sf
    names = args.names or [f"mic{i + 1}" for i in range(len(args.devices))]
    if len(names) != len(args.devices):
        sys.exit("--names must have the same count as --devices")
    outdir = Path(args.out) / datetime.now().strftime("take_%Y%m%d_%H%M%S")
    outdir.mkdir(parents=True, exist_ok=True)

    recs = [MicRecorder(sd, sf, d, n, args.rate, outdir, args.channel)
            for d, n in zip(args.devices, names)]
    print(f"Take folder: {outdir}\n")
    for r in recs:
        print(f"  {r.name:<14} <- [{r.index}] {r.dev_name}")
    print("\nPress Enter to START recording...")
    input()

    t0 = time.perf_counter()
    for r in recs:           # start as close together as possible
        r.start()
    print("RECORDING - clap once, loudly, near all mics now.  Press Enter to stop "
          "(clap again first if you want drift correction).\n")

    stop = threading.Event()
    threading.Thread(target=lambda: (input(), stop.set()), daemon=True).start()
    while not stop.is_set():
        line = "  ".join(f"{r.name}:{_db(r.peak):>6.1f}dB" for r in recs)
        print("\r" + line + f"   {time.perf_counter() - t0:6.1f}s", end="", flush=True)
        time.sleep(0.25)
    print()
    for r in recs:
        r.stop()

    meta = {"rate": args.rate, "tracks": []}
    base = min(r.first_adc for r in recs if r.first_adc)
    for r in recs:
        dur = (r.last_cb - r.first_cb) if r.first_cb else 0
        meta["tracks"].append({
            "name": r.name, "device_index": r.index, "device": r.dev_name,
            "frames": r.frames, "capture_rate": r.rate,
            "start_offset_s": (r.first_adc - base) if r.first_adc else None,
            "measured_rate_hz": (r.frames / dur) if dur > 1 else None,
            "driver_warnings": sorted(set(r.status_msgs))[:5],
        })
        flag = "  PEAK > -1 dB, may have clipped!" if r.peak > 0.89 else ""
        print(f"{r.name}: {r.frames / r.rate:.1f}s at {r.rate} Hz, peak {_db(r.peak):.1f} dB{flag}")
        if r.status_msgs:
            print(f"   driver warnings: {sorted(set(r.status_msgs))[:3]}")
    (outdir / "take.json").write_text(json.dumps(meta, indent=2))
    print(f"\nSaved. Next:  python multimic.py align \"{outdir}\"")


# ------------------------------------------------------------ format conversion
def _prep(x, subtype):
    """Float samples ready for writing: 16-bit output gets TPDF dither, integer output is clipped."""
    import numpy as np
    if subtype == "FLOAT":
        return x
    if subtype == "PCM_16" and len(x):
        rng = np.random.default_rng()
        x = x + ((rng.random(len(x), dtype=np.float32) - rng.random(len(x), dtype=np.float32)) / 32768.0)
    return np.clip(x, -1.0, 0.99997)


def write_wav(path, x, rate, subtype):
    import soundfile as sf
    sf.write(str(path), _prep(x, subtype), rate, subtype=subtype)


def resample_array(x, r_in, r_out):
    """Resample a mono float32 array. Uses soxr (high quality) if installed, else linear interpolation.
    Returns (samples, basic) where basic is True when the lower-quality fallback was used."""
    import numpy as np
    if r_in == r_out:
        return x, False
    try:
        import soxr
        return soxr.resample(x.astype(np.float32), r_in, r_out, quality="VHQ").astype(np.float32), False
    except ImportError:
        n_out = int(round(len(x) * r_out / r_in))
        y = np.interp(np.arange(n_out) * (r_in / r_out), np.arange(len(x)), x)
        return y.astype(np.float32), True


def read_mono(path):
    import soundfile as sf
    data, r = sf.read(str(path), dtype="float32")
    if data.ndim > 1:
        data = data[:, 0]
    return data, r


def export_folder(folder, rate, subtype, dest_name="converted"):
    """Write a copy of every raw WAV at the chosen sample rate / bit depth into <folder>/<dest_name>.
    The raw files themselves are never changed. Returns (dest, notes)."""
    folder = Path(folder)
    dest = folder / dest_name
    dest.mkdir(exist_ok=True)
    notes = []
    for p in sorted(folder.glob("*.wav")):
        x, r = read_mono(p)
        x, basic = resample_array(x, r, rate)
        write_wav(dest / p.name, x, rate, subtype)
        if basic:
            notes.append(f"{p.stem}: basic-quality conversion (run  py -m pip install soxr  for better)")
    return dest, notes


# -------------------------------------------------------------------- align
def find_events(x, rate, start_s, end_s):
    """Sudden loud events in x[start_s:end_s] as a list of onset sample indices.

    An event is a sample that is clearly audible (> 0.04 full scale) and at least
    8x louder than the average of the ~200 ms before it. Hits within 0.3 s are
    grouped into one event. The reported position is the first sample reaching 25%
    of the event's peak, so mics at different levels still agree on the moment."""
    import numpy as np
    a = np.abs(x).astype(np.float64)
    cs = np.concatenate([[0.0], np.cumsum(a)])
    far, near = int(0.20 * rate), int(0.01 * rate)
    idx = np.arange(len(a))
    lo = np.maximum(idx - far, 0)
    hi = np.maximum(idx - near, 0)
    bg = (cs[hi] - cs[lo]) / np.maximum(hi - lo, 1)
    lo_i, hi_i = max(int(start_s * rate), far), int(min(end_s, len(x) / rate) * rate)
    cand = np.nonzero((a > 0.04) & (a > 8 * bg))[0]
    cand = cand[(cand >= lo_i) & (cand < hi_i)]
    if cand.size == 0:
        return []
    gaps = np.nonzero(np.diff(cand) > int(0.3 * rate))[0]
    starts = np.concatenate([[cand[0]], cand[gaps + 1]])
    out = []
    for c in starts:
        win = a[c:c + int(0.03 * rate)]
        out.append(int(c + np.nonzero(win >= 0.25 * win.max())[0][0]))
    return out


def find_clap(x, rate, start_s, end_s, last=False):
    """First (or last) event in one track, or None."""
    ev = find_events(x, rate, start_s, end_s)
    return (ev[-1] if last else ev[0]) if ev else None


def find_common_clap(tracks, rate, start_s, end_s, last=False, tol=0.25):
    """The clap must be heard by EVERY mic. Take the first (or last) event in the
    first track that has a matching event (within `tol` s) in all the others. A
    keypress or bump that only one mic picked up is ignored. Returns {name: sample}."""
    names = list(tracks)
    ev = {n: find_events(tracks[n], rate, start_s, end_s) for n in names}
    cands = ev[names[0]][::-1] if last else ev[names[0]]
    for c in cands:
        found = {names[0]: c}
        for n in names[1:]:
            near = [e for e in ev[n] if abs(e - c) <= tol * rate]
            if not near:
                break
            found[n] = min(near, key=lambda e: abs(e - c))
        else:
            return found
    return None


def refine_clap(ref, x, pr, px, rate, pre=0.02, post=0.15, margin=0.05):
    """The clap detector is only good to a few ms on real recordings (mics hear the
    onset differently). Fix it by sliding the waveform around the reference clap
    along x near px and taking the best match: accurate to about a sample."""
    import numpy as np
    a, b, m = int(pre * rate), int(post * rate), int(margin * rate)
    lo, hi = px - a - m, px + b + m
    if pr - a < 0 or lo < 0 or hi > len(x) or pr + b > len(ref):
        return px
    w, seg = ref[pr - a:pr + b], x[lo:hi]
    n = 1 << int(np.ceil(np.log2(len(seg) + len(w))))
    c = np.fft.irfft(np.fft.rfft(seg, n) * np.conj(np.fft.rfft(w, n)), n)
    c = c[:len(seg) - len(w) + 1]
    return lo + int(np.argmax(c)) + a


def measure_drift_ppm(ref, x, rate, win=2.0, step=3.0, max_lag=0.1):
    """Independent check from the music itself: lag between two tracks at many
    points through the take; the slope of that lag over time is the drift.
    Returns ppm, or None if there isn't enough shared sound to tell."""
    import numpy as np
    W, ml = int(win * rate), int(max_lag * rate)
    pts = []
    for s in np.arange(1.0, len(ref) / rate - win - 1, step):
        i = int(s * rate)
        p, q = ref[i:i + W], x[i:i + W]
        if len(p) < W or len(q) < W:
            break
        if np.sqrt((p ** 2).mean()) < 0.01 or np.sqrt((q ** 2).mean()) < 0.01:
            continue
        n = 1 << int(np.ceil(np.log2(2 * W)))
        c = np.fft.irfft(np.fft.rfft(q, n) * np.conj(np.fft.rfft(p, n)), n)
        c = np.concatenate([c[-ml:], c[:ml + 1]])
        k = int(np.argmax(c))
        if c[k] / np.sqrt((p ** 2).sum() * (q ** 2).sum()) < 0.4:
            continue
        pts.append((s, k - ml))
    if len(pts) < 5 or pts[-1][0] - pts[0][0] < 15:
        return None
    t, l = np.array(pts).T
    fit = np.polyfit(t, l, 1)
    resid = l - np.polyval(fit, t)           # drop outliers and refit once
    keep = np.abs(resid) <= max(3 * np.median(np.abs(resid)), 3)
    if keep.sum() >= 5:
        fit = np.polyfit(t[keep], l[keep], 1)
    return float(fit[0] / rate * 1e6)


def stretch(x, delta):
    """Add (delta>0) or remove (delta<0) |delta| samples spread evenly across x.
    Inaudible for the few-ms corrections drift needs, and keeps the exact length."""
    import numpy as np
    n = abs(int(delta))
    if n == 0:
        return x
    pos = np.linspace(0, len(x) - 1, n + 2)[1:-1].astype(int)
    if delta > 0:
        return np.insert(x, pos, x[pos])
    return np.delete(x, pos)


def _align_live(tracks, rate, search_start=15.0, search_end=15.0, end_clap=False):
    """tracks: {name: float array}. Returns ({name: aligned array}, report)."""
    import numpy as np
    names = list(tracks)
    first = find_common_clap(tracks, rate, 0, search_start)
    if first is None:
        ev = {n: find_events(tracks[n], rate, 0, search_start) for n in names}
        who = ""
        if ev[names[0]]:
            c0 = ev[names[0]][0]
            quiet = [n for n in names[1:] if not any(abs(e - c0) <= 0.25 * rate for e in ev[n])]
            if quiet:
                who = (f" These mics did not hear the clap that {names[0]} heard at "
                       f"{c0 / rate:.1f} s: {', '.join(quiet)} (was one muted?).")
        else:
            who = f" {names[0]} heard no clap at all (was it muted, or was there no clap?)."
        raise SystemExit(f"No clap heard by all mics in the first {search_start:.0f}s." + who +
                         " Clap louder and closer to every mic, keep every mic un-muted for the "
                         "first clap, keep quiet before it, or raise --search.")
    ref = names[0]
    report = {n: {"start_clap_s": first[n] / rate} for n in names}
    spread = (max(first.values()) - min(first.values())) / rate
    if spread > 2.0:
        raise SystemExit(
            "The start claps are %.1f s apart, which is not credible for mics in one room.\n"
            "Detected at: %s\nProbably something else was picked up as the clap. Clap louder, "
            "keep quiet before it, or check the levels." %
            (spread, {n: round(first[n] / rate, 2) for n in names}))

    for n in names[1:]:      # sharpen to ~1 sample using the waveform around the clap
        first[n] = refine_clap(tracks[ref], tracks[n], first[ref], first[n], rate)
        report[n]["start_clap_s"] = first[n] / rate

    # 1) line the first claps up
    out = {}
    anchor = max(first.values())
    for n in names:
        pad = anchor - first[n]
        out[n] = np.concatenate([np.zeros(pad, tracks[n].dtype), tracks[n]])
        report[n]["shift_ms"] = pad / rate * 1000

    # 2) optional drift fix with the end clap
    if end_clap:
        L = min(len(out[n]) for n in names) / rate
        last = find_common_clap(out, rate, max(0, L - search_end), L, last=True)
        if last is None:
            raise SystemExit("No end clap heard by all mics; clap again at the end and stay "
                             "quiet for 2 s afterwards, or skip --end-clap.")
        for n in names[1:]:
            last[n] = refine_clap(out[ref], out[n], last[ref], last[n], rate)
        target = last[ref] - anchor
        for n in names:
            span = last[n] - anchor
            if span <= 0:
                continue
            tail = out[n][anchor:]            # keep everything before the start clap untouched
            if abs(target - span) / span > 0.002:      # > 2000 ppm: not clock drift
                raise SystemExit(
                    "Start-to-end clap distance differs by %.1f ms between %s and %s: too much "
                    "for clock drift, so a wrong clap was probably picked. Run without --end-clap, "
                    "or re-record with clear claps at both ends." %
                    (abs(target - span) / rate * 1000, ref, n))
            if n != ref:   # independent estimate from the audio itself (sign matches drift_ppm)
                m = measure_drift_ppm(out[ref], out[n], rate)
                report[n]["drift_check_ppm"] = None if m is None else -m
            ppm = (target - span) / span * 1e6
            chk = report[n].get("drift_check_ppm")
            report[n]["used_check"] = False
            if chk is not None and abs(chk - ppm) > 150:
                # the end clap is shaky (clipped, or one mic missed it); the audio-wide
                # measurement averages many points, so trust that instead
                ppm = chk
                report[n]["used_check"] = True
            delta = round(ppm * 1e-6 * len(tail))
            out[n] = np.concatenate([out[n][:anchor], stretch(tail, delta)])
            report[n]["drift_ms_fixed"] = delta / rate * 1000
            report[n]["drift_ppm"] = ppm
            report[n]["clap_ppm"] = (target - span) / span * 1e6
            if n != ref:
                m = measure_drift_ppm(out[ref], out[n], rate)
                report[n]["residual_ppm"] = m

    # 3) equal length
    m = max(len(v) for v in out.values())
    for n in names:
        out[n] = np.pad(out[n], (0, m - len(out[n])))
    return out, report


def align_tracks(tracks, rate, search_start=15.0, search_end=15.0, end_clap=False):
    """Line the tracks up by their claps. Tracks that are completely silent (a mic muted for the
    whole take) are left out of the alignment and kept as silence of the same length."""
    import numpy as np
    names = list(tracks)
    empty = [n for n in names if len(tracks[n]) == 0]            # the device never delivered any audio
    silent = [n for n in names if n in empty or float(np.abs(tracks[n]).max()) < 1e-4]
    live = [n for n in names if n not in silent]
    if len(live) >= 2 and not silent:
        return _align_live(tracks, rate, search_start, search_end, end_clap)
    if len(live) >= 2:
        sub, rep = _align_live({n: tracks[n] for n in live}, rate, search_start, search_end, end_clap)
        length = len(next(iter(sub.values())))
    else:                                    # nothing to line up against
        sub = {n: tracks[n] for n in live}
        rep = {n: {"start_clap_s": 0.0, "shift_ms": 0.0, "unaligned": True} for n in live}
        length = max(len(v) for v in tracks.values())
        sub = {n: np.pad(v, (0, length - len(v))) for n, v in sub.items()}
    out, report = {}, {}
    for n in names:
        if n in silent:
            out[n] = np.zeros(length, np.float32)
            report[n] = {"start_clap_s": 0.0, "shift_ms": 0.0, "silent": True, "empty": n in empty}
        else:
            out[n], report[n] = sub[n], rep[n]
    return out, report


def align_folder(folder, end_clap=True, search=15.0, rate=None, subtype="PCM_24"):
    """Align every WAV in a take folder. Writes <folder>/aligned/*.wav (at `rate`, default = the highest
    rate among the files) and returns (aligned_folder, report). The raw files are left as recorded; a mic
    that recorded at another rate is converted. Raises SystemExit(message) if it can't be done."""
    import soundfile as sf
    folder = Path(folder)
    files = sorted(p for p in folder.glob("*.wav"))
    if len(files) < 2:
        raise SystemExit("Need at least two WAV files in that folder.")
    raw = {p.stem: read_mono(p) for p in files}
    rate = rate or max(r for _, r in raw.values())
    tracks = {n: (resample_array(x, r, rate)[0] if len(x) else x) for n, (x, r) in raw.items()}
    del raw
    out, rep = align_tracks(tracks, rate, search, search, end_clap)
    dest = folder / "aligned"
    dest.mkdir(exist_ok=True)
    for n, x in out.items():
        write_wav(dest / f"{n}.wav", x, rate, subtype)
    return dest, rep


def format_report(rep):
    """Plain-text lines describing an align report."""
    lines = []
    for n, r in rep.items():
        if r.get("empty"):
            lines.append(f"{n}: recorded no audio at all, so it was left out (a silent file of the same length "
                         "is kept)")
            continue
        if r.get("silent"):
            lines.append(f"{n}: silent for the whole take (muted), kept as silence of the same length")
            continue
        if r.get("unaligned"):
            lines.append(f"{n}: the only mic with sound, so there was nothing to line it up with")
            continue
        extra = (f", drift fixed {r['drift_ms_fixed']:+.1f} ms ({r['drift_ppm']:+.0f} ppm)"
                 if "drift_ms_fixed" in r else "")
        lines.append(f"{n}: clap at {r['start_clap_s']:.3f}s, shifted {r['shift_ms']:.1f} ms{extra}")
        chk = r.get("drift_check_ppm")
        if r.get("used_check"):
            lines.append(f"    end clap said {r['clap_ppm']:+.0f} ppm but the audio itself says "
                         f"{chk:+.0f} ppm; used the audio (the end clap was probably clipped or unclear)")
        elif chk is not None:
            lines.append(f"    cross-check from the audio: {chk:+.0f} ppm (agrees)")
        elif "drift_ppm" in r and n != list(rep)[0]:
            lines.append("    cross-check: not enough shared sound to measure")
    return lines


def cmd_align(args):
    try:
        dest, rep = align_folder(args.folder, args.end_clap, args.search, args.rate)
    except SystemExit as e:
        sys.exit(e.code)
    print(f"Aligned files written to: {dest}\n")
    for line in format_report(rep):
        print("  " + line)
    print("\nOpen Audacity and drag the files from the aligned folder into one project.")


# --------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list", help="list input devices")
    p.add_argument("--all", action="store_true", help="show every driver type")
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("record", help="record several mics, one WAV each")
    p.add_argument("--devices", type=int, nargs="+", required=True,
                   help="device numbers from `list`")
    p.add_argument("--names", nargs="+", help="file names, e.g. violin cello voice")
    p.add_argument("--rate", type=int, default=48000)
    p.add_argument("--channel", type=int, default=0, help="input channel to keep (default 0)")
    p.add_argument("--out", default="takes")
    p.set_defaults(fn=cmd_record)

    p = sub.add_parser("align", help="line up a take using the clap")
    p.add_argument("folder")
    p.add_argument("--end-clap", action="store_true", help="also fix drift using a clap at the end")
    p.add_argument("--rate", type=int, default=None, help="sample rate of the aligned files (default: highest recorded)")
    p.add_argument("--search", type=float, default=15.0,
                   help="seconds to look for the clap at each end (default 15)")
    p.set_defaults(fn=cmd_align)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
