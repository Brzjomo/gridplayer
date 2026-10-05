# Boot and lifecycle

The exact order in which the application comes up, and how it goes down. Getting
this wrong produces failures that only appear on a machine where VLC is not where
you expect, or only on one platform, so the ordering constraints here are all
documented in the code and restated here.

## Startup, step by step

`gridplayer/__main__.py`:

```python
def main():
    freeze_support()          # 1
    init_cli_args()           # 2
    init_app_env()            # 3
    init_log()                # 4
    sys.excepthook = excepthook  # 5

    # MacOS has OpenFile events
    if not env.IS_MACOS:
        exit_if_delegated()   # 6

    log_environment()         # 7
    ret = run_app()           # 8
    sys.exit(ret)
```

Note the module-level import ordering at the top of the file — before `main()` is
ever called:

```python
import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)

import sys
from multiprocessing import freeze_support

from gridplayer.main.init_app_env import init_app_env
...
```

There is a deliberate `warnings.filterwarnings` **before** the GridPlayer imports,
and then an `E402`-ignoring import block after it (allowed by
`pyproject.toml:130`, `"__main__.py" = ["E402"]`). Anything newly imported at
module scope here is imported before `init_log()`, `init_cli_args()` and
`init_app_env()` run — so it must not need a configured logger, `sys.argv`, or the
environment variables those set. `tests/test_translation_timing.py` fails the
build if such a module also calls `translate()` at import time.

### 1. `freeze_support()`

Makes a frozen (PyInstaller) build safe to use `multiprocessing`. It must be the
first thing, before any process is spawned.

### 2. `init_cli_args()` — `main/init_cli.py`

`argparse` with `parse_intermixed_args`, so options and files can interleave.
Effects, in order:

* A bare `--` is split off **by hand before parsing**, because
  `parse_intermixed_args` on Python < 3.12 rejects `--` followed only by
  dash-prefixed names. Everything after `--` is treated as a file however it
  looks.
* **`sys.argv` is rewritten** to `[sys.argv[0], *args.files, *files_after_dashdash]`.
  Everything downstream reads `sys.argv[1:]` as the file list. The original
  command line does not survive — do not rely on it later.
* `--user-data-dir` sets `os.environ[GP_USER_DATA_DIR]`.
* `--platform` sets `env.QT_PLATFORM` (Linux only).

Why this must come before `init_app_env`: `init_app_env` builds a `QApplication`,
and both the data directory (needed by `init_log`) and the Qt platform plugin
choice must be settled first.

### 3. `init_app_env()` — `main/init_app_env.py`

```python
def init_app_env():
    if env.IS_LINUX:
        # Hardware video is drawn by VLC into an X11 window, not by Qt OpenGL.
        # If the xcb plugin loads libGL here, forked decoder processes inherit
        # a broken GLdispatch and abort on the second Hardware process:
        #   DispatchCurrentUnref: Assertion `dispatch->currentThreads >= 0'
        os.environ.setdefault("QT_XCB_GL_INTEGRATION", "none")

    if env.IS_WINDOWS:
        from PyQt5.QtWinExtras import QtWin
        QtWin.setCurrentProcessExplicitAppUserModelID(__app_id__)

    init_app_env_id()

    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps)
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
```

Three ordering facts:

1. **`QT_XCB_GL_INTEGRATION` must be set before any Qt object exists**, and it is
   a hard requirement for the hardware decoder on Linux. The symptom if it is
   wrong is an abort on the *second* hardware video, not the first.
2. **The Windows AppUserModelID must be set before the first window**, or taskbar
   grouping and the icon are wrong.
3. **The high-DPI attributes must be set before the `QApplication` is
   constructed** — Qt only honours them at construction time. That is why they
   live here and not in `init_app`.

`QApplication.setApplicationName` / `DisplayName` / `OrganizationName` /
`ApplicationVersion` are set here too. These are not cosmetic: `QStandardPaths`
uses them to compute the AppData location, which is where settings, logs and
cookies go. Setting them late moves the data directory.

### 4. `init_log()` — `main/init_log.py`

Resolves the log path from `get_app_data_dir()`, reads the three log-limit
settings, calls `log_config.config_log(...)`, and installs a Qt message handler.
Both a file handler and stderr are registered, at `DEBUG` handler level, with the
root level from `logging/log_level`.

`init_log` **must come after `init_cli_args`** (the data directory override) and
should come before anything that logs. Anything imported at `__main__` module
scope has already missed this — see the note above.

### 5. `sys.excepthook = excepthook`

`utils/excepthook.py` shows the exception dialog and logs the traceback. Set
early so a startup failure after this point is reported rather than silent.

### 6. `exit_if_delegated()` — single instance

```python
def exit_if_delegated():
    if Settings().get("player/one_instance") and is_delegated_to_primary(sys.argv):
        sys.exit(0)
