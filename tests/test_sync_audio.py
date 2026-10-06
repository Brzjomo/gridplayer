"""Reading the sound of recordings to find where they line up.

Two halves, checked apart: what the numbers do, which needs no player, and
reading the numbers off a file, which needs one.
"""

import array
import random
import wave
from itertools import pairwise
from pathlib import Path

import pytest

from gridplayer.utils.sync_audio import (
    BLOCKS_PER_SEC,
    MIN_PROMINENCE,
    MIN_SCORE,
    SAMPLE_RATE,
    Lag,
    Outcome,
    Reading,
    SnippetCache,
    Sound,
    align,
    best_lag,
    envelope_from_samples,
    envelope_from_wav,
    file_key,
    shared_window,
)

# A block of samples at the rates used throughout.
BLOCK = SAMPLE_RATE // BLOCKS_PER_SEC


def _samples(*runs):
    """Samples made of runs of one value each."""

    values = array.array("h")

    for value, count in runs:
        values.extend([value] * count)

    return values


def _pattern(length: int = 6000) -> tuple[float, ...]:
    """A shape worth matching: peaks of uneven size at uneven gaps.

    Deliberately not periodic. A signal that repeats every so many blocks
    matches itself just as well a period earlier or later, and a test that
    cannot tell those apart is not testing the search. Twelve seconds of it
    at the rate the envelopes are measured at, which is long enough for a
    lag of several seconds to have something to sit against.
    """

    values = []
    state = 12345

    for _ in range(length):
        state = (state * 1103515245 + 12345) % 2147483648
        values.append(float(state % 1000) / 100 if state % 7 < 3 else 0.0)

    return tuple(values)


def _shifted(envelope, by: int) -> tuple[float, ...]:
    """`envelope` with the same sound arriving `by` blocks later."""

    if by >= 0:
        return (0.0,) * by + tuple(envelope)

    return tuple(envelope)[-by:]


def _noise(length: int = 6000, seed: int = 1) -> tuple[float, ...]:
    """A recording of something else entirely: sound that never stops.

    Dense rather than made of bursts, and made with a generator of its own
    rather than the arithmetic generator the burst patterns come from --
    whose low bits run together from one seed to the next, so that two of
    its seeds are not two unrelated recordings at all.

    It is dense, too, because a shape with the swell taken out is nearly
    all zero in a signal that is mostly quiet: what is left of such a
    signal is the half-second at each end of the reading, where the swell
    is measured over a shorter window than it is anywhere else, and two
    unrelated quiet recordings then agree far better than they should.
    """

    rolling = random.Random(seed)

    values = []
    level = 0.6

    for _ in range(length):
        level = max(0.15, min(1.0, level + (rolling.random() - 0.5) * 0.4))
        values.append(level + rolling.random() * 0.2)

    return tuple(values)


def _write_wav(path, samples, channels=1, rate=SAMPLE_RATE):
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(samples.tobytes())


# --- what the numbers do ---


