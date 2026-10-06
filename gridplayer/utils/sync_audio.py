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
from pathlib import Path

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

MIN_OVERLAP_BLOCKS = round(MIN_OVERLAP_SEC * BLOCKS_PER_SEC)

# How far apart two recordings may be and still be lined up by this. Long
# enough to be worth doing at all: a range of a second or two is a range
# that could have been matched by hand, and the point of this is the
# recordings nobody wants to match by hand.
MAX_LAG_SEC = 12.0

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
# worst the rough answer can be, with room to spare.
FINE_REACH_BLOCKS = COARSE_DECIMATION * 3

# How much of the sound the close look compares. Four seconds of it is more
# than enough to place a sound, and keeping it to a stretch is what keeps
# the close look quick enough to run while somebody waits.
FINE_SPAN_BLOCKS = 2000


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

    @property
    def offset_shift_ms(self) -> int:
        return round(self.blocks * 1000 / BLOCKS_PER_SEC)


# How far away another peak has to be to count as another peak and not the
# shoulder of this one. A second either way, which is well past the width
# of any one sound and well short of the range being searched.
PEAK_SEPARATION_SEC = 1.0

PEAK_SEPARATION_BLOCKS = round(PEAK_SEPARATION_SEC * BLOCKS_PER_SEC)

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

SHAPE_SPAN_BLOCKS = round(SHAPE_SPAN_SEC * BLOCKS_PER_SEC)


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


