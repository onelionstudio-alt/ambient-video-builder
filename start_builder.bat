@echo off
cd /d "%~dp0"
py -3 ambient_video_builder_gui.pyw
if errorlevel 1 (
  echo.
  echo Python 3 was not found. Install it from python.org, then open this file again.
  pause
)
