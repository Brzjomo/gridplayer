"""Lining up recordings that begin at different times, all on one screen.

The offsets are set one video at a time, by moving each to the same moment
of what it recorded and marking that moment. Doing that through the context
menu means right-clicking one cell at a time and remembering what the last
one said, so this puts every video, its offset and what it currently holds
in one list, with the moves beside each.

It is deliberately not modal: the videos are moved by hand while it is open,
and a dialog that had to be dismissed before the seek bar could be reached
would be no use for the one thing it is for.
"""

from pathlib import Path

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from gridplayer.utils.qt import translate
from gridplayer.utils.sync_align import AlignMeasure
from gridplayer.utils.sync_audio import compare
from gridplayer.utils.time_txt import ms_time_txt
from gridplayer.widgets.video_block import sync_gap_txt

# How often the rows are read afresh. The videos are moved by hand while
# this is open, and a position lagging behind the picture is worse than no
# position at all.
REFRESH_MS = 200

# The title column, wide enough for a filename and no wider: the offsets
# beside it are what the dialog is for.
TITLE_WIDTH = 210

# Wide enough for the longest of what goes in them without eliding: the
# offset's own format is bounded, a gap is not.
STATE_WIDTH = 300

# How well a recording's sound agreed with the reference's, once it has
# been read. Wide enough for a score and the word for what it came to.
MATCH_WIDTH = 108

# Every column is a fixed width, and that is not tidiness. A position
# counting up wants a wider label than the one it had, the layout makes
# room for it, and the buttons to the right of it move. A button that moves
# between the press and the release is not clicked: the release lands
# somewhere else, or nowhere. Widths that cannot change are what keep the
# buttons where the press found them.
COLUMNS = (
    ("title", TITLE_WIDTH),
    ("position", 88),
    ("offset", 200),
    ("state", STATE_WIDTH),
    ("match", MATCH_WIDTH),
)

# Past the last of the labels, for the moves: worked out rather than
# written down, since a column added above must not land on top of them.
MOVES_COLUMN = len(COLUMNS)

# Coarse and fine moves, each set in the order the numbers run: the two
# sides of a shift belong beside each other, and the one that moves a lot
# beside the one that moves a little.
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
ACTION_BUTTON_WIDTH = 74

# Marks the rows that move a frame rather than a second, so what they need
# to be live can be asked of them without reading their labels back.
IS_FRAMES = "is_frames"

# And the one move that needs the sync mode as well as a video to act on.
IS_ALIGN = "is_align"


