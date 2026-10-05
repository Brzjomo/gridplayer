# Development

How to get a working checkout, run the thing, and debug it.

## Prerequisites

| Tool | Needed for | Note |
| --- | --- | --- |
| **Python 3.10+** | running at all | `requires-python = ">=3.10"` in `pyproject.toml`. CI tests 3.10 and 3.14. |
| **uv** | dependency management | `uv sync`, `uv run`. |
| **just** | task runner | `just generate-ui`, `just build-*`. Not required for plain development. |
| **VLC** | playback | System VLC on Windows/macOS; `libvlc5`/`vlc-plugin-base` on Linux. The app refuses to start without it. |
| **Deno / Node / Bun / QuickJS** | YouTube links only | Optional. See `README.md` → *JavaScript runtime*. |

This checkout has `uv` and Python 3.10.19 on `PATH`, but **not** `just` and **not**
a `.venv`. Run `uv sync` first; anything needing `just` needs it installed
separately.

## Setup

```bash
uv sync                       # create .venv and install everything, incl. dev group
uv run gridplayer             # run from source
```

`uv run` uses the project environment without activating it. If you prefer an
activated shell, `uv venv` + `.venv\Scripts\activate` (Windows) or
`source .venv/bin/activate` works too.

## Running the app

```bash
uv run gridplayer                     # empty window
uv run gridplayer video1.mp4 video2.mp4
uv run gridplayer playlist.gpls
uv run gridplayer https://example.com/stream
```

### Command line options

Parsed in `main/init_cli.py` with `argparse` and `parse_intermixed_args`, so
options and files may interleave. Positional arguments become media files,
`.gpls` playlists or URLs. Anything starting with `-` that is not a known option
is **rejected** — use `--` to pass a dash-prefixed filename.

| Option | Effect |
| --- | --- |
| `--user-data-dir PATH` | Sets `GP_USER_DATA_DIR`; settings and log live here. Relative paths resolve against the CWD. |
| `--platform {auto,xcb,wayland}` | **Linux only.** Overrides the detected Qt platform plugin. `auto` picks `xcb` when an X server is present, else `wayland`. |
| `--version` | Prints version and exits. |
| `--help` | Prints help and exits. |

Note what `init_cli_args` does to the process: it **rewrites `sys.argv`** to
`[sys.argv[0], *files]`, moving post-`--` entries to the end
(`main/init_cli.py:60`). Everything downstream reads `sys.argv[1:]` as the file
list — `main/run.py:41` and `Player.process_arguments`. Do not expect the
original argv to survive.

The `--` split is done by hand *before* parsing, because `parse_intermixed_args`
on Python < 3.12 rejects a bare `--` with only dash-prefixed names after it.

### Environment variables

| Variable | Effect |
| --- | --- |
| `GP_USER_DATA_DIR` | Same as `--user-data-dir`; the command line wins. `utils/app_dir.py:23`. |
| `QT_QPA_PLATFORM` | Respected by Qt, but **not** consulted by `_detect_qt_platform()` — see the comment at `params/env.py:31`. On Linux the app forces `xcb` unless there is no `DISPLAY`. |

### Running against a scratch data directory

The single most useful development habit — it keeps your real settings, cookies
and recent list out of the way:

```bash
uv run gridplayer --user-data-dir ./scratch_data
```

Everything lands there: `settings.ini`, `gridplayer.log`, `cookies.txt`,
screenshots.

## Where the files are

`get_app_data_dir()` (`utils/app_dir.py:37`) resolves in this order:

1. `GP_USER_DATA_DIR` (from `--user-data-dir` or the environment) — created if
   missing.
2. **Windows portable build only:** a `portable_data` folder beside the
   executable, if it exists (`is_portable()`).
3. `QStandardPaths.AppDataLocation` — i.e.
   `%APPDATA%\vzhd1701\GridPlayer`,
   `~/.local/share/vzhd1701/GridPlayer` (or `$XDG_DATA_HOME`), or
   `~/Library/Application Support/vzhd1701/GridPlayer`.

**Settings → Open data folder** opens it from inside the app.

## Logging

`init_log()` (`main/init_log.py:9`) configures logging at startup, *before* the
QApplication exists:

