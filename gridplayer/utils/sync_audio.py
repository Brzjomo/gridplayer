"""Reading the sound of two recordings to find where they line up.

Lining recordings up by eye means finding the same moment twice, and the
moment a picture changes is not always the moment the sound does. Both
recordings carry the same sound though, so the sound can be lined up
instead, and once two envelopes agree the videos are on the same moment.

Nothing here touches VLC or Qt: it is given samples and gives back numbers,
so what it does can be checked without a player. Reading the samples off a
recording is `vlc_player.audio_probe`'s part, and the stretch it read is
kept here once it has been read, since reading it is the slow part.
"""

import array
import atexit
import logging
import math
import shutil
import tempfile
import time
import wave
from collections import Counter, OrderedDict, defaultdict, deque
from dataclasses import dataclass
from enum import auto
from itertools import combinations
from operator import mul
from pathlib import Path
from threading import Lock

from gridplayer.params.static import AutoName

_log = logging.getLogger(__name__)

# How finely the sound is measured, and the number that decides how close
# the answer can be. Two milliseconds: a twentieth of a second is heard as
# two of the same sound one after the other rather than as one sound, which
# is the thing lining them up is meant to stop.
BLOCKS_PER_SEC = 500

SAMPLE_RATE = 8000

# The overlap two envelopes must share before a lag between them means
# anything. A second of shared sound is enough to place a clap among other
# sounds; much less and a single loud block decides the answer.
MIN_OVERLAP_SEC = 1.0

# How far apart two recordings may be and still be lined up by the close
# pass. Long enough to be worth doing at all -- a range of a second or two is
# a range that could have been matched by hand -- and long enough for the one
# case the coarse pass can be right about and the viewer still be stuck in:
# a recording with a section added to it, where the two are together on one
# side of the addition and a section apart on the other.
#
# It is half of the snippet, which is as far as one reading can be moved
# against the other and still have half of them left to compare: with
# FINE_SNIPPET_MS at a minute, thirty seconds either way leaves thirty
# seconds of the two still shared at the far end.
MAX_LAG_SEC = 30.0

# How far apart two recordings may be and still be lined up at all, which is
# a different question from the one above. The fine range is what a snippet
# read either side of the moment can still share; this one is what a stretch
# read across minutes of the recordings is looked over to find.
#
# Ten minutes either way is what a grid of recordings of one event is out by
# before anything has been done about it -- a few minutes, measured on the
# recordings this is for -- so it is the range that makes lining them up
# automatic rather than something to be done by hand first.
COARSE_MAX_LAG_SEC = 600.0

# How much sound a reading looks at, and how much more than that is taken
# where the recordings may be minutes apart.
#
# A wide reading is the whole of the coarse range either side of the moment
# with the fine snippet on top of it, because the fine pass reads *after*
# the coarse one has been acted on: the moment it reads around can be a
# whole range further into the recording than the moment the wide reading
# was taken at, and its snippet has to be inside what has already been
# decoded. One decode answering both passes is what that buys. The margin
# is for the rounding and for a seek that lands a moment short.
#
# A minute of snippet rather than thirty seconds, at a cost of thirty seconds
# more envelope to measure and a range twice as wide to search through it:
# what that buys is that a recording with a section added in it can be lined
# up at either side of the addition, and not only at the side the coarse pass
# happened to settle on. See MAX_LAG_SEC.
FINE_SNIPPET_MS = 60000

WIDE_READ_MARGIN_MS = 5000

WIDE_READ_MS = (
    round(2 * COARSE_MAX_LAG_SEC * 1000) + FINE_SNIPPET_MS + WIDE_READ_MARGIN_MS
)

# Looking that far at that fineness is a great many comparisons, and almost
# all of them are nowhere near. Every COARSE_DECIMATION blocks are averaged
# into one, the whole range is searched at that size, and only around what
# it finds is the sound looked at closely.
#
# Kept small deliberately. Averaging flattens: pool a hundred blocks into
# one and a stretch of short sounds -- claps, footsteps, a door -- comes
# out as nearly one value, with no shape left to match and nothing to stop
# some unrelated moment scoring as well as the right one. Pooling twenty
# keeps the shape and still leaves the range cheap enough to search.
COARSE_DECIMATION = 20

# How far either side of the rough answer the close look reaches. One
# coarse block is COARSE_DECIMATION of the fine ones, so this covers the
# worst the rough answer can be, with room to spare. In seconds, since how
# long a block is is the scale's to say.
FINE_REACH_SEC = COARSE_DECIMATION * 3 / BLOCKS_PER_SEC

# How much of the sound the close look compares. Four seconds of it is more
# than enough to place a sound, and keeping it to a stretch is what keeps
# the close look quick enough to run while somebody waits.
FINE_SPAN_SEC = 4.0


@dataclass(frozen=True)
class Lag:
    """How far one envelope sits ahead of another, and how well they agree.

    `blocks` is how much later in `other` the sound that `reference` has at
    its own start turns up. The offset is the moment a recording is on at a
    common time, so a sound arriving that much later means the offset wants
    moving on by the same amount: see offset_shift_ms.

    `score` is how well the two agree there, and `prominence` how much
    better they agree there than anywhere else -- see how_sure.
    """

    blocks: int
    score: float
    prominence: float = 1.0

    # what a block of this lag is worth on the clock, which is the scale's
    # to say: a lag found over minutes of a recording is in second-long
    # blocks and one found over a snippet is in two-millisecond ones
    blocks_per_sec: int = BLOCKS_PER_SEC

    @property
    def offset_shift_ms(self) -> int:
        return round(self.blocks * 1000 / self.blocks_per_sec)


# How far away another peak has to be to count as another peak and not the
# shoulder of this one. A second either way, which is well past the width
# of any one sound and well short of the range being searched.
PEAK_SEPARATION_SEC = 1.0

# How far the best peak has to stand above the best one far from it before
# the answer is worth acting on, as a share of the best peak's own score.
#
# The score on its own cannot say whether an answer is worth acting on,
# because what a high score is worth depends on the material: measured on
# two recordings of one event, the right lag scored 0.667, and a score of
# 0.3 on a quieter pair of recordings can mean just as much. What does not
# depend on the material is whether one place in the range stands out from
# the rest of it -- a lag that is as good as another lag is no answer at
# all, however well the two agree there. A quarter above the next-best
# distant peak, and the answer is being told apart from its rivals.
MIN_PROMINENCE = 0.25


