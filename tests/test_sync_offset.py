"""Sync offset: lining up recordings that begin at different times.

The offset is the point on one recording's timeline that a common moment
falls on, so two videos are on the same moment when

    t_a - offset_a == t_b - offset_b

which is what every case here is about.

The modules that reach libVLC are imported inside the tests that need them,
so what does not need VLC can still be run on a machine without it.
"""

import logging
from pathlib import Path
from types import MethodType, SimpleNamespace

import pytest
from PyQt5.QtCore import QEvent, QPoint, QPointF, Qt
from PyQt5.QtGui import QKeyEvent, QMouseEvent, QWheelEvent
from PyQt5.QtWidgets import QApplication

from gridplayer.dialogs.align_videos import MILLISECONDS
from gridplayer.models.playlist import Playlist
from gridplayer.models.spectrum_colors import SPECTRUM_COLORS, SpectrumColors
from gridplayer.models.video import Video
from gridplayer.params.actions import ACTIONS
from gridplayer.params.menu import SECTIONS, SUBMENUS
from gridplayer.params.static import SeekSyncMode
from gridplayer.utils.sync_align import ReadingRequest
from gridplayer.utils.sync_audio import (
    BLOCKS_PER_SEC,
    COARSE_SCALE,
    FINE_SNIPPET_MS,
    WIDE_READ_MS,
    Reading,
    common_moment_ms,
)
from gridplayer.widgets.sync_strip import (
    AXIS_HEIGHT,
    OVERLAY_ALPHA,
    PICKED_ALPHA,
    tick_label,
    tick_step_ms,
    ticks_between,
)


@pytest.fixture(scope="module", autouse=True)
def _qapp():
    return QApplication.instance() or QApplication([])


def _has_vlc() -> bool:
    try:
        import gridplayer.vlc_player.libvlc  # noqa: F401
    except Exception:
        return False

    return True


needs_vlc = pytest.mark.skipif(not _has_vlc(), reason="VLC is not installed")


def _sync_offset_txt(offset_ms, fps=None):
    from gridplayer.widgets.video_block import sync_offset_txt

    return sync_offset_txt(offset_ms, fps)


def _parse_sync_offset_txt(text):
    from gridplayer.widgets.video_block import parse_sync_offset_txt

    return parse_sync_offset_txt(text)


# --- the offset as it reads ---


class TestAnOffsetReads:
    @pytest.mark.parametrize(
        ("offset_ms", "expected"),
        [
            (0, "+0:00.000"),
            (5000, "+0:05.000"),
            (75000, "+01:15.000"),
            (3661000, "+01:01:01.000"),
            (-5000, "-0:05.000"),
        ],
    )
    def test_signed_time(self, offset_ms, expected):
        assert _sync_offset_txt(offset_ms) == expected

    def test_frames_where_the_rate_is_known(self):
        # 25 fps, so five seconds is 125 frames
        assert _sync_offset_txt(5000, fps=25.0) == "+0:05.000 (+125f)"

    def test_frames_go_with_the_sign_of_the_offset(self):
        assert _sync_offset_txt(-5000, fps=25.0) == "-0:05.000 (-125f)"

    def test_no_frames_where_the_rate_is_not_known(self):
        assert _sync_offset_txt(5000, fps=None) == "+0:05.000"


class TestAnOffsetTyped:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("1:15.000", 75000),
            ("+1:15.000", 75000),
            ("-0:05", -5000),
            ("  0:05.000  ", 5000),
            ("0", 0),
            ("45", 45000),
        ],
    )
    def test_it_is_read_back(self, text, expected):
        assert _parse_sync_offset_txt(text) == expected

    @pytest.mark.parametrize("text", ["", "not a time", "abc", "1:2:3:4:5:6:7"])
    def test_what_makes_no_sense_is_refused(self, text):
        assert _parse_sync_offset_txt(text) is None

    def test_it_reads_what_it_wrote(self):
        for offset_ms in (0, 5000, -5000, 75000, 3661000):
            assert _parse_sync_offset_txt(_sync_offset_txt(offset_ms)) == offset_ms


# --- the seek the offsets come to ---


class _StubBlock:
    """A video block with just what carrying a seek through it needs."""

    def __init__(self, block_id, offset_ms=0, is_paused=True, time_ms=0):
        self.id = block_id
        self.video_id = block_id
        self.sync_offset_ms = offset_ms
        self.time = time_ms
        self.video_params = SimpleNamespace(is_paused=is_paused)
        self.applied = []

    def apply_sync_position(self, target_ms, is_paused):
        self.applied.append((target_ms, is_paused))


class _PauseSignal:
    """The manager's set_pause, which every video is listening to."""

    def __init__(self):
        self.emitted = []

    def emit(self, is_paused):
        self.emitted.append(is_paused)


def _manager(mode, blocks):
    """The real sync methods, over a context of just what they read."""

    from gridplayer.player.managers.video_blocks import VideoBlocks, VideoBlocksManager

    video_blocks = VideoBlocks()
    for block in blocks:
        video_blocks.append(block)

    class _Manager:
        _sync_offset_source = VideoBlocksManager._sync_offset_source
        seek_sync_offset = VideoBlocksManager.seek_sync_offset
        seek_sync_playback = VideoBlocksManager.seek_sync_playback

        def __init__(self):
            self._ctx = SimpleNamespace(video_blocks=video_blocks, seek_sync_mode=mode)
            self.set_pause = _PauseSignal()

    return _Manager()


def _seek_sync_offset(manager, source_id, time_ms, is_paused=None):
    manager.seek_sync_offset(source_id, time_ms, is_paused)


def _seek_sync_playback(manager, source_id, is_paused):
    manager.seek_sync_playback(source_id, is_paused)


@needs_vlc
class TestCarryingASeekThrough:
    def test_the_moment_is_found_from_the_source_offset(self):
        """The example: three recordings of one thing, started apart.

        Video 1 is on the moment at 5s, video 2 at 1:15, video 3 at 48s, so
        a seek of video 1 to 5s must put video 2 at 1:15 and video 3 at 48s.
        """

        one = _StubBlock("one", offset_ms=5000)
        two = _StubBlock("two", offset_ms=75000)
        three = _StubBlock("three", offset_ms=48000)

        _seek_sync_offset(_manager(SeekSyncMode.OFFSET, [one, two, three]), "one", 5000)

        assert two.applied == [(75000, True)]
        assert three.applied == [(48000, True)]

    def test_the_source_is_left_where_it_is(self):
        one = _StubBlock("one", offset_ms=5000)
        two = _StubBlock("two", offset_ms=75000)

        _seek_sync_offset(_manager(SeekSyncMode.OFFSET, [one, two]), "one", 5000)

        assert one.applied == []

    def test_a_later_moment_moves_every_other_on_by_the_same_amount(self):
        one = _StubBlock("one", offset_ms=0)
        two = _StubBlock("two", offset_ms=70000)

        _seek_sync_offset(_manager(SeekSyncMode.OFFSET, [one, two]), "one", 3000)

        assert two.applied == [(73000, True)]

    def test_offsets_of_their_own_are_not_needed_to_carry_a_seek(self):
        """Unset offsets all count as 0, which is every video on one time."""

        one = _StubBlock("one")
        two = _StubBlock("two")

        _seek_sync_offset(_manager(SeekSyncMode.OFFSET, [one, two]), "one", 1234)

        assert two.applied == [(1234, True)]

    @pytest.mark.parametrize(
        "mode",
        [SeekSyncMode.DISABLED, SeekSyncMode.PERCENT, SeekSyncMode.TIMECODE],
    )
    def test_another_mode_carries_nothing_through_the_offsets(self, mode):
        one = _StubBlock("one")
        two = _StubBlock("two", offset_ms=70000)

        _seek_sync_offset(_manager(mode, [one, two]), "one", 5000)

        assert two.applied == []

    def test_a_source_that_is_gone_carries_nothing(self):
        two = _StubBlock("two")

        _seek_sync_offset(_manager(SeekSyncMode.OFFSET, [two]), "gone", 5000)

        assert two.applied == []

    def test_the_pause_of_the_source_is_carried(self):
        one = _StubBlock("one", is_paused=False)
        two = _StubBlock("two")

        _seek_sync_offset(_manager(SeekSyncMode.OFFSET, [one, two]), "one", 5000)

        assert two.applied == [(5000, False)]


@needs_vlc
class TestCarryingPlaybackThrough:
    """They have to run together: one carrying on while the rest sit where
    they were leaves them a minute apart within a minute."""

    def test_pausing_one_pauses_every_one(self):
        one = _StubBlock("one", is_paused=False)
        two = _StubBlock("two", is_paused=False)
        manager = _manager(SeekSyncMode.OFFSET, [one, two])

        _seek_sync_playback(manager, "one", True)

        assert manager.set_pause.emitted == [True]

    def test_pausing_moves_nobody(self):
        one = _StubBlock("one", is_paused=False)
        two = _StubBlock("two", is_paused=False)
        manager = _manager(SeekSyncMode.OFFSET, [one, two])

        _seek_sync_playback(manager, "one", True)

        assert one.applied == []
        assert two.applied == []

    def test_starting_one_starts_every_one_on_its_own_moment(self):
        one = _StubBlock("one", offset_ms=5000, time_ms=5000)
        two = _StubBlock("two", offset_ms=75000)
        three = _StubBlock("three", offset_ms=48000)
        manager = _manager(SeekSyncMode.OFFSET, [one, two, three])

        _seek_sync_playback(manager, "one", False)

        assert two.applied == [(75000, False)]
        assert three.applied == [(48000, False)]

    def test_starting_from_a_later_moment_carries_the_rest_along(self):
        one = _StubBlock("one", offset_ms=0, time_ms=3000)
        two = _StubBlock("two", offset_ms=70000)
        manager = _manager(SeekSyncMode.OFFSET, [one, two])

        _seek_sync_playback(manager, "one", False)

        assert two.applied == [(73000, False)]

    def test_starting_does_not_inherit_the_source_still_being_paused(self):
        """The source's own flag is what it was a moment ago.

        It is set by VLC reporting back, which has not happened yet at the
        press: reading it here is what left every other video paused while
        the one asked for played on by itself.
        """

        one = _StubBlock("one", is_paused=True, time_ms=1000)
        two = _StubBlock("two", is_paused=True)
        manager = _manager(SeekSyncMode.OFFSET, [one, two])

        _seek_sync_playback(manager, "one", False)

        assert two.applied == [(1000, False)]

    def test_starting_needs_no_blanket_pause_of_its_own(self):
        """Each video is given the state it is to be in by the position it
        is sent to, which is what keeps one with nothing there held."""

        one = _StubBlock("one", time_ms=5000)
        two = _StubBlock("two")
        manager = _manager(SeekSyncMode.OFFSET, [one, two])

        _seek_sync_playback(manager, "one", False)

        assert manager.set_pause.emitted == []

    @pytest.mark.parametrize(
        "mode",
        [SeekSyncMode.DISABLED, SeekSyncMode.PERCENT, SeekSyncMode.TIMECODE],
    )
    def test_another_mode_carries_no_playback_through(self, mode):
        one = _StubBlock("one", is_paused=False)
        two = _StubBlock("two", is_paused=False)
        manager = _manager(mode, [one, two])

        _seek_sync_playback(manager, "one", True)
        _seek_sync_playback(manager, "one", False)

        assert manager.set_pause.emitted == []
        assert two.applied == []

    def test_a_source_that_is_gone_carries_nothing(self):
        two = _StubBlock("two")
        manager = _manager(SeekSyncMode.OFFSET, [two])

        _seek_sync_playback(manager, "gone", False)

        assert manager.set_pause.emitted == []
        assert two.applied == []


