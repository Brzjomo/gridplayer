# Playback and VLC

How a decoded frame gets on screen, and how the child decoder processes work.

## The layers

```text
VideoBlock                    widgets/video_block.py        one per video, UI + policy
  └── VideoFrameVLC*          widgets/video_frame_vlc_*.py  render surface + driver handle
        └── VLCVideoDriver*   vlc_player/video_driver_base*.py
              └── VlcPlayerBase   vlc_player/player_base.py    ALL libVLC calls live here
                    └── vlc         vlc_player/vlc.py          VENDORED python-vlc binding
```

The important structural rule: **`VlcPlayerBase` is the only class that calls
libVLC.** Everything above it works with plain Python data (`ViewParams`,
`MediaInput`, `Media`, `Chapter`). If you need a libVLC capability that is not
exposed, add a method to `VlcPlayerBase` and a thin pass-through on the driver
and frame — do not reach for a media player from a widget.

Data crosses those layers as dataclasses, not raw VLC handles:

| Type | File | Carries |
| --- | --- | --- |
| `MediaInput` | `vlc_player/static.py` | A `Video` model plus the resolved URIs to open. |
| `Media` | `vlc_player/static.py` | What the player found after opening: track lists, dimensions, chapters, duration. |
| `ViewParams` | `params/static.py:106` | aspect, scale, crop, anchor, shift, `is_shift_past_edges`. |
| `VideoTrack` / `AudioTrack` / `SubtitleTrack` | `vlc_player/static.py` | Per-track metadata incl. `language`, `description`, `codec_info`, `video_dimensions`, `orientation`, `fps`. |

`VideoBlock` keeps `video_params: Video` (the *desired* state, persisted into
playlists) separate from the frame's live `media` (the *actual* state reported
by VLC). A large share of `video_block.py` is the code that reconciles one with
the other — see `_apply_wanted_audio_track`, `_apply_wanted_subtitle_track`,
`restore_audio_selection`.

## The five video drivers

Selected by the `player/video_driver` setting through
`VideoDriverManager.video_driver()` (`player/managers/video_driver.py:43`).

| Setting value | Frame class | Process? | Decoding | Rendering |
| --- | --- | --- | --- | --- |
| `vlc_hw` (default) | `VideoFrameVLCHW` | yes, pooled | GPU | VLC draws into a **native child window** (`set_xwindow` / `set_hwnd` / `set_nsobject`). |
| `vlc_hw_sp` | `VideoFrameVLCHWSP` | no, in-process | GPU | same native-window approach, inside the main process. **The only hardware mode on macOS.** |
| `vlc_sw` | `VideoFrameVLCSW` | yes, pooled | CPU | VLC writes frames to **shared memory**; Qt paints them. |
| `vlc_sw_sp` | `VideoFrameVLCSWSP` | no, in-process | CPU | shared-memory path, in-process. |
| `dummy` | `VideoFrameDummy` | no | — | Test/placeholder surface. |

`_multiprocess_drivers = {VLC_SW, VLC_HW}` and `_process_instances` maps each to
its `InstanceProcess*` class. The non-`_sp` modes get a `ProcessManagerVLC`
bound with `functools.partial`, so the frame class receives
`process_manager=` at construction.

Two hard constraints encoded here:

* **macOS is forced to `vlc_hw_sp`.** `video_driver()` rewrites the setting in
  place (`video_driver.py:46`) because one process cannot render into another's
  window on macOS. `VideoFrameVLCHW.ui_video_surface` raises `NotImplementedError`
  on macOS for the same reason.
* **Linux defaults to the `xcb` Qt platform.** Hardware video needs an X11
  drawable; `_detect_qt_platform()` (`params/env.py:26`) picks `wayland` only
  when there is no `DISPLAY`. `init_app_env` also sets
  `QT_XCB_GL_INTEGRATION=none` on Linux (`main/init_app_env.py:30`), because a
  forked decoder inheriting a GL dispatch from the xcb plugin aborts on the
  second hardware process.

