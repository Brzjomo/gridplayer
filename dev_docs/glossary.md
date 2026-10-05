# Glossary

Project-specific terms, in the sense the code uses them. Where a term is
overloaded or misleading, that is noted.

## Core concepts

**Action** — An entry in the `ACTIONS` table (`params/actions.py`). A *declaration*
of something invocable: title, icon, shortcut, target command, conditions. Not a
`QAction` — see **dynamic action**.

**Active block** — The video cell that per-video actions apply to. Tracked by
`ActiveBlockManager` from mouse and drag events. `None` when the window is empty
or the pointer is on chrome. Actions routed through `("active", ...)` are silently
dropped when it is `None` or the video is not playable.

**Command** — A named callable published by a manager through its `commands`
property and registered in the shared `Commands` registry. **Command names are a
global namespace**; a collision between two managers raises `ValueError` at
startup.

**Command spec** — What an action stores to say what to run: a string, a
`("command", arg…)` tuple, or an `AND`/`OR`/`NOT` composition from
`utils/command_helpers.py`. Resolved to a callable by `Commands.resolve`.

**Context (`self._ctx`)** — The shared state object every manager receives. Holds
`active_block`, `video_blocks`, `commands`, `grid_state`, `is_single_mode`, etc.
`Context.__getattr__` **calls** a value if it is callable, which is why a plain
function like `is_single_mode` can be stored and read as a value.

**Dynamic action (`QDynamicAction`)** — The real, long-lived action object, plus a
per-menu **proxy** `QAction` for display. The proxy is what gets grayed out and
what carries the displayed shortcut; the real action stays enabled so its shortcut
keeps firing. Do not disable an action with `setEnabled(False)` — use `enable_if`.

**Manager** — One class per concern in `player/managers/`, subclassing
`ManagerBase`. Registered in `Player.managers`, instantiated with the shared
context, and initialised in dict order.

**ManagersManager** — The mixin on `Player` that instantiates managers, wires named
signal connections, installs event filters, and builds the command registry.

## Video and playback

**Cell** — One position in the grid. Holds either a `VideoBlock` or an `EmptyCell`.

**Driver** — The video *frame* implementation chosen by `player/video_driver`:
`vlc_hw`, `vlc_hw_sp`, `vlc_sw`, `vlc_sw_sp`, or `dummy`. `_sp` = **single
process** (in-process); without it, the frame runs in a child decoder process.
Note "driver" also loosely refers to the VLC-side `VLCVideoDriver*` objects the
frame owns.

**Frame** — A `VideoFrameVLC*` widget: the render surface plus the handle to a
player. Sits between `VideoBlock` and `vlc_player.Player`.

**Hardware / software video** — Whether VLC decodes on the GPU and draws into a
native window (hardware), or decodes on the CPU into shared memory that Qt paints
(software). Hardware is the default; software is the fallback and the only one
compatible with compositing-friendly overlays.

**Media** — `vlc_player.static.Media`: what the player *found* after opening an
input (tracks, dimensions, chapters, duration). Distinct from **MediaInput**.

**MediaInput** — What the widget *asks* the player to open: the `Video` model plus
the URIs and options.

**Native surface** — The real X11 window / HWND / NSObject that VLC draws into in
hardware mode. Excluded from Qt's layout, so
`apply_vlc_hw_surface_geometry` is the only thing that positions it.

**Snapshot** — Two unrelated things, distinguished by context:

1. A **paused live stream's frozen frame** (`VideoFrameVLC.pause_snapshot`),
   because pausing a live stream means stopping it.
2. A **saved grid moment** in a playlist (`models.playlist.Snapshot`): a
   `GridState` plus a video list. See also **screenshot**.

**Screenshot** — A user-requested image file of the current frame, written per the
`screenshots/*` settings. Not a snapshot.

**Shared memory** — The software driver's frame transport
(`multiprocess/safe_shared_memory.py`), one segment per player, written by the
child's `ImageDecoder` and read at display time.

**Stream** — `models.stream.Stream`: one resolved, playable variant of a URL, with
a `protocol` that selects a **wrapper**.

**Stream proxy** — The local HTTP server (`utils/stream_proxy/`) that fetches on
VLC's behalf. Used when a cookie is needed or when a network setting VLC cannot
honour is in play. See **relay**.

**Relay** — Serving a stream through the local proxy instead of handing the URL to
VLC. Decided by `needs_relay(url)` and `NetworkOpts.is_relay_required`.

**Wrapper** — The per-format relay implementation: `HTTPStreamProxy`, `HLSProxy`,
`HLSProxyLive`, `HLSMuxedStream`, `DASHManifestProxy`, `DASHPlaylistStream`,
`HTTPPlaylistStream`.

**Resolver** — Turns a page URL into playable streams. `DirectResolver`,
`StreamlinkResolver`, `YoutubeDLResolver`.

**yt-dlp / Streamlink** — External extractors. yt-dlp is tried first and is the
only one used for YouTube.

**SponsorBlock** — The crowd-sourced segment database. A **SkipSpan** is one or
more adjacent `SKIP` segments merged into a single seek.

## Settings and persistence

**Setting key** — A flat `"section/name"` string, e.g. `"playlist/grid_rows"`.

