# Testing

The suite lives in `tests/` (~25k lines, 150+ files) and drives the real
application rather than reimplementing it. This document covers how it is wired
up, the patterns to copy, and the traps.

## Running

```bash
uv run pytest                                  # everything
uv run pytest tests/test_menu.py               # one file
uv run pytest tests/test_menu.py::test_x       # one test
uv run pytest -k bookmark                      # by keyword
uv run pytest --cov                            # coverage (pytest-cov)
uv run pytest -x                               # stop at first failure
```

Dependencies (`pytest`, `pytest-cov`, `pytest-mock`) are in the `dev` dependency
group. `mocker` comes from `pytest-mock` and is used heavily — it is the
`unittest.mock` patcher with automatic teardown.

## Environment setup: `conftest.py`

`tests/conftest.py` is short and every line of it is load-bearing. Read it before
writing a test that touches anything global.

### Environment, set at import time

```python
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=/nonexistent/gridplayer-tests"
```

* **Offscreen Qt.** A test that shows a widget would otherwise put a real window
  on the developer's desktop.
* **A dead D-Bus address.** On Linux the dark-mode check asks the desktop portal
  over the session bus; on a machine with a bus but no portal (a login over ssh)
  every ask waits out the 25-second D-Bus timeout — twice per test. Pointing at a
  socket that is not there fails immediately.

These are both set with `setdefault`/direct assignment at module import, so they
take effect before any GridPlayer module imports Qt.

### Session fixtures

| Fixture | Scope | Purpose |
| --- | --- | --- |
| `_qapp_session` | session, autouse | One `QApplication` for the whole run. Destroying a `QApplication` destroys every `QObject` alive at the time — including the `QSettings` the settings singleton holds — so exactly one is kept for the session and modules that ask for their own get it back. |

### Autouse isolation fixtures

These exist specifically so a test cannot depend on, or damage, the developer's
machine. Understand them before writing anything that reads settings, cookies or
the network:

| Fixture | What it isolates |
| --- | --- |
| `_clear_playlist_settings` | Calls `PlaylistSettings().clear()` **before and after every test**, so a session override cannot leak between tests. |
| `_no_real_cookies` | Monkeypatches `cookies.cookie_store` to a `CookieStore` in `tmp_path`. Without it, a test that resolves a URL would resolve differently on a machine that happens to have a login stored. |
| `_no_real_network_settings` | Monkeypatches `network.Settings` with a `FakeSettings` page of defaults, so a proxy configured on the machine cannot change a resolution. |

### Helper classes and fixtures

* **`FakeSettings(values)`** — a dict-backed stand-in for the app-wide settings
  singleton. Answers `get` and `sync_get` only, which is all the modules it is
  patched into need.
* **`FakeStreamlinkSession`** — enough of a Streamlink session for settings to
  land on and for a stream to be built. Crucially it *records* what it was told
  (`http.proxies`, `http.verify`, `http.timeout`, `address_family`,
  `http.headers`) so a test can tell "setting applied" from "setting cleared
  again".