# --- dragging a video's sound along the clock ---


class _DraggedBlock:
    """As much of a video block as the three drag methods touch.

    What the dialog says is where the drag would put the sound; what that
    comes to on the recording is the video block's own business, and this is
    a block with nothing of its own but where it is and what it holds.
    """

    def __init__(self, offset_ms=0, time=10000, is_paused=True):
        from gridplayer.widgets.video_block import VideoBlock

        # bound by hand: these are the video block's own methods, and this is
        # not a video block
        self.sync_offset_drag = MethodType(VideoBlock.sync_offset_drag, self)
        self.sync_offset_drop = MethodType(VideoBlock.sync_offset_drop, self)
        self.sync_offset_drag_give_up = MethodType(
            VideoBlock.sync_offset_drag_give_up, self
        )

        self.is_video_initialized = True
        self.is_live = False
        self._drag_offset_ms = None
        self.time = time
        self.positioned = []
        self._log = logging.getLogger("test")

        self.video_params = SimpleNamespace(
            sync_offset_ms=offset_ms or None, is_paused=is_paused
        )

    @property
    def sync_offset_ms(self):
        return self.video_params.sync_offset_ms or 0

    @sync_offset_ms.setter
    def sync_offset_ms(self, offset_ms):
        self.video_params.sync_offset_ms = int(offset_ms) or None

    def apply_sync_position(self, target_ms, is_paused):
        self.positioned.append((target_ms, is_paused))

        # the real one seeks, and a seek leaves the video where it was sent:
        # that is what the next move is measured from
        self.time = target_ms


class TestDraggingASyncPoint:
    """A drag of a video's sound, as the video itself sees it.

    Every step of the drag moves the offset; the picture moves once, when
    the drag is let go of; and a drag given up leaves nothing behind.
    """

    def test_every_step_of_a_drag_moves_the_offset_and_moves_no_picture(self):
        """A seek for every pixel of a drag would be a stutter for every
        pixel of it, so the picture waits."""

        block = _DraggedBlock()

        block.sync_offset_drag(500)
        block.sync_offset_drag(1200)

        assert block.sync_offset_ms == 1200
        assert block.positioned == []

    def test_letting_it_go_moves_the_picture_by_what_the_point_moved_by(self):
        block = _DraggedBlock(offset_ms=2000, time=10000)

        block.sync_offset_drag(3500)
        block.sync_offset_drop()

        # the moment on show is kept: the recording is described rightly and
        # the picture goes where that says it should be
        assert block.positioned == [(11500, True)]

    def test_a_drag_let_go_of_where_nothing_moved_moves_nothing(self):
        block = _DraggedBlock(offset_ms=2000)

        block.sync_offset_drag(2000)
        block.sync_offset_drop()

        assert block.positioned == []

    def test_a_drag_given_up_puts_the_offset_back_and_seeks_nothing(self):
        block = _DraggedBlock(time=10000)

        block.sync_offset_drag(2500)
        block.sync_offset_drag_give_up()

        assert block.sync_offset_ms == 0
        assert block.positioned == []

    def test_a_second_drag_starts_from_where_the_first_left_off(self):
        block = _DraggedBlock(time=10000)

        block.sync_offset_drag(1000)
        block.sync_offset_drop()

        block.sync_offset_drag(1500)
        block.sync_offset_drop()

        assert block.positioned == [(11000, True), (11500, True)]

    def test_a_drag_of_a_video_that_is_not_loaded_does_nothing(self):
        block = _DraggedBlock()
        block.is_video_initialized = False

        block.sync_offset_drag(1000)
        block.sync_offset_drop()

        assert block.sync_offset_ms == 0
        assert block.positioned == []


# --- what gets written down ---


class TestAnOffsetInAPlaylist:
    def test_it_survives_a_round_trip(self):
        playlist = Playlist(videos=[Video(uri="http://host/a", sync_offset_ms=75000)])

        restored = Playlist.parse(playlist.dumps())

        assert restored.videos[0].sync_offset_ms == 75000

    def test_a_negative_one_survives_too(self):
        playlist = Playlist(videos=[Video(uri="http://host/a", sync_offset_ms=-5000)])

        restored = Playlist.parse(playlist.dumps())

        assert restored.videos[0].sync_offset_ms == -5000

    def test_an_unset_one_is_not_written_at_all(self):
        """0 and unset are the same thing, and a playlist is not the place
        to be told so on every video it holds."""

        playlist = Playlist(videos=[Video(uri="http://host/a")])

        assert "sync_offset_ms" not in playlist.dumps()

    def test_an_unset_one_reads_back_as_unset(self):
        playlist = Playlist(videos=[Video(uri="http://host/a")])

        restored = Playlist.parse(playlist.dumps())

        assert restored.videos[0].sync_offset_ms is None

    def test_a_playlist_without_one_reads_back_as_unset(self):
        restored = Playlist.parse(
            '{"format": "gridplayer-playlist", "version": 1,'
            ' "videos": [{"uri": "http://host/a"}]}'
        )

        assert restored.videos[0].sync_offset_ms is None


# --- the tables ---


def _action_ids(items):
    """Every action id in a menu section, however deeply it is nested."""

    for item in items:
        if isinstance(item, tuple):
            yield from _action_ids(item[1:])
        elif item != "---":
            yield item


class TestTheTablesAgree:
    """A typo in a section is a menu that raises when it is opened, which
    no other test would find."""

    def test_every_id_in_a_section_is_a_real_action(self):
        for section_name, section in SECTIONS.items():
            for action_id in _action_ids(section):
                assert action_id in ACTIONS, f"{section_name}: {action_id}"

    def test_every_submenu_in_a_section_is_a_real_submenu(self):
        def submenus(items):
            for item in items:
                if isinstance(item, tuple):
                    yield item[0]
                    yield from submenus(item[1:])

        for section_name, section in SECTIONS.items():
            for submenu in submenus(section):
                assert submenu in SUBMENUS, f"{section_name}: {submenu}"

    def test_every_action_does_something(self):
        for action_id, action in ACTIONS.items():
            assert "func" in action or "menu_generator" in action, action_id


OFFSET_ACTIONS = (
    "Seek Sync (Offset)",
    "Sync Offset: %v",
    "Set Sync Point Here",
    "Shift Sync Point Forward By One Frame",
    "Shift Sync Point Back By One Frame",
    "Shift Sync Point Forward By One Second",
    "Shift Sync Point Back By One Second",
    "Sync Offset Reset",
)


class TestTheOffsetMode:
    def test_it_is_a_seek_sync_mode(self):
        assert SeekSyncMode.OFFSET.value == "offset"

    def test_it_is_offered_in_the_settings_form(self):
        from gridplayer.params.defaults_fields import _seek_sync_modes

        assert SeekSyncMode.OFFSET in _seek_sync_modes()

    @pytest.mark.parametrize("action_id", OFFSET_ACTIONS)
    def test_the_offset_actions_are_there(self, action_id):
        assert action_id in ACTIONS


@needs_vlc
class TestTheOffsetActionsBuild:
    """Every entry through the real factory.

    An entry naming a command no manager publishes looks fine in the table
    and raises when the row is clicked, which nothing else would catch.
    """

    @staticmethod
    def _build(action_id, is_in_frames=True):
        from functools import partial
        from unittest.mock import Mock

        from gridplayer.player.manager import Commands
        from gridplayer.player.managers.actions import ActionsManager

        commands = Commands()
        commands.update(
            {
                "active": lambda command, *args: "+01:15.000",
                "set_seek_sync_mode": lambda mode: None,
                "is_seek_sync_mode_set_to": lambda mode: False,
                "is_active_seekable": lambda: True,
                "is_active_offset_in_frames": lambda: is_in_frames,
            }
        )

        manager = Mock(spec=ActionsManager)
        manager._ctx = Mock()
        manager._ctx.commands = commands
        manager.parent = Mock(return_value=None)
        manager._map_dynamic_functions = partial(
            ActionsManager._map_dynamic_functions, manager
        )

        return ActionsManager._make_action(manager, ACTIONS[action_id])

    @pytest.mark.parametrize("action_id", OFFSET_ACTIONS)
    def test_it_builds(self, action_id):
        assert self._build(action_id) is not None

    def test_the_readout_shows_the_offset_it_is_given(self):
        action = self._build("Sync Offset: %v")
        action.adapt()

        assert action.text() == "Offset: +01:15.000"

    def test_the_mode_row_is_a_tick_box(self):
        action = self._build("Seek Sync (Offset)")
        action.adapt()

        assert action.isCheckable()

    def test_the_frame_rows_are_only_live_where_the_rate_is_known(self):
        forward = "Shift Sync Point Forward By One Frame"

        assert self._build(forward, is_in_frames=True).is_enabled
        assert not self._build(forward, is_in_frames=False).is_enabled

    def test_the_second_rows_need_no_frame_rate(self):
        forward = "Shift Sync Point Forward By One Second"

        assert self._build(forward, is_in_frames=False).is_enabled


# --- the alignment dialog ---