# How well two envelopes have to agree before the answer is worth acting
# on. Judged on the shape of the sound rather than on its loudness, so this
# is not the number it used to be: two cameras recording the same event
# reach about a third on loudness alone -- because each has its own
# automatic gain, and the level is the part they agree on least -- and get
# thrown away for it, while the shape of the sound reaches about two
# thirds. Unrelated sound reaches nearly nothing on either.
MIN_SCORE = 0.3

# How much of the sound either side of a block is averaged away and
# subtracted before two envelopes are compared.
#
# Two microphones in one room hear the same sounds, but not at the same
# level: one is nearer, one is behind something, one simply recorded
# louder. Worse, the level across whole seconds is what they agree on
# least and what a plain loudness envelope is mostly made of -- so
# comparing loudness compares the part they share least, and two
# recordings of the same moment come out looking nearly unrelated. What
# they do agree on is when the sound changed. Taking out the swell across
# half a second leaves that, and it is what gets compared.
SHAPE_SPAN_SEC = 0.5

# How far two pairs of readings may disagree about where a recording sits
# before the disagreement is reported instead of acted on.
#
# A recording's sound carries a constant of its own from the encoder it
# came through -- measured at 128 ms between PCM and AAC -- so a pair of
# differently encoded recordings is not expected to agree to the
# millisecond, and a tolerance below that would report the encoder as a
# disagreement. A lag settled on the wrong peak, on the other hand, is out
# by whole seconds. This sits well above the one and well below the other.
CONFLICT_MS = 300


@dataclass(frozen=True)
class Scale:
    """How finely a run of readings is looked at, and how far.

    Everything the search does that depends on how long a block is, in one
    place, because there is more than one size worth searching at and none
    of them is the other's: the fine scale looks at a snippet read around
    the moment the videos are on, and the coarse one looks at minutes of the
    recordings, which is what finds a lag of minutes at all. `best_lag`,
    `measure_pairs` and `align` take one of these and do not care which.

    How long a block is is the scale's to say, and it is the number that
    turns a lag into milliseconds: ten blocks of a coarse reading is ten
    seconds and ten blocks of a fine one is twenty milliseconds.
    """

    blocks_per_sec: int

    # how far a lag may be, and how much of the two readings must still be
    # shared at that lag for it to mean anything
    max_lag_sec: float
    min_overlap_sec: float

    # how much of the swell is taken out of an envelope before two are
    # compared, and how far away another peak has to be to count as a rival
    # rather than as the shoulder of the answer
    shape_span_sec: float
    peak_separation_sec: float

    # the close look: how much of the sound it compares, and how far either
    # side of the rough answer it reaches
    fine_span_sec: float
    fine_reach_sec: float

    # how much of the range the rough look pools away before searching it
    coarse_decimation: int

    # how well two envelopes must agree, and how far the answer must stand
    # above its best distant rival, before it is acted on
    min_score: float
    min_prominence: float

    # how far two pairs may disagree about one recording
    conflict_ms: int

    @property
    def ms_per_block(self) -> float:
        return 1000 / self.blocks_per_sec

    @property
    def max_lag_blocks(self) -> int:
        return round(self.max_lag_sec * self.blocks_per_sec)

    @property
    def min_overlap_blocks(self) -> int:
        return round(self.min_overlap_sec * self.blocks_per_sec)

    @property
    def shape_span_blocks(self) -> int:
        return round(self.shape_span_sec * self.blocks_per_sec)

    @property
    def peak_separation_blocks(self) -> int:
        return round(self.peak_separation_sec * self.blocks_per_sec)

    @property
    def fine_span_blocks(self) -> int:
        return round(self.fine_span_sec * self.blocks_per_sec)

    @property
    def fine_reach_blocks(self) -> int:
        return round(self.fine_reach_sec * self.blocks_per_sec)


# The scale the search over a snippet is made at: two milliseconds a block,
# which is what places a lag to the millisecond. Everything above is here,
# and the numbers behind it are the ones argued for where they are written.
#
# The snippet is a minute and the range is half of it: see FINE_SNIPPET_MS
# and MAX_LAG_SEC for what that is for and what it costs.
FINE_SCALE = Scale(
    blocks_per_sec=BLOCKS_PER_SEC,
    max_lag_sec=MAX_LAG_SEC,
    min_overlap_sec=MIN_OVERLAP_SEC,
    shape_span_sec=SHAPE_SPAN_SEC,
    peak_separation_sec=PEAK_SEPARATION_SEC,
    fine_span_sec=FINE_SPAN_SEC,
    fine_reach_sec=FINE_REACH_SEC,
    coarse_decimation=COARSE_DECIMATION,
    min_score=MIN_SCORE,
    min_prominence=MIN_PROMINENCE,
    conflict_ms=CONFLICT_MS,
)

# The scale the search over minutes of a recording is made at: a block a
# second, over ten minutes either way.
#
# A second a block is as coarse as this can be and still place anything. A
# quarter of an hour of a recording is a thousand blocks of it, which is
# enough shape to be going on, and comparing the whole range at that size
# costs a fraction of a second in pure Python. Measured on synthetic
# recordings of one event an hour long, shifted by a known amount: lags of
# 5, 61, 300 and 599 seconds all came back exactly, at scores of 0.8 to 0.9,
# where two recordings of different events reached 0.05 -- and where the
# search settled on the wrong place instead, it did so at a score of 0.02 to
# 0.11, so the answer is thrown away rather than acted on. A wrong answer
# that looks like a right one is the thing this has to not do.
#
# The overlap wanted is a minute of shared sound, because an hour of it is
# what is being compared and a second of it either side of a real match is
# nothing to go on. The distance between rivals is the same idea at this
# size: half a minute apart is two peaks at a block a second.
#
# `coarse_decimation` is five here, and not the fine scale's twenty. Pooling
# twenty blocks of this size averages twenty seconds of a recording into
# one, which flattens a stretch of short sounds -- claps, footsteps, a door
# -- into nearly one value, and the rough pass then guides the close one
# from a peak nowhere near the answer. Measured over 64 synthetic pairs of
# one event an hour long, shifted by 0, ±5, ±61, ±300, ±450 and ±599
# seconds: pooled twenty and reaching a minute either way, the rough pass
# put the close pass on the right place 60 times and on a place it had to
# throw away 4; pooled five, it was right 64 times and threw nothing away.
# Nothing was acted on wrongly either way -- a wrong peak scores near
# nothing over the whole of a long overlap, which is what makes this safe --
# but a reading that finds nothing where there was something is worth 5 s a
# block to be rid of, and searching the range at that size is still about a
# tenth of a second a pair.
COARSE_SCALE = Scale(
    blocks_per_sec=1,
    max_lag_sec=COARSE_MAX_LAG_SEC,
    min_overlap_sec=60.0,
    shape_span_sec=10.0,
    peak_separation_sec=30.0,
    fine_span_sec=1200.0,
    fine_reach_sec=60.0,
    coarse_decimation=5,
    min_score=MIN_SCORE,
    min_prominence=MIN_PROMINENCE,
    # five seconds, where the fine scale allows a fifth of one: at a block a
    # second the encoder's own constant is a block or two, and a pair that
    # disagrees by more than a few seconds has settled on another peak
    conflict_ms=5_000,
)


