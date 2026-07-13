@echo off
REM Launch the Range Chart Analyzer desktop GUI.
cd /d "%~dp0"
python gui.py
if errorlevel 1 pause