* The log file is `<data dir>/gridplayer.log`.
* A rotating handler is used when `logging/log_limit` is on, sized by
  `logging/log_limit_size` (MiB) with `logging/log_limit_backups` backups.
* **Both a file handler and stderr** are installed, at `DEBUG` handler level,
  with the root logger level from `logging/log_level`.
* Qt messages are routed through a `qInstallMessageHandler` into the `"QT"`
  logger (`utils/log_config.py:46`), with known-noisy messages filtered by
  `QT_LOG_IGNORED`.

Log line format includes the process id:

```text
2026-01-01 12:00:00,000 (12345) | GridManager | DEBUG | message
```

That PID is how you tell a main-process line from a decoder-process line.

### Seeing VLC's own output

VLC's log is **off by default** (`logging/log_level_vlc` defaults to
`DISABLED = logging.CRITICAL + 1`, `utils/log_config.py:11`). Turn it up in
*Settings → Logging* → *VLC log level*. The callback that forwards it is
`InstanceVLC.libvlc_log_callback`; messages are tagged with the VLC module name
and file:line, and when the app's own level is `DEBUG` you also get the VLC
source context line.

Increasing `logging/log_level_vlc` takes effect on **running** decoder processes
(`ProcessManagerVLC.set_log_level_vlc` forwards to every active instance), so you
do not have to reload videos.

### Debugging a decoder process

The decoder processes are separate interpreters. To debug them:

* Raise `logging/log_level` to `DEBUG` — child log records are forwarded to the
  parent's handlers (through a queue on spawn-based platforms; inherited
  directly when the start method is `fork`).
* The children are started by `ProcessManager`, so an IDE "attach to subprocess"
  setup works, but the practical approach is log-based.
* A child crash is reported through the `crash` signal and turned into a
  `PlayerException` by `VideoDriverManager.crash`
  (`player/managers/video_driver.py:89`), which reaches the global excepthook
  and the exception dialog.

### Unhandled exceptions

`sys.excepthook = excepthook` is set in `__main__.main()` (`utils/excepthook.py`,
`dialogs/exception.py`). Anything that escapes shows the exception dialog and is
logged — useful in tests too, since it means a Qt-slot exception is not silently
swallowed.

## Linting and formatting

```bash
uv run ruff check .
uv run ruff format --check .
uv run rumdl check .            # markdown
```

The configuration lives in `pyproject.toml`. Two exclusions matter:

* `gridplayer/vlc_player/vlc.py` is excluded from Ruff entirely
  (`extend-exclude`) — it is vendored.
* `scripts/*` is excluded from linting.

`pre-commit` runs `ruff format`, `rumdl fmt` and `ruff check` (see
`.pre-commit-config.yaml`). Note the hooks are **not** read-only — `ruff format`
and `rumdl fmt` rewrite files.

CI (`test.yml`) lints with `ruff check`, `ruff format --check` and
`rumdl check`, then runs pytest on Python 3.10 and 3.14.

## Generated files — never edit by hand

| Generated | From | Regenerate |
| --- | --- | --- |
| `gridplayer/**/*_ui.py` | `resources/ui/*.ui` | `just generate-ui` |
| `gridplayer/resources/**` | `resources/` | `just generate-resources` |

`just generate-ui` runs `scripts/qt_resources/build_ui.sh`, which needs
`pyuic5` (from PyQt5). `just generate-resources` runs
`scripts/qt_resources/build_resources.sh` and `build_resources.py`, which needs
`pyrcc5` and, for translations, `lrelease` (`qttools5-dev-tools` on Ubuntu).

**Out-of-tree contributors:** `gridplayer/resources/` is committed, so you only
need these tools if you actually change a raw resource or a `.ui` file.

## Working on the UI

1. Edit the `.ui` file in `resources/ui/` with Qt Designer (or by hand — it is
   XML).
2. `just generate-ui`
3. The generated `*_ui.py` file is reimported by the dialog module. Widget names
   in the `.ui` file are the attribute names the Python code uses, so **renaming
   a widget in Designer breaks `settings_map` and every other reference** — grep
   the name first.

For the settings window specifically, most pages come from `.ui` and the
defaults forms are generated; see [settings-system.md](settings-system.md).

