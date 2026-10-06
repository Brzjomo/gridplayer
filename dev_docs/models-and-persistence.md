# Models and persistence

What gets saved, in what shape, and how it is read back.

## The four persistence surfaces, again

| Surface | Where | Lifetime |
| --- | --- | --- |
| `Settings` | `settings.ini` | Machine-wide user preference. See [settings-system.md](settings-system.md). |
| `PlaylistSettings` | in memory + into `.gpls` | Session/playlist override of a preference. |
| `.gpls` playlist | user-chosen file | Videos, per-video state, playlists settings, bookmarks, snapshots. |
| Cookies | `cookies.txt` | See [streaming-and-network.md](streaming-and-network.md). |

This document is about the third.

## The models

All models are **pydantic v2 `BaseModel`s** in `gridplayer/models/`.

| Model | File | Represents |
| --- | --- | --- |
| `Playlist` | `playlist.py:109` | A whole saved session. |
| `PlaylistVideoDefaults` | `playlist.py:60` | The playlist's overrides of per-video defaults. Every field `| None`, `None` meaning "inherit". |
| `Video` | `video.py:148` | One video and everything the user chose about it. |
| `GridState` | `grid_state.py:15` | Grid mode/size plus `cells` and `video_order`. |
| `GridCell` | `grid_state.py:7` | A cell's identity for layout restore. |
| `Snapshot` | `playlist.py:55` | A saved "all videos at this moment" — grid state plus its own video list. |
| `FileBookmarks` | `file_bookmarks.py` | Bookmarks belonging to a *file*, not to a playlist. |
| `Bookmark` | `bookmark.py:26` | One named position, with an optional colour. |
| `VideoBlockMime` | `video.py:316` | The drag-and-drop payload for a video. |
| `KeymapOverrides` | `keymap_overrides.py` | Sparse shortcut overrides (stored in settings, not playlists). |
| `SpectrumColors` | `spectrum_colors.py` | The colour each recording's sound is drawn in, by file path (stored in settings, not playlists — a colour says nothing about the file). |
| `RecentList*` | `recent_list.py` | The recent-videos and recent-playlists lists. |
| `Stream`, `Streams`, `StreamSessionOpts`, `StreamOrigin`, `StreamFragment` | `stream.py` | Resolved stream data. Covered in [streaming-and-network.md](streaming-and-network.md). |
| `AudioSelection` family | `audio_selection.py` | How audio was chosen — see below. |
| `SubtitleSelection` family | `subtitle_selection.py` | How subtitles were chosen. |

### `Video` and `session_field`

The most important detail in `Video` is how its defaults work:

```python
class Video(BaseModel):
    id: UUID4 = Field(default_factory=uuid4)
    uri: VideoURI

    title: str | None = None
    color: Color = session_field("video_defaults/color", validate=True)

    current_position: int = 0
    loop_start: int | None = None
    loop_end: int | None = None

    end_action: VideoEndAction = session_field("video_defaults/end_action")
    ...
```

`session_field(key)` (`playlist_settings.py:78`) is a `Field` whose
`default_factory` reads **`PlaylistSettings().get(key)`** — which itself falls back
to `Settings().get(key)`. So constructing a `Video` with no arguments gives you
the *effective* preference at that moment, and a new video automatically follows
whatever the playlist/session defaults are.

Consequences to internalise:

* **`Video` fields have no fixed default.** `Video(uri=...)` picks up current
  settings; a test that wants determinism must pin `PlaylistSettings` or patch
  `Settings().get` (the suite does the latter).
* Fields that are inherently per-video rather than a preference deliberately do
  **not** use `session_field` — `current_position`, `loop_start`, `loop_end`,
  `shift`, `audio_delay_ms`, `subtitle_delay_ms`, `video_track_id`,
  `external_audio`, `external_subtitles`, `audio_device`. The comments in
  `video.py:205` and `:216` say why: an audio delay belongs to *this* video, and
  an audio device id names a jack on one machine and nothing on the next.
