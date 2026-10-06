"""Reading every video's sound, off the interface thread.

Reading a stretch means decoding a recording up to a moment, which takes
long enough that doing it where the viewer is looking would be taken for
the dialog having frozen: seconds for a snippet, and tens of seconds where
the moment lies hours into a five-hour recording. Every recording is read
on a thread of its own, so a grid of three pays for one of them rather
than for all three -- what they cost is the decoding, and a machine has
more than one core to do it on.

What comes back is a signal with every reading in it, and a signal as each
one arrives, so that a dialog can say which file it is waiting on. A run
is abandoned the moment it is no longer wanted, between one reading and
the next: a reading already begun is left to finish rather than pulled out
from under VLC.
"""

import logging
from dataclasses import dataclass
from threading import Event

from PyQt5.QtCore import QObject, QThread, pyqtSignal

from gridplayer.utils.sync_audio import (
    BLOCKS_PER_SEC,
    FINE_SNIPPET_MS,
    Reading,
)

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReadingRequest:
    """One recording to read, how much of it, and how finely.

    The moment is a position of that recording's own, and the stretch is
    taken around it. Two of these are made of every recording and only the
    first of them is decoded: a wide one at a coarse size for the pass that
    finds minutes of misalignment, and the snippet inside it at the fine
    size for the pass that places it to the millisecond. See
    sync_audio.WIDE_READ_MS and SnippetCache.
    """

    block_id: str
    uri: str
    center_ms: int
    snippet_ms: int = FINE_SNIPPET_MS
    blocks_per_sec: int = BLOCKS_PER_SEC


class _Reader(QObject):
    """One recording's reading, on a thread that is not the one anybody is
    looking at."""

    done = pyqtSignal(object)

    def __init__(self, request: ReadingRequest, is_cancelled: Event):
        super().__init__()

        self._request = request
        self._is_cancelled = is_cancelled

    def run(self):
        # imported here: this module is reached from the interface, and
        # libVLC is loaded on import
        from gridplayer.vlc_player.audio_probe import AudioProbe

        request = self._request
        reading = None
        probe = AudioProbe()

        try:
            if not self._is_cancelled.is_set():
                envelope, origin_ms = probe.envelope(
                    request.uri,
                    request.center_ms,
                    request.snippet_ms,
                    request.blocks_per_sec,
                )

                if envelope:
                    reading = Reading(envelope, origin_ms)
        except Exception:
            _log.exception("Reading the sound of a snippet failed")
        finally:
            probe.cleanup()

        # said whether or not anything was read: a file with no sound is
        # one of the things whoever asked is waiting to hear about
        self.done.emit((request.block_id, reading))


class AlignMeasure(QObject):
    """A run of readings, on threads that go away when the run does.

    One thread to a recording, all of them reading at once: what a run
    costs is the slowest of its recordings rather than the sum of them.
    """

    measured = pyqtSignal(object)

    # how many are back, how many were asked for, and which one just
    # arrived, so that a dialog can name the file it is waiting on
    progress = pyqtSignal(int, int, str)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

        self._threads = []
        self._readers = []
        self._is_cancelled = Event()

        self._readings = {}
        self._answered = 0
        self._total = 0

    @property
    def is_running(self) -> bool:
        return bool(self._threads)

    def start(self, requests) -> bool:
        """Read these, where nothing is being read already."""

        if self.is_running or not requests:
            return False

        self._is_cancelled.clear()

        self._readings = {}
        self._answered = 0
        self._total = len(requests)

        for request in requests:
            self._start_reading(request)

        return True

    def _start_reading(self, request: ReadingRequest):
        thread = QThread()
        reader = _Reader(request, self._is_cancelled)
        reader.moveToThread(thread)

        thread.started.connect(reader.run)
        reader.done.connect(self._on_done)

        self._threads.append(thread)
        self._readers.append(reader)

        thread.start()

    def _on_done(self, answered):
        if not self._threads:
            # the run was let go of while this was in the air
            return

        block_id, reading = answered

        self._answered += 1

        if reading is not None:
            self._readings[block_id] = reading

        self.progress.emit(self._answered, self._total, block_id)

        if self._answered < self._total:
            return

        readings, self._readings = self._readings, {}

        self._stop()
        self.measured.emit(readings)

    def cleanup(self):
        """Stop reading and wait for the readings in hand to finish.

        Waits rather than leaving them: a thread holds a player, and a
        player whose owner has been taken away is a crash waiting for the
        sound to stop.
        """

        self._is_cancelled.set()

        self._stop()

    def _stop(self):
        threads, self._threads = self._threads, []
        self._readers = []
        self._answered = 0
        self._total = 0

        for thread in threads:
            thread.quit()

        for thread in threads:
            thread.wait()
