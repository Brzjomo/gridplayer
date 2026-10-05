# Commands, actions, menus and shortcuts

Everything the user can invoke is declared as data in `params/actions.py`, laid
out in `params/menu.py`, dispatched through the `Commands` registry, and bound to
keys by `utils/keymap.py`. This document explains each piece and gives the recipe
for adding a new one.

## The four tables

| Table | File | Purpose |
| --- | --- | --- |
| `ACTIONS` | `params/actions.py` | Every invocable action: title, icon, default shortcut, target command, conditionals. |
| `SECTIONS` | `params/menu.py` | The context menu, as ordered lists of action ids, strings and nested submenu tuples. |
| `SUBMENUS` | `params/menu.py` | Title and icon for each named submenu. |
| `manager.commands` | `player/managers/*.py` | The command names that `ACTIONS` entries resolve against. |

`ACTIONS` is a `MappingProxyType` — read-only at runtime. All four are built at
import time, so any new entry is available without touching the dispatch code.

## Anatomy of an action entry

```python
"Play / Pause": {
    "title": (translate("Actions", "Play"), translate("Actions", "Pause")),
    "keys": ["Ctrl+Space", "Click"],
    "icon": ("play", "pause"),
    "toggle": ("is_active_param_set_to", "is_paused", False),
    "func": ("active", "play_pause"),
    "show_if": "is_active_playable",
},
```

Every field, with real usage counts from the current table:

| Field | Count | Type | Meaning |
| --- | --- | --- | --- |
| `title` | 325 | `str` or `(str, str)` | Menu text. A **tuple** means the action has two states and `toggle` picks which; the `Toggle` pattern is used by `QDynamicAction.title`. |
| `func` | 316 | command spec | What to run when triggered. Absent for `menu_generator` entries. |
| `icon` | 293 | `str`, `(str, str)`, or a `QIcon` | A theme icon name, a state pair, or a ready-made `QIcon` (generated items do this, e.g. bookmark colour markers). `"empty"` means no icon. |
| `show_if` | 281 | command spec | If falsy, the item is **left out of the menu entirely**. |
| `key` | 146 | `str` or list | A single default shortcut. **Legacy**; `keys` supersedes it. |
| `check_if` | 72 | command spec | Makes the item checkable and ticked when truthy. |
| `keys` | 8 | `list[str]` | Multiple default shortcuts, mixing keyboard chords and mouse sequences. |
| `menu_generator` | 9 | command spec | Builds the item's submenu dynamically. |
| `value_getter` | 6 | command spec | Substituted into `%v` in the title — for "Size: 3x3" style rows. |
| `enable_if` | 5 | command spec | If falsy, the item is shown but **grayed out**. |
| `toggle` | 3 | command spec | Picks which half of a tuple `title`/`icon` to use. |
| `is_menu_inline` | 2 | `bool` | Generated items go into the parent menu instead of a submenu. |

Because a `show_if` that is false removes the row, **prefer `enable_if` when the
user needs to know the feature exists** — a grayed-out row explains itself, a
missing one does not. The codebase's own comment on preferences follows the same
principle (`active_block.py:505`).

### Command specs

`func`, `check_if`, `enable_if`, `show_if` and `value_getter` are all *command
specs*, resolved by `Commands.resolve` (`player/manager.py:24`):

| Spec | Resolves to |
| --- | --- |
| `"toggle_disable_overlay"` | the command itself |
| `("active", "play_pause")` | `commands.active("play_pause")` |
| `("active", "manual_seek", "seek_percent", 0.5)` | `commands.active("manual_seek", "seek_percent", 0.5)` |
| `AND("is_single_mode", "is_more_than_one_video")` | callable returning `all(...)` |
| `OR(...)` / `NOT(...)` | `any(...)` / `not ...` |

`AND`/`OR`/`NOT` come from `utils/command_helpers.py`. They nest.

The `("active", "<method>", …)` form is by far the most common `func`: it routes
to the currently active `VideoBlock` through `ActiveBlockManager.cmd_active`,
which **drops the call unless the video is playable**, except for the names in
`LOADING_COMMANDS` (`player/managers/active_block.py:44`). If you add an action
that must work while a video is still loading or showing a network error, add its
command name to that frozenset. `QUIETLY_DROPPED_COMMANDS` is the separate list of
names whose drops are not worth a log line, because the mouse fires them
continuously.