`VideoDriverManager.cleanup()` drops the process manager; it is wired to
`video_blocks.reload_all_closed` in `player/player.py:94`, so reloading the
videos tears the decoder processes down and starts fresh ones.

## The multiprocess protocol

`CommandLoop` (`multiprocess/command_loop.py`) is a tiny RPC over a
`multiprocessing.Pipe`. The wire format is a `(command_name, args)` tuple.

* The **parent** owns `ProcessManager` (`multiprocess/process_manager.py`), which
  runs its own `CommandLoopThreaded` on a background thread, starts instances on
  demand, and reaps dead ones in `_reap_dead_instances`.
* Each **child** is an `InstanceProcess` (`multiprocess/instance_process.py`)
  that hosts up to `player/video_driver_players` players (default 4) sharing one
  `vlc.Instance`.
* `cmd_process_command` (`command_loop.py:48`) dispatches by
  `getattr(self, command_name)` — **the command name is the method name on the
  receiving object.** Adding a remote call means adding a method to both sides
  with the same name.
* `cmd_child_pipe()` returns the pair of pipe ends so the parent can hand its
  half to the child at construction.
* `__getstate__`/`__setstate__` deliberately drop the locks so the object can be
  pickled to the child, which builds its own.
* One pipe carries messages from two threads (the command thread and VLC's event
  thread), hence `_pipe_lock` around every send.

**Instance reuse is by VLC options.** `_get_available_instance` only reuses an
instance whose `options` compare equal and whose `player_count` is under the
limit — videos needing different VLC options get separate processes.

### Lifetime and orphan prevention

Two independent guards, deliberately redundant:

* **Parent side:** `create_kill_on_close_job()` (`multiprocess/job_object.py`)
  creates a Windows job object with kill-on-close; each child PID is assigned to
  it. `ProcessManager.cleanup()` waits up to 5s, then calls
  `force_terminate_children()`, then closes the job.
* **Child side:** `InstanceProcess._guard_against_orphaning` and
  `multiprocess/parent_death_signal.py` — the child watches the parent itself, so
  a parent killed without any cleanup still does not leave a decoder running.

`ProcessManagerVLC.create_instance` reads `logging/log_level_vlc` and
`logging/log_level` **in the parent** and passes them down; on `spawn` start
methods a `QueueListenerRoot` forwards child log records back to the parent's
handlers (`process_manager.py:60`). On `fork` the child inherits the log config
directly and no queue is created.

## The video load state machine

`VlcPlayerBase.load_video` (`vlc_player/player_base.py:530`) is a staged
sequence, not a single call. Each stage has a `loopback_*` counterpart so the
in-process (threaded) player can drive it from its own thread, and a real one
for the process player:

| Stage | In-process (`loopback_*`) | What it does |
| --- | --- | --- |
| 2 | `loopback_load_video_st2_set_media` / `load_video_st2_set_media` | Build `vlc.Media` from the input URIs and options, assign to the player. |
| 3 | `loopback_load_video_st3_extract_media_track` / `load_video_st3_extract_media_track` | Parse and read tracks (`_read_media_tracks`). |
| 4 | `loopback_load_video_st4_loaded` / `load_video_st4_loaded` | Apply initial state, view, tracks, chapters; then `notify_load_video_done`. |

Between stages 3 and 4 sits `_load_video_with_slaves`, which attaches external
audio/subtitle files as **slaves** and *waits for their tracks to appear*
(`_await_subtitle_slave_track`, `_await_audio_slave_track` — both retry loops,
because VLC reports slave tracks asynchronously). This is why
`VideoBlock._attached_subtitle_files` is a list in the order files were handed
over: track ids come back in that order.

`load_video` also sets `_media_options` per input — for example the software
player appends `avcodec-hw=none`. The comment at
`widgets/video_frame_vlc_sw.py:85` explains why that must be a *media* option:
`avcodec-hw` passed as an instance option is ignored and the hw decoder lookup
silently falls back to `any`.