```

**Skipped entirely on macOS**, and the comment gives the reason: macOS delivers
`OpenFile` events instead, handled by `MacOSFileOpenManager`. On other platforms:

* If another instance is already listening, this process sends it `sys.argv[1:]`
  and exits **before creating a QApplication**. So a second launch is cheap: no
  Qt, no VLC, no window.
* The channel is a `multiprocessing.connection.Listener` on a named pipe
  (`\\.\pipe\gridplayer-fileopen`) on Windows, or a Unix socket in
  `$XDG_RUNTIME_DIR/gridplayer/` elsewhere, or inside the Flatpak runtime dir
  under Flatpak.
* **`is_other_instance_running()` creates the listener as a side effect** — it
  sets the module-global `LISTENER`. On Linux it first probes an existing socket,
  and `_is_socket_working` unlinks a stale one that is not a socket or that
  refuses a ping. So a crashed instance does not lock out future launches.
* `player/one_instance` gates all of it, and it is read **here**, before the main
  settings are otherwise in play.

This is why the settings are read this early: `Settings()` is already functional
after `init_log`, because it only needs the data directory.

### 7. `log_environment()`

Logs versions and environment facts, so a bug report's log starts with them.

### 8. `run_app()` — `main/run.py`

```python
def run_app():
    app = init_app()

    try:
        vlc_version, vlc_python_version = init_vlc()
    except FileNotFoundError:
        QCustomMessageBox.critical(...)
        return 1

    env.VLC_VERSION = vlc_version
    env.VLC_PYTHON_VERSION = vlc_python_version

    # Need to postpone import parts that depend on vlc
    # because python-vlc loads VLC DLL on import
    # and we need to set environment vars before that
    from gridplayer.player import Player

    player = Player()
    app.installEventFilter(player)

    if sys.argv[1:]:
        player.process_arguments(sys.argv[1:])

    if Settings().get("player/start_fullscreen"):
        player.showFullScreen()
    elif Settings().get("player/start_maximized"):
        player.showMaximized()
    else:
        player.show()

    # force load main app icon for Windows 11, sometimes it doesn't load if called before show
    if env.IS_WINDOWS:
        init_icon(app)

    return app.exec_()
```

**The single most important line in the project** is the `from gridplayer.player
import Player` sitting *inside* `run_app`, after `init_vlc()`. Importing python-vlc
loads the VLC shared library, and the environment variables pointing at an
embedded VLC must be set before that. Anything that pulls in `gridplayer.player`
at module scope breaks this on every platform where VLC is not on the default
search path.

#### `init_app()` — `main/init_app.py`

```python
def init_app():
    if env.QT_PLATFORM:
        app = QApplication([*sys.argv, "-platform", env.QT_PLATFORM])
    else:
        app = QApplication(sys.argv)

    follow_desktop_cursor()

    init_resources()

    app.setStyle(create_fusion_style())

    app.setAttribute(Qt.AA_DisableWindowContextHelpButton)
    app.styleHints().setShowShortcutsInContextMenus(True)

    apply_theme(app)
    app.paletteChanged.connect(lambda: on_system_theme_changed(app))
    watch_system_theme(lambda: on_system_theme_changed(app), app)

    init_icon(app)

    app.setFont(QFont("Hack", FONT_SIZE_MAIN))

    init_translator(app)

    return app
```

Ordering notes:

* The **Qt platform is passed as an argument** (`-platform xcb`) rather than via
  `QT_QPA_PLATFORM`, which `_detect_qt_platform` deliberately ignores because the
  whole desktop often sets it to `wayland` (`params/env.py:31`).
* `init_resources()` must precede anything that looks up a themed icon: it
  appends `gridplayer/resources/icons` to `QIcon.themeSearchPaths()` and registers
  every `.ttf` under `resources/fonts`. The "Hack" font set as the application
  font is registered here — set the font before this and it silently falls back.
* `init_translator(app)` is last. Everything built before it is untranslated, which
  is precisely the trap `tests/test_translation_timing.py` guards.
* `init_icon(app)` is called here and **again** in `run_app` on Windows, with the
  comment that Windows 11 sometimes fails to load the taskbar icon if it is set
  before `show()`.

#### `init_vlc()` — `utils/libvlc.py`

Two phases:

1. **Find an embedded VLC and set the environment for it.** `_get_embed_vlc_root`
   returns a root for PyInstaller (`sys.executable/../libVLC`), Snap
   (`$SNAP/usr/lib/x86_64-linux-gnu`), AppImage (`$APPDIR/usr/lib`) and Flatpak
   (`/app/lib`), or `None`. `_get_embed_vlc_paths` then maps that to the
   platform's plugin directory and library filename. If both exist, it sets
   `PYTHON_VLC_MODULE_PATH` and `PYTHON_VLC_LIB_PATH`; otherwise it logs "will try
   to find system VLC".
2. **Import `gridplayer.vlc_player.vlc` and ask it for the version.**

```python
try:
    with importing_embed_vlc():
        from gridplayer.vlc_player import vlc
