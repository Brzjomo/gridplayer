# Architecture

A whole-program mental model. Read this before changing anything structural.

## The shape of the program

```text
                       ┌──────────────────────────────────────────┐
   argv ──► init_cli ──┤ __main__.main()                          │
                       │  init_cli_args → init_app_env → init_log  │
                       └───────────────────┬──────────────────────┘
                                           ▼
                       ┌──────────────────────────────────────────┐
                       │ main/run.run_app()                        │
                       │  init_app()  → QApplication + theme + i18n│
                       │  init_vlc()  → load libVLC, or die        │
                       │  Player()    → the main window            │
                       └───────────────────┬──────────────────────┘
                                           ▼
   ┌───────────────────────────────────────────────────────────────────┐
   │ Player(QWidget, ManagersManager)                                  │
   │  21 managers, one per concern, all talking over a shared Context   │
   ├───────────────────────────────────────────────────────────────────┤
   │  grid          playlist      actions/menu      video_blocks        │
   │  active_block  drag_n_drop   single_mode       window_state        │
   │  snapshots     bookmarks     recent_list       pan/mouse_hide      │
   │  video_driver  stream_proxy  settings          dialogs/log         │
   └───────────────────────────────┬───────────────────────────────────┘
                                   │ owns N
                                   ▼
   ┌───────────────────────────────────────────────────────────────────┐
   │ VideoBlock  (one per video, a QWidget in the grid)                 │
   │  overlay · status · controls · bookmarks · sponsorblock            │
   └───────────────────────────────┬───────────────────────────────────┘
                                   │ picked by VideoDriver
                                   ▼
   ┌───────────────────────────────────────────────────────────────────┐
   │ VideoFrameVLC*   (hw / hw_sp / sw / sw_sp / dummy)                 │
   │  owns the render surface + a video_driver handle                   │
   └───────────────────────────────┬───────────────────────────────────┘
                                   │ commands ▼        ▲ events
                                   ▼                           │
   ┌───────────────────────────────────────────────────────────────────┐
   │ vlc_player.Player  ── in-process, or ──► child decoder process     │
   │  python-vlc ─► libVLC ─► video out / audio out                     │
   └───────────────────────────────────────────────────────────────────┘
```

## The three central ideas

### 1. Managers and the shared Context

`Player` is both a `QWidget` and a `ManagersManager`. It declares a dict of
managers in `player/player.py:40`:

```python
self.managers = {
    "video_driver": VideoDriverManager,
    "window_state": WindowStateManager,
    "video_blocks": VideoBlocksManager,
    "grid": GridManager,
    ...
}
```

`ManagersManager.init()` (`player/manager.py:80`) then runs a fixed four-phase
sequence:

1. `_init_managers_instances()` — instantiate each class as
   `manager_cls(context=self._context, parent=self)`, register its `commands`
   into the shared `Commands` registry, and put the instance in
   `self._managers_inst[name]`.
2. `_init_connections()` — walk the `self.connections` table and wire Qt
   signals to slots by *name*.
3. `_init_event_filters()` — install the managers named in `self.event_filters`
   as Qt event filters on the window.
4. `_init_managers()` — call `manager.init()` on every manager, in dict order.

Everything a manager wants to publish goes in a `commands` property
(`player/managers/base.py` gives each manager `self._ctx` and `self._log`):

```python
@property
def commands(self):
    return {
        "add_video_blocks": self.add_videos,
        "remove_video_blocks": self.remove_videos,
        "video_count_batch": self.batch,
        ...
    }
```

`_register_commands` **raises `ValueError` on a name collision between
managers** (`player/manager.py:143`). Command names are therefore a global
namespace: pick a distinctive name, and if you get a collision the app will not
start at all.

### 2. Commands, resolved by name

`Commands.resolve()` (`player/manager.py:24`) turns a *specification* into a
callable. It accepts four shapes:

| Spec | Meaning |
| --- | --- |
| `"cmd_name"` | the callable itself |
| `("cmd_name", arg1, arg2)` | call it with these arguments |
| `NOT(x)` / `AND(x, y)` / `OR(x, y)` | boolean composition, from `utils/command_helpers.py` |
| a plain value | used as-is |

This is what lets `params/actions.py` describe behaviour *declaratively* without
importing any of the objects involved. An action entry is data:

```python
"play_pause": {
    "title": ...,
    "icon": "play",
    "func": ("active", "play_pause"),          # → context.commands.active("play_pause")
    "enable_if": "is_any_videos_playable",
    "check_if": ("is_active_param_set_to", "is_paused", False),
},
```

There are two lookup namespaces:

* `self._context.commands` — the merged `Commands` registry built from every
  manager's `commands` property.
* `self._context` — a `Context` object holding *state* (`active_block`,
  `video_blocks`, `commands`, `grid_state`, …). `Context.__getattr__` **calls**
  a value if it is callable (`player/manager.py:56`), which is why
  `self._ctx.grid_state` yields the state object, not the method — and why
  `GridManager.__init__` has to stash `self._ctx.grid_state = self.grid_state`
  (`player/managers/grid.py:52`).

