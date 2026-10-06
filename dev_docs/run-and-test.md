# Running and testing a checkout

`scripts/run_dev.cmd` runs the app, or the test suite, from a clone — with the
VLC that machine happens to have. It exists because the app loads libVLC as it
imports, so a checkout needs a library **and** its plugin directory pointed at
it before anything runs, and both the app and the VLC-backed tests need the
same two variables set the same way.

`scripts/build_win.cmd` is its opposite number for packaging: one command that
finds the Git Bash the build scripts are written in and runs them, needing
nothing installed but Git, Python and `uv`. See
[build-and-release.md](build-and-release.md).

Read together with [development.md](development.md), which is how the app is
run and debugged in general, and [testing.md](testing.md), which is what the
suite expects of the machine it runs on.

## One command, two jobs

```text
scripts\run_dev.cmd                        the app, from the checkout
scripts\run_dev.cmd media.mp4 more.mkv     the app, with those files
scripts\run_dev.cmd --tests                the whole test suite
scripts\run_dev.cmd --tests -k sync        the tests that match, to pytest
scripts\run_dev.cmd --help                 what it does with VLC, and where
```

Everything after `--tests` is handed to `pytest` unchanged; everything else is
handed to `gridplayer` unchanged. `--tests` has to be the **first** argument,
because that is what decides which of the two is run.

What the two do underneath, and what to run when the script is not what you
want:

| Script | Equivalent, with VLC configured by hand |
| --- | --- |
| `run_dev.cmd …` | `uv run gridplayer …` |
| `run_dev.cmd --tests …` | `uv run pytest …` |

## Configuration

There is nothing to configure to get started: on a clone with `uv` and Python,
`scripts\run_dev.cmd` is enough. `uv run` creates `.venv` and installs
everything on the first run, which needs the network once.

VLC is **discovered**, in this order, and the first one found is used:

| # | Where | For |
| --- | --- | --- |
| 1 | `PYTHON_VLC_LIB_PATH` **and** `PYTHON_VLC_MODULE_PATH` already set | CI, an IDE, or a harness that has its own VLC. Used as they are; the script sets nothing. |
| 2 | `GRIDPLAYER_VLC_DIR` | A VLC unpacked anywhere: the script sets both variables from this directory. |
| 3 | `%ProgramFiles%\VideoLAN\VLC`, then `%ProgramFiles(x86)%\VideoLAN\VLC` | A normal Windows installation. |
| 4 | `build\vlc-*\libvlc.dll` | The VLC the packaging script downloads and unpacks ([build-and-release.md](build-and-release.md)). |

The script prints the one it picked before anything else runs:

```text
run_dev: VLC D:\...\build\vlc-3.0.24
```

`GRIDPLAYER_VLC_DIR` is the one setting a developer is expected to touch, and
it is meant to be set per shell:

```bat
set GRIDPLAYER_VLC_DIR=C:\tools\vlc-3.0.24
```

The directory is a VLC tree as it comes out of the release zip: `libvlc.dll`
and `libvlccore.dll` in it, `plugins\` underneath. The version the packaging
script embeds — and therefore the one a checkout is developed against — is
pinned in `scripts/pyinstaller/build_win.sh`:

```text
https://get.videolan.org/vlc/3.0.24/win64/vlc-3.0.24-win64.zip
```

Only the app and the VLC-backed tests need any of this. `uv run pytest` on the
tests that do not touch a player, `uv run ruff check .` and `uv run rumdl check .`
need no VLC at all, and the script is not in their way.

## Constraints

These are the rules the script keeps. A change that breaks one of them is a
change to this document as well.

1. **Two jobs and no more.** It runs the app or the suite. Packaging,
   generating resources and translations stay in the `justfile` and CI.
2. **It writes nothing of its own.** No file is created or copied by the
   script: not a VLC, not a setting, not a log. What it runs writes where those
   tools normally do — `.venv` and uv's cache, `.pytest_cache` and
   `__pycache__` — and the application's own data directory is untouched
   unless it is asked for with `--user-data-dir`.
3. **VLC is discovered, never assumed.** The order in the table above is the
   whole of it, and **no machine-local absolute path may appear in the
   script** — a clone has to work on a machine nobody has seen.
4. **Both VLC variables, or neither.** python-vlc is handed a library *and* a
   plugin directory. A library without its plugins loads and then reads
   nothing and plays nothing, which is the silent failure
   [audio-alignment.md](audio-alignment.md) and
   [build-and-release.md](build-and-release.md) both describe from a release.
   The script sets the pair together or leaves the environment alone.
5. **The environment wins.** Variables set by whoever called the script are
   used as they are. The script never overrides a VLC that something else has
   already chosen.
6. **A missing VLC is loud.** Non-zero exit, the ways to fix it, and no
   attempt to run anything: never a libvlc load error three frames deep.
7. **Plain `cmd`.** It works on a bare Windows machine with `uv` and Python on
   `PATH`, and needs neither PowerShell, `just`, a POSIX shell nor an
   unpacker. It is also **LF-ending**, like every other file here
   (`.gitattributes`): keep it free of `goto` and `call :label`, which `cmd`
   reads unreliably without CRLF.
8. **It stays optional.** `uv run gridplayer` and `uv run pytest` keep working
   without it, given the two variables. Nothing may come to depend on the
   script being there.
9. **This document moves with it.** A new mode, or a change to the order VLC
   is found in, edits this file and the `--help` text in the same change.
   `tests/test_dev_run_script.py` holds the parts of it that can be checked
   without a machine to check them on.

## What it does not do

* It does not install VLC, unpack a zip, or download anything.
* It does not run only the tests that need VLC — it runs whatever `pytest`
  was asked for, and those tests skip themselves where there is no VLC
  (`needs_vlc`, see [testing.md](testing.md)).
* It is **Windows only**. Elsewhere the application finds a system libvlc on
  its own, and a checkout that needs pointing at a particular one exports the
  same two variables:

  ```bash
  export PYTHON_VLC_LIB_PATH=/usr/lib/x86_64-linux-gnu/libvlc.so.5
  export PYTHON_VLC_MODULE_PATH=/usr/lib/x86_64-linux-gnu/vlc/plugins
  uv run pytest
  ```

## Checking it

Three commands, and what each one proves:

```bat
scripts\run_dev.cmd --help
scripts\run_dev.cmd --version
scripts\run_dev.cmd --tests tests\test_sync_audio.py -q -k Probe
```

The first prints the order VLC is looked for in; the second runs the app from
the checkout, printing the version and exiting; the third runs the six tests
that read a stretch of sound off a recording end to end, which is the strongest
evidence available without clicking anything that **the VLC it found is a
usable one** — a VLC whose plugins are wrong loads, plays, and reads nothing,
and those six are what says so.

The parts of this document that can be checked without a machine are checked:
`tests/test_dev_run_script.py` holds the script to its ladder, to setting both
VLC variables together, to its LF endings, and to carrying no path from
anybody's machine.

Evidence that it picked the VLC it should have: the `run_dev: VLC …` line, and
the application's own log at debug level (`<data dir>\gridplayer.log`), where
`utils/libvlc.py` writes the VLC version it ended up with.