except (OSError, NotImplementedError):
    log.exception("Failed to load VLC")
    raise FileNotFoundError
```

`FileNotFoundError` is the contract, and `run_app` catches exactly it to show the
"VLC player is required!" message box and return exit code 1. Note it also catches
`NotImplementedError` — that is what the vendored binding raises when it cannot
find the library at all.

The version is stored in `env.VLC_VERSION` / `env.VLC_PYTHON_VERSION` for the
About dialog and the log.

## Single instance in detail

| Platform | Channel | Delegation |
| --- | --- | --- |
| Windows | `\\.\pipe\gridplayer-fileopen` (`AF_PIPE`) | Yes |
| Linux (normal) | `$XDG_RUNTIME_DIR/gridplayer/gridplayer-fileopen.socket` (`AF_UNIX`) | Yes |
| Flatpak | `$XDG_RUNTIME_DIR/app/<FLATPAK_ID>/gridplayer/gridplayer-fileopen.socket` | Yes |
| macOS | — | No; `OpenFile` events instead |

The listener is a **daemon thread** (`single_instance.Listener.start`), so it never
keeps the process alive. `_handle_input_data` treats the string `"ping"` as a
liveness probe and a non-empty list as files to open, emitting `open_files`.
`cleanup()` sends `None` to break the accept loop and joins the thread.

`is_the_only_instance()` is a macOS-only counter over `NSWorkspace` running
applications with the same bundle id.

## Shutdown

Shutdown is a chain, and it is **not** a normal Qt teardown at the end.

```text
window close
  └── WindowStateManager.closeEvent            window_state.py:75
        ├── check_playlist_save()  ── False ──► event.ignore(), nothing else happens
        ├── parent().hide()                    window off screen first
        ├── force_close_playlist()             release the video players
        ├── closing.emit()
        └── force_terminate()                  misc.py:32
              ├── force_terminate_children_all()
              │     ├── force_terminate_resource_tracker()
              │     └── force_terminate_children()
              └── os._exit(0)
