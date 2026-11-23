"""Synthetic test: 3 'mics' hear the same room, with different start offsets and clock drift."""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import numpy as np
from multimic import align_tracks, find_clap

rate = 48000
rng = np.random.default_rng(0)
dur = 60
room = rng.standard_normal(dur * rate)
room = np.convolve(room, np.ones(40) / 40, mode='same')      # smooth, music-like
room = (0.05 * room / room.std()).astype(np.float32)
t0, t1 = 2.0, 57.0                      # start and end claps (seconds, room time)
for t in (t0, t1):
    i = int(t * rate); room[i:i + 200] += 0.9

def mic(offset_s, ppm):
    # mic's clock runs ppm fast/slow; recording starts offset_s late
    n = int((dur - offset_s) * rate * (1 + ppm / 1e6))
    pos = (offset_s * rate) + np.arange(n) / (1 + ppm / 1e6)
    return np.interp(pos, np.arange(len(room)), room).astype(np.float32)

tracks = {"a": mic(0.000, 0), "b": mic(0.137, +60), "c": mic(0.051, -90)}
out, rep = align_tracks(tracks, rate, end_clap=True)
for n in out:
    s = find_clap(out[n], rate, 1, 15)
    e = find_clap(out[n], rate, 40, 60, last=True)
    print(n, "start", s, "end", e, {k:(round(v,1) if isinstance(v,float) else v) for k,v in rep[n].items()})
S = {find_clap(out[n], rate, 1, 15) for n in out}
E = [find_clap(out[n], rate, 40, 60, last=True) for n in out]
assert max(S) - min(S) <= 1, S
assert max(E) - min(E) <= 3, E          # < 0.1 ms
print("OK: claps aligned at both ends")

# decoy: a click only mic "b" hears (keypress), earlier than the real clap
tracks["b"][int(0.5 * rate):int(0.5 * rate) + 100] += 0.3
out, rep = align_tracks(tracks, rate, end_clap=True)
S = {find_clap(out[n], rate, 1, 15) for n in out}
assert max(S) - min(S) <= 1, S
print("OK: decoy click on one mic ignored")

# ---- mute/solo gate: fades without clicks, keeps length ----
from multimic import MicRecorder
r = MicRecorder.__new__(MicRecorder)
r.rate, r.gate_off, r.gain, r.muted_frames = rate, False, 1.0, 0
x = np.full(1024, 0.5, np.float32)
assert (r._gate(x) == x).all()                                  # open: untouched
r.gate_off = True
y = r._gate(x)                                                  # closing: 10 ms fade (480 samples)
assert len(y) == 1024 and abs(y[0] - 0.5) < 0.02 and y[-1] == 0 and (np.diff(y) <= 1e-7).all()
assert 470 < int((y > 0).sum()) < 490, int((y > 0).sum())
assert (r._gate(x) == 0).all() and r.muted_frames >= 2048       # closed: silence, counted
r.gate_off = False
z = r._gate(x)                                                  # opening: fade back in
assert z[0] < 0.02 and abs(z[-1] - 0.5) < 1e-6 and (np.diff(z) >= -1e-7).all()
print("OK: mute/solo gate fades and keeps length")

# ---- a mic muted for the whole take: other mics still align, silent file kept ----
t2 = {"a": tracks["a"], "b": tracks["b"], "m": np.zeros_like(tracks["a"])}
out, rep = align_tracks(t2, rate, end_clap=True)
assert len({len(v) for v in out.values()}) == 1 and not out["m"].any() and rep["m"]["silent"]
S = {find_clap(out[n], rate, 1, 15) for n in ("a", "b")}; assert max(S) - min(S) <= 1
print("OK: fully muted mic kept as silence; the others still line up")
only = align_tracks({"a": tracks["a"], "m": np.zeros_like(tracks["a"])}, rate)[1]
assert only["a"].get("unaligned") and only["m"]["silent"]
print("OK: single live mic handled")

# ---- muted during the start clap: clear message, not a wrong alignment ----
t3 = {k: v.copy() for k, v in tracks.items()}
t3["b"][: int(10 * rate)] = 0                                   # mic b muted for the first 10 s
try:
    align_tracks(t3, rate); raise AssertionError("should refuse")
except SystemExit as e:
    assert "did not hear the clap that a heard at 2.0 s: b (was one muted?)" in str(e), e
    print("OK: message names the muted mic:", str(e)[:110], "...")
