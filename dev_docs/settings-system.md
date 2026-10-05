# Settings system

How settings are declared, stored, read, shown, and applied — plus the exact
recipe for adding one.

## The three storage layers

This is the single most confusing part of the codebase, so read it before
touching any setting.

### Layer 1 — `Settings` (global defaults, persisted)

`gridplayer/settings.py` exposes a module-level singleton:

```python
def Settings():
    global SETTINGS
    if not SETTINGS:
        SETTINGS = _Settings()
    return SETTINGS
```

`_Settings` wraps a `QSettings` in **INI format** at
`get_app_data_dir() / "settings.ini"` (`settings.py:188`, the `QSettings` itself
at `:192`) and is declared flat — every key is a `"section/name"` string. `get`/`set` accept that string:

```python
Settings().get("playlist/grid_mode")     # -> GridMode.AUTO_ROWS
Settings().set("playlist/grid_rows", 4)
```

`set` **type-checks against the declared default** and raises `ValueError` on a
mismatch (`settings.py:296`). This is deliberate and catches a whole class of
bugs — but it means an `int` cannot be stored where a `float` default is
declared, and an `Enum` must be passed as the enum member, not its value.

**A key must exist in `_default_settings` (settings.py:54) or both `get` and
`set` raise `KeyError`.** That dict is the single source of truth for defaults,
and it is *separate from* the `SettingField` table that drives the UI. A setting
that exists in one but not the other is a common mistake — see the checklist
below.

Value encoding on the way to disk (`_get_storage_value`, `settings.py:378`):

| Python type | Stored as |
| --- | --- |
| `Enum` | its `.value` (a string) |
| pydantic `BaseModel` | `model_dump_json()` |
| `tuple` (a `NamedTuple`) | comma-joined values |
| `Path`, `str` | `str()` |
| everything else | `str()` |

…and decoded back by type dispatch in `get` (`settings.py:274`): enums via
`_parse_enum`, pydantic models via `_parse_pydantic`, `RecentList` subclasses
via `_parse_list` (stored as a QSettings group of numbered keys), `NamedTuple`s
via `_parse_named_tuple` (JSON-parsed per comma-separated part).

Every decoder **falls back to the declared default on any parse error**
(`with suppress(ValueError, TypeError)`), which is what makes a hand-edited or
corrupted `settings.ini` survivable rather than fatal.

`Settings().sync()` flushes to disk. `sync_get(key)` exists for the case where
a *forked child* must read a value the parent just wrote — see
`_subtitle_style_options` in `vlc_player/instance.py:242`, which reads settings
inside the decoder process.

### Legacy key migration

`_migrate_legacy_keys` (`settings.py:198`) runs once at construction and renames
obsolete keys, writing only the new names. Two patterns are in use:

* A simple `renames` dict (`old → new`), copied then removed.
* Purpose-built migrations where the *value* changes meaning —
  `_migrate_track_changes_flag` (bool → `UnsavedChangesMode`),
  `_migrate_paused_to_initial_state` (bool → `VideoInitialState`),
  `_migrate_repeat_to_end_action` (old `Repeat` enum → `VideoEndAction`).

When you rename or reshape a setting, add a migration here. Do not leave the old
key readable — nothing else in the code will read it.

### Layer 2 — `PlaylistSettings` (session/playlist overlay, sparse)

`playlist_settings.py` holds **overrides**: a plain dict of `settings_key →
value`, remembered only for the session (and written into a saved `.gpls`).

```python
PlaylistSettings().get(key)   # override if present, else Settings().get(key)
PlaylistSettings().set(key, value)
PlaylistSettings().is_overridden(key)
```

The mental model: `Settings` is *what the user prefers*; `PlaylistSettings` is
*what this playlist has been changed to*. A saved playlist that pins its grid to
3×3 restores that on load without editing the user's global preference.

Managers that change a playlist-level option usually do **both** — update live
state *and* record the override:

```python
def cmd_set_grid_mode(self, mode):
    ...
    self._mode = mode
    self._flow.mode = _flow_mode(mode)
    PlaylistSettings().set("playlist/grid_mode", mode)
    self.layout_changed.emit()
```

Note the split by *kind* of setting:

* `PLAYLIST_FIELDS` entries with `is_grid=True` are grid keys, mapped by
  `GRID_STATE_ATTR` (`defaults_fields.py:879`) to `GridState` attribute names.
  They are stored in the playlist's `grid_state`, not as a flat list.
* `PLAYLIST_FIELDS` with a `playlist_attr` are stored on the `Playlist` model.
* `VIDEO_FIELDS` with a `video_attr` are stored in the playlist's
  `video_defaults`.

The conversion helpers are `overrides_from_playlist`, `playlist_kwargs_from_overrides`,
`grid_overrides_from_state` and `grid_state_for_dump` — all in
`playlist_settings.py`. `grid_state_for_dump` is worth reading: it emits only
session-overridden grid attributes so a saved file does not pin values it merely
inherited.

`suppress_capture()` temporarily stops `set` from recording, used while loading
a playlist so the act of applying saved values does not itself look like a new
override.

### Layer 3 — the widgets

`Dialogs → SettingsDialog` writes to layer 1 on **OK**; the playlist/video
defaults forms inside it write to layer 2 when opened from the *Playlist
Settings* dialog. `widgets/defaults_form.py` is the shared renderer for both.

## The declarative field table

`params/defaults_fields.py` describes every defaults-form row as a frozen
dataclass, so one renderer can build all of them:

```python
@dataclass(frozen=True)
class SettingField:
    settings_key: str
    kind: FieldKind
    section: str
    label: str
    playlist_attr: str | None = None
    video_attr: str | None = None
    is_grid: bool = False
    menu_action: str | None = None
    combo_values: Callable[[], dict] | None = None
    spin_min: float = 0
    spin_max: float = 100
    spin_decimals: int = 0
    spin_step: float = 0.1
    spin_special: str | None = None
    spin_suffix: str | None = None
    text_placeholder: str | None = None
    checked_value: object | None = None
    unchecked_value: object | None = None
    enabled_by: str | None = None
    enabled_by_value: object | None = None
    grid_visibility: GridVisibility = GridVisibility.ALWAYS
    tooltip: str | None = None
```

`FieldKind` → widget, in `DefaultsForm._make_row` (`widgets/defaults_form.py:78`):

| `FieldKind` | Widget | Value get/set |
| --- | --- | --- |
| `CHECKBOX` | `QCheckBox` (label is the checkbox text) | `bool`, or `checked_value`/`unchecked_value` when the setting is not a plain bool |
| `COMBO` | `QLabel` + `QComboBox` | `currentData()` — the real value, not the display text |
| `TEXT` | `QLabel` + `QLineEdit` | `text()` |
| `SPIN` | `QLabel` + `QSpinBox` | `value()` (int) |
| `FLOAT_SPIN` | `QLabel` + `QDoubleSpinBox` | `value()` (float) |
| `CROP` | `QLabel` + `QCompactCropPicker` | `widget.value` |
| `COLOR` | `QLabel` + `QCompactColorPicker` | hex string via `QColor.name(HexRgb)` |

Behavioural features of the form, all driven by the spec:

* **`section`** — a new value starts a bold header. Rows are therefore ordered
  by section in the tuple; do not interleave.
* **`enabled_by` / `enabled_by_value`** — greys a row out unless the driver row
  is "on". `None` means "any truthy value"; a **tuple** means "any of these",
  which is how `subtitles/outline_color` stays enabled for three of the four
  outline values (`_OUTLINE_DRAWN`, `defaults_fields.py:348`).
* **`grid_visibility`** — `AUTO_ONLY` / `FIXED_ONLY` show the row only in the
  matching grid mode. Implemented by watching `playlist/grid_mode` directly
  (`_sync_grid_visibility`, `defaults_form.py:336`).
* **`spin_special`** — the text shown at the minimum, which is how "0" is
  displayed as *Auto* or *Disabled*.
* **`show_reset=True`** — adds a per-row reset button and italicises overridden
  labels. Only used by the playlist settings dialog.
* Field `label`s in `PLAYLIST_FIELDS` use the shared context
  (`Playlist Settings`, `Transform`, …) when the same toggle also appears in the
  context menu, per `AGENTS.md`. Form-only strings use `SettingsDialog`.