def _shaped(envelope, span: int) -> tuple[float, ...]:
    """The envelope with its slow swell taken out."""

    if span < 2 or len(envelope) < span:
        return tuple(envelope)

    running = [0.0]

    for value in envelope:
        running.append(running[-1] + value)

    half = span // 2
    count = len(envelope)

    return tuple(
        value
        - (running[min(count, index + half + 1)] - running[max(0, index - half)])
        / (min(count, index + half + 1) - max(0, index - half))
        for index, value in enumerate(envelope)
    )


@dataclass(frozen=True)
class Alignment:
    """What one recording's offset has to move by to sit on another's."""

    block_id: str
    shift_ms: int
    score: float


@dataclass(frozen=True)
class Reading:
    """What was heard of one recording, and where in it that began.

    The origin is not decoration. A stretch read from the middle of a
    recording begins wherever the container was able to begin, which for a
    video file is routinely a fraction of a second away from the moment
    asked for -- and a different fraction for the next file. Two readings
    compared without it are two stretches that started in places nobody
    chose, and the answer is out by the difference.
    """

    envelope: tuple
    origin_ms: int


# What one block of an envelope is worth on the clock the recordings are
# meant to share: the offsets are in milliseconds, the sound is measured in
# blocks, and this is where the two meet.
MS_PER_BLOCK = 1000 / BLOCKS_PER_SEC


@dataclass(frozen=True)
class Sound:
    """A reading placed on the clock every recording is meant to share.

    The offset says which moment of its own timeline a common moment falls
    on, so the common moment a reading begins at is its origin less that
    offset: `t - offset` is the shared clock, which is the whole of what an
    offset means. Two readings of one event therefore land on one another
    once the two offsets are right -- and where they do not, an eye on the
    two of them side by side is the fallback. See `widgets/sync_strip.py`.
    """

    envelope: tuple
    origin_ms: int
    offset_ms: int

    @property
    def start_ms(self) -> int:
        """The common moment the reading's first block is on."""

        return self.origin_ms - self.offset_ms

    @property
    def end_ms(self) -> int:
        """The common moment just past its last block."""

        return round(self.start_ms + len(self.envelope) * MS_PER_BLOCK)

    @property
    def read_around_ms(self) -> int:
        """The common moment the reading was taken around, which is where
        the video was when its sound was read."""

        return round(self.start_ms + len(self.envelope) * MS_PER_BLOCK / 2)

    def ms_at(self, block: int) -> int:
        """The common moment a block of the reading is on."""

        return round(self.start_ms + block * MS_PER_BLOCK)

    def levels_in(self, from_ms: int, to_ms: int, columns: int) -> tuple[float, ...]:
        """The loudest sound of each column's worth of a run of the reading.

        Between two moments of the common clock, so that two readings can
        be measured against one axis. What a strip of a few hundred columns
        is of a reading of tens of thousands of blocks: the loudest of each
        column, since one loud moment hidden between two quiet ones is
        exactly the thing being looked for by eye.
        """

        if columns <= 0 or to_ms <= from_ms or not self.envelope:
            return ()

        count = len(self.envelope)
        span = to_ms - from_ms

        levels = []

        for column in range(columns):
            first = _block_at(from_ms + span * column / columns, self.start_ms)
            last = _block_at(from_ms + span * (column + 1) / columns, self.start_ms)

            first, last = max(0, min(count, first)), max(0, min(count, last))

            if last <= first:
                levels.append(0.0)
            else:
                levels.append(max(self.envelope[first:last]))

        return tuple(levels)


def _block_at(common_ms: float, start_ms: int) -> int:
    """Which block of a reading the common clock is on at a moment."""

    return round((common_ms - start_ms) / MS_PER_BLOCK)


def common_moment_ms(position_ms: int, offset_ms: int) -> int:
    """Which moment of the shared clock a recording is showing.

    The one line the whole feature is about: `t - offset` is the clock every
    recording is meant to share, so where a video is on that clock is where
    it is in its own timeline less the sync point it has been given. Two
    videos showing the same moment of what they recorded come to the same
    number; the strips draw it, and where two of them disagree by is exactly
    how far out the offsets are.
    """

    return int(position_ms) - int(offset_ms)


def shared_window(first: Sound, second: Sound) -> tuple[int, int] | None:
    """The stretch of the common clock both readings cover, or None where
    they cover none of it."""

    start = max(first.start_ms, second.start_ms)
    end = min(first.end_ms, second.end_ms)

    if end <= start:
        return None

    return start, end


# How many samples of a block are looked at where a block is long. A coarse
# reading covers a quarter of an hour of a recording at a block a second,
# which is eight thousand samples a block and ten million for the reading:
# measuring every one of them is seconds of arithmetic for a number that is
# there to say how loud a second of a recording was. Taking every fourth
# sample through the block estimates the same loudness to about a percent,
# which is far finer than the shape of it is ever read at.
MAX_BLOCK_SAMPLES = 2000


