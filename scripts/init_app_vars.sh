#!/bin/bash

die() {
    printf '%s\n' "$*" >&2
    exit 1
}

# The interpreter a build is made with.
#
# `python3` is the name on macOS and on most Linux distributions, and `python`
# is the name on Windows: python.org, the Microsoft Store and Anaconda all
# install that one and nothing else, which is why creating a build venv used to
# fail on a Windows checkout with a perfectly good Python on PATH.
#
# Being on PATH is not enough to be an interpreter: a Windows checkout whose
# Store Python is turned off still has a `python3` -- a redirector that exits 49
# and prints nothing -- and it is found first. Taking it for one breaks the
# build at the first thing it runs. So a candidate has to run before it is
# chosen, and one that cannot is passed over like one that is not there.
build_python() {
    for candidate in python3 python; do
        if command -v "$candidate" &> /dev/null && "$candidate" -c "" &> /dev/null; then
            printf '%s\n' "$candidate"
            return
        fi
    done

    # uv can bring its own, and is how this checkout is run anyway
    if command -v uv &> /dev/null; then
        uv python find && return
    fi

    die "python3 (or python, or uv) is required to build"
}

# Fetching what a build embeds.
#
# Written for wget, which is not on Windows: Git Bash ships curl and no wget,
# and the Chocolatey package that provides wget is one more thing to install
# before a Windows build will run. Either downloader will do, and a file that
# is already there is left alone -- which is what a second build relies on.
download() {
    URL="$1"
    DESTINATION="$2"

    [ -s "$DESTINATION" ] && return 0

    if command -v wget &> /dev/null; then
        wget -q -O "$DESTINATION" "$URL"
        return
    fi

    if command -v curl &> /dev/null; then
        curl -fsSL -o "$DESTINATION" "$URL"
        return
    fi

    die "wget (or curl) is required to download $URL"
}

# Zipping a directory, keeping the directories inside it.
#
# zip is not on Windows either (Git Bash has unzip and no zip), and the
# portable build is a zip with one empty directory in it -- the data directory
# it looks for beside the executable -- so the archive has to keep directories
# that hold no files. Python is what the build is made with and writes exactly
# that, empty directories included.
zip_dir() {
    ARCHIVE="$1"
    SOURCE="$2"

    if command -v zip &> /dev/null; then
        (cd "$(dirname "$SOURCE")" && zip -qr "$ARCHIVE" "$(basename "$SOURCE")")
        return
    fi

    "$(build_python)" - "$ARCHIVE" "$SOURCE" <<'PYTHON'
import os
import sys
import zipfile

archive, source = sys.argv[1], sys.argv[2]
parent = os.path.dirname(os.path.abspath(source))

with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zipped:
    for directory, _dirnames, filenames in os.walk(source):
        inside = os.path.relpath(directory, parent).replace(os.sep, "/")

        # an entry of its own, so that a directory with nothing in it survives
        zipped.writestr(inside + "/", "")

        for name in filenames:
            zipped.write(os.path.join(directory, name), f"{inside}/{name}")
PYTHON
}

# Where Inno Setup's compiler is, if the machine has one.
#
# Only the installer needs it; the portable build needs nothing. It is not on
# PATH however it was installed, the usual install is a directory with a space
# in it, and a per-user or portable copy is somewhere else again -- so the
# environment gets the first say and the two standard places come after.
find_iscc() {
    if [ -n "$ISCC" ]; then
        [ -x "$ISCC" ] || die "ISCC is set to $ISCC, which is not an executable"

        printf '%s\n' "$ISCC"
        return 0
    fi

    for candidate in \
        "/c/Program Files (x86)/Inno Setup 6/ISCC.exe" \
        "/c/Program Files/Inno Setup 6/ISCC.exe" \
        "$LOCALAPPDATA/Programs/Inno Setup 6/ISCC.exe"; do
        if [ -x "$candidate" ]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done

    if command -v ISCC.exe &> /dev/null; then
        command -v ISCC.exe
        return 0
    fi

    return 1
}