* **`serving`** — runs any object with `serve_forever` on a daemon thread and
  shuts it down at teardown, using `SHUTDOWN_POLL_SEC = 0.01` so shutdown is fast
  (the stdlib default poll interval of 0.5s would dominate the suite's runtime).
  Every proxy test needs at least two servers at once, and a test that leaves one
  running leaks a thread into the next.
* **`local_login`** — stores a cookie for `127.0.0.1` (where the test HTTP hosts
  live) and pins `cookies/enabled` + `cookies/allow_update`, so a test is not at
  the mercy of whatever the developer has cookies set to. Uses the shared
  `LOGIN`/`LOGIN_NAME`/`LOGIN_VALUE` constants that the test hosts check for.

## The dominant testing patterns

### 1. Manager-under-test with a synthetic Context

Managers are constructed directly with a hand-made `Context` (or a
`SimpleNamespace`), never through a real `Player`. From
`tests/test_managers_playlist.py:77`:

```python
def _make_manager(ctx=None):
    parent = QWidget()
    manager = PlaylistManager(
        context=_without_bookmarks(SimpleNamespace() if ctx is None else ctx),
        parent=parent,
    )
    ...
```

Two rules this illustrates:

* **Hold the parent.** The docstring in `tests/test_close_teardown.py:38` says it
  plainly: if the parent `QWidget` is dropped, Qt takes the manager down with it
  and every later use raises about a deleted C++ object. Keep a reference for the
  length of the test and `deleteLater()` at the end.
* **Stub only the commands you exercise.** `_without_bookmarks` fills in the few
  `ctx.commands` entries `PlaylistManager` calls, as plain lambdas. This is how
  a manager is tested without building its collaborators.

Managers need `commands` for `Commands.resolve` to work; a helper like
`test_close_teardown.py:_window_state_manager` builds a real `Commands()` and
`.update()`s it with a dict.

### 2. Patching the settings singleton

Because `Settings()` is a module-level singleton reading the developer's real INI,
tests either:

* patch it with the shipped defaults —

  ```python
  mocker.patch.object(Settings(), "get", side_effect=_default_settings.__getitem__)
  ```

  (`tests/test_managers_playlist.py:56`), which is the pattern to copy for
  anything that reads many settings; or
* set the specific key through `PlaylistSettings().set(...)`, which is cleaned up
  automatically by the autouse fixture.

There is also a dedicated test that the INI round-trip works at all
(`tests/test_settings.py`), which asserts against `UnsavedChangesMode.DISCARD.value`
to pin the on-disk representation.

### 3. Faking libVLC objects

`vlc_player` code is tested by handing it objects that look enough like the
binding's. `tests/test_vlc_player_tracks_manager.py` defines
`FakeMediaTrack`, `FakeUnion`, `FakePointer`, `FakeVideoContent` and so on — note
`FakePointer`, which mimics the `.contents` indirection the ctypes bindings use:

```python
class FakePointer:
    def __init__(self, contents):
        self.contents = contents
```

and `FakeMediaTrack.u = FakeUnion(video=FakePointer(video_content), ...)`.

`tests/test_vlc_player_chapters.py` and `tests/test_vlc_player_snapshot.py` go
further and set `player._media_player = _FakeMediaPlayer()` on a real player
object, so the class under test runs its own logic against a fake media player.
This is the pattern for testing `VlcPlayerBase` methods in isolation without a
VLC instance.

### 4. Subclassing to stub the boundary

When a class's job is to talk to something unreachable in a test, override the
single method that builds it:

```python
class _StubSWFrame(VideoFrameVLCSW):
    def driver_setup(self, vlc_options):
        return MagicMock()
```

(`tests/test_close_teardown.py:17`). The real `cleanup_start`/`cleanup`/
`cleanup_wait` orchestration is then exercised against a `MagicMock` driver, and
assertions are made on call counts:

```python
def test_frame_cleanup_start_is_idempotent():
    frame = _StubSWFrame(process_manager=MagicMock(), vlc_options=[])
    frame.cleanup_start()
    frame.cleanup()
    frame.video_driver.cleanup_start.assert_called_once()
    frame.video_driver.cleanup_wait.assert_called_once()
```

### 5. Recording order instead of asserting on mocks

For sequencing guarantees, tests append to a list and compare the whole list.
`test_close_teardown.py` proves the "start every player before waiting on any"
invariant this way:

```python
def test_close_all_starts_every_player_before_closing_any(blocks_manager):
    manager, with_blocks = blocks_manager
    events = with_blocks("a", "b", "c")
    manager.close_all()
    assert events == [
        ("start", "a"), ("start", "b"), ("start", "c"),
        ("close", "a"), ("close", "b"), ("close", "c"),
    ]
```

**Prefer this to `assert_called_before`-style checks** — the expected sequence is
readable and the failure message shows the actual order.

### 6. Walking the declarative tables

Because actions and menus are data, tests assert on the data. The
`tests/test_menu_*.py` files locate their submenu in `SECTIONS`, collect the
action ids, and assert properties:

```python
playback = _submenu(SECTIONS["video_all"], "[ALL]", "Playback")
for name in CHAPTER_ACTIONS:
    assert "key" not in ACTIONS[name], name
    assert "keys" not in ACTIONS[name], name
```

(`tests/test_menu_chapters.py:52`). This is how the tables are kept honest:
chapters deliberately have no shortcuts, and the test says so.

`tests/test_menu_audio_delay.py` and friends go further and *build* the action
through the real machinery:

```python
def _action(name):
    manager = SimpleNamespace(...)
    return ActionsManager._make_action(manager, ACTIONS[name])
```

so that a malformed entry fails in a test rather than at first right-click.

`tests/test_menu.py` asserts **section composition** — which submenus appear for
an empty window versus one with videos — by building a `MenuManager` with a
`SimpleNamespace(video_blocks=[], active_block=None)` context and calling
`_menu_sections()`.

### 7. Static/source analysis tests

A few tests assert things about the source itself, because a runtime check would
be too late or too slow:

* **`tests/test_translation_timing.py`** is the important one. It runs a
  subprocess that imports `gridplayer.__main__` and prints every
  `gridplayer.*` module loaded, then AST-parses each one looking for
  `translate()`/`tr()` calls **outside any function** — i.e. executed at import
  time. Any offender fails the test.

  The docstring explains why this exists: `__main__.py` imports much of the app
  before `init_translator()` runs, so a module that translates while being
  imported gets the English text back **and keeps it for good**. That is exactly
  how every menu entry once came out in English regardless of language. The
  second test names `params/actions.py` explicitly so a failure says what broke.

  Practical consequence for you: **never call `translate()` at module level in a
  module that `__main__` imports.** Pass a callable where you need lazy text
  (which is why `combo_values` in `defaults_fields.py` is a function, not a dict).

* **`tests/test_scripts_changelog_to_appdata.py`** covers the release tooling.

### 8. Real servers for the network stack

The stream-proxy tests (`tests/test_stream_proxy_*.py`, ~1300 lines across nine
files) start real `http.server` instances on localhost via the `serving` fixture
and make real requests through the proxy. They assert on bytes served, manifest
rewriting, live-playlist renewal, and header/cookie injection. This is the only
practical way to test manifest rewriting — a mock would just restate the
implementation.

`tests/test_stream_proxy_session.py` is a good small example of the
parametrised style used to pin the protocol→wrapper mapping, including the
negative case:

```python
def test_a_protocol_nobody_handles_is_refused(session):
    with pytest.raises(RuntimeError, match="rtmp"):
        session.get_stream(_stream("rtmp"))
```

## Harnessing the real application

Some tests and any manual harness you write need a real `Player`. There is a
documented trap, repeated from `AGENTS.md` because it costs everyone an hour
once:

> **Closing the player asks "do you want to save?" by default, and a harness has
> nobody to answer it, so it hangs forever after the main window closes.** Set the
> playlist to discard before building the player.

```python
from gridplayer.params.static import UnsavedChangesMode
from gridplayer.settings import Settings

Settings().set("playlist/unsaved_changes", UnsavedChangesMode.DISCARD)
```

The suite avoids this by setting the same key through `PlaylistSettings()` in the
tests that exercise the save-prompt paths
(`tests/test_managers_playlist.py:717`), where the autouse fixture clears it
afterwards.

Related teardown facts, from `tests/test_close_teardown.py`:

* `WindowStateManager.closeEvent` **hides the window first**, then asks about
  saving, then closes (`assert events == ["ask", "hide", "close"]`). If the user
  cancels, the event is ignored and nothing is torn down.
* If `check_playlist_save` returns `False`, `force_close_playlist` and
  `force_terminate` are **not** called.

## Tests that need VLC

A good part of the suite drives code that reaches libVLC, and on a machine
without VLC installed those tests have to be skipped rather than failed. Two
patterns, and it is worth knowing which one a file uses:

* **Collect-time import.** Most files import `gridplayer.vlc_player...` at module
  level, which loads the shared library as the module is imported. On a machine
  without VLC **the whole file fails to collect** — `pytest` reports it as an
  error, not as a skip, and `-x` stops there. Nothing can be done about this from
  inside the file; install VLC (CI installs `libvlc5 vlc-plugin-base` and the GL
  and font libraries Qt wants offscreen).
* **A marker, where only some of the file needs it.** Files that mix the two
  import VLC-using modules *inside* the tests that need them and mark those tests
  or classes:

  ```python
  def _has_vlc() -> bool:
      try:
          import gridplayer.vlc_player.libvlc  # noqa: F401
      except Exception:
          return False
      return True

  needs_vlc = pytest.mark.skipif(not _has_vlc(), reason="VLC is not installed")

  @needs_vlc
  class TestTheProbeReadsASnippet:
      ...
  ```

  `tests/test_sync_audio.py` and `tests/test_sync_offset.py` are written this
  way: the arithmetic, the offsets, the dialog and the actions all run without
  VLC, and only reading sound off a file is skipped. Mark classes and not whole
  files, and only where the VLC import is really reached — a class of pure
  arithmetic under a `needs_vlc` mark is coverage thrown away on every machine
  that has no VLC.

  Where a test needs a recording to read, it writes a **temporary wav** with
  `wave` (`_write_clicks`, `_write_irregular_bursts` in `test_sync_audio.py`,
  `_write_a_cut_of_the_event` for two cuts of one event) rather than shipping a
  media file: bursts of known lengths at known moments say what the reading
  should have come back with, the same file can be read from two positions to
  measure the error of the whole path, and two cuts of one event a known
  distance apart are what the two passes are tested end to end on.

  Two of those tests are worth knowing before changing anything about the
  search, because nothing else covers what they cover:

  * `TestTwoRecordingsThatBeganAMinuteApart` runs the whole chain over two real
    files — wide read, coarse align, then a snippet read out of what the wide
    one decoded — and asserts the minute comes back exactly. A minute is what
    the close pass alone cannot see any part of, so a regression in either pass
    or in the sharing between them shows up here and nowhere else.
  * `TestReadingEveryRecordingAtOnce` runs `AlignMeasure` for real, over
    threads, and pins the two properties a dialog depends on: every reading
    comes back (including "there is no sound in this file"), and each one is
    reported as it arrives. A run that never says it is done is a dialog that
    waits for ever, which is a bug no unit test of the reading itself would
    catch.

## What to test when you change something

| Change | Minimum coverage |
| --- | --- |
| A settings key | `tests/test_settings.py` round trip; if it drives behaviour, a test on the consuming manager. |
| A defaults-form row | `tests/test_settings_sections.py` asserts the section/row structure of the dialog. |
| An action or menu entry | A `tests/test_menu_*.py` assertion that the id resolves and the flags are what you meant. |
| A `VideoBlock` feature | The relevant `tests/test_video_block_*.py`. |
| Playback/track handling | `tests/test_vlc_player_*.py` with a fake media player. |
| Stream resolution | `tests/test_resolver_yt_dlp_*.py`, `tests/test_url_resolve_worker.py`. |
| The proxy | A `tests/test_stream_proxy_*.py` test with a real localhost server. |
| Grid/drop behaviour | `tests/test_grid_layout.py`, `tests/test_managers_grid.py`, `tests/test_drag_n_drop.py`. |
| Anything in the sound of a recording, or in lining recordings up | `tests/test_sync_audio.py` (the arithmetic, the cache of what was read, placing a reading on the clock, the pairing up of several readings, and the two scales `best_lag` searches at). |
| The alignment dialog, offsets, a seek carried between videos, or the envelope strips | `tests/test_sync_offset.py`. |
| Anything touching a `translate()` call site | `tests/test_translation_timing.py` must still pass. |

The suite has a test file per feature area, so the fastest way to find the right
home for a new test is to search for the feature name:

```bash
uv run pytest --collect-only -q | grep -i bookmark
```

## Traps

* **Global state leaks.** `Settings`, `PlaylistSettings`, running server threads
  and the app's own log configuration are all process-wide. If a test passes
  alone but fails in the suite, that is the cause — check whether your test needs
  one of the autouse fixtures or its own `mocker.patch`.
* **Module-level `translate()`.** See above; it is a test failure, not a style
  nit.
* **A dropped parent widget.** Always keep a reference to the parent of a
  `QObject`-derived manager for the duration of the test.
* **Forgetting `_qapp`.** Tests that touch widgets need a `QApplication`. Many
  files declare their own module-scoped fixture; the session fixture in
  `conftest.py` means you can also just call
  `QApplication.instance() or QApplication([])`.
* **Real VLC is loaded on import.** `gridplayer.vlc_player.vlc` loads the VLC
  shared library, so the test machine needs VLC installed (CI installs
  `libvlc5 vlc-plugin-base`, plus `libegl1 libgl1 libxkbcommon0 libfontconfig1
  libdbus-1-3` because Qt draws offscreen but still wants GL and fonts). Tests do
  not need a working *video output*.
* **Timing.** The stub for the proxy's shutdown poll interval exists so server
  teardown does not cost half a second per test. If you add a server test, use
  the `serving` fixture rather than rolling your own thread, and never
  `time.sleep()` where a poll would do.

## CI

`.github/workflows/test.yml` runs on push to `master`/`develop` and on every pull
request:

* **lint** — `ruff check .`, `ruff format --check .`, `rumdl check .`
  (dev group only, `--locked`).
* **test** — `pytest` on **Python 3.10 and 3.14**, with `UV_PYTHON` set per
  matrix entry, `fail-fast: false`. 3.10 is the oldest supported and what the
  release builds use.

So a change must pass on both the oldest and newest supported Python, and must be
Ruff-clean *and* Ruff-formatted *and* rumdl-clean.