Three separate tuples exist: `PLAYLIST_FIELDS`, `VIDEO_FIELDS` (both combined as
`ALL_DEFAULT_FIELDS`) and `SUBTITLE_STYLE_FIELDS`. `GRID_STATE_ATTR` maps the six
grid keys to `GridState` attribute names.

## The settings dialog itself

`dialogs/settings.py` is **not** generated — only its `_ui.py` companion is. It
mixes two mechanisms:

1. **Named widgets from the `.ui` file**, looked up in a `settings_map` dict
   (`dialogs/settings.py:196`) that binds `"section/key"` → widget. These are the
   settings that need bespoke widgets (the keymap tree, the cookie list, the
   resolver-pattern list, page-specific combos, checkup buttons).
2. **Generated `DefaultsForm`s** for the three field tuples, spliced into
   `.ui`-defined scroll pages by `_replace_scroll_page` (`ui_customize`,
   `dialogs/settings.py:256`).

So *most* settings are added purely as data, and only those needing a custom
widget touch `settings_map` and the `.ui` file.

`load_settings` / `save_settings` (`dialogs/settings.py:827`, `:864`) do the
round trip. Note `_is_session_driver_kept`: changing the video driver mid-session
is handled so the running session's driver is not silently swapped underneath
live videos.

### Platform-conditional pages

`ui_customize` hides rows that make no sense on this platform — `playerStayOnTop`
and the overlay internals on non-Linux, and the "hardware needs an X server"
tooltip when `is_hw_video_available()` is false. Follow this pattern rather than
adding a disabled control with no explanation.

### Live checkups

Two pages can *test* themselves before OK:

* **Test a YouTube link** — `run_youtube_checkup` (`dialogs/settings.py:487`)
  builds a `YouTubeCheckup` and shows it in a `CheckupDialog`.
* **Test network** — `run_network_checkup` (`:514`) does the same with
  `NetworkCheckup`.

Both read the settings **off the widgets, not out of the store**
(`network_opts_on_page`, `:528`) — the point is to try what is on screen before it
has been saved. They also import `yt_dlp`/`streamlink` lazily inside the method,
because those imports are slow and the settings dialog module is imported at
startup. Follow both conventions if you add a checkup.

## How changes reach the running app

The dialog writes to `Settings`; something has to tell the rest of the program.
That is `SettingsManager` (`player/managers/settings.py`), which brackets the
dialog with a before/after snapshot:

```python
def cmd_settings(self):
    previous_settings = Settings().get_all()
    SettingsDialog(self.parent()).exec_()
    self._apply_settings(previous_settings)
    if self._is_restart_needed(previous_settings): ...inform...
    if self._is_reload_needed(previous_settings): self.reload.emit()
```

`_apply_settings` (`:58`) contains a `checks` dict mapping **setting key →
signal**. Only changed keys emit. Then:

* `_is_reload_needed` (`:112`) — keys that require closing and reopening every
  video: the video driver, videos-per-process, the two `internal/opaque_*`
  flags, `misc/vlc_options`, and every `SUBTITLE_STYLE_SETTINGS` key (subtitle
  style is settled when a VLC process starts).
* `_is_restart_needed` (`:126`) — keys that need a full restart:
  `player/language`, the three log-limit keys, `player/stay_on_top`.
* Theme changes apply immediately via `apply_theme()`.
* Grid key changes are routed through `apply_grid_config` after resolving
  overrides with `PlaylistSettings().applied_grid_state(live)`.
* SponsorBlock changes emit one `sponsorblock_changed` signal; the videos
  re-read what they need themselves.
* A change to `playlist/bookmarks_shared` can be **vetoed** by
  `_is_sharing_confirmed`, which asks the user and rewrites the setting back if
  they decline.

The signals are then wired to slots in `player/player.py`'s `connections` dict
(the `"settings"` block, `player.py:96`). So the full path for a new live-applied
setting is: dialog → `Settings` → `SettingsManager` signal → `connections` entry
→ manager method.

Changes to a setting that is *only* read at startup need no signal at all —
`player/start_maximized` is read once in `main/run.py:46`.

## Recipe: add a new setting end to end

Worked example — a boolean `playlist/auto_advance` that both the menu and the
settings form expose, and that applies live.

1. **Declare the default** in `settings.py` `_default_settings`:

   ```python
   "playlist/auto_advance": True,
   ```