## Where actions are built

`ActionsManager` (`player/managers/actions.py`) turns the table into live Qt
objects once, at startup:

* `_make_actions` iterates `ACTIONS` and builds a `QDynamicAction` per entry.
* For a normal entry, the `func` spec is resolved and connected to
  `_run_action`, which guards re-entrancy with `self._invoking` and re-checks
  `is_enabled` before running (`actions.py:322`).
* For a `menu_generator` entry, no trigger is connected; instead the generator
  and a *templates* callable are stored. `menu_templates` lets `is_skipped` ask
  "would this submenu be empty?" without building it (`actions.py:54`).
* `_map_dynamic_functions` wires `check_if`, `enable_if`, `show_if`,
  `value_getter` and `toggle` onto the action as plain callables.

### `QDynamicAction` is not a `QAction`

This indirection is the subtlest part of the action system.

The real action stays registered on the **window** so its shortcuts work
globally. When a menu is built, `to_menu_action` (`actions.py:112`) creates a
**proxy** `QAction` for display:

* the proxy carries the shortcut text with `Qt.WidgetShortcut` context, so the
  chord is *shown* in the menu without registering a second window shortcut;
* `enable_if` is applied to the **proxy** (grayed out), while the real action
  stays enabled — the comment at `actions.py:91` states the reason: a disabled
  `QAction` would stop its shortcut from firing;
* `check_if` is mirrored onto the proxy;
* a generated submenu is moved from the real action to the proxy.

Practical consequence: **`action.setEnabled(False)` is not how you disable an
action.** Set `enable_if` in the table.

## The context menu

`MenuManager` (`player/managers/menu.py`) is an event filter on the window for
`QEvent.ContextMenu`. On each context menu it:

1. Finds the cell under the cursor (`get_cell_at`) and, if it is an **empty
   cell**, records it as the add-anchor so "Add Files" lands in that cell instead
   of appending. The anchor is cleared after the menu closes.
2. Chooses which sections to show: `video_active` (only when there is an active
   block), `video_all` (only when there are videos), then `playlist`, then
   `program` — joined with separators by `_join_menu_sections`.
3. Walks the resulting structure, resolving submenu tuples against `SUBMENUS`
   and action ids against `self._ctx.actions`.
4. `menu.deleteLater()` afterwards; the comment at `menu.py:32` notes that
   without it every open leaks a `QMenu`.

Separator handling is defensive by design: `_add_separator` refuses to add one at
the start of a menu, after another separator, or at the end;
`_strip_trailing_separator` cleans up after items that decided not to show; and
`_add_menu_items` removes a submenu that turned out to be empty. **When you add a
conditional item, you do not need to manage the separators around it** — that is
already handled.

### Generated submenus

Two flavours, both fed by a `menu_generator` command:

* **Submenu** (default). `QDynamicAction._generate_submenu` builds a `CustomMenu`,
  calls the generator, skips items whose `is_skipped` is true, handles `"---"`
  separators, and installs the result as the action's menu. The generated menu is
  **parented to the menu it appears in**, not to the window, so it is destroyed
  with the menu rather than accumulating one per open (`actions.py:139`).
* **Inline** (`is_menu_inline: True`). `_add_inline_actions` puts the generated
  items directly into the parent menu. Used for chapter and bookmark lists, whose
  rows belong next to fixed actions. Note that generated items **cannot have
  shortcuts** and are not part of the keymap.

A generator returns a list of *action templates* — plain dicts with the same keys
as an `ACTIONS` entry. `ActiveBlockManager.menu_generator_chapters`
(`active_block.py:407`) is a compact example:

```python
{
    "title": timed_title(block.chapter_name(index), chapter.start_ms, length_ms),
    "icon": "empty",
    "func": ("active", "manual_seek", "seek_chapter", index),
    "check_if": ("is_active_chapter_playing", index),
    "enable_if": ("is_active_chapter_reachable", index),
    "show_if": "is_active_has_chapters",
}
```

