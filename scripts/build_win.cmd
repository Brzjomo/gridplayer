@echo off
rem Build the Windows packages from a checkout with nothing installed but Git,
rem Python and uv: no just, no wget, no zip, and no Inno Setup unless an
rem installer is wanted -- the portable build is made either way.
rem
rem It runs the same two scripts `just build-win-package` runs, through the Git
rem Bash they are written in. What it configures, what each step produces, what
rem BUILD_ARCH changes and what a previous build leaves behind:
rem dev_docs/build-and-release.md
setlocal

set "ROOT=%~dp0.."
rem the full path without the \scripts\.. in the middle, which is what gets
rem printed and what the scripts are run from
for %%i in ("%ROOT%") do set "ROOT=%%~fi"
set "BUILD_ARCH="

if /i "%~1"=="--help" (
    echo   build_win.cmd             build the win64 packages
    echo   build_win.cmd win32       build the 32-bit ones ^(needs a 32-bit Python^)
    echo.
    echo   The artifacts land in dist\: GridPlayer-^<version^>-^<arch^>-install.exe
    echo   ^(Inno Setup 6 only^), GridPlayer-^<version^>-^<arch^>-portable.zip, and
    echo   the GridPlayer\ folder PyInstaller produced.
    echo   See dev_docs/build-and-release.md.
    exit /b 0
)

if /i "%~1"=="win32" set "BUILD_ARCH=win32"
if /i "%~1"=="win64" set "BUILD_ARCH=win64"

set "BASH=%ProgramFiles%\Git\bin\bash.exe"
if not exist "%BASH%" set "BASH=%ProgramFiles(x86)%\Git\bin\bash.exe"
if not exist "%BASH%" set "BASH=%LocalAppData%\Programs\Git\bin\bash.exe"

if not exist "%BASH%" (
    echo build_win: the build scripts are Bash, and Git Bash is not where Git
    echo            for Windows puts it. https://git-scm.com/download/win
    exit /b 1
)

rem Either of these can bring the interpreter a build is made with: the scripts
rem add uv's own Python to the two names if neither is on PATH.
set "PYTHON="
where python >nul 2>nul && set "PYTHON=python"
if not defined PYTHON where python3 >nul 2>nul && set "PYTHON=python3"
if not defined PYTHON where uv >nul 2>nul && set "PYTHON=uv"

if not defined PYTHON (
    echo build_win: no python, python3 or uv on PATH, and a build makes its own
    echo            virtualenv with one of them.
    exit /b 1
)

cd /d "%ROOT%"

echo build_win: PyInstaller
"%BASH%" -lc "BUILD_ARCH=%BUILD_ARCH% ./scripts/pyinstaller/build_win.sh"
if errorlevel 1 exit /b 1

echo build_win: installer and portable zip
"%BASH%" -lc "BUILD_ARCH=%BUILD_ARCH% ./scripts/windows/build_packages.sh"
if errorlevel 1 exit /b 1

echo.
echo build_win: dist\
dir /b "%ROOT%\dist"
