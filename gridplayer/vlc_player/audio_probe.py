"""Reading a slice of a recording's sound, without playing it.

The sound is wanted as numbers, so the snippet is transcoded into a wav
file by a player of its own and read back. There are two other ways to get
at the samples and both are worse:

* taking them from the player that is playing would take the sound away
  from the viewer, since a player given audio callbacks has no output left
  to give the sound to;
* decoding into a buffer in real time takes as long as the sound lasts,
  where writing it to a file takes as long as the machine needs to decode
  it, which is a good deal less.

This player has no window, no interface and nothing to send its sound to.
It is asked for a few seconds around a moment and left to get on with it.

What it writes is kept by `SnippetCache`, so a second reading of the same
recording -- which is what moving the videos about and lining them up again
comes to -- reads the sound off the file rather than decoding it again.
"""

import logging
import time
from pathlib import Path

from gridplayer.utils.log_config import DISABLED
from gridplayer.utils.sync_audio import (
    SAMPLE_RATE,
    SnippetCache,
    file_key,
)
from gridplayer.vlc_player.libvlc import vlc

_log = logging.getLogger(__name__)

# How much sound is looked at either side of the moment. Long, because
# what can be lined up is bounded by it: two recordings can only be matched
# as far apart as they still overlap by the least worth matching on, so a
# short snippet means a range that could have been matched by hand. See
# sync_audio.MAX_LAG_SEC.
SNIPPET_MS = 30000

# Decoding is faster than playing, but a stuck input must not hold the
# interface for ever.
PROBE_TIMEOUT_SEC = 30.0

POLL_SEC = 0.02

# The player's own state, once it has nothing left to do.
FINISHED_STATES = (vlc.State.Ended, vlc.State.Stopped)

# One cache of read stretches for the whole application: a probe is made
# for every run of readings, and what makes the keeping worth anything is
# that the next run finds what the last one read.
_envelope_cache = SnippetCache()


def envelope_cache() -> SnippetCache:
    return _envelope_cache


def _sout(dst: Path) -> str:
    """The stream output that writes the snippet out as a wav.

    Forward slashes in the path: the destination sits inside a brace, and a
    backslash before a brace or a colon reads as an escape or as another
    option rather than as part of the name.
    """

    return (
        f"#transcode{{acodec=s16le,channels=1,samplerate={SAMPLE_RATE}}}"
        f":std{{access=file,mux=wav,dst={dst.as_posix()}}}"
    )


class AudioProbe:
    """A player kept for reading snippets, and never heard."""

    def __init__(self, cache: SnippetCache | None = None):
        self._instance = None

        # the same stretches across probes, since a probe is made for every
        # run of readings and the point of keeping them is to have them the
        # next time somebody lines the videos up
        self._cache = cache if cache is not None else envelope_cache()

    @property
    def instance(self):
        if self._instance is None:
            self._instance = vlc.Instance(self._options())

        return self._instance

    @staticmethod
    def _options():
        return [
            "--intf=dummy",
            "--no-video",
            # nothing is to be heard: the sound is wanted as a file, and
            # this player has no business making any
            "--aout=dummy",
            "--no-osd",
            "--no-lua",
            "--no-interact",
            "--no-stats",
            "--no-snapshot-preview",
            "--no-sub-autodetect-file",
            "--quiet",
        ]

    def envelope(
        self, uri, center_ms: int, snippet_ms: int = SNIPPET_MS
    ) -> tuple[tuple[float, ...], int]:
        """The loudness of the sound around a moment, and where it began.

        Read from the start of the recording rather than sought to. Seeking
        a video file lands on the nearest point the container is able to
        begin at, and the sound then starts as much as a second away from
        the moment asked for -- by a different amount for each file, and
        with nothing in the reading to say by how much. That is a second of
        error in the answer and no way to see it; measured, on a file with
        a keyframe every two seconds, as anywhere between nothing and 1.1
        seconds wrong.

        What reading from the start costs is decoding everything before the
        moment, which runs at a few hundred times the length of what it is
        decoding, and is the price of the first block beginning somewhere
        that is known: at the start of the recording.

        What has been decoded once is kept, so the cost is paid the first
        time a recording is read and not on every reading of it: see
        SnippetCache.
        """

        origin_ms = max(0, int(center_ms) - snippet_ms // 2)

        envelope = self._cache.envelope(
            file_key(uri),
            origin_ms,
            snippet_ms,
            lambda until_ms, dst: self._write_snippet(uri, until_ms, dst),
        )

        return envelope, origin_ms

    def _write_snippet(self, uri, until_ms: int, dst: Path) -> bool:
        instance = self.instance

        player = instance.media_player_new()

        if player is None:
            _log.warning("Could not make a player to read a snippet with")
            return False

        try:
            media = instance.media_new(str(uri))

            # nothing is skipped: what is written begins at the start of the
            # recording, which is the whole point of writing it this way
            media.add_option(f":stop-time={until_ms / 1000:.3f}")
            media.add_option(f":sout={_sout(dst)}")
            # a transcode with the sound going nowhere still has an audio
            # output in the graph; muting it costs nothing and means a
            # machine where the dummy output is missing stays silent
            media.add_option(":no-audio")

            player.set_media(media)
            player.audio_set_mute(True)
            player.play()

            return self._wait_for(player, until_ms) and dst.is_file()

        finally:
            player.stop()
            player.release()

    @staticmethod
    def _wait_for(player, until_ms: int = 0) -> bool:
        # decoding what comes before the moment is what takes the time, and
        # it is not the same for the first minute of a recording as for the
        # last; a few hundred times the length is the slowest seen
        deadline = time.monotonic() + PROBE_TIMEOUT_SEC + until_ms / 1000 / 20

        while time.monotonic() < deadline:
            state = player.get_state()

            if state == vlc.State.Error:
                _log.warning("Reading a snippet failed")
                return False

            if state in FINISHED_STATES:
                return True

            time.sleep(POLL_SEC)

        _log.warning("Reading a snippet timed out")
        return False

    def cleanup(self):
        if self._instance is None:
            return

        self._instance.release()
        self._instance = None


def probe_log_level() -> int:
    """What the probe's own VLC has to say, which is nothing by default."""

    return DISABLED
