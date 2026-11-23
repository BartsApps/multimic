@echo off
rem Starts MultiMic without a console window.
cd /d "%~dp0"
where pyw >nul 2>nul
if %errorlevel%==0 (
    start "" pyw multimic_app.py
    exit /b
)
where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw multimic_app.py
    exit /b
)
echo Python was not found. Install it from python.org (tick "Add python.exe to PATH"),
echo then run:  py -m pip install -r requirements.txt
pause