class _DialogBlock:
    """A video block with just what a row of the dialog reads and calls.

    The moves are given `*args` and refuse any, which is the contract the
    dialog has to keep. A video block's own methods are wrapped as taking
    anything, so `clicked` hands them its checked flag and they act on that
    instead of the video; a stub with plain no-argument methods would take
    the flag silently and say nothing about it.
    """

    def __init__(
        self,
        block_id,
        title="a recording",
        offset_ms=0,
        fps=None,
        out_of_range=None,
        uri=None,
    ):
        self.id = block_id
        self.title = title
        self.time = 12345
        self.sync_offset_ms = offset_ms
        self.video_fps = fps
        self.is_video_initialized = True
        self.is_live = False
        self.sync_out_of_range = out_of_range
        self.dragged_from = None
        self.video_params = SimpleNamespace(
            uri_name="a recording",
            uri=Path("/tmp/recording.mp4") if uri is None else uri,
        )
        self.called = []

    @property
    def is_sync_offset_in_frames(self):
        return bool(self.video_fps)

    def get_sync_offset_txt(self):
        return _sync_offset_txt(self.sync_offset_ms, self.video_fps)

    def _no_args(self, args):
        assert not args, f"called with {args}"

    def set_sync_point_here(self, *args):
        self._no_args(args)
        self.called.append(("set_point", None))

    def sync_offset_align_others(self, *args):
        self._no_args(args)
        self.called.append(("align_others", None))

    def sync_offset_shift_frames(self, amount, *args):
        self._no_args(args)
        self.called.append(("shift_frames", amount))

    def sync_offset_shift_ms(self, amount, *args):
        self._no_args(args)
        self.called.append(("shift_ms", amount))

    def sync_offset_reset(self, *args):
        self._no_args(args)
        self.called.append(("reset", None))

    def sync_offset_drag(self, offset_ms, *args):
        """The offset follows the drag; the picture does not, until it is let
        go of: see VideoBlock.sync_offset_drag."""

        self._no_args(args)
        self.called.append(("drag_offset", offset_ms))

        if self.dragged_from is None:
            self.dragged_from = self.sync_offset_ms

        self.sync_offset_ms = offset_ms

    def sync_offset_drop(self, *args):
        self._no_args(args)
        self.called.append(("drop_offset", None))

        # the real one takes the video to the moment the offset now says
        self.time += self.sync_offset_ms - self.dragged_from
        self.dragged_from = None

    def sync_offset_drag_give_up(self, *args):
        self._no_args(args)
        self.called.append(("give_up_drag", None))

        self.sync_offset_ms = self.dragged_from
        self.dragged_from = None

    def sync_offset_set(self, offset_ms, *args):
        self._no_args(args)
        self.called.append(("set_offset", offset_ms))

        # the real one moves the video on by what the offset moved by, so
        # that it goes on showing the moment of the shared clock it was
        # showing: see VideoBlock.sync_offset_set
        self.time += offset_ms - self.sync_offset_ms
        self.sync_offset_ms = offset_ms


