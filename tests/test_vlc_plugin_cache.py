"""The plugin cache the app is told to trust, and what a bad one costs.

`InstanceVLC` asks VLC not to scan its plugin directory when a cache of it
is there, which is most of what starting a decoder process costs. A cache
written for an empty directory is a stub of a few dozen bytes, and trusting
one is worse than not having it at all: VLC is told not to look, finds
nothing to load, and `libvlc_new` returns nothing -- so every decoder
process dies with "VLC failed to initialize" and nothing says why.

Measured against a build of this app: the plugin set a release ships comes
to about 200 KB of cache, an empty directory comes to 24 bytes, and with
that 24-byte file in place and the plugins all there, no video would play
at all while the app itself started up looking perfectly healthy.
"""

import logging
from pathlib import Path

import pytest
from PyQt5.QtCore import QSettings

from gridplayer.settings import Settings
from gridplayer.vlc_player import vlc
from gridplayer.vlc_player.instance import InstanceVLC

# What a cache written for a directory with no plugins in it comes to.
STUB_BYTES = 24


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path):
    settings = Settings()
    real_settings = settings.settings

    settings.settings = QSettings(str(tmp_path / "test.ini"), QSettings.IniFormat)

    yield

    settings.settings = real_settings


def _init_options(mocker, plugin_path) -> list:
    mocker.patch.object(vlc, "plugin_path", plugin_path)

    instance = InstanceVLC(0, [])
    instance._logger = logging.getLogger("test")

    return instance.init_options


def _with_cache(tmp_path, size) -> Path:
    plugins = tmp_path / "plugins"
    plugins.mkdir()

    (plugins / "plugins.dat").write_bytes(b"\x00" * size)

    return plugins


class TestThePluginCache:
    def test_a_cache_of_a_directory_with_plugins_in_it_is_used(self, tmp_path, mocker):
        plugins = _with_cache(tmp_path, size=200_000)

        assert "--no-plugins-scan" in _init_options(mocker, str(plugins))

    def test_a_stub_of_a_cache_is_not(self, tmp_path, mocker):
        plugins = _with_cache(tmp_path, size=STUB_BYTES)

        assert "--no-plugins-scan" not in _init_options(mocker, str(plugins))

    def test_nothing_to_cache_is_not(self, tmp_path, mocker):
        plugins = tmp_path / "plugins"
        plugins.mkdir()

        assert "--no-plugins-scan" not in _init_options(mocker, str(plugins))

    def test_no_plugin_directory_at_all_is_not(self, mocker):
        assert "--no-plugins-scan" not in _init_options(mocker, None)

    def test_a_plugin_directory_that_is_not_there_is_not(self, tmp_path, mocker):
        assert "--no-plugins-scan" not in _init_options(
            mocker, str(tmp_path / "absent")
        )
