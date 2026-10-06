"""The sound of every video, drawn against the clock they are meant to share.

Automatic alignment answers with a number and a confidence, and there are
recordings where neither is to be trusted: the sound is faint, every moment
of it sounds like another, or the answer is a second out with a score that
looks healthy. What no number takes away is that a person can look at two
runs of sound and see whether they are the same run of sound, and by how
much they miss each other. That is what this draws -- the sound and not the
picture, since three cameras pointed at one event have far more sound in
common than picture.

Four things make it readable, and all four are deliberate:

* **The clock is the one every recording shares** (`Sound`, `common_moment_ms`).
  Where a run of sound sits on it is its own moment less the offset it has
  been given, so two offsets that are right put the same sound in the same
  place. Zero is the moment the readings were taken around -- where the
  videos were when their sound was read -- and it stays put while the
  offsets are tuned under it.
* **Each row draws only its own sound.** What the other recordings are doing
  is the overview strip's to say, and a row carrying another recording's
  shape as well would be two things to read at once in the room for one.
* **The overview draws them all, one over another**, each in its own colour
  and through itself, and the recording being worked on last and solid: the
  one place a peak of one recording is seen against a peak of another, which
  is the whole question being asked of the eye.
* **One ruler, at the top.** The strips of a dialog share one window, which
  the dialog owns and hands to every one of them, so the band above the
  overview reads as the scale for everything below it -- and how much of the
  sound that window covers is said in words above the strips rather than
  left to be guessed at.
"""

import math
from dataclasses import dataclass

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QColor, QFontMetrics, QPainter, QPen
from PyQt5.QtWidgets import QWidget

from gridplayer.utils.qt import translate
from gridplayer.utils.sync_audio import Sound

# How tall a strip is: enough sound under the ruler's band for the shape of a
# peak to be read off, and short enough that a grid of them still fits on one
# screen. A row has no ruler of its own -- there is one band, at the top --
# so it is that much shorter.
OVERVIEW_STRIP_HEIGHT = 92

ROW_STRIP_HEIGHT = 44

AXIS_HEIGHT = 15

# What is on show to begin with, and how far it can be taken either way.
# Ten seconds holds a few sounds and still shows a second of misalignment as
# a hand's width; the rest is what zooming is for.
DEFAULT_WINDOW_MS = 10_000
MIN_WINDOW_MS = 200
MAX_WINDOW_MS = 300_000

# One notch of the wheel, and the space left under a bar for the line the
# bars stand on.
ZOOM_STEP = 1.25
BASELINE_PX = 3

# The steps a label may sit on, coarsest first: a ruler numbered 1, 1.1, 1.2
# is not a ruler. Whichever of these leaves room for its own label is used.
TICK_STEPS_MS = (
    120_000,
    60_000,
    30_000,
    15_000,
    10_000,
    5_000,
    2_000,
    1_000,
    500,
    250,
    100,
    50,
    20,
    10,
)

# How much room a label wants: a step is chosen that leaves at least this
# much room between neighbours, measured against a strip of this width.
MIN_TICK_GAP_PX = 54
ASSUMED_STRIP_WIDTH_PX = 400

HINT_ALPHA = 110
FRAME_ALPHA = 70
MARKER_ALPHA = 70
AXIS_ALPHA = 130
ZERO_ALPHA = 110

# A row's own sound, and the same sound where it is one of several drawn over
# each other: through itself, so that a colour seen darker is a place where
# more than one recording has sound. The one being worked on is drawn last and
# a little more solidly, which lifts it out of the others without hiding them
# -- it is drawn for them to be read against, not instead of them.
SOUND_ALPHA = 210
OVERLAY_ALPHA = 120
PICKED_ALPHA = 175


@dataclass(frozen=True)
class DrawnSound:
    """One recording's sound as the overview is to draw it.

    `is_picked` is the one being worked on: drawn last and solid, so that
    moving it is watched rather than hunted for among the others.
    """

    sound: Sound
    colour: str
    is_picked: bool = False


