# UI layer

The window, the grid, the video cells, and the managers that drive them from
user input.

## The widget tree

```text
Player (QWidget)                        player/player.py
├── QGridLayout                         owned by GridManager
│   └── VideoBlock / EmptyCell …        one widget per grid cell
│       ├── video_clip                  clips the native surface
│       │   └── video_surface           native window, or SoftwareVideoSurface
│       │       └── (VLC draws here)
│       ├── pause_snapshot              frozen frame for a paused live stream
│       ├── audio_only_placeholder
│       ├── status widget               VideoStatus / loading / info
│       └── overlay                     VideoOverlay (seek bar, buttons, …)
└── (no other children; the menu is a transient CustomMenu)
```

`Player` keeps `setMouseTracking(True)` and `setAcceptDrops(True)`
(`player/player.py:37`), so it sees mouse movement and drops even where there is
no video.

## The managers that handle input

Everything user-facing is one of the 21 managers. The ones concerned with input
and layout, and the order they see events in:

| Manager | Registered as event filter? | Concern |
| --- | --- | --- |
| `WindowStateManager` | yes (`event_filters[0]`) | window state, minimize, close. |
| `DragNDropManager` | yes, and **global** | drag start, drop targets, drop indicators. |
| `ActiveBlockManager` | yes | which cell is "active". |
| `MenuManager` | yes | the context menu. |
| `ActionsManager` | yes | mouse shortcuts, and the last filter to see an event. |
| `PanManager` | **global only** | dragging a picture about. |
| `MouseHideManager` | no (uses `event_map` via the window filter) | hiding the cursor. |
| `GridManager` | no | the layout of cells. |
| `SingleModeManager` | no | one video filling the window. |
| `VideoBlocksManager` | no | creating, closing and broadcasting to blocks. |

Two registration lists in `player/player.py` matter:

```python
self.global_event_filters.append("drag_n_drop")
self.global_event_filters.append("pan")

self.event_filters = [
    "window_state",
    "drag_n_drop",
    "active_block",
    "menu",
    # After other filters: only consumes when a mouse chord matches.
    # Receives events targeted at the Player (empty chrome), not VideoBlocks.
    "actions",
]
```

* **`event_filters`** are installed on the `Player` widget with
  `installEventFilter`, so they see events delivered to the window.
* **`global_event_filters`** are consulted from `ManagersManager.eventFilter`,
  which the `Player` itself is installed as for the whole application
  (`app.installEventFilter(player)` in `main/run.py:39`). That is why pan and
  drag work over video cells and even outside the window.

**Order is significant.** `actions` is deliberately last and its comment explains
why: it only consumes the event when a mouse chord actually matches, so it must
not shadow the managers that handle drags and clicks. If you add a filter,
decide deliberately where it goes.

`ManagerBase.eventFilter` (`player/managers/base.py:15`) dispatches through an
`event_map` dict and **adapts the call signature by arity**:

```python
nparams = len(inspect.signature(event_function).parameters)
if nparams == 0:    result = event_function()
elif nparams == 1:  result = event_function(event)
else:               result = event_function(event, event_object)
```

So a handler may take `()`, `(event)` or `(event, event_object)` — the last is
needed when the handler must know which widget received it (`drag_n_drop` uses
it for `mapToGlobal`). Returning exactly `True` marks the event as handled.

## The grid

### Two layers, deliberately

The grid is split between **layout models** (pure data, no Qt widgets) and the
**manager** that materialises them.

`utils/layout/base.py` defines the `Layout` protocol. Two implementations:

| Class | Mode | Has holes? |
| --- | --- | --- |
| `FlowLayout` | `AUTO_ROWS` / `AUTO_COLS` | No — a wrapping 1-D order. |
| `GridLayout` | `FIXED` | Yes — a real 2-D grid with empty cells. |

`FlowLayout.displayed_size()` (`flow_layout.py:30`) is the whole sizing rule:

```python
count = len(self.ids)
if count <= 1:
    return 1, 1
grid_size = self.size
if grid_size == 0:
    grid_size = math.ceil(math.sqrt(count))
grid_slices = math.ceil(count / grid_size)
if self.mode != FlowMode.COLS:
    return grid_size, grid_slices
return grid_slices, grid_size
```

