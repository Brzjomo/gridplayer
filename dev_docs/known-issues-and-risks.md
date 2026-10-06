# Known issues and risks

Where the code is fragile, what is deliberately a workaround, and what to be
careful of when changing things. Grounded in the code's own comments and in the
structure of the project.

This is a maintenance risk map, not a bug list — for user-visible bugs see the
GitHub issue tracker.

## Summary: the riskiest areas

| Area | Risk | Why |
| --- | --- | --- |
| Native-surface lifetime (hardware video) | **High** | A use-after-free of a GPU drawable crashes the decoder process. |
| Decoder process teardown | **High** | Orphans, hangs on close, and platform-specific timing. |
| `VideoBlock` | **High** | 3581 lines; nearly every per-video feature passes through it. |
| Platform workarounds | **Medium-High** | Several are Linux/Wayland/macOS-specific with no test coverage. |
| Manifest rewriting (stream proxy) | **Medium** | Many real-world format variants; tested against local servers only. |
| Settings ↔ `PlaylistSettings` layering | **Medium** | Easy to write a setting that appears to have no effect. |
| Declarative tables (`ACTIONS`, `SECTIONS`) | **Medium** | Errors surface at first right-click, not at startup. |
| Subtitle style | **Medium** | Settled per VLC process; needs a reload; affects the win build's plugin list. |

## Fragile: native video surfaces

The single most dangerous code path. `detach_hw_xwindow`
(`widgets/video_frame_vlc_hw.py:21`) documents the constraint:

> libvlc can tolerate an invalid handle; GPU drivers (EGL/GLX/VA/VDPAU) cannot.
> The window must stay valid until playback is stopped.

Defences in place, each covering a different failure:

* `detach_hw_xwindow` stops playback and zeroes the handle **before** the Qt window
  is destroyed.
* `_start_cleanup` in `video_blocks.py:95` starts every block releasing before
  waiting on any, so a full-grid close overlaps the waits instead of serialising
  them.
* `VideoDriverVLCHW.cleanup_wait` waits up to
  `HW_PLAYER_RELEASE_TIMEOUT_S = 5` for a `player_released` command, and **only
  logs a warning on timeout** — it does not fail. So a wedged child degrades to a
  warning plus whatever `force_terminate` does later.
* On Linux, a `multiprocessing.Semaphore` serialises video loading, because "video
  loading is not thread safe on linux" (`video_frame_vlc_hw.py:73`).

**Risk if you change it:** reordering the stop/handle-zero/destroy sequence, or
making cleanup parallel without the semaphore, produces crashes that appear only
under heavy grid loads on specific GPU drivers.

**Note:** `Event`/`Condition` cannot cross the pipe, which is why the release
notification reuses the command channel rather than a synchronisation primitive.
Copy that if you add a similar notification.

## Fragile: decoder process lifetime

* **The job-object assignment has a window.** `process_manager.py:94` admits it: a
  crash between `process.start()` and `_job.assign(pid)` would leave the child
  outside the job. The child-side watchdog covers it — but only because both
  guards exist. **Do not remove either one as "redundant".**
* **Instance reuse depends on comparing VLC options.** `_get_available_instance`
  only reuses an instance whose `options` compare equal. A new per-video option
  that varies (rather than being constant per driver) would silently spawn a
  process per video, defeating `player/video_driver_players`.
* **`cleanup()` waits 5 seconds** and then force-terminates. A slow shutdown is
  logged as a warning, not an error, so it is easy to miss in the field.
* **Teardown is signalled, not called.** `ProcessManager.cleanup` and
  `StreamProxyManager.cleanup` run from Qt signals in `player/player.py`, and the
  process finally exits with `os._exit(0)` in `force_terminate`. **No destructor,
  `__del__`, or `atexit` hook runs.** Anything needing cleanup must be wired to a
  signal.

## Fragile: `VideoBlock`

At 3581 lines it is by far the largest module, and the concentration point for
per-video behaviour. It owns, at minimum: driver creation and replacement, URL
resolution, load state, track reconciliation, seeking and adaptive-stream
retry, chapters, bookmarks, SponsorBlock, repeat/loop, audio devices and
languages, external files, network retry, quality adaptation, snapshots,
screenshots, renaming, the overlay, and drag data.

Specific hazards:

* **Three overlapping state notions.** `video_params` (persistent, saved), the
  frame's `media` (what VLC has), and runtime attributes (`_is_error`, `is_live`,
  `streams`, `_stream_quality_playing`, `_seek_target`, `_network_retries`, …).
  `apply_snapshot` copies a `Video` in without reloading; `set_video` assigns a
  different video and triggers a full load. Confusing them silently loses state.