Stage completion is signalled upward as `video_ready` (see `qt_connect` in
`VideoBlock.init_video_driver`, `video_block.py:582`), which lands in
`VideoBlock.load_video_finish` (`video_block.py:3456`) — the single place where a
newly loaded video is reconciled against its saved parameters.

## VLC instance options

`InstanceVLC.init_options` (`vlc_player/instance.py:92`) assembles the option
list in a deliberate order:

```text
--quiet --no-disable-screensaver --no-sub-autodetect-file --no-lua --no-osd
--no-snapshot-preview --no-interact --no-stats --no-keyboard-events --no-mouse-events
  * per-video vlc_options (from the Video model)
  * subtitle style options (per process, from settings)
  * user options from misc/vlc_options
```

plus `--no-plugins-scan` when `plugins.dat` exists, `--aout=directsound` on
Windows, certifi trust options on Windows 7, `--aout=pulse` on AppImage, and
`--vout=vdummy` for the software decoder instances.

Two landmines documented in the source:

* **`--no-spu` must never be added to the instance options.** The comment at
  `instance.py:103` is emphatic: it is settled when the input opens and cannot be
  undone, so `libvlc_video_set_spu` returns success while nothing is ever drawn.
  Subtitles are disabled per video by asking for no track instead.
* **User options from `misc/vlc_options` come last so they win**, and
  `_iter_settings_options` normalises `--option value` into `--option=value`.

Subtitles look the same across every video in one process because VLC settles
subtitle style per instance, not per media. That is why
`_subtitle_style_options()` reads the settings *inside the child process*
(`instance.py:242`) and why changing subtitle style requires a reload.

## The render surface

Two rendering strategies, and the difference explains most of the frame code.

### Native window (hardware)

VLC draws into a real X11/HWND/NSObject window. `VideoFrameVLCHW` creates a
`QWidget` for `video_surface`, takes its `winId()`, and passes that to the child
(`driver_setup`, `widgets/video_frame_vlc_hw.py:149`). The surface is excluded
from Qt's layout (`is_native_surface = True`), so
`apply_vlc_hw_surface_geometry` (`widgets/video_frame_vlc_base.py:105`) is the
only thing that positions it — a zero offset still has to size it.

That helper deliberately **oversizes** the surface: VLC fills its window with a
centred picture, so growing the window on every side pushes VLC's own crop border
and (on Windows) the unpainted Direct3D11 rows outside the visible frame.
`vlc_hw_crop_border_offset` decides how much: 8px on Windows, 2px elsewhere, and
on Linux **0 when the frame already fills the window** — otherwise the surface
would sit at (-2,-2), outside the top-level window, where KWin draws a seam.
`internal/hw_crop_border_offset` overrides all of this.

Resize is expensive here: each step is a `SetWindowPos` plus three libVLC
control calls per video. Frames therefore coalesce view updates at
`NATIVE_VIEW_RESIZE_INTERVAL_MS = 100` on the trailing edge
(`_adjust_view_on_resize`, `video_frame_vlc_base.py:628`) — leading edge stays
instant so a single resize is immediate. Software frames have no interval and
apply every resize.

**Releasing a native surface is the fragile part.** `detach_hw_xwindow`
(`video_frame_vlc_hw.py:21`) stops playback and sets the window handle to 0
*before* the Qt window is destroyed, because GPU drivers cannot tolerate an
invalid handle even though libVLC can. The parent then waits up to
`HW_PLAYER_RELEASE_TIMEOUT_S = 5` for a `player_released` command
(`VideoDriverVLCHW.cleanup_wait`). `Event`/`Condition` objects cannot cross the
pipe, which is why the notification reuses the command channel.

On Linux, video loading is additionally serialised with a
`multiprocessing.Semaphore` acquired in `init_player` and released in
`notify_load_video_done` — the comment at `video_frame_vlc_hw.py:73` says video
loading is not thread-safe there.