if ! command -v realpath &> /dev/null; then
    realpath() {
        [[ $1 = /* ]] && echo "$1" || echo "$PWD/${1#./}"
    }
fi

if [[ "$OSTYPE" == "darwin"* ]]; then
    command -v gsed >/dev/null 2>&1 || die "gsed is required on macOS. Run ./scripts/macos/_init_local_env.sh"

    sed() {
        command gsed "$@"
    }
fi

normalize_macos_arch() {
    case "$1" in
        arm64)
            printf '%s\n' "arm64"
            ;;
        x86_64|intel64)
            printf '%s\n' "x86_64"
            ;;
        *)
            die "Unsupported macOS architecture: $1"
            ;;
    esac
}

macos_arch_suffix() {
    case "$1" in
        arm64)
            printf '%s\n' "arm64"
            ;;
        x86_64)
            printf '%s\n' "intel64"
            ;;
        *)
            die "Unsupported macOS architecture: $1"
            ;;
    esac
}

replace_app_vars() {
    TARGET_FILE="$1"

    [ ! -f "$TARGET_FILE" ] && return

    VARS=$(env | cut -d '=' -f1 | grep "^APP_")

    while IFS= read -r var; do
        eval replace='$'$var

        [ -z "$replace" ] && die "$var variable is empty!"

        sed -i "s#{$var}#$replace#g" "$TARGET_FILE"
    done <<< "$VARS"
}

copy_with_app_vars() {
    SOURCE_FILE="$1"
    DESTINATION="$2"

    if [ -d "$DESTINATION" ]; then
        DEST_FILE="$(realpath $DESTINATION)/$(basename $SOURCE_FILE)"
    else
        DEST_FILE="$DESTINATION"
    fi

    cp "$SOURCE_FILE" "$DEST_FILE"
    replace_app_vars "$DEST_FILE"
}

# The requirements a build installs, compiled from pyproject.toml.
#
# `just build-requirements` compiles this for the builds that go through
# `just`, and scripts/build_win.cmd is one that does not: it used to die at
# `pip install -r` naming a file nothing had written. Every build script asks
# for it here instead, so the path that skips `just` is not the path that
# breaks. Kept if it is already there, which is what a second build relies on.
#
# Resolved for the interpreter the build will install into, not for whatever
# `uv` finds first. On a checkout whose `.venv` is 3.12 and whose `python` is
# 3.10 -- which is what uv creating a venv and python.org installing one comes
# to -- a universal resolution taken for 3.12 pins a package that needs 3.11,
# and the build venv cannot install it:
#
#   ERROR: No matching distribution found for websockets==17.2
make_requirements() {
    REQUIREMENTS_FILE="$BUILD_DIR/requirements.txt"

    if [ -f "$REQUIREMENTS_FILE" ]; then
        return
    fi

    mkdir -p "$BUILD_DIR"

    uv pip compile "$ROOT_DIR/pyproject.toml" \
        -q --universal --no-annotate --no-header \
        --python "$(build_python)" -o "$REQUIREMENTS_FILE"
}

init_venv() {
    VENV_DIR="$1"

    if [ -d "$VENV_DIR" ]; then
        activate_venv "$VENV_DIR"

        return
    fi

    PYTHON="$(build_python)"

    "$PYTHON" -m venv "$VENV_DIR"

    activate_venv "$VENV_DIR"

    "$PYTHON" -m pip install --upgrade pip
}

activate_venv() {
    VENV_DIR="$1"

    if [ -f "$VENV_DIR/Scripts/activate" ]; then
        . "$VENV_DIR/Scripts/activate"
    else
        . "$VENV_DIR/bin/activate"
    fi
}

# What a frozen build has to be able to import, checked before anything is
# frozen with it.
#
# A build venv holding PyQt5 for some other Python -- which is what happens
# when site-packages is copied between environments, or installed into with
# another interpreter's pip -- freezes into an application that cannot start:
# PyInstaller collects no sip module, says nothing about it, and the payload
# dies on `from PyQt5.QtCore import Qt` with "No module named 'PyQt5.sip'".
# Asking here is a second, and it names the command that fixes it.
check_pyqt() {
    VENV_DIR="$1"

    if [ -f "$VENV_DIR/Scripts/python.exe" ]; then
        VENV_PYTHON="$VENV_DIR/Scripts/python.exe"
    else
        VENV_PYTHON="$VENV_DIR/bin/python"
    fi

    "$VENV_PYTHON" -c "import PyQt5.QtCore, PyQt5.sip" &> /dev/null && return

    die "the build environment in $VENV_DIR cannot import PyQt5, so anything frozen with it would not start. Remove that directory and run this again: rm -rf \"$VENV_DIR\""
}

export ROOT_DIR="$(realpath $(pwd))"

export RESOURCES_DIR="$ROOT_DIR/resources"
export BUILD_DIR="$ROOT_DIR/build"
export DIST_DIR="$ROOT_DIR/dist"
export SCRIPTS_DIR="$ROOT_DIR/scripts"

export APP_MODULE="gridplayer"
export APP_BASE_DIR=$(realpath "./$APP_MODULE")
export APP_REPO_SLUG="vzhd1701/gridplayer"

export APP_DISP_NAME=$(sed -n 's/__display_name__ = "\([^"]*\).*/\1/p' "$APP_BASE_DIR/version.py")
export APP_NAME=$(sed -n 's/__app_name__ = "\([^"]*\).*/\1/p' "$APP_BASE_DIR/version.py")
export APP_ID=$(sed -n 's/__app_id__ = "\([^"]*\).*/\1/p' "$APP_BASE_DIR/version.py")
export APP_VERSION=$(sed -n 's/__version__ = "\([^"]*\).*/\1/p' "$APP_BASE_DIR/version.py")
export APP_VERSION_DATE=$(sed -n 's/__version_date__ = "\([^"]*\).*/\1/p' "$APP_BASE_DIR/version.py")
export APP_AUTHOR=$(sed -n 's/__author_name__ = "\([^"]*\).*/\1/p' "$APP_BASE_DIR/version.py")
export APP_AUTHOR_CONTACT=$(sed -n 's/__author_contact__ = "\([^"]*\).*/\1/p' "$APP_BASE_DIR/version.py")
export APP_URL=$(sed -n 's/__app_url__ = "\([^"]*\).*/\1/p' "$APP_BASE_DIR/version.py")
export APP_BUGTRACKER_URL=$(sed -n 's/__app_bugtracker_url__ = "\([^"]*\).*/\1/p' "$APP_BASE_DIR/version.py")

[[ "$APP_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+ ]] || die "Cannot parse version: $APP_VERSION"

# X.Y.Z alone, for the version fields that take nothing but numbers
export APP_VERSION_NUMERIC="${BASH_REMATCH[0]}"

# A dev snapshot (see scripts/stamp_dev_version.py) gets fixed file names,
# so the download links of the "continuous" pre-release never change,
# notes from the unreleased changes, and AppImage updates from that
# pre-release rather than from the latest stable release
if [[ "$APP_VERSION" == *.dev.* ]]; then
    export APP_FILE_VERSION="dev"
    export APP_CHANGELOG_VERSION="unreleased"
    export APP_UPDATE_RELEASE="continuous"
else
    export APP_FILE_VERSION="$APP_VERSION"
    export APP_CHANGELOG_VERSION="$APP_VERSION"
    export APP_UPDATE_RELEASE="latest"
fi

# a dev snapshot has no tag of its own, it borrows the last release's
export APP_CDN_URL_ROOT="https://cdn.jsdelivr.net/gh/${APP_REPO_SLUG}@v${APP_VERSION_NUMERIC}"

if [[ "$OSTYPE" == "darwin"* ]]; then
    export APP_TARGET_ARCH=$(normalize_macos_arch "${BUILD_MACOS_ARCH:-arm64}")
    export APP_TARGET_ARCH_SUFFIX=$(macos_arch_suffix "$APP_TARGET_ARCH")
fi
