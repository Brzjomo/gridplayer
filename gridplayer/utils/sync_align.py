"""Reading every video's sound, off the interface thread.

Reading a snippet means decoding several seconds of a recording, which
takes long enough that doing it where the viewer is looking would be taken
for the dialog having frozen. It goes on a thread of its own and comes back
as a signal, one recording at a time, with the whole run abandoned the
moment it is no longer wanted.
"""

import logging
from threading import Event

from PyQt5.QtCore import QObject, QThread, pyqtSignal

_log = logging.getLogger(__name__)


class _Measurer(QObject):
    """The readings themselves, running on a thread that is not the one
    anybody is looking at."""

    measured = pyqtSignal(object)

    def __init__(self, requests, is_cancelled: Event):
        super().__init__()

        self._requests = requests
        self._is_cancelled = is_cancelled

    def run(self):
        # imported here: this module is reached from the interface, and
        # libVLC is loaded on import
        from gridplayer.utils.sync_audio import Reading
        from gridplayer.vlc_player.audio_probe import AudioProbe

        readings = {}
        probe = AudioProbe()

        try:
            for block_id, uri, center_ms in self._requests:
                # between one and the next: a reading already begun is left
                # to finish rather than pulled out from under VLC
                if self._is_cancelled.is_set():
                    break

                envelope, origin_ms = probe.envelope(uri, center_ms)

                if envelope:
                    readings[block_id] = Reading(envelope, origin_ms)
        except Exception:
            _log.exception("Reading the sound of a snippet failed")
        finally:
            probe.cleanup()

        self.measured.emit(readings)


class AlignMeasure(QObject):
    """A run of readings, on a thread that goes away when the run does."""

    measured = pyqtSignal(object)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

        self._thread = None
        self._measurer = None
        self._is_cancelled = Event()

    @property
    def is_running(self) -> bool:
        return self._thread is not None

    def start(self, requests) -> bool:
        """Read these, where nothing is being read already."""

        if self.is_running or not requests:
            return False

        self._is_cancelled.clear()

        self._thread = QThread()
        self._measurer = _Measurer(requests, self._is_cancelled)
        self._measurer.moveToThread(self._thread)

        self._thread.started.connect(self._measurer.run)
        self._measurer.measured.connect(self._on_measured)

        self._thread.start()

        return True

    def cleanup(self):
        """Stop reading and wait for the reading in hand to finish.

        Waits rather than leaving it: the thread holds a player, and a
        player whose owner has been taken away is a crash waiting for the
        sound to stop.
        """

        self._is_cancelled.set()

        self._stop()

    def _on_measured(self, envelopes):
        self._stop()
        self.measured.emit(envelopes)

    def _stop(self):
        if self._thread is None:
            return

        thread, self._thread = self._thread, None
        self._measurer = None

        thread.quit()
        thread.wait()