* Validation is declared inline with `Annotated[..., Field(ge=..., le=...)]`
  using the bounds from `params/static.py` (`MIN_RATE`/`MAX_RATE`, `MIN_SCALE`/
  `MAX_SCALE`, the delay ranges).

### Track selection is semantic, not numeric

`audio_selection` and `subtitle_selection` are discriminated unions, not track
ids, because **VLC renumbers tracks on reload**:

| `AudioSelection` variant | Means |
| --- | --- |
| `AudioDefault` | Whatever VLC picks. |
| `AudioDisabled` | No audio. |
| `AudioLanguage` | A language tag. |
| `AudioExternal` | A file the user attached. |
| `AudioPreferred` | The best of the preferred languages. |
| `AudioTrackId` | An explicit id (the fallback). |

`subtitle_selection.py` mirrors this with six variants including
`SubtitleDisabled`. `Video.video_track_id` is still a plain `int | None`, since
video tracks are rarely renumbered and there is usually only one.

## The `.gpls` format

Format identity:

```python
FORMAT_ID = "gridplayer-playlist"
FORMAT_VERSION = 1
```

`Playlist.parse` (`playlist.py:180`) **sniffs** the format rather than being told
it:

```python
text = playlist_txt.lstrip("\ufeff")
stripped = text.lstrip()
if stripped.startswith("{"):
    return cls._parse_json(stripped, base_dir)
return cls._parse_legacy(text, base_dir)
```

A leading BOM is stripped either way, and `read()` opens with
`encoding="utf-8-sig"` for the same reason.

### JSON format (current)

```json
{
  "format": "gridplayer-playlist",
  "version": 1,
  "settings": { ...playlist-level settings, flat "section/key" names... },
  "videos": [ { "uri": "...", ... } ],
  "bookmarks": [ { "uri": "...", "bookmarks": [ ... ] } ],
  "snapshots": { "0": { "grid_state": {...}, "videos": [...] } }
}
```

Only `format` is required. `videos`, `bookmarks` and `snapshots` are written only
when non-empty (`dumps`, `playlist.py:223`).

**`settings` holds the playlist-level keys as a flat dict** — the same
`"section/key"` names as `Settings`. `_parse_json` builds the model payload by
copying `settings` and then *popping the three top-level arrays out of it* before
assigning them, so a `settings` block that happens to contain a `videos` key
cannot displace the real one (`playlist.py:261`).

Version handling is strict and deliberate:

```python
# Omitted version is JSON v1, not "whatever FORMAT_VERSION is now".
version = doc.get("version", 1)
if isinstance(version, bool) or not isinstance(version, int) or version < 1:
    _invalid_playlist()
if version > FORMAT_VERSION:
    raise UnsupportedPlaylistVersion(version)
```

Note the `isinstance(version, bool)` check — in Python `True` is an `int`, so a
`"version": true` would otherwise pass. A **newer** file raises
`UnsupportedPlaylistVersion`, which the playlist manager reports rather than
treating as corruption. An older file is accepted and migrated.

### Legacy format (still read)

An m3u-like text format:

```text
#GRIDPLAYER
#P:{"grid_state": {...}, "unsaved_changes": "ask"}
#V0:{"volume": 50, "aspect_mode": "fit"}
#V1:{"rate": 2.0}
C:\videos\one.mp4
https://example.com/two.mp4
```

* The **first line must be exactly `#GRIDPLAYER`** or parsing fails.
* `#P:{json}` is the playlist parameters — `_parse_params` takes the **first**
  one and ignores any others (`playlist.py:321`).
* `#V<index>:{json}` holds per-video parameters. The index is the position in the
  list of non-comment lines, so `_parse_video_params` builds
  `{index: params}` and `_parse_videos` looks it up while enumerating the paths.