def envelope_from_samples(
    samples, sample_rate: int = SAMPLE_RATE, blocks_per_sec: int = BLOCKS_PER_SEC
) -> tuple[float, ...]:
    """The loudness of each slice of a run of samples.

    The square root of the mean square, which is what makes a loud moment
    stand out from a quiet one whatever the recording was levelled at.

    A slice at a time, summed through `map(mul, ...)`, which is done in C
    and comes to about twice the speed of adding the squares up one sample
    at a time in Python -- measured on ten million samples, 0.9 s against
    1.6. See MAX_BLOCK_SAMPLES for what happens where a block is long.
    """

    if sample_rate <= 0 or blocks_per_sec <= 0:
        return ()

    every = max(1, round(sample_rate / blocks_per_sec) // MAX_BLOCK_SAMPLES)
    block = max(1, round(sample_rate / blocks_per_sec) // every)

    if every > 1:
        samples = samples[::every]

    envelope = []

    for at in range(0, len(samples) // block * block, block):
        one = samples[at : at + block]

        envelope.append(math.sqrt(sum(map(mul, one, one)) / block))

    return tuple(envelope)


def envelope_from_wav(
    path,
    blocks_per_sec: int = BLOCKS_PER_SEC,
    from_ms: int = 0,
    span_ms: int | None = None,
):
    """The envelope of a stretch of a wav file, or () where none can be read.

    A stretch and not the whole of it: the sound is read from the start of
    a recording, which for a long one is minutes of audio written out to be
    looked at for a few seconds in the middle. `blocks_per_sec` is how
    finely that stretch is to be measured, which is what a coarse reading
    of minutes of a recording asks to have turned down.
    """

    try:
        with wave.open(str(path), "rb") as wav:
            channels = wav.getnchannels()
            width = wav.getsampwidth()
            rate = wav.getframerate()

            if from_ms > 0:
                wav.setpos(min(int(from_ms * rate / 1000), wav.getnframes()))

            frames_wanted = wav.getnframes()
            if span_ms is not None:
                frames_wanted = min(frames_wanted, int(span_ms * rate / 1000))

            frames = wav.readframes(frames_wanted)
    except (wave.Error, OSError, EOFError) as e:
        _log.warning(f"Could not read the probe's wav: {e}")
        return ()

    if width != 2:
        _log.warning(f"Probe gave {width * 8}-bit samples, wanted 16")
        return ()

    samples = array.array("h")
    samples.frombytes(frames)

    if channels > 1:
        # one channel is asked for, so this is only ever a fallback: taking
        # every nth sample keeps the timing and is enough to see a clap by
        samples = samples[::channels]

    return envelope_from_samples(samples, rate, blocks_per_sec)


# How many recordings' stretches are kept. The sound of every stretch up to
# the moment asked for has been decoded to get it, and one stretch per
# recording is all that is ever wanted at once: a grid is a handful of
# videos, and a reading of one of them is replaced rather than added to.
KEPT_RECORDINGS = 3


# What a kept stretch is a stretch of, to the length of a stretch asked
# for. A recording whose file has been replaced or written to again is a
# different recording, and its stretch is read afresh rather than served
# from a file that no longer holds it.
def file_key(uri) -> tuple | None:
    """What tells one state of a recording from another, or None for one
    that cannot be looked at."""

    try:
        stat = Path(uri).stat()
    except OSError:
        return None

    return (str(uri), stat.st_mtime_ns, stat.st_size)


@dataclass(frozen=True)
class _Kept:
    """One stretch of one recording, held as the sound it was read into.

    `until_ms` is what the read was asked for and is what decides whether
    it can answer for a later reading: asked for no more than this, and the
    file holds everything a fresh read would have written. `covered_ms` is
    what the file turned out to hold, which is short of what was asked for
    where the recording ends before the moment does -- and is short by a
    few milliseconds often enough (a transcode stops on a block boundary)
    that it cannot be the thing a reading is matched against.
    """

    path: Path
    until_ms: int
    covered_ms: int


class SnippetCache:
    """Stretches of recordings, kept between readings of them.

    Reading a stretch means decoding everything before it, which runs at a
    few hundred times the length of what is being decoded and grows with
    how far into a recording the moment lies. Lining up by sound is the
    thing done over and over -- read, correct, look, read again -- and
    every reading after the first of a recording pays for a decode that has
    already been done.

    What is kept is the sound itself rather than the envelope of it: the
    envelope of a stretch can be read out of the sound in a moment, and
    keeping whole envelopes of every reading would cost more memory than
    the stretches cost on disk (16 KB for each second of a recording).

    A stretch is only ever read from the start of a recording up to some
    moment, so a stretch read for one moment answers for every earlier one,
    and one file per recording covers every reading of it until a reading
    deeper in than any before. Nothing is sought to: see audio_probe.

    What decides whether a kept stretch answers is the moment it was asked
    for, and not what the file turned out to hold: a transcode stops on a
    block boundary, so a file written for the seventh second of a recording
    holds a few milliseconds less than seven seconds, and matching against
    that would read the same sound again every time it was asked for.

    A reading is run one to a recording at a time -- see sync_align -- so
    what is here is reached by several of them at once. Each recording has
    a lock of its own, held for the whole of a reading of it: readings of
    different recordings go on at once, which is the point, and two
    readings of one recording are kept to one decode of it.
    """

    def __init__(self, max_recordings: int = KEPT_RECORDINGS):
        self._max_recordings = max_recordings
        self._kept: OrderedDict[tuple, _Kept] = OrderedDict()
        self._directory: Path | None = None
        self._written = 0

        # everything kept, and the locks one recording at a time is read
        # under. The locks are never let go of, even where what they guard
        # is: a lock is a few bytes, and one let go of while another
        # reading waits on it is two readings of one recording at once
        self._lock = Lock()
        self._key_locks: dict[tuple, Lock] = {}
        self._no_key_lock = Lock()

    def envelope(
        self,
        key,
        origin_ms: int,
        snippet_ms: int,
        fetch,
        blocks_per_sec: int = BLOCKS_PER_SEC,
    ) -> tuple:
        """The envelope of a stretch, read for the first time or not.

        `fetch(until_ms, destination)` is what reads a stretch off a
        recording and is only called where what is already kept cannot
        answer: it is the caller that knows how to talk to a player.

        `blocks_per_sec` is how finely the stretch is to be measured, which
        is asked for per reading and does not change what is kept: a
        stretch read for a coarse look at minutes of a recording answers
        just as well for a fine look at a snippet of it, and that is what
        makes the two passes cost one decode between them.
        """

        with self._lock_for(key):
            return self._envelope(key, origin_ms, snippet_ms, fetch, blocks_per_sec)

    def _envelope(
        self, key, origin_ms: int, snippet_ms: int, fetch, blocks_per_sec: int
    ) -> tuple:
        until_ms = origin_ms + snippet_ms
        kept = self._kept_now(key)

        if kept is not None:
            if origin_ms >= kept.covered_ms:
                # past the end of the recording, where reading it again
                # would only find the same nothing
                return ()

            if until_ms <= kept.until_ms:
                _log.debug(
                    f"Sound up to {until_ms} ms of {self._name(key)} was read"
                    " already: reading it from there"
                )

                # read outside the lock: another recording is being read
                # while this one is looked at, which is the whole point of
                # the locks being per recording
                envelope = envelope_from_wav(
                    kept.path,
                    blocks_per_sec=blocks_per_sec,
                    from_ms=origin_ms,
                    span_ms=snippet_ms,
                )

                if envelope:
                    return envelope

                # the stretch is no longer readable: read it again rather
                # than serve an answer made of nothing
                self._forget(key)

        path = self._new_path()

        started = time.monotonic()

        if not fetch(until_ms, path):
            return ()

        # said out loud because it is the slow part of lining recordings up,
        # and because a second reading of one of them that does not say it
        # is a second reading that did not have to decode anything
        _log.info(
            f"Read {self._name(key)} up to {until_ms} ms in"
            f" {time.monotonic() - started:.2f}s"
        )

        covered_ms = _covered_ms(path)

        if key is not None and covered_ms > 0:
            self._keep(key, _Kept(path, until_ms, covered_ms))

        return envelope_from_wav(
            path,
            blocks_per_sec=blocks_per_sec,
            from_ms=origin_ms,
            span_ms=snippet_ms,
        )

    def _kept_now(self, key) -> _Kept | None:
        """What is kept for a recording, freshly used.

        Held for no longer than the look at what is here: whether it
        answers is a question about the file, which takes no locking to ask.
        """

        if key is None:
            return None

        with self._lock:
            kept = self._kept.get(key)

            if kept is not None:
                self._kept.move_to_end(key)

            return kept

    def _lock_for(self, key) -> Lock:
        """The lock one recording is read under.

        A reading of a recording that cannot be told from another -- a
        stream, or a file that has gone -- is read under a lock of its own
        rather than under the one everything kept is, which would hold up
        every other reading for as long as a decode lasts.
        """

        if key is None:
            return self._no_key_lock

        with self._lock:
            return self._key_locks.setdefault(key, Lock())

    def clear(self):
        """Let go of everything kept, files and all."""

        with self._lock:
            for key in list(self._kept):
                self._drop(key)

            if self._directory is not None:
                shutil.rmtree(self._directory, ignore_errors=True)
                self._directory = None

    def _new_path(self) -> Path:
        with self._lock:
            if self._directory is None:
                self._directory = Path(tempfile.mkdtemp(prefix="gridplayer-sound-"))

                # the stretches are worth keeping for as long as the process
                # is and no longer: they are copies of sound the recordings
                # still hold
                atexit.register(shutil.rmtree, self._directory, ignore_errors=True)

            # a name nothing else in this run has had, so a file let go of
            # and read again is never written over one still in use
            self._written += 1

            return self._directory / f"snippet-{self._written}.wav"

    def _keep(self, key, kept: _Kept):
        with self._lock:
            self._drop(key)
            self._kept[key] = kept

            while len(self._kept) > self._max_recordings:
                _, oldest = self._kept.popitem(last=False)
                self._unlink(oldest.path)

    def _forget(self, key):
        with self._lock:
            self._drop(key)

    def _drop(self, key):
        """Let go of one recording, the lock already held."""

        kept = self._kept.pop(key, None)

        if kept is not None:
            self._unlink(kept.path)

    @staticmethod
    def _unlink(path: Path):
        """A stretch let go of, where it can be.

        Another reading may be part way through the same file -- it is let
        go of for being the oldest rather than for being unused -- and a
        file still open cannot be taken away on Windows. What is left of
        one is the temporary directory, which goes when the process does.
        """

        try:
            path.unlink(missing_ok=True)
        except OSError as e:
            _log.debug(f"Could not let go of {path.name}: {e}")

    @staticmethod
    def _name(key) -> str:
        return Path(key[0]).name if key else "the recording"


def _covered_ms(path: Path) -> int:
    """How much sound the file actually holds, or 0 where it holds none."""

    try:
        with wave.open(str(path), "rb") as wav:
            return round(wav.getnframes() * 1000 / wav.getframerate())
    except (wave.Error, OSError, EOFError, ZeroDivisionError):
        return 0


def best_lag(
    reference,
    other,
    max_lag_blocks: int | None = None,
    scale: Scale = FINE_SCALE,
) -> Lag | None:
    """How far `other` sits behind `reference`, and how sure that is.

    Found in two passes, roughly and then closely, because the range worth
    searching is long and the fineness worth having is small, and searching
    the whole range at that fineness is a great many comparisons to answer
    a question that a few of them would have placed. The rough pass says
    where in the range it is; the close pass says exactly where.

    How sure the answer is takes a third thing: what the other places in
    the range are worth. See _prominence.

    `scale` says how long a block of the envelopes given is and how far
    apart they may be, so that the same search answers for a snippet read
    around the moment and for minutes of the recordings.
    """

    if not reference or not other:
        return None

    if max_lag_blocks is None:
        max_lag_blocks = scale.max_lag_blocks

    shaped_reference = _shaped(reference, scale.shape_span_blocks)
    shaped_other = _shaped(other, scale.shape_span_blocks)

    rough = _rough_lag(reference, other, max_lag_blocks, scale)

    if rough is None:
        return None

    found = _fine_lag(
        shaped_reference, shaped_other, rough.lag.blocks, max_lag_blocks, scale
    )

    answer = rough.lag.blocks if found is None else found.blocks

    # Judged over everything the two have in common, and not over the short
    # stretch the timing was taken from. Four seconds of a quiet room says
    # nothing about whether two recordings are the same, and a lag settled
    # on such a stretch would be thrown away for the number it scored.
    score = _overall_score(shaped_reference, shaped_other, answer, scale)

    if score <= 0:
        # nothing could be measured over all they share, which is what the
        # rough pass was a shortcut for: its own number is all there is
        score = rough.lag.score

    return Lag(
        answer,
        score,
        _prominence(
            score, _rival_score(shaped_reference, shaped_other, rough, answer, scale)
        ),
        scale.blocks_per_sec,
    )


def _overall_score(reference, other, blocks: int, scale: Scale) -> float:
    """How well two envelopes agree at a lag, over all they share."""

    overlap = _overlap(reference, other, blocks, scale.min_overlap_blocks)

    if overlap is None:
        return 0.0

    score = _correlation(*overlap)

    return 0.0 if score is None else score


# How many of the other places the rough search liked are measured again
# before deciding whether the answer stands out. The rough search is a
# shortcut and its numbers are not the numbers the answer is judged by, so
# the rivals it liked are put back on the same footing as the answer --
# and the few it liked most are the only ones that could beat it.
RIVAL_CANDIDATES = 8


def _rival_score(reference, other, rough, answer_blocks, scale: Scale) -> float | None:
    """What the best place other than the answer is worth, measured the
    same way the answer was.

    The same way, because the rough search compared pooled sound and the
    answer is judged on sound at full size: pooling flattens a stretch of
    short sounds into nearly one value, so how far a rival peak stands
    below the answer depends on how much of it survives the pooling. A
    rival measured while pooled can look closer to the answer than it is.
    """

    rivals = sorted(
        (
            found
            for found in rough.scored
            if abs(found[0] * rough.step - answer_blocks)
            >= scale.peak_separation_blocks
        ),
        key=lambda found: -found[1],
    )

    if not rivals:
        return None

    return max(
        _overall_score(reference, other, blocks * rough.step, scale)
        for blocks, _score in rivals[:RIVAL_CANDIDATES]
    )


def _prominence(best: float, rival: float | None) -> float:
    """How far the best peak stands above the best one far from it.

    As a share of the best peak's own score, so that it reads the same on a
    quiet pair of recordings as on a loud one. With no rival peak at all
    there is nothing to doubt; with a best peak that does not even stand
    above nothing there is no answer to trust.
    """

    if rival is None or best <= 0:
        return 1.0 if rival is None else 0.0

    return max(0.0, min(1.0, 1.0 - rival / best))


@dataclass(frozen=True)
class _Rough:
    """What the rough pass found, and what it saw on the way.

    `scored` is at the size the rough pass searched, which over the whole
    range is coarser than the envelopes themselves: `step` says how many of
    the finer blocks one of its steps is worth.
    """

    lag: Lag
    scored: tuple
    step: int


def _rough_lag(reference, other, max_lag_blocks, scale: Scale) -> _Rough | None:
    """Where in the range it is, to within a coarse block or so."""

    if max_lag_blocks <= scale.coarse_decimation * 4:
        # a range too short to be worth making rough: look at all of it
        scored = _scan(
            _shaped(reference, scale.shape_span_blocks),
            _shaped(other, scale.shape_span_blocks),
            -max_lag_blocks,
            max_lag_blocks,
            scale.min_overlap_blocks,
        )

        step = 1
    else:
        coarse_max = max(1, max_lag_blocks // scale.coarse_decimation)

        # shaped at the size it is now and not before: averaging away the
        # swell at one size and then pooling the result would pool what is
        # left of a signal that has already had its middle taken out, which
        # is very nearly nothing
        coarse_span = max(2, scale.shape_span_blocks // scale.coarse_decimation)

        scored = _scan(
            _shaped(_decimate(reference, scale.coarse_decimation), coarse_span),
            _shaped(_decimate(other, scale.coarse_decimation), coarse_span),
            -coarse_max,
            coarse_max,
            max(1, scale.min_overlap_blocks // scale.coarse_decimation),
        )

        step = scale.coarse_decimation

    best = _best(scored, scale)

    if best is None:
        return None

    return _Rough(
        lag=Lag(best.blocks * step, best.score, blocks_per_sec=scale.blocks_per_sec),
        scored=scored,
        step=step,
    )


def _fine_lag(reference, other, rough_blocks, max_lag_blocks, scale: Scale):
    """The same, looked at closely around where it was found roughly.

    A stretch of what the two have in common at the lag, and not the whole
    of it: four seconds is enough to place a sound and keeps the close look
    quick, where the whole of a snippet at every lag in reach would be a
    great many more comparisons for the same answer.

    What is compared is the middle of the overlap at that lag -- the same
    stretch `_overall_score` measures -- rather than a window taken from one
    reading and looked for in the other. A window has to *fit* in the other
    at that lag, and a reading barely longer than the window has nowhere to
    put it but where it already is: that is the wide pass's shape, whose
    readings are minutes of a recording against a window of minutes, and
    taking a window that cannot move leaves every negative lag unlooked at.
    """

    low = max(-max_lag_blocks, rough_blocks - scale.fine_reach_blocks)
    high = min(max_lag_blocks, rough_blocks + scale.fine_reach_blocks)

    best = None

    for blocks in range(low, high + 1):
        overlap = _overlap(reference, other, blocks, scale.min_overlap_blocks)

        if overlap is None:
            continue

        shared, also_shared = overlap

        span = min(scale.fine_span_blocks, len(shared))
        start = (len(shared) - span) // 2

        score = _correlation(
            shared[start : start + span], also_shared[start : start + span]
        )

        if score is None:
            continue

        if best is None or score > best.score:
            best = Lag(blocks, score, blocks_per_sec=scale.blocks_per_sec)

    return best


def _decimate(envelope, by: int = COARSE_DECIMATION):
    """Every `by` blocks averaged into one: the same shape, coarser."""

    return tuple(
        sum(envelope[index : index + by]) / by
        for index in range(0, len(envelope) - by + 1, by)
    )


def _scan(reference, other, low: int, high: int, min_overlap: int):
    """How well the two agree at every lag from one end of the range to the
    other, at whatever size the envelopes given are."""

    scored = []

    for blocks in range(low, high + 1):
        overlap = _overlap(reference, other, blocks, min_overlap)

        if overlap is None:
            continue

        score = _correlation(*overlap)

        if score is None:
            continue

        scored.append((blocks, score))

    return scored


def _best(scored, scale: Scale) -> Lag | None:
    """The best of a range that has been scanned."""

    if not scored:
        return None

    blocks, score = max(scored, key=lambda found: found[1])

    return Lag(blocks, score, blocks_per_sec=scale.blocks_per_sec)


def _overlap(reference, other, blocks: int, min_overlap: int):
    """The two runs of blocks that sit on top of each other at this lag.

    The lag says where the reference's start lands in the other, so block `i`
    of the reference is block `blocks + i` of the other, and what the two
    share is bounded by *both* lengths: however much of the reference there
    is, and however much of the other is left after the lag.

    What it is **not** is the reference's length less the lag. That is what
    the share comes to when the two readings are of one length, and it was
    written for two of one length -- a snippet against a snippet -- where the
    two are the same number. They are not the same number at the wide scale,
    where the recordings are of different lengths: 120 s of recording against
    one with a minute added at the front is 119 blocks against 179, and at
    the lag that lines them up (a minute) the reference's length less the lag
    is 59 blocks -- one short of the minute of shared sound wanted, so every
    lag past about `min_overlap` was left unevaluated and a recording with a
    minute of something extra in it came back as nothing like the other at
    all. Measured on two such files: 40 s inserted, found; 80 s inserted,
    thrown away; with this, both.
    """

    if blocks >= 0:
        shared = min(len(reference), len(other) - blocks)
        a = reference[:shared]
        b = other[blocks : blocks + shared]
    else:
        shared = min(len(other), len(reference) + blocks)
        a = reference[-blocks : -blocks + shared]
        b = other[:shared]

    if shared < min_overlap:
        return None

    return a, b


def _correlation(a, b) -> float | None:
    """How well two runs of blocks agree, from -1 to 1."""

    count = len(a)

    mean_a = sum(a) / count
    mean_b = sum(b) / count

    covariance = 0.0
    spread_a = 0.0
    spread_b = 0.0

    for value_a, value_b in zip(a, b):
        off_a = value_a - mean_a
        off_b = value_b - mean_b

        covariance += off_a * off_b
        spread_a += off_a * off_a
        spread_b += off_b * off_b

    if spread_a <= 0 or spread_b <= 0:
        # one of them is a flat line: there is no shape to match against
        return None

    return covariance / math.sqrt(spread_a * spread_b)


class Outcome(AutoName):
    """What became of one recording."""

    REFERENCE = auto()  # the one the rest were judged against
    LINED_UP = auto()  # its offset was moved to where the sound says
    NO_SOUND = auto()  # nothing could be read from it
    NOTHING_ALIKE = auto()  # read, but like nothing else that was read
    AMBIGUOUS = auto()  # matched too many places to pick one
    CONTRADICTED = auto()  # the sound disagreed with itself about it
    UNCONNECTED = auto()  # agreed with something, but not with the reference


@dataclass(frozen=True)
class Pair:
    """What one recording's sound said about one other's.

    `delta_ms` is the distance between the offsets the sound asks for: the
    recording named by `block_id` wants to sit this far after the one named
    by `reference_id` on the common clock. That is a property of the two
    recordings and not of the offsets they happen to have, which is why the
    offsets in hand do not come into it.

    `lag_ms` is only how much later in the second reading the first
    reading's sound turned up, which is not the same number: the two were
    read around different moments of their recordings, and where each of
    them began is the rest of the difference.
    """

    reference_id: str
    block_id: str
    delta_ms: int
    lag_ms: int
    score: float
    prominence: float
    is_reliable: bool


@dataclass(frozen=True)
class Verdict:
    """What became of one recording, and the numbers behind it.

    The score and prominence are the ones for the pair with the reference
    where those two were compared directly, and for the best pair the
    recording was part of where they were not.
    """

    block_id: str
    outcome: Outcome
    score: float | None = None
    prominence: float | None = None

    @property
    def is_lined_up(self) -> bool:
        return self.outcome in (Outcome.REFERENCE, Outcome.LINED_UP)


@dataclass(frozen=True)
class Conflict:
    """A pair whose sound does not agree with the rest about where the two
    of them sit."""

    reference_id: str
    block_id: str
    residual_ms: int


@dataclass(frozen=True)
class AlignResult:
    """What the sound of every recording, taken together, came to.

    Every recording asked about is in `verdicts`, whether or not anything
    was done about it: whoever wonders why one of them was left where it
    was is owed the reason, and the numbers the reason was made from.
    """

    reference_id: str | None
    alignments: tuple[Alignment, ...]
    verdicts: dict[str, Verdict]
    pairs: tuple[Pair, ...]
    conflicts: tuple[Conflict, ...]


def measure_pairs(
    readings: dict,
    ids=None,
    min_score: float | None = None,
    min_prominence: float | None = None,
    scale: Scale = FINE_SCALE,
) -> tuple[Pair, ...]:
    """Every pair of readings, each with what it says about the other.

    Every pair, and not each against one chosen recording. One recording
    may have nothing to offer -- no sound, a moment far from anyone
    else's, nothing like what the others heard -- and a run that judged
    everything against it would carry that into every one of its answers.
    Pairs are also what can be checked against each other: three readings
    give three answers where each of them can be worked out from the other
    two, and a set that does not add up is a set to report rather than to
    choose from.
    """

    min_score = scale.min_score if min_score is None else min_score
    min_prominence = scale.min_prominence if min_prominence is None else min_prominence

    heard = [
        block_id
        for block_id in (ids if ids is not None else readings)
        if readings.get(block_id) is not None and readings[block_id].envelope
    ]

    pairs = []

    for reference_id, block_id in combinations(heard, 2):
        reference = readings[reference_id]
        other = readings[block_id]

        lag = best_lag(reference.envelope, other.envelope, scale=scale)

        if lag is None:
            continue

        pairs.append(
            Pair(
                reference_id=reference_id,
                block_id=block_id,
                delta_ms=other.origin_ms - reference.origin_ms + lag.offset_shift_ms,
                lag_ms=lag.offset_shift_ms,
                score=lag.score,
                prominence=lag.prominence,
                is_reliable=(
                    lag.score >= min_score and lag.prominence >= min_prominence
                ),
            )
        )

    return tuple(pairs)


def align(
    readings: dict,
    offsets: dict,
    ids=None,
    min_score: float | None = None,
    min_prominence: float | None = None,
    scale: Scale = FINE_SCALE,
) -> AlignResult:
    """Where each recording's offset wants moving to, from all their sound.

    `offsets` are the ones in hand when the sound was read, and the shifts
    that come back are against those: a recording already known to be a
    second ahead does not want telling again.

    Nothing is moved on a disagreement. Where two pairs of the same three
    recordings disagree by more than the scale's tolerance about where one
    of them sits, the pairs involved in the disagreement and everything
    that was only placed through them are left where they are, and the
    disagreement is reported. Picking one of the two would be picking a
    recording to believe without knowing which of them is wrong.

    `scale` is the size the readings were measured at, which is what turns
    a lag in blocks into milliseconds: see Scale.
    """

    min_score = scale.min_score if min_score is None else min_score
    min_prominence = scale.min_prominence if min_prominence is None else min_prominence

    order = list(ids) if ids is not None else list(readings)
    pairs = measure_pairs(readings, order, min_score, min_prominence, scale)
    reliable = tuple(pair for pair in pairs if pair.is_reliable)

    reference_id = _reference_of(order, reliable)

    if reference_id is None:
        return AlignResult(
            None,
            (),
            _verdicts(
                order, readings, pairs, None, {}, frozenset(), min_score, min_prominence
            ),
            pairs,
            (),
        )

    deltas = _deltas_to(reference_id, reliable)
    conflicts = _conflicts(deltas, reliable, scale.conflict_ms)

    suspects = frozenset(
        block_id
        for conflict in conflicts
        for block_id in (conflict.reference_id, conflict.block_id)
        if block_id != reference_id
    )

    if suspects:
        deltas = _deltas_to(
            reference_id,
            tuple(pair for pair in reliable if not _touches(pair, suspects)),
        )

    verdicts = _verdicts(
        order,
        readings,
        pairs,
        reference_id,
        deltas,
        suspects,
        min_score,
        min_prominence,
    )

    alignments = []

    for block_id, verdict in verdicts.items():
        if verdict.outcome is not Outcome.LINED_UP:
            continue

        shift = deltas[block_id] - (
            offsets.get(block_id, 0) - offsets.get(reference_id, 0)
        )

        alignments.append(
            Alignment(block_id, shift, verdict.score if verdict.score else 0.0)
        )

    return AlignResult(reference_id, tuple(alignments), verdicts, pairs, conflicts)


def _reference_of(order, reliable) -> str | None:
    """The recording the rest are judged against, or None where no two
    recordings agreed about anything.

    The one that the most of the others agreed with, and then the one they
    agreed with most. With three recordings and three pairs, each of them
    is in two pairs, so the count settles nothing -- but the one in the
    middle scored well twice where each of the ends scored well once and
    poorly once, and the middle is the one to measure the others from.

    Where two of them are agreed with equally, the sums they are compared
    by are rounded before they are compared: two recordings that the others
    agree with equally are equal to the last bit of a sum of correlations
    or they are not, and the last bit of a sum is no reason to prefer one
    recording to another. What is left of a tie goes to the earlier video,
    so the same grid lines up the same way twice running.
    """

    if not reliable:
        return None

    agreed_with = Counter()
    total_score = defaultdict(float)

    for pair in reliable:
        for block_id, score in (
            (pair.reference_id, pair.score),
            (pair.block_id, pair.score),
        ):
            agreed_with[block_id] += 1
            total_score[block_id] += score

    return max(
        order,
        key=lambda block_id: (
            agreed_with[block_id],
            round(total_score[block_id], 6),
            -order.index(block_id),
        ),
    )


def _deltas_to(reference_id, pairs) -> dict:
    """How far each recording's offset sits from the reference's.

    Along the fewest pairs that connect the two, so a recording spoken for
    directly is not placed through something else, and a recording the
    sound says nothing about is not in the answer at all.
    """

    neighbours = defaultdict(list)

    for pair in pairs:
        neighbours[pair.reference_id].append((pair.block_id, pair.delta_ms))
        neighbours[pair.block_id].append((pair.reference_id, -pair.delta_ms))

    deltas = {reference_id: 0}
    queue = deque([reference_id])

    while queue:
        here = queue.popleft()

        for block_id, delta in neighbours[here]:
            if block_id in deltas:
                continue

            deltas[block_id] = deltas[here] + delta
            queue.append(block_id)

    return deltas


def _conflicts(deltas, pairs, conflict_ms: int = CONFLICT_MS) -> tuple[Conflict, ...]:
    """The pairs whose sound does not agree with the rest."""

    found = []

    for pair in pairs:
        if pair.reference_id not in deltas or pair.block_id not in deltas:
            continue

        residual = pair.delta_ms - (deltas[pair.block_id] - deltas[pair.reference_id])

        if abs(residual) > conflict_ms:
            found.append(Conflict(pair.reference_id, pair.block_id, residual))

    return tuple(found)


def _touches(pair, block_ids) -> bool:
    return pair.reference_id in block_ids or pair.block_id in block_ids


def _verdicts(
    order, readings, pairs, reference_id, deltas, suspects, min_score, min_prominence
) -> dict:
    """What became of each recording, in the order the caller asked."""

    verdicts = {}

    for block_id in order:
        reading = readings.get(block_id)

        if reading is None or not reading.envelope:
            verdicts[block_id] = Verdict(block_id, Outcome.NO_SOUND)
            continue

        best = _pair_between(pairs, reference_id, block_id) or _best_pair_of(
            block_id, pairs
        )

        if block_id == reference_id:
            outcome = Outcome.REFERENCE
        elif block_id in suspects:
            outcome = Outcome.CONTRADICTED
        elif block_id in deltas:
            outcome = Outcome.LINED_UP
        elif best is None:
            # nothing was ever compared with it
            outcome = Outcome.UNCONNECTED
        elif best.score < min_score:
            outcome = Outcome.NOTHING_ALIKE
        elif best.prominence < min_prominence:
            outcome = Outcome.AMBIGUOUS
        else:
            # it agreed with something, and that something did not reach
            # the reference
            outcome = Outcome.UNCONNECTED

        verdicts[block_id] = Verdict(
            block_id,
            outcome,
            None if best is None else best.score,
            None if best is None else best.prominence,
        )

    return verdicts


def _pair_between(pairs, reference_id, block_id) -> Pair | None:
    """The pair that compared these two directly, whichever way round."""

    if reference_id is None:
        return None

    for pair in pairs:
        if {pair.reference_id, pair.block_id} == {reference_id, block_id}:
            return pair

    return None


def _best_pair_of(block_id, pairs) -> Pair | None:
    """The pair this recording got the most out of."""

    scored = [pair for pair in pairs if block_id in (pair.reference_id, pair.block_id)]

    return max(scored, key=lambda pair: pair.score, default=None)