* **Everything VLC-facing is asynchronous.** Tracks, chapters and dimensions can
  arrive after the load is reported as complete, hence
  `_schedule_tracks_reapply`, `_schedule_chapters_refresh` and
  `_arm_tracks_reapply_if_renumbered`.
* **Track ids are not stable across reloads.** Selections are stored semantically
  (`AudioLanguage`, `AudioExternal`, `SubtitleTrackId`, …) precisely so they can be
  re-resolved. Storing a raw id will break on the next reload.
* **Seeking adaptive streams is unreliable.** `_follow_seek` re-issues a seek that
  landed short up to `SEEK_RETRIES` times.

**Recommendation:** extract before extending. New per-video features should become
collaborators (a helper object or a manager) rather than more methods on
`VideoBlock`. Existing extractions to use as models: `utils/sponsorblock.py`,
`utils/chapters.py`, `utils/external_subtitles.py`, `utils/screenshots.py`.

## Deliberate workarounds (do not "clean up" without checking)

Each of these is load-bearing and has a comment explaining why:

| Workaround | Where | Remove when |
| --- | --- | --- |
| `QT_XCB_GL_INTEGRATION=none` on Linux | `main/init_app_env.py:30` | Never, as long as hardware video uses X11. Without it, forked decoders inherit a broken GL dispatch and abort on the **second** hardware process. |
| `--vout=vdummy` | `video_frame_vlc_sw.py:32` | Never for software mode. |
| `avcodec-hw=none` as a **media** option | `video_frame_vlc_sw.py:88` | Never. As an instance option libvlc ignores it and decodes on GPU anyway. |
| `--no-spu` deliberately **absent** | `vlc_player/instance.py:103` | Never add it. It is settled at input open and cannot be undone: `libvlc_video_set_spu` reports success while nothing is drawn. |
| Oversized native surface to hide VLC's edges | `video_frame_vlc_base.py:75-102` | When VLC stops leaving crop borders / unpainted D3D11 rows. Platform- and driver-specific. |
| Linux: **no** crop offset when the frame fills the window | `video_frame_vlc_base.py:98` | Never. At (-2,-2) the surface leaves the top-level window and KWin draws a seam. |
| Fake drag on Linux | `drag_n_drop.py:178` | **When native Wayland is an option** — the comment names the condition. |
| `_DRAG_LEAVE_GRACE_MS = 250` on Linux | `drag_n_drop.py:27` | When X11 XDND Enter stops being a later client message. |
| `error.ignore()` never from a drag filter | `drag_n_drop.py:307` | Never. Rejecting leaves the source app (Dolphin, QXcbDrag) stuck until restart. |
| Always-accept then `QTimer.singleShot(0, ...)` on drop | `drag_n_drop.py:310` | Never. The deferral is what stops the drag UI being torn down over rearranged widgets. |
| Streamlink's explicit `set_address_family` | `utils/network.py:234` | When Streamlink's `ipv4` option can turn the restriction *off*. Currently it is a urllib3 global that only ever turns on. |
| Streamlink `_original` stashing on the session | `utils/network.py:351` | Never. Without it, clearing a setting leaves the last value we wrote. |
| `untangle_percent_re()` after every yt-dlp import | `utils/percent_re.py` call sites | When yt-dlp stops patching urllib3 on import. |
| `MILLISECONDS = "qint64"` | `utils/qt.py:13` | Never. A plain `int` signal wraps silently after 24.8 days, and DASH live clocks count from the epoch. |
| macOS forced to `vlc_hw_sp` | `video_driver.py:46` | Never, without a cross-process rendering mechanism on macOS. |
| `QT_XCB_GL_INTEGRATION`, `--platform xcb` instead of `QT_QPA_PLATFORM` | `params/env.py:31` | When Wayland hardware video works. |
| Overlay opacity workarounds | `video_overlay.py:463`, `video_status.py:42` | When KWin stops making tool windows opaque. |
| `deleteLater()` on every context menu | `player/managers/menu.py:32` | Never. Without it each open leaks a `QMenu`. |

## Platform-specific risk