class TestTheAlignmentDialog:
    @pytest.fixture(autouse=True)
    def _settings(self, mocker):
        """The settings, in memory.

        Which of the sizes a row's buttons move by is kept between dialogs,
        and a test that moves it is not something to write into the machine
        running it.
        """

        settings = mocker.MagicMock()
        settings.get.return_value = 0

        mocker.patch("gridplayer.dialogs.align_videos.Settings", return_value=settings)

        return settings

    @staticmethod
    def _dialog(*blocks, provider=None, is_offset_mode=None, set_offset_mode=None):
        from gridplayer.dialogs.align_videos import AlignVideosDialog

        if provider is None:

            def provider():
                return list(blocks)

        return AlignVideosDialog(
            provider,
            is_offset_mode=is_offset_mode,
            set_offset_mode=set_offset_mode,
        )

    @staticmethod
    def _button(dialog, block_id, text):
        from PyQt5.QtWidgets import QPushButton

        moves = dialog._rows[block_id]["moves"]

        return next(b for b in moves.findChildren(QPushButton) if b.text() == text)

    @staticmethod
    def _press(button):
        """Press it, because that is what it acts on.

        `click()` would emit `clicked`, which these buttons are deliberately
        not connected to: a release that anything takes away between it and
        the press is a click that never happens. A real press is what has to
        reach the video.
        """

        from PyQt5.QtTest import QTest

        QTest.mousePress(button, Qt.LeftButton)

    def test_it_is_not_modal(self):
        """The videos are moved by hand while it is open."""

        dialog = self._dialog(_DialogBlock("a"))

        assert not dialog.isModal()

        dialog.close()

    def test_it_lists_a_row_per_video(self):
        dialog = self._dialog(_DialogBlock("a"), _DialogBlock("b"))

        assert list(dialog._rows) == ["a", "b"]

        dialog.close()

    def test_the_row_shows_where_the_video_is_and_its_offset(self):
        dialog = self._dialog(_DialogBlock("a", offset_ms=75000, fps=25.0))

        assert dialog._rows["a"]["position"].text() == "0:12.345"
        assert dialog._rows["a"]["offset"].text() == "+01:15.000 (+1875f)"

        dialog.close()

    def test_the_row_says_how_far_past_the_recording_the_moment_lies(self):
        """Read off the tooltip, which holds it whole.

        The label is elided to its column, and how much of it survives that
        is a matter of the font the machine happens to have.
        """

        early = _DialogBlock("a", fps=25.0, out_of_range=(True, 5000))
        # an exact number of frames, so what is read back is not also a
        # question of how a half is rounded
        late = _DialogBlock("b", fps=25.0, out_of_range=(False, 12000))
        dialog = self._dialog(early, late)

        assert dialog._rows["a"]["state"].toolTip() == "starts in 0:05.000 (125f)"
        assert dialog._rows["b"]["state"].toolTip() == "ended 0:12.000 (300f) ago"

        dialog.close()

    def test_a_video_with_nothing_amiss_says_nothing(self):
        dialog = self._dialog(_DialogBlock("a"))

        assert dialog._rows["a"]["state"].text() == ""

        dialog.close()

    def test_a_video_with_no_frame_rate_offers_no_move_in_frames(self):
        """A size in frames needs a frame rate to be worth anything, and one
        of those is the video's to say."""

        dialog = self._dialog(_DialogBlock("a", fps=None))

        dialog._step_combo.setCurrentIndex(dialog._step_combo.findText("1 frame"))

        assert not self._button(dialog, "a", "-").isEnabled()
        assert not self._button(dialog, "a", "+").isEnabled()

        dialog._step_combo.setCurrentIndex(dialog._step_combo.findText("1 ms"))

        assert self._button(dialog, "a", "-").isEnabled()
        assert self._button(dialog, "a", "+").isEnabled()

        dialog.close()

    def test_a_video_with_a_frame_rate_offers_the_move_in_frames(self):
        dialog = self._dialog(_DialogBlock("a", fps=25.0))

        dialog._step_combo.setCurrentIndex(dialog._step_combo.findText("1 frame"))

        assert self._button(dialog, "a", "+").isEnabled()

        dialog.close()

    def test_the_sizes_offered_run_from_a_millisecond_to_ten_frames(self):
        """Milliseconds first, since that is what is left to move once a grid
        has been lined up by sound, and frames last, since they are the ones
        a recording need not have a rate for."""

        dialog = self._dialog(_DialogBlock("a"))

        assert [
            dialog._step_combo.itemText(index)
            for index in range(dialog._step_combo.count())
        ] == ["1 ms", "10 ms", "100 ms", "1 s", "10 s", "1 frame", "10 frames"]

        dialog.close()

    def test_the_size_being_moved_by_is_the_one_that_was_left_set(self, mocker):
        """A dialog opened again opens on the size it was left on, rather
        than on the smallest one every time."""

        settings = mocker.MagicMock()
        settings.get.return_value = 3

        mocker.patch("gridplayer.dialogs.align_videos.Settings", return_value=settings)

        dialog = self._dialog(_DialogBlock("a"))

        assert dialog._step_combo.currentIndex() == 3
        assert dialog._step() == (1_000, MILLISECONDS)

        dialog._step_combo.setCurrentIndex(1)

        assert settings.set.call_args == mocker.call("playlist/sync_nudge_step", 1)

        dialog.close()

    def test_a_size_past_the_end_of_the_list_is_taken_as_the_last_one(self, mocker):
        """The list may have grown or shrunk since the size was written
        down, and a settings file is not something to refuse to open."""

        settings = mocker.MagicMock()
        settings.get.return_value = 99

        mocker.patch("gridplayer.dialogs.align_videos.Settings", return_value=settings)

        dialog = self._dialog(_DialogBlock("a"))

        assert dialog._step_combo.currentIndex() == dialog._step_combo.count() - 1

        dialog.close()

    def test_the_hints_are_the_steps_in_the_order_they_are_done(self):
        """What the dialog is for is a procedure, and a procedure has an order
        to it: read the sound, look at what came of it, move what is still out
        by hand, and colour what colour is the only way to tell apart.

        A list, numbered by the widget rather than by the words, so that
        reordering or adding a step does not mean renumbering a translation --
        and each control named by its own label, since a hint that says one
        thing while the button says another is worse than no hint.
        """

        dialog = self._dialog(_DialogBlock("a"))

        hints = dialog._hints.text()

        assert hints.startswith("<ol>")
        assert hints.count("<li>") == 6

        order = [
            hints.index("Press"),
            hints.index("strip at the top"),
            hints.index("Click"),
            hints.index("look along"),
            hints.index("press - or +"),
            hints.index("Double-click"),
        ]

        assert order == sorted(order)

        # the words the controls are labelled with, and not new ones
        assert dialog._auto_button.text() in hints
        assert dialog._read_button.text() in hints
        assert "Working on" in hints
        assert "Move by" in hints
        assert dialog._fit_button.text() in hints

        # and what the sound is looked for over, which is the one thing about
        # the automatic path a viewer cannot see for themselves
        assert "ten minutes either side" in hints

        dialog.close()

    def test_reading_the_sound_moves_nothing(self):
        """The strips have nothing to draw until the sound has been read, and
        reading it in order to look at it should not line anything up or move
        a single offset: looking is not deciding."""

        one = _DialogBlock("a", uri=Path("/tmp/a.mp4"))
        two = _DialogBlock("b", uri=Path("/tmp/b.mp4"))

        measure = _StubMeasure()
        dialog = TestLiningUpBySound._dialog_with(one, two, measure=measure)

        self._press(dialog._read_button)

        # the snippet the strips draw, and not the minutes either side of it:
        # a look at the sound is not a search over it
        assert measure.started == [
            [
                ReadingRequest(
                    block_id="a",
                    uri=str(Path("/tmp/a.mp4")),
                    center_ms=12345,
                    snippet_ms=FINE_SNIPPET_MS,
                    blocks_per_sec=BLOCKS_PER_SEC,
                ),
                ReadingRequest(
                    block_id="b",
                    uri=str(Path("/tmp/b.mp4")),
                    center_ms=12345,
                    snippet_ms=FINE_SNIPPET_MS,
                    blocks_per_sec=BLOCKS_PER_SEC,
                ),
            ]
        ]

        assert not dialog._auto_button.isEnabled()
        assert not dialog._read_button.isEnabled()

        dialog._measured_sound(
            {
                "a": Reading(_sound(), 1000),
                "b": Reading(_heard_later(_sound(), HALF_A_SECOND), 3000),
            }
        )

        # read, kept for the strips, and nothing judged or moved
        assert sorted(dialog._readings) == ["a", "b"]
        assert one.called == []
        assert two.called == []
        assert "Nothing has been moved" in dialog._sound_note.text()

        # the ruler is measured from the video being worked on, there being no
        # recording the rest were judged against to take it from
        assert dialog._zero_ms == dialog._sound_of("a", {"a": one}).read_around_ms

        assert dialog._auto_button.isEnabled()
        assert dialog._read_button.isEnabled()

        dialog.close()

    def test_the_tick_turns_sync_offset_on_and_off(self):
        """The mode is needed by this dialog and the menu that owns it is
        behind the dialog, so it is turned on and off from here.

        The mode is the setting's, and the tick is told what it is rather than
        the other way round: what is asserted here is that clicking it asks
        for the mode to change, and that it shows the change once it has.
        """

        is_on = [False]
        asked = []

        def set_mode(value):
            asked.append(value)
            is_on[0] = value

        dialog = self._dialog(
            _DialogBlock("a"),
            _DialogBlock("b"),
            is_offset_mode=lambda: is_on[0],
            set_offset_mode=set_mode,
        )

        assert not dialog._mode_check.isChecked()
        assert "Sync Offset" in dialog._mode_note.text()

        dialog._mode_check.setChecked(True)

        assert asked == [True]
        assert is_on[0] is True

        # and the note goes as soon as the mode is on
        dialog._refresh()

        assert dialog._mode_check.isChecked()
        assert dialog._mode_note.isHidden()

        dialog._mode_check.setChecked(False)

        assert asked == [True, False]
        assert is_on[0] is False

        dialog.close()

    def test_the_tick_follows_the_mode_being_changed_elsewhere(self):
        """The menu can turn it on while this is open, and a tick that went on
        saying otherwise would be the dialog disagreeing with the grid."""

        is_on = [False]

        dialog = self._dialog(
            _DialogBlock("a"),
            _DialogBlock("b"),
            is_offset_mode=lambda: is_on[0],
            set_offset_mode=lambda _is_on: None,
        )

        assert not dialog._mode_check.isChecked()

        is_on[0] = True
        dialog._refresh()

        assert dialog._mode_check.isChecked()
        assert dialog._mode_note.isHidden()

        dialog.close()

    def test_a_row_writes_nothing_down_by_hand(self):
        """Where a video is, as a sync point, is the right-click menu's
        business: what this dialog does with a video is line it up, by sound
        or by dragging its own sound until it sits where the others' does."""

        from PyQt5.QtWidgets import QPushButton

        block = _DialogBlock("a")
        dialog = self._dialog(block)

        moves = dialog._rows["a"]["moves"]

        assert "Set point" not in [
            one.text() for one in moves.findChildren(QPushButton)
        ]

        # and nothing on the row reaches the block but the moves, by the
        # smallest size the list offers
        for one in moves.findChildren(QPushButton):
            self._press(one)

        assert block.called == [
            ("shift_ms", -1),
            ("shift_ms", 1),
            ("reset", None),
        ]

        dialog.close()

    def test_the_one_being_worked_on_is_the_first_video_until_it_is_changed(self):
        dialog = self._dialog(_DialogBlock("a"), _DialogBlock("b"))

        assert dialog._align_to == "a"

        dialog.close()

    def test_exactly_one_video_is_the_one_being_worked_on(self):
        """The video a drag moves has to be one video: a drag that meant two
        things at once would mean nothing."""

        dialog = self._dialog(_DialogBlock("a"), _DialogBlock("b"), _DialogBlock("c"))

        assert self._checked(dialog) == ["a"]

        dialog._rows["c"]["align_to"].setChecked(True)

        assert self._checked(dialog) == ["c"]

        # and the one switched off must not be taken for the one switched on
        assert dialog._align_to == "c"

        dialog.close()

    def test_the_video_being_worked_on_is_the_one_the_overview_lifts_out(self):
        one = _DialogBlock("a")
        two = _DialogBlock("b")
        dialog = self._dialog(one, two)

        dialog._measured({"a": Reading(_sound(), 0), "b": Reading(_sound(), 0)})
        dialog._rows["b"]["align_to"].setChecked(True)

        assert [drawn.is_picked for drawn in dialog._overview._drawn] == [False, True]

        dialog.close()

    @staticmethod
    def _checked(dialog) -> list:
        return [
            block_id
            for block_id, row in dialog._rows.items()
            if row["align_to"].isChecked()
        ]

    def test_each_move_moves_only_the_video_it_is_beside(self):
        one = _DialogBlock("a", fps=25.0)
        two = _DialogBlock("b", fps=25.0)
        dialog = self._dialog(one, two)

        # one size in frames, and one in milliseconds: the size is set for the
        # dialog, and which video a press reaches is what is being asked here
        dialog._step_combo.setCurrentIndex(dialog._step_combo.findText("10 frames"))

        self._press(self._button(dialog, "a", "+"))

        dialog._step_combo.setCurrentIndex(dialog._step_combo.findText("1 s"))

        self._press(self._button(dialog, "b", "+"))

        assert one.called == [("shift_frames", 10)]
        assert two.called == [("shift_ms", 1_000)]

        dialog.close()

    @pytest.mark.parametrize(
        ("size", "expected"),
        [
            ("1 ms", ("shift_ms", 1)),
            ("10 ms", ("shift_ms", 10)),
            ("100 ms", ("shift_ms", 100)),
            ("1 s", ("shift_ms", 1_000)),
            ("10 s", ("shift_ms", 10_000)),
            ("1 frame", ("shift_frames", 1)),
            ("10 frames", ("shift_frames", 10)),
        ],
    )
    def test_a_move_is_worth_the_size_it_is_set_to(self, size, expected):
        """Every size, since one doing the wrong amount is not something a
        look at the dialog would find."""

        block = _DialogBlock("a", fps=25.0)
        dialog = self._dialog(block)

        dialog._step_combo.setCurrentIndex(dialog._step_combo.findText(size))

        self._press(self._button(dialog, "a", "+"))

        assert block.called == [expected]

        # and the other way, by the same amount
        block.called.clear()

        self._press(self._button(dialog, "a", "-"))

        assert block.called == [(expected[0], -expected[1])]

        dialog.close()

    def test_a_video_that_goes_away_does_not_take_the_refresh_with_it(self):
        """A cell closed while this is open leaves a name with no widget
        behind it for a tick, and the timer goes on regardless."""

        calls = []

        def provider():
            calls.append(None)

            if len(calls) > 1:
                raise RuntimeError("wrapped C/C++ object has been deleted")

            return [_DialogBlock("a")]

        dialog = self._dialog(provider=provider)
        dialog._refresh()

        assert list(dialog._rows) == ["a"]

        dialog.close()

    def test_the_mode_note_says_what_is_missing_where_the_mode_is_off(self):
        dialog = self._dialog(
            _DialogBlock("a"), _DialogBlock("b"), is_offset_mode=lambda: False
        )

        assert "Sync Offset" in dialog._mode_note.text()

        dialog.close()

    def test_a_mode_note_is_shown_only_where_the_mode_is_off(self):
        """`isHidden`, not `isVisible`: a dialog nobody has shown reports
        every child of it as not visible either."""

        on = self._dialog(_DialogBlock("a"), is_offset_mode=lambda: True)
        off = self._dialog(_DialogBlock("a"), is_offset_mode=lambda: False)

        on._update_mode_note()
        off._update_mode_note()

        assert on._mode_note.isHidden()
        assert not off._mode_note.isHidden()

        on.close()
        off.close()

    def test_the_moves_do_not_need_the_sync_mode(self):
        dialog = self._dialog(_DialogBlock("a", fps=25.0), is_offset_mode=lambda: False)

        assert self._button(dialog, "a", "-").isEnabled()
        assert self._button(dialog, "a", "+").isEnabled()

        dialog.close()


# --- lining up by sound ---

# Half a second, in blocks, for the cases that move one video against
# another by a known amount.
HALF_A_SECOND = round(0.5 * BLOCKS_PER_SEC)


def _sound(length: int = 6000) -> tuple[float, ...]:
    """A shape worth matching: peaks of uneven size at uneven gaps.

    Not periodic, and long enough for a lag of a second or two to have
    something to sit against -- see the same shape in test_sync_audio.
    """

    values = []
    state = 12345

    for _ in range(length):
        state = (state * 1103515245 + 12345) % 2147483648
        values.append(float(state % 1000) / 100 if state % 7 < 3 else 0.0)

    return tuple(values)


def _heard_later(envelope, by: int) -> tuple[float, ...]:
    """The same sound turning up `by` blocks further into the recording."""

    return (0.0,) * by + tuple(envelope) if by >= 0 else tuple(envelope)[-by:]


def _other_sound(length: int = 6000) -> tuple[float, ...]:
    """A shape worth matching that is nothing like `_sound`.

    Unrelated on purpose: a recording that heard none of what another one
    heard is what the threshold is there to refuse.
    """

    values = []
    state = 987654321

    for _ in range(length):
        state = (state * 1103515245 + 12345) % 2147483648
        values.append(float(state % 997) / 50 if state % 5 < 2 else 0.0)

    return tuple(values)


class _StubMeasure:
    """Reading the sound, without reading any."""

    def __init__(self, is_running=False):
        self.started = []
        self.is_running = is_running

    def start(self, requests):
        self.started.append(requests)
        return True

    def cleanup(self):
        self.started.append(None)


def _wide_reading(block_id, uri="a recording.mp4", center_ms=12345):
    """The reading the pass over minutes of every recording asks for."""

    return ReadingRequest(
        block_id=block_id,
        uri=uri,
        center_ms=center_ms,
        snippet_ms=WIDE_READ_MS,
        blocks_per_sec=COARSE_SCALE.blocks_per_sec,
    )