The `("manager.command", …)` dotted form is only used in the `connections`
table; `_get_manager_function` splits on `.` and treats the pseudo-manager
`s` as the `Player` itself (`player/manager.py:127`), which is how
`s.arguments_received` is wired.

### 3. Two-layer layout: widgets vs. layout models

The grid is deliberately split:

* **`utils/layout/`** holds `FlowLayout` and `GridLayout` — pure layout
  *models* that own ordering (`ids`, `cells`), capacity, drop semantics and
  shuffle. They contain no Qt widgets.
* **`player/managers/grid.py`** translates between those models and the real
  `QGridLayout` of `VideoBlock` widgets, and owns the mode switching
  (`AUTO_ROWS` / `AUTO_COLS` / `FIXED`).

`VideoBlocks` (`player/managers/video_blocks.py:24`) is a registry, not a list:
it keeps `_by_instance` (by widget id) and `_by_video_id` (by the video's
`id` string). Because `set_video()` can change a block's video in place, there
is an explicit `reindex(block)` to refresh the second index — used by the
replace-on-drop path (`GridManager._replace_cell_video`).

## The command path end to end

Taking "user picks *Play/Pause* from the context menu on cell 3" as the example:

1. `MenuManager.contextMenuEvent` fires (it is an event filter on the window,
   `player/managers/menu.py:14`). It resolves the cell under the cursor, sets
   the drop anchor, and builds the menu from `params/menu.py`.
2. Menu structure comes from `SECTIONS` / `SUBMENUS` in `params/menu.py`, which
   reference action **ids** as strings. `MenuManager._add_menu_items` looks each
   one up in `self._ctx.actions` and adds it.
3. Actions were built once, at startup, by `ActionsManager._make_actions()`
   (`player/managers/actions.py:288`) from the `ACTIONS` table in
   `params/actions.py`. Each is a `QDynamicAction`, not a plain `QAction`.
4. The menu gets a `QAction` *proxy* from `to_menu_action()`
   (`player/managers/actions.py:112`). This matters: the proxy carries the
   shortcut text for display with `Qt.WidgetShortcut` context, while the real
   action keeps its window-level shortcut. `enable_if` is applied to the proxy
   (grayed out) but the real action stays enabled so **shortcuts still fire** —
   the comment at `actions.py:91` says so explicitly.
5. Clicking calls `QDynamicAction.trigger` → `ActionsManager._run_action`, which
   guards re-entrancy with `self._invoking` and re-checks `is_enabled`.
6. The bound command runs. For most per-video actions the spec is
   `("active", "<method>")`, which calls `ActiveBlockManager.cmd_active`:

```python
def cmd_active(self, command, *args):
    if self.is_no_active_block:
        return None
    if not self.is_active_playable() and command not in LOADING_COMMANDS:
        if command not in QUIETLY_DROPPED_COMMANDS:
            self._log.debug(f"Dropped {command}, active video is not playable")
        return None
    return getattr(self._ctx.active_block, command)(*args)
```

So **every** `("active", X)` action ends up calling `VideoBlock.X()`, and is
silently dropped unless the active video is playable — except for the
allow-list `LOADING_COMMANDS` (`player/managers/active_block.py:44`), which
covers things you must be able to do *while* loading or after a network error
(switch quality, reload, close, choose audio/subtitles). If you add an action
that should work on a video stuck in an error state, **add its command name to
`LOADING_COMMANDS`**, or it will be dropped before it ever reaches the widget.

## Lining recordings up by sound

The one part of the program that is not about playing anything. It is small, has
no Qt and no VLC in its middle, and is documented properly in
[audio-alignment.md](audio-alignment.md); what belongs here is where it sits.

| File | Role |
| --- | --- |
| `dialogs/align_videos.py` | The dialog: a row per video, and **Align By Sound**. Asks for a reading per video and applies what comes back. |
| `widgets/sync_strip.py` | The sound of one video drawn over the reference's, against the clock the records are meant to share: the eye's fallback where the numbers are not to be trusted. |
| `utils/sync_align.py` | `AlignMeasure`: reading every video's sound on a `QThread`, one signal with all the readings at the end. |
| `vlc_player/audio_probe.py` | A player of its own, with no window and a dummy audio output, transcoding a stretch of a recording into a wav file. `AudioProbe.envelope()` is the only thing here that needs VLC. |
| `utils/sync_audio.py` | All of the arithmetic: samples in, a lag out, plus the cache of stretches already read (`SnippetCache`), the wav reading, and `Sound` (a reading placed by an offset). Touches neither VLC nor Qt, which is why most of its tests need no player. |

Nothing here shares a player with the grid. A player given audio callbacks has no
output left to give the sound to, so analysing what is being played would take
the sound away from the viewer; a second player, with `--aout=dummy`, does not.

