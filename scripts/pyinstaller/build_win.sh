#!/bin/bash

set -e

SCRIPT_DIR="$( cd "$( dirname $0 )" && pwd )"

. "scripts/init_app_vars.sh"

# Get machine architecture
if [ -z "$BUILD_ARCH" ]; then
    BUILD_ARCH=$(python -c "import platform; print('win32' if platform.architecture()[0] == '32bit' else 'win64')")
fi

VLC_URL="https://get.videolan.org/vlc/3.0.24/$BUILD_ARCH/vlc-3.0.24-$BUILD_ARCH.zip"
PYINSTALLER_VERSION="6.22.3"

mkdir -p "$BUILD_DIR"

make_requirements

init_venv "$BUILD_DIR/venv-pyinstaller"

# Reduce size by installing src version of pydantic
export PIP_NO_BINARY="pydantic"

REQUIREMENTS="$BUILD_DIR/requirements.txt"

# curl-cffi has no 32-bit Windows wheel, and its sdist builds against a
# libcurl DLL that nothing bundles, so yt-dlp goes without browser
# impersonation in that build
if [ "$BUILD_ARCH" = "win32" ]; then
    REQUIREMENTS="$BUILD_DIR/requirements-win32.txt"
    grep -v "^curl-cffi==" "$BUILD_DIR/requirements.txt" > "$REQUIREMENTS"
fi

pip install -r "$REQUIREMENTS"
pip install pyinstaller=="$PYINSTALLER_VERSION"

check_pyqt "$BUILD_DIR/venv-pyinstaller"

# Copy icons to build dir
cp "$RESOURCES_DIR/icons/main/sys/windows.ico" "$BUILD_DIR/main.ico"
cp "$RESOURCES_DIR/icons/playlist/sys/windows.ico" "$BUILD_DIR/mime.ico"

# Make version_info
copy_with_app_vars "$SCRIPT_DIR/version_info.py" "$BUILD_DIR"

copy_with_app_vars "$SCRIPT_DIR/pyinstaller_win.spec" "$BUILD_DIR/$APP_NAME.spec"

pyinstaller --clean --noconfirm "$BUILD_DIR/$APP_NAME.spec"

if [ ! -d "$DIST_DIR/$APP_NAME" ]; then
    die "PyInstaller left no $DIST_DIR/$APP_NAME to put VLC into"
fi

# Post-build
# =============

echo "Embedding VLC"

VLC_EMBED_SRC=$(realpath "$BUILD_DIR/libVLC")