def _close_reading(block_id, uri="a recording.mp4", center_ms=12345):
    """The reading the pass that places it to the millisecond asks for."""

    return ReadingRequest(
        block_id=block_id,
        uri=uri,
        center_ms=center_ms,
        snippet_ms=FINE_SNIPPET_MS,
        blocks_per_sec=BLOCKS_PER_SEC,
    )


class TestLiningUpBySound:
    @staticmethod
    def _dialog_with(*blocks, measure=None):
        dialog = TestTheAlignmentDialog._dialog(*blocks)

        if measure is not None:
            dialog._measure = measure

        return dialog

    def test_it_reads_the_files_and_leaves_the_streams_alone(self):
        """A stream would have to be fetched again for every reading, at the
        length of the snippet each time.

        What is read first is minutes of each file, a second a block: a grid
        that is minutes out is not something a snippet read around the
        moment can see anything of.
        """

        one = _DialogBlock("a", uri=Path("/tmp/a.mp4"))
        two = _DialogBlock("b", uri=Path("/tmp/b.mp4"))
        stream = _DialogBlock("c", uri="http://host/c.mp4")

        measure = _StubMeasure()
        dialog = self._dialog_with(one, two, stream, measure=measure)

        dialog._auto_align()

        assert measure.started == [
            [
                _wide_reading("a", str(Path("/tmp/a.mp4"))),
                _wide_reading("b", str(Path("/tmp/b.mp4"))),
            ]
        ]

        dialog.close()

    def test_the_minutes_are_read_first_and_the_snippet_after_them(self):
        """The snippet is read around where the wide pass left the videos, so
        that what is compared at two milliseconds a block is the sound that
        already lines up rather than the sound that did not."""

        from gridplayer.dialogs.align_videos import CLOSE_PASS, WIDE_PASS

        one = _DialogBlock("a", uri=Path("/tmp/a.mp4"))
        two = _DialogBlock("b", uri=Path("/tmp/b.mp4"))

        measure = _StubMeasure()
        dialog = self._dialog_with(one, two, measure=measure)

        dialog._auto_align()

        assert dialog._pass == WIDE_PASS
        assert measure.started == [
            [
                _wide_reading("a", str(Path("/tmp/a.mp4"))),
                _wide_reading("b", str(Path("/tmp/b.mp4"))),
            ]
        ]

        # nothing could be read of either of them, and the close pass is
        # read anyway: a misalignment a snippet can see is one the wide pass
        # was never needed for
        dialog._measured_wide({})

        assert dialog._pass == CLOSE_PASS
        assert measure.started[1] == [
            _close_reading("a", str(Path("/tmp/a.mp4"))),
            _close_reading("b", str(Path("/tmp/b.mp4"))),
        ]

        dialog.close()

    def test_a_misalignment_of_minutes_is_taken_out_before_the_snippet_is_read(self):
        """The wide pass is not a second opinion: what it finds is applied,
        the videos are taken to the moment it leaves them sharing, and the
        close pass reads around that."""

        one = _DialogBlock("a", uri=Path("/tmp/a.mp4"))
        two = _DialogBlock("b", uri=Path("/tmp/b.mp4"))

        measure = _StubMeasure()
        dialog = self._dialog_with(one, two, measure=measure)

        dialog._auto_align()

        # five minutes later in the second recording, at a second a block
        dialog._measured_wide(
            {
                "a": Reading(_sound(3000), 0),
                "b": Reading(_heard_later(_sound(3000), 300), 0),
            }
        )

        assert ("set_offset", 300_000) in two.called
        assert one.called == [("align_others", None)]

        # and what the offsets now say is what the close pass measures
        # against, or the five minutes would be taken out a second time
        assert dialog._measured_offsets == {"a": 0, "b": 300_000}

        # the second video has been moved five minutes further into its own
        # recording, and that is where its snippet is read
        assert measure.started[1] == [
            _close_reading("a", str(Path("/tmp/a.mp4")), 12345),
            _close_reading("b", str(Path("/tmp/b.mp4")), 312345),
        ]

        dialog.close()

    def test_it_says_which_file_it_is_waiting_on(self):
        """Minutes of a five-hour recording take tens of seconds to decode,
        and a note that only ever says "reading" cannot be told from a dialog
        that has stopped."""

        one = _DialogBlock("a", title="first.mp4")
        two = _DialogBlock("b", title="second.mp4")

        dialog = self._dialog_with(one, two, measure=_StubMeasure())

        dialog._auto_align()
        dialog._on_progress(1, 2, "a")

        assert "1 of 2" in dialog._sound_note.text()
        assert "first.mp4" in dialog._sound_note.text()

        dialog.close()

    def test_the_note_says_where_the_wider_stretch_matched_nothing(self):
        """From the outside that reads exactly like a sound that matched
        nothing at all, and it is the diagnosis that says what to do next."""

        one = _DialogBlock("a")
        two = _DialogBlock("b")

        dialog = self._dialog_with(one, two, measure=_StubMeasure())

        dialog._auto_align()
        dialog._measured_wide({})

        dialog._measured({"a": Reading(_sound(), 0), "b": Reading(_sound(), 0)})

        assert "Nothing in the minutes" in dialog._sound_note.text()

        dialog.close()

    def test_the_note_says_nothing_of_the_wider_stretch_where_it_matched(self):
        one = _DialogBlock("a")
        two = _DialogBlock("b")

        dialog = self._dialog_with(one, two, measure=_StubMeasure())

        dialog._auto_align()

        dialog._measured_wide(
            {"a": Reading(_sound(3000), 0), "b": Reading(_sound(3000), 0)}
        )
        dialog._measured({"a": Reading(_sound(), 0), "b": Reading(_sound(), 0)})

        assert "Nothing in the minutes" not in dialog._sound_note.text()

        dialog.close()

    def test_no_file_is_what_the_rest_are_judged_against_before_the_sound_is_read(self):
        """Which of them the others agree with is the sound's to say, so a
        dialog that has only asked for the sound has nothing to show yet."""

        one = _DialogBlock("a")
        two = _DialogBlock("b")

        dialog = self._dialog_with(one, two, measure=_StubMeasure())

        dialog._auto_align()

        assert dialog._run_reference_id is None

        dialog.close()

    def test_the_file_the_others_agree_with_is_what_they_are_judged_against(self):
        """The middle one here, and not the first: it is the only one that
        heard both of the others. Judging everything against the first
        would leave the third with nothing it could be compared against."""

        first = _DialogBlock("a")
        middle = _DialogBlock("b")
        last = _DialogBlock("c")

        dialog = self._dialog_with(first, middle, last, measure=_StubMeasure())
        dialog._auto_align()

        dialog._measured(
            {
                "a": Reading(_sound(3000), 0),
                "b": Reading(_sound(3000) + _other_sound(3000), 0),
                "c": Reading(_other_sound(3000), 0),
            }
        )

        assert dialog._run_reference_id == "b"
        assert "2 of 2" in dialog._sound_note.text()

        dialog.close()

    def test_one_file_with_no_sound_does_not_stop_the_other_two(self):
        """Three recordings where only two have a track.

        The one that could not be read says so in the note rather than
        being counted as lined up or as having failed to match.
        """

        silent = _DialogBlock("a", title="silent.mp4")
        first = _DialogBlock("b", title="first.mp4")
        second = _DialogBlock("c", title="second.mp4")

        dialog = self._dialog_with(silent, first, second, measure=_StubMeasure())
        dialog._auto_align()

        dialog._measured(
            {
                "b": Reading(_sound(), 0),
                "c": Reading(_heard_later(_sound(), HALF_A_SECOND), 0),
            }
        )

        assert dialog._run_reference_id == "b"
        assert ("set_offset", 500) in second.called
        assert silent.called == []
        assert "1 of 2" in dialog._sound_note.text()
        assert "silent.mp4" in dialog._sound_note.text()
        assert "nothing could be read" in dialog._sound_note.text()

        dialog.close()

    def test_one_file_is_not_enough_to_line_anything_up_against(self):
        dialog = self._dialog_with(_DialogBlock("a"), measure=_StubMeasure())

        dialog._auto_align()

        assert "two videos" in dialog._sound_note.text()

        dialog.close()

    def test_it_says_so_while_it_is_reading(self):
        one = _DialogBlock("a")
        two = _DialogBlock("b")

        dialog = self._dialog_with(one, two, measure=_StubMeasure())

        dialog._auto_align()

        assert "Reading the sound" in dialog._sound_note.text()
        assert not dialog._auto_button.isEnabled()

        dialog.close()

    def test_what_the_sound_asked_for_is_what_the_offset_gets(self):
        one = _DialogBlock("a")
        two = _DialogBlock("b", offset_ms=1000)

        dialog = self._dialog_with(one, two, measure=_StubMeasure())
        dialog._auto_align()

        # the second hears it half a second further in, and is already a
        # second ahead of where that says it should be: half of that second
        # is the correction
        dialog._measured(
            {
                "a": Reading(_sound(), 0),
                "b": Reading(_heard_later(_sound(), HALF_A_SECOND), 0),
            }
        )

        assert two.called == [("set_offset", 500)]

        dialog.close()

    def test_a_reading_that_began_early_is_accounted_for(self):
        """The whole reason the readings carry where they began.

        The same sound in both, and nothing wrong with either offset -- but
        the second was read from a second and a half further into its
        recording. Compared as though both began in the same place, that
        second and a half is charged to the offset and the video is moved
        by it for no reason.
        """

        one = _DialogBlock("a")
        two = _DialogBlock("b")

        dialog = self._dialog_with(one, two, measure=_StubMeasure())
        dialog._auto_align()

        dialog._measured({"a": Reading(_sound(), 0), "b": Reading(_sound(), 1500)})

        assert two.called == [("set_offset", 1500)]

        dialog.close()

    def test_the_videos_are_taken_to_where_the_offsets_say_they_are(self):
        """Setting an offset does not move a video, so lining the offsets up
        is not the same as lining the videos up: something has to seek them
        against the reference afterwards, or nothing has been heard to
        change at all."""

        one = _DialogBlock("a")
        two = _DialogBlock("b")

        dialog = self._dialog_with(one, two, measure=_StubMeasure())
        dialog._auto_align()

        dialog._measured({"a": Reading(_sound(), 0), "b": Reading(_sound(), 1500)})

        assert ("align_others", None) in one.called
        assert ("align_others", None) not in two.called

        dialog.close()

    def test_a_video_whose_sound_is_nothing_like_it_is_left_where_it_is(self):
        one = _DialogBlock("a")
        silent = _DialogBlock("b", offset_ms=1000)

        dialog = self._dialog_with(one, silent, measure=_StubMeasure())
        dialog._auto_align()

        dialog._measured({"a": Reading(_sound(), 0), "b": Reading((0.0,) * 6000, 0)})

        assert silent.called == []
        assert "Nothing was lined up" in dialog._sound_note.text()

        dialog.close()

    def test_it_says_so_where_no_recording_was_agreed_with(self):
        """Two of them read and neither like the other: there is no recording
        to name as the one the rest were judged against, and naming none of
        them would read as though one had been."""

        one = _DialogBlock("a")
        two = _DialogBlock("b")

        dialog = self._dialog_with(one, two, measure=_StubMeasure())
        dialog._auto_align()

        dialog._measured({"a": Reading(_sound(), 0), "b": Reading(_other_sound(), 0)})

        assert "Nothing was lined up" in dialog._sound_note.text()
        assert "None" not in dialog._sound_note.text()

        dialog.close()

    def test_the_note_says_what_the_wide_pass_did_where_the_close_one_gave_up(self):
        """The case a viewer is most likely to meet and least likely to
        understand: the minutes either side are enough to line the grid up,
        and the moment they are on is one only one of the recordings covers,
        so the close pass has nothing to say about it.

        Saying only what the close pass made of its snippet reads as though
        nothing had happened at all -- and by then the offsets have already
        been moved by the other pass.
        """

        one = _DialogBlock("a")
        two = _DialogBlock("b")

        dialog = self._dialog_with(one, two, measure=_StubMeasure())
        dialog._auto_align()

        dialog._measured_wide(
            {"a": Reading(_sound(3000), 0), "b": Reading(_sound(3000), 0)}
        )

        assert dialog._wide_result.reference_id == "a"

        dialog._measured({"a": Reading(_sound(), 0), "b": Reading(_other_sound(), 0)})

        note = dialog._sound_note.text()

        assert "Lined up to within a second over the minutes either side" in note
        assert "Move the videos to a moment they both hold" in note

        # and it is not also said that they were left where they were, which
        # is not true of them by then
        assert "Left where they were" not in note

        dialog.close()

    def test_nothing_is_moved_where_the_reference_could_not_be_read(self):
        one = _DialogBlock("a")
        two = _DialogBlock("b", offset_ms=1000)

        dialog = self._dialog_with(one, two, measure=_StubMeasure())
        dialog._auto_align()

        dialog._measured({"b": Reading(_sound(), 0)})

        assert two.called == []
        assert "Nothing could be read" in dialog._sound_note.text()

        dialog.close()

    def test_the_column_says_which_video_the_rest_were_judged_against(self):
        one = _DialogBlock("a")
        two = _DialogBlock("b")

        dialog = self._dialog_with(one, two, measure=_StubMeasure())
        dialog._auto_align()

        dialog._measured(
            {
                "a": Reading(_sound(), 0),
                "b": Reading(_heard_later(_sound(), HALF_A_SECOND), 0),
            }
        )

        assert dialog._rows["a"]["match"].text() == "reference"
        assert dialog._rows["b"]["match"].text() == "1.00"

        dialog.close()

    def test_it_reads_nothing_while_a_reading_is_already_in_hand(self):
        one = _DialogBlock("a")
        two = _DialogBlock("b")

        measure = _StubMeasure(is_running=True)
        dialog = self._dialog_with(one, two, measure=measure)

        dialog._auto_align()

        assert measure.started == []

        dialog.close()


