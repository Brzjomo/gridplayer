# Audio alignment

How GridPlayer finds where two recordings of the same event line up, by
comparing their sound. This is the machinery behind **Align By Sound** in the
alignment dialog (`gridplayer/dialogs/align_videos.py`).

Read [design-sync-offset.md](design-sync-offset.md) first for what a sync offset
*is* and how it is stored. This document covers only the part that listens.

## The shape of it

```text
align_videos.py        asks for a reading per video, applies what comes back
      │
      ├── utils/sync_align.py      AlignMeasure: a thread, so the window lives
      │
      ├── vlc_player/audio_probe.py   VLC → a wav file on disk
      │
      └── utils/sync_audio.py      pure DSP: samples in, a lag out
```

The split matters: `sync_audio.py` touches neither VLC nor Qt, so everything it
does can be tested without a player. Getting samples off a recording is the only
part that needs VLC.

`align_videos.py` hands the readings to `sync_audio.align()`, which compares
**every pair** of them and returns one `AlignResult`: the shifts to apply, a
`Verdict` per video, and any `Conflict` between the pairs. Nothing it decides is
a matter of taste, so the tests for it need no player at all: the pairs are
handed to it ready-made (see `TestSeveralReadingsAtOnce`).

## Three decisions that are not obvious

### 1. The snippet is read from the start of the file, never sought to

`AudioProbe.envelope()` asks VLC for the audio from the beginning of the
recording, stopping at the end of the window it wants, and then reads only the
window out of the wav. It does **not** use `:start-time`.

Seeking a **video container** lands on the nearest point the container is able
to begin at, and the sound then starts as much as a second away from the moment
asked for. Measured on a file with a keyframe every two seconds:

| Asked to start at | Sound actually began |
| --- | --- |
| 20000 ms | +399 ms late |
| 25000 ms | **−1112 ms early** |
| 18000 ms | −32 ms late |

The amount differs per file, varies with position, and **VLC reports nothing
that can be used to correct for it** (`get_time()` returns −1 throughout such a
transcode). Worse, a reading taken this way correlates at **1.000** — the search
finds the right content, and only the origin is wrong, so no confidence
threshold can catch it.

Reading from the start makes the origin exactly the start of the recording, so
the error is zero by construction. It costs the time to decode everything before
the window, which runs at roughly **300× the length of the audio** (40 s of
audio through VLC in 0.12–0.14 s, `acodec=s16le`, 8 kHz mono), so an hour-long
recording read to the half-hour mark takes about 6 seconds.

### 2. What gets compared is the shape of the sound, not its loudness

Two cameras at one event each set their own level: one is nearer, one is behind
something, one simply recorded louder. The loudness across seconds is therefore
the part of two such recordings that agrees **least** — and it is most of what a
plain RMS envelope contains. Two recordings of the same moment come out looking
nearly unrelated.

What they agree on is *when the sound changed*. `_shaped()` subtracts a moving
average over `SHAPE_SPAN_SEC` (half a second), which leaves that and takes out
the swell.

Measured on two recordings of one event with independent automatic gain:

| Judged on | Score at the correct lag |
| --- | --- |
| loudness alone | 0.341 — thrown away |
| the shape of the sound | 0.667 — acted on |

The lag itself was found exactly either way. It is the number that decides
*whether to act* that loudness alone fails.

### 3. Setting an offset does not move a video

An offset says **which moment of that recording** a common moment falls on. When
one is set, `VideoBlock` deliberately keeps the video on the moment it was
already showing (`p − o` is preserved). So lining the *offsets* up is not the
same as lining the *videos* up: on their own, a corrected set of offsets leaves
the recordings described rightly and the videos no closer together than they
were.

`AlignVideosDialog._measured()` therefore finishes by seeking every video onto
the reference's moment (`sync_offset_align_others`). Without that step the
button appears to do nothing audible until the next play.

### 4. Every pair is read, not each against one recording

The reference is not the first video in the grid. Every pair of readings is
compared, and the video the most of the others agree with becomes the reference
(`_reference_of`); with three recordings that is usually the middle one, and a
recording that heard nothing the first one heard is no longer able to spoil the
other two's answer.

The pairs are also a check on each other: three readings give three answers, and
the deltas `d_ab`, `d_bc`, `d_ac` have to satisfy `d_ac = d_ab + d_bc`. Each
video is placed along the fewest pairs that connect it to the reference, and
every reliable pair is then compared against that placement. A pair that
disagrees by more than `CONFLICT_MS` (300 ms) is a **conflict**: the pairs the
conflict touches are dropped, anything that was only placed through them is left
where it is, and the disagreement is reported rather than resolved by picking
one of the two. 300 ms is above the ~128 ms an encoder contributes and far below
the seconds a wrong peak is out by.