| Platform | Extra risk |
| --- | --- |
| **Linux** | The most workarounds: X11 vs Wayland platform choice, the fake drag, the drag-leave grace, the semaphore around video loading, compositor opacity fixes. Hardware video needs an X drawable, so a pure-Wayland session is degraded by design. `--platform` is the diagnostic lever. |
| **Windows** | The job object, the Direct3D11 unpainted-rows workaround, the AppUserModelID, the duplicated `init_icon` call for Windows 11. The 32-bit build lacks `curl-cffi`, so **browser impersonation is absent** there (`pyproject.toml:40`, `build_win.sh:29`) — some hosts will fail to resolve on 32-bit only. |
| **macOS** | No multiprocess hardware decoder; `is_the_only_instance` uses `NSWorkspace`; `OpenFile` events replace CLI delegation; `gsed` is required to build. |
| **Snap** | Strict confinement: the host's `/usr/bin` is absent and the home interface refuses hidden folders, which is why `js_runtime` searches the app data directory. |
| **Flatpak** | Needs the host filesystem at `/run/host`, and has its own socket path and a from-source libvlc build. |
| **AppImage** | Needs `--aout=pulse` (set in `instance.py`), and self-updates from the *continuous* pre-release for dev builds. |

## Test coverage gaps

The suite is large (~25k lines) but has real holes. Areas that are **hard to test**
and therefore lightly covered:

* **Real video decoding.** Tests fake the VLC objects
  (`FakeMediaPlayer`, `FakeMediaTrack`) or stub `driver_setup`. Nothing verifies
  that a real decode produces the right pixels for a given driver.
* **Hardware rendering at all.** `tests/test_video_frame_vlc_sw.py` and
  `tests/test_video_driver_sw_sp.py` exist; there is no equivalent running a real
  native window. The geometry helpers (`apply_vlc_hw_surface_geometry`,
  `vlc_hw_crop_border_offset`) are testable in isolation but the placement in a
  live window is not.
* **Platform-specific code.** `darkmode_linux`, `darkmode_windows`,
  `compositor_linux`, `win32_window`, `wayland`, `keepawake_macos` are effectively
  untested, and CI runs on Linux only (plus a Windows build with no test job).
* **Multiprocess end-to-end.** The command loop, instance pooling and orphan
  guards have no integration test that actually spawns a decoder.
* **UI interaction.** Mouse chords, drop zones into a real widget hierarchy, and
  overlay hit-testing are tested at the model level only.
* **No global table consistency test.** Nothing asserts that every action id in
  `SECTIONS` exists in `ACTIONS`, that every `func` names a command some manager
  publishes, or that no two actions claim the same default shortcut. Per-feature
  `tests/test_menu_*.py` files guard their own submenus. **Adding such a test
  would be a cheap, high-value win.**
* **No settings-declaration consistency test.** Nothing asserts that every key in
  `_default_settings` appears in a `*_FIELDS` tuple or vice versa, nor that every
  `SettingField.playlist_attr` / `video_attr` exists on the corresponding model.

CI runs pytest on Linux (Python 3.10 and 3.14). **There is no test job on Windows
or macOS**, so platform regressions in non-Linux code paths are caught only by
builds succeeding.

## Structural risks

* **Import-order coupling.** `gridplayer.player` must not be imported before
  `init_vlc()`, because importing python-vlc loads the shared library and the
  embedded-VLC environment variables must be set first. This is enforced only by
  the placement of one import inside `run_app` plus a comment. A tool that
  reorders imports, or a module-scope import added for convenience, breaks
  startup on machines without system VLC.
* **Startup import cost.** streamlink and yt-dlp each take ~⅓ second to import, and
  are kept out of the startup path by lazy imports in
  `url_resolve._resolver_map`, `StreamProxyManager.stream_proxy`,
  `dialogs/settings.py:run_*_checkup` and `utils/network.py:fetch_capped`. A
  single module-scope import in a startup-path module silently adds that cost to
  every launch.
* **Translation timing.** `tests/test_translation_timing.py` guards it, but the
  failure mode is subtle: a module-level `translate()` bakes in English
  permanently, and this is how every menu entry once came out in English.
* **New UI is English until the next translation round.** `.ts` files are
  managed on Crowdin and are never edited here, so a string added to the source
  shows in English in every language until somebody runs the translation update
  ([build-and-release.md](build-and-release.md), `scripts/translations/`).
  Measured when the alignment dialog was reworked: **no `.ts` in the tree
  contains the `Dialog - Align Videos` context at all**, so that whole dialog —
  every label, button and note — is English in every language, including the
  ones that are otherwise fully translated. Its 46 strings are extractable now
  (`pylupdate5`, checked), which is the precondition for the round picking them
  up at all.