## Threading model

| Thread | What runs there |
| --- | --- |
| Qt main thread | All widgets, all managers, all dialog interaction, VLC event handling for in-process players. |
| Worker `QThread`s | URL resolution (`utils/url_resolve/url_resolve.py`), network checkups, thumbnail/screenshot work, and reading a video's sound for the alignment dialog (`utils/sync_align.py`). They hand results back by signal. |
| `single_instance.Listener` thread | A daemon thread accepting connections on the instance socket; emits `open_files` into the main thread. |
| Child **processes** | The default decoder mode. One process per `VideoFrameVLC*`, each running its own `vlc_player.Player`; the main process talks to them over stdio. |
| `stream_proxy` server thread | The local HTTP server, when the proxy is active. |

Rule of thumb: **widgets are only ever touched from the main thread.** Anything
that must be done off-thread communicates back by a Qt signal.

## Processes and orphans

`VideoDriverManager` (`player/managers/video_driver.py`) decides which frame
class to build and maintains a pool of decoder processes sized by
`player/video_driver_players` (default 4 videos per process). Multiprocess
plumbing lives in `gridplayer/multiprocess/`:

| File | Role |
| --- | --- |
| `process_manager.py` | Starts and tracks child processes. |
| `instance_process.py` | The child side: sets up, runs the command loop. |
| `command_loop.py` | The stdio protocol: read a command, dispatch, write a reply. |
| `safe_shared_memory.py` | Shared-memory segment handling for video frames. |
| `parent_death_signal.py` | Ensures children die with the parent. |
| `job_object.py` | Windows job object, the Windows-specific orphan guard. |

Teardown is a known-fragile area: closing a block waits for its player to
release the video output, so `_start_cleanup()` in
`player/managers/video_blocks.py:95` starts *all* blocks releasing before
waiting on any of them. See [known-issues-and-risks.md](known-issues-and-risks.md).

## Persistence surfaces

GridPlayer writes four different kinds of state, and they are not
interchangeable:

| What | Where | Written by |
| --- | --- | --- |
| Application settings | `settings.ini` in the user data dir | `Settings` (`settings.py`) |
| Session overrides | in memory, and into `.gpls` | `PlaylistSettings` (`playlist_settings.py`) |
| Playlists | `.gpls` JSON files chosen by the user | `models/playlist.py` |
| Cookies | `cookies.txt` in the user data dir | `utils/cookies.py` |

The distinction between the first two is the subtlest thing in the codebase.
`Settings` is the *global default*; `PlaylistSettings` is a sparse overlay
recording what this session (and, if saved, this playlist) changed. Reads go
through `PlaylistSettings().get(key)`, which falls back to `Settings().get(key)`.
Managers that change a user-visible grid/playlist option usually write to
`PlaylistSettings` **and** update their own live state — see
`GridManager.cmd_set_grid_mode` (`player/managers/grid.py:286`), which does both.
Details in [settings-system.md](settings-system.md) and
[models-and-persistence.md](models-and-persistence.md).

## What is generated, and from what

| Generated | Source | Command |
| --- | --- | --- |
| `gridplayer/resources/**` (icons, fonts, `.qm`) | `resources/` | `just generate-resources` |
| `gridplayer/**/*_ui.py` | `resources/ui/*.ui` | `just generate-ui` |
| `gridplayer/dialogs/settings_dialog_ui.py` | `resources/ui/settings_dialog.ui` | `just generate-ui` |

`gridplayer/vlc_player/vlc.py` is **vendored**, not generated. Never edit it and
never let a formatter touch it — `pyproject.toml:71` excludes it from Ruff.

## Entry points, in order

| Function | File | Does |
| --- | --- | --- |
| `main()` | `__main__.py:19` | `freeze_support`, CLI parse, env, log, excepthook, single-instance delegation, `run_app()`. |
| `init_cli_args()` | `main/init_cli.py:12` | argparse; rewrites `sys.argv` to `[prog, *files]`; sets `GP_USER_DATA_DIR`. |
| `init_app_env()` | `main/init_app_env.py:24` | Qt env vars, high-DPI attributes, application identity. |
| `init_log()` | `main/init_log.py:9` | Rotating log to the data dir, plus a Qt message handler. |
| `run_app()` | `main/run.py:12` | `init_app`, `init_vlc`, construct `Player`, `exec_()`. |
| `init_app()` | `main/init_app.py:21` | `QApplication`, resources, Fusion style, theme, icon, font, translator. |

Note the ordering constraint documented at `main/run.py:33`: **`gridplayer.player`
is imported inside `run_app`, after `init_vlc()`**, because importing
python-vlc loads the VLC shared library, and environment variables must be set
first. Anything that would import the player package at module import time
breaks this.

See [boot-and-lifecycle.md](boot-and-lifecycle.md) for the full sequence and the
shutdown path.