`playlist/grid_size` of 0 means "square, up to the square root of the count" —
the default. `grid_fit` decides whether that size is a hard limit or grows.

### GridManager

`GridManager` (`player/managers/grid.py`) owns the real `QGridLayout` and
translates the model into it. Its `commands` property is the public surface
(`grid.py:85`) — `set_grid_mode`, `ask_grid_size`, `grid_cell_count`,
`layout_drop`, `layout_order`, `shuffle_layout`, `get_cell_at`,
`set_add_anchor`, `apply_grid_config`, and the `is_*` predicates actions use.

`_clear_layout` recursively tears down nested layouts before a rebuild.
`reload_video_grid` is connected to `video_blocks.video_count_changed` and to its
own `layout_changed` signal, so **any change to the layout model must emit
`layout_changed`** to be reflected on screen.

The last row/column is filled differently from the rest so the leftover cells
share the space evenly rather than being left ragged:

* `_fill_grid` — the square body, via a generator that yields `(col, row)` in the
  order the flow mode implies.
* `_fill_last_row` — a `QHBoxLayout` spanning the last row, each widget with
  stretch 1.
* `_fill_last_col` — a `QVBoxLayout` spanning the last column.

Empty cells are `EmptyCell` widgets; the placeholder shown when there are no
videos at all is also an `EmptyCell` carrying "Drag and drop media files or URLs
here".

### Single mode

`SingleModeManager` (`player/managers/single_mode.py`) does not resize or reflow
anything — it **hides every other block** and leaves one visible:

```python
def single_mode_on(self):
    self._ctx.is_single_mode = True
    for vb in self._ctx.video_blocks:
        if vb == self._ctx.active_block:
            continue
        self._pause_playing(vb)
        vb.hide()
    self.mode_changed.emit()
```

Because the grid layout is untouched, leaving single mode is just showing them
again. Note:

* `is_single_mode_available()` requires at least one video **and**
  `grid_cell_count() > 1` — single mode is pointless in a 1×1 grid.
* `set_video_count` exits single mode whenever the number of videos changes.
* `_switch_single_video` finds the visible block and steps through
  `layout_order()` so "next video" follows the on-screen order, wrapping at the
  end. `previous` uses `blocks[idx - 1]`, relying on Python's negative indexing.
* `_pause_playing` / `_restore_playing` record the pre-single-mode pause state in
  `_pre_sm_states` keyed by block id, so videos that were already paused stay
  paused on exit. `pause_background_videos` gates whether pausing happens at all.

## Drag and drop

The most intricate input code in the project, and much of its complexity is
platform workarounds with comments explaining each.

### Starting a drag

`mousePressEvent` records the press position unless panning is starting instead.
`mouseMoveEvent` checks it exceeded `QApplication.startDragDistance()` and then
branches:

```python
# Native QDrag on xcb/XWayland freezes modifiers (pointer grab) and
# GNOME does not forward keys to an unfocused X11 window.
if env.IS_LINUX and not Settings().get("internal/force_native_drag_events"):
    self._start_fake_drag()
    return True
```

So on Linux there is a **fake drag** implementation: it sets an override cursor,
tracks the pointer itself, and finishes on mouse release, because KWin forbids the
native `QDrag` cursor and GNOME/XWayland swallows modifier keys during
`QDrag.exec()`. The comment at `drag_n_drop.py:178` states this is to be removed
when native Wayland is an option. `internal/force_native_drag_events` is the
escape hatch.

`_is_swallow_click_after_fake_drag` exists so the release that ends a fake drag is
not also delivered as a click — it would otherwise toggle play/pause.

The payload is a JSON-serialised `VideoBlockMime` under the MIME type
`application/x-gridplayer-video`. Because the `VideoBlockMime` carries the full
`Video` model, a drag that lands in **another GridPlayer instance** is handled
too (`_drop_video_block` logs "Dropped video from another instance" and re-adds
the video, together with its bookmarks).

### Drop zones and indicators

`utils/drop_zone.py` is pure geometry:

* `zone_at(x, y, w, h, grid_mode)` returns a `DropZone`, or `None` outside the
  cell. The rule differs by mode: in `AUTO_ROWS`/`AUTO_COLS` the cell is divided
  **thirds along the flow axis** (before / center / after); in `FIXED` mode it is
  a **25% frame** producing `BEFORE` / `AFTER` / `ABOVE` / `BELOW`, with
  everything inside the inner rectangle being `CENTER`.
