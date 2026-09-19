@echo off
setlocal EnableExtensions
cd /d "%~dp0"
echo EchoLecture AI 2.1 - Installing dependencies
if exist ".venv\Scripts\python.exe" goto deps
call :find_python
if defined ECHO_PYTHON goto create_env
where winget >nul 2>nul
if errorlevel 1 (
  echo Python is unavailable. Install Python 3.12 from https://www.python.org/downloads/windows/ and rerun.
  exit /b 1
)
winget install --id Python.Python.3.12 -e --accept-source-agreements --accept-package-agreements
if errorlevel 1 exit /b 1
call :find_python
if not defined ECHO_PYTHON (
  echo Python was installed. Close this window and run run_app.bat again to refresh PATH.
  exit /b 1
)
:create_env
"%ECHO_PYTHON%" -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)"
if errorlevel 1 (
  echo Python 3.10 or newer is required. Python 3.12 is recommended for this build.
  exit /b 1
)
"%ECHO_PYTHON%" -m venv .venv
if errorlevel 1 exit /b 1
:deps
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 exit /b 1
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 exit /b 1
echo Setup complete. Microsoft Edge is used for institution login.
exit /b 0
:find_python
set "ECHO_PYTHON="
for /f "delims=" %%I in ('py -3.12 -c "import sys; print(sys.executable)" 2^>nul') do set "ECHO_PYTHON=%%I"
if defined ECHO_PYTHON exit /b 0
if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "ECHO_PYTHON=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if defined ECHO_PYTHON exit /b 0
for /f "delims=" %%I in ('py -3 -c "import sys; print(sys.executable)" 2^>nul') do set "ECHO_PYTHON=%%I"
if defined ECHO_PYTHON exit /b 0
for /f "delims=" %%I in ('python -c "import sys; print(sys.executable)" 2^>nul') do set "ECHO_PYTHON=%%I"
exit /b 0
