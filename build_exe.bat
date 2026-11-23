@echo off
rem Builds a single MultiMic.exe (with the icon) that runs without Python installed.
cd /d "%~dp0"
py -m pip install -r requirements.txt pyinstaller
if errorlevel 1 goto fail
py -m PyInstaller --noconfirm --onefile --windowed --name MultiMic --icon multimic.ico --collect-submodules pycaw --collect-submodules comtypes --collect-all soxr --add-data "multimic.ico;." multimic_app.py
if errorlevel 1 goto fail
echo.
echo Done. Your program is:  %~dp0dist\MultiMic.exe
pause
exit /b
:fail
echo.
echo The build failed. Copy the messages above and send them to Claude.
pause
