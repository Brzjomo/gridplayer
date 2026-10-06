"""One video's sound, drawn against the reference's on a shared axis.

Automatic alignment answers with a number and a confidence, and there are
recordings where neither is to be trusted: the sound is faint, every moment
of it sounds like another, or the answer is a second out with a score that
looks healthy. What no number takes away is that a person can look at two
runs of sound and see whether they are the same run of sound, and by how
much they miss each other. That is what this draws -- the sound and not the
picture, since three cameras pointed at one event have far more sound in
common than picture.

Three things make it readable, and all three are deliberate:

* **The clock is the one every recording shares** (`Sound`, `common_moment_ms`).
  The reference's own sound is the ruler: the middle of what was read from it
  is zero, and it stays where it is while other videos are moved against it,
  so "the two shapes line up at zero" is the whole of tuning an offset.
* **The ruler is drawn**, labelled in seconds, in a band along the top of
  every strip. The strips of one dialog share one window (the dialog owns it,
  see `view_changed`), so the bands line up and read as one scale down the
  whole dialog -- and how much of the sound that window covers is said in
  words by the caption above them rather than left to be guessed at.
* **A strip says only what its row is**: the reference's sound underneath,
  this video's over it, and a thin line where this video is now, so that a
  window which has been scrolled away from the sound being looked at is
  visible as such.
"""

import math

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QColor, QFontMetrics, QPainter, QPen
from PyQt5.QtWidgets import QWidget

from gridplayer.utils.qt import translate
from gridplayer.utils.sync_audio import Sound

# How tall a strip is: a band for the ruler, then enough sound under it for
# the shape of a peak to be read off, and short enough that a grid of them
# still fits on one screen.
STRIP_HEIGHT = 58

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
OTHER_ALPHA = 170
REFERENCE_ALPHA = 120
ZERO_ALPHA = 110


class SyncEnvelopeStrip(QWidget):
    """Two readings of one event, drawn over each other on a shared clock.

    It is given `Sound`s rather than samples: the readings are the ones the
    last run of measurement took, kept by whoever owns this, and they are
    placed by the offsets as those stand at the moment of drawing -- which
    is what makes a nudge visible as it happens.

    **The window is not this widget's to keep.** Zooming or dragging says so
    on `view_changed`, and whoever owns the strips hands the same window to
    every one of them: two strips showing different stretches of one clock
    are two strips that cannot be read against each other.
    """

    view_changed = pyqtSignal(int, int)
    view_reset = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)

        self._reference = None
        self._other = None
        self._now_ms = None

        self._window_ms = DEFAULT_WINDOW_MS
        self._center_ms = 0

        self._dragged_from = None

        self.setFixedHeight(STRIP_HEIGHT)

        tooltip = translate(
            "Dialog - Align Videos", "The reference's sound with this video's over it,"
            " against the clock the videos share. Wheel to zoom, drag to move along,"
            " double-click to fit."
        )  # fmt: skip

        self.setToolTip(tooltip)

    # --- what is drawn ---

    def show_pair(self, reference: Sound | None, other: Sound | None, now_ms=None):
        """The reference's sound, this row's, and where this row is now.

        Called every time the dialog refreshes its rows, several times a
        second, so a pair that is the same pair as the last one is left
        alone: the sounds are held by the caller precisely so that this can
        be told by looking at them.
        """

        if (
            reference is self._reference
            and other is self._other
            and now_ms == self._now_ms
        ):
            return

        was_read = None if self._reference is None else self._reference.envelope
        now_read = None if reference is None else reference.envelope

        self._reference, self._other, self._now_ms = reference, other, now_ms

        # a new reading is a new stretch of the recordings, and a window left
        # where the last one was may well have nothing in it: whoever owns
        # the view is the only one that can fit all of them at once
        if now_read is not was_read:
            self.view_reset.emit()

        self.update()

    @property
    def zero_ms(self):
        """The moment the ruler is measured from: the middle of what was read
        from the reference, which is the one thing on the strip that stays
        put while other videos are moved against it."""

        for sound in (self._reference, self._other):
            if sound is not None:
                return sound.read_around_ms

        return None

    def set_view(self, center_ms: int, window_ms: int):
        self._center_ms = int(center_ms)
        self._window_ms = int(window_ms)

        self.update()

    # --- where the window is ---

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

    def mouseDoubleClickEvent(self, event):
        self.view_reset.emit()

        super().mouseDoubleClickEvent(event)

    def resizeEvent(self, event):
        self.update()

        super().resizeEvent(event)

    # --- the drawing itself ---

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), self.palette().color(self.backgroundRole()))
        painter.setPen(QPen(_text_colour(self, FRAME_ALPHA)))
        painter.drawRect(self.rect().adjusted(0, 0, -1, -1))

        if self.zero_ms is None:
            self._paint_nothing_read(painter)
            return

        self._paint_ruler(painter)
        self._paint_zero(painter)

        # the reference under the other, so that neither hides the other
        # where they disagree and both are read where they agree
        self._paint_sound(painter, self._reference, _text_colour(self, REFERENCE_ALPHA))

        if self._other is not self._reference:
            self._paint_sound(
                painter, self._other, _highlight_colour(self, OTHER_ALPHA)
            )

        self._paint_now(painter)

    def _paint_ruler(self, painter):
        """A band of ticks along the top, labelled in seconds from zero.

        The same band, on the same window, in every strip of a dialog, so
        the labels read as one scale down the whole of it.
        """

        zero = self.zero_ms
        from_ms, to_ms = self._shown_ms()
        step_ms = tick_step_ms(self._window_ms, self.width())

        painter.setPen(QPen(_text_colour(self, AXIS_ALPHA)))

        metrics = QFontMetrics(self.font())

        for at_ms in ticks_between(from_ms, to_ms, step_ms):
            x = round(self._x_of(at_ms))

            painter.drawLine(x, AXIS_HEIGHT - 5, x, AXIS_HEIGHT - 2)

            label = tick_label(at_ms - zero, step_ms)
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
        """The line the ear is being asked about: the two shapes should agree
        across it."""

        zero = self.zero_ms
        from_ms, to_ms = self._shown_ms()

        if not from_ms <= zero <= to_ms:
            return

        x = round(self._x_of(zero))

        painter.setPen(QPen(_text_colour(self, ZERO_ALPHA)))
        painter.drawLine(x, AXIS_HEIGHT, x, self.height() - BASELINE_PX)

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
        tallest = baseline - AXIS_HEIGHT

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
            round(x), AXIS_HEIGHT + 1, round(x), self.height() - BASELINE_PX - 1
        )

    def _paint_nothing_read(self, painter):
        text = translate("Dialog - Align Videos", "No sound read yet")

        painter.setPen(QPen(_text_colour(self, HINT_ALPHA)))
        painter.drawText(
            self.rect(),
            Qt.AlignCenter,
            QFontMetrics(self.font()).elidedText(text, Qt.ElideRight, self.width()),
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


def _text_colour(widget, alpha: int) -> QColor:
    colour = QColor(widget.palette().color(widget.foregroundRole()))
    colour.setAlpha(alpha)

    return colour


def _highlight_colour(widget, alpha: int) -> QColor:
    colour = QColor(widget.palette().color(widget.palette().Highlight))
    colour.setAlpha(alpha)

    return colour