# --- the sound drawn under each row ---


class TestTheSoundStrips:
    """What each row draws, and what moving a video does to it.

    This is the fallback for the recordings where the numbers are not to be
    trusted, so what it shows has to follow the offsets as they stand now
    rather than as they stood when the sound was read.
    """

    @staticmethod
    def _strip(dialog, block_id):
        return dialog._rows[block_id]["strip"]

    @staticmethod
    def _dialog_with(*blocks):
        dialog = TestTheAlignmentDialog._dialog(*blocks)

        # the readings are handed in ready-made, so nothing is read for
        # real; what is being tested is what is done with what came back
        dialog._measure = _StubMeasure()

        return dialog

    def test_a_strip_has_nothing_to_draw_until_the_sound_has_been_read(self):
        dialog = self._dialog_with(_DialogBlock("a"), _DialogBlock("b"))

        strip = self._strip(dialog, "a")

        assert strip._sound is None
        assert strip._zero_ms is None

        dialog.close()

    def test_a_strip_is_given_its_own_sound_and_the_offset_on_it(self):
        """Only its own: what the other recordings are doing is the overview
        strip's to say, and a row carrying both would be two things to read
        at once in the room for one."""

        one = _DialogBlock("a", offset_ms=5000)
        two = _DialogBlock("b", offset_ms=7000)

        dialog = self._dialog_with(one, two)
        dialog._auto_align()

        # the second reading began two seconds further into its recording,
        # which is what the two offsets already say: nothing wants moving
        dialog._measured(
            {
                "a": Reading(_sound(), 1000),
                "b": Reading(_sound(), 3000),
            }
        )

        strip = self._strip(dialog, "b")

        assert strip._sound.origin_ms == 3000
        assert strip._sound.offset_ms == 7000
        assert strip._zero_ms == dialog._zero_ms

        dialog.close()

    def test_moving_a_video_moves_its_sound_along_the_clock(self):
        one = _DialogBlock("a", offset_ms=5000)
        two = _DialogBlock("b", offset_ms=7000)

        dialog = self._dialog_with(one, two)
        dialog._auto_align()
        dialog._measured({"a": Reading(_sound(), 0), "b": Reading(_sound(), 0)})

        strip = self._strip(dialog, "b")
        was_own = strip._sound.start_ms

        # the offset says which moment of its recording a common moment is,
        # so moving it on by a second moves that recording's sound a second
        # earlier
        two.sync_offset_ms += 1000
        dialog._refresh()

        assert strip._sound.start_ms == was_own - 1000

        # and no other row's sound was moved by it
        other = self._strip(dialog, "a")

        assert other._sound.start_ms == dialog._sound_of("a", {"a": one}).start_ms

        dialog.close()

    def test_a_strip_is_not_drawn_again_while_nothing_has_moved(self):
        """The rows are read several times a second, and a strip is told
        which pair it has by being given the same objects as last time."""

        one = _DialogBlock("a")
        two = _DialogBlock("b")

        dialog = self._dialog_with(one, two)
        dialog._auto_align()
        dialog._measured({"a": Reading(_sound(), 0), "b": Reading((0.0,) * 6000, 0)})

        strip = self._strip(dialog, "b")
        redraws = []

        strip.update = lambda *args: redraws.append(args)

        dialog._refresh()

        assert redraws == []

        two.sync_offset_ms += 1000
        dialog._refresh()

        assert redraws != []

        dialog.close()

    def test_the_strips_can_be_drawn(self):
        """Painted for real, offscreen. The drawing is most of what the
        widget is, and an exception in it would otherwise only show up as a
        dialog that stopped repainting."""

        one = _DialogBlock("a")
        two = _DialogBlock("b")

        dialog = self._dialog_with(one, two)

        for block_id in ("a", "b"):
            strip = self._strip(dialog, block_id)
            strip.resize(280, 44)

            # nothing read yet, and then something to draw. The size is
            # whatever the layout gives it, not what was asked for: laying
            # the dialog out again happens on the way through `grab`
            assert strip.grab().width() > 0

        dialog._measured(
            {
                "a": Reading(_sound(), 0),
                "b": Reading(_heard_later(_sound(), HALF_A_SECOND), 0),
            }
        )

        for block_id in ("a", "b"):
            assert self._strip(dialog, block_id).grab().width() > 0

        dialog.close()

    def test_a_second_of_the_clock_is_the_same_width_wherever_it_is(self):
        one = _DialogBlock("a")
        two = _DialogBlock("b")

        dialog = self._dialog_with(one, two)
        dialog._measured({"a": Reading(_sound(), 0), "b": Reading((0.0,) * 6000, 0)})

        strip = self._strip(dialog, "a")
        strip.resize(500, 44)

        across = strip._x_of(0) - strip._x_of(-1000)

        assert across == pytest.approx(500 * 1000 / strip._window_ms)
        assert strip._x_of(strip._ms_at(120)) == pytest.approx(120)

        dialog.close()

    def test_the_wheel_zooms_in_on_what_the_cursor_is_over(self):
        one = _DialogBlock("a")
        two = _DialogBlock("b")

        dialog = self._dialog_with(one, two)
        dialog._measured({"a": Reading(_sound(), 0), "b": Reading((0.0,) * 6000, 0)})

        strip = self._strip(dialog, "a")
        strip.resize(500, 58)

        under_cursor = strip._ms_at(400)
        was_ms = strip._window_ms

        strip.wheelEvent(_wheel_event(400, notches=1))

        # the strip asked for a window and was handed one back by the dialog,
        # which is the only thing that can hand it to every strip at once
        assert strip._window_ms < was_ms
        assert strip._ms_at(400) == pytest.approx(under_cursor, abs=1)

        dialog.close()

    def test_a_drag_moves_the_window_along_the_clock(self):
        """The row of a video that is not being worked on looks further along
        when it is dragged: a strip that cannot be dragged at all is a strip
        that cannot be looked along."""

        one = _DialogBlock("a")
        two = _DialogBlock("b")

        dialog = self._dialog_with(one, two)
        dialog._measured({"a": Reading(_sound(), 0), "b": Reading((0.0,) * 6000, 0)})

        # the first video is the one being worked on, so this is one of the
        # rows that only looks
        strip = self._strip(dialog, "b")
        strip.resize(500, 58)

        was_center = strip._center_ms
        per_pixel = strip._ms_per_pixel()

        strip.mousePressEvent(_mouse_event(QEvent.MouseButtonPress, 400, Qt.LeftButton))
        strip.mouseMoveEvent(
            _mouse_event(QEvent.MouseMove, 300, Qt.NoButton, Qt.LeftButton)
        )
        strip.mouseReleaseEvent(
            _mouse_event(QEvent.MouseButtonRelease, 300, Qt.LeftButton)
        )

        assert strip._center_ms == round(was_center + 100 * per_pixel)

        dialog.close()

    def test_dragging_the_video_being_worked_on_moves_its_own_sound(self):
        """And nothing else: the offset follows the finger, no other row's
        sound moves, and the window stays where it was."""

        one = _DialogBlock("a")
        two = _DialogBlock("b")

        dialog = self._dialog_with(one, two)
        dialog._measured({"a": Reading(_sound(), 0), "b": Reading(_sound(), 0)})

        dialog._set_align_to("b")

        strip = self._strip(dialog, "b")
        strip.resize(500, 44)

        per_pixel = strip._ms_per_pixel()
        was_center = dialog._view_center_ms

        # what the alignment did to them is not what the drag is being asked
        # about: the drag is what is read from here on
        one.called.clear()
        two.called.clear()

        strip.mousePressEvent(_mouse_event(QEvent.MouseButtonPress, 400, Qt.LeftButton))
        strip.mouseMoveEvent(
            _mouse_event(QEvent.MouseMove, 300, Qt.NoButton, Qt.LeftButton)
        )
        strip.mouseReleaseEvent(
            _mouse_event(QEvent.MouseButtonRelease, 300, Qt.LeftButton)
        )

        # a hundred pixels to the left, so the sound is that much earlier on
        # the clock, which is that much further into its own recording
        moved = round(100 * per_pixel)

        assert ("drag_offset", moved) in two.called
        assert two.sync_offset_ms == moved

        # and the picture goes where the sound was dragged to, once, when the
        # drag is let go of rather than for every pixel of it
        assert ("drop_offset", None) in two.called
        assert two.time == 12345 + moved

        assert one.called == []
        assert dialog._view_center_ms == was_center

        dialog.close()

    def test_a_drag_given_up_puts_the_offset_back_and_moves_nothing(self):
        one = _DialogBlock("a")
        two = _DialogBlock("b")

        dialog = self._dialog_with(one, two)
        dialog._measured({"a": Reading(_sound(), 0), "b": Reading(_sound(), 0)})

        dialog._set_align_to("b")

        strip = self._strip(dialog, "b")
        strip.resize(500, 44)

        strip.mousePressEvent(_mouse_event(QEvent.MouseButtonPress, 400, Qt.LeftButton))
        strip.mouseMoveEvent(
            _mouse_event(QEvent.MouseMove, 300, Qt.NoButton, Qt.LeftButton)
        )
        strip.keyPressEvent(_key_event(Qt.Key_Escape))

        assert ("give_up_drag", None) in two.called
        assert two.sync_offset_ms == 0

        # nothing was seeked anywhere, so nothing had to be seeked back
        assert two.time == 12345

        dialog.close()

    def test_a_row_with_nothing_read_looks_along_instead_of_moving(self):
        """A drag has to mean something: with no sound read there is nothing
        to move against, and looking further along is what is left."""

        one = _DialogBlock("a")
        two = _DialogBlock("b")

        dialog = self._dialog_with(one, two)

        strip = self._strip(dialog, "a")
        strip.resize(500, 44)

        was_center = strip._center_ms

        strip.mousePressEvent(_mouse_event(QEvent.MouseButtonPress, 400, Qt.LeftButton))
        strip.mouseMoveEvent(
            _mouse_event(QEvent.MouseMove, 300, Qt.NoButton, Qt.LeftButton)
        )
        strip.mouseReleaseEvent(
            _mouse_event(QEvent.MouseButtonRelease, 300, Qt.LeftButton)
        )

        assert one.called == []
        assert strip._center_ms != was_center

        dialog.close()

    def test_every_strip_looks_through_the_same_window(self):
        """A dialog of strips each showing a different stretch of one clock is
        a dialog that cannot be read: this is what replaces it."""

        one = _DialogBlock("a")
        two = _DialogBlock("b")
        three = _DialogBlock("c")

        dialog = self._dialog_with(one, two, three)
        dialog._measured(
            {
                "a": Reading(_sound(), 0),
                "b": Reading((0.0,) * 6000, 0),
                "c": Reading(_sound(), 2000),
            }
        )

        strip_a = self._strip(dialog, "a")
        strip_c = self._strip(dialog, "c")

        assert (strip_a._center_ms, strip_a._window_ms) == (
            strip_c._center_ms,
            strip_c._window_ms,
        )

        # one of them zoomed: all of them did
        strip_a.resize(500, 58)
        strip_a.wheelEvent(_wheel_event(400, notches=2))

        assert (strip_a._center_ms, strip_a._window_ms) == (
            strip_c._center_ms,
            strip_c._window_ms,
        )

        # and one of them dragged: all of them followed
        was_center = strip_c._center_ms

        strip_c.resize(500, 58)
        strip_c.mousePressEvent(
            _mouse_event(QEvent.MouseButtonPress, 400, Qt.LeftButton)
        )
        strip_c.mouseMoveEvent(
            _mouse_event(QEvent.MouseMove, 200, Qt.NoButton, Qt.LeftButton)
        )

        assert strip_a._center_ms == strip_c._center_ms
        assert strip_c._center_ms > was_center

        dialog.close()

    def test_the_window_starts_over_the_whole_of_what_was_read(self):
        one = _DialogBlock("a")
        two = _DialogBlock("b")

        dialog = self._dialog_with(one, two)

        assert dialog._view_note.text() == ""

        # a second reading with nothing in it, so that the run has no pair it
        # trusts and moves neither offset: what the window comes to is then
        # the two readings and not the shifts as well
        dialog._measured({"a": Reading(_sound(), 0), "b": Reading((0.0,) * 6000, 0)})

        assert "Showing all of it" in dialog._view_note.text()

        strip = self._strip(dialog, "a")
        assert strip._window_ms == 12_000

        dialog.close()

    def test_the_caption_says_how_much_of_the_sound_is_on_show(self):
        """What the window covers is the thing a strip cannot say for itself,
        which is why it is said in words."""

        one = _DialogBlock("a", offset_ms=5000)
        two = _DialogBlock("b", offset_ms=7000)

        dialog = self._dialog_with(one, two)

        # two twelve-second readings, two seconds apart on the shared clock:
        # ten seconds of the recordings are on both of them
        dialog._measured({"a": Reading(_sound(), 0), "b": Reading((0.0,) * 6000, 0)})

        assert dialog._view_note.text() == "Showing all of it: 10.0 s."

        # zoomed in to under a third of it
        dialog._apply_view(0, 3_000)

        assert dialog._view_note.text() == "Showing 3.0 s of the 10.0 s that was read."

        # and Fit puts all of it back
        TestTheAlignmentDialog._press(dialog._fit_button)

        assert dialog._view_note.text() == "Showing all of it: 10.0 s."

        dialog.close()

    def test_fit_has_nothing_to_fit_until_the_sound_has_been_read(self):
        dialog = self._dialog_with(_DialogBlock("a"), _DialogBlock("b"))

        assert not dialog._fit_button.isEnabled()

        dialog._measured({"a": Reading(_sound(), 0), "b": Reading((0.0,) * 6000, 0)})

        assert dialog._fit_button.isEnabled()

        dialog.close()

    def test_the_zero_of_every_strip_is_the_moment_the_sound_was_read_around(self):
        """Zero is where the videos were when their sound was read, and it
        stays there: the offsets are tuned under it, so a sound that has been
        moved is seen to have moved."""

        one = _DialogBlock("a", offset_ms=5000)
        two = _DialogBlock("b", offset_ms=7000)

        dialog = self._dialog_with(one, two)

        dialog._measured({"a": Reading(_sound(), 1000), "b": Reading(_sound(), 3000)})

        # the reference is the earlier of the two, and zero is where it was:
        # its own position less the offset it holds
        assert dialog._zero_ms == common_moment_ms(one.time, one.sync_offset_ms)

        assert self._strip(dialog, "a")._zero_ms == dialog._zero_ms
        assert self._strip(dialog, "b")._zero_ms == dialog._zero_ms
        assert dialog._overview._zero_ms == dialog._zero_ms

        # moving the other video's offset moves its sound, not the ruler
        two.sync_offset_ms += 1000
        dialog._refresh()

        assert dialog._zero_ms == common_moment_ms(one.time, one.sync_offset_ms)

        dialog.close()

    def test_a_strip_knows_where_its_video_is_now(self):
        """The line is what keeps a window scrolled away from the sound being
        looked at from passing for the sound itself."""

        one = _DialogBlock("a")
        two = _DialogBlock("b")

        dialog = self._dialog_with(one, two)
        dialog._measured({"a": Reading(_sound(), 0), "b": Reading(_sound(), 0)})

        strip = self._strip(dialog, "b")
        two.sync_offset_ms = 2000
        two.time = 9000
        dialog._refresh()

        assert strip._now_ms == 7000

        dialog.close()