* `indicator_for(zone, is_internal, grid_mode, is_replace)` picks the glyph, using
  different arrows for the two axes and distinguishing an internal swap (`SWAP`)
  from an external dot (`DOT`).
* `insert_index(dst_index, zone)` maps `AFTER` to `dst_index + 1`.

In `AUTO_COLS` mode the before/after zones render as **up/down** arrows, because
the flow runs down the columns — the indicator follows the layout, not the screen
axis.

`_hover_at` converts global to cell-local coordinates and calls `zone_at`.
`_update_drop_target` then decides `is_replace` from the drop modifier, and forces
`CENTER` when an internal drag hovers an empty cell ("only swap with empty cells
on internal drag").

### Replace vs. insert

`_is_replace` consults the session's `playlist/drop_action_internal` or
`drop_action_external` plus `playlist/drop_modifier`, via
`utils/drag_n_drop.drop_is_replace`. So the same gesture means different things
depending on which mouse button/modifier is held at the moment of the drop, and
that rule is a setting rather than hardcoded.

### Drop ordering, and why it is deferred

```python
# Always accept so Qt sends XdndFinished. Rejecting from a filter
# without that leaves Dolphin and QXcbDrag stuck until restart.
event.acceptProposedAction()
QTimer.singleShot(0, lambda: self._finish_drop(...))
```

Two separate lessons in three lines:

* **Never reject a drag from an event filter.** Qt needs to send `XdndFinished`;
  refusing leaves the *source* application (Dolphin, another Qt app) stuck with a
  drag cursor until it restarts.
* **Finish the drop on the next event-loop turn**, not inline, so the drag UI is
  torn down before the widgets it refers to are rearranged.

`_DRAG_LEAVE_GRACE_MS = 250` on Linux exists for a related reason documented at
`drag_n_drop.py:27`: X11 `XDND Enter` is a *later* client message, so if the drag
UI is ended on a zero-delay timer, the player can end up no longer tracked by Qt
when KWin sends the drop — no `XdndFinished`, a stuck "+" cursor, and no further
drops accepted. On Linux the grace period is used instead of `timer(0)`.

When drop files arrive, `get_playlist_path` is checked **first**: a dropped
`.gpls` opens as a playlist rather than being added as a video. Otherwise
`filter_video_uris` filters to supported media and the rest are dropped into the
layout.

### Applying the drop

Everything funnels through `_drop_on_layout`, which calls the single
`layout_drop` command and then updates the recent list:

```python
def _drop_on_layout(self, videos, src_block, dst, zone, is_replace):
    self._ctx.commands.layout_drop(videos, src_block, dst, zone, is_replace)
    if videos:
        self._ctx.commands.add_recent_videos(videos)
    self._ctx.commands.activate_window()
```

`GridManager.cmd_layout_drop` resolves the drop into model operations and returns
the *new* video ids; `VideoBlocksManager` then creates or reuses blocks, which is
why `VideoBlocks.reindex` exists. **If you add a new way to rearrange cells, go
through `layout_drop`** rather than manipulating the `Layout` directly — the model
and the widgets must not drift.

## The active cell

`ActiveBlockManager` maintains `self._ctx.active_block`, the cell that actions
apply to. It is updated from mouse events:

| Event | Effect |
| --- | --- |
| `MouseMove`, `MouseButtonPress`, `MouseButtonRelease` | `update_active_under_mouse` |
| `NonClientAreaMouseMove`, `NonClientAreaMouseButtonPress` | `update_active_reset` |
| `DragEnter`, `DragMove`, `Drop` | `update_active_from_drag` |

Events are **ignored while a modal dialog is open** (`is_modal_open()`), the
reason being that a dialog can be dragged across cells. It also listens to
`video_blocks.video_count_changed` and `grid.layout_changed` to re-evaluate.

`active_block_change` is emitted on every change, which is what refreshes the
overlay border and the menu's `video_active` section.

The manager also holds the **`cmd_active` dispatch** described in
[commands-and-actions.md](commands-and-actions.md), the `LOADING_COMMANDS`
allow-list, and all the `is_active_*` predicates used by `show_if` / `enable_if`.

## The overlay

`widgets/video_overlay.py` composes the on-video chrome:

* `VideoOverlay` — the container, sized to the *video's placement* rather than the
  pane, so the seek bar lines up with the picture even in letterboxed cells.
* `video_overlay_elements.py` (~1475 lines) — `OverlayBar`, seek bar, volume, time,
  title, status text.
* `video_overlay_buttons.py` — `OverlayButton`, the icon button row.
* `video_overlay_icons.py` — SVG icon rendering for the buttons.

Three things to know before touching it:

1. **Native surfaces cannot be drawn over.** When the hardware driver has a real
   window over the cell, the overlay either becomes a separate `Qt.Tool` window
   or switches to opaque mode. `VideoBlock.is_overlay_fits` and
   `_apply_overlay_size_policy` handle the switch; the
   `internal/opaque_hw_overlay` and `internal/fake_overlay_invisibility` settings
   exist for the platforms where this misbehaves (the README documents the
   "black screen" fix). `drag_n_drop` and `pan` both have comments about overlay
   `Tool` windows stealing X11 focus or being the actual XDND target.
2. **The overlay is click-through except on its controls.** `OverlayButton` and
   `OverlayBar` are special-cased in `PanManager._is_trigger`, which refuses to
   start a pan on top of them.
3. **Hiding is timer-driven.** `overlay_hide_timer` is single-shot and restarted
   by `OVERLAY_ACTIVITY_EVENT` (`params/static.py:13`, value 2000) and by mouse
   movement. `playlist/overlay_hide_on_timeout` and `overlay_timeout` control it;
   `playlist/disable_overlay` removes it entirely.

## Cursor hiding

`MouseHideManager` (`player/managers/mouse_hide.py`) is small and worth reading as
a model of the `event_map` idiom:

```python
move_events = {QEvent.ShortcutOverride, QEvent.NonClientAreaMouseMove, ...}

def __init__(self, **kwargs):
    self.mouse_timer = QTimer(self)
    self.mouse_timer.timeout.connect(self.hide_cursor)
    if Settings().get("misc/mouse_hide"):
        self.mouse_timer.start(1000 * Settings().get("misc/mouse_hide_timeout"))
    QApplication.instance().focusObjectChanged.connect(self.show_cursor)

@property
def event_map(self):
    return dict.fromkeys(self.move_events, self.show_cursor)
```

`dict.fromkeys` maps every event in the set to the same handler. Hiding is
suppressed when there are no videos or a modal is open, and `show_cursor` only
restores when the current override cursor is actually `BlankCursor` — so it will
not clobber another part of the program's cursor (pan and fake-drag both set
`ClosedHandCursor`).