class AlignVideosDialog(QDialog):
    """Every video, its sync offset, and where it currently is.

    `blocks_provider` is asked for the videos each time rather than handed
    them once: cells are added and closed while this is open, and a list
    taken when it opened would go on naming ones that are gone.
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

        # what the last reading of the sound came to, by video, and which of
        # them the others were lined up against
        self._reference_id = None
        self._scores: dict[str, float] = {}
        self._measured_offsets: dict[str, int] = {}

        self._measure = AlignMeasure(parent=self)
        self._measure.measured.connect(self._measured)

        self._auto_button = self._button(
            translate("Dialog - Align Videos", "Align By Sound"),
            translate(
                "Dialog - Align Videos",
                "Read the sound of every file and line them up on the first"
                " one, which is what the offsets are worked out from",
            ),
            self._doing(self._auto_align),
            140,
        )

        buttons = QDialogButtonBox(QDialogButtonBox.Close, Qt.Horizontal, self)
        buttons.rejected.connect(self.close)

        layout = QVBoxLayout(self)
        layout.addWidget(self._explain())
        layout.addWidget(self._mode_note)
        layout.addLayout(self._grid)
        layout.addWidget(self._sound_note)
        layout.addWidget(self._auto_button)
        layout.addStretch(1)
        layout.addWidget(buttons)

        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(REFRESH_MS)
        self._refresh_timer.timeout.connect(self._refresh)
        self._refresh_timer.start()

        self._refresh()

    def _explain(self) -> QLabel:
        label = QLabel(
            translate(
                "Dialog - Align Videos",
                "Pause the videos, move each one to the same moment of what it"
                " recorded, and press Set on it. Once every video has one, a"
                " seek on any of them takes the rest to the matching moment.",
            )
        )
        label.setWordWrap(True)

        return label

    def _build_header(self):
        headers = (
            translate("Dialog - Align Videos", "Video"),
            translate("Dialog - Align Videos", "Position"),
            translate("Dialog - Align Videos", "Offset"),
            translate("Dialog - Align Videos", "Holds nothing"),
            translate("Dialog - Align Videos", "Sound"),
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

        The timer runs whether or not anything is being torn down, and a
        cell closed while this is open leaves a name with no widget behind
        it for one tick.
        """

        try:
            self._refresh_rows()
        except RuntimeError:
            pass

    def _refresh_rows(self):
        blocks = list(self._blocks_provider())

        if [b.id for b in blocks] != list(self._rows):
            self._rebuild(blocks)

        for block in blocks:
            self._update_row(block)

        self._update_mode_note()

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

        for column, (key, width) in enumerate(COLUMNS):
            label = QLabel()
            label.setFixedWidth(width)

            row[key] = label

            self._grid.addWidget(label, index, column)

        moves = QWidget()
        moves_layout = QHBoxLayout(moves)
        moves_layout.setContentsMargins(0, 0, 0, 0)
        moves_layout.setSpacing(2)
        moves_layout.addWidget(self._make_buttons(block))

        self._grid.addWidget(moves, index, MOVES_COLUMN)

        row["moves"] = moves

        return row

    def _make_buttons(self, block) -> QWidget:
        holder = QWidget()
        layout = QHBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        # every one of these goes through _doing: a bound method of a video
        # block is a wrapper taking anything, and `clicked` would find the
        # checked flag in its first argument
        align = self._button(
            translate("Dialog - Align Videos", "Align"),
            translate(
                "Dialog - Align Videos",
                "Take every other video to the moment this one is on",
            ),
            self._doing(block.sync_offset_align_others),
            ACTION_BUTTON_WIDTH,
        )
        align.setProperty(IS_ALIGN, True)

        layout.addWidget(align)
        layout.addWidget(
            self._button(
                translate("Dialog - Align Videos", "Set"),
                translate(
                    "Dialog - Align Videos",
                    "Mark where this video is as the moment the others line up on",
                ),
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
                translate(
                    "Dialog - Align Videos",
                    "Play this recording from its own start again",
                ),
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
                "Dialog - Align Videos",
                "Move this recording by that many frames against the others",
            )
            on_press = self._doing(block.sync_offset_shift_frames, amount)
        else:
            tooltip = translate(
                "Dialog - Align Videos",
                "Move this recording by that many seconds against the others",
            )
            on_press = self._doing(block.sync_offset_shift_ms, amount)

        button = self._button(text, tooltip, on_press, NUDGE_BUTTON_WIDTH)

        if is_frames:
            button.setProperty(IS_FRAMES, True)

        return button

    @staticmethod
    def _doing(func, *args):
        """A click handler that takes nothing of its own.

        `clicked` carries a checked flag and hands it to whichever slot will
        take it, and a bound method of a video block is wrapped as one that
        takes anything. Called with the flag, that wrapper finds it where
        the video itself should be -- and a bool has no video to act on, so
        the click comes to nothing at all.
        """

        def handler():
            func(*args)

        return handler

    @staticmethod
    def _button(text: str, tooltip: str, on_click, width: int) -> QPushButton:
        button = QPushButton(text)
        button.setToolTip(tooltip)
        button.setFixedWidth(width)

        # pressed, not clicked. A move asked for is worth making at once,
        # and making it on the press is making it before anything can take
        # the focus or shift the button out from under the release -- either
        # of which costs the click, and this dialog is one that has to be
        # used while the videos behind it are still live.
        button.pressed.connect(on_click)

        return button

    def _update_row(self, block):
        row = self._rows.get(block.id)

        if row is None:
            return

        title = self._title_of(block)

        row["title"].setText(
            row["title"].fontMetrics().elidedText(title, Qt.ElideMiddle, TITLE_WIDTH)
        )
        row["title"].setToolTip(title)
        row["position"].setText(ms_time_txt(block.time))
        row["offset"].setText(block.get_sync_offset_txt())

        state = self._state_of(block)

        # elided rather than clipped: a gap longer than the column would
        # otherwise lose the end of itself, which is the part that says
        # which way it goes
        row["state"].setText(
            row["state"].fontMetrics().elidedText(state, Qt.ElideRight, STATE_WIDTH)
        )
        row["state"].setToolTip(state)
        row["match"].setText(self._match_txt(block))

        self._set_moves_live(block, row["moves"])

    def _match_txt(self, block) -> str:
        """How well this one's sound agreed, and which it was judged against."""

        if self._reference_id == block.id:
            return translate("Dialog - Align Videos", "reference")

        score = self._scores.get(block.id)

        if score is None:
            return ""

        return f"{score:.2f}"

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

        # read from the moment being asked for, which is where the viewer
        # is looking: the recording is not there yet, or is already past
        if is_before:
            return translate("Dialog - Align Videos", "starts in {GAP}").format(GAP=gap)

        return translate("Dialog - Align Videos", "ended {GAP} ago").format(GAP=gap)

    def _set_moves_live(self, block, moves: QWidget):
        """What each move needs before it can do anything.

        A video offers no move in frames without a frame rate to go by, and
        no move of any kind until it has loaded. Align is the one that needs
        the sync mode as well: outside it the offsets are not what seeks are
        carried by, so the move does nothing and a button that looks live
        and does nothing is worse than one that says why.
        """

        is_seekable = block.is_video_initialized and not block.is_live
        in_frames = block.is_sync_offset_in_frames
        is_offset_mode = self._offset_mode()

        for button in moves.findChildren(QPushButton):
            if button.property(IS_ALIGN):
                button.setEnabled(is_seekable and is_offset_mode)
                continue

            wants_frames = bool(button.property(IS_FRAMES))

            button.setEnabled(is_seekable and (in_frames or not wants_frames))

    def _offset_mode(self) -> bool:
        if self._is_offset_mode is None:
            return True

        return bool(self._is_offset_mode())

    # --- lining up by sound ---

    def _blocks_by_id(self) -> dict:
        return {block.id: block for block in self._blocks()}

    @staticmethod
    def _can_measure(block) -> bool:
        """Only files on this machine.

        A stream would have to be fetched again for every reading, holding
        the reading for as long as the snippet lasts, and the recordings
        this is for are files that are already here.
        """

        return block.is_video_initialized and isinstance(block.video_params.uri, Path)

    def _auto_align(self):
        if self._measure.is_running:
            return

        blocks = [block for block in self._blocks() if self._can_measure(block)]

        if len(blocks) < 2:
            self._set_sound_note(
                translate(
                    "Dialog - Align Videos",
                    "Lining up by sound needs two videos that are files on"
                    " this machine.",
                )
            )
            return

        self._reference_id = blocks[0].id
        self._scores = {}

        # kept as they were when the sound was asked for: what comes back
        # was measured against these, and applying it to anything else
        # would be answering a question nobody asked
        self._measured_offsets = {block.id: block.sync_offset_ms for block in blocks}

        self._auto_button.setEnabled(False)
        self._set_sound_note(translate("Dialog - Align Videos", "Reading the sound…"))

        self._measure.start(
            [(block.id, str(block.video_params.uri), block.time) for block in blocks]
        )

    def _measured(self, readings):
        """What the sound said, put to the videos it was read from."""

        self._auto_button.setEnabled(True)

        if self._reference_id not in readings:
            self._set_sound_note(
                translate(
                    "Dialog - Align Videos",
                    "Nothing could be read from the first video's sound, so"
                    " nothing was lined up.",
                )
            )
            return

        found, self._scores = compare(
            self._reference_id, readings, self._measured_offsets
        )

        by_id = self._blocks_by_id()
        aligned = 0

        for alignment in found:
            block = by_id.get(alignment.block_id)

            if block is None:
                continue

            was = self._measured_offsets.get(alignment.block_id, 0)

            block.sync_offset_set(was + alignment.shift_ms)
            aligned += 1

        # And then put them where those offsets say they should be. Setting
        # an offset leaves a video on the moment it was already showing --
        # the offset only says which moment that is, it does not move it --
        # so on their own the offsets would have the recordings described
        # rightly and the videos no closer together than they were. Seeking
        # them against the reference is what brings them together.
        reference = by_id.get(self._reference_id)

        if reference is not None:
            reference.sync_offset_align_others()

        self._report(aligned, len(readings) - 1)
        self._refresh()

    def _report(self, aligned: int, others: int):
        """Said plainly, and never as though more happened than did.

        A video whose sound had nothing like the reference's is left where
        it is and shows no score; counting it as lined up would be a lie
        the offsets would go on telling.
        """

        self._set_sound_note(
            translate(
                "Dialog - Align Videos",
                "Lined up {ALIGNED} of {OTHERS} by sound.",
            ).format(ALIGNED=aligned, OTHERS=others)
        )

    def _set_sound_note(self, text: str):
        self._sound_note.setText(text)

    def _update_mode_note(self):
        is_missing = self._is_offset_mode is not None and not self._offset_mode()

        self._mode_note.setVisible(is_missing)

        if is_missing:
            self._mode_note.setText(
                translate(
                    "Dialog - Align Videos",
                    "Align needs Sync Offset mode, which is off. Turn it on"
                    " under Seek Sync in the right-click menu.",
                )
            )

    # --- lifecycle ---

    def closeEvent(self, event):
        self._refresh_timer.stop()

        # waits for a reading in hand rather than leaving it: it holds a
        # player, and a player whose owner has gone is a crash waiting for
        # the sound to stop
        self._measure.cleanup()

        super().closeEvent(event)
