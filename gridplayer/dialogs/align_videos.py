"""Lining up recordings that begin at different times, all on one screen.

An offset says which moment of a recording the videos are meant to be sharing,
so lining a grid up is one question asked of every video: where in this
recording is the moment the others are on. This window asks it in the two ways
it can be answered, and shows the answer as it changes.

**By sound, first.** *Align By Sound* reads every file, works out which video
the others agree with, sets the offsets from that and moves the videos onto it.
It is the only way to do it that does not need a person to recognise anything,
and it reports what it made of each video.

**By hand, when it cannot be trusted.** Pick the video the others are to be
lined up against -- *Align to* -- put it on a moment all of them recorded, and
press *Align others to it*. A video that is still out is moved by the buttons
beside it, one frame or one second at a time, until the two runs of sound agree
at the zero of the strip under its row. A video whose sound is no use at all
can simply be put where it belongs by hand and marked with *Set point*.

The strips are what makes the hand path work: one ruler shared by every row, a
line down each of them at the moment the videos are on, and the reference's
sound drawn under that row's. They are also what makes the automatic path
checkable, which is the point of them -- a score can be wrong, a shape that
does not match the shape beside it is not.

It is deliberately not modal: the videos are moved by hand while it is open,
and a dialog that had to be dismissed before the seek bar could be reached
would be no use for the one thing it is for.
"""

from pathlib import Path