`_separated(*groups)` in `active_block.py:1417` is the helper for building a
grouped menu with `"---"` between the groups that have content — use it rather
than hand-placing separators.

## Keymap and shortcut binding

`utils/keymap.py` owns shortcut semantics. The stored setting is
`player/keymap`, a `KeymapOverrides` pydantic model holding a **sparse** map of
`action_id → [shortcut, …]` — only actions that differ from the defaults.

Vocabulary:

* A shortcut is either a **keyboard chord** (`"Ctrl+Space"`, `"PgDown"`) or a
  **mouse sequence** (`"Click"`, `"Double Click"`, `"Middle-Click"`,
  `"Ctrl+Wheel-Down"`). `MouseButtonSequence` recognises the mouse ones.
* `normalize_shortcut` canonicalises through
  `QKeySequence.PortableText`, so `u` and `U` compare equal, and maps Qt's
  `Return`/`Enter` distinction onto one logical `Enter`. `qkeysequences_for_shortcut`
  then expands that logical `Enter` back into **both** physical keys, so either
  one triggers the action.
* `default_keymap` reads `key`/`keys` from `ACTIONS`, skipping `menu_generator`
  entries (a generated submenu cannot hold a shortcut).
* `merge_keymap` overlays the sparse overrides on the defaults.
* `dedupe_keymap` enforces **one owner per chord**. When two actions claim the
  same shortcut, an explicit user assignment beats a default, and ties are
  broken in `ACTIONS` order with the loser's chord stripped.
* `sparse_overrides` is the inverse: given a full map, keep only the differences —
  that is what gets written to the setting.
* `find_duplicate_shortcuts` is the corruption check.

`ActionsManager.apply_bindings` (`actions.py:185`) applies the effective keymap in
two phases, and the two-phase structure matters:

```python
# Phase 1: detach and clear every action so no stale chords remain
for action in self._ctx.actions.values():
    if action in parent.actions():
        parent.removeAction(action)
    action.setShortcuts([])

# Phase 2: apply normalized keyboard / mouse bindings
for cmd_name, action in self._ctx.actions.items():
    if action.menu_generator:
        continue
    ...
    for mouse_key in mouse_keys:
        self._mouse_index[mouse_key] = cmd_name
    ...
    action.setShortcuts(sequences)
    parent.addAction(action)
```

Without phase 1, reassigning a shortcut would leave the old chord live because
Qt has no "remove one shortcut and rebind" primitive here.

**On duplicate shortcuts in a hand-edited settings file, `apply_bindings` logs an
error and falls back to the whole default keymap** (`actions.py:196`) rather than
crashing. Corrupt settings must stay survivable.

### Mouse dispatch

Keyboard shortcuts are plain Qt shortcuts on the window. Mouse sequences cannot
be, so `ActionsManager` keeps a reverse index `_mouse_index: sequence → action_id`
and handles them in its own `event_map` for `MouseButtonRelease`,
`MouseButtonDblClick` and `Wheel`.

Two documented subtleties:

* Events are delivered to the filter **only for the `Player` widget itself**, not
  its `VideoBlock` children (see the docstring at `actions.py:173`). Video-area
  mouse binds are handled in `VideoBlock` and routed through
  `_dispatch_mouse_action`; this is why a bind does not double-fire.
* `is_mouse_event_bound` exists so other components can ask whether a mouse
  gesture is claimed before acting on it.
* Click and wheel handling both check the `playlist/disable_mouse_click_events` /
  `disable_mouse_wheel_events` settings before dispatching.

`ActionsManager.trigger(action_id)` is the programmatic entry point and is used by
tests to invoke an action "the same way a menu would"; it returns `False` for
unknown or `menu_generator` actions and for disabled ones.

## Recipe: add a new action

Worked example — *Restart Playback*, a per-video action with a shortcut and a
menu entry in the Video submenu.

1. **Implement the command.** Add a method to `VideoBlock`
   (`widgets/video_block.py`) named exactly what the action will call:

   ```python
   def restart_playback(self):
       self.seek(0)
       self.set_pause(False)
   ```