## Panning the picture

`PanManager` (`player/managers/pan.py`) moves the video *within* its cell. Its
docstring states the interaction rules:

* Default trigger is the **middle button**, which nothing else uses.
* Alternatively left-drag with a modifier (`player/pan_trigger` = ctrl/shift/alt).
* A plain left drag is always "move the video to another cell", and the modifier
  must be **down before the button** — a key pressed mid-drag belongs to the drop.
* A click with no movement is still a click, so the keymap sees it.

Implementation notes:

* It is a **global** event filter, and it explicitly ignores events whose
  `event_object` is a `QWindow`. The comment at `pan.py:53` explains: filtering the
  whole application, a mouse event is seen on the window before Qt hands it to a
  widget; if the release were eaten there, Qt would keep sending every later move
  to the widget the button went down on, and the overlay's bars under the pointer
  would never hear of them.
* It works in **global cursor positions** (`QCursor.pos()`), not local ones.
* Leftover sub-pixel movement is carried in `_left_over` and fed back into the
  next `pan_by` call, so slow drags are not lost to integer rounding.
* `_is_trigger` combines `QApplication.queryKeyboardModifiers()` with
  `event.modifiers()`, for the same reason the drop code does: a window that was
  not focused when the press arrived may not report the modifier in the event.
* On release it calls `block.set_shift(block.video_params.shift)` — routing the
  final position through the same path a menu-driven move takes, which is what
  records it for saving.

## Window state and shutdown

`WindowStateManager` (`player/managers/window_state.py`):