**`Settings()`** — The global, persisted INI-backed singleton. Reads and writes
`settings.ini` in the data directory. Raises `KeyError` on an undeclared key and
`ValueError` on a type mismatch.

**`PlaylistSettings()`** — The sparse, in-memory **override** layer shadowing
`Settings`. Records what this session (or this saved playlist) changed. `get()`
falls through to `Settings`.

**`session_field(key)`** — A pydantic `Field` whose default factory reads
`PlaylistSettings().get(key)`. This is why a `Video` has no fixed default for most
of its fields — it picks up the effective preference at construction time.

**`SettingField`** — The dataclass describing one row of a generated defaults form:
`settings_key`, `kind`, `section`, `label`, plus optional binding to a playlist or
video attribute.

**`FieldKind`** — Which widget a `SettingField` becomes: `CHECKBOX`, `COMBO`,
`TEXT`, `SPIN`, `FLOAT_SPIN`, `CROP`, `COLOR`.

**Defaults form** — `widgets/defaults_form.py`; renders any `SettingField` tuple.
Used for the playlist defaults, video defaults and subtitle style pages.

**`.gpls`** — A GridPlayer playlist file. Current format is JSON with
`format: "gridplayer-playlist"`; the legacy m3u-like `#GRIDPLAYER` format is still
read.

**Grid state** — The six grid attributes plus `cells` and `video_order`
(`models/grid_state.py`). Stored in a playlist and in `PlaylistSettings`
separately from the flat override dict.

**Inherited / overridden** — A grid attribute is *overridden* if it appears in
`model_fields_set`; otherwise it is *inherited* from global settings and is
**omitted from the saved file** so it keeps following the setting.

**`GridVisibility`** — Whether a settings row shows always, only in auto modes, or
only in fixed mode.

## UI and input

**Drop zone** — Which part of a cell a drop lands on: `BEFORE`, `CENTER`, `AFTER`
(auto modes) or `BEFORE`/`AFTER`/`ABOVE`/`BELOW`/`CENTER` (fixed mode).

**Drop indicator** — The glyph shown for a zone: arrows, `SWAP`, `DOT`, `REPLACE`,
or `SOURCE`.

**Fake drag** — The Linux-only reimplementation of `QDrag` (`DragNDropManager`),
because KWin forbids the native drag cursor and GNOME/XWayland swallow modifier
keys during `QDrag.exec()`. Gated by `internal/force_native_drag_events`.

**Flow layout** — The no-holes wrapping order used in auto grid modes
(`utils/layout/flow_layout.py`). Contrast **grid layout**.

**Grid layout** — The fixed 2-D layout with holes
(`utils/layout/grid_layout.py`). Also the name of the `QGridLayout` the manager
owns — read the context.

**Overlay** — The on-video chrome: seek bar, buttons, time, title, status.

**Opaque overlay** — The overlay mode used when a native surface cannot be drawn
over. The `internal/opaque_hw_overlay` setting is the documented fix for a black
screen over hardware video.

**Single mode** — One video filling the window. Implemented by **hiding** the other
blocks, so the grid layout is untouched.

**Pan** — Dragging a video's *picture* within its cell, moving the shift. Default
trigger is the middle mouse button. Not the same as dragging a video to another
cell.

## Multiprocess

**Instance** — A child decoder process hosting up to
`player/video_driver_players` players (default 4) sharing one `vlc.Instance`.

**Command loop** — The parent↔child RPC over a `multiprocessing.Pipe`. The wire
format is `(command_name, args)`, and `command_name` is dispatched by
`getattr(self, name)` — so a remote call needs a method of the same name on both
sides.

**Players per instance** — `player/video_driver_players`. Also the pool size for
shared-memory locks in the software driver.

**Orphan** — A decoder process outliving its parent. Prevented twice: a Windows
job object with kill-on-close (parent side) and a parent watchdog (child side).

**Job object** — The Windows mechanism (`multiprocess/job_object.py`) that kills
child processes when the parent dies.

## GridPlayer-specific verbs

**Bootstrap** — Not used. The startup sequence is called **init** throughout
(`init_app`, `init_vlc`, `init_translator`).

**Broadcast / `[ALL]`** — An action variant applying to every video, distinguished
in the menu by an `[ALL]` submenu and by ids ending in `" [ALL]"`.

**LOADING_COMMANDS** — The allow-list of per-video commands that still work while a
video is loading or showing a network error. Add to it, or your action is dropped.

**Cmd prefix** — Manager methods starting `cmd_` are command implementations;
those starting `is_` / `get_` / `ask_` are queries used by `show_if`, `check_if`
and `enable_if`.

**`internal/` settings** — Settings under the `internal/` prefix are not on the
normal settings page; they are diagnostics and platform workarounds
(`opaque_hw_overlay`, `fake_overlay_invisibility`, `force_native_drag_events`,
`hw_crop_border_offset`).

## Abbreviations

| Short | Means |
| --- | --- |
| `_ctx` | The shared `Context` object |
| `vb` | A `VideoBlock` |
| `vlc` | The vendored python-vlc binding, or libVLC itself — context |
| hw / sw | Hardware / software decoding |
| `_sp` | Single process (in-process), as opposed to a child decoder process |
| HLS / DASH | The two adaptive streaming manifests the proxy rewrites |
| VOD / live | Fixed-length vs. open-ended stream |
| UA | User agent |
| shm | Shared memory |
