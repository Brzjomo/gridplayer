"""Lining up recordings that begin at different times, all on one screen.

An offset says which moment of a recording the videos are meant to be sharing,
so lining a grid up is one question asked of every video: where in this
recording is the moment the others are on. This window asks it in the two ways
it can be answered, and shows the answer as it changes.

**By sound, first.** *Align By Sound* reads every file, works out which video
the others agree with, sets the offsets from that and moves the videos onto it.
It is the only way to do it that does not need a person to recognise anything,
and it reports what it made of each video.

It reads them twice, and the first reading is the wide one: minutes of every
recording, compared a second at a time, which is what finds a grid that is
minutes out before anything has been done about it. What that comes to is
acted on, and only then is a snippet of each recording read -- around the
moment the wide pass has left them all on -- and compared at two
milliseconds a block. The snippet is read out of the sound the wide pass
already decoded, so the second pass costs a reading and no decoding at all:
see `sync_audio.WIDE_READ_MS`.

**By hand, when it cannot be trusted.** Click *Working on* against the video to
be moved and drag its own strip: the sound follows the finger, that recording's
picture goes where the sound was dragged to, and no other row moves at all. Every
other row's strip drags the window along instead, which is what looking further
along is. A video that is still out by a hair is moved by the buttons beside it,
one frame or one second at a time, until its sound sits where the others' does at
the zero of the strip. Double-clicking a strip is what gives that video's sound
its colour.

The strips are what makes the hand path work: one window shared by every row, a
line at the moment the videos were read around, each row's own sound under it,
and one strip above them all with every sound drawn over every other. They are
also what makes the automatic path checkable, which is the point of them -- a
score can be wrong, a shape that does not sit where the shape beside it sits is
not.

It is deliberately not modal: the videos are moved by hand while it is open,
and a dialog that had to be dismissed before the seek bar could be reached
would be no use for the one thing it is for.
"""

from pathlib import Path

from PyQt5.QtCore import QSignalBlocker, Qt, QTimer
from PyQt5.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from gridplayer.models.spectrum_colors import (
    SPECTRUM_COLORS,
    remember_spectrum_color,
    spectrum_color,
)
from gridplayer.settings import Settings
from gridplayer.utils.qt import translate
from gridplayer.utils.sync_align import AlignMeasure, ReadingRequest
from gridplayer.utils.sync_audio import (
    BLOCKS_PER_SEC,
    COARSE_SCALE,
    FINE_SNIPPET_MS,
    WIDE_READ_MS,
    Outcome,
    Sound,
    Verdict,
    align,
    common_moment_ms,
    shared_window,
)
from gridplayer.utils.time_txt import ms_time_txt
from gridplayer.widgets.bookmark_colors import color_picked_by_hand
from gridplayer.widgets.sync_strip import (
    DEFAULT_WINDOW_MS,
    MAX_WINDOW_MS,
    MIN_WINDOW_MS,
    DrawnSound,
    SyncOverviewStrip,
    SyncSoundStrip,
)
from gridplayer.widgets.video_block import sync_gap_txt

# How often the rows are read afresh. The videos are moved by hand while
# this is open, and a position lagging behind the picture is worse than no
# position at all.
REFRESH_MS = 200

# Which of the three kinds of run a set of readings is for. The wide one
# reads minutes of every recording and finds a misalignment of minutes; the
# close one, read after it has been acted on, places what is left to the
# millisecond; and the sound one is the close read on its own, drawn and not
# acted on at all. See sync_audio.COARSE_SCALE.
WIDE_PASS = "wide"
CLOSE_PASS = "close"
SOUND_PASS = "sound"

# The rows of the grid, above the one-video-to-a-row part of it: what every
# video's sound is doing against every other's, and then the headings for the
# columns the videos below are laid out in.
OVERVIEW_ROW = 0
HEADER_ROW = 1
FIRST_VIDEO_ROW = HEADER_ROW + 1

# The title column, wide enough for a filename and no wider: the offsets
# beside it are what the dialog is for.
TITLE_WIDTH = 210

# Wide enough for the heading over it and no wider: it is a tick, and the
# heading says what it means.
ALIGN_TO_WIDTH = 78

# Wide enough for the longest of what goes in them without eliding: the
# offset's own format is bounded, a gap is not.
STATE_WIDTH = 300

# How well a recording's sound agreed with the reference's, once it has been
# read. Wide enough for a score and the word for what it came to.
MATCH_WIDTH = 130

# How much of a filename the note at the bottom names a video by: long enough
# to tell one recording from another and short enough that three of them
# still read as a sentence.
NAME_WIDTH = 24

# Every column is a fixed width, and that is not tidiness. A position counting
# up wants a wider label than the one it had, the layout makes room for it,
# and the buttons to the right of it move. A button that moves between the
# press and the release is not clicked: the release lands somewhere else, or
# nowhere. Widths that cannot change are what keep the buttons where the
# press found them.
COLUMNS = (
    ("align_to", ALIGN_TO_WIDTH),
    ("title", TITLE_WIDTH),
    ("position", 88),
    ("offset", 200),
    ("state", STATE_WIDTH),
    ("match", MATCH_WIDTH),
)

# Past the last of the labels, for the moves: worked out rather than written
# down, since a column added above must not land on top of them.
MOVES_COLUMN = len(COLUMNS)

# What one press of a row's move buttons is worth, in the order the list
# offers them.
#
# The small sizes first, because what is left to move once a grid has been
# lined up by sound is milliseconds and a frame is a long way at that point.
# The sizes in frames last, because they are the ones a recording need not
# have a frame rate for: see sync_offset_shift_frames.
MILLISECONDS = "ms"
FRAMES = "frames"

NUDGE_STEPS = (
    (1, MILLISECONDS),
    (10, MILLISECONDS),
    (100, MILLISECONDS),
    (1_000, MILLISECONDS),
    (10_000, MILLISECONDS),
    (1, FRAMES),
    (10, FRAMES),
)

# The setting the size being used is kept in, so that a dialog opened again
# opens on the size it was left on rather than on the smallest one.
NUDGE_STEP_SETTING = "playlist/sync_nudge_step"

NUDGE_BUTTON_WIDTH = 34
ACTION_BUTTON_WIDTH = 84

# Between the buttons that read the sound and the tick that says which mode
# the sound is read for, so that the two read apart.
ACTION_GROUP_GAP = 10

# How close to the whole of what was read the window has to be before it is
# called the whole of it: a fit lands a rounding away from the span.
VIEW_SLACK_MS = 50

# Marks the two buttons a row is moved with, so that what they need to be
# live can be asked of them without reading their labels back: the Reset
# beside them is not moved by the step and is not held back by it.
IS_NUDGE = "is_nudge"


