# Third-party software

MultiMic is built on the open-source packages below. Each is used under its own licence; the
licence texts are included with each package (in its pip install folder) and at the links given.
Please check the links for the current licence terms before redistributing.

| Package | Used for | Licence | Link |
|---|---|---|---|
| sounddevice | Microphone input (uses PortAudio) | MIT | https://github.com/spatialaudio/python-sounddevice |
| PortAudio (bundled in sounddevice's Windows build) | Audio drivers | MIT-style | http://www.portaudio.com |
| soundfile (python-soundfile) | Reading and writing WAV files | BSD-3-Clause | https://github.com/bastibe/python-soundfile |
| libsndfile (bundled in soundfile's Windows build) | WAV file library | LGPL-2.1-or-later | https://libsndfile.github.io/libsndfile/ |
| NumPy | Audio maths | BSD-3-Clause | https://numpy.org |
| python-soxr / libsoxr | High-quality sample-rate conversion | LGPL-2.1-or-later | https://github.com/dofuuz/python-soxr |
| pycaw | Windows microphone volume control | MIT | https://github.com/AndreMiras/pycaw |
| comtypes | Windows COM access (used by pycaw) | MIT | https://github.com/enthought/comtypes |
| Pillow | Only used by make_icon.py to draw the icon | HPND (MIT-style) | https://python-pillow.org |
| Python, Tcl/Tk | The runtime and the window toolkit | PSF / BSD-style | https://www.python.org |
| PyInstaller | Only used to build MultiMic.exe | GPL-2.0 with an exception for the programs it builds | https://pyinstaller.org |

## About the LGPL libraries (libsndfile and libsoxr)

These are used unmodified as separate libraries. If you run MultiMic from source (`run_app.bat`),
they are ordinary pip packages that you can replace or upgrade at any time. In the packaged
MultiMic.exe they are included as separate library files inside the bundle, so they can be
replaced too. Their source code is available at the links above.

RØDE, NT-USB and NT-USB+ are trademarks of their owner. MultiMic is an independent project and is
not made, endorsed or supported by RØDE.
