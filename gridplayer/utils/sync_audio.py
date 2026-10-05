"""Reading the sound of two recordings to find where they line up.

Lining recordings up by eye means finding the same moment twice, and the
moment a picture changes is not always the moment the sound does. Both
recordings carry the same sound though, so the sound can be lined up
instead, and once two envelopes agree the videos are on the same moment.

Nothing here touches VLC or Qt: it is given samples and gives back numbers,
so what it does can be checked without a player. Reading the samples off a
recording is `vlc_player.audio_probe`'s part.
"""

import array
import logging
import math
import wave
from dataclasses import dataclass

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
    """

    blocks: int
    score: float

    @property
    def offset_shift_ms(self) -> int:
        return round(self.blocks * 1000 / BLOCKS_PER_SEC)


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
    """

    if not reference or not other:
        return None

    if max_lag_blocks is None:
        max_lag_blocks = round(MAX_LAG_SEC * BLOCKS_PER_SEC)

    rough = _rough_lag(reference, other, max_lag_blocks)

    if rough is None:
        return None

    shaped_reference = _shaped(reference, SHAPE_SPAN_BLOCKS)
    shaped_other = _shaped(other, SHAPE_SPAN_BLOCKS)

    found = _fine_lag(shaped_reference, shaped_other, rough.blocks, max_lag_blocks)

    if found is None:
        return rough

    # Judged over everything the two have in common, and not over the short
    # stretch the timing was taken from. Four seconds of a quiet room says
    # nothing about whether two recordings are the same, and a lag settled
    # on such a stretch would be thrown away for the number it scored.
    return Lag(
        found.blocks, _overall_score(shaped_reference, shaped_other, found.blocks)
    )


def _overall_score(reference, other, blocks: int) -> float:
    """How well two envelopes agree at a lag, over all they share."""

    overlap = _overlap(reference, other, blocks, MIN_OVERLAP_BLOCKS)

    if overlap is None:
        return 0.0

    score = _correlation(*overlap)

    return 0.0 if score is None else score


def _rough_lag(reference, other, max_lag_blocks):
    """Where in the range it is, to within a coarse block or so."""

    if max_lag_blocks <= COARSE_DECIMATION * 4:
        # a range too short to be worth making rough: look at all of it
        return _search(
            _shaped(reference, SHAPE_SPAN_BLOCKS),
            _shaped(other, SHAPE_SPAN_BLOCKS),
            -max_lag_blocks,
            max_lag_blocks,
            MIN_OVERLAP_BLOCKS,
        )

    coarse_max = max(1, max_lag_blocks // COARSE_DECIMATION)

    # shaped at the size it is now and not before: averaging away the swell
    # at one size and then pooling the result would pool what is left of a
    # signal that has already had its middle taken out, which is very
    # nearly nothing
    coarse_span = max(2, SHAPE_SPAN_BLOCKS // COARSE_DECIMATION)

    found = _search(
        _shaped(_decimate(reference), coarse_span),
        _shaped(_decimate(other), coarse_span),
        -coarse_max,
        coarse_max,
        max(1, MIN_OVERLAP_BLOCKS // COARSE_DECIMATION),
    )

    if found is None:
        return None

    return Lag(found.blocks * COARSE_DECIMATION, found.score)


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


def _search(reference, other, low: int, high: int, min_overlap: int):
    """Every lag from one end of the range to the other, at whatever size
    the envelopes given are."""

    best = None

    for blocks in range(low, high + 1):
        overlap = _overlap(reference, other, blocks, min_overlap)

        if overlap is None:
            continue

        score = _correlation(*overlap)

        if score is None:
            continue

        if best is None or score > best.score:
            best = Lag(blocks, score)

    return best


def _overlap(reference, other, blocks: int, min_overlap: int):
    """The two runs of blocks that sit on top of each other at this lag."""

    if blocks >= 0:
        a = reference[: len(reference) - blocks]
        b = other[blocks:]
    else:
        a = reference[-blocks:]
        b = other[: len(other) + blocks]

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


def compare(
    reference_id: str,
    readings: dict,
    offsets: dict,
    min_score: float = MIN_SCORE,
) -> tuple[list[Alignment], dict]:
    """What each recording wants moving by, and how well each agreed.

    One pass and not two: finding the lag is the costly part of this, and
    a caller wanting both the shifts and the scores would otherwise have
    every one of them found twice.

    The shift is not the lag alone. What two readings put on top of each
    other says where their sounds sit against where each reading began,
    and what is wanted is where they sit in the recordings -- so the
    difference of the origins comes in, and so does the difference of the
    offsets already in hand, since a recording already known to be a
    second ahead does not want telling again.
    """

    reference = readings.get(reference_id)

    if reference is None or not reference.envelope:
        return [], {}

    found = []
    agreed = {}

    for block_id, reading in readings.items():
        if block_id == reference_id or not reading.envelope:
            continue

        lag = best_lag(reference.envelope, reading.envelope)

        if lag is None:
            continue

        agreed[block_id] = lag.score

        if lag.score < min_score:
            continue

        shift = (
            reading.origin_ms
            - reference.origin_ms
            + lag.offset_shift_ms
            - (offsets.get(block_id, 0) - offsets.get(reference_id, 0))
        )

        found.append(Alignment(block_id, shift, lag.score))

    return found, agreed


def alignments(
    reference_id: str,
    readings: dict,
    offsets: dict,
    min_score: float = MIN_SCORE,
) -> list[Alignment]:
    """What each recording's offset wants moving by to sit on the reference's.

    Only the ones that matched well enough to act on come back. A recording
    whose sound says nothing -- silence, or nothing like the reference --
    is left alone rather than moved somewhere wrong, and the caller says so
    by leaving it out of the list it reports.
    """

    return compare(reference_id, readings, offsets, min_score)[0]


def scores(reference_id: str, readings: dict, offsets: dict) -> dict:
    """How well each recording agrees with the reference, act on it or not.

    Reported whether or not it was worth acting on: a viewer wondering why
    a video was left where it was is owed the number that decided it.
    """

    return compare(reference_id, readings, offsets)[1]
