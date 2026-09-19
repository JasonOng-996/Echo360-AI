@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title EchoLecture AI 2.1
if not exist ".venv\Scripts\python.exe" (
  call install_portable.bat
  if errorlevel 1 goto failed
)
".venv\Scripts\python.exe" app_gui.py
if errorlevel 1 goto failed
exit /b 0
:failed
echo.
echo EchoLecture AI did not start successfully. Please keep this window and copy the error above.
pause
exit /b 1
