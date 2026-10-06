#!/bin/bash

set -e

# What a machine needs for this: Git, Python, and Inno Setup 6 for the
# installer -- which is the only one of the three that is not already there on
# a Windows checkout, and the only step that is skipped when it is missing.
# See dev_docs/build-and-release.md

SCRIPT_DIR="$( cd "$( dirname $0 )" && pwd )"

source "scripts/init_app_vars.sh"

# Get machine architecture
if [ -z "$BUILD_ARCH" ]; then
    BUILD_ARCH=$("$(build_python)" -c "import platform; print('win32' if platform.architecture()[0] == '32bit' else 'win64')")
fi

# Convert c:\path to c:\\path
escapeSubst() { sed 's/[&#\]/\\&/g'; }

if [ "$BUILD_ARCH" = "win32" ]; then
    INSTALLER_SUFFIX="win32-install"
    PORTABLE_SUFFIX="win32-portable"
else
    INSTALLER_SUFFIX="win64-install"
    PORTABLE_SUFFIX="win64-portable"
fi

if [ ! -d "$DIST_DIR/$APP_NAME" ]; then
    die "there is nothing to package: $DIST_DIR/$APP_NAME is not there. Run scripts/pyinstaller/build_win.sh first -- just build-win-pyinstaller runs it too."
fi

ISCC="$(find_iscc || true)"

if [ -z "$ISCC" ]; then
    echo "No Inno Setup 6 found, so no installer: the portable build below needs"
    echo "nothing. Get it from https://jrsoftware.org/isdl.php, or set ISCC to"
    echo "the ISCC.exe it installs, and run this script again."
else
    echo "Building installer"

    cp "$SCRIPT_DIR/installer.iss" "$BUILD_DIR/installer.iss"

    # Update installer.iss for architecture
    if [ "$BUILD_ARCH" = "win32" ]; then
        sed -i 's/ArchitecturesAllowed=x64/ArchitecturesAllowed=x86/g' "$BUILD_DIR/installer.iss"
        sed -i 's/ArchitecturesInstallIn64BitMode=x64/ArchitecturesInstallIn64BitMode=x86/g' "$BUILD_DIR/installer.iss"
        sed -i 's/Flags: nowait postinstall skipifsilent 64bit/Flags: nowait postinstall skipifsilent/g' "$BUILD_DIR/installer.iss"
    fi

    APP_SRC=$(cygpath -w "$DIST_DIR/$APP_NAME" | escapeSubst)

    replace_app_vars "$BUILD_DIR/installer.iss"

    sed -i "s#{APP_SRC}#$APP_SRC#g" "$BUILD_DIR/installer.iss"
    PYTHONPATH="$ROOT_DIR" "$(build_python)" "$SCRIPT_DIR/generate_file_associations.py" "{APP_FILE_ASSOCIATIONS}" "$BUILD_DIR/installer.iss"

    "$ISCC" //O"dist" //F"$APP_NAME-$APP_FILE_VERSION-$INSTALLER_SUFFIX" "$BUILD_DIR/installer.iss"
fi

echo "Building portable zip"

pushd "$DIST_DIR"

mkdir "$APP_NAME/portable_data"

zip_dir "$APP_NAME-$APP_FILE_VERSION-$PORTABLE_SUFFIX.zip" "$APP_NAME"

rmdir "$APP_NAME/portable_data"

popd