* Every non-`#` line is a video URI.
* The video params are fed through the same `_resolve_external_files` /
  `_resolve_file_selection` helpers as the JSON path.

There is no legacy *writer* — `dumps` always emits JSON. Legacy is read-only
support for files saved by older versions.

### Migration lives in model validators

Old field names and meanings are migrated by `@model_validator(mode="before")`
hooks, so both formats get the same treatment:

| Validator | Migrates |
| --- | --- |
| `PlaylistVideoDefaults._migrate_paused_default` (`playlist.py:92`) | `paused: bool` → `initial_state`; legacy `repeat` → `end_action` via `migrate_end_action`. |
| `Playlist._migrate_legacy_mouse_flags` (`playlist.py:138`) | `disable_click_pause` → `disable_mouse_click_events`; `disable_wheel_seek` → `disable_mouse_wheel_events`. |
| `Playlist._migrate_legacy_track_changes` (`playlist.py:156`) | `track_changes: bool` → `unsaved_changes: UnsavedChangesMode`. |
| `Video._migrate_legacy_fields` (`video.py:261`) | legacy playback-state fields → `playback_state`. |

Note the pattern: each validator **pops the old key whether or not it uses it**,
so a stale key cannot linger and trip `model_config` extra-field handling.

### What is written, and what is left out

`dumps` (`playlist.py:192`) does `model_dump(mode="json", exclude_none=True)` and
then prunes. `None` never reaches the file, which is what makes the "inherit
otherwise" semantics work: an absent key means "take it from settings".

Three settings gates decide how much per-video state is kept
(`_effective_flag`, `playlist.py:347` — reads the `Playlist` attribute, falling
back to `Settings`):

| Setting | Omitted when off |
| --- | --- |
| `save_position` (default off) | `current_position` |
| `save_state` (default off) | `playback_state` |
| `save_window` (default off) | `window_state` |

So by default a playlist records *what* you were watching and *how*, but not
where you had got to. That is intentional — a playlist is a setup, not a resume
point, unless you ask for one.

Grid state gets the same treatment one level down (`_grid_state_data`,
`playlist.py:378`): any grid attribute **not in `state.model_fields_set`** is
dropped, so a value merely inherited from global settings is not frozen into the
file. `cells` and `video_order` are dropped when empty. If nothing is left, the
whole `grid_state` key goes.

### Relative paths

`playlist/save_paths_relative` (off by default) makes local paths relative to the
playlist file. The conversion is centralised:

* `_dump_uri` (`playlist.py:519`) — leaves anything containing `://` alone
  (it is a URL), relativizes absolute paths when the flag is on, else writes the
  path as-is.
* `parse_uri(uri, base_dir)` on the way back in.

This is applied consistently to the video URI, `external_audio`,
`external_subtitles`, the `external` variants of the two selections, and bookmark
URIs — see `_dump_external_files`, `_dump_file_selection`,
`_resolve_external_files`, `_resolve_file_selection`, `_file_bookmarks_data`. The
comment at `playlist.py:473` states the reason: a playlist that travels with its
videos has to carry the attached files the same way, or they are looked for where
they are not.

`_playlist_base_dir` is the playlist file's absolute parent, so relativization
does not depend on the process CWD.

### Tolerant parsing

The two parsing paths differ in how forgiving they are, and the difference is
deliberate:

* **Structure is validated strictly.** A non-dict document, a wrong `format`, a
  non-list `videos`, a non-dict settings block, a bad version — all raise
  (`_invalid_playlist()` or a specific exception).
* **Individual entries are skipped, not fatal.** `_parse_json_videos` wraps each
  video in a try and `logger.error`s the ones that fail
  (`playlist.py:297`). `_parse_json_bookmarks` does the same and its docstring
  says why: bookmarks are no reason to turn the whole playlist down, since its
  videos play the same without them.

If you add a parsed collection, follow that split: fail the file for bad
structure, log-and-skip for a bad entry.