### Shared memory (software)

`--vout=vdummy` means VLC produces no window at all. The child's `ImageDecoder`
(`vlc_player/image_decoder.py`) grabs each frame in VLC's own callback and writes
RGB32 into a `SafeSharedMemory` segment named after the player id, signalling
readiness with `cmd_send("process_image")`.

The parent's `VideoDriverVLCSW` reacts on the Qt thread:
`process_image` → `image_ready_sig` → `_schedule_show` (a zero-delay
`QTimer.singleShot`) → `_show_frame`, which copies out via `FrameReader` and
calls `present_rgb32` on a `SoftwareVideoSurface`. Frames arriving faster than
the window shows them are never copied at all — the copy happens at display
time.

Memory-sharing bookkeeping lives in `InstanceProcessVLCSW`: a fixed pool of
`players_per_instance` locks, each with an `is_busy` flag and an embedded
player-id array. `SafeSharedMemory` (`multiprocess/safe_shared_memory.py`) wraps
the segment plus its lock, and supports being re-attached at a larger size when
the video resolution changes (`init_frame`).

`PlayerProcessSingleVLCSW._sync_decoder_pause` exists because the paused flag
used to be set only by explicit `play`/`stop`/`set_pause` calls, while initial
load and the live-unpause failsafe moved the player behind their backs — leaving
the decoder paused for the whole session.

Both `_sp` variants do the same rendering without the process hop; see
`video_frame_vlc_hw_sp.py` and `video_frame_vlc_sw_sp.py`.

## View geometry: the single source of truth

All aspect/zoom/crop/anchor/shift maths lives in `utils/aspect_calc.py` as pure
functions over `(frame_size, pane_size, ViewParams, transform)`:

| Function | Answers |
| --- | --- |
| `calc_view_placement` | Where the visible part of the picture goes. |
| `calc_whole_placement` | The same, but for the *whole* frame — used when the file says the picture is turned, because VLC crops a rotated picture wrongly. |
| `calc_picture_rect` | Where all of the picture is, including past the pane edges. |
| `calc_shift_range` | The least and most the picture can be moved. |
| `calc_moved_shift` | The shift a step of the arrow keys comes to. |
| `calc_dragged_shift` | The shift a pointer drag comes to, plus leftover pixels. |

`ViewParams` is an immutable `NamedTuple`, and the frame replaces it wholesale:

```python
def set_scale(self, scale) -> None:
    self._view = self._view._replace(scale=scale)
    self.adjust_view()
```

Both `VideoBlock` panning (`pan.py`) and the position dialog
(`dialogs/position.py`) drive these functions rather than doing geometry of their
own. If you add anything that moves or sizes the picture, **put the maths in
`aspect_calc.py` and call it from both the frame and the widget.**

**Milliseconds are `qint64`.** `utils/qt.py:13` defines `MILLISECONDS = "qint64"`
because PyQt maps a bare `int` in a signal to 32 bits, which wraps silently after
24.8 days — and a DASH live stream counts its clock from the epoch. Any new
signal carrying a time must use `MILLISECONDS`.

## The overlay

`widgets/video_overlay.py` composes `video_overlay_elements.py` widgets (seek
bar, volume, time, title, button row, status) over the video. Key behaviours:

* The overlay sizes itself to the video's placement, not the pane — hence
  `is_overlay_fits` / `_apply_overlay_size_policy` on `VideoBlock`. With
  `internal/opaque_hw_overlay` set, a native surface cannot be drawn over, so the
  overlay switches to an opaque mode instead. The "Opaque overlay (fix black
  screen)" setting is user-facing; `README.md` documents the black-screen symptom.
* `playlist/overlay_hide_on_timeout` + `playlist/overlay_timeout` drive a
  single-shot `QTimer` (`overlay_hide_timer`). `mouse_hide.py` hides the cursor
  on the same activity, and consumers listen to `OVERLAY_ACTIVITY_EVENT = 2000`.
