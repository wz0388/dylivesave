@echo off
chcp 65001 >nul
setlocal

rem Build the recorder GUI into a single-file exe.
rem Any python works as long as it has tkinter + PyInstaller; the script picks one.
set PY=
where python >nul 2>nul
if %errorlevel%==0 set PY=python
if "%PY%"=="" set PY=%USERPROFILE%\.workbuddy\binaries\python\envs\pyinstaller-build312\Scripts\python.exe

if not exist "%PY%" (
  echo Python not found. Install python3, or edit PY in this file.
  pause
  exit /b 1
)

"%PY%" "%~dp0build_record_exe.py" %*
echo.
pause
