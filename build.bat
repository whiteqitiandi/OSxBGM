@echo off
setlocal EnableDelayedExpansion
REM ------------------------------------------------------------------
REM  OSxBGM one-click build  ->  release\OSxBGM.exe (single file, ffmpeg bundled)
REM
REM  KEEP THIS FILE ASCII-ONLY. cmd.exe on Windows 10/11 mis-seeks inside
REM  batch files that contain multi-byte (e.g. Chinese) characters, in both
REM  UTF-8 and CP950, and then executes random fragments of lines as commands.
REM ------------------------------------------------------------------
cd /d "%~dp0"
title OSxBGM build
echo ============================================================
echo  OSxBGM build  -^>  release\OSxBGM.exe  (single exe, ffmpeg bundled)
echo ============================================================
echo.

REM ---------- 1. Python ----------
set PY=
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY (
    echo [ERROR] Python not found. Install Python 3.9+ from https://www.python.org/downloads/
    echo         and tick "Add python.exe to PATH" during setup, then run this file again.
    goto :fail
)
for /f "tokens=*" %%v in ('%PY% -c "import sys;print(sys.version.split()[0])"') do set PYVER=%%v
echo [1/5] Python %PYVER%

REM ---------- 2. dependencies ----------
echo [2/5] Installing numpy / scipy / pyinstaller ...
%PY% -m pip install --quiet --upgrade pip
%PY% -m pip install --quiet -r requirements.txt pyinstaller || goto :fail

REM ---------- 3. ffmpeg ----------
if not exist ffmpeg.exe if exist OSxBGM.ini (
    REM If OSxBGM.ini already points at an ffmpeg.exe, copy it instead of downloading.
    for /f "usebackq tokens=1,* delims==" %%a in (`findstr /i /b /c:"ffmpeg" OSxBGM.ini`) do (
        set "FFP=%%b"
        REM trim leading/trailing spaces and quotes, keep spaces inside the path
        for /f "tokens=* delims= " %%p in ("!FFP!") do set "FFP=%%p"
        set "FFP=!FFP:"=!"
        if exist "!FFP!" copy /y "!FFP!" ffmpeg.exe >nul
    )
)
if exist ffmpeg.exe (
    echo [3/5] ffmpeg.exe found, skipping download
) else (
    echo [3/5] Downloading ffmpeg (about 90 MB, first time only^) ...
    set "FFURL=https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
    curl -L --progress-bar -o ffmpeg_dl.zip "!FFURL!"
    if errorlevel 1 (
        echo       gyan.dev failed, trying GitHub BtbN ...
        set "FFURL=https://github.com/BtbN/FFmpeg-Builds/releases/latest/download/ffmpeg-master-latest-win64-gpl.zip"
        curl -L --progress-bar -o ffmpeg_dl.zip "!FFURL!" || goto :fail
    )
    if exist ffmpeg_dl rmdir /s /q ffmpeg_dl
    powershell -NoProfile -Command "Expand-Archive -Force -Path 'ffmpeg_dl.zip' -DestinationPath 'ffmpeg_dl'" || goto :fail
    for /r ffmpeg_dl %%f in (ffmpeg.exe) do if exist "%%f" copy /y "%%f" ffmpeg.exe >nul
    rmdir /s /q ffmpeg_dl
    del /q ffmpeg_dl.zip
    if not exist ffmpeg.exe (
        echo [ERROR] ffmpeg.exe not found inside the downloaded archive
        goto :fail
    )
)
.\ffmpeg.exe -version | findstr /i "version" | findstr /v "lib"

REM ---------- 4. PyInstaller ----------
echo [4/5] PyInstaller packaging (first run takes about 1-3 minutes^) ...
if exist build rmdir /s /q build
if exist dist  rmdir /s /q dist
set ICON=
if exist icon.ico set "ICON=--icon icon.ico"
%PY% -m PyInstaller --noconfirm --clean --onefile --windowed --name OSxBGM ^
    --add-binary "ffmpeg.exe;." %ICON% ^
    --exclude-module matplotlib --exclude-module pandas --exclude-module PIL ^
    --exclude-module IPython --exclude-module jupyter --exclude-module pytest ^
    app.py
if errorlevel 1 goto :fail
if not exist dist\OSxBGM.exe goto :fail

REM ---------- 5. release ----------
echo [5/5] Assembling the release folder ...
if not exist release mkdir release
copy /y dist\OSxBGM.exe release\OSxBGM.exe >nul
copy /y README.md release\README.md >nul
if exist build rmdir /s /q build
if exist dist  rmdir /s /q dist
del /q OSxBGM.spec 2>nul
for %%A in (release\OSxBGM.exe) do set SIZE=%%~zA
set /a SIZEMB=!SIZE!/1048576
echo.
echo ============================================================
echo  DONE.  release\OSxBGM.exe  (about !SIZEMB! MB^)
echo  Copy that exe to any Windows PC and run it; no Python or ffmpeg needed.
echo ============================================================
start "" explorer.exe "%~dp0release"
pause
exit /b 0

:fail
echo.
echo ************ BUILD FAILED - please paste the messages above ************
pause
exit /b 1