* `init()` sets the minimum size (`PLAYER_MIN_SIZE`) and initial size
  (`PLAYER_INITIAL_SIZE`), applies `player/stay_on_top` **except on Linux** ("has
  window manager for this, the flag doesn't work there anyway"), and records
  `is_maximized_pre_fullscreen` from the startup settings.
* `cmd_fullscreen` remembers whether the window was maximized before going
  fullscreen, so leaving fullscreen restores maximize rather than normal.
* `window_state()` returns a `WindowState` with the geometry base64-encoded from
  `saveGeometry()`, and `restore_window_state` reverses it. This is the value
  stored in a `.gpls` when `playlist/save_window` is on.
* `changeEvent` implements `pause_minimized` by snapshotting `.unpaused` before
  minimizing and unpausing exactly those on restore.
* `closeEvent` is the shutdown ordering, and it is deliberate:

```python
def closeEvent(self, event):
    # Ask about unsaved changes while the window is still up, then take it
    # off screen before closing the playlist: closing it releases the video
    # players, and a hardware video output takes a moment to let go, which
    # the user would otherwise sit and watch happen pane by pane.
    if not self._ctx.commands.check_playlist_save():
        event.ignore()
        return True
    self.parent().hide()
    self._ctx.commands.force_close_playlist()
    self.closing.emit()
    force_terminate()
```

The sequence **ask → hide → close → terminate** is asserted in
`tests/test_close_teardown.py`. Two reasons it is this order: the save prompt must
appear while the window is still visible, and hiding first means the slow release
of hardware video outputs is not watched pane by pane.

## Themes

`params/theme.py` provides the theme names, a Fusion style factory, and
`apply_theme()`. `init_app()` applies it at startup and connects
`app.paletteChanged` plus a system-theme watcher
(`utils/darkmode.watch_system_theme`) so a light/dark switch is followed live.
Per-platform watchers exist for Linux (portal) and Windows (registry) in
`utils/darkmode_linux.py` and `utils/darkmode_windows.py`.

`player/color_scheme` selects System / Light / Dark; changing it in the settings
dialog calls `apply_theme()` immediately (`SettingsManager._apply_settings`) with
no reload needed.

## Gotchas

* **Filter order is load-bearing.** Read the comments in
  `player/player.py:229` before inserting a manager into either list.
* **Never reject a drag.** Accept, then decide on the next event-loop turn.
* **Emit `layout_changed` after any model change**, or the grid will not rebuild.
* **Go through `layout_drop`** to rearrange videos; direct `Layout` manipulation
  desynchronises model and widgets.
* **Widget coordinates vs. global coordinates.** Drag and pan use global positions
  because `QCursor.pos()` is stale during X11 XDND — several comments say so.
  Use `event_object.mapToGlobal(event.pos())` rather than the cursor.
* **`is_modal_open()` guards almost every input handler.** Keep that check when
  adding one; without it, dragging a dialog across the grid triggers video
  actions.
* **A `QWidget` parent must outlive its manager.** `ManagersManager` passes
  `parent=self` (the `Player`), and tests that build a manager standalone must
  hold their own parent (see [testing.md](testing.md)).
* **Do not call `translate()` at import time** in anything the window pulls in —
  see [testing.md](testing.md) and `AGENTS.md`.

## Key files

| File | Role |
| --- | --- |
| `player/player.py` | The window; manager registration, connections, filter order. |
| `player/managers/grid.py` | Grid rebuild, modes, size, layout drops. |
| `player/managers/video_blocks.py` | Block registry and broadcast operations. |
| `player/managers/active_block.py` | Which cell is active; `cmd_active`; `is_active_*`. |
| `player/managers/drag_n_drop.py` | Drag start, drop targeting, fake drag. |
| `player/managers/single_mode.py` | Hide-all-but-one. |
| `player/managers/pan.py` | Moving the picture inside a cell. |
| `player/managers/mouse_hide.py` | Cursor auto-hide. |
| `player/managers/window_state.py` | Window sizing, fullscreen, close. |
| `utils/layout/flow_layout.py`, `grid_layout.py`, `base.py` | Layout models. |
| `utils/drop_zone.py` | Drop zone geometry and indicator choice. |
| `widgets/video_block.py` | The cell widget. |
| `widgets/video_overlay.py`, `video_overlay_elements.py` | On-video chrome. |
| `widgets/empty_cell.py` | Placeholder cells. |
| `params/theme.py` | Themes and style. |
