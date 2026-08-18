@echo off
REM Launch the Range Chart Analyzer web server, then open the browser.
REM M3 fix (REVIEW-2026-11-07): the old script opened the browser BEFORE
REM `python server.py` had bound the port, so a slow startup showed
REM "can't reach this page" until a manual refresh. main.py server owns
REM the browser-open itself (1s timer after the socket is serving).
cd /d "%~dp0"
python main.py server --port 8000
if errorlevel 1 pause
