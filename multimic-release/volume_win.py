#!/usr/bin/env python3
"""Read and set a microphone's Windows input volume (the "Volume" slider in
Settings > System > Sound > the microphone).

Uses pycaw (Windows Core Audio). Everything fails safely: if pycaw is missing, the
device can't be matched, or this isn't Windows, find_capture_volume() returns None
and the app simply hides the slider.

Self-test from a terminal (Windows):
    py volume_win.py                       list microphones and their volume
    py volume_win.py "Microphone name" 60  set that microphone to 60 %
    py volume_win.py formats               show each microphone's Windows format (sample rate, bit depth)
"""
import sys
import time
import warnings

# pycaw warns about every unplugged/stale audio endpoint Windows remembers; ignore that noise
warnings.filterwarnings("ignore", message=".*COMError attempting to get property.*")

LAST_ERROR = ""      # why the last find_capture_volume() returned None (for the app to show)


class VolumeControl:
    """Wraps one capture endpoint's IAudioEndpointVolume."""

    def __init__(self, endpoint, name):
        self._ep = endpoint
        self.name = name

    def get(self):
        """Volume as 0.0 .. 1.0."""
        return float(self._ep.GetMasterVolumeLevelScalar())

    def set(self, value):
        value = min(1.0, max(0.0, float(value)))
        self._ep.SetMasterVolumeLevelScalar(value, None)

    def muted(self):
        try:
            return bool(self._ep.GetMute())
        except Exception:
            return False


_cache = (0.0, [])


def _is_active(device):
    """Windows remembers microphones that were plugged in before; only use the plugged-in ones."""
    st = getattr(device, "state", None)
    if st is None:
        return True
    name = getattr(st, "name", None)
    if name:
        return name == "Active"
    try:
        return int(st) == 1
    except Exception:
        return True


def _capture_devices():
    """(friendly name, pycaw device) for each ACTIVE microphone endpoint (cached for 3 s)."""
    global _cache
    if time.time() - _cache[0] < 3 and _cache[1]:
        return _cache[1]
    from pycaw.pycaw import AudioUtilities
    out = []
    for d in AudioUtilities.GetAllDevices():
        dev_id = str(getattr(d, "id", "") or "")
        name = getattr(d, "FriendlyName", None)
        # capture endpoint ids start with {0.0.1.  (playback ones with {0.0.0.)
        if name and dev_id.startswith("{0.0.1.") and _is_active(d):
            out.append((name, d))
    _cache = (time.time(), out)
    return out


def _endpoint_of(device):
    ev = getattr(device, "EndpointVolume", None)          # newer pycaw gives this directly
    if ev is not None:
        return ev
    from ctypes import POINTER, cast                      # older pycaw: activate it ourselves
    from comtypes import CLSCTX_ALL
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    enum = AudioUtilities.GetDeviceEnumerator()
    iface = enum.GetDevice(device.id).Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    return cast(iface, POINTER(IAudioEndpointVolume))


def find_capture_volume(name):
    """VolumeControl for the microphone called `name` (as PortAudio reports it), or None.
    When it returns None, LAST_ERROR says why."""
    global LAST_ERROR
    LAST_ERROR = ""
    if sys.platform != "win32":
        LAST_ERROR = "only available on Windows"
        return None
    try:
        import pycaw  # noqa: F401
    except Exception:
        LAST_ERROR = ("the volume helper is not installed. Run:  py -m pip install -r "
                      "requirements.txt   then restart MultiMic")
        return None
    try:
        devices = _capture_devices()
        want = name.casefold().strip()
        pick = next((d for n, d in devices if n.casefold().strip() == want), None)
        if pick is None:   # PortAudio sometimes shortens or tweaks the name
            hits = [(n, d) for n, d in devices
                    if want in n.casefold() or n.casefold() in want]
            pick = hits[0][1] if len(hits) == 1 else None
        if pick is None:
            LAST_ERROR = ("Windows lists no matching microphone. It lists: "
                          + (", ".join(n for n, _ in devices) or "(none)"))
            return None
        return VolumeControl(_endpoint_of(pick), name)
    except Exception as e:
        LAST_ERROR = f"{type(e).__name__}: {e}"
        return None