from PyQt5.QtCore import QSignalBlocker, Qt, QTimer
from PyQt5.QtWidgets import (
    QButtonGroup,
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

from gridplayer.utils.qt import translate
from gridplayer.utils.sync_align import AlignMeasure
from gridplayer.utils.sync_audio import (
    Outcome,
    Sound,
    Verdict,
    align,
    common_moment_ms,
    shared_window,
)
from gridplayer.utils.time_txt import ms_time_txt
from gridplayer.widgets.sync_strip import (
    DEFAULT_WINDOW_MS,
    MAX_WINDOW_MS,
    MIN_WINDOW_MS,
    SyncEnvelopeStrip,
)
from gridplayer.widgets.video_block import sync_gap_txt

# How often the rows are read afresh. The videos are moved by hand while
# this is open, and a position lagging behind the picture is worse than no
# position at all.
REFRESH_MS = 200

# The title column, wide enough for a filename and no wider: the offsets
# beside it are what the dialog is for.
TITLE_WIDTH = 210

# Wide enough for a radio and no wider: it is a tick, and the header above it
# says what it means.
ALIGN_TO_WIDTH = 60

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

# Coarse and fine moves, each set in the order the numbers run: the two sides
# of a shift belong beside each other, and the one that moves a lot beside the
# one that moves a little.
SECONDS_NUDGES = (
    ("-10s", -10_000),
    ("-1s", -1_000),
    ("+1s", 1_000),
    ("+10s", 10_000),
)

FRAME_NUDGES = (("-10f", -10), ("-1f", -1), ("+1f", 1), ("+10f", 10))

# Between the seconds and the frames, so the two sets read apart.
NUDGE_GROUP_GAP = 10

NUDGE_BUTTON_WIDTH = 46
ACTION_BUTTON_WIDTH = 84
ALIGN_BUTTON_WIDTH = 280

# How close to the whole of what was read the window has to be before it is
# called the whole of it: a fit lands a rounding away from the span.
VIEW_SLACK_MS = 50

# Marks the rows that move a frame rather than a second, so what they need to
# be live can be asked of them without reading their labels back.
IS_FRAMES = "is_frames"


class AlignVideosDialog(QDialog):
    """Every video, its sync point, and where it currently is.

    `blocks_provider` is asked for the videos each time rather than handed
    them once: cells are added and closed while this is open, and a list taken
    when it opened would go on naming ones that are gone.
    """

    def __init__(self, blocks_provider, is_offset_mode=None, parent=None):
        super().__init__(parent)

        self._blocks_provider = blocks_provider
        self._is_offset_mode = is_offset_mode

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

        # Which video the others are to be aligned to, and which one the last
        # reading of the sound judged them against. They are the same thing
        # until somebody says otherwise, and then both are worth showing: the
        # scores are a record of the run, this is what the buttons use.
        self._align_to = None
        self._run_reference_id = None
        self._align_group = QButtonGroup(self)

        # what the last reading of the sound came to, by video. The readings
        # themselves are kept too: they are what the strips draw.
        self._verdicts: dict[str, Verdict] = {}
        self._measured_offsets: dict[str, int] = {}
        self._readings: dict = {}
        self._sounds: dict[str, tuple[Sound, int]] = {}

        # One window on the shared clock for every strip in the dialog. This
        # is the only place it lives: see SyncEnvelopeStrip.
        self._view_center_ms = None
        self._view_window_ms = DEFAULT_WINDOW_MS

        self._measure = AlignMeasure(parent=self)
        self._measure.measured.connect(self._measured)

        # every string that goes into a widget is built on a line of its own
        # first: `pylupdate5` reads the text of a `translate()` call only when
        # it begins on the same line as the context, and the formatter moves
        # the text off that line for any call that does not fit on one line --
        # which every call nested inside another one is. See `# fmt: skip`
        # below, and translate_extraction in `tests/`.
        axis_note_txt = self._axis_note_txt()
        self._axis_note = QLabel(axis_note_txt)
        self._axis_note.setWordWrap(True)

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

        align_tooltip = translate(
            "Dialog - Align Videos", "Take every other video to the moment the one"
            " in Align to is on now"
        )  # fmt: skip

        self._align_button = self._button(
            self._align_button_txt(),
            align_tooltip,
            self._doing(self._align_others),
            ALIGN_BUTTON_WIDTH,
        )

        buttons = QDialogButtonBox(QDialogButtonBox.Close, Qt.Horizontal, self)
        buttons.rejected.connect(self.close)

        actions = QHBoxLayout()
        actions.addWidget(self._auto_button)
        actions.addWidget(self._align_button)
        actions.addStretch(1)
        actions.addWidget(buttons)

        caption = QHBoxLayout()
        caption.addWidget(self._axis_note)
        caption.addStretch(1)
        caption.addWidget(self._view_note)
        caption.addWidget(self._fit_button)

        layout = QVBoxLayout(self)
        layout.addWidget(self._explain())
        layout.addWidget(self._mode_note)
        layout.addLayout(self._grid)
        layout.addLayout(caption)
        layout.addWidget(self._sound_note)
        layout.addStretch(1)
        layout.addLayout(actions)

        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(REFRESH_MS)
        self._refresh_timer.timeout.connect(self._refresh)
        self._refresh_timer.start()

        self._refresh()

    @staticmethod
    def _axis_note_txt() -> str:
        return translate(
            "Dialog - Align Videos", "Each strip: the reference's sound with that"
            " video's over it, and seconds running from zero, the middle of the"
            " reference's sound."
        )  # fmt: skip

    def _explain(self) -> QLabel:
        text = translate(
            "Dialog - Align Videos", "Pause the videos and press Align By Sound: it"
            " reads the sound of every file, works the offsets out and moves them"
            " onto the moment they share. Where its answer is not to be trusted,"
            " line them up by hand instead: choose the video to align the rest to"
            " under Align to, put it on a moment they all recorded, and press"
            " Align others to it. What is still out is moved one frame or one"
            " second at a time by the buttons beside it, until the shapes in its"
            " strip agree at zero. Set point marks where a video is by hand, for a"
            " recording the sound cannot help with."
        )  # fmt: skip

        label = QLabel(text)
        label.setWordWrap(True)

        return label

    def _build_header(self):
        headers = (
            translate("Dialog - Align Videos", "Align to"),
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

            self._grid.addWidget(header, 0, column)

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
        labels_at = index * 2 - 1

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

        strip = SyncEnvelopeStrip()
        strip.view_changed.connect(self._on_view_changed)
        strip.view_reset.connect(self._fit_view)

        self._grid.addWidget(strip, labels_at + 1, 0, 1, MOVES_COLUMN + 1)

        row["strip"] = strip

        return row

    def _make_align_radio(self, block) -> QRadioButton:
        """The one tick that decides what everything else is measured from."""

        radio = QRadioButton()
        radio.setFixedWidth(ALIGN_TO_WIDTH)
        radio.setToolTip(
            translate("Dialog - Align Videos", "Align the other videos to this one")
        )
        radio.toggled.connect(self._when_checked(self._set_align_to, block.id))

        self._align_group.addButton(radio)

        return radio

    def _make_buttons(self, block) -> QWidget:
        holder = QWidget()
        layout = QHBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        set_point_tooltip = translate(
            "Dialog - Align Videos", "Mark where this video is as the moment the"
            " others line up on"
        )  # fmt: skip
        reset_tooltip = translate(
            "Dialog - Align Videos", "Play this recording from its own start again"
        )

        # every one of these goes through _doing: a bound method of a video
        # block is a wrapper taking anything, and `clicked` would find the
        # checked flag in its first argument
        layout.addWidget(
            self._button(
                translate("Dialog - Align Videos", "Set point"),
                set_point_tooltip,
                self._doing(block.set_sync_point_here),
                ACTION_BUTTON_WIDTH,
            )
        )

        for text, amount in SECONDS_NUDGES:
            layout.addWidget(self._nudge(block, text, amount, is_frames=False))

        layout.addSpacing(NUDGE_GROUP_GAP)

        for text, amount in FRAME_NUDGES:
            layout.addWidget(self._nudge(block, text, amount, is_frames=True))

        layout.addWidget(
            self._button(
                translate("Dialog - Align Videos", "Reset"),
                reset_tooltip,
                self._doing(block.sync_offset_reset),
                ACTION_BUTTON_WIDTH,
            )
        )

        return holder

    def _nudge(self, block, text: str, amount: int, is_frames: bool) -> QPushButton:
        """One move of one video, by what its label says.

        The label carries the size and the sign, so the tooltip only has to
        say what is being moved and against what.
        """

        if is_frames:
            tooltip = translate(
                "Dialog - Align Videos", "Move this recording by that many frames"
                " against the others"
            )  # fmt: skip
            on_press = self._doing(block.sync_offset_shift_frames, amount)
        else:
            tooltip = translate(
                "Dialog - Align Videos", "Move this recording by that many seconds"
                " against the others"
            )  # fmt: skip
            on_press = self._doing(block.sync_offset_shift_ms, amount)

        button = self._button(text, tooltip, on_press, NUDGE_BUTTON_WIDTH)

        if is_frames:
            button.setProperty(IS_FRAMES, True)

        return button

    @staticmethod
    def _when_checked(func, *args):
        """A handler for a tick, which only acts when the tick goes on.

        `toggled` fires for both directions, and the one going off is not the
        one a finger chose: the video the others are aligned to would end up
        being whichever row happened to be unticked last.
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

        row["strip"].show_pair(
            self._sound_of(self._align_to, by_id),
            self._sound_of(block.id, by_id),
            common_moment_ms(block.time, block.sync_offset_ms),
        )

        self._set_moves_live(block, row["moves"])

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

        A video offers no move in frames without a frame rate to go by, and no
        move of any kind until it has loaded.
        """

        is_seekable = block.is_video_initialized and not block.is_live
        in_frames = block.is_sync_offset_in_frames

        for button in moves.findChildren(QPushButton):
            wants_frames = bool(button.property(IS_FRAMES))

            button.setEnabled(is_seekable and (in_frames or not wants_frames))

    def _set_actions_live(self, by_id):
        """What the two dialog-wide moves need."""

        is_offset_mode = self._offset_mode()
        is_seekable = any(
            block.is_video_initialized and not block.is_live for block in by_id.values()
        )

        self._align_button.setEnabled(
            is_seekable
            and is_offset_mode
            and self._align_to in by_id
            and len(by_id) > 1
        )

        self._fit_button.setEnabled(
            any(reading.envelope for reading in self._readings.values())
        )

    def _offset_mode(self) -> bool:
        if self._is_offset_mode is None:
            return True

        return bool(self._is_offset_mode())

    # --- what is aligned to what ---

    def _set_align_to(self, block_id, refresh=True, by_id=None):
        """Choose the video the rest are aligned to, and say so on the button."""

        self._align_to = block_id

        self._align_button.setText(
            self._align_button_txt(by_id if by_id is not None else self._blocks_by_id())
        )

        if refresh:
            self._refresh()

    def _align_button_txt(self, by_id=None) -> str:
        """Named, not a bare verb: which video the others go to is the whole
        of what the button does, and it is not always the one it was.

        Named only where the grid has been looked at: this is asked for
        while the button is being built, before there is anything to name.
        """

        if by_id is None:
            return translate("Dialog - Align Videos", "Align others to it")

        if self._align_to not in by_id:
            return translate("Dialog - Align Videos", "Align others to it")

        return translate("Dialog - Align Videos", "Align others to {NAME}").format(
            NAME=self._name_of(self._align_to, by_id)
        )

    def _align_others(self):
        """Take every other video to the moment the chosen one is on.

        What a seek already does, sent for a video that has been put where it
        wanted to be rather than moved there: the offsets are what carry it,
        so nothing here needs to know them.
        """

        block = self._blocks_by_id().get(self._align_to)

        if block is not None:
            block.sync_offset_align_others()

    def _blocks_by_id(self) -> dict:
        return {block.id: block for block in self._blocks()}

    def _name_of(self, block_id, by_id) -> str:
        """What to call a video in the note and on the button, kept short
        enough to read a list of."""

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

        blocks = [block for block in self._blocks() if self._can_measure(block)]

        if len(blocks) < 2:
            needs_two = translate(
                "Dialog - Align Videos", "Lining up by sound needs two videos that"
                " are files on this machine."
            )  # fmt: skip
            self._set_sound_note(needs_two)
            return

        # what the last reading came to is left on show while the next one is
        # read: it is what the strips are drawing, and a comparison half taken
        # away is worse than a stale one
        #
        # kept as they were when the sound was asked for: what comes back was
        # measured against these, and applying it to anything else would be
        # answering a question nobody asked
        self._measured_offsets = {block.id: block.sync_offset_ms for block in blocks}

        self._auto_button.setEnabled(False)
        self._set_sound_note(translate("Dialog - Align Videos", "Reading the sound…"))

        self._measure.start(
            [(block.id, str(block.video_params.uri), block.time) for block in blocks]
        )

    def _measured(self, readings):
        """What the sound said, put to the videos it was read from."""

        self._auto_button.setEnabled(True)

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

        lined_up = translate(
            "Dialog - Align Videos", "Lined up {ALIGNED} of {OTHERS} by sound, judged"
            " against {NAME}."
        )  # fmt: skip

        note = lined_up.format(
            ALIGNED=len(result.alignments),
            OTHERS=len(result.verdicts) - 1,
            NAME=self._name_of(result.reference_id, by_id),
        )

        left = self._left_note(result, by_id)

        self._set_sound_note(f"{note} {left}" if left else note)

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
        is_missing = self._is_offset_mode is not None and not self._offset_mode()

        self._mode_note.setVisible(is_missing)

        if is_missing:
            needs_mode = translate(
                "Dialog - Align Videos", "Align needs Sync Offset mode, which is off."
                " Turn it on under Seek Sync in the right-click menu."
            )  # fmt: skip
            self._mode_note.setText(needs_mode)

    # --- lifecycle ---

    def closeEvent(self, event):
        self._refresh_timer.stop()

        # waits for a reading in hand rather than leaving it: it holds a
        # player, and a player whose owner has gone is a crash waiting for the
        # sound to stop
        self._measure.cleanup()

        super().closeEvent(event)
