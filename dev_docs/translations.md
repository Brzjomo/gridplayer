# Translations

How the interface comes to be in a language, and how to add strings the code has
grown since. Read together with [development.md](development.md) (running a
checkout) and [ui-layer.md](ui-layer.md) (what the strings belong to).

## Where the files are

| Path | What it is |
| --- | --- |
| `resources/translations/*.ts` | The sources, one per language, **edited here** |
| `gridplayer/resources/translations/*.qm` | Compiled catalogs, what the app loads |
| `resources/translations/en_US.ts` | The template the Crowdin recipes regenerate |

`gridplayer/resources/` is generated ([README.md](README.md) boundary 2), but a
catalog is compiled straight from its `.ts`, so rebuilding one language does not
need the icons rebuilt with it — see *Compiling* below.

## What the app loads

`init_translator()` (`gridplayer/main/init_translator.py`) runs once, before the
main window, and installs up to two catalogs:

1. **Qt's own**, asked for as `qtbase_<language>` in Qt's translations directory
   (`:27`). This is what translates OK/Cancel/Yes/No and the standard file
   dialog.
2. **This project's**, as `<language>.qm` out of `gridplayer/resources/translations`
   (`:33`).

The language is the `player/language` setting; `en_US` returns early, there being
nothing to translate into (`:13`). A language whose catalog is missing does not
fail: Qt falls back to the source text, which is the English in the code.

## Topping a language up

```bash
uv run scripts/translations/update_ts.py zh_CN
uv run scripts/translations/update_ts.py zh_CN --apply new-strings.json
```

The script extracts every string the code has, keeps every translation the
catalog already holds, and writes the catalog back with the rest marked
`<translation type="unfinished"></translation>`:

```text
resources\translations\zh_CN.ts: 960 strings, 248 translated, 712 unfinished
57 translated strings the code no longer has:
  Actions / 'Anti-transpose'
  ...
```

Three things about it are deliberate:

* **The code is flattened first**, in a copy. `pylupdate5` only sees a
  `translate()` call whose text begins on the line the call opens on, and
  `AGENTS.md` allows exactly one escape hatch for calls the formatter wrapped:
  building the string in a statement of its own with `# fmt: skip`.
  `flatten_translate_calls.py` rewrites those in the copy so the extraction sees
  them — the same thing `build_ts.sh` does, and the reason a catalog built
  without it is silently short.
* **The catalog it writes carries no `<location>`.** The paths in an extracted
  file name the machine that ran the extraction and the temporary directory it
  used; the per-language files in this repo have never had them, and a diff full
  of somebody's `C:\Users\...` is not a reviewable one. Contexts are written in
  name order and indented two spaces, which is the shape the downloaded files
  already have.
* **What no longer matches is reported, not dropped quietly.** A string the code
  stopped saying is a translation somebody wrote; the run names it so it can be
  looked at rather than discovered later in a Crowdin diff.

`--apply` takes JSON files, each a list of
`{"context": ..., "source": ..., "translation": ...}`. An entry is **refused**
(and left unfinished) when its placeholders, its line breaks, its HTML tags or
its leading/trailing spaces do not survive the trip, and the run says which and
why. Nothing is written half-translated.

## Compiling

`lrelease` is what turns a `.ts` into the `.qm` the app loads. **A PyQt5 wheel
does not ship it** — it ships `pylupdate5`, `pyrcc5` and `pyuic5` only. Qt's own
`qttools` archive has it, and pulling just that is a small download:

```bash
uvx --from aqtinstall aqt install-qt windows desktop 5.15.2 win64_msvc2019_64 \
    --archives qtbase qttools --outputdir build\qt
```

Then, for one language:

```bash
build/qt/5.15.2/msvc2019_64/bin/lrelease resources/translations/zh_CN.ts -silent \
    -qm gridplayer/resources/translations/zh_CN.qm
```

That is the exact command `scripts/qt_resources/build_resources.py:115` runs per
language for `just generate-resources`; running it alone rebuilds one catalog
without touching icons, fonts or the other languages. Drop `-silent` and
`lrelease` prints how many strings a catalog holds and how many are still
unfinished, which is the quickest way to see how far a language is:

```text
Generated 960 translations (712 finished and 248 unfinished).
```

## Qt's own strings

Qt's standard strings — the buttons `QMessageBox` and `QDialogButtonBox` draw
(OK, Cancel, Yes, No, Apply), the file dialog, and everything else Qt says on its
own — are a **separate catalog** from this project's, asked for as
`qtbase_<language>.qm`. Nothing in this repository's `.ts` files touches them.

It is worth knowing that **no Qt release ships a Simplified Chinese one**: the
Qt 5.15.2 `qttranslations` archive has `qtbase_zh_TW.qm` and a `qt_zh_CN.qm`
meta-catalog that depends on the missing `qtbase_zh_CN.qm`, and PyQt5's wheels
carry the same set. The translation itself, however, is all but complete
upstream — `qtbase_zh_CN.ts` on the `5.15` branch of Qt's `qttranslations`
holds **1696 finished strings and 90 untranslated**, which is why compiling it
is worth doing rather than waiting:

```bash
curl -o build/qtbase_zh_CN.ts \
    "https://code.qt.io/cgit/qt/qttranslations.git/plain/translations/qtbase_zh_CN.ts?h=5.15"
build/qt/5.15.2/msvc2019_64/bin/lrelease build/qtbase_zh_CN.ts \
    -qm resources/translations/qtbase_zh_CN.qm
```

The result is **shipped with the application**: `resources/resources.csv` lists
it as a `file` row, so `just generate-resources` copies it to
`gridplayer/resources/translations/qtbase_zh_CN.qm`, and `init_translator()`
looks there *before* the Qt the machine has (`init_translator.py:26`). Nothing
about the other languages changes: Qt does ship their catalogs, so the fallback
covers them.

Before this was carried, startup logged `Failed to load QT translation for
zh_CN` and every standard button stayed English. It no longer does either.

## Carried by hand, against the Crowdin recipes

`scripts/translations/*` and the `just translations-*` recipes still exist and
still point at Crowdin. Their round-trip **overwrites** the `.ts` files here, so
a language translated in this tree is either exported to Crowdin before the next
round or checked back in after one. Saying so where the round-trip happens is the
whole of the protection, which is why `AGENTS.md` says it.

Current state: **`zh_CN` is carried by hand**; the other languages are as they
were last downloaded.

## Checking a language

* `uv run pytest tests/test_translation_extraction.py tests/test_translation_timing.py`
  — the strings are still written where `pylupdate5` can see them, and nothing
  translates before the translator is installed.
* `lrelease` without `-silent`, on the catalog, for the finished/unfinished count.
* Load the `.qm` offscreen and ask it for a few strings — `QTranslator` on the
  file `init_translator()` loads, then `QCoreApplication.translate(context, source)`,
  which is exactly what the widgets ask. Anything that comes back equal to the
  source text is a string no catalog covers.
* `uv run gridplayer` with the language set, for the parts only an eye can check:
  whether a menu is wide enough for what it now says.
