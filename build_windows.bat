@echo off
setlocal EnableExtensions
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  call install_portable.bat
  if errorlevel 1 exit /b 1
)
if not exist ".buildvenv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -m venv .buildvenv
  if errorlevel 1 exit /b 1
)
".buildvenv\Scripts\python.exe" -m pip install -r requirements-build.txt
if errorlevel 1 exit /b 1
".buildvenv\Scripts\python.exe" -m unittest discover -s tests -v
if errorlevel 1 exit /b 1
".buildvenv\Scripts\pyinstaller.exe" --noconfirm --clean EchoLectureAI.spec
if errorlevel 1 exit /b 1
"dist\EchoLectureAI\EchoLectureAI.exe" --self-test
if errorlevel 1 exit /b 1
echo Built: dist\EchoLectureAI\EchoLectureAI.exe
echo Run build_installer.bat to create the installer.
exit /b 0
