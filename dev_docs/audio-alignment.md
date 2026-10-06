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
      ├── utils/sync_align.py      AlignMeasure: a thread per recording, so the
      │                            window lives, and progress as each arrives
      │
      ├── vlc_player/audio_probe.py   VLC → a wav file on disk
      │
      └── utils/sync_audio.py      pure DSP: samples in, a lag out, at one of
                                   two scales (see *Two passes* below)
```

The split matters: `sync_audio.py` touches neither VLC nor Qt, so everything it
does can be tested without a player. Getting samples off a recording is the only
part that needs VLC.

`align_videos.py` hands the readings to `sync_audio.align()`, which compares
**every pair** of them and returns one `AlignResult`: the shifts to apply, a
`Verdict` per video, and any `Conflict` between the pairs. Nothing it decides is
a matter of taste, so the tests for it need no player at all: the pairs are
handed to it ready-made (see `TestSeveralReadingsAtOnce`).

## Two passes, at two scales

A grid of recordings of one event is usually minutes out before anything has
been done about it, and a snippet read either side of the moment cannot see any
part of that: two sixty-second readings five minutes apart share nothing at all,
and no search over them can answer. So **Align By Sound** reads every file
twice, and acts on the first reading before taking the second.

| Pass | Reads | Compares | Finds |
| --- | --- | --- | --- |
| wide | minutes of every recording, at a block a second | the whole range, ±10 minutes | the lag to about a second |
| close | a 60 s snippet of every recording, at 2 ms a block | ±30 s | the lag to 2 ms |

Both readings are taken **around the video's own position**, and a reading is
placed on the shared clock by the offset that video holds
(`Sound.start_ms = origin_ms − offset_ms`). That is what makes the two windows
overlap at all: in Sync Offset mode every seek carries the other videos to the
same moment of the clock the offsets describe, so all the readings begin at the
same place on that clock however far apart the recordings actually are. A
misalignment of `e` therefore lands inside a window of half-width `H` as long as
`|e| ≤ H`, which is what `COARSE_MAX_LAG_SEC` and `WIDE_READ_MS` are for.

**The two passes cost one decode between them.** A wide reading is a stretch of
a recording read from its start up to `position + H`, which is exactly what the
close reading then wants a snippet of -- around the position the wide pass has
just moved the video to, which can be a whole range further in. `SnippetCache`
keeps what was decoded and the close reading is taken out of it, so the second
pass costs a reading and no decoding at all: see
`test_the_snippet_the_close_pass_reads_costs_no_second_decode`.

Everything the search does that depends on how long a block is lives in one
`Scale` (`sync_audio.scale`), and `best_lag`, `measure_pairs` and `align` take
one: `FINE_SCALE` is the two-millisecond one described below, and
`COARSE_SCALE` is the block-a-second one. Measured over 64 synthetic pairs of
one event an hour long, shifted by 0, ±5, ±61, ±300, ±450 and ±599 seconds, the
coarse search put the close pass on the right place 64 times out of 64, and
never acted on a wrong answer. Where it did settle on the wrong place at all it
did so at a score of 0.02 to 0.11 -- a wrong peak scores near nothing over the
whole of a long overlap -- so the answer is thrown away rather than acted on.
That is the property worth having: a wrong answer that scores like a right one
would move videos by minutes on no evidence.

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

The wide reading is the same rule at a larger size -- from the start of the
recording to `position + H` -- and it is the same decode the close pass then
reads its snippet out of. Seeking would be cheaper by a wide margin and is not
done for the same reason: a second of origin error is nothing to a search over
minutes of sound and everything to one that places a lag to the millisecond.

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
(±30 s at 2 ms a block is 30000 positions × 30000 blocks) is nothing like the
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

**A middling score means something of its own.** Two recordings of one event
score at or near the top of the range; two unrelated ones score near nothing.
In between -- the 0.5 to 0.7 a pair of *partly* related recordings comes to --
is the case where they agree over part of what they share and not the rest, and
there is a common way for that to happen: **one of them holds something the
other does not**. A recording with a minute of something added in the middle
does not differ from the original by any one offset at all -- before the
addition they are together, after it they are a minute apart -- so at the lag
that lines the first half up the second half is a minute out, and the score over
the whole overlap is pulled towards the middle. Measured on two such files: the
wide pass came to 0.58 where a pair of recordings of one event comes to 1.00.

That is not a wrong answer so much as an answer to a different question, and it
is why the dialog reads the sound **around the moment the videos are on**: what
it can line up is the part of the pair the viewer is looking at, and a minute of
snippet either side of that moment is what says which part that is. Paused in
the shared half, the close pass finds them lined up to the millisecond (1.00);
paused in the half that was added to, it finds the section's worth of lag
instead and lines *that* half up (measured on the two files above: a section of
30 s, found at 30 s, score 1.00). One number per recording cannot describe the
pair, so what comes of this is that the half being watched is right and the
other half is a section out -- and the middling score the wide pass reported is
the honest sign of it.

## Constants

The fine scale's numbers are in `utils/sync_audio.py`, and most of them are in
`FINE_SCALE` (a `Scale` holds everything the search needs that depends on how
long a block is). The coarse scale's are in `COARSE_SCALE`, beside it.

| Name | Value | Why |
| --- | --- | --- |
| `SAMPLE_RATE` | 8000 | enough to see a clap by, and cheap in pure Python |
| `FINE_SCALE.blocks_per_sec` (`BLOCKS_PER_SEC`) | 500 (2 ms) | a twentieth of a second is heard as two of the same sound |
| `FINE_SCALE.min_overlap_sec` (`MIN_OVERLAP_SEC`) | 1.0 | less than this and one loud block decides the answer |
| `FINE_SCALE.max_lag_sec` (`MAX_LAG_SEC`) | 30.0 | half a snippet: as far as one reading can move against the other and still have half of them to compare |
| `FINE_SCALE.shape_span_sec` (`SHAPE_SPAN_SEC`) | 0.5 | the swell taken out before comparing |
| `FINE_SCALE.peak_separation_sec` (`PEAK_SEPARATION_SEC`) | 1.0 | a rival nearer than this is the shoulder of the answer |
| `FINE_SCALE.fine_span_sec` (`FINE_SPAN_SEC`) | 4.0 | enough to place a sound, short enough to stay quick |
| `FINE_SCALE.fine_reach_sec` (`FINE_REACH_SEC`) | 0.12 | ±120 ms, three coarse blocks either way |
| `FINE_SCALE.coarse_decimation` (`COARSE_DECIMATION`) | 20 | see above — shallow on purpose |
| `FINE_SCALE.conflict_ms` (`CONFLICT_MS`) | 300 | how far two pairs may disagree about one recording; above the encoder, far below a wrong peak |
| `MIN_SCORE` / `FINE_SCALE.min_score` | 0.3 | on shaped sound; unrelated sound scores near zero |
| `MIN_PROMINENCE` / `FINE_SCALE.min_prominence` | 0.25 | a quarter above the best distant rival, or the answer is not acted on |
| `RIVAL_CANDIDATES` | 8 | how many of the rough search's other favourites are measured again at full resolution |
| `COARSE_SCALE.blocks_per_sec` | 1 (1 s) | as coarse as a search over minutes can be and still place anything |
| `COARSE_SCALE.max_lag_sec` (`COARSE_MAX_LAG_SEC`) | 600.0 | ten minutes either way: what a grid of one event is usually out by |
| `COARSE_SCALE.min_overlap_sec` | 60.0 | an hour is being compared; a second of it either side of a match is nothing |
| `COARSE_SCALE.shape_span_sec` | 10.0 | the same swell at this size |
| `COARSE_SCALE.peak_separation_sec` | 30.0 | half a minute apart is two peaks at a block a second |
| `COARSE_SCALE.fine_span_sec` | 1200.0 | what the close look compares at this size |
| `COARSE_SCALE.fine_reach_sec` | 60.0 | how far from the rough answer the close look goes |
| `COARSE_SCALE.coarse_decimation` | 5 | **not** the fine scale's twenty: see *Two passes* and the note on `COARSE_SCALE` |
| `COARSE_SCALE.conflict_ms` | 5000 | the encoder is a block or two at this size; a pair out by more has settled on another peak |
| `FINE_SNIPPET_MS` | 60000 | what the close pass reads, and so what its range is half of: a section added to a recording is lined up on either side of the addition, not only on the side the wide pass settled on |
| `WIDE_READ_MS` | 1 265 000 | the coarse range either side of the moment, plus the snippet, so one decode answers both passes |
| `MAX_BLOCK_SAMPLES` | 2000 | how many samples of a block are measured where a block is seconds long |
| `KEPT_RECORDINGS` | 3 | stretches kept between readings; one per recording is all that is wanted |

## Why there is no numpy

Deliberate. The 32-bit Windows build cannot have it (numpy publishes no 32-bit
wheels), and that build is already shipped without other optional dependencies.
Everything here is pure Python, which is why the rate is 8 kHz and the passes are
coarse-to-fine rather than a full-resolution search or an FFT.

## Cost, and what is kept

| Step | Cost |
| --- | --- |
| reading one video, first time | ~(position + range) ÷ 300, i.e. ~6 s per hour of position, plus ~2 s for the ten minutes either way |
| reading one video, after that | reading the snippet out of a wav: milliseconds |
| correlation, per pair | ~0.7 s at the fine scale, ~0.03 s at the coarse one |
| envelopes | ~0.9 s for a quarter of an hour at 2 ms blocks, ~0.1 s at a block a second (`MAX_BLOCK_SAMPLES`) |
| disk, per recording | position × 16 KB/s, held for the process (`SnippetCache`) |

**Readings run one to a recording at a time.** `AlignMeasure` starts a thread per
recording, so a grid of three pays for the slowest of them rather than for all
three -- which is what makes reading minutes of a five-hour recording bearable at
all. `SnippetCache` gives each recording a lock of its own for the whole of a
reading of it: readings of different recordings go on at once, and two readings
of one recording are kept to one decode. What is kept is reached by several
threads at a time, so the locks around it are held for no longer than the look at
what is there; the decoding itself happens outside them.

The reading is the dominant cost and grows with how far into the recording the
moment is, so what was read is kept: `SnippetCache` holds, per recording, the
sound from its start up to the deepest moment read so far, and answers every
earlier request out of it. Lining up by sound is read-correct-look-read again,
and the videos move between one reading and the next, so the second press of
**Align By Sound** wants a different moment of the same recordings — which the
stretch already read covers. The wide pass is what makes the stretch deep, and
the close pass is what reads the snippet out of it. A recording whose file
changed (`file_key`: path, mtime, size) is read again, at most
`KEPT_RECORDINGS` are held, and the files live in a temporary directory removed
when the process ends.

## What the dialog draws of it

`widgets/sync_strip.py`. The readings are what the strips are for, and there are
two kinds of them:

* **One strip above everything else**, with every video's sound drawn over every
  other's, each in its own colour and through itself, and the video being worked
  on drawn last and a little more solidly. This is the only place two recordings
  are seen against each other, and it is where an offset that is a little out is
  seen to be a little out.
* **One strip under each row**, drawing that video's own sound and nothing else.
  A row carrying another recording's shape as well would be two things to read at
  once in the room for one, and what the other recordings are doing is the
  overview's to say.

They share one window -- the dialog owns it and hands it to all of them -- and
one zero: the moment the readings were taken around, which is where the videos
were when their sound was read and stays put while the offsets are tuned under
it. A drag along the row of the video being worked on moves that video's sound
and nothing else; along any other row it moves the window, which is what looking
further along is. Double-clicking a strip is what gives that video's sound its
colour, which is kept against the file rather than the playlist
(`models/spectrum_colors.py`).

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
readings are not always the same length, and the overlap at a lag is bounded by
**both** of them — the reference's length, and what is left of the other past
the lag. What it is not is the reference's length less the lag: that is the same
number while the two readings are of one length, which is what a snippet against
a snippet is, and the wide pass's readings are never of one length (the shorter
recording gives the shorter reading). See the trap below.

A fourth is the whole two-pass feature, and it is pinned end to end rather than
in pieces: `TestTwoRecordingsThatBeganAMinuteApart` writes two files that are
cuts of one event a minute apart, reads them the way the dialog does -- wide
first, then a snippet out of what the wide read already decoded -- and asserts
that the minute comes back exactly and that the close pass then has nothing left
to move. A minute is what the fine scale cannot see any part of, so a regression
that broke the wide pass or the reading of the snippet out of it would show up
here and nowhere else.

Two more traps came out of this work, both of them in the search rather than in
the fixtures:

* **Pooling twenty blocks at the coarse scale is too much.** Twenty blocks at a
  block a second is twenty seconds of sound averaged into one, which flattens a
  stretch of short sounds into nearly one value, and the rough pass then guides
  the close one from a peak nowhere near the answer. At the fine scale twenty
  blocks is forty milliseconds and pooling that much is the right thing; the
  scales do not share the number. Measured over the 64 pairs above: pooled
  twenty, 60 of them were placed rightly and 4 were thrown away; pooled five,
  64 and none.
* **A close look whose window cannot move does not look at negative lags.**
  `_fine_lag` compares a stretch of what the two readings share at that lag. It
  used to take a window from the middle of one reading and look for it in the
  other, which can only be done where the window *fits* -- and a reading barely
  longer than the window (the wide pass's shape: minutes of a recording against
  a window of minutes) has nowhere to put it but where it already is, so every
  negative lag was skipped and the answer came back as no lag at all. Four
  seconds of a snippet against a snippet has all the room in the world, which is
  why the fine scale never showed it.

A third came out of the same work, and it is the one that cost a user their
pairs of short files:

* **The overlap at a lag is bounded by both readings, not by the reference's
  length less the lag.** `_overlap` used to work the share out as
  `reference[:len(reference) - lag]` against `other[lag:]`, which is exactly
  right while the two readings are the same length -- and the fine pass's
  always are, both being the snippet that was asked for. A wide reading is not:
  two minutes of a recording against the same two minutes with something added
  in front gives 119 blocks against 219. At the lag that lines them up (100 s)
  the old sum leaves 19 blocks of a 120-second recording, below the minute of
  shared sound a lag is believed on, so **every lag past about
  `min_overlap_sec` was left unevaluated** and both passes came back saying
  nothing was alike. Measured on those files, with the fix and without:

  | Inserted at the front | Before | After |
  | --- | --- | --- |
  | 40 s | found, 1.00 | found, 1.00 |
  | 60 s | found a second out, 0.47 (the last second was rescued by the close pass) | found, 1.00 |
  | 80 s | **thrown away, 0.28** | found, 1.00 |
  | 100 s | **thrown away, 0.29** | found, 1.00 |

  `TestReadingsOfDifferentLengths` now pins all four. The lesson is the general
  one: a bound that is right for one size of reading is not right for the other,
  and the two scales read the same two files at sizes that differ by three
  orders of magnitude.