class TestTheEnvelope:
    def test_a_loud_run_reads_louder_than_a_quiet_one(self):
        envelope = envelope_from_samples(_samples((0, BLOCK * 2), (1000, BLOCK)), 8000)

        assert envelope == (0.0, 0.0, 1000.0)

    def test_it_measures_loudness_and_not_signedness(self):
        """A sound that swings either side of nothing is as loud as one that
        does not, which is what makes a recording's level not matter."""

        envelope = envelope_from_samples(
            _samples((1000, BLOCK // 2), (-1000, BLOCK // 2))
        )

        assert envelope == (1000.0,)

    def test_it_measures_either_side_of_a_different_level(self):
        quiet = envelope_from_samples(_samples((100, BLOCK)), 8000)[0]
        loud = envelope_from_samples(_samples((1000, BLOCK)), 8000)[0]

        assert quiet < loud

    def test_a_part_block_at_the_end_is_left_out(self):
        envelope = envelope_from_samples(_samples((1000, BLOCK * 3 + BLOCK // 2)), 8000)

        assert len(envelope) == 3

    def test_nothing_in_gives_nothing_back(self):
        assert envelope_from_samples(array.array("h"), 8000) == ()

    def test_a_rate_that_makes_no_sense_gives_nothing_back(self):
        assert envelope_from_samples(_samples((1, BLOCK)), 0) == ()


class TestTheEnvelopeOfAWav:
    def test_it_reads_a_mono_file(self, tmp_path):
        path = tmp_path / "mono.wav"
        _write_wav(path, _samples((0, BLOCK), (2000, BLOCK)))

        assert envelope_from_wav(path) == (0.0, 2000.0)

    def test_it_takes_one_channel_of_a_stereo_file(self, tmp_path):
        """One channel is asked of the probe; this is the fallback."""

        path = tmp_path / "stereo.wav"
        left = _samples((1500, BLOCK))
        interleaved = array.array("h")

        for value in left:
            interleaved.extend([value, 0])

        _write_wav(path, interleaved, channels=2)

        assert envelope_from_wav(path) == (1500.0,)

    def test_a_file_that_is_not_a_wav_gives_nothing_back(self, tmp_path):
        path = tmp_path / "not-a-wav"
        path.write_bytes(b"this is not a wav file at all")

        assert envelope_from_wav(path) == ()

    def test_a_file_that_is_not_there_gives_nothing_back(self, tmp_path):
        assert envelope_from_wav(tmp_path / "absent.wav") == ()


class TestFindingTheLag:
    def test_the_same_sound_lines_up_at_no_lag(self):
        envelope = _pattern()

        lag = best_lag(envelope, envelope)

        assert lag.blocks == 0
        assert lag.score == pytest.approx(1.0)

    @pytest.mark.parametrize("arrives_by", [1, 5, 25, -1, -5, -25])
    def test_a_sound_arriving_later_is_found_that_much_later(self, arrives_by):
        reference = _pattern()

        lag = best_lag(reference, _shifted(reference, arrives_by))

        assert lag.blocks == arrives_by
        # not quite one: the score is taken over all the two share, and one
        # of them is padded with silence past the end of the other
        assert lag.score > 0.99

    def test_a_recording_hearing_it_later_moves_its_offset_on(self):
        """The whole point of the sign: an offset says which moment of a
        recording a common moment is, so a sound heard a second and a half
        later wants the offset moving on by a second and a half."""

        reference = _pattern()
        later_by = round(1.5 * BLOCKS_PER_SEC)

        lag = best_lag(reference, _shifted(reference, later_by))

        assert lag.offset_shift_ms == 1500

    def test_a_quieter_recording_of_the_same_sound_still_matches(self):
        """The average is taken off first, so a recording made quieter
        matches one made louder."""

        reference = _pattern()
        quieter = tuple(value / 20 for value in reference)

        assert best_lag(reference, quieter).blocks == 0

    def test_a_flat_line_matches_nothing(self):
        assert best_lag(_pattern(), (0.0,) * 6000) is None

    def test_a_lag_of_several_seconds_is_found(self):
        """The whole reason for the two passes: a range worth searching is
        longer than one worth searching closely."""

        reference = _pattern()
        later_by = round(5.0 * BLOCKS_PER_SEC)

        lag = best_lag(reference, _shifted(reference, later_by))

        assert lag.blocks == later_by
        assert lag.offset_shift_ms == 5000

    def test_a_lag_between_the_rough_steps_is_still_found_exactly(self):
        """The rough pass lands on a coarse block, which is not where the
        sound is: the close pass is what puts it there."""

        reference = _pattern()
        between = round(3.0 * BLOCKS_PER_SEC) + 37

        lag = best_lag(reference, _shifted(reference, between))

        assert lag.blocks == between

    def test_both_passes_together_agree_with_looking_at_everything(self):
        """The rough pass is an economy, not an approximation of the answer."""

        reference = _pattern(3000)
        other = _shifted(reference, 700)

        everywhere = best_lag(reference, other, max_lag_blocks=2500)

        assert everywhere.blocks == 700

    def test_nothing_in_matches_nothing(self):
        assert best_lag((), _pattern()) is None
        assert best_lag(_pattern(), ()) is None

    def test_too_little_overlap_says_nothing(self):
        """Half a second of shared sound is the least worth an answer."""

        assert best_lag(_pattern(20), _pattern(20)) is None

    def test_the_answer_is_bounded_by_what_it_is_allowed_to_look_at(self):
        reference = _pattern()
        other = _shifted(reference, 25)

        assert best_lag(reference, other, max_lag_blocks=10).blocks != 25


class TestTwoCamerasWithTheirOwnGain:
    """The case this is for, and the one loudness on its own gets wrong.

    Cameras recording the same event each set their own level, so the
    loudness across seconds is the part of the two recordings that agrees
    least -- which is the part a plain loudness envelope is most made of.
    What they agree on is when the sound changed.

    Measured on two recordings of one event with independent automatic
    gain: loudness alone scores 0.34 at the right lag and the shape of the
    sound scores 0.67, for the same lag, found exactly either way. It is
    the number that decides whether to act that loudness alone fails, not
    the lag itself.
    """

    @staticmethod
    def _source(length: int) -> tuple[float, ...]:
        """A continuous sound that never quite stops, and never repeats.

        Aperiodic for the same reason as `_pattern`: a sound that comes
        round again matches itself just as well a period out.
        """

        state = 12345
        values = []

        for _ in range(length):
            state = (state * 1103515245 + 12345) % 2147483648
            values.append(0.6 + (state % 1000) / 1250.0)

        return tuple(values)

    @staticmethod
    def _own_gain(seed: int, length: int, hold: int = 400) -> tuple[float, ...]:
        """A level of its own, moving over about a second and then holding."""

        rolling = random.Random(seed)

        values = []
        level = 0.6

        while len(values) < length:
            level = max(0.15, min(1.0, level + (rolling.random() - 0.5) * 0.4))
            values.extend([level] * hold)

        return tuple(values[:length])

    def test_a_camera_with_its_own_level_is_still_recognised(self):
        length, arrives_by = 6000, 900

        source = self._source(length)
        first = tuple(a * b for a, b in zip(source, self._own_gain(1, length)))
        second = tuple(
            a * b for a, b in zip(self._source(length), self._own_gain(2, length))
        )

        lag = best_lag(first, _shifted(second, arrives_by))

        assert lag.blocks == arrives_by
        assert lag.score >= MIN_SCORE


class TestTheLagReads:
    def test_the_shift_is_the_lag_in_milliseconds(self):
        assert Lag(blocks=BLOCKS_PER_SEC // 2, score=1.0).offset_shift_ms == 500

    def test_a_negative_lag_goes_back_in_time(self):
        assert Lag(blocks=-(BLOCKS_PER_SEC // 2), score=1.0).offset_shift_ms == -500

    def test_one_block_is_what_a_block_is_worth(self):
        assert Lag(blocks=1, score=1.0).offset_shift_ms == round(1000 / BLOCKS_PER_SEC)


class TestHowSureTheAnswerIs:
    """A score says how well two recordings agree; it does not say whether
    anything else agreed nearly as well.

    What a high score is worth depends on the material, so it cannot be the
    thing that decides whether to act. What does not depend on the material
    is whether the place the search settled on stands out from the rest of
    the range.
    """

    def test_a_shape_worth_matching_is_an_answer(self):
        lag = best_lag(_pattern(), _pattern())

        assert lag.score > 0.9
        assert lag.prominence >= MIN_PROMINENCE

    def test_it_is_an_answer_however_quietly_the_two_agree(self):
        """Prominence is a share of the score and not the score: a pair of
        recordings that agree weakly and agree nowhere else is still a
        pair of recordings of the same thing."""

        quiet = tuple(value / 50 for value in _pattern())
        noisier = tuple(value / 50 for value in _pattern())

        lag = best_lag(quiet, noisier)

        assert lag.prominence >= MIN_PROMINENCE

    def test_a_sound_that_comes_round_again_is_not_an_answer(self):
        """One second of sound, twelve times over.

        Everything in it matches everything in it one second along, and the
        search has nothing to choose between those places: the lag it
        settles on is one of a dozen that are just as good.
        """

        envelope = _pattern(BLOCKS_PER_SEC) * 12

        lag = best_lag(envelope, envelope)

        assert lag.score > 0.9
        assert lag.prominence < MIN_PROMINENCE

    def test_nothing_like_it_is_not_an_answer(self):
        """Two recordings of two different things, neither of them quiet."""

        lag = best_lag(_noise(seed=1), _noise(seed=2))

        assert lag.score < MIN_SCORE


class TestReadingsOfDifferentLengths:
    """Readings are as long as the snippet asked for, or shorter where the
    recording ends inside it, so two of them are not always the same
    length."""

    def test_a_sound_that_arrives_before_the_other_starts_is_still_found(self):
        """The whole of the second reading sits inside the first, at the
        end of it: an overlap worked out from the length of the second
        finds nothing to compare at all."""

        reference = _pattern()
        at_the_end = reference[len(reference) // 2 :]

        lag = best_lag(reference, at_the_end)

        assert lag.blocks == -(len(reference) // 2)
        assert lag.score > 0.9


class TestPlacingTheSoundOnTheClock:
    """A reading with an offset on it: what the strips in the dialog draw.

    The offset says which moment of a recording a common moment falls on,
    so a reading placed by one is a run of sound at a place on the clock
    every recording is meant to share.
    """

    def test_a_reading_sits_where_its_origin_less_its_offset_says(self):
        sound = Sound((1.0,) * 100, origin_ms=5000, offset_ms=2000)

        # a hundred blocks of two milliseconds, from 3000
        assert sound.start_ms == 3000
        assert sound.end_ms == 3200
        assert sound.ms_at(50) == 3100
        assert sound.read_around_ms == 3100

    def test_the_same_sound_in_two_recordings_lands_on_the_same_moment(self):
        """The whole point of the offsets: the sound the first recording
        has at its own start, the second has five hundred blocks in, and
        the two offsets say so."""

        first = Sound(_pattern(2000), origin_ms=10000, offset_ms=5000)
        second = Sound((0.0,) * 500 + _pattern(2000), origin_ms=10000, offset_ms=6000)

        assert first.ms_at(0) == second.ms_at(500)

    def test_a_column_is_the_loudest_of_what_it_covers(self):
        sound = Sound((1.0, 2.0, 3.0, 4.0), origin_ms=0, offset_ms=0)

        # four blocks of two milliseconds, drawn as two columns of four
        assert sound.levels_in(0, 8, 2) == (2.0, 4.0)

    def test_a_column_the_reading_does_not_reach_is_drawn_as_nothing(self):
        sound = Sound((1.0,), origin_ms=0, offset_ms=0)

        assert sound.levels_in(100, 108, 2) == (0.0, 0.0)

    def test_the_columns_past_the_end_of_the_reading_are_blank(self):
        """Asked for over a window the reading only partly covers: what it
        does not reach is nothing, and not its last block over and over."""

        sound = Sound((7.0,) * 10, origin_ms=0, offset_ms=0)

        assert sound.levels_in(0, 40, 4) == (7.0, 7.0, 0.0, 0.0)

    def test_readings_that_share_no_time_have_no_window(self):
        first = Sound((1.0,) * 100, origin_ms=0, offset_ms=0)
        second = Sound((1.0,) * 100, origin_ms=5000, offset_ms=0)

        assert shared_window(first, second) is None

    def test_the_window_is_the_stretch_the_two_of_them_share(self):
        first = Sound((1.0,) * 1000, origin_ms=0, offset_ms=0)
        second = Sound((1.0,) * 1000, origin_ms=500, offset_ms=0)

        assert shared_window(first, second) == (500, 2000)


class TestRecordingsOfOneEventStartedApart:
    """The case the whole feature is for: cameras rolling from three
    different moments of the same event.

    Each reading begins at the start of its own recording, and each one
    therefore hears the same sound at a different point of its own
    timeline. What comes out has to be one clock that all three can be
    placed on, which is the thing the offsets exist to describe.
    """

    # One snippet's worth of blocks: what a reading of a whole snippet is.
    READING_BLOCKS = round(30 * BLOCKS_PER_SEC)

    @staticmethod
    def _hears(source, began_ms: int, length: int = READING_BLOCKS) -> tuple:
        """What one recording heard, read from its own start.

        The recording began `began_ms` into the event, so what it holds at
        a point of its own timeline is what the event sounded like that
        much earlier.
        """

        return tuple(
            source[index - began_ms // 2]
            if 0 <= index - began_ms // 2 < len(source)
            else 0.0
            for index in range(length)
        )

    def test_every_recording_comes_out_on_one_clock(self):
        # all of them inside the ±12 s the search reaches either way
        began_ms = {"a": 5000, "b": 7000, "c": 11000}

        source = _noise(self.READING_BLOCKS + 12000)
        readings = {
            block_id: Reading(self._hears(source, began), 0)
            for block_id, began in began_ms.items()
        }

        result = align(readings, dict.fromkeys(began_ms, 0), list(began_ms))

        placed = {result.reference_id: 0}
        placed.update(
            {alignment.block_id: alignment.shift_ms for alignment in result.alignments}
        )

        assert set(placed) == set(began_ms)
        assert result.conflicts == ()

        for block_id, began in began_ms.items():
            assert placed[block_id] == began - began_ms[result.reference_id]

    def test_the_sound_answers_the_same_way_twice(self):
        """Which of them the rest are judged against is the sound's answer,
        and the same sound answers the same way: the videos are moved and
        the button pressed again, and a choice that wandered would move
        them somewhere else each time."""

        source = _noise(self.READING_BLOCKS + 12000)
        readings = {
            block_id: Reading(self._hears(source, began), 0)
            for block_id, began in {"a": 5000, "b": 7000, "c": 11000}.items()
        }
        offsets = {"a": 0, "b": 0, "c": 0}

        first = align(readings, offsets, ["a", "b", "c"])
        second = align(readings, offsets, ["a", "b", "c"])

        assert first.reference_id == second.reference_id
        assert first.alignments == second.alignments


class TestSeveralReadingsAtOnce:
    """The putting together of several answers, with the answers given.

    Finding a lag is TestFindingTheLag's business and costs a correlation
    over thousands of blocks; what is tested here is what is done with two
    or three of them, so they are handed over ready-made.
    """

    @staticmethod
    def _readings(*block_ids) -> dict:
        """One reading per name, each told apart by its own sound."""

        return {
            block_id: Reading((float(index),) * 100, 0)
            for index, block_id in enumerate(block_ids)
        }

    @staticmethod
    def _answers(mocker, readings, answers, score=0.9, prominence=0.5):
        """`best_lag`, answered from a table of what each pair came to.

        The table is keyed by the two block ids, in the order they are
        compared in, which is the order they were given in.
        """

        by_envelope = {
            reading.envelope: block_id for block_id, reading in readings.items()
        }

        def find(reference, other, max_lag_blocks=None):
            lag = answers.get((by_envelope[reference], by_envelope[other]))

            return None if lag is None else Lag(lag, score, prominence)

        return mocker.patch("gridplayer.utils.sync_audio.best_lag", side_effect=find)

    def test_the_recording_the_others_agree_with_is_the_one_judged_against(
        self, mocker
    ):
        """The middle one here, which is the only one that heard both of the
        others. Judged against the first, the third would have nothing."""

        readings = self._readings("a", "b", "c")
        self._answers(mocker, readings, {("a", "b"): 0, ("b", "c"): 0})

        result = align(readings, {}, ["a", "b", "c"])

        assert result.reference_id == "b"
        assert result.verdicts["b"].outcome is Outcome.REFERENCE
        assert result.verdicts["c"].outcome is Outcome.LINED_UP
        assert {alignment.block_id for alignment in result.alignments} == {"a", "c"}

    def test_no_two_recordings_that_agree_leave_nothing_to_judge_by(self, mocker):
        readings = self._readings("a", "b")
        self._answers(mocker, readings, {("a", "b"): 0}, score=0.1)

        result = align(readings, {}, ["a", "b"])

        assert result.reference_id is None
        assert result.alignments == ()
        assert result.verdicts["b"].outcome is Outcome.NOTHING_ALIKE

    def test_an_answer_that_does_not_stand_out_is_not_acted_on(self, mocker):
        readings = self._readings("a", "b")
        self._answers(mocker, readings, {("a", "b"): 0}, prominence=0.05)

        result = align(readings, {}, ["a", "b"])

        assert result.reference_id is None
        assert result.verdicts["b"].outcome is Outcome.AMBIGUOUS

    def test_a_recording_with_no_sound_is_reported_and_the_rest_still_line_up(
        self, mocker
    ):
        """One of three with no track at all: the two that were read are
        lined up, and the third is named as not having been read."""

        readings = self._readings("a", "c")
        self._answers(mocker, readings, {("a", "c"): 500})

        result = align(readings, {}, ["a", "b", "c"])

        assert result.reference_id == "a"
        assert result.verdicts["b"].outcome is Outcome.NO_SOUND
        assert [alignment.block_id for alignment in result.alignments] == ["c"]
        assert result.alignments[0].shift_ms == 1000

    def test_the_shift_is_against_the_offset_the_recording_already_has(self, mocker):
        """A recording already known to be three seconds ahead does not
        want telling again."""

        readings = self._readings("a", "b")
        self._answers(mocker, readings, {("a", "b"): 500})

        result = align(readings, {"a": 0, "b": 3000}, ["a", "b"])

        assert result.alignments[0].shift_ms == 1000 - 3000

    def test_pairs_that_disagree_are_reported_rather_than_chosen_between(self, mocker):
        """Three answers where two of them do not add up.

        A and B agree, B and C agree, and A and C are two seconds apart
        from what those two say. Any one of the three could be the wrong
        one and there is no telling which, so nothing is moved and the
        disagreement is reported instead.
        """

        readings = self._readings("a", "b", "c")
        self._answers(
            mocker, readings, {("a", "b"): 0, ("a", "c"): 1000, ("b", "c"): 0}
        )

        result = align(readings, {}, ["a", "b", "c"])

        assert result.reference_id == "a"
        assert result.alignments == ()
        assert result.verdicts["b"].outcome is Outcome.CONTRADICTED
        assert result.verdicts["c"].outcome is Outcome.CONTRADICTED
        assert [conflict.residual_ms for conflict in result.conflicts] == [-2000]

    def test_what_the_disagreement_does_not_touch_is_still_lined_up(self, mocker):
        """The same three answers with a fourth recording besides, which
        the disagreement says nothing about."""

        readings = self._readings("a", "b", "c", "d")
        self._answers(
            mocker,
            readings,
            {("a", "b"): 0, ("a", "c"): 1000, ("b", "c"): 0, ("a", "d"): 0},
        )

        result = align(readings, {}, ["a", "b", "c", "d"])

        assert result.verdicts["b"].outcome is Outcome.CONTRADICTED
        assert result.verdicts["c"].outcome is Outcome.CONTRADICTED
        assert result.verdicts["d"].outcome is Outcome.LINED_UP
        assert [alignment.block_id for alignment in result.alignments] == ["d"]

    def test_nothing_that_does_not_add_up_is_reported_where_it_does(self, mocker):
        readings = self._readings("a", "b", "c")
        self._answers(
            mocker, readings, {("a", "b"): 0, ("a", "c"): 1000, ("b", "c"): 1000}
        )

        result = align(readings, {}, ["a", "b", "c"])

        assert result.conflicts == ()
        assert result.verdicts["c"].outcome is Outcome.LINED_UP

    def test_a_recording_that_agreed_with_nothing_the_reference_did_is_named(
        self, mocker
    ):
        """Two pairs of recordings that each agree with each other and not
        with the other pair: the offsets of the two pairs are not on one
        clock, so only the reference's pair can be placed."""

        readings = self._readings("a", "b", "c", "d")
        self._answers(
            mocker, readings, {("a", "b"): 0, ("c", "d"): 0, ("a", "c"): None}
        )

        result = align(readings, {}, ["a", "b", "c", "d"])

        assert result.reference_id == "a"
        assert result.verdicts["c"].outcome is Outcome.UNCONNECTED
        assert [alignment.block_id for alignment in result.alignments] == ["b"]


class TestWhatIsKeptBetweenReadings:
    """Reading a stretch means decoding a recording up to it, which is the
    slow part of lining recordings up and is paid again on every reading
    unless what was read is kept."""

    @staticmethod
    def _reading_into(written, clicks=(0.5,), duration_sec=10):
        """A `fetch` that reads a stretch off a recording by writing one."""

        def fetch(until_ms, dst):
            written.append(until_ms)
            _write_clicks(dst, duration_sec=duration_sec, clicks=clicks)

            return True

        return fetch

    def test_a_stretch_read_once_is_not_read_again(self):
        cache = SnippetCache()
        written = []
        fetch = self._reading_into(written)

        first = cache.envelope("somewhere", 0, 4000, fetch)
        second = cache.envelope("somewhere", 0, 4000, fetch)

        assert written == [4000]
        assert first
        assert second == first

    def test_a_stretch_read_for_a_later_moment_answers_for_an_earlier_one(self):
        """One reading covers every reading before it, which is what makes
        the second press of the button free: the videos move between one
        reading and the next, and where they move to is inside it."""

        cache = SnippetCache()
        written = []
        fetch = self._reading_into(written)

        later = cache.envelope("somewhere", 4000, 2000, fetch)
        earlier = cache.envelope("somewhere", 1000, 2000, fetch)

        assert written == [6000]
        assert later
        assert earlier
        assert len(earlier) == 1000

        # and the same stretch, read afresh, comes to the same thing
        fresh = cache.envelope("somewhere-else", 1000, 2000, self._reading_into([]))

        assert fresh == earlier

    def test_a_stretch_that_came_back_a_breath_short_is_still_that_stretch(self):
        """A transcode stops on a block boundary, so a file written for the
        seventh second of a recording holds a few milliseconds less than
        seven seconds. What a kept stretch answers for is the moment it was
        asked for; matched against what the file really holds, every reading
        would decode the recording all over again -- measured, on a real
        one, at exactly the moment this is asked for."""

        cache = SnippetCache()
        written = []

        def fetch(until_ms, dst):
            written.append(until_ms)
            _write_clicks(dst, duration_sec=int(until_ms / 1000) - 0.004, clicks=(0.5,))

            return True

        first = cache.envelope("short-by-a-hair", 0, 7000, fetch)
        second = cache.envelope("short-by-a-hair", 0, 7000, fetch)

        assert written == [7000]
        assert first
        assert second == first

    def test_asking_for_more_than_was_read_reads_it_again(self):
        cache = SnippetCache()
        written = []
        fetch = self._reading_into(written, duration_sec=30)

        cache.envelope("somewhere", 0, 4000, fetch)
        cache.envelope("somewhere", 5000, 4000, fetch)

        assert written == [4000, 9000]

    def test_a_moment_past_the_end_of_the_recording_is_read_once(self):
        """A recording that ended before the moment asked for holds nothing
        there, and reading it again would find the same nothing."""

        cache = SnippetCache()
        written = []
        fetch = self._reading_into(written, duration_sec=2)

        assert cache.envelope("short", 20000, 2000, fetch) == ()
        assert cache.envelope("short", 20000, 2000, fetch) == ()

        assert written == [22000]

    def test_a_recording_written_to_again_is_not_the_one_that_was_read(self, tmp_path):
        path = tmp_path / "recording.wav"

        _write_clicks(path, duration_sec=2)
        was = file_key(path)

        _write_clicks(path, duration_sec=4, clicks=(0.5, 1.5))

        assert file_key(path) != was

    def test_a_recording_that_is_not_there_has_nothing_to_tell_one_from_another(
        self, tmp_path
    ):
        assert file_key(tmp_path / "absent.wav") is None

    def test_what_is_kept_is_bounded_and_what_falls_out_is_let_go_of(self):
        cache = SnippetCache(max_recordings=2)
        written = []
        kept = []
        fetch = self._reading_into(written)

        def recording_fetch(until_ms, dst):
            kept.append(dst)

            return fetch(until_ms, dst)

        for name in ("one", "two", "three"):
            cache.envelope(name, 0, 1000, recording_fetch)

        assert len(kept) == 3
        assert not kept[0].exists()
        assert kept[1].exists()
        assert kept[2].exists()

        # and the one that fell out is read again, since it is gone
        cache.envelope("one", 0, 1000, recording_fetch)

        assert len(kept) == 4

    def test_a_stretch_that_could_not_be_read_gives_nothing_back(self):
        cache = SnippetCache()

        assert cache.envelope("nowhere", 0, 1000, lambda until_ms, dst: False) == ()

    def test_letting_go_of_everything_leaves_nothing_behind(self):
        cache = SnippetCache()
        kept = []

        def fetch(until_ms, dst):
            kept.append(dst)

            return self._reading_into([])(until_ms, dst)

        cache.envelope("somewhere", 0, 1000, fetch)

        assert kept[0].exists()

        cache.clear()

        assert not kept[0].exists()


# --- reading the sound off a file ---


def _has_vlc() -> bool:
    try:
        import gridplayer.vlc_player.libvlc  # noqa: F401
    except Exception:
        return False

    return True


needs_vlc = pytest.mark.skipif(not _has_vlc(), reason="VLC is not installed")


class TestWhatAPackagedBuildMustCarry:
    """The VLC plugins a release build has to ship for reading a snippet.

    Reading a snippet is a transcode into a file, which asks VLC for more
    than playing a video does, and the release build copies the plugins a
    player needs and no more. Measured against a payload built from the
    release script's own list, with any one of these left out: the probe
    reads nothing at all, and Align By Sound does nothing without saying
    anything about it. Nothing else in the suite would notice, which is why
    the list is checked here rather than trusted.
    """

    SCRIPT = Path(__file__).parent.parent / "scripts" / "pyinstaller" / "build_win.sh"

    @pytest.mark.parametrize(
        ("directory", "plugin"),
        [
            ("audio_output", "libadummy_plugin.dll"),
            ("stream_out", "libstream_out_transcode_plugin.dll"),
            ("stream_out", "libstream_out_standard_plugin.dll"),
            ("mux", "libmux_wav_plugin.dll"),
            ("access_output", "libaccess_output_file_plugin.dll"),
        ],
    )
    def test_the_plugin_is_copied_into_the_build(self, directory, plugin):
        script = self.SCRIPT.read_text(encoding="utf-8")

        assert f"plugins/{directory}/{plugin}" in script


def _write_clicks(path, duration_sec=10, clicks=(1.5, 3.0, 4.5, 6.0)):
    """Silence with a short burst of tone at each moment given.

    A fraction of a second of length is allowed, and rounds down: a file
    that comes back a few milliseconds short of the moment it was written
    for is what a real transcode does.
    """

    samples = array.array("h", bytes(int(duration_sec * SAMPLE_RATE) * 2))

    for at in clicks:
        start = int(at * SAMPLE_RATE)

        for offset in range(SAMPLE_RATE // 20):
            index = start + offset

            if index < len(samples):
                samples[index] = 12000 if offset % 2 else -12000

    _write_wav(path, samples)


def _burst_centres(envelope, relative_to=0.5):
    """Where the loud runs are, in blocks."""

    if not envelope:
        return []

    threshold = max(envelope) * relative_to
    loud = [index for index, value in enumerate(envelope) if value > threshold]

    runs = []

    for index in loud:
        if runs and index - runs[-1][-1] <= 2:
            runs[-1].append(index)
        else:
            runs.append([index])

    return [sum(run) / len(run) for run in runs]


def _write_irregular_bursts(path, duration_sec=40):
    """Bursts at uneven gaps of uneven loudness.

    Uneven on purpose: a regular train matches itself at every multiple of
    its own gap, so a reading taken between two readings could be found a
    gap out and the test would not know.
    """

    samples = array.array("h", bytes(duration_sec * SAMPLE_RATE * 2))

    state = 987654321
    at = 0.4

    while at < duration_sec - 1:
        state = (state * 1103515245 + 12345) % 2147483648
        level = 4000 + (state % 6) * 1500
        start = int(at * SAMPLE_RATE)

        for offset in range(SAMPLE_RATE // 20):
            index = start + offset

            if index < len(samples):
                samples[index] = level if offset % 2 else -level

        state = (state * 1103515245 + 12345) % 2147483648
        at += 0.3 + (state % 90) / 100

    _write_wav(path, samples)


@needs_vlc
class TestTheProbeReadsASnippet:
    def _probe(self):
        from gridplayer.vlc_player.audio_probe import AudioProbe

        return AudioProbe()

    def test_it_gives_back_the_sound_around_the_moment(self, tmp_path):
        """End to end: a file with known bursts in it, read around a known
        moment, must come back with the bursts where they belong."""

        path = tmp_path / "clicks.wav"
        _write_clicks(path, clicks=(1.5, 3.0, 4.5, 6.0))

        probe = self._probe()

        try:
            envelope, origin = probe.envelope(path, center_ms=4000, snippet_ms=6000)
        finally:
            probe.cleanup()

        assert envelope, "the probe gave nothing back"

        # read from the start of the recording, so the stretch begins where
        # it was asked to and not wherever a seek happened to land
        assert origin == 1000

        # the stretch runs 1s..7s, so the bursts fall 0.5s, 2s, 3.5s and 5s
        # into it
        wanted = [round(at * BLOCKS_PER_SEC) for at in (0.5, 2.0, 3.5, 5.0)]
        centres = _burst_centres(envelope)

        assert len(centres) == len(wanted)

        # The gaps between them are exact. A transcode puts about 24ms of
        # its own at the front of what it writes -- measured, and the same
        # before every reading of every file -- and that cancels in a lag
        # taken between two readings, which is the only thing a lag is ever
        # taken between.
        gaps = [later - earlier for earlier, later in pairwise(centres)]
        wanted_gaps = [later - earlier for earlier, later in pairwise(wanted)]

        for gap, want in zip(gaps, wanted_gaps):
            assert abs(gap - want) <= 2, (centres, wanted)

        # and near enough where they were asked for, so a reading taken from
        # somewhere else altogether is still caught
        for centre, want in zip(centres, wanted):
            assert abs(centre - want) <= 20, (centres, wanted)

    def test_a_recording_read_once_is_not_decoded_again(self, tmp_path, mocker):
        """Reading is the slow part of lining recordings up, and lining them
        up is done over and over: read, move, read again. What was read the
        first time is kept, so the second press of the button costs the
        sound being read out of a file rather than decoded."""

        path = tmp_path / "clicks.wav"
        _write_clicks(path)

        probe = self._probe()

        try:
            first, first_origin = probe.envelope(path, center_ms=4000, snippet_ms=6000)

            decoding = mocker.spy(probe, "_write_snippet")
            second, second_origin = probe.envelope(
                path, center_ms=4000, snippet_ms=6000
            )
        finally:
            probe.cleanup()

        assert first
        assert second == first
        assert second_origin == first_origin
        assert decoding.call_count == 0

    def test_two_readings_a_known_distance_apart_are_found_that_far_apart(
        self, tmp_path
    ):
        """The number that matters, measured end to end.

        One recording read around two moments a known distance apart is
        what two recordings out of step look like, so what comes back is
        the error the whole path adds up to -- the reading, the envelope
        and the search together.
        """

        path = tmp_path / "irregular.wav"
        _write_irregular_bursts(path)

        probe = self._probe()

        try:
            first, first_origin = probe.envelope(
                path, center_ms=20000, snippet_ms=20000
            )
            second, second_origin = probe.envelope(
                path, center_ms=20000 - 7537, snippet_ms=20000
            )
        finally:
            probe.cleanup()

        assert first
        assert second

        # both stretches begin where they were asked to begin, which is the
        # whole reason for reading from the start instead of seeking
        assert first_origin == 10000
        assert second_origin == 2463

        lag = best_lag(first, second)

        # the second was read 7537ms earlier in the same recording, so what
        # the first has at its start the second has that much further in.
        # Allowed a block and no more: a block is two milliseconds, and the
        # error over the whole path is under one of them.
        assert lag.offset_shift_ms == pytest.approx(7537, abs=2)
        assert lag.score > 0.8

    def test_a_moment_before_the_recording_starts_is_read_from_its_start(
        self, tmp_path
    ):
        path = tmp_path / "clicks.wav"
        _write_clicks(path, clicks=(0.5,))

        probe = self._probe()

        try:
            envelope, origin = probe.envelope(path, center_ms=0, snippet_ms=6000)
        finally:
            probe.cleanup()

        assert envelope
        assert origin == 0
        assert _burst_centres(envelope)[0] == pytest.approx(
            0.5 * BLOCKS_PER_SEC, abs=20
        )

    def test_a_file_with_no_sound_in_it_has_no_shape_to_match(self, tmp_path):
        path = tmp_path / "silence.wav"
        _write_clicks(path, clicks=())

        probe = self._probe()

        try:
            envelope, _origin = probe.envelope(path, center_ms=4000, snippet_ms=6000)
        finally:
            probe.cleanup()

        # read, but flat: nothing to line anything up against
        assert envelope
        assert best_lag(envelope, envelope) is None

    def test_a_file_that_is_not_there_gives_nothing_back(self, tmp_path):
        probe = self._probe()

        try:
            envelope, _origin = probe.envelope(tmp_path / "absent.wav", center_ms=0)
        finally:
            probe.cleanup()

        assert envelope == ()