class _ClockStrip(QWidget):
    """Something drawn against the clock every recording is meant to share.

    **The window is not this widget's to keep.** Zooming or dragging says so
    on `view_changed`, and whoever owns the strips hands the same window to
    every one of them: two strips showing different stretches of one clock
    are two strips that cannot be read against each other.

    What each kind of strip adds is what it draws. The window, the wheel, the
    drag and the drawing of one run of sound are here.
    """

    view_changed = pyqtSignal(int, int)

    def __init__(self, parent=None):
        super().__init__(parent)

        self._window_ms = DEFAULT_WINDOW_MS
        self._center_ms = 0
        self._zero_ms = None
        self._now_ms = None

        self._dragged_from = None

    # --- where the window is ---

    def set_view(self, center_ms: int, window_ms: int):
        self._center_ms = int(center_ms)
        self._window_ms = int(window_ms)

        self.update()

    def _shown_ms(self) -> tuple[float, float]:
        half = self._window_ms / 2

        return self._center_ms - half, self._center_ms + half

    def _ms_per_pixel(self) -> float:
        return self._window_ms / max(1, self.width())

    def _ms_at(self, x: float) -> float:
        from_ms, _to_ms = self._shown_ms()

        return from_ms + x * self._ms_per_pixel()

    def _x_of(self, ms: float) -> float:
        from_ms, _to_ms = self._shown_ms()

        return (ms - from_ms) / self._ms_per_pixel()

    # --- moving it about ---

    def wheelEvent(self, event):
        notches = event.angleDelta().y() / 120

        if not notches:
            return

        # whatever is under the cursor stays under it, so that zooming in on
        # a sound does not also mean hunting for it again
        under_cursor = self._ms_at(event.pos().x())

        was_ms = self._window_ms
        window_ms = _clamp(
            round(was_ms / ZOOM_STEP**notches), MIN_WINDOW_MS, MAX_WINDOW_MS
        )
        center_ms = round(
            under_cursor + (self._center_ms - under_cursor) * window_ms / was_ms
        )

        self.view_changed.emit(center_ms, window_ms)
        event.accept()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._dragged_from = (event.pos().x(), self._center_ms)

        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._dragged_from is not None:
            from_x, from_center = self._dragged_from

            self.view_changed.emit(
                round(from_center - (event.pos().x() - from_x) * self._ms_per_pixel()),
                self._window_ms,
            )

        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._dragged_from = None

        super().mouseReleaseEvent(event)

    def resizeEvent(self, event):
        self.update()

        super().resizeEvent(event)

    # --- the drawing itself ---

    def _top_px(self) -> int:
        """Where the sound begins: under the ruler where a strip has one."""

        return 1

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), self.palette().color(self.backgroundRole()))
        painter.setPen(QPen(_text_colour(self, FRAME_ALPHA)))
        painter.drawRect(self.rect().adjusted(0, 0, -1, -1))

        if self._zero_ms is None:
            self._paint_nothing_read(painter)
            return

        self._paint_above(painter)
        self._paint_zero(painter)
        self._paint_contents(painter)
        self._paint_now(painter)

    def _paint_above(self, painter):
        """The band above the sound, where a strip has one."""

    def _paint_contents(self, painter):
        """The sound, and whatever else this kind of strip draws."""

    def _paint_ruler(self, painter):
        """A band of ticks along the top, labelled in seconds from zero.

        The same band, on the same window, over every strip of a dialog, so
        the labels read as one scale down the whole of it.
        """

        from_ms, to_ms = self._shown_ms()
        step_ms = tick_step_ms(self._window_ms, self.width())

        painter.setPen(QPen(_text_colour(self, AXIS_ALPHA)))

        metrics = QFontMetrics(self.font())

        for at_ms in ticks_between(from_ms, to_ms, step_ms):
            x = round(self._x_of(at_ms))

            painter.drawLine(x, AXIS_HEIGHT - 5, x, AXIS_HEIGHT - 2)

            label = tick_label(at_ms - self._zero_ms, step_ms)
            width = metrics.horizontalAdvance(label)

            painter.drawText(
                min(max(0, x - width // 2), max(0, self.width() - width)),
                0,
                width,
                AXIS_HEIGHT - 4,
                Qt.AlignHCenter | Qt.AlignVCenter,
                label,
            )

        painter.drawLine(0, AXIS_HEIGHT - 1, self.width(), AXIS_HEIGHT - 1)

    def _paint_zero(self, painter):
        """The line the ear is being asked about: the sounds should agree
        across it."""

        from_ms, to_ms = self._shown_ms()

        if not from_ms <= self._zero_ms <= to_ms:
            return

        x = round(self._x_of(self._zero_ms))

        painter.setPen(QPen(_text_colour(self, ZERO_ALPHA)))
        painter.drawLine(x, self._top_px(), x, self.height() - BASELINE_PX)

    def _paint_sound(self, painter, sound: Sound | None, colour: QColor):
        if sound is None or not sound.envelope:
            return

        from_ms, to_ms = self._shown_ms()

        # drawn only where it is: a stretch of silence on either side of a
        # reading is not a reading that says the recording was silent
        first = max(from_ms, sound.start_ms)
        last = min(to_ms, sound.end_ms)

        if last <= first:
            return

        left = max(0, math.floor(self._x_of(first)))
        right = min(self.width(), math.ceil(self._x_of(last)))
        columns = right - left

        if columns <= 0:
            return

        levels = sound.levels_in(first, last, columns)
        peak = max(levels, default=0.0)

        if peak <= 0:
            return

        # each drawn to its own loudest: two cameras record one event at two
        # levels, and what is being compared is the shape of it
        painter.setPen(QPen(colour))
        baseline = self.height() - BASELINE_PX
        tallest = baseline - self._top_px()

        for column, level in enumerate(levels):
            bar = round(level / peak * tallest)

            if bar > 0:
                painter.drawLine(left + column, baseline, left + column, baseline - bar)

    def _paint_now(self, painter):
        """Where this video is now, which need not be on show at all: the
        window is over the sound that was read, and the video has been moved
        since it was."""

        if self._now_ms is None:
            return

        x = self._x_of(self._now_ms)

        if not 0 <= x <= self.width():
            return

        painter.setPen(QPen(_highlight_colour(self, MARKER_ALPHA)))
        painter.drawLine(
            round(x), self._top_px(), round(x), self.height() - BASELINE_PX - 1
        )

    def _paint_nothing_read(self, painter):
        text = translate("Dialog - Align Videos", "No sound read yet")

        painter.setPen(QPen(_text_colour(self, HINT_ALPHA)))
        painter.drawText(
            self.rect(),
            Qt.AlignCenter,
            QFontMetrics(self.font()).elidedText(text, Qt.ElideRight, self.width()),
        )


class SyncSoundStrip(_ClockStrip):
    """One video's sound, on the clock every recording shares, and where that
    video is on it.

    It is given `Sound`s rather than samples: the readings are the ones the
    last run of measurement took, kept by whoever owns this, and they are
    placed by the offsets as those stand at the moment of drawing -- which is
    what makes a nudge visible as it happens.

    **Dragging it does one of two things**, and which one is the dialog's to
    say by way of `is_picked`. On the video being worked on, a drag moves
    that recording's sound along the clock and says so on `sound_dragged`:
    the sound follows the finger and no other row moves, which is what makes
    an offset something that can be felt for rather than typed at. On any
    other row, a drag moves the window everyone is looking through, because
    a strip that cannot be dragged at all is a strip that cannot be looked
    along.
    """

    color_requested = pyqtSignal()

    # where the drag would put the offset: how far the sound has been moved
    # from where the drag began, taken off the offset it began on; and the
    # drag settling or being given up
    sound_dragged = pyqtSignal(int)
    sound_dropped = pyqtSignal()
    sound_drag_given_up = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)

        self._sound = None
        self._colour = None
        self._is_picked = False

        self._sound_dragged_from = None

        self.setFixedHeight(ROW_STRIP_HEIGHT)

        self.setToolTip(self._tooltip_txt())

    @staticmethod
    def _tooltip_txt() -> str:
        return translate(
            "Dialog - Align Videos", "This video's sound, against the clock the"
            " videos share. Wheel to zoom, double-click to choose its colour, and"
            " drag the row of the video being worked on to move its sound."
        )  # fmt: skip

    def show_sound(
        self, sound, colour: str, now_ms=None, zero_ms=None, is_picked=False
    ):
        """This video's sound, where it is now, and where zero is.

        Called every time the dialog refreshes its rows, several times a
        second, so a strip that is being handed what it already has is left
        alone: the sounds are held by the caller precisely so that this can
        be told by looking at them.
        """

        shown = (sound, colour, now_ms, zero_ms, is_picked)

        if shown == (
            self._sound,
            self._colour,
            self._now_ms,
            self._zero_ms,
            self._is_picked,
        ):
            return

        self._sound, self._colour, self._now_ms, self._zero_ms, self._is_picked = shown

        self.update()

    def mouseDoubleClickEvent(self, event):
        self.color_requested.emit()

        super().mouseDoubleClickEvent(event)

    # --- dragging it ---

    def mousePressEvent(self, event):
        if self._starts_a_move(event):
            # where the drag began, and what the sound was drawn at when it
            # did: a refresh part way through a drag hands this strip a sound
            # drawn at an offset the drag has already moved, and measuring
            # from that would count every pixel twice
            self._sound_dragged_from = (event.pos().x(), self._sound.offset_ms)

            return

        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._sound_dragged_from is None:
            super().mouseMoveEvent(event)

            return

        from_x, from_offset = self._sound_dragged_from

        moved_ms = round((event.pos().x() - from_x) * self._ms_per_pixel())

        # a sound dragged a second later along the clock is a recording whose
        # sync point is a second earlier
        self.sound_dragged.emit(from_offset - moved_ms)

    def mouseReleaseEvent(self, event):
        if self._sound_dragged_from is not None:
            self._sound_dragged_from = None
            self.sound_dropped.emit()

            return

        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape and self._sound_dragged_from is not None:
            self._sound_dragged_from = None
            self.sound_drag_given_up.emit()

            return

        super().keyPressEvent(event)

    def _starts_a_move(self, event) -> bool:
        """Whether a drag of this strip moves its video or the window.

        The video being worked on, and only where there is a sound to move:
        with nothing read there is nothing to align against, and a strip that
        cannot be dragged at all is a strip that cannot be looked along.
        """

        return (
            event.button() == Qt.LeftButton
            and self._is_picked
            and self._sound is not None
        )

    def _paint_contents(self, painter):
        alpha = PICKED_ALPHA if self._is_picked else SOUND_ALPHA

        self._paint_sound(
            painter, self._sound, _named_colour(self, self._colour, alpha)
        )


class SyncOverviewStrip(_ClockStrip):
    """Every video's sound, one over another, on the clock they share.

    The one place the recordings are seen against each other: each row draws
    only its own sound, so whether the peaks of one sit where the peaks of
    another do is read here. Each is drawn in its own colour and through
    itself, so a colour coming up darker is a place where more than one of
    them has sound, and the one being worked on is drawn last and solid.
    """

    def __init__(self, parent=None):
        super().__init__(parent)

        self._drawn: tuple[DrawnSound, ...] = ()
        self._now_ms = None

        self.setFixedHeight(OVERVIEW_STRIP_HEIGHT)

        tooltip = translate(
            "Dialog - Align Videos", "The sound of every video over every other's,"
            " each in its own colour, against the clock they share. Wheel to zoom,"
            " drag to move along."
        )  # fmt: skip

        self.setToolTip(tooltip)

    def show_sounds(self, drawn, now_ms=None, zero_ms=None):
        """Every video's sound, where the videos are now, and where zero is.

        The order given is the order drawn, so that whoever owns this can put
        the recording being worked on last, over the rest.
        """

        shown = (tuple(drawn), now_ms, zero_ms)

        if shown == (self._drawn, self._now_ms, self._zero_ms):
            return

        self._drawn, self._now_ms, self._zero_ms = shown

        self.update()

    def _top_px(self) -> int:
        return AXIS_HEIGHT

    def _paint_above(self, painter):
        self._paint_ruler(painter)

    def _paint_contents(self, painter):
        # the one being worked on first of all, and solid: drawn in its turn
        # it would be painted over by whichever recording came after it
        for drawn in self._drawn:
            if drawn.is_picked:
                continue

            self._paint_sound(
                painter, drawn.sound, _named_colour(self, drawn.colour, OVERLAY_ALPHA)
            )

        for drawn in self._drawn:
            if not drawn.is_picked:
                continue

            self._paint_sound(
                painter, drawn.sound, _named_colour(self, drawn.colour, PICKED_ALPHA)
            )


def tick_step_ms(window_ms: int, width_px: int = ASSUMED_STRIP_WIDTH_PX) -> int:
    """The step a ruler over this much time should be labelled in.

    The finest step that still leaves room for its own labels, so that
    zooming in adds ticks rather than crowding the ones already there.
    """

    least_ms = window_ms * MIN_TICK_GAP_PX / max(1, width_px)

    for step_ms in reversed(TICK_STEPS_MS):
        if step_ms >= least_ms:
            return step_ms

    return TICK_STEPS_MS[0]


def ticks_between(from_ms: float, to_ms: float, step_ms: int) -> tuple[int, ...]:
    """Every multiple of the step inside a window, the ends included."""

    if step_ms <= 0 or to_ms <= from_ms:
        return ()

    first = math.ceil(from_ms / step_ms) * step_ms

    return tuple(range(int(first), int(to_ms) + 1, step_ms))


def tick_label(offset_ms: float, step_ms: int) -> str:
    """A moment as seconds either side of zero: `-2`, `-0.5`, `+15`, `0`.

    Plain numbers with a sign, in the unit the caption above the strips
    names: five identical units on one ruler is five times the translating.
    """

    seconds = offset_ms / 1000

    if not seconds:
        return "0"

    if step_ms >= 1000:
        return f"{seconds:+.0f}"

    return f"{seconds:+.1f}"


def _clamp(value: float, low: int, high: int) -> int:
    return int(max(low, min(high, value)))


def _named_colour(widget, name: str | None, alpha: int = SOUND_ALPHA) -> QColor:
    """A colour as it is named, see-through by this much.

    A colour named by hand can be anything at all -- it is read out of a
    settings file -- so one that is not a colour at all is drawn in the
    strip's own highlight rather than as nothing.
    """

    colour = QColor(name) if name else QColor()

    if not colour.isValid():
        colour = _highlight_colour(widget, alpha)

        return colour

    colour.setAlpha(alpha)

    return colour


def _text_colour(widget, alpha: int) -> QColor:
    colour = QColor(widget.palette().color(widget.foregroundRole()))
    colour.setAlpha(alpha)

    return colour


def _highlight_colour(widget, alpha: int) -> QColor:
    colour = QColor(widget.palette().color(widget.palette().Highlight))
    colour.setAlpha(alpha)

    return colour
