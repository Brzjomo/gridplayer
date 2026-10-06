# GridPlayer developer documentation

Internal engineering documentation for continuing development and maintenance of
GridPlayer. `README.md` at the repository root is written for **users**;
`CONTRIBUTING.md` covers the **release workflow**. Everything in this directory
is written for people changing the **code**.

## Start here

| If you want to… | Read |
| --- | --- |
| Get a mental model of the whole program | [architecture.md](architecture.md) |
| Run it, test it, lint it, debug it | [development.md](development.md) |
| Run the app or the suite from a clone, in one command | [run-and-test.md](run-and-test.md) |
| Understand startup, argv, single instance | [boot-and-lifecycle.md](boot-and-lifecycle.md) |
| Add a menu item, shortcut or command | [commands-and-actions.md](commands-and-actions.md) |
| Add or change a setting | [settings-system.md](settings-system.md) |
| Touch the main window, grid, or a video cell | [ui-layer.md](ui-layer.md) |
| Touch playback, VLC, or the decoder processes | [playback-and-vlc.md](playback-and-vlc.md) |
| Touch streaming URLs, yt-dlp, Streamlink, the proxy | [streaming-and-network.md](streaming-and-network.md) |
| Change how state is saved into `.gpls` playlists | [models-and-persistence.md](models-and-persistence.md) |
| Understand the tests before changing behaviour | [testing.md](testing.md) |
| Ship a build | [build-and-release.md](build-and-release.md) |
| Look up a term | [glossary.md](glossary.md) |
| See what is fragile or unfinished | [known-issues-and-risks.md](known-issues-and-risks.md) |
| Work on multi-camera sync alignment | [design-sync-offset.md](design-sync-offset.md) |
| Work on the audio matching itself | [audio-alignment.md](audio-alignment.md) |
| See what is planned next, and what was decided against | [roadmap.md](roadmap.md) |

## The one-paragraph summary

GridPlayer is a PyQt5 desktop application that shows N videos at once in a grid.
Each cell is a `VideoBlock` widget. All actual decoding and rendering is done by
**libVLC**, which is driven either in-process or — by default — in a pool of
child processes, one frame object per video. The main process never touches a
decoder directly; it sends commands over a small stdio protocol and receives
events back. Everything the user can do is defined as a named **action** in a
central table (`params/actions.py`), dispatched through a registry of
**commands** that the window's **managers** publish. Settings are a flat
`"section/key"` namespace backed by an INI file, with the Qt settings dialog
generated automatically from a description table.

## Conventions used in these documents

* Paths are relative to the repository root.
* `file.py:123` refers to a line in the current working tree. Line numbers drift;
  the symbol names next to them do not, so search for the symbol if a line looks
  wrong.
* Code excerpts are real, taken from the tree, and trimmed of unrelated lines.
  Elisions are marked with `...`.
* **Verified** statements were checked by reading the code. Where a document
  states a behaviour that is not obvious from a single function, the function is
  named so you can check it yourself.
* Most documents are in English, as reference material for whoever changes the
  code. The two that are **working documents** — `design-sync-offset.md` and
  `roadmap.md` — are in Chinese, matching the language the feature was specified
  and decided in. Measurements are quoted with the file and setting they were
  taken on; where a number came from a synthetic fixture rather than real media,
  that is said.

## Repository map

```text
gridplayer/            application package
├── __main__.py        console entry point
├── main/              startup sequence (init_*.py, run.py)
├── player/            the main window and its managers
│   ├── manager.py     ManagersManager / Commands / Context
│   └── managers/      one module per concern (grid, playlist, actions, …)
├── widgets/           VideoBlock, video frames, overlay, settings widgets
├── vlc_player/        libVLC wrappers + the vendored python-vlc binding
├── models/            pydantic data models (Video, Stream, Playlist, …)
├── params/            static tables: actions, menus, defaults, enums, themes
├── dialogs/           modal dialogs, incl. the generated settings dialog
├── utils/             helpers, plus url_resolve/, stream_proxy/, layout/
├── resources/         GENERATED Qt resources — do not hand-edit
├── settings.py        the settings singleton
└── version.py         version constants

resources/             raw sources for the generated resources
├── ui/                Qt Designer .ui files  → *_ui.py via `just generate-ui`
├── icons/, fonts/     artwork compiled into resources via `just generate-resources`
└── translations/      .ts sources (managed on Crowdin — do not edit)

scripts/               build/packaging for each platform + translations tooling
tests/                 pytest suite (~25k lines) driving the real application
justfile               task runner entry points
```

## Hard boundaries

These are restated from `AGENTS.md` because breaking them causes silent,
hard-to-diagnose breakage:

1. **Do not create commits or push** unless explicitly asked.
2. `gridplayer/resources/` is **generated**. Edit `resources/` and run
   `just generate-resources`.
3. **Translations are managed externally.** Do not modify `.ts`/`.qm` files,
   `scripts/translations/*`, or run `just translations-*`.
4. `gridplayer/vlc_player/vlc.py` is a **vendored** copy of the python-vlc
   binding. Do not modify it.
5. Files matching `*_ui.py` are generated from `.ui` sources. Edit the `.ui` file
   and run `just generate-ui`.
