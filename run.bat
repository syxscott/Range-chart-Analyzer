@echo off
REM One-click launcher for Range Chart Analyzer (desktop GUI).
cd /d "%~dp0"
python main.py
if errorlevel 1 pause