## Testing

```bash
uv run pytest                       # whole suite
uv run pytest tests/test_menu.py    # one file
uv run pytest -k bookmark           # by keyword
uv run pytest --cov                 # with coverage (pytest-cov installed)
```

The suite is large (~25k lines) and uses **offscreen Qt**: `conftest.py` sets
`QT_QPA_PLATFORM=offscreen` and points `DBUS_SESSION_BUS_ADDRESS` at a
nonexistent socket so the Linux dark-mode check fails fast instead of waiting out
a D-Bus timeout, twice, per test.

`conftest.py` also installs autouse fixtures that isolate the tests from your
machine:

* a session-wide `QApplication` (`_qapp_session`),
* `PlaylistSettings().clear()` before and after every test,
* a temporary `CookieStore` (`_no_real_cookies`), so a login you happen to have
  stored cannot change a resolution,
* `FakeSettings` for the network module (`_no_real_network_settings`), so your
  proxy setting cannot change a resolution either.

Full details in [testing.md](testing.md).

## Common development tasks

### Watch out for import order with VLC

`gridplayer.player` must not be imported before `init_vlc()`, because importing
python-vlc loads the VLC shared library, and `init_app_env` has to set
environment variables before that (`main/run.py:33`). If you add a module-level
import of anything under `gridplayer.player` into a module that is imported
earlier, you can break startup in a way that only shows on a machine where VLC is
not on the default search path.

Similarly, `init_app_env` sets `QT_XCB_GL_INTEGRATION=none` on Linux before the
QApplication is constructed; that must stay ahead of Qt.

### Adding a setting

See the recipe in [settings-system.md](settings-system.md#recipe-add-a-new-setting-end-to-end).

### Adding a menu item or shortcut

See the recipe in
[commands-and-actions.md](commands-and-actions.md#recipe-add-a-new-action).

### Trying a change to a video driver

The driver is chosen at startup and changing it requires a reload. Fastest loop:

```bash
uv run gridplayer --user-data-dir ./scratch_data
```

…then change the driver in Settings, or edit `scratch_data/settings.ini` directly
and restart. The four drivers are selected by name in
`VideoDriverManager._video_drivers`; the software ones are the easiest to debug
because frames go through shared memory rather than a native window.

### Reproducing a Linux display problem

`--platform xcb|wayland` forces the Qt platform plugin, which is the fastest way
to tell whether a rendering bug is platform-specific. Hardware video needs an X
drawable, so `wayland` plus the `vlc_hw` driver is expected to be degraded — see
`README.md`.

## Gotchas

* **`uv run pytest` picks up your real settings** unless a fixture isolates the
  module under test. If a test passes alone and fails in the suite (or vice
  versa), suspect leaked global state — `Settings`, `PlaylistSettings`, or a
  running server thread. The `serving` fixture exists for exactly that reason.
* **Never commit `settings.ini` or logs.** They live outside the repository by
  default; only the `--user-data-dir ./` habit puts them inside it, and
  `.gitignore` does not cover an arbitrary directory name you choose. Keep
  scratch data directories out of the tree, or add them to `.gitignore` locally
  without committing that change.
* **Line endings.** `ruff format` is configured with `line-ending = "lf"`, and
  pre-commit enforces LF via `mixed-line-ending --fix=lf`. On Windows, check your
  editor is not writing CRLF into Python files.
* **`git` state:** the repository's root `README.md`, `CONTRIBUTING.md` and this
  directory are the only prose; there is no separate wiki.

## Key files

| File | Role |
| --- | --- |
| `pyproject.toml` | Dependencies, Ruff config, build backend. |
| `justfile` | All build/generate recipes. |
| `.pre-commit-config.yaml` | Local hooks (ruff, rumdl) and hygiene hooks. |
| `.github/workflows/test.yml` | CI lint + test matrix. |
| `tests/conftest.py` | Shared fixtures and the isolation the suite relies on. |
| `main/init_cli.py` | CLI parsing and `sys.argv` rewriting. |
| `main/init_log.py`, `utils/log_config.py` | Logging setup. |
| `utils/app_dir.py` | Where the data directory is. |