## The arithmetic

For each video `i` compared against the reference `ref`:

```text
δ_i = (origin_i − origin_ref) + L_i          # how much later the same sound is in i
o_i' = o_ref + δ_i                            # the offset that makes the recordings agree
```

* `L_i` is the lag `best_lag()` found between the two envelopes, in ms.
* `origin_i` is where the reading of `i` began **in its own recording** — known
  exactly, because nothing is sought (decision 1). Dropping this term is the bug
  described above; it is what `Reading.origin_ms` exists for.
* `o` are the offsets already in hand. They are subtracted because a recording
  already known to be a second ahead does not want telling again.

## The search

`best_lag()` looks for the lag in two passes, because the range worth searching
(±12 s at 2 ms a block is 24000 positions × 15000 blocks) is nothing like the
fineness worth having.

| Pass | Compares | Finds |
| --- | --- | --- |
| rough | envelopes pooled `COARSE_DECIMATION`:1, whole range | the lag to ±40 ms |
| close | full resolution, ±`FINE_REACH_BLOCKS` around the rough answer, over `FINE_SPAN_BLOCKS` | the lag to 2 ms |

Pooling is kept deliberately shallow (20:1). Pool harder and a stretch of short
sounds — claps, footsteps, a door — averages into nearly one value, with no
shape left to match and nothing to stop an unrelated moment scoring as well as
the right one. This was measured, not guessed: at 100:1 a known lag of 25 blocks
was reported as 5300.

The score reported is then taken over the **whole overlap** at the chosen lag,
not over the short stretch the timing was taken from. Four seconds of a quiet
room says nothing about whether two recordings are the same, and a lag settled
on such a stretch would be thrown away for the number it scored.

## How sure the answer is

The score says how well the two agree at the lag chosen; it cannot say whether
anything else agreed nearly as well, and what a high score is worth depends on
the material — measured on two recordings of one event the right lag scored
0.667, and 0.3 on a quieter pair of the same event means just as much. So the
decision to act is not made on the score alone.

`_prominence()` compares the answer with the best peak at least
`PEAK_SEPARATION_SEC` (1 s) away from it, as a share of the answer's own score.
Below `MIN_PROMINENCE` (0.25, i.e. the answer has to stand a quarter above its
best distant rival) the lag is reported as `AMBIGUOUS` and nothing is moved. A
sound that comes round again — a train of claps, an alarm, anything periodic —
is the case this catches: every repetition matches as well as the one the search
settled on.

The rival is measured **the same way the answer was**, at full resolution rather
than pooled. Pooling flattens a stretch of short sounds into nearly one value,
so a rival's height relative to the answer depends on how much of it survives
the pooling, and a rival measured while pooled can look far closer to the answer
than it is. Measured on the fixture in `TestHowSureTheAnswerIs`: the same known
lag scores 1.00 with prominence 0.96 when the samples are compared at the same
resolution, and prominence 0.14 when the rival is compared pooled.

Where the score is weakest is at the ends of the range, where the two readings
share least: two unrelated recordings of a room that never falls silent scored
0.19 over the whole range on a synthetic fixture, but two unrelated recordings
of a quieter kind reached 0.5 — what a moving average over half a second leaves
of a signal that is mostly silent is the half-second at each end of the reading,
where that average is taken over a shorter window than anywhere else. Both are
synthetic fixtures, not real recordings; the claim `MIN_SCORE` rests on (0.341
for loudness, 0.667 for shape) is from real ones.

## Constants

All in `utils/sync_audio.py` unless noted.