def envelope_from_samples(
    samples, sample_rate: int = SAMPLE_RATE, blocks_per_sec: int = BLOCKS_PER_SEC
) -> tuple[float, ...]:
    """The loudness of each slice of a run of samples.

    The square root of the mean square, which is what makes a loud moment
    stand out from a quiet one whatever the recording was levelled at.
    """

    if sample_rate <= 0 or blocks_per_sec <= 0:
        return ()

    block = max(1, round(sample_rate / blocks_per_sec))
    count = len(samples) // block

    envelope = []
    total_squares = 0

    # one pass rather than a slice per block: at this fineness a snippet is
    # hundreds of thousands of samples, and making a new list of each slice
    # of them costs more than the arithmetic
    for index in range(count * block):
        total_squares += samples[index] * samples[index]

        if (index + 1) % block == 0:
            envelope.append(math.sqrt(total_squares / block))
            total_squares = 0

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
    looked at for a few seconds in the middle.
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
    """

    def __init__(self, max_recordings: int = KEPT_RECORDINGS):
        self._max_recordings = max_recordings
        self._kept: OrderedDict[tuple, _Kept] = OrderedDict()
        self._directory: Path | None = None
        self._written = 0

    def envelope(self, key, origin_ms: int, snippet_ms: int, fetch) -> tuple:
        """The envelope of a stretch, read for the first time or not.

        `fetch(until_ms, destination)` is what reads a stretch off a
        recording and is only called where what is already kept cannot
        answer: it is the caller that knows how to talk to a player.
        """

        until_ms = origin_ms + snippet_ms
        kept = self._kept.get(key) if key is not None else None

        if kept is not None:
            self._kept.move_to_end(key)

            if origin_ms >= kept.covered_ms:
                # past the end of the recording, where reading it again
                # would only find the same nothing
                return ()

            if until_ms <= kept.until_ms:
                _log.debug(
                    f"Sound up to {until_ms} ms of {self._name(key)} was read"
                    " already: reading it from there"
                )

                envelope = envelope_from_wav(
                    kept.path, from_ms=origin_ms, span_ms=snippet_ms
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

        return envelope_from_wav(path, from_ms=origin_ms, span_ms=snippet_ms)

    def clear(self):
        """Let go of everything kept, files and all."""

        for key in list(self._kept):
            self._forget(key)

        if self._directory is not None:
            shutil.rmtree(self._directory, ignore_errors=True)
            self._directory = None

    def _new_path(self) -> Path:
        if self._directory is None:
            self._directory = Path(tempfile.mkdtemp(prefix="gridplayer-sound-"))

            # the stretches are worth keeping for as long as the process is
            # and no longer: they are copies of sound the recordings still
            # hold
            atexit.register(shutil.rmtree, self._directory, ignore_errors=True)

        # a name nothing else in this run has had, so a file let go of and
        # read again is never written over one still in use
        self._written += 1

        return self._directory / f"snippet-{self._written}.wav"

    def _keep(self, key, kept: _Kept):
        self._forget(key)
        self._kept[key] = kept

        while len(self._kept) > self._max_recordings:
            _, oldest = self._kept.popitem(last=False)
            oldest.path.unlink(missing_ok=True)

    def _forget(self, key):
        kept = self._kept.pop(key, None)

        if kept is not None:
            kept.path.unlink(missing_ok=True)

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
) -> Lag | None:
    """How far `other` sits behind `reference`, and how sure that is.

    Found in two passes, roughly and then closely, because the range worth
    searching is long and the fineness worth having is small, and searching
    the whole range at that fineness is a great many comparisons to answer
    a question that a few of them would have placed. The rough pass says
    where in the range it is; the close pass says exactly where.

    How sure the answer is takes a third thing: what the other places in
    the range are worth. See _prominence.
    """

    if not reference or not other:
        return None

    if max_lag_blocks is None:
        max_lag_blocks = round(MAX_LAG_SEC * BLOCKS_PER_SEC)

    shaped_reference = _shaped(reference, SHAPE_SPAN_BLOCKS)
    shaped_other = _shaped(other, SHAPE_SPAN_BLOCKS)

    rough = _rough_lag(reference, other, max_lag_blocks)

    if rough is None:
        return None

    found = _fine_lag(shaped_reference, shaped_other, rough.lag.blocks, max_lag_blocks)

    answer = rough.lag.blocks if found is None else found.blocks

    # Judged over everything the two have in common, and not over the short
    # stretch the timing was taken from. Four seconds of a quiet room says
    # nothing about whether two recordings are the same, and a lag settled
    # on such a stretch would be thrown away for the number it scored.
    score = _overall_score(shaped_reference, shaped_other, answer)

    if score <= 0:
        # nothing could be measured over all they share, which is what the
        # rough pass was a shortcut for: its own number is all there is
        score = rough.lag.score

    return Lag(
        answer,
        score,
        _prominence(score, _rival_score(shaped_reference, shaped_other, rough, answer)),
    )


def _overall_score(reference, other, blocks: int) -> float:
    """How well two envelopes agree at a lag, over all they share."""

    overlap = _overlap(reference, other, blocks, MIN_OVERLAP_BLOCKS)

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


def _rival_score(reference, other, rough, answer_blocks) -> float | None:
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
            if abs(found[0] * rough.step - answer_blocks) >= PEAK_SEPARATION_BLOCKS
        ),
        key=lambda found: -found[1],
    )

    if not rivals:
        return None

    return max(
        _overall_score(reference, other, blocks * rough.step)
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


def _rough_lag(reference, other, max_lag_blocks) -> _Rough | None:
    """Where in the range it is, to within a coarse block or so."""

    if max_lag_blocks <= COARSE_DECIMATION * 4:
        # a range too short to be worth making rough: look at all of it
        scored = _scan(
            _shaped(reference, SHAPE_SPAN_BLOCKS),
            _shaped(other, SHAPE_SPAN_BLOCKS),
            -max_lag_blocks,
            max_lag_blocks,
            MIN_OVERLAP_BLOCKS,
        )

        step = 1
    else:
        coarse_max = max(1, max_lag_blocks // COARSE_DECIMATION)

        # shaped at the size it is now and not before: averaging away the
        # swell at one size and then pooling the result would pool what is
        # left of a signal that has already had its middle taken out, which
        # is very nearly nothing
        coarse_span = max(2, SHAPE_SPAN_BLOCKS // COARSE_DECIMATION)

        scored = _scan(
            _shaped(_decimate(reference), coarse_span),
            _shaped(_decimate(other), coarse_span),
            -coarse_max,
            coarse_max,
            max(1, MIN_OVERLAP_BLOCKS // COARSE_DECIMATION),
        )

        step = COARSE_DECIMATION

    best = _best(scored)

    if best is None:
        return None

    return _Rough(lag=Lag(best.blocks * step, best.score), scored=scored, step=step)


def _fine_lag(reference, other, rough_blocks, max_lag_blocks):
    """The same, looked at closely around where it was found roughly."""

    low = max(-max_lag_blocks, rough_blocks - FINE_REACH_BLOCKS)
    high = min(max_lag_blocks, rough_blocks + FINE_REACH_BLOCKS)

    span = min(FINE_SPAN_BLOCKS, len(reference))
    start = max(0, (len(reference) - span) // 2)
    window = reference[start : start + span]

    best = None

    for blocks in range(low, high + 1):
        at = start + blocks

        if at < 0 or at + span > len(other):
            continue

        score = _correlation(window, other[at : at + span])

        if score is None:
            continue

        if best is None or score > best.score:
            best = Lag(blocks, score)

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


def _best(scored) -> Lag | None:
    """The best of a range that has been scanned."""

    if not scored:
        return None

    blocks, score = max(scored, key=lambda found: found[1])

    return Lag(blocks, score)


def _overlap(reference, other, blocks: int, min_overlap: int):
    """The two runs of blocks that sit on top of each other at this lag.

    The lag says where the reference's start lands in the other. For a
    negative lag it lands before the other begins, and the share runs from
    the start of the other to the end of the reference -- so it is the
    reference's length that says how much of it there is. Taking the
    other's length instead comes to the same number while the two readings
    are of one length, and not when they are not: a recording that ends
    inside its own snippet gives a reading shorter than the rest.
    """

    if blocks >= 0:
        a = reference[: len(reference) - blocks]
        b = other[blocks:]
    else:
        a = reference[-blocks:]
        b = other[: len(reference) + blocks]

    shared = min(len(a), len(b))

    if shared < min_overlap:
        return None

    return a[:shared], b[:shared]


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
    min_score: float = MIN_SCORE,
    min_prominence: float = MIN_PROMINENCE,
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

    heard = [
        block_id
        for block_id in (ids if ids is not None else readings)
        if readings.get(block_id) is not None and readings[block_id].envelope
    ]

    pairs = []

    for reference_id, block_id in combinations(heard, 2):
        reference = readings[reference_id]
        other = readings[block_id]

        lag = best_lag(reference.envelope, other.envelope)

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
    min_score: float = MIN_SCORE,
    min_prominence: float = MIN_PROMINENCE,
) -> AlignResult:
    """Where each recording's offset wants moving to, from all their sound.

    `offsets` are the ones in hand when the sound was read, and the shifts
    that come back are against those: a recording already known to be a
    second ahead does not want telling again.

    Nothing is moved on a disagreement. Where two pairs of the same three
    recordings disagree by more than CONFLICT_MS about where one of them
    sits, the pairs involved in the disagreement and everything that was
    only placed through them are left where they are, and the disagreement
    is reported. Picking one of the two would be picking a recording to
    believe without knowing which of them is wrong.
    """

    order = list(ids) if ids is not None else list(readings)
    pairs = measure_pairs(readings, order, min_score, min_prominence)
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
    conflicts = _conflicts(deltas, reliable)

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


def _conflicts(deltas, pairs) -> tuple[Conflict, ...]:
    """The pairs whose sound does not agree with the rest."""

    found = []

    for pair in pairs:
        if pair.reference_id not in deltas or pair.block_id not in deltas:
            continue

        residual = pair.delta_ms - (deltas[pair.block_id] - deltas[pair.reference_id])

        if abs(residual) > CONFLICT_MS:
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