* **Strings the extractor cannot see.** `pylupdate5` reads a call's text only
  where that text *begins on the same line as the context*. `ruff format` splits
  any call that does not fit on one line, which puts the text on the next line,
  and Python's own joining of adjacent literals hides the difference from every
  other check. Measured over the package: **100 call sites were like that** —
  including the whole of the settings form (`params/defaults_fields.py`, 18) and
  the network checkups (27) — each of them a string no translator would ever be
  given. The alignment dialog's 18 were fixed with the recipe now in `AGENTS.md`
  (statement of its own + `# fmt: skip` on its last line); **82 remain**, held
  against growth by `tests/test_translation_extraction.py`. Sweeping them is a
  mechanical change of its own, worth doing before the next translation round.
* **The command namespace is global.** A new manager's `commands` name colliding
  with an existing one raises `ValueError` at startup — good, but it means command
  names must be chosen with global uniqueness in mind.
* **Generated files are committed.** `gridplayer/resources/` and `*_ui.py` are in
  the tree. They can drift from their sources if someone regenerates without
  committing, or edits a generated file directly.
* **`ACTIONS` is a `MappingProxyType`** and the tables are built at import time, so
  a malformed entry cannot be fixed at runtime.

## Fragile: input, focus and non-modal dialogs

Three things combine badly, and the combination is easy to walk into:

1. **The hit-tests are geometric and ignore window stacking.**
   `ActiveBlockManager.get_video_block_at` and `GridManager.get_cell_at` both walk
   the visible blocks asking whether the block's rect contains the point, and
   `get_video_block_under_mouse` feeds them `QCursor.pos()`. Neither asks **what is
   over that point**. So a dialog lying across a video cell reads as "the pointer
   is on that video".
2. **`DragNDropManager` and `PanManager` are global event filters**
   (`app.installEventFilter(player)` in `main/run.py`, plus
   `global_event_filters` in `player/player.py`). They therefore see a press on
   *any* window, including a dialog's buttons. `DragNDropManager.mousePressEvent`
   then calls `activate_window()` — `Player.raise_()` + `activateWindow()` — which
   takes focus off the dialog between the press and the release, so a
   `QPushButton` never completes a click.
3. **The only guard was `is_modal_open()`.** Modal dialogs are out of reach
   already, so this never showed up until a dialog had to be **non-modal** — which
   the alignment dialog must be, because the videos are moved by hand while it is
   open.

The symptom is distinctive and worth recognising: **the first press or two on a
button do nothing and the dialog loses focus; clicking repeatedly starts
working.** It works on the repeat because the player is already the active window,
so there is no focus left to take mid-click.

`utils/qt.belongs_to_a_dialog` is the general guard, and the mouse handlers in
`DragNDropManager` and `PanManager` consult it. Two rules follow:

* **Any new non-modal window that floats over the videos needs this guard.** Pass
  `event_object` into the handler (a two-parameter `event_map` entry gets it) and
  return early.
* **Keep the guard about where the event went, not about presses in general.** A
  press on the video itself must still activate the window — overlay `Qt.Tool`
  windows eat X11 focus, and the comment at `drag_n_drop.py` explains that Shift
  would otherwise not be delivered.

A related, narrower trap lives in the same area: **`clicked` carries a `checked`
flag and hands it to any slot that will accept arguments.** A bound method of a
`VideoBlock` is wrapped by `only_initialized` / `only_seekable` as
`wrapper(*args, **kwargs)`, which accepts the flag and then reads it as `self`.
Connect buttons through a handler that takes no arguments of its own
(`AlignVideosDialog._doing` is the example). A test stub with plain
no-argument methods will **not** catch this — give the stub `*args` and assert
none arrive.

## Maintenance hotspots by size

Largest non-generated modules, as a proxy for where changes are hardest:

| Lines | File | Note |
| --- | --- | --- |
| 3581 | `widgets/video_block.py` | See above; extract before extending. |
| 2327 | `params/actions.py` | Pure data. Large but low-risk to *add* to; risky to restructure. |
| 1475 | `widgets/video_overlay_elements.py` | Custom-drawn widgets; geometry-sensitive. |
| 1353 | `dialogs/bookmarks.py` | |
| 1296 | `vlc_player/player_base.py` | All libVLC calls. Highest-consequence file in the project. |
| 1256 | `widgets/keymap_tree_view.py` | |
| 1124 | `player/managers/active_block.py` | Routing, `LOADING_COMMANDS`, generated menus. |
| 995 | `dialogs/playlist_bookmarks.py` | |
| 919 | `params/defaults_fields.py` | Data; adding is routine. |
| 779 | `dialogs/settings.py` | Two mechanisms (`.ui` widgets + generated forms). |
| 763 | `widgets/video_frame_vlc_base.py` | Geometry and cleanup ordering. |

