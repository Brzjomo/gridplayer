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

## Constants

All in `utils/sync_audio.py` unless noted.

| Name | Value | Why |
| --- | --- | --- |
| `SAMPLE_RATE` | 8000 | enough to see a clap by, and cheap in pure Python |
| `BLOCKS_PER_SEC` | 500 (2 ms) | a twentieth of a second is heard as two of the same sound |
| `MIN_OVERLAP_SEC` | 1.0 | less than this and one loud block decides the answer |
| `MAX_LAG_SEC` | 12.0 | the range that makes this worth doing at all |
| `COARSE_DECIMATION` | 20 | see above — shallow on purpose |
| `FINE_REACH_BLOCKS` | 60 | ±120 ms, three coarse blocks either way |
| `FINE_SPAN_BLOCKS` | 2000 (4 s) | enough to place a sound, short enough to stay quick |
| `SHAPE_SPAN_SEC` | 0.5 | the swell taken out before comparing |
| `MIN_SCORE` | 0.3 | on shaped sound; unrelated sound scores near zero |
| `SNIPPET_MS` (`audio_probe.py`) | 30000 | caps the range: two windows must still overlap |

## Why there is no numpy

Deliberate. The 32-bit Windows build cannot have it (numpy publishes no 32-bit
wheels), and that build is already shipped without other optional dependencies.
Everything here is pure Python, which is why the rate is 8 kHz and the passes are
coarse-to-fine rather than a full-resolution search or an FFT.

## Cost

| Step | Cost |
| --- | --- |
| reading one video | ~position ÷ 300, i.e. ~6 s per hour of position |
| correlation, per pair | ~0.3 s |
| disk, per reading | position × 16 KB/s, transient (a `TemporaryDirectory`) |

The reading is the dominant cost and grows with how far into the recording the
moment is. Envelopes are not cached between runs; a second **Align By Sound**
re-reads everything. See [roadmap.md](roadmap.md).

## Testing this

`tests/test_sync_audio.py` holds both halves: the pure DSP (`needs_vlc`-free)
and the end-to-end probe (`needs_vlc`).

Three traps this code has already fallen into, all of them worth knowing before
touching the tests:

1. **Test signals must be aperiodic.** A pattern that repeats matches itself just
   as well one period out, and the search picks whichever it reaches first. Both
   `_pattern` and the continuous `_source` were written with a short period
   (`index % 37`, `math.sin(index / 11)`) and both had to be replaced with an
   LCG. A test that cannot tell a lag from a lag-plus-a-period is not testing the
   search.
2. **Audio-only files hide the container behaviour.** A wav seeks exactly (error
   measured at +1 ms), so a wav-only end-to-end test passes while the video path
   is out by a second. The end-to-end tests that matter use a **video container
   with an audio track**.
3. **Do not assert what the fixture cannot show.** One draft asserted that
   loudness alone scores below the threshold, on a synthetic fixture where it
   scores 0.70. The claim was true of real recordings (0.34) and false of the
   fixture; the assertion was removed rather than the fixture bent to fit it.

Two end-to-end properties are worth keeping pinned, because both were bugs:

* A reading reports the origin it was asked for, and the correction computed for
  a file measured **against itself is zero** at any pair of positions.
* `Align By Sound` seeks the videos afterwards, not merely the offsets.
