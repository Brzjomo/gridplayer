"""Sync offset: lining up recordings that begin at different times.

The offset is the point on one recording's timeline that a common moment
falls on, so two videos are on the same moment when

    t_a - offset_a == t_b - offset_b

which is what every case here is about.

The modules that reach libVLC are imported inside the tests that need them,
so what does not need VLC can still be run on a machine without it.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication

from gridplayer.models.playlist import Playlist
from gridplayer.models.video import Video
from gridplayer.params.actions import ACTIONS
from gridplayer.params.menu import SECTIONS, SUBMENUS
from gridplayer.params.static import SeekSyncMode
from gridplayer.utils.sync_audio import BLOCKS_PER_SEC, Reading


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


@needs_vlc
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


@needs_vlc
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

    def sync_offset_set(self, offset_ms, *args):
        self._no_args(args)
        self.called.append(("set_offset", offset_ms))
        self.sync_offset_ms = offset_ms


@needs_vlc
class TestTheAlignmentDialog:
    @staticmethod
    def _dialog(*blocks, provider=None, is_offset_mode=None):
        from gridplayer.dialogs.align_videos import AlignVideosDialog

        if provider is None:

            def provider():
                return list(blocks)

        return AlignVideosDialog(provider, is_offset_mode=is_offset_mode)

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
        dialog = self._dialog(_DialogBlock("a", fps=None))

        assert not self._button(dialog, "a", "+1f").isEnabled()
        assert not self._button(dialog, "a", "-1f").isEnabled()
        assert self._button(dialog, "a", "+1s").isEnabled()

        dialog.close()

    def test_a_video_with_a_frame_rate_offers_the_move_in_frames(self):
        dialog = self._dialog(_DialogBlock("a", fps=25.0))

        assert self._button(dialog, "a", "+1f").isEnabled()

        dialog.close()

    def test_set_marks_the_offset_where_that_video_is(self):
        block = _DialogBlock("a")
        dialog = self._dialog(block)

        self._press(self._button(dialog, "a", "Set"))

        assert block.called == [("set_point", None)]

        dialog.close()

    def test_align_takes_the_others_to_that_video(self):
        block = _DialogBlock("a")
        dialog = self._dialog(block)

        self._press(self._button(dialog, "a", "Align"))

        assert block.called == [("align_others", None)]

        dialog.close()

    def test_each_move_moves_only_the_video_it_is_beside(self):
        one = _DialogBlock("a", fps=25.0)
        two = _DialogBlock("b", fps=25.0)
        dialog = self._dialog(one, two)

        self._press(self._button(dialog, "a", "+1f"))
        self._press(self._button(dialog, "b", "+1s"))

        assert one.called == [("shift_frames", 1)]
        assert two.called == [("shift_ms", 1000)]

        dialog.close()

    @pytest.mark.parametrize(
        ("label", "expected"),
        [
            ("-10s", ("shift_ms", -10000)),
            ("-1s", ("shift_ms", -1000)),
            ("+1s", ("shift_ms", 1000)),
            ("+10s", ("shift_ms", 10000)),
            ("-10f", ("shift_frames", -10)),
            ("-1f", ("shift_frames", -1)),
            ("+1f", ("shift_frames", 1)),
            ("+10f", ("shift_frames", 10)),
        ],
    )
    def test_a_move_is_worth_what_its_label_says(self, label, expected):
        """Every one of them, since a button doing the wrong amount is not
        something a look at the dialog would find."""

        block = _DialogBlock("a", fps=25.0)
        dialog = self._dialog(block)

        self._press(self._button(dialog, "a", label))

        assert block.called == [expected]

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

    def test_align_is_live_while_the_sync_mode_is_on(self):
        dialog = self._dialog(_DialogBlock("a"), is_offset_mode=lambda: True)

        assert self._button(dialog, "a", "Align").isEnabled()
        assert dialog._mode_note.isHidden()

        dialog.close()

    def test_align_is_held_where_the_sync_mode_is_off(self):
        """Outside the mode a seek carries nothing, so the move would come
        to nothing and say nothing about it."""

        dialog = self._dialog(_DialogBlock("a"), is_offset_mode=lambda: False)

        assert not self._button(dialog, "a", "Align").isEnabled()
        assert "Sync Offset mode" in dialog._mode_note.text()

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

    def test_the_other_moves_do_not_need_the_sync_mode(self):
        dialog = self._dialog(_DialogBlock("a", fps=25.0), is_offset_mode=lambda: False)

        assert self._button(dialog, "a", "Set").isEnabled()
        assert self._button(dialog, "a", "+1f").isEnabled()
        assert self._button(dialog, "a", "+1s").isEnabled()

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


@needs_vlc
class TestLiningUpBySound:
    @staticmethod
    def _dialog_with(*blocks, measure=None):
        dialog = TestTheAlignmentDialog._dialog(*blocks)

        if measure is not None:
            dialog._measure = measure

        return dialog

    def test_it_reads_the_files_and_leaves_the_streams_alone(self):
        """A stream would have to be fetched again for every reading, at the
        length of the snippet each time."""

        one = _DialogBlock("a", uri=Path("/tmp/a.mp4"))
        two = _DialogBlock("b", uri=Path("/tmp/b.mp4"))
        stream = _DialogBlock("c", uri="http://host/c.mp4")

        measure = _StubMeasure()
        dialog = self._dialog_with(one, two, stream, measure=measure)

        dialog._auto_align()

        assert measure.started == [
            [
                ("a", str(Path("/tmp/a.mp4")), 12345),
                ("b", str(Path("/tmp/b.mp4")), 12345),
            ]
        ]

        dialog.close()

    def test_the_first_file_is_what_the_rest_are_judged_against(self):
        one = _DialogBlock("a")
        two = _DialogBlock("b")

        dialog = self._dialog_with(one, two, measure=_StubMeasure())

        dialog._auto_align()

        assert dialog._reference_id == "a"

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
        assert "0 of 1" in dialog._sound_note.text()

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