2. **Describe the form row** in `params/defaults_fields.py`, in the right
   `section` and in the `PLAYLIST_FIELDS` tuple:

   ```python
   _f(
       settings_key="playlist/auto_advance",
       playlist_attr="auto_advance",
       kind=FieldKind.CHECKBOX,
       section=translate("SettingsDialog", "Playback"),
       label=translate("Playlist Settings", "Advance to the next file"),
       menu_action="Auto Advance",
   ),
   ```

   `playlist_attr` requires a matching field on the `Playlist` model
   (`models/playlist.py`) — add it if you want it saved into `.gpls`. Omit
   `playlist_attr` for a purely global setting.
3. **Add the menu action** in `params/actions.py` (see
   [commands-and-actions.md](commands-and-actions.md)) and reference it by its
   exact id in `menu_action`, plus in the right `SECTIONS` block of
   `params/menu.py`. The established shape for a playlist-level toggle is a
   `toggle_*` command that writes the `PlaylistSettings` override, paired with
   an `is_*` command for `check_if`:

   ```python
   "Disable Overlay": {
       "title": translate("Playlist Settings", "Disable Overlay"),
       "icon": "empty",
       "func": "toggle_disable_overlay",
       "check_if": "is_disable_overlay",
   },
   ```

   `PlaylistSettings` is deliberately *not* reached from `params/actions.py`:
   the table holds only command names, and the manager behind the command does
   the writing.
4. **Expose it to the managers** as a command where a toggle needs to flip it,
   and wire `SettingsManager` → manager in `player/player.py`'s `connections`
   if it must apply to videos already open.
5. **Persist it in the playlist**, if it is playlist-level:
   * add the attribute to the `Playlist` model,
   * include it in `overrides_from_playlist` / `playlist_kwargs_from_overrides`
     (the generic loops over `PLAYLIST_FIELDS` handle this automatically once
     `playlist_attr` is set).
6. **Test it** — see [testing.md](testing.md). At minimum check the settings
   round trip (`tests/test_settings.py`, `tests/test_playlist_settings.py`) and
   the menu generation (`tests/test_menu_generated.py`).

If the setting needs a bespoke widget rather than one of the seven `FieldKind`s,
you are in the second mechanism: add the widget to `resources/ui/settings_dialog.ui`,
run `just generate-ui`, and add a `settings_map` entry plus the
`load_settings`/`save_settings` handling in `dialogs/settings.py`.

### Common mistakes

* Adding to `_default_settings` but not to a `*_FIELDS` tuple (invisible in the
  UI) or the reverse (`KeyError` on read).
* Using `Settings().set` with the wrong type — `ValueError` at runtime, not at
  import.
* Forgetting that `PlaylistSettings` shadows `Settings`: if a key is overridden,
  writing to `Settings` will appear to have no effect in the running session.
* Calling `Settings().set` inside a widget's signal handler without a matching
  `PlaylistSettings().set` — the change is lost when the playlist is reloaded.
* Putting a `tr()`/`translate()` call inside an f-string or a non-literal
  context; `pylupdate5` will not extract it (see `AGENTS.md`).

## Where settings are read

Almost every manager reads its settings once in `__init__` and then follows
signals. Note the exception in `GridManager.__init__`
(`player/managers/grid.py:54`): the grid reads its initial mode/rows/cols/size
straight from `Settings`, not from `PlaylistSettings`, because the playlist has
not been loaded yet; the playlist load then pushes saved state in through
`set_grid_state`.

## Key files

| File | Role |
| --- | --- |
| `settings.py` | `Settings` singleton, defaults table, INI storage, migrations. |
| `playlist_settings.py` | Sparse session/playlist overrides and conversions. |
| `params/defaults_fields.py` | `SettingField` table for the generated forms. |
| `widgets/defaults_form.py` | Renders any `SettingField` tuple. |
| `dialogs/settings.py` | The settings window; `settings_map` for bespoke widgets. |
| `player/managers/settings.py` | Applies changes to the running app; reload/restart policy. |
| `params/static.py` | All setting enums (`AutoName` subclasses). |
| `params/subtitle_style.py` | `SUBTITLE_STYLE_SETTINGS` and VLC option mapping. |
| `params/theme.py` | Theme names and `apply_theme`. |
