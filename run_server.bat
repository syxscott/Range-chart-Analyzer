@echo off
REM Launch the Range Chart Analyzer web server, then open the browser.
cd /d "%~dp0"
start "" http://127.0.0.1:8000/
python server.py --port 8000
if errorlevel 1 pause