class TestTheOverviewStrip:
    """Every video's sound over every other's, at the top of the dialog.

    The rows draw one sound each, so this is the only place two recordings
    are seen against each other -- which is the whole question being asked
    of the eye, and the reason it is drawn at all.
    """

    @staticmethod
    def _dialog_with(*blocks):
        return TestTheSoundStrips._dialog_with(*blocks)

    def test_it_is_given_every_video_s_sound(self):
        one = _DialogBlock("a")
        two = _DialogBlock("b")

        dialog = self._dialog_with(one, two)
        dialog._measured(
            {
                "a": Reading(_sound(), 1000),
                "b": Reading(_heard_later(_sound(), HALF_A_SECOND), 3000),
            }
        )

        drawn = dialog._overview._drawn

        assert [one.sound.origin_ms for one in drawn] == [1000, 3000]

        # the offsets the sound is drawn by are the ones the videos hold now,
        # which is what the alignment has just moved them to
        assert [one.sound.offset_ms for one in drawn] == [
            one.sync_offset_ms,
            two.sync_offset_ms,
        ]

        dialog.close()

    def test_a_video_whose_sound_was_never_read_is_not_drawn_at_all(self):
        one = _DialogBlock("a")
        two = _DialogBlock("b")

        dialog = self._dialog_with(one, two)
        dialog._measured({"a": Reading(_sound(), 0)})

        assert len(dialog._overview._drawn) == 1

        dialog.close()

    def test_the_video_being_worked_on_is_painted_over_the_rest(self):
        one = _DialogBlock("a")
        two = _DialogBlock("b")

        dialog = self._dialog_with(one, two)
        dialog._measured({"a": Reading(_sound(), 0), "b": Reading(_sound(), 0)})

        assert [one.is_picked for one in dialog._overview._drawn] == [True, False]

        painted = []
        dialog._overview._paint_sound = lambda painter, sound, colour: painted.append(
            colour
        )

        dialog._overview.grab()

        # last of all, and solid: painted in its turn it would be covered by
        # whichever recording came after it
        assert painted[-1].alpha() == PICKED_ALPHA
        assert all(colour.alpha() == OVERLAY_ALPHA for colour in painted[:-1])

        dialog.close()

    def test_nothing_is_painted_until_the_sound_has_been_read(self):
        dialog = self._dialog_with(_DialogBlock("a"), _DialogBlock("b"))

        assert dialog._overview._zero_ms is None

        painted = []
        dialog._overview._paint_sound = lambda painter, sound, colour: painted.append(
            sound
        )

        dialog._overview.grab()

        assert painted == []

        dialog.close()

    def test_it_looks_through_the_same_window_as_every_row(self):
        one = _DialogBlock("a")
        two = _DialogBlock("b")

        dialog = self._dialog_with(one, two)
        dialog._measured({"a": Reading(_sound(), 0), "b": Reading(_sound(), 0)})

        strip = TestTheSoundStrips._strip(dialog, "a")

        dialog._overview.resize(500, 92)
        dialog._overview.wheelEvent(_wheel_event(400, notches=2))

        assert (dialog._overview._center_ms, dialog._overview._window_ms) == (
            strip._center_ms,
            strip._window_ms,
        )

        strip.resize(500, 44)
        strip.mousePressEvent(_mouse_event(QEvent.MouseButtonPress, 400, Qt.LeftButton))
        strip.mouseMoveEvent(
            _mouse_event(QEvent.MouseMove, 200, Qt.NoButton, Qt.LeftButton)
        )

        assert dialog._overview._center_ms == strip._center_ms

        dialog.close()

    def test_the_ruler_is_drawn_where_the_strips_are_read_against_it(self):
        """One band, at the top: a ruler under every row of a grid is the
        same ruler drawn as many times as there are videos."""

        dialog = self._dialog_with(_DialogBlock("a"), _DialogBlock("b"))
        dialog._measured({"a": Reading(_sound(), 0), "b": Reading(_sound(), 0)})

        # the sound of a row begins at the top of the widget, and the sound of
        # the overview begins under the band the numbers are in
        assert TestTheSoundStrips._strip(dialog, "a")._top_px() == 1
        assert dialog._overview._top_px() == AXIS_HEIGHT

        dialog.close()

    def test_it_can_be_drawn(self):
        """Painted for real, offscreen: an exception in it would otherwise
        only show up as a strip that stopped repainting."""

        dialog = self._dialog_with(_DialogBlock("a"), _DialogBlock("b"))

        assert dialog._overview.grab().width() > 0

        dialog._measured(
            {
                "a": Reading(_sound(), 0),
                "b": Reading(_heard_later(_sound(), HALF_A_SECOND), 0),
            }
        )

        assert dialog._overview.grab().width() > 0

        dialog.close()