def _match(name):
    devices = _capture_devices()
    want = name.casefold().strip()
    pick = next((d for n, d in devices if n.casefold().strip() == want), None)
    if pick is None:
        hits = [(n, d) for n, d in devices if want in n.casefold() or n.casefold() in want]
        pick = hits[0][1] if len(hits) == 1 else None
    return pick


def parse_waveformat(blob):
    """Find a WAVEFORMATEX(TENSIBLE) inside a registry/property blob: (rate, bits, is_float) or None.
    The blob has a small header of unknown length, so try the first few offsets and accept the one
    whose fields are consistent with each other."""
    import struct
    for off in range(0, 40):
        if len(blob) < off + 16:
            break
        tag, ch, rate, avg, align, bits = struct.unpack_from("<HHIIHH", blob, off)
        if tag not in (1, 3, 0xFFFE) or not (1 <= ch <= 16) or bits not in (8, 16, 24, 32, 64):
            continue
        if not (8000 <= rate <= 384000) or align != ch * bits // 8 or avg != rate * align:
            continue
        is_float = tag == 3
        if tag == 0xFFFE and len(blob) >= off + 40:
            valid = struct.unpack_from("<H", blob, off + 18)[0]
            sub = struct.unpack_from("<H", blob, off + 24)[0]        # first 2 bytes of the SubFormat GUID
            is_float = sub == 3
            if 0 < valid <= bits:
                bits = valid
        return rate, bits, is_float
    return None


def native_format(name):
    """(sample rate, bits, is_float) of the microphone's Windows default format (the one under
    Sound settings > Properties > Advanced), or None if it can't be read."""
    if sys.platform != "win32":
        return None
    try:
        import winreg
        pick = _match(name)
        if pick is None:
            return None
        guid = str(pick.id).split("}.")[-1]                  # "{0.0.1.00000000}.{guid}" -> "{guid}"
        key = "\\".join(["SOFTWARE", "Microsoft", "Windows", "CurrentVersion", "MMDevices", "Audio",
                          "Capture", guid, "Properties"])
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as k:
            for i in range(winreg.QueryInfoKey(k)[1]):
                vname, val, typ = winreg.EnumValue(k, i)
                # PKEY_AudioEngine_DeviceFormat = the default format
                if vname.lower().startswith("{f19f064d-082c-4e27-bc73-6882a1bb8e4c}") and typ == winreg.REG_BINARY:
                    fmt = parse_waveformat(bytes(val))
                    if fmt:
                        return fmt
    except Exception:
        return None
    return None


def _main(argv):
    if sys.platform != "win32":
        print("This only works on Windows.")
        return 1
    try:
        devices = _capture_devices()
    except Exception as e:
        print(f"Could not use Windows audio controls: {e}\n"
              "Install the helper with:  py -m pip install -r requirements.txt")
        return 1
    if len(argv) == 2 and argv[1] == "formats":
        for n, _ in devices:
            nf = native_format(n)
            print(f"{n}: " + (f"{nf[0]} Hz, {nf[1]}-bit{' float' if nf[2] else ''}" if nf else "format not readable"))
        return 0
    if len(argv) >= 3:
        vc = find_capture_volume(argv[1])
        if vc is None:
            print(f"No microphone matching: {argv[1]}")
            return 1
        vc.set(float(argv[2]) / 100)
        print(f"{argv[1]}: now {vc.get() * 100:.0f} %")
        return 0
    if not devices:
        print("No active microphones found.")
    for n, d in devices:
        try:
            vol = _endpoint_of(d)
            print(f"{n}: {float(vol.GetMasterVolumeLevelScalar()) * 100:.0f} %")
        except Exception as e:
            print(f"{n}: could not read ({e})")
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