```

**`force_terminate` ends the process with `os._exit(0)`** — no Qt teardown, no
`atexit`, no destructors. That is intentional: the decoder processes and the
VLC instances are torn down explicitly beforehand, and a graceful Qt shutdown
would be slower and less predictable. Consequences:

* Anything relying on a destructor, `__del__`, or an `atexit` hook will **not**
  run. Register cleanup with `closing` or do it in the manager's own `cleanup()`.
* `ProcessManager.cleanup()` and `StreamProxyManager.cleanup()` are driven by the
  signals in `player/player.py`, not by the exit path.

The ordering inside `closeEvent` is load-bearing (comment at `window_state.py:76`):

> Ask about unsaved changes while the window is still up, then take it off screen
> before closing the playlist: closing it releases the video players, and a
> hardware video output takes a moment to let go, which the user would otherwise
> sit and watch happen pane by pane.

`force_terminate_resource_tracker` is called first on non-Windows platforms "so it
won't complain about leaked resources", and it kills the tracker with `SIGKILL`
via `_resource_tracker._pid`.

Tests assert this sequence (`tests/test_close_teardown.py`):
`["ask", "hide", "close"]`, and that a cancelled save calls neither
`force_close_playlist` nor `force_terminate`.

## Teardown during a run

Not every release is a process exit. Three separate mechanisms exist:

| Trigger | Mechanism |
| --- | --- |
| Changing the video driver or videos-per-process | `settings.reload` signal → `video_blocks.reload_all_closed` → `video_driver.cleanup()` drops the whole process pool. |
| Closing one cell | `VideoBlock.cleanup_start()` then `cleanup()`; the frame's `cleanup_start` is idempotent, and `cleanup_wait` is what actually blocks. |
| Closing everything | `VideoBlocksManager.close_all()` runs `_start_cleanup` over every block **before** waiting on any. |

The last one has a stated rationale (`video_blocks.py:95`):

> Closing a block waits for its player to let go of the video output, which takes
> a noticeable moment in hardware mode. Done block by block that wait is serial,
> and closing a full grid reads as the videos going dark one after another.
> Starting them all first makes the waits overlap.

Any new bulk teardown should follow the same shape: **start all, then wait all.**

The hardware frame additionally waits up to `HW_PLAYER_RELEASE_TIMEOUT_S = 5` for
the child to confirm it has dropped the X drawable (`player_released`), and
`detach_hw_xwindow` stops playback and zeroes the handle *before* the Qt window is
destroyed, because GPU drivers cannot tolerate a stale handle even though libVLC
can. See [playback-and-vlc.md](playback-and-vlc.md).

## Orphan prevention

Decoder processes must not outlive the parent, and this is defended twice on
purpose:

* **Parent side** — `create_kill_on_close_job()` (`multiprocess/job_object.py`)
  makes a Windows job object with kill-on-close and assigns every child PID to it.
  `ProcessManager.cleanup()` waits 5 seconds, force-terminates whatever is left,
  then closes the job.
* **Child side** — `InstanceProcess._guard_against_orphaning` plus
  `multiprocess/parent_death_signal.py`, so a parent killed outright (SIGKILL, a
  crash, a debugger stop) still does not leave a decoder running.

The code comments acknowledge the gap being covered
(`process_manager.py:94`): a crash in the sliver between `process.start()` and
`_job.assign(pid)` would leave the child outside the job, and the child's own
watchdog covers that case.

`ProcessManager.cleanup` also terminates the command loop and the log listener,
in that order, and `crash_all` force-terminates every child before emitting the
`crash` signal.

## Environment and paths

`params/env.py` computes platform facts **once at import**:

```python
IS_LINUX = sys.platform.startswith("linux")
IS_MACOS = sys.platform == "darwin"
IS_WINDOWS = sys.platform == "win32"
```

with a comment explaining the choice: `platform.system()` asks WMI on Windows,
which "holds up every startup by a few dozen milliseconds to say what
`sys.platform` already does". Use these flags rather than re-deriving them.

Also computed there: `IS_PYINSTALLER` (`sys.frozen`), `IS_SNAP`, `IS_APPIMAGE`,
`IS_FLATPAK`, `IS_KDE`, `QT_PLATFORM`, `PYINSTALLER_LIB_ROOT`, `RESOURCES_DIR`,
and `FLATPAK_RUNTIME_DIR` (which is *created* if inside a Flatpak). `VLC_VERSION`
and `VLC_PYTHON_VERSION` start as `None` and are filled in by `run_app`.

`RESOURCES_DIR` resolves differently when frozen, and the comment says why:

> importlib.resources is unreliable there because the package lives in the PYZ
> archive.

So a frozen build reads resources from `sys._MEIPASS/gridplayer/resources`.

## The data directory

`get_app_data_dir()` (`utils/app_dir.py:37`), in priority order:

1. `GP_USER_DATA_DIR` — from `--user-data-dir` or the environment; created if
   missing. Relative paths resolve against the **current working directory**.
2. **Portable mode**, Windows frozen builds only: a `portable_data` directory
   beside the executable, if one exists.
3. `QStandardPaths.AppDataLocation` — created if missing.

This is why setting the application name/organisation before the first window
matters: it is what `AppDataLocation` is built from.

## What runs where

| Thing | Thread / process |
| --- | --- |
| Qt widgets, all managers, dialog interaction | main thread |
| URL resolution | a `QThread` per resolve, abandoned if busy at close |
| Single-instance listener | a daemon thread |
| SponsorBlock lookups | a daemon thread, cached |
| Stream proxy server | a `QThread` running a `ThreadingHTTPServer` (a thread per request) |
| Checkups | dialog-driven, with their own workers |
| Decoder processes | separate processes, one `vlc.Instance` each, N players per process |

## Boot order rules to remember

1. `freeze_support()` first.
2. CLI parsing before anything that reads the data directory or Qt platform.
3. `QT_XCB_GL_INTEGRATION`, high-DPI attributes and the application identity
   before the `QApplication` exists.
4. Logging configured before anything that logs.
5. Single-instance check **before** creating a `QApplication`, so a delegated
   launch costs nothing.
6. `init_resources()` before any themed icon or the application font.
7. `init_translator()` before any UI text is created.
8. **`init_vlc()` before importing `gridplayer.player`.**
9. `Player()` before `process_arguments()`, and before `show()`.

## Key files

| File | Role |
| --- | --- |
| `__main__.py` | The top-level sequence. |
| `main/init_cli.py` | Argument parsing and `sys.argv` rewriting. |
| `main/init_app_env.py` | Pre-Qt environment and attributes. |
| `main/init_log.py` | Logging setup. |
| `main/run.py` | VLC init, `Player` construction, `exec_()`. |
| `main/init_app.py` | QApplication, resources, theme, font, translator. |
| `main/init_translator.py` | Qt and app translations. |
| `main/init_resources.py`, `main/init_icons.py` | Icon theme, fonts, window icon. |
| `utils/libvlc.py` | Embedded-VLC discovery and the `FileNotFoundError` contract. |
| `utils/single_instance.py` | Delegation to a running instance. |
| `utils/app_dir.py` | The data directory. |
| `utils/misc.py` | Force termination and child cleanup. |
| `player/managers/window_state.py` | The close sequence. |
| `multiprocess/job_object.py`, `parent_death_signal.py` | Orphan prevention. |