* `playlist/disable_overlay` hides it entirely.
* The seek bar's marks come from `VideoBlock.seek_marks`
  (`video_block.py:3636`), which merges **chapters**, **bookmarks** and
  **SponsorBlock segments** into one tuple of `SeekMark`. That is the single
  extension point for anything new that should appear as a marker on the bar.

## Where playback state actually lives

This trips people up. There are three overlapping notions of state:

1. **`VideoBlock.video_params: Video`** — the persistent, user-visible state
   saved into playlists (volume, rate, aspect, loop points, selected tracks,
   `is_paused`, position …). Changing it is a real change.
2. **The frame's `media`** — what VLC currently has. Updated by
   `load_video_finish` and the `tracks_changed` / `chapters_changed` signals.
3. **`VideoBlock` runtime attributes** — `_is_error`, `is_live`, `streams`,
   `_stream_quality_playing`, `_audio_language_playing`, `_seek_target`,
   `_network_retries`, and the transient timers. These do **not** persist.

`apply_snapshot()` (`video_block.py:3003`) copies a `Video` into a block without
reloading — that is the mechanism for restoring a saved playlist. `set_video()`
(`video_block.py:3058`) assigns a *different* video and triggers a full
`_start_load`. Do not confuse them.

## Gotchas

* **Everything VLC-facing is asynchronous.** Tracks, chapters and dimensions can
  arrive after the load is reported. Hence `_schedule_tracks_reapply`,
  `_schedule_chapters_refresh`, `_arm_tracks_reapply_if_renumbered`, and
  `set_track_dimensions` filling in dimensions that came back empty.
* **Track ids are not stable across a reload.** `_arm_tracks_reapply_if_renumbered`
  exists because of that; selections are stored by *semantic* identity
  (`AudioLanguage`, `AudioExternal`, `SubtitleTrackId`, …) so they can be
  re-resolved — see `models/audio_selection.py` and
  `models/subtitle_selection.py`.
* **Seeking an adaptive stream is unreliable.** `SEEK_RETRIES` and the
  `_seek_target` / `_seek_landed_at` / `_seek_retries` triad in `_follow_seek`
  (`video_block.py:2795`) re-issue a seek that landed short, giving up after a
  few tries. `_is_adaptive` records that VLC's adaptive demuxer is in play.
* **Never call `cleanup` on one block at a time.** `_start_cleanup` in
  `player/managers/video_blocks.py:95` starts every block releasing before
  waiting on any, so closing a full grid does not read as videos going dark one
  by one.
* **Do not add `--spu` options to the VLC instance.** See above.
* **Changing subtitle style or encoding needs a reload.** Both are settled when
  the input opens; `VideoBlocksManager.apply_subtitle_encoding` works by handing
  each block a differing `Video`, which makes `set_video` reopen on its own.

## Key files

| File | Role |
| --- | --- |
| `vlc_player/player_base.py` | All libVLC calls; the load state machine. |
| `vlc_player/player_tracks_manager.py` | Track selection and re-application. |
| `vlc_player/instance.py` | VLC instance options, logging, process manager subclass. |
| `vlc_player/static.py` | `Media`, `MediaInput`, track dataclasses. |
| `vlc_player/image_decoder.py`, `rgb_buffer.py` | Software frame extraction. |
| `player/managers/video_driver.py` | Picks the driver class; owns the process manager. |
| `widgets/video_frame_vlc_base.py` | Surface, view application, snapshot/screenshot, cleanup. |
| `widgets/video_frame_vlc_{hw,sw,hw_sp,sw_sp}.py` | The four concrete drivers. |
| `multiprocess/command_loop.py` | The parent↔child RPC protocol. |
| `multiprocess/process_manager.py` | Instance pool, logging, teardown. |
| `utils/aspect_calc.py` | All view geometry. |