| Name | Value | Why |
| --- | --- | --- |
| `SAMPLE_RATE` | 8000 | enough to see a clap by, and cheap in pure Python |
| `BLOCKS_PER_SEC` | 500 (2 ms) | a twentieth of a second is heard as two of the same sound |
| `MIN_OVERLAP_SEC` | 1.0 | less than this and one loud block decides the answer |
| `MAX_LAG_SEC` | 12.0 | the range that makes this worth doing at all |
| `COARSE_DECIMATION` | 20 | see above — shallow on purpose |
| `RIVAL_CANDIDATES` | 8 | how many of the rough search's other favourites are measured again at full resolution |
| `FINE_REACH_BLOCKS` | 60 | ±120 ms, three coarse blocks either way |
| `FINE_SPAN_BLOCKS` | 2000 (4 s) | enough to place a sound, short enough to stay quick |
| `SHAPE_SPAN_SEC` | 0.5 | the swell taken out before comparing |
| `MIN_SCORE` | 0.3 | on shaped sound; unrelated sound scores near zero |
| `MIN_PROMINENCE` | 0.25 | a quarter above the best distant rival, or the answer is not acted on |
| `PEAK_SEPARATION_SEC` | 1.0 | a rival nearer than this is the shoulder of the answer |
| `CONFLICT_MS` | 300 | how far two pairs may disagree about one recording; above the encoder, far below a wrong peak |
| `KEPT_RECORDINGS` | 3 | stretches kept between readings; one per recording is all that is wanted |
| `SNIPPET_MS` (`audio_probe.py`) | 30000 | caps the range: two windows must still overlap |

## Why there is no numpy

Deliberate. The 32-bit Windows build cannot have it (numpy publishes no 32-bit
wheels), and that build is already shipped without other optional dependencies.
Everything here is pure Python, which is why the rate is 8 kHz and the passes are
coarse-to-fine rather than a full-resolution search or an FFT.

## Cost, and what is kept

| Step | Cost |
| --- | --- |
| reading one video, first time | ~position ÷ 300, i.e. ~6 s per hour of position |
| reading one video, after that | reading the snippet out of a wav: milliseconds |
| correlation, per pair | ~0.3 s |
| disk, per recording | position × 16 KB/s, held for the process (`SnippetCache`) |

The reading is the dominant cost and grows with how far into the recording the
moment is, so what was read is kept: `SnippetCache` holds, per recording, the
sound from its start up to the deepest moment read so far, and answers every
earlier request out of it. Lining up by sound is read-correct-look-read again,
and the videos move between one reading and the next, so the second press of
**Align By Sound** wants a different moment of the same recordings — which the
stretch already read covers. A recording whose file changed (`file_key`: path,
mtime, size) is read again, at most `KEPT_RECORDINGS` are held, and the files
live in a temporary directory removed when the process ends.

## Testing this

`tests/test_sync_audio.py` holds both halves: the pure DSP (`needs_vlc`-free)
and the end-to-end probe (`needs_vlc`). What is kept between readings is tested
without a player, by handing `SnippetCache.envelope()` a `fetch` that writes a
wav of its own: which readings cost a decode and which do not is a property of
the cache and not of VLC.

Five traps this code has already fallen into, all of them worth knowing before
touching the tests:

1. **Test signals must be aperiodic.** A pattern that repeats matches itself just
   as well one period out, and the search picks whichever it reaches first. Both
   `_pattern` and the continuous `_source` were written with a short period
   (`index % 37`, `math.sin(index / 11)`) and both had to be replaced with an
   LCG. A test that cannot tell a lag from a lag-plus-a-period is not testing the
   search.
2. **One generator's seeds are not two recordings.** The LCG's low bits run
   together from one seed to the next, so two "unrelated" signals built from
   `state % 1000` with two seeds behave like two recordings of the same thing.
   `_noise` uses `random.Random` for that reason. It is also dense on purpose:
   see the note on where the score is weakest, above.
3. **An unrelated fixture can agree by construction.** `_own_gain` steps its
   level every `hold` blocks for every seed, so two of its seeds step at the
   same moments and their shaped versions correlate at 0.85 while being nothing
   to do with each other. Randomise when the level changes as well as the level.
4. **Audio-only files hide the container behaviour.** A wav seeks exactly (error
   measured at +1 ms), so a wav-only end-to-end test passes while the video path
   is out by a second. The end-to-end tests that matter use a **video container
   with an audio track**.
5. **Do not assert what the fixture cannot show.** One draft asserted that
   loudness alone scores below the threshold, on a synthetic fixture where it
   scores 0.70. The claim was true of real recordings (0.34) and false of the
   fixture; the assertion was removed rather than the fixture bent to fit it.

Two end-to-end properties are worth keeping pinned, because both were bugs:

* A reading reports the origin it was asked for, and the correction computed for
  a file measured **against itself is zero** at any pair of positions.
* `Align By Sound` seeks the videos afterwards, not merely the offsets.

A third was found by the tests above and is worth keeping pinned as well:
readings are not always the same length, and the overlap at a lag is worked out
from the reference's length rather than the other's.