2. **Expose it as a command** if it is not reached through `("active", ...)`.
   Per-video actions go through `ActiveBlockManager.cmd_active`, which needs no
   registration — but if the action must survive a loading/error state, add
   `"restart_playback"` to `LOADING_COMMANDS` in `active_block.py`.
3. **Declare the action** in `ACTIONS` (`params/actions.py`), in the right group
   comment block:

   ```python
   "Restart Playback": {
       "title": translate("Actions", "Restart Playback"),
       "key": "Ctrl+Shift+R",
       "icon": "play",
       "func": ("active", "restart_playback"),
       "show_if": "is_active_playable",
   },
   ```

   Use `translate("Actions", …)` for menu strings, or the shared setting-name
   context when the same toggle also appears in the settings form (see
   `AGENTS.md`).
4. **Place it in the menu** in `params/menu.py`'s `SECTIONS`, inside the relevant
   tuple, by its **exact action id string**:

   ```python
   (
       "Playback",
       "Play / Pause",
       "Stop",
       "Restart Playback",
       ...
   ),
   ```

   Add the id to a new submenu in `SUBMENUS` first if you need a new group.
5. **Check the shortcut does not collide.** `dedupe_keymap` will silently strip
   the loser's chord, and `find_duplicate_shortcuts` will reject a *user* file —
   but two colliding defaults just quietly lose one binding.
6. **Test it.** There is **no global test that every id in `SECTIONS` exists in
   `ACTIONS`** — the coverage is per-feature: `tests/test_menu.py` checks section
   composition, and the `tests/test_menu_*.py` files walk their own submenu and
   assert their own action ids resolve (`tests/test_menu_chapters.py:65`,
   `tests/test_menu_bookmarks.py:72`). Add an assertion of that kind for your
   submenu, or a typo will surface as a `KeyError` only when a user right-clicks.
   See [testing.md](testing.md).

### Common mistakes

* **A typo in an action id inside `SECTIONS`.** `MenuManager._add_menu_items`
  indexes `self._ctx.actions[m_item]` directly, so the menu raises `KeyError` at
  the moment it is opened — not at startup. No test guards this globally; the
  per-feature `tests/test_menu_*.py` files guard only their own submenus.
* **A command name that no manager publishes.** `Commands.resolve` raises
  `KeyError` on first use.
* **A command name that collides with another manager's.** `_register_commands`
  raises `ValueError` **at startup**, listing the conflict.
* **Editing the title string but not its translation context**, so the string is
  never extracted by `pylupdate5` (see `AGENTS.md` and
  `tests/test_translation_timing.py`).
* **Adding a `menu_generator` action and giving it a shortcut.** Generated
  submenus are skipped by `apply_bindings`; the shortcut will do nothing.
* **Expecting a disabled-looking row to be unclickable.** `enable_if` grays the
  proxy, but the window action remains live so its shortcut still works — which is
  intentional. The invoke path re-checks `is_enabled`, so a shortcut on a
  currently-invalid action is safely ignored.

## Keymap UI

`widgets/keymap_tree_view.py` (~1256 lines) is the editor shown in
*Settings → Keymap*. It works on the full effective map and writes back through
`sparse_overrides`, so unmodified bindings never enter the settings file. It uses
`key_sequence_from_event` to capture a chord, and `normalize_shortcuts` to
validate it. If you add an action, it appears in this tree automatically — no UI
change is needed unless you want a new column or grouping.

## Key files

| File | Role |
| --- | --- |
| `params/actions.py` | The `ACTIONS` table. |
| `params/menu.py` | `SECTIONS` and `SUBMENUS`. |
| `player/managers/actions.py` | Builds `QDynamicAction`s, applies bindings, dispatches mouse. |
| `player/managers/menu.py` | Builds and shows the context menu. |
| `player/managers/active_block.py` | `cmd_active` routing, `LOADING_COMMANDS`, the generated menus. |
| `player/manager.py` | `Commands`, `Context`, `ManagersManager`. |
| `utils/command_helpers.py` | `AND` / `OR` / `NOT`. |
| `utils/keymap.py` | Shortcut normalisation, merge, dedupe, mouse sequences. |
| `widgets/keymap_tree_view.py` | The keymap editor. |
| `widgets/custom_menu.py` | `CustomMenu`, used for every menu. |