class AlignVideosDialog(QDialog):
    """Every video, its sync point, and where it currently is.

    `blocks_provider` is asked for the videos each time rather than handed
    them once: cells are added and closed while this is open, and a list taken
    when it opened would go on naming ones that are gone.

    `is_offset_mode` says whether Sync Offset mode is on, and `set_offset_mode`
    turns it on or off -- the mode is what the dialog is for, and the menu
    that owns it is behind the dialog: a viewer who finds it off should not
    have to close this to go and turn it on.
    """

    def __init__(
        self,
        blocks_provider,
        is_offset_mode=None,
        set_offset_mode=None,
        parent=None,
    ):
        super().__init__(parent)

        self._blocks_provider = blocks_provider
        self._is_offset_mode = is_offset_mode
        self._set_offset_mode = set_offset_mode

        self.setWindowTitle(translate("Dialog - Align Videos", "Align Videos"))
        self.setModal(False)
        self.setAttribute(Qt.WA_DeleteOnClose)

        self._rows: dict[str, dict] = {}
        self._grid = QGridLayout()

        # a stretch past the moves and no further: the buttons end where the
        # fixed columns do, so a wider dialog moves nothing
        self._grid.setColumnStretch(MOVES_COLUMN + 1, 1)

        self._build_header()

        self._mode_note = QLabel()
        self._mode_note.setWordWrap(True)
        self._mode_note.hide()

        self._sound_note = QLabel()
        self._sound_note.setWordWrap(True)

        # Which video is being worked on, and which one the last reading of
        # the sound judged the rest against. They are the same thing until
        # somebody says otherwise, and then both are worth showing: the
        # scores are a record of the run, the first is what a drag moves.
        self._align_to = None
        self._run_reference_id = None
        self._align_group = QButtonGroup(self)

        # what the last reading of the sound came to, by video. The readings
        # themselves are kept too: they are what the strips draw.
        self._verdicts: dict[str, Verdict] = {}
        self._measured_offsets: dict[str, int] = {}
        self._readings: dict = {}
        self._sounds: dict[str, tuple[Sound, int]] = {}

        # What colour each video's sound is drawn in, worked out once a video
        # and kept: a colour named by hand comes out of the settings, and one
        # that was never named is taken from the palette and taken away from
        # it, so that two videos in one dialog are not drawn alike.
        self._colors: dict[str, str] = {}

        # Which pass the readings in hand were asked for, and what the wide
        # one came to. The close pass reads after the wide one has been
        # acted on, so whether the recordings are anywhere near each other
        # is the wide pass's to say, and it is worth saying where it could
        # not: the close pass then has only its own snippet to go on.
        self._pass = CLOSE_PASS
        self._wide_result = None

        # One window on the shared clock for every strip in the dialog, and
        # one zero for them to be read against: the moment the sound was read
        # around, which is where the videos were when it was read and stays
        # put while the offsets are tuned under it. Both are the dialog's to
        # keep -- see _ClockStrip.
        self._view_center_ms = None
        self._view_window_ms = DEFAULT_WINDOW_MS
        self._zero_ms = None

        self._measure = AlignMeasure(parent=self)
        self._measure.measured.connect(self._on_measured)
        self._measure.progress.connect(self._on_progress)

        self._view_note = QLabel()
        self._view_note.setWordWrap(True)

        fit_tooltip = translate(
            "Dialog - Align Videos", "Show all of the sound that was read"
        )

        self._fit_button = self._button(
            translate("Dialog - Align Videos", "Fit"),
            fit_tooltip,
            self._doing(self._fit_view),
            ACTION_BUTTON_WIDTH,
        )

        auto_tooltip = translate(
            "Dialog - Align Videos", "Read the sound of every file and line them up"
            " on whichever of them the others agree with"
        )  # fmt: skip

        self._auto_button = self._button(
            translate("Dialog - Align Videos", "Align By Sound"),
            auto_tooltip,
            self._doing(self._auto_align),
            140,
        )

        read_tooltip = translate(
            "Dialog - Align Videos", "Read the sound of every file around where the"
            " videos are and draw it, moving nothing"
        )  # fmt: skip

        self._read_button = self._button(
            translate("Dialog - Align Videos", "Read Sound"),
            read_tooltip,
            self._doing(self._read_sound),
            140,
        )

        mode_tooltip = translate(
            "Dialog - Align Videos", "Line the videos up by their sync points: this"
            " dialog needs it on to move anything"
        )  # fmt: skip

        self._mode_check = QCheckBox(translate("Seek Sync", "Sync Offset"))
        self._mode_check.setToolTip(mode_tooltip)
        self._mode_check.setChecked(self._offset_mode())
        self._mode_check.toggled.connect(self._when_offset_mode_toggled)

        buttons = QDialogButtonBox(QDialogButtonBox.Close, Qt.Horizontal, self)
        buttons.rejected.connect(self.close)

        actions = QHBoxLayout()
        actions.addWidget(self._auto_button)
        actions.addWidget(self._read_button)
        actions.addSpacing(ACTION_GROUP_GAP)
        actions.addWidget(self._mode_check)
        actions.addStretch(1)
        actions.addWidget(buttons)

        caption = QHBoxLayout()
        caption.addStretch(1)
        caption.addWidget(self._view_note)
        caption.addWidget(self._fit_button)

        self._build_step()

        self._overview = SyncOverviewStrip()
        self._overview.view_changed.connect(self._on_view_changed)

        self._grid.addWidget(self._overview, OVERVIEW_ROW, 0, 1, MOVES_COLUMN + 1)

        self._hints = self._explain()

        layout = QVBoxLayout(self)
        layout.addWidget(self._hints)
        layout.addWidget(self._mode_note)
        layout.addLayout(caption)
        layout.addLayout(self._grid)
        layout.addWidget(self._sound_note)
        layout.addStretch(1)
        layout.addLayout(actions)

        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(REFRESH_MS)
        self._refresh_timer.timeout.connect(self._refresh)
        self._refresh_timer.start()

        self._refresh()

    def _explain(self) -> QLabel:
        """The steps, in the order they are done, and short.

        A list rather than a paragraph: what this dialog is for is a
        procedure, and the procedure has an order to it -- read the sound,
        look at it, move what is still out, and colour what colour is the only
        way to tell apart. Kept to one line a step, since a hint nobody reads
        to the end is a hint nobody has.

        The controls are named by their own words and not by new ones: each
        name is the string the widget itself is labelled with, put in by
        `format`. A hint that says "Align By Sound" while the button says
        something else is worse than no hint, and two strings that have to be
        translated into the same thing are two strings that drift apart.

        Numbered by the widget and not by the words, so that a step added or
        moved later renumbers itself.

        Every string is built in a statement of its own first, and the run of
        them is joined into the markup afterwards: `pylupdate5` reads the text
        of a `translate()` call only when it begins on the line the call opens
        on, which a call nested inside another one does not. See `AGENTS.md`
        and translate_extraction in `tests/`.
        """

        names = {
            "ALIGN": self._auto_button.text(),
            "READ": self._read_button.text(),
            "WORKING_ON": self._working_on_txt(),
            "MOVE_BY": self._move_by_txt(),
            "FIT": self._fit_button.text(),
        }

        align_txt = translate(
            "Dialog - Align Videos", "Press {ALIGN} to line the videos up, or {READ} to"
            " look at their sound without moving anything."
        )  # fmt: skip

        look_txt = translate(
            "Dialog - Align Videos", "The strip at the top is every video's sound over"
            " every other's, one colour each."
        )  # fmt: skip

        drag_txt = translate(
            "Dialog - Align Videos", "Click {WORKING_ON} against a video and drag its"
            " own strip to move that video; no other row moves."
        )  # fmt: skip

        along_txt = translate(
            "Dialog - Align Videos", "Drag any other row's strip to look along. The"
            " wheel zooms, and {FIT} shows all of it."
        )  # fmt: skip

        fine_txt = translate(
            "Dialog - Align Videos", "Set {MOVE_BY}, then press - or + beside a video to"
            " move it by that much."
        )  # fmt: skip

        colour_txt = translate(
            "Dialog - Align Videos", "Double-click a strip to choose the colour that"
            " video's sound is drawn in."
        )  # fmt: skip

        reach_txt = translate(
            "Dialog - Align Videos", "{ALIGN} looks ten minutes either side of where the"
            " videos are now."
        )  # fmt: skip

        steps = "".join(
            f"<li>{step.format(**names)}</li>"
            for step in (
                align_txt,
                look_txt,
                drag_txt,
                along_txt,
                fine_txt,
                colour_txt,
            )
        )

        label = QLabel(f"<ol>{steps}</ol><p>{reach_txt.format(**names)}</p>")
        label.setWordWrap(True)
        label.setTextFormat(Qt.RichText)

        return label

    @staticmethod
    def _working_on_txt() -> str:
        """What the tick that says which video is being worked on is called.

        Named here and in the column heading by one `translate()` call each:
        they are the same words, and the header is built before the hints are.
        """

        return translate("Dialog - Align Videos", "Working on")

    @staticmethod
    def _move_by_txt() -> str:
        """What the list of sizes a move can be made in is called."""

        return translate("Dialog - Align Videos", "Move by")

    def _build_header(self):
        headers = (
            self._working_on_txt(),
            translate("Dialog - Align Videos", "Video"),
            translate("Dialog - Align Videos", "Position"),
            translate("Dialog - Align Videos", "Sync point"),
            translate("Dialog - Align Videos", "Range"),
            translate("Dialog - Align Videos", "Sound match"),
        )

        for column, ((_key, width), text) in enumerate(zip(COLUMNS, headers)):
            header = QLabel(text)
            header.setFixedWidth(width)
            font = header.font()
            font.setBold(True)
            header.setFont(font)

            self._grid.addWidget(header, HEADER_ROW, column)

    # --- how far one press moves a video ---

    def _build_step(self):
        """The list every row's two buttons are read off, over their column.

        One list for the whole dialog rather than a pair of buttons a size:
        what is left to do by hand after lining a grid up by sound is a
        millisecond or two, and eight buttons a row to say so is eight
        buttons a row too many.
        """

        step_txt = self._move_by_txt()
        self._step_note = QLabel(step_txt)
        step_font = self._step_note.font()
        step_font.setBold(True)
        self._step_note.setFont(step_font)

        step_tooltip = translate(
            "Dialog - Align Videos", "How far one press of - or + moves a video"
        )

        self._step_combo = QComboBox()
        self._step_combo.setToolTip(step_tooltip)

        for amount, kind in NUDGE_STEPS:
            self._step_combo.addItem(self._step_label(amount, kind))

        self._step_combo.setCurrentIndex(self._saved_step())
        self._step_combo.currentIndexChanged.connect(self._when_step_changed)

        # the heading and the list in one column, over the two buttons they
        # are read by: the grid stretches whatever is past the moves, and a
        # list left out there would sit at the far edge of the dialog with
        # its heading nowhere near it
        step_holder = QWidget()
        step_layout = QHBoxLayout(step_holder)
        step_layout.setContentsMargins(0, 0, 0, 0)
        step_layout.setSpacing(4)
        step_layout.addWidget(self._step_note)
        step_layout.addWidget(self._step_combo)

        self._grid.addWidget(step_holder, HEADER_ROW, MOVES_COLUMN)

    @staticmethod
    def _step_label(amount: int, kind: str) -> str:
        """What a size of move is called, in the words the rest of the sound
        is measured in."""

        if kind is FRAMES:
            if amount == 1:
                one_frame_txt = translate("Dialog - Align Videos", "1 frame")

                return one_frame_txt

            frames_txt = translate("Dialog - Align Videos", "{COUNT} frames")
            label = frames_txt.format(COUNT=amount)

            return label

        if amount < 1_000:
            millis_txt = translate("Dialog - Align Videos", "{COUNT} ms")
            label = millis_txt.format(COUNT=amount)

            return label

        seconds_txt = translate("Dialog - Align Videos", "{COUNT} s")
        label = seconds_txt.format(COUNT=amount // 1_000)

        return label

    @staticmethod
    def _saved_step() -> int:
        """Which size was being moved by last time, as far as it is still
        one of the sizes offered."""

        saved = Settings().get(NUDGE_STEP_SETTING)

        return max(0, min(len(NUDGE_STEPS) - 1, int(saved)))

    def _step(self) -> tuple:
        return NUDGE_STEPS[self._step_combo.currentIndex()]

    def _step_kind(self) -> str:
        return self._step()[1]

    def _when_step_changed(self, index: int):
        """A new size: every row's buttons are worth different things now."""

        Settings().set(NUDGE_STEP_SETTING, int(index))

        self._refresh()

    # --- building and refreshing the rows ---

    def _blocks(self) -> list:
        return list(self._blocks_provider())

    def _refresh(self):
        """Read the videos afresh, past any that went away under us.

        The timer runs whether or not anything is being torn down, and a cell
        closed while this is open leaves a name with no widget behind it for
        one tick.
        """

        try:
            self._refresh_rows()
        except RuntimeError:
            pass

    def _refresh_rows(self):
        blocks = list(self._blocks_provider())

        if [b.id for b in blocks] != list(self._rows):
            self._rebuild(blocks)

        # one look at the grid per refresh, passed around: asking for it
        # again in the middle of a refresh can come back different, and half
        # a refresh built from one list and half from another is a dialog
        # naming a video that is gone
        by_id = {block.id: block for block in blocks}

        if self._align_to not in by_id:
            self._set_align_to(next(iter(by_id), None), by_id=by_id, refresh=False)

        for block in blocks:
            self._update_row(block, by_id)

        self._update_nudge_tooltips()
        self._update_overview(by_id)
        self._update_mode_note()
        self._update_view_note(by_id)
        self._set_actions_live(by_id)

    def _rebuild(self, blocks):
        for row in self._rows.values():
            for widget in row.values():
                if isinstance(widget, QWidget):
                    self._grid.removeWidget(widget)
                    widget.deleteLater()

        self._rows = {}

        for index, block in enumerate(blocks, start=1):
            self._rows[block.id] = self._make_row(block, index)

    def _make_row(self, block, index: int) -> dict:
        row = {}

        # two lines to a video: what it is and what can be done about it, and
        # under it the sound, drawn across the whole width -- the wider it is,
        # the more of a second of misalignment can be seen
        labels_at = FIRST_VIDEO_ROW + (index - 1) * 2

        for column, (key, width) in enumerate(COLUMNS):
            if key == "align_to":
                widget = self._make_align_radio(block)
            else:
                widget = QLabel()
                widget.setFixedWidth(width)

            row[key] = widget

            self._grid.addWidget(widget, labels_at, column)

        moves = QWidget()
        moves_layout = QHBoxLayout(moves)
        moves_layout.setContentsMargins(0, 0, 0, 0)
        moves_layout.setSpacing(2)
        moves_layout.addWidget(self._make_buttons(block))

        self._grid.addWidget(moves, labels_at, MOVES_COLUMN)

        row["moves"] = moves

        strip = SyncSoundStrip()
        strip.view_changed.connect(self._on_view_changed)
        strip.color_requested.connect(self._when_color_requested(block.id))
        strip.sound_dragged.connect(self._when_sound_dragged(block.id))
        strip.sound_dropped.connect(self._when_sound_dropped(block.id))
        strip.sound_drag_given_up.connect(self._when_sound_drag_given_up(block.id))

        self._grid.addWidget(strip, labels_at + 1, 0, 1, MOVES_COLUMN + 1)

        row["strip"] = strip

        return row

    def _when_color_requested(self, block_id: str):
        """A handler for a strip asking for a colour for its own video.

        The signal carries nothing of its own, and the id is bound here
        rather than read back out of the widgets: a strip knows which sound
        it is drawing and not which video that sound was read from.
        """

        def handler():
            self._pick_color(block_id)

        return handler

    def _when_sound_dragged(self, block_id: str):
        """A handler for the video being worked on being dragged along the
        clock.

        What the drag says is where the offset would put its sound, so all
        that is left here is to say it to the video: the sound follows the
        finger because the offset it is drawn by follows the finger, and no
        other row's sound moves at all.
        """

        def handler(offset_ms):
            block = self._blocks_by_id().get(block_id)

            if block is not None:
                block.sync_offset_drag(offset_ms)

        return handler

    def _when_sound_dropped(self, block_id: str):
        """A handler for a drag settling: the video goes where its sound was
        dragged to."""

        def handler():
            block = self._blocks_by_id().get(block_id)

            if block is not None:
                block.sync_offset_drop()

        return handler

    def _when_sound_drag_given_up(self, block_id: str):
        """A handler for a drag being given up: the offset goes back to what
        it was, and nothing has been seeked anywhere to have to put right."""

        def handler():
            block = self._blocks_by_id().get(block_id)

            if block is not None:
                block.sync_offset_drag_give_up()

        return handler

    def _make_align_radio(self, block) -> QRadioButton:
        """The one tick that says which video is being worked on.

        Its sound is the one a drag moves, and it is the one the overview
        draws over the rest: one video at a time, since a drag has to mean
        one thing and the eye can only hold one of them at once.
        """

        radio = QRadioButton()
        radio.setFixedWidth(ALIGN_TO_WIDTH)
        radio.setToolTip(
            translate("Dialog - Align Videos", "Work on this video's sound")
        )
        radio.toggled.connect(self._when_checked(self._set_align_to, block.id))

        self._align_group.addButton(radio)

        return radio

    def _make_buttons(self, block) -> QWidget:
        holder = QWidget()
        layout = QHBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        reset_tooltip = translate(
            "Dialog - Align Videos", "Play this recording from its own start again"
        )

        # every one of these goes through _doing: a bound method of a video
        # block is a wrapper taking anything, and `clicked` would find the
        # checked flag in its first argument
        layout.addWidget(self._nudge(block, by=-1))
        layout.addWidget(self._nudge(block, by=1))

        layout.addWidget(
            self._button(
                translate("Dialog - Align Videos", "Reset"),
                reset_tooltip,
                self._doing(block.sync_offset_reset),
                ACTION_BUTTON_WIDTH,
            )
        )

        return holder

    def _nudge(self, block, by: int) -> QPushButton:
        """One move of one video, by whatever the step is set to.

        The size is not on the button: it is one list for the whole dialog,
        above the column, and a button saying what it is worth would have to
        be relabelled every time that list moves.
        """

        button = self._button(
            "-" if by < 0 else "+",
            self._nudge_tooltip(by),
            self._doing(self._move, block, by),
            NUDGE_BUTTON_WIDTH,
        )
        button.setProperty(IS_NUDGE, True)

        return button

    def _move(self, block, by: int):
        """Move one video by one step of whatever size is being moved by."""

        amount, kind = self._step()

        if kind is FRAMES:
            block.sync_offset_shift_frames(by * amount)
        else:
            block.sync_offset_shift_ms(by * amount)

    def _nudge_tooltip(self, by: int) -> str:
        """What one press will do, said with the size it will do it in."""

        amount, kind = self._step()
        size = self._step_label(amount, kind)

        if by > 0:
            later_txt = translate(
                "Dialog - Align Videos", "Move this recording {SIZE} later against"
                " the others"
            )  # fmt: skip
            tooltip = later_txt
        else:
            earlier_txt = translate(
                "Dialog - Align Videos", "Move this recording {SIZE} earlier against"
                " the others"
            )  # fmt: skip
            tooltip = earlier_txt

        return tooltip.format(SIZE=size)

    def _update_nudge_tooltips(self):
        """Say what every row's two buttons are worth now, since the step
        they are read off may have moved."""

        for row in self._rows.values():
            for button in row["moves"].findChildren(QPushButton):
                if not button.property(IS_NUDGE):
                    continue

                button.setToolTip(
                    self._nudge_tooltip(-1 if button.text() == "-" else 1)
                )

    @staticmethod
    def _when_checked(func, *args):
        """A handler for a tick, which only acts when the tick goes on.

        `toggled` fires for both directions, and the one going off is not the
        one a finger chose: the video being worked on would end up being
        whichever row happened to be unticked last.
        """

        def handler(checked):
            if checked:
                func(*args)

        return handler

    @staticmethod
    def _doing(func, *args):
        """A click handler that takes nothing of its own.

        `clicked` carries a checked flag and hands it to whichever slot will
        take it, and a bound method of a video block is wrapped as one that
        takes anything. Called with the flag, that wrapper finds it where the
        video itself should be -- and a bool has no video to act on, so the
        click comes to nothing at all.
        """

        def handler():
            func(*args)

        return handler

    @staticmethod
    def _button(text: str, tooltip: str, on_click, width: int) -> QPushButton:
        button = QPushButton(text)
        button.setToolTip(tooltip)
        button.setFixedWidth(width)

        # pressed, not clicked. A move asked for is worth making at once, and
        # making it on the press is making it before anything can take the
        # focus or shift the button out from under the release -- either of
        # which costs the click, and this dialog is one that has to be used
        # while the videos behind it are still live.
        button.pressed.connect(on_click)

        return button

    def _update_row(self, block, by_id):
        row = self._rows.get(block.id)

        if row is None:
            return

        title = self._title_of(block)

        # ticked to match what it is, with its own signal held: a refresh
        # setting the tick is not a finger choosing a video, and taking it
        # for one would have the button name whichever row was drawn last
        with QSignalBlocker(row["align_to"]):
            row["align_to"].setChecked(self._align_to == block.id)

        row["title"].setText(
            row["title"].fontMetrics().elidedText(title, Qt.ElideMiddle, TITLE_WIDTH)
        )
        row["title"].setToolTip(title)
        row["position"].setText(ms_time_txt(block.time))
        row["offset"].setText(block.get_sync_offset_txt())

        state = self._state_of(block)

        # elided rather than clipped: a gap longer than the column would
        # otherwise lose the end of itself, which is the part that says which
        # way it goes
        row["state"].setText(
            row["state"].fontMetrics().elidedText(state, Qt.ElideRight, STATE_WIDTH)
        )
        row["state"].setToolTip(state)
        row["match"].setText(self._match_txt(block))

        row["strip"].show_sound(
            self._sound_of(block.id, by_id),
            self._color_of(block),
            common_moment_ms(block.time, block.sync_offset_ms),
            self._zero_ms,
            is_picked=block.id == self._align_to,
        )

        self._set_moves_live(block, row["moves"])

    def _update_overview(self, by_id):
        """Every video's sound, one over another, with the one being worked
        on drawn last.

        Read here rather than in the rows because this is the only place two
        recordings' sound is seen against each other: a row draws its own and
        nothing else.
        """

        drawn = [
            DrawnSound(
                sound=self._sound_of(block.id, by_id),
                colour=self._color_of(block),
                is_picked=block.id == self._align_to,
            )
            for block in by_id.values()
        ]

        self._overview.show_sounds(
            [one for one in drawn if one.sound is not None],
            self._now_ms(by_id),
            self._zero_ms,
        )

    def _now_ms(self, by_id) -> int | None:
        """The moment the video being worked on is on now, which is the one
        line the overview draws: one line per video would be one line too
        many to read."""

        block = by_id.get(self._align_to)

        if block is None:
            return None

        return common_moment_ms(block.time, block.sync_offset_ms)

    def _color_of(self, block) -> str:
        """What colour this video's sound is drawn in.

        Named by hand once and remembered against the file, so that the same
        recording is the same colour in the next dialog it is opened in; and
        taken from the palette until then, one per video and not one that
        another video of this dialog is already drawn in.
        """

        colour = self._colors.get(block.id)

        if colour is not None:
            return colour

        colour = spectrum_color(block.video_params.uri) or self._unused_color()

        self._colors[block.id] = colour

        return colour

    def _unused_color(self) -> str:
        """The first colour of the palette nothing else here is drawn in."""

        taken = set(self._colors.values())

        for colour in SPECTRUM_COLORS:
            if colour not in taken:
                return colour

        # more videos than colours: round again rather than draw two of them
        # in nothing at all
        return SPECTRUM_COLORS[len(self._colors) % len(SPECTRUM_COLORS)]

    def _pick_color(self, block_id: str):
        """Ask for a colour for one video's sound, and remember it for the
        file it was read from."""

        block = self._blocks_by_id().get(block_id)

        if block is None:
            return

        picked = color_picked_by_hand(self, self._color_of(block))

        if picked is None:
            return

        remember_spectrum_color(block.video_params.uri, picked)

        self._colors[block_id] = picked

        self._refresh()

    def _sound_of(self, block_id, by_id):
        """One video's reading, as its offset stands at this moment.

        Made once per offset and kept: the rows are read several times a
        second, and the strip tells a pair it has already been given from a
        new one by looking at them, which it can only do while they are the
        same objects from one refresh to the next.
        """

        reading = self._readings.get(block_id)
        block = by_id.get(block_id)

        if reading is None or block is None or not reading.envelope:
            return None

        sound, offset_ms = self._sounds.get(block_id, (None, None))

        if sound is None or offset_ms != block.sync_offset_ms:
            sound = Sound(reading.envelope, reading.origin_ms, block.sync_offset_ms)
            self._sounds[block_id] = (sound, block.sync_offset_ms)

        return sound

    def _match_txt(self, block) -> str:
        """How well this one's sound agreed, and which it was judged against.

        A recording that was read and left where it was says so beside its
        score: a number with nothing wrong with it that was still not acted on
        is the one thing a viewer cannot read from the number alone.
        """

        if self._run_reference_id == block.id:
            return translate("Dialog - Align Videos", "reference")

        verdict = self._verdicts.get(block.id)

        if verdict is None or verdict.score is None:
            return ""

        score = f"{verdict.score:.2f}"

        if verdict.is_lined_up:
            return score

        return translate("Dialog - Align Videos", "{SCORE} ({NOTE})").format(
            SCORE=score, NOTE=self._short_reason(verdict)
        )

    @staticmethod
    def _short_reason(verdict) -> str:
        if verdict.outcome is Outcome.NOTHING_ALIKE:
            return translate("Dialog - Align Videos", "weak")

        if verdict.outcome is Outcome.AMBIGUOUS:
            return translate("Dialog - Align Videos", "unsure")

        if verdict.outcome is Outcome.CONTRADICTED:
            return translate("Dialog - Align Videos", "conflict")

        return translate("Dialog - Align Videos", "unlinked")

    @staticmethod
    def _title_of(block) -> str:
        return block.title or block.video_params.uri_name

    @staticmethod
    def _state_of(block) -> str:
        """Said in a column of its own, under a heading that says the rest:
        "Out of video range" on every row would leave no room for the one
        thing the row is there to tell."""

        out_of_range = block.sync_out_of_range

        if out_of_range is None:
            return ""

        is_before, short_by = out_of_range
        gap = sync_gap_txt(short_by, block.video_fps)

        # read from the moment being asked for, which is where the viewer is
        # looking: the recording is not there yet, or is already past
        if is_before:
            return translate("Dialog - Align Videos", "starts in {GAP}").format(GAP=gap)

        return translate("Dialog - Align Videos", "ended {GAP} ago").format(GAP=gap)

    def _set_moves_live(self, block, moves: QWidget):
        """What each move needs before it can do anything.

        A video offers no move in frames without a frame rate to go by --
        which is a question about the video *and* about the size being moved
        by, since the size is the dialog's now -- and no move of any kind
        until it has loaded.
        """

        is_seekable = block.is_video_initialized and not block.is_live
        in_frames = block.is_sync_offset_in_frames
        moves_frames = self._step_kind() is FRAMES

        for button in moves.findChildren(QPushButton):
            if not button.property(IS_NUDGE):
                button.setEnabled(is_seekable)

                continue

            button.setEnabled(is_seekable and (in_frames or not moves_frames))

    def _set_actions_live(self, by_id):
        """What the dialog-wide moves need."""

        self._fit_button.setEnabled(
            any(reading.envelope for reading in self._readings.values())
        )

    def _offset_mode(self) -> bool:
        if self._is_offset_mode is None:
            return True

        return bool(self._is_offset_mode())

    # --- which video is being worked on ---

    def _set_align_to(self, block_id, refresh=True, by_id=None):
        """Choose the video being worked on: the one a drag moves, and the one
        the overview draws over the rest."""

        self._align_to = block_id

        if refresh:
            self._refresh()

    def _blocks_by_id(self) -> dict:
        return {block.id: block for block in self._blocks()}

    def _name_of(self, block_id, by_id) -> str:
        """What to call a video in the note, kept short enough to read a list
        of."""

        block = by_id.get(block_id)

        if block is None:
            return block_id

        title = self._title_of(block)

        if len(title) <= NAME_WIDTH:
            return title

        return f"{title[:NAME_WIDTH]}…"

    # --- the window the strips are looking through ---

    def _on_view_changed(self, center_ms: int, window_ms: int):
        """One strip was zoomed or dragged: every strip follows.

        A window per strip would be a dialog full of rulers that do not line
        up, which is what this replaces.
        """

        self._apply_view(center_ms, window_ms)

    def _apply_view(self, center_ms: int, window_ms: int):
        self._view_center_ms = int(center_ms)
        self._view_window_ms = int(window_ms)

        self._overview.set_view(self._view_center_ms, self._view_window_ms)

        for row in self._rows.values():
            row["strip"].set_view(self._view_center_ms, self._view_window_ms)

        self._update_view_note()

    def _sound_window(self, by_id) -> tuple[int, int] | None:
        """Everything the strips can show: the reference's reading against
        each row's, all of them on one window."""

        reference = self._sound_of(self._align_to, by_id)

        if reference is None:
            return None

        windows = []

        for block in by_id.values():
            other = self._sound_of(block.id, by_id)

            if other is None or other is reference:
                continue

            window = shared_window(reference, other)

            if window is not None:
                windows.append(window)

        if not windows:
            return reference.start_ms, reference.end_ms

        return min(start for start, _ in windows), max(end for _, end in windows)

    def _fit_view(self):
        """Show the whole of what was read, in every strip at once."""

        window = self._sound_window(self._blocks_by_id())

        if window is None:
            return

        span_ms = window[1] - window[0]

        self._apply_view(
            round((window[0] + window[1]) / 2),
            max(MIN_WINDOW_MS, min(MAX_WINDOW_MS, span_ms)),
        )

    def _update_view_note(self, by_id=None):
        """Say what the window is over, so that it does not have to be
        guessed at from the shapes."""

        if by_id is None:
            by_id = self._blocks_by_id()

        window = self._sound_window(by_id)

        if window is None:
            self._view_note.setText("")
            return

        total_ms = window[1] - window[0]
        shown_ms = min(self._view_window_ms, total_ms)

        if shown_ms >= total_ms - VIEW_SLACK_MS:
            all_of_it = translate(
                "Dialog - Align Videos", "Showing all of it: {TOTAL} s."
            )  # fmt: skip
            self._view_note.setText(all_of_it.format(TOTAL=self._seconds_txt(total_ms)))
            return

        some_of_it = translate(
            "Dialog - Align Videos", "Showing {SHOWN} s of the {TOTAL} s that was read."
        )  # fmt: skip

        self._view_note.setText(
            some_of_it.format(
                SHOWN=self._seconds_txt(shown_ms), TOTAL=self._seconds_txt(total_ms)
            )
        )

    @staticmethod
    def _seconds_txt(ms: int) -> str:
        return f"{ms / 1000:.1f}"

    # --- lining up by sound ---

    @staticmethod
    def _can_measure(block) -> bool:
        """Only files on this machine.

        A stream would have to be fetched again for every reading, holding the
        reading for as long as the snippet lasts, and the recordings this is
        for are files that are already here.
        """

        return block.is_video_initialized and isinstance(block.video_params.uri, Path)

    def _auto_align(self):
        if self._measure.is_running:
            return

        blocks = self._measurable()

        if blocks is None:
            return

        # what the last reading came to is left on show while the next one is
        # read: it is what the strips are drawing, and a comparison half taken
        # away is worse than a stale one
        #
        # kept as they were when the sound was asked for: what comes back was
        # measured against these, and applying it to anything else would be
        # answering a question nobody asked
        self._measured_offsets = {block.id: block.sync_offset_ms for block in blocks}
        self._wide_result = None

        self._set_reading(True)

        # minutes of every recording first, since a misalignment of minutes
        # is not something a snippet read around the moment can see at all
        self._pass = WIDE_PASS
        self._set_sound_note(translate("Dialog - Align Videos", "Reading the sound…"))

        if not self._read(blocks, WIDE_READ_MS, COARSE_SCALE.blocks_per_sec):
            self._set_reading(False)

    def _read_sound(self):
        """Read the sound of every file around where the videos are, and draw it.

        The strips only have something to draw once the sound has been read,
        and reading it is the first thing lining up by sound does -- so a
        viewer who wants to look at the sound, or to look again after moving a
        video by hand, had to press a button that also lines the videos up and
        moves their offsets. This is that reading on its own: nothing is
        judged, no offset is changed and no video is seeked, so it is the same
        thing to do whatever mode the grid is in.
        """

        if self._measure.is_running:
            return

        blocks = self._measurable()

        if blocks is None:
            return

        self._pass = SOUND_PASS
        self._set_reading(True)
        self._set_sound_note(translate("Dialog - Align Videos", "Reading the sound…"))

        if not self._read(blocks, FINE_SNIPPET_MS, BLOCKS_PER_SEC):
            self._set_reading(False)

    def _measurable(self) -> list | None:
        """The videos there is a reading to be had from, or None and a note.

        Reading needs two of them and only files on this machine: see
        `_can_measure`.
        """

        blocks = [block for block in self._blocks() if self._can_measure(block)]

        if len(blocks) >= 2:
            return blocks

        needs_two = translate(
            "Dialog - Align Videos", "Reading the sound needs two videos that are"
            " files on this machine."
        )  # fmt: skip
        self._set_sound_note(needs_two)

        return None

    def _set_reading(self, is_reading: bool):
        """Whether the two buttons that start a run can start one."""

        self._auto_button.setEnabled(not is_reading)
        self._read_button.setEnabled(not is_reading)

    def _read(self, blocks, snippet_ms: int, blocks_per_sec: int) -> bool:
        """Ask for a reading of every one of these, at this size.

        The video's own position is what is read around, which is what makes
        the two passes line up: the wide reading covers the whole range the
        search looks over either side of it, and the close reading covers
        the snippet the wide one already decoded.
        """

        return self._measure.start(
            [
                ReadingRequest(
                    block_id=block.id,
                    uri=str(block.video_params.uri),
                    center_ms=block.time,
                    snippet_ms=snippet_ms,
                    blocks_per_sec=blocks_per_sec,
                )
                for block in blocks
            ]
        )

    def _on_progress(self, done: int, total: int, block_id: str):
        """How far a run of readings has got, said with the name of the file
        that just came back.

        Said at all because the wide reading of a five-hour recording takes
        tens of seconds, and a note that only ever says "reading" is one
        that cannot be told from a dialog that has stopped.
        """

        by_id = self._blocks_by_id()

        if self._pass == CLOSE_PASS:
            reading_txt = translate(
                "Dialog - Align Videos", "Reading the sound closely… {DONE} of"
                " {TOTAL} ({NAME})"
            )  # fmt: skip
        else:
            reading_txt = translate(
                "Dialog - Align Videos", "Reading the sound… {DONE} of {TOTAL}"
                " ({NAME})"
            )  # fmt: skip

        self._set_sound_note(
            reading_txt.format(
                DONE=done, TOTAL=total, NAME=self._name_of(block_id, by_id)
            )
        )

    def _on_measured(self, readings):
        """A run of readings is in: what is done with them depends on what
        asked for them."""

        if self._pass == WIDE_PASS:
            self._measured_wide(readings)
        elif self._pass == SOUND_PASS:
            self._measured_sound(readings)
        else:
            self._measured(readings)

    def _measured_wide(self, readings):
        """What the sound of minutes of every recording came to.

        Every pair of them is compared at a block a second, and the answers
        are put to the videos: which of them the others agree with first, so
        that the snippet the close pass reads is read around a moment that
        already means the same thing in all of them.

        Where nothing agreed, the close pass is read anyway, over the moment
        each video is on: a misalignment small enough for a snippet to see is
        one the wide pass was never needed for.
        """

        by_id = self._blocks_by_id()

        result = align(
            readings,
            self._measured_offsets,
            [block.id for block in by_id.values() if self._can_measure(block)],
            scale=COARSE_SCALE,
        )

        self._wide_result = result

        reference = by_id.get(result.reference_id)

        if reference is not None:
            for alignment in result.alignments:
                block = by_id.get(alignment.block_id)

                if block is None:
                    continue

                was = self._measured_offsets.get(alignment.block_id, 0)

                block.sync_offset_set(was + alignment.shift_ms)

            # And then put them where those offsets say they should be: the
            # close pass reads around the position each video is on, and a
            # video left on the moment it was already showing would have its
            # snippet read around sound nobody else recorded.
            reference.sync_offset_align_others()

        self._start_close_pass()

    def _start_close_pass(self):
        """Read the snippet the close pass works on, around where the videos
        are now."""

        blocks = [block for block in self._blocks() if self._can_measure(block)]

        self._pass = CLOSE_PASS
        self._measured_offsets = {block.id: block.sync_offset_ms for block in blocks}

        close_txt = translate(
            "Dialog - Align Videos", "Reading the sound closely…"
        )  # fmt: skip
        self._set_sound_note(close_txt)

        if self._read(blocks, FINE_SNIPPET_MS, BLOCKS_PER_SEC):
            return

        # nothing was asked for, so what the wide pass alone came to is all
        # there is to say
        self._set_reading(False)

        if self._wide_result is not None:
            self._report(self._wide_result, self._blocks_by_id())

    def _measured_sound(self, readings):
        """The sound, read and drawn, with nothing judged and nothing moved.

        The ruler is measured from the video being worked on: nothing was
        lined up, so there is no recording the rest were judged against to
        take it from, and the one the viewer is pointing at is the one whose
        sound they asked to look at.
        """

        self._set_reading(False)

        by_id = self._blocks_by_id()

        # kept for the strips, which is the whole of what this run is for
        self._readings = readings
        self._sounds = {}

        self._zero_ms = self._zero_of(self._align_to, by_id)

        self._fit_view()

        read_txt = translate(
            "Dialog - Align Videos", "Read the sound of {READ} of {ASKED} files."
            " Nothing has been moved."
        )  # fmt: skip

        self._set_sound_note(read_txt.format(READ=len(readings), ASKED=len(by_id)))

        self._refresh()

    def _zero_of(self, block_id, by_id) -> int | None:
        """The moment a video's own sound was read around, which is what the
        ruler is measured from where nothing has been lined up yet."""

        sound = self._sound_of(block_id, by_id)

        return None if sound is None else sound.read_around_ms

    def _measured(self, readings):
        """What the sound said, put to the videos it was read from."""

        self._set_reading(False)

        blocks = self._blocks()
        by_id = {block.id: block for block in blocks}

        result = align(
            readings,
            self._measured_offsets,
            [block.id for block in blocks if self._can_measure(block)],
        )

        # kept for the strips: the sound that was read is the sound the
        # offsets were worked out from, and looking at it is what there is to
        # fall back on where the numbers are not to be trusted
        self._readings = readings
        self._sounds = {}

        # Which of them the rest were judged against is the sound's to say,
        # not the dialog's: it is the reading the others agree with, and which
        # one that is is only known once they have all been read. What the
        # dialog does with it afterwards is the user's to say.
        self._run_reference_id = result.reference_id
        self._verdicts = result.verdicts

        if result.reference_id is not None:
            self._set_align_to(result.reference_id, refresh=False)

        for alignment in result.alignments:
            block = by_id.get(alignment.block_id)

            if block is None:
                continue

            was = self._measured_offsets.get(alignment.block_id, 0)

            block.sync_offset_set(was + alignment.shift_ms)

        # And then put them where those offsets say they should be. Setting an
        # offset leaves a video on the moment it was already showing -- the
        # offset only says which moment that is, it does not move it -- so on
        # their own the offsets would have the recordings described rightly
        # and the videos no closer together than they were. Seeking them
        # against the reference is what brings them together.
        reference = by_id.get(self._run_reference_id)

        if reference is not None:
            reference.sync_offset_align_others()

            # Zero is the moment the sound was read around, and it is the
            # reference's because every one of these readings was taken
            # around where that video stood: the offsets are tuned under it,
            # and it does not move while they are.
            self._zero_ms = common_moment_ms(reference.time, reference.sync_offset_ms)

        # a new reading is a new stretch of the recordings: show the whole of
        # it rather than whatever was being looked at before
        self._fit_view()

        self._report(result, by_id)
        self._refresh()

    def _report(self, result, by_id):
        """Said plainly, and never as though more happened than did.

        A video whose sound had nothing like the reference's is left where it
        is and shows no score; counting it as lined up would be a lie the
        offsets would go on telling. One that was left for any other reason is
        named with the reason, since from the outside "it did not move" looks
        the same whichever way it came about.

        Both passes are accounted for, and not only the last of them. The
        close pass is the one that places a lag to the millisecond, so its
        answer is the one worth leading with -- but the wide pass has been
        over minutes of every recording by then and may well have moved them
        already. Saying only what the close pass made of its snippet reads as
        though nothing had happened at all, which is exactly what a viewer
        sees after lining a grid up by the minutes and then pausing somewhere
        only one of the recordings covers.
        """

        heard = [
            verdict
            for verdict in result.verdicts.values()
            if verdict.outcome is not Outcome.NO_SOUND
        ]

        if len(heard) < 2:
            nothing_read = translate(
                "Dialog - Align Videos", "Nothing could be read from the sound of"
                " enough files to line anything up."
            )  # fmt: skip
            self._set_sound_note(nothing_read)
            return

        if result.reference_id is None:
            self._set_sound_note(self._nothing_close_note(result, by_id))
            return

        note = self._lined_up_note(result, by_id)

        left = self._left_note(result, by_id)

        self._set_sound_note(self._with_wide_note(f"{note} {left}" if left else note))

    def _lined_up_note(self, result, by_id) -> str:
        """What the close pass did, said with the recording it judged by."""

        lined_up = translate(
            "Dialog - Align Videos", "Lined up {ALIGNED} of {OTHERS} by sound, judged"
            " against {NAME}."
        )  # fmt: skip

        return lined_up.format(
            ALIGNED=len(result.alignments),
            OTHERS=len(result.verdicts) - 1,
            NAME=self._name_of(result.reference_id, by_id),
        )

    def _nothing_close_note(self, result, by_id) -> str:
        """What to say where the close pass trusted none of them.

        Two things can have happened, and they read very differently from the
        outside: the wide pass may have lined them up and left nothing for the
        close one to say about the moment they are on, or nothing anywhere may
        have matched. Both are said for what they are.
        """

        wide = self._wide_result

        if wide is not None and wide.reference_id is not None:
            wide_txt = translate(
                "Dialog - Align Videos", "Lined up to within a second over the"
                " minutes either side, judged against {NAME}, but nothing like that"
                " was heard around the moment the videos are on now."
            )  # fmt: skip

            note = wide_txt.format(
                NAME=self._name_of(wide.reference_id, by_id),
            )

            again_txt = translate(
                "Dialog - Align Videos", "Move the videos to a moment they both hold"
                " and press Align By Sound again."
            )  # fmt: skip

            return f"{note} {again_txt}"

        if wide is not None:
            nothing_lined = translate(
                "Dialog - Align Videos", "Nothing was lined up: no recording was found"
                " that the others agreed with."
            )  # fmt: skip

            return self._with_wide_note(nothing_lined)

        # the close pass on its own, which is what a dialog asked for the
        # sound before this one was ever read comes to
        return self._left_note_only(result, by_id)

    def _left_note_only(self, result, by_id) -> str:
        nothing_lined = translate(
            "Dialog - Align Videos", "Nothing was lined up: no recording was found"
            " that the others agreed with."
        )  # fmt: skip

        left = self._left_note(result, by_id)

        return f"{nothing_lined} {left}" if left else nothing_lined

    def _with_wide_note(self, note: str) -> str:
        """The note, and what the pass over minutes of the recordings came to
        where it came to nothing.

        Said because the close pass then has only its own snippet to go on:
        recordings more than ten minutes apart, or with nothing in common
        across a quarter of an hour of them, read from the outside exactly
        like a sound that matched nothing at all.
        """

        if self._wide_result is None or self._wide_result.reference_id is not None:
            return note

        nothing_wide = translate(
            "Dialog - Align Videos", "Nothing in the minutes either side of it"
            " matched, so only a few seconds either way was looked over."
        )  # fmt: skip

        return f"{note} {nothing_wide}"

    def _left_note(self, result, by_id) -> str:
        """Which of them were left where they were, and why each was."""

        named = [
            f"{self._name_of(block_id, by_id)} ({self._reason_txt(verdict, result)})"
            for block_id, verdict in result.verdicts.items()
            if not verdict.is_lined_up
        ]

        if not named:
            return ""

        left_txt = translate(
            "Dialog - Align Videos", "Left where they were: {NAMED}."
        )  # fmt: skip

        return left_txt.format(NAMED=", ".join(named))

    @staticmethod
    def _reason_txt(verdict, result) -> str:
        if verdict.outcome is Outcome.NO_SOUND:
            return translate("Dialog - Align Videos", "nothing could be read from it")

        if verdict.outcome is Outcome.NOTHING_ALIKE:
            return translate(
                "Dialog - Align Videos", "nothing like the rest was heard in it"
            )

        if verdict.outcome is Outcome.AMBIGUOUS:
            return translate(
                "Dialog - Align Videos", "its sound matched too many moments"
            )

        if verdict.outcome is Outcome.CONTRADICTED:
            disagreed = translate(
                "Dialog - Align Videos", "the sound disagreed about where it goes, by"
                " {GAP}"
            )  # fmt: skip

            return disagreed.format(
                GAP=ms_time_txt(AlignVideosDialog._disagreement(result, verdict))
            )

        return translate(
            "Dialog - Align Videos", "nothing linked it to the rest of the sound"
        )

    @staticmethod
    def _disagreement(result, verdict) -> int:
        """How far out the pairs that disagreed about this one were."""

        return max(
            (
                abs(conflict.residual_ms)
                for conflict in result.conflicts
                if verdict.block_id in (conflict.reference_id, conflict.block_id)
            ),
            default=0,
        )

    def _set_sound_note(self, text: str):
        self._sound_note.setText(text)

    def _update_mode_note(self):
        """Say that the mode is off, and keep the tick in step with it.

        The tick is the menu's setting as well as this dialog's: the mode can
        be turned on or off from the right-click menu while this is open, and
        a tick that went on saying otherwise would be the dialog disagreeing
        with the grid it is lining up.
        """

        is_off = self._is_offset_mode is not None and not self._offset_mode()

        self._mode_note.setVisible(is_off)

        if is_off:
            needs_mode = translate(
                "Dialog - Align Videos", "Aligning needs Sync Offset, which is off."
                " Nothing can be moved until it is on."
            )  # fmt: skip
            self._mode_note.setText(needs_mode)

        if self._set_offset_mode is not None:
            with QSignalBlocker(self._mode_check):
                self._mode_check.setChecked(not is_off)

    def _when_offset_mode_toggled(self, is_on: bool):
        """The tick was clicked: the mode follows it.

        Nothing here knows what the mode was before: turning Sync Offset off
        puts back whichever mode was in force when this turned it on, which is
        the business of whoever owns the setting. See
        `VideoBlocksManager._set_offset_mode`.
        """

        if self._set_offset_mode is not None:
            self._set_offset_mode(is_on)

        self._update_mode_note()

    # --- lifecycle ---

    def closeEvent(self, event):
        self._refresh_timer.stop()

        # waits for a reading in hand rather than leaving it: it holds a
        # player, and a player whose owner has gone is a crash waiting for the
        # sound to stop
        self._measure.cleanup()

        super().closeEvent(event)