Notable test files are similarly large (`test_managers_playlist.py` 1213,
`test_bookmarks_dialog.py` 1108), which is a sign the structures they cover are
complex rather than that the tests are verbose.

## If you are new to the codebase

Read in this order, and stop when you have enough:

1. [architecture.md](architecture.md) — the manager/command pattern.
2. [commands-and-actions.md](commands-and-actions.md) — how anything the user can
   do is declared.
3. [settings-system.md](settings-system.md) — the `Settings` vs `PlaylistSettings`
   split, which is the most common source of "my change did nothing".
4. The area you are changing.

Then, before your first substantial change:

* Run `uv run pytest` and note the baseline.
* Pick a small, real change and follow it end to end through the tables before
  touching `VideoBlock`.
* Add the global consistency test described above if your change touches
  `ACTIONS` or `SECTIONS` — it will pay for itself immediately.

## Fragile: audio alignment (`Align By Sound`)

Everything here was measured rather than reasoned about, because the obvious
reasoning was wrong three times. See [audio-alignment.md](audio-alignment.md) for
the measurements and [roadmap.md](roadmap.md) for what is still open.

| Risk | Consequence | Guard |
| --- | --- | --- |
| Seeking into a **video container** misses by up to ~1 s, invisibly | A wrong offset that still scores 1.000 — no threshold can catch it | Read from the file's start; never `:start-time`. `Reading.origin_ms` exists for this |
| Comparing **loudness** rather than the shape of the sound | Real multi-camera material scores 0.2 and gets skipped, so the button does nothing | `_shaped()`; measured 0.34 (loudness) vs 0.67 (shape) on the same pair |
| `MIN_SCORE` is an absolute correlation | Sensitive to the material, not to whether the answer is right | Under review — see roadmap item 4 (peak prominence) |
| Setting an offset **does not move a video** (`p − o` preserved) | Offsets corrected, videos no closer together; looks like the button did nothing | `_measured()` ends with `sync_offset_align_others()` |
| The reference is **the first row** | If that video is the odd one out, the others are aligned to it | Single-pair measurement is the weak point — roadmap item 5 |
| Reading costs ~position ÷ 300 s | Repeated alignment re-reads everything; an hour-long recording is ~6 s per video | Nothing yet — roadmap item 3 (cache) |
| Transcode adds a **codec-dependent constant** (128 ms measured, PCM vs AAC) | Residual of ~100 ms when a set mixes codecs | Documented in the UI-facing limitations; cancels for a matched set |

## Traps that have already bitten

Each of these cost real time in this codebase. They are not style preferences.

**Test fixtures must be aperiodic.** A signal that repeats matches itself just as
well one period out. Both audio fixtures were first written with a short period
(`index % 37`, `math.sin(index / 11)`) and both reported a lag several thousand
blocks from the truth, confidently. A test that cannot tell a lag from a
lag-plus-a-period is not testing the search.

**An audio-only fixture hides container behaviour.** A wav seeks exactly
(measured error +1 ms), so an end-to-end test built on a wav passes while the
real, video-container path is out by a second. Test the path the user has.

**Do not assert what the fixture cannot demonstrate.** One draft asserted that
loudness alone scores below the threshold; the real recordings score 0.34, the
synthetic fixture 0.70. The assertion was deleted rather than the fixture bent
until it agreed.

**`getattr(mock, name, default)` does not return the default.** A `Mock` context
answers with a truthy child `Mock`, so a guard written as
`getattr(ctx, "flag", False)` reads as *set*. Likewise a property read from a
`Mock` that stands in for `self` is a truthy `Mock`. `tests/test_video_block_repeat.py`
calls `VideoBlock.show_overlay(block)` with a `Mock`, which is how this surfaced:
the guard has to read only from the context the test configures, and compare with
`is True` rather than by truthiness.

**`clicked` passes a `checked` flag to any slot that will accept one.** A
handler written as `lambda amount=amount: ...` or as a `*args` wrapper receives
`False` as its first argument. See the note on non-modal dialogs above.

**Ruff formats Python inside Markdown**, so `ruff format --check .` inspects
fenced Python in these documents and rewrites excerpts. `dev_docs` is in
`extend-exclude` in `pyproject.toml` for that reason — do not remove it.

**Finding the running GUI process from a script.** The console entry point leaves
a small `gridplayer` wrapper process (a few MB, no window) alongside the real
`python` process that owns the window. Matching on process *name* finds the
wrapper and reports the application as hung when it is not. Find it by the window
title instead.