## Snapshots

A `Snapshot` is a saved moment: the `GridState` plus the whole `videos` list. The
`Playlist.snapshots` field is `dict[int, Snapshot]` keyed by the grid-size index
it belongs to, which is how "remember what I had at 2×2" works.

Snapshot URIs are relativized/resolved separately from the top-level ones by
`_relativize_snapshot_uris` / `_resolve_snapshot_uris` (`playlist.py:527`). Note
the comment there: JSON file URIs stay `str` while video URIs go through
`parse_uri` to become `Path`, and the snapshot resolver exists so the two agree.

`Snapshot` is where `save_window` differs from the other flags — a snapshot
always carries grid state, since that is its point.

## Bookmarks: two different things

This is worth getting straight, because the names collide:

| Concept | Model | Lives in | Identified by |
| --- | --- | --- | --- |
| **Per-file bookmarks** | `FileBookmarks` → list of `Bookmark` | `.gpls` `bookmarks` array, and the shared `BookmarksManager` | the **file's** URI |
| Per-video loop points | `Video.loop_start` / `loop_end` | that video's entry | — |

`playlist/bookmarks_shared` (default on) means bookmarks follow the *file*, so two
cells playing the same file share them. That is why they are stored per-file at
the top level rather than inside each `Video`. When sharing is off, changes are
not propagated and the setting's change can be vetoed by the user
(`SettingsManager._is_sharing_confirmed`, `player/managers/settings.py:101`).

`Bookmark` validates its colour with a regex (`bookmark.py:11`:
`#[0-9a-f]{6}`) and coerces unreadable values to `None` rather than rejecting the
bookmark.

## The playlist manager

`PlaylistManager` (`player/managers/playlist.py`) owns the file lifecycle. Its
commands (`playlist.py:78`) are the public surface:

| Command | Effect |
| --- | --- |
| `open_playlist` | File dialog (`.gpls` filter), then `load_playlist_file`. |
| `save_playlist` | Writes to the known path, or falls through to Save As. |
| `save_playlist_as` | Always asks for a path. |
| `close_playlist` | Asks about unsaved changes, then closes. |
| `force_close_playlist` | Closes without asking. |
| `check_playlist_save` | The save/discard/cancel decision; returns `False` to cancel. |
| `is_playlist_changed` / `is_playlist_saved` | Title-bar state. |
| `playlist_settings` | Opens `PlaylistSettingsDialog`. |
| `toggle_shuffle_on_load` / `is_shuffle_on_load` | Session flag. |

`check_playlist_save` implements `UnsavedChangesMode`'s four values:

| Mode | Named playlist | Unnamed playlist |
| --- | --- | --- |
| `ASK` | Ask | Ask |
| `DISCARD` | Discard silently | Discard silently |
| `AUTO_SAVE_DISCARD` | Save, fall back to asking if the save fails | Discard silently |
| `AUTO_SAVE_ASK` | Save, fall back to asking if the save fails | Ask |

These four rows are exactly what `tests/test_managers_playlist.py` pins down,
including the **fallback to asking when an auto-save fails**
(`test_check_playlist_save_auto_save_failure_falls_back_to_ask`). The fallback
matters: silently discarding after a failed save would lose the user's work.

### The close ordering, and why

`force_close_playlist` exists as a separate command from `close_playlist`
because of a specific ordering requirement, documented at `playlist.py:159`:
**window close asks first so the window can go off screen before the video
players are torn down**, and asking again inside would prompt twice after a
Discard. `WindowStateManager.closeEvent` therefore does
*hide → close*, and the sequence is asserted in `tests/test_close_teardown.py`.

### `PlaylistSettings` round trip through the dialog

`cmd_playlist_settings` (`playlist.py:101`) is the one place where the sparse
override dict is replaced wholesale:

```python
dialog = PlaylistSettingsDialog(
    overrides=PlaylistSettings().as_dict(),
    grid_state=self._ctx.grid_state,
    parent=self.parent(),
)
if not dialog.exec_():
    return
PlaylistSettings().replace(self._with_bookmarks_sharing_confirmed(dialog.result_overrides()))
self._apply_effective_session()
self._ctx.commands.apply_grid_config(dialog.result_grid_state(self._ctx.grid_state))
```

The dialog works on `result_overrides()` — only what the user actually changed —
and `grid_state` is handled separately because grid overrides live in `GridState`
attributes rather than the flat dict. `_with_bookmarks_sharing_confirmed` can
still veto the bookmarks-sharing change.

## Recipe: add a field to the playlist format

1. **Add it to the model.** For a per-playlist setting, an `X | None = None`
   field on `Playlist`; for a per-video one, on `Video` (using `session_field`
   if it should follow a default).
2. **Make it round-trip through `PlaylistSettings`** if it is also a setting: set
   `playlist_attr` (or `video_attr`) on its `SettingField` in
   `params/defaults_fields.py`. The generic loops in
   `overrides_from_playlist` / `playlist_kwargs_from_overrides` then handle it
   with no further code.
3. **Decide the omission rule.** If it should only be written when a flag is on,
   add a branch to `_params_data` or `_video_data` alongside the existing
   `save_position`/`save_state` checks.
4. **Path-valued?** Route it through `_dump_uri` and `parse_uri` so relative
   saving works, and add it to `_parse_json_videos`' resolution block.
5. **Add a migration** in a `model_validator(mode="before")` if you renamed or
   reshaped anything.
6. **Test both formats.** `tests/test_managers_playlist.py` has the round-trip
   and legacy-parse tests; the legacy path is the one that silently rots, so a
   legacy fixture is the valuable test.
7. **Consider `FORMAT_VERSION`.** Bump it only for a change old versions cannot
   read at all — a new optional field does not need a bump, and bumping makes
   every older build refuse the file.

## Gotchas

* **`exclude_none=True` everywhere.** A field set to `None` and a field that was
  never set look identical on disk. That is what makes inheritance work, but it
  also means **you cannot store an explicit "unset"** distinct from "inherit" —
  if you need three states, use a sentinel value, not `None`.
* **`model_fields_set` is how "explicitly set" is detected** for grid state. If
  you build a `GridState` in code and want its value written, it must appear in
  the constructor, not just be equal to the default.
* **Never write a playlist with a newer `version` by hand.** Older builds raise
  `UnsupportedPlaylistVersion` and will not open it at all.
* **`PlaylistSettings` shadows `Settings` on read.** A field defaulted with
  `session_field` will pick up a session override even in a brand-new video, which
  is usually right and occasionally surprising.
* **Snapshots and top-level URIs are resolved separately.** If you add a
  URI-bearing field, remember it exists in both places.
* **Do not import `models.playlist` from `playlist_settings.py`.** The import is
  deliberately broken by passing a plain dict for `video_defaults` and letting
  `Playlist` coerce it (`playlist_settings.py:117` explains it is to avoid an
  import cycle).

## Key files

| File | Role |
| --- | --- |
| `models/playlist.py` | `Playlist`, `Snapshot`, both parsers, the writer, migrations. |
| `models/video.py` | `Video` and its `session_field` defaults; DND mime payload. |
| `models/audio_selection.py`, `models/subtitle_selection.py` | Semantic track selection. |
| `models/grid_state.py` | `GridState`, `GridCell`. |
| `models/file_bookmarks.py`, `models/bookmark.py` | Per-file bookmarks. |
| `playlist_settings.py` | The override dict and all model↔settings conversions. |
| `player/managers/playlist.py` | File lifecycle and the unsaved-changes decision. |
| `player/managers/settings.py` | Grid/bookmarks change routing into a live session. |
| `dialogs/playlist_settings.py` | The playlist settings dialog. |
