# MultiMic

**Record several separate USB microphones at the same time, one WAV file per mic, lined up automatically.**

Built for ensembles and home studios that use USB mics from the same brand but can't be combined
in the maker's own software (for example one RØDE NT-USB next to NT-USB+ mics). MultiMic is free and
open source. Windows 10 / 11.

- One file per mic, each saved in the mic's **own** sample rate and bit depth, never altered
- Lines the mics up using a clap at the start (and optionally at the end to remove clock drift)
- Mixer-style window: live **In** and **Rec** meters, volume sliders, mute / solo, colours, drag to reorder
- Works with any input device, including Bluetooth headsets (converted as needed)
- Dark theme. Output copies in the sample rate / bit depth you pick (16, 24 or 32-bit float)
- Ready to drag into Audacity

## Download and install

**Easiest:** download `MultiMic.exe` from the [Releases](../../releases) page and double-click it.
Windows may show *"Windows protected your PC"* because the program is not code-signed: click
**More info**, then **Run anyway**.

**From source:** install [Python 3.12](https://www.python.org/downloads/) for Windows, download this
repository (Code, then Download ZIP), open a terminal in the folder and run:

    py -m pip install -r requirements.txt
    py multimic_app.py

(or double-click `run_app.bat` after the first command).

## Quick start

1. Plug in your mics. Open **Available Mics** (top left) and tick the ones to record.
2. Set each mic's volume until its meter reaches the green zone and never the red end.
3. Press **Record**, stay quiet for a moment, **clap once** near all the mics, play, clap again at
   the end, stay quiet for 2 seconds, press **Stop**, and choose **Save**.
4. Import the files in the take's `aligned` folder into Audacity (File > Import > Audio).

## Support the project

MultiMic is free. If it saved you time and you would like to say thanks, you can donate here:
**[paypal.me/MarcusImhoff]**. Bug reports and ideas are welcome in the Issues tab. Support is best-effort.

## Licence

MIT, see [LICENSE](LICENSE). Third-party components: see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
MultiMic is independent and is not affiliated with or endorsed by RØDE.

---

## The easy way: the window app
Double-click **run_app.bat** (first time only, and again after an update: `py -m pip install -r requirements.txt`).

1. Each microphone is a vertical strip, like a mixer channel. Put the strips in the same
   left-to-right order as your mics: drag a strip's coloured bar, or use the arrows.
   The arrangement is remembered. Only the mics you record are shown. Open **Available Mics**
   (top left): it always lists every microphone Windows reports (including virtual inputs such as
   RØDE Connect). Tick the ones to record and untick to hide a strip. Name each mic.
   Click the coloured bar at the top of a strip to give that mic a colour (remembered); press and
   drag the same bar to move the strip. The app uses a dark theme.
2. Each strip has two live meters. **In** shows what the mic hears (always live). **Rec** shows
   what is going into the file: it stays empty (OFF) for a muted mic, or for mics silenced by a solo.
   Check the levels: play at your loudest. The In bar should reach the shaded green zone and never
   touch the red end (CLIPPING! appears). Use the strip's vertical **Volume** slider, the same
   volume as in Windows Sound settings; it is locked while recording so a take stays consistent.
   If a mic shows "volume: n/a", Windows won't let the app control it (use *Windows sound settings*).
   The white peak-hold line, the *Peak* reading and the CLIPPING warning all follow the **Rec** meter,
   so they show what is actually recorded (*Reset peaks* clears them).
   **Any microphone works**, including Bluetooth headsets and other devices that don't support 48 kHz:
   MultiMic opens each device at a rate it accepts (and falls back to another Windows driver if the
   first refuses), shows that rate under the mic's name, and converts it for the aligned copies.
3. **M (mute)**: that mic records silence. **S (solo)**: every other mic records silence.
   Mute wins over solo. They work during a take, with a 10 ms fade so there are no clicks, and the
   take log records when each change happened. Files stay the same length and in sync.
   If a mic is muted or silenced when you press Record, the app warns you first.
   Keep every mic un-muted for the first clap, so the mics can be lined up.
   A mic muted for the whole take is kept as a silent file, and the others are still lined up.
   **Save files as** (Options) sets the sample rate (22.05 to 96 kHz) and bit depth (16-bit, 24-bit,
   32-bit float) of the lined-up copies only. **Your original recordings are never changed.** Each mic
   is recorded in its own format, the one under Windows Sound settings > the mic > Properties > Advanced
   (for example the original NT-USB: 48 kHz, 16-bit; the NT-USB+: 48 kHz, 24-bit). The format each mic
   uses is shown under its name. The copies go in the `aligned` folder (16-bit output is dithered), or in
   a `converted` folder if lining up is switched off. A higher rate adds file size, not detail, so choose
   what your mics really record (usually 44.1 or 48 kHz). To see what Windows reports for each mic,
   run `py volume_win.py formats`.
   The mics stay open from the moment you tick them until you close the app, so pressing Record or
   Stop never switches a device off and on again (Bluetooth headsets drop out when that happens).
4. Press **Record**. Stay quiet for a moment, **clap once** near all the mics, play,
   **clap again** at the end, stay quiet for 2 seconds, press **Stop**.
   Recording ends the moment you press Stop, and the app asks whether to **save** it (button on the
   right) or **discard** it (left; asks you to confirm). Closing the window or pressing Esc saves.
5. If you saved, the files are lined up for you. Press *Open last take folder* and import the files
   in the `aligned` folder into Audacity (File > Import > Audio, select all).

Your mic choices, names and options are remembered.

## Desktop shortcut with the icon
Run once from this folder in PowerShell:

    powershell -ExecutionPolicy Bypass -File .\create_shortcut.ps1

It puts **MultiMic** (two-microphone icon) on your Desktop, wherever Windows keeps it.
The shortcut starts the app without a console window, and uses `dist\MultiMic.exe` automatically
if you have built it. To redraw the icon, run `py make_icon.py` (needs `pip install pillow`).

## One .exe to double-click
Run **build_exe.bat** once. It creates `dist\MultiMic.exe`, which runs without Python and can be
copied to other Windows computers.

## Command line (still works)
    py multimic.py list
    py multimic.py record --devices 34 38 --names plus usb
    py multimic.py align takes\take_YYYYMMDD_HHMMSS --end-clap

## Notes
- Each mic is mono, 48 kHz, 24-bit. The two NT-USB+ units are recorded as plain audio devices,
  without RODE Connect's processing. Close RODE Connect first, otherwise it may keep a mic busy.
- Clap quietly and from 30-50 cm: a clap right at a mic clips easily. Both mics must hear it.
- Alignment: the claps give the starting offset; the audio itself is used to measure and cancel
  clock drift (typically 50-60 ppm between USB mics, about 3 ms per minute).
- Tested: alignment logic and the full window flow with simulated microphones.
  Not tested here: real hardware, and the .exe build (needs Windows).