class TestTheColoursOfTheSound:
    """One colour a video, so that a sound seen at the top of the dialog can
    be told from the one beside it."""

    @staticmethod
    def _colours(dialog):
        return [row["strip"]._colour for row in dialog._rows.values()]

    def test_two_videos_are_drawn_in_colours_that_tell_them_apart(self):
        dialog = TestTheAlignmentDialog._dialog(_DialogBlock("a"), _DialogBlock("b"))

        assert len(set(self._colours(dialog))) == 2
        assert set(self._colours(dialog)) <= set(SPECTRUM_COLORS)

        dialog.close()

    def test_a_colour_given_to_a_file_before_is_the_one_it_is_drawn_in(self, mocker):
        mocker.patch(
            "gridplayer.dialogs.align_videos.spectrum_color", return_value="#123456"
        )

        dialog = TestTheAlignmentDialog._dialog(_DialogBlock("a"), _DialogBlock("b"))

        assert self._colours(dialog) == ["#123456", "#123456"]

        dialog.close()

    def test_double_clicking_a_strip_asks_for_a_colour_and_remembers_it(self, mocker):
        mocker.patch(
            "gridplayer.dialogs.align_videos.color_picked_by_hand",
            return_value="#abcdef",
        )
        remembered = mocker.patch(
            "gridplayer.dialogs.align_videos.remember_spectrum_color"
        )

        one = _DialogBlock("a", uri=Path("/tmp/a.mp4"))
        two = _DialogBlock("b", uri=Path("/tmp/b.mp4"))

        dialog = TestTheAlignmentDialog._dialog(one, two)
        strip = dialog._rows["b"]["strip"]

        strip.mouseDoubleClickEvent(
            _mouse_event(QEvent.MouseButtonDblClick, 100, Qt.LeftButton)
        )

        assert remembered.call_args == mocker.call(Path("/tmp/b.mp4"), "#abcdef")

        colours = self._colours(dialog)

        assert colours[1] == "#abcdef"
        assert colours[0] != "#abcdef"

        dialog.close()

    def test_giving_up_the_colour_leaves_the_one_it_had(self, mocker):
        mocker.patch(
            "gridplayer.dialogs.align_videos.color_picked_by_hand", return_value=None
        )
        remembered = mocker.patch(
            "gridplayer.dialogs.align_videos.remember_spectrum_color"
        )

        dialog = TestTheAlignmentDialog._dialog(_DialogBlock("a"), _DialogBlock("b"))
        strip = dialog._rows["b"]["strip"]
        was = strip._colour

        strip.mouseDoubleClickEvent(
            _mouse_event(QEvent.MouseButtonDblClick, 100, Qt.LeftButton)
        )

        assert not remembered.called
        assert strip._colour == was

        dialog.close()


class _FakeSettings:
    """The settings, in memory: writing a colour in a test is not something
    to do to the machine running it."""

    def __init__(self):
        self.values = {}

    def get(self, setting):
        return self.values.get(setting, SpectrumColors())

    def set(self, setting, value):
        if not isinstance(value, SpectrumColors):
            raise TypeError(f"not a SpectrumColors: {value!r}")

        self.values[setting] = value


class TestTheColoursKept:
    """What a colour given by hand is remembered against: the file, and not
    the playlist it happened to be in."""

    @pytest.fixture(autouse=True)
    def _settings(self, mocker):
        settings = _FakeSettings()

        mocker.patch("gridplayer.settings.Settings", return_value=settings)

        return settings

    def test_a_colour_is_kept_against_the_file_it_was_given_to(self):
        from gridplayer.models.spectrum_colors import (
            remember_spectrum_color,
            spectrum_color,
        )

        remember_spectrum_color("/tmp/a.mp4", "#123456")

        assert spectrum_color("/tmp/a.mp4") == "#123456"
        assert spectrum_color("/tmp/b.mp4") is None

    def test_a_second_colour_replaces_the_first(self):
        from gridplayer.models.spectrum_colors import (
            remember_spectrum_color,
            spectrum_color,
        )

        remember_spectrum_color("/tmp/a.mp4", "#123456")
        remember_spectrum_color("/tmp/a.mp4", "#abcdef")

        assert spectrum_color("/tmp/a.mp4") == "#abcdef"

    def test_a_colour_can_be_taken_away(self):
        from gridplayer.models.spectrum_colors import (
            remember_spectrum_color,
            spectrum_color,
        )

        remember_spectrum_color("/tmp/a.mp4", "#123456")
        remember_spectrum_color("/tmp/a.mp4", None)

        assert spectrum_color("/tmp/a.mp4") is None

    def test_only_so_many_are_kept_and_the_ones_in_use_are_what_survives(self, mocker):
        from gridplayer.models import spectrum_colors as kept

        mocker.patch.object(kept, "KEPT_COLORS", 2)

        kept.remember_spectrum_color("/tmp/a.mp4", "#111111")
        kept.remember_spectrum_color("/tmp/b.mp4", "#222222")
        kept.remember_spectrum_color("/tmp/c.mp4", "#333333")

        assert kept.spectrum_color("/tmp/a.mp4") is None
        assert kept.spectrum_color("/tmp/b.mp4") == "#222222"
        assert kept.spectrum_color("/tmp/c.mp4") == "#333333"

        # and one given a colour again is the last to go rather than the first
        kept.remember_spectrum_color("/tmp/b.mp4", "#222222")
        kept.remember_spectrum_color("/tmp/d.mp4", "#444444")

        assert kept.spectrum_color("/tmp/b.mp4") == "#222222"
        assert kept.spectrum_color("/tmp/c.mp4") is None


class TestTheRulerOnAStrip:
    """The numbers down the top of a strip: seconds from the zero of the
    dialog, at a step that fits the zoom."""

    def test_the_step_grows_with_the_window(self):
        assert tick_step_ms(1_000, 400) == 250
        assert tick_step_ms(10_000, 400) == 2_000
        assert tick_step_ms(300_000, 400) == 60_000

    def test_a_wider_strip_affords_finer_steps(self):
        assert tick_step_ms(10_000, 400) == 2_000
        assert tick_step_ms(10_000, 1_600) == 500

    def test_the_ticks_are_the_multiples_inside_the_window(self):
        assert ticks_between(-7_000, 5_000, 2_000) == (
            -6_000,
            -4_000,
            -2_000,
            0,
            2_000,
            4_000,
        )

    def test_a_window_with_nothing_in_it_has_no_ticks(self):
        assert ticks_between(1_000, 1_000, 500) == ()
        assert ticks_between(0, 1_000, 0) == ()

    def test_a_tick_reads_as_seconds_either_side_of_zero(self):
        assert tick_label(0, 1_000) == "0"
        assert tick_label(-6_000, 2_000) == "-6"
        assert tick_label(15_000, 5_000) == "+15"
        assert tick_label(-500, 250) == "-0.5"


def _wheel_event(x: int, notches: int = 1):
    """One notch of a wheel over a widget, without a real mouse."""
    position = QPoint(x, 20)

    return QWheelEvent(
        position,
        position,
        QPoint(0, 0),
        QPoint(0, 120 * notches),
        Qt.NoButton,
        Qt.NoModifier,
        Qt.NoScrollPhase,
        False,
    )


def _mouse_event(kind, x: int, button, buttons=None):
    """A press, move or release over a widget, without a real mouse.

    `button` is which one changed and `buttons` which are held down, which
    are not the same thing: nothing changes during a move, and the left
    button is still down while it happens.
    """

    return QMouseEvent(
        kind,
        QPointF(x, 20),
        button,
        buttons if buttons is not None else button,
        Qt.NoModifier,
    )


def _key_event(key):
    """A key pressed on a widget, without a real keyboard."""

    return QKeyEvent(QEvent.KeyPress, key, Qt.NoModifier)
