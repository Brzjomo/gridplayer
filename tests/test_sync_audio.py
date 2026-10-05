"""Reading the sound of recordings to find where they line up.

Two halves, checked apart: what the numbers do, which needs no player, and
reading the numbers off a file, which needs one.
"""

import array
import random
import wave
from itertools import pairwise

import pytest

from gridplayer.utils.sync_audio import (
    BLOCKS_PER_SEC,
    MIN_SCORE,
    SAMPLE_RATE,
    Lag,
    best_lag,
    envelope_from_samples,
    envelope_from_wav,
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


# --- reading the sound off a file ---


def _has_vlc() -> bool:
    try:
        import gridplayer.vlc_player.libvlc  # noqa: F401
    except Exception:
        return False

    return True


needs_vlc = pytest.mark.skipif(not _has_vlc(), reason="VLC is not installed")


def _write_clicks(path, duration_sec=10, clicks=(1.5, 3.0, 4.5, 6.0)):
    """Silence with a short burst of tone at each moment given."""

    samples = array.array("h", bytes(duration_sec * SAMPLE_RATE * 2))

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
