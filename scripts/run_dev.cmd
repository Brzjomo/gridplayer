@echo off
rem Run GridPlayer from a checkout, or run its tests, with the VLC this
rem machine has -- installed, unpacked, or pointed at from the environment.
rem
rem What it configures, in what order, and the contract it keeps:
rem dev_docs/run-and-test.md
setlocal

set "ROOT=%~dp0.."
rem the full path without the \scripts\.. in the middle, which is what gets
rem printed and handed to python-vlc
for %%i in ("%ROOT%") do set "ROOT=%%~fi"
set "MODE=app"
set "ARGS=%*"

if /i "%~1"=="--help" (
    echo   run_dev.cmd                     run the app from the checkout
    echo   run_dev.cmd media.mp4 more.mkv  run it with those files
    echo   run_dev.cmd --tests             run the whole test suite
    echo   run_dev.cmd --tests -k sync     run the tests that match
    echo.
    echo   VLC is looked for in this order: PYTHON_VLC_LIB_PATH with
    echo   PYTHON_VLC_MODULE_PATH, GRIDPLAYER_VLC_DIR, a normal VLC
    echo   installation, then a vlc-* tree unpacked into build\.
    echo   See dev_docs/run-and-test.md.
    exit /b 0
)

rem Everything after --tests belongs to pytest; everything else to the app.
if /i "%~1"=="--tests" (
    set "MODE=tests"
    for /f "tokens=1,*" %%a in ("%*") do set "ARGS=%%b"
)

where uv >nul 2>nul
if errorlevel 1 (
    echo run_dev: uv is not on PATH, and this checkout is run with it.
    echo           https://docs.astral.sh/uv/getting-started/installation/
    exit /b 1
)

set "VLC_DIR="
set "VLC_FROM_ENV="

if defined PYTHON_VLC_LIB_PATH if defined PYTHON_VLC_MODULE_PATH set "VLC_FROM_ENV=1"

if not defined VLC_FROM_ENV if defined GRIDPLAYER_VLC_DIR if exist "%GRIDPLAYER_VLC_DIR%\libvlc.dll" set "VLC_DIR=%GRIDPLAYER_VLC_DIR%"

if not defined VLC_FROM_ENV if not defined VLC_DIR if exist "%ProgramFiles%\VideoLAN\VLC\libvlc.dll" set "VLC_DIR=%ProgramFiles%\VideoLAN\VLC"

if not defined VLC_FROM_ENV if not defined VLC_DIR if exist "%ProgramFiles(x86)%\VideoLAN\VLC\libvlc.dll" set "VLC_DIR=%ProgramFiles(x86)%\VideoLAN\VLC"

if not defined VLC_FROM_ENV if not defined VLC_DIR for /d %%d in ("%ROOT%\build\vlc-*") do if exist "%%d\libvlc.dll" set "VLC_DIR=%%d"

if not defined VLC_FROM_ENV if not defined VLC_DIR (
    echo run_dev: no VLC found, and nothing plays or is read by sound without it.
    echo.
    echo   Install VLC, or unpack the win64 zip the packaging script embeds:
    echo     https://get.videolan.org/vlc/3.0.24/win64/vlc-3.0.24-win64.zip
    echo     into  build\vlc-3.0.24\
    echo   or point this script at a VLC you already have:
    echo     set GRIDPLAYER_VLC_DIR=C:\path\to\vlc
    exit /b 1
)

if defined VLC_DIR (
    set "PYTHON_VLC_LIB_PATH=%VLC_DIR%\libvlc.dll"
    set "PYTHON_VLC_MODULE_PATH=%VLC_DIR%\plugins"
    echo run_dev: VLC %VLC_DIR%
)

if defined VLC_FROM_ENV echo run_dev: VLC from PYTHON_VLC_LIB_PATH and PYTHON_VLC_MODULE_PATH

cd /d "%ROOT%"

if /i "%MODE%"=="tests" (
    uv run pytest %ARGS%
) else (
    uv run gridplayer %ARGS%
)
