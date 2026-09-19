@echo off
setlocal
cd /d "%~dp0"
if not exist "dist\EchoLectureAI\EchoLectureAI.exe" call build_windows.bat
if errorlevel 1 exit /b 1
set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" (
  echo Inno Setup 6 was not found. Install it, then rerun this file.
  exit /b 1
)
"%ISCC%" installer.iss
