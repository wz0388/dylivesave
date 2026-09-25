@echo off
chcp 65001 >nul
setlocal

rem Pick a python: PATH first, then the build venv used for this project.
set PY=
where python >nul 2>nul
if %errorlevel%==0 set PY=python
if "%PY%"=="" if exist "%USERPROFILE%\.workbuddy\binaries\python\envs\pyinstaller-build312\Scripts\python.exe" set PY="%USERPROFILE%\.workbuddy\binaries\python\envs\pyinstaller-build312\Scripts\python.exe"

if "%PY%"=="" (
  echo Python not found. Install python3 and add it to PATH, or edit this file.
  pause
  exit /b 1
)

set /p RID=Douyin room id or live url: 
set /p SECS=Duration in seconds [0 = record until Ctrl+C]: 
if "%SECS%"=="" set SECS=0
set /p WAIT=If offline, max wait seconds [0 = wait forever, -1 = quit]: 
if "%WAIT%"=="" set WAIT=0

%PY% "%~dp0record.py" %RID% --duration %SECS% --wait %WAIT%
echo.
pause