if [ ! -d "$VLC_EMBED_SRC" ]; then
    download "$VLC_URL" "$BUILD_DIR/vlc.zip"

    if [ ! -s "$BUILD_DIR/vlc.zip" ]; then
        die "no VLC to embed: put $VLC_URL at $BUILD_DIR/vlc.zip and run this again"
    fi

    unzip -oq "$BUILD_DIR/vlc.zip" -d "$BUILD_DIR"

    mkdir -p "$VLC_EMBED_SRC/plugins"

    mkdir "$VLC_EMBED_SRC/plugins/audio_output"

    cp "$BUILD_DIR"/vlc-*/plugins/audio_output/libdirectsound_plugin.dll "$VLC_EMBED_SRC/plugins/audio_output"

    # The silent output, which is not silent for the sake of it: lining
    # recordings up by sound reads them by transcoding a snippet to a file,
    # through a player of its own that must not be heard. Nothing else here
    # asks for a second audio output, so without this the probe has none to
    # name and cannot be given one. See vlc_player/audio_probe.py.
    cp "$BUILD_DIR"/vlc-*/plugins/audio_output/libadummy_plugin.dll "$VLC_EMBED_SRC/plugins/audio_output"

    # And the rest of that chain, of which the silent output is only one
    # end. A snippet is written by #transcode into #std, with mux=wav and
    # access=file, so the stream output modules, the wav muxer and the file
    # access output are all needed -- and none of the three is needed to
    # play a video, which is why none of the three was ever here.
    #
    # Measured on a payload built from exactly the list below: leaving out
    # any one of the three directories leaves the probe reading nothing at
    # all, and Align By Sound doing nothing and saying nothing about it.
    mkdir -p "$VLC_EMBED_SRC/plugins/access_output"
    mkdir -p "$VLC_EMBED_SRC/plugins/mux"
    mkdir -p "$VLC_EMBED_SRC/plugins/stream_out"

    cp "$BUILD_DIR"/vlc-*/plugins/access_output/libaccess_output_file_plugin.dll "$VLC_EMBED_SRC/plugins/access_output"
    cp "$BUILD_DIR"/vlc-*/plugins/mux/libmux_wav_plugin.dll "$VLC_EMBED_SRC/plugins/mux"
    cp "$BUILD_DIR"/vlc-*/plugins/stream_out/libstream_out_standard_plugin.dll "$VLC_EMBED_SRC/plugins/stream_out"
    cp "$BUILD_DIR"/vlc-*/plugins/stream_out/libstream_out_transcode_plugin.dll "$VLC_EMBED_SRC/plugins/stream_out"

    cp -a "$BUILD_DIR"/vlc-*/plugins/access "$VLC_EMBED_SRC/plugins"
    cp -a "$BUILD_DIR"/vlc-*/plugins/audio_filter "$VLC_EMBED_SRC/plugins"
    cp -a "$BUILD_DIR"/vlc-*/plugins/audio_mixer "$VLC_EMBED_SRC/plugins"
    cp -a "$BUILD_DIR"/vlc-*/plugins/codec "$VLC_EMBED_SRC/plugins"
    cp -a "$BUILD_DIR"/vlc-*/plugins/demux "$VLC_EMBED_SRC/plugins"
    cp -a "$BUILD_DIR"/vlc-*/plugins/misc "$VLC_EMBED_SRC/plugins"
    cp -a "$BUILD_DIR"/vlc-*/plugins/packetizer "$VLC_EMBED_SRC/plugins"
    cp -a "$BUILD_DIR"/vlc-*/plugins/stream_filter "$VLC_EMBED_SRC/plugins"
    cp -a "$BUILD_DIR"/vlc-*/plugins/video_chroma "$VLC_EMBED_SRC/plugins"
    cp -a "$BUILD_DIR"/vlc-*/plugins/video_output "$VLC_EMBED_SRC/plugins"
    cp -a "$BUILD_DIR"/vlc-*/plugins/d3d9 "$VLC_EMBED_SRC/plugins"
    cp -a "$BUILD_DIR"/vlc-*/plugins/d3d11 "$VLC_EMBED_SRC/plugins"

    mkdir -p "$VLC_EMBED_SRC/plugins/video_filter"
    cp "$BUILD_DIR"/vlc-*/plugins/video_filter/libtransform_plugin.dll "$VLC_EMBED_SRC/plugins/video_filter"

    # Subtitles need both of these, and neither says so when it is missing:
    # the track is decoded and selected, and nothing is ever drawn.
    # blend composites the subpicture onto the frame, scale fits bitmap
    # subtitles (DVD, PGS) to it.
    cp "$BUILD_DIR"/vlc-*/plugins/video_filter/libblend_plugin.dll "$VLC_EMBED_SRC/plugins/video_filter"
    cp "$BUILD_DIR"/vlc-*/plugins/video_filter/libscale_plugin.dll "$VLC_EMBED_SRC/plugins/video_filter"

    # freetype draws the text ones
    cp -a "$BUILD_DIR"/vlc-*/plugins/text_renderer "$VLC_EMBED_SRC/plugins"

    "$BUILD_DIR"/vlc-*/vlc-cache-gen.exe "$VLC_EMBED_SRC/plugins"

    cp -a "$BUILD_DIR"/vlc-*/libvlc.dll "$VLC_EMBED_SRC"
    cp -a "$BUILD_DIR"/vlc-*/libvlccore.dll "$VLC_EMBED_SRC"
fi

# Copied rather than moved: preparing it is the slow half of a build -- 83 MB
# downloaded, unpacked, a plugin cache built -- and a dist that has been wiped
# is no reason to do all of that again. A second build therefore skips the
# whole block above and only puts the plugins in place.
VLC_DEST="$DIST_DIR/$APP_NAME/libVLC"

[ -d "$VLC_DEST" ] && rm -rf "$VLC_DEST"

cp -a "$VLC_EMBED_SRC" "$VLC_DEST"
