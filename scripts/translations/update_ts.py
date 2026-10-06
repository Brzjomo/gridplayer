"""Keep a language's .ts in step with the strings the code has.

    uv run scripts/translations/update_ts.py zh_CN
    uv run scripts/translations/update_ts.py zh_CN --apply new-strings.json

Extraction follows `build_ts.sh`: a copy of the code is flattened first,
because pylupdate5 cannot see a `translate()` call the formatter wrapped -- the
one escape hatch AGENTS.md allows. Everything the file already says is kept;
what the code has grown since is added as unfinished.

The catalog is written in the shape the per-language files are kept in -- no
`<location>`, two space indent, contexts in name order -- because the paths in
an extracted file name the machine that ran it and have no business in the
repo.

`--apply` takes JSON files, each a list of `{context, source, translation}`.
Anything whose placeholders, line breaks or tags do not survive the trip is
left unfinished and reported, rather than written out broken.
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from collections import OrderedDict
from pathlib import Path
from xml.etree import ElementTree
from xml.sax.saxutils import escape, unescape

APP_MODULE = "gridplayer"
LANGUAGES_DIR = Path("resources/translations")
SCRATCH_DIR = Path("build/ts-scratch")

PLACEHOLDER = re.compile(r"\{[A-Z_]+\}|%\d|%[a-z]")
TAG = re.compile(r"</?[a-zA-Z][^>]*>")


def find_pylupdate() -> str:
    found = shutil.which("pylupdate5")

    if found:
        return found

    beside = Path(sys.executable).parent / "pylupdate5"

    for candidate in (beside, beside.with_suffix(".exe")):
        if candidate.is_file():
            return str(candidate)

    raise SystemExit("pylupdate5 not found: run this with uv run")


def extract(language: str) -> Path:
    """Every string the code has, as pylupdate5 sees the flattened code."""

    app_dir = Path(APP_MODULE)
    scripts_dir = Path(__file__).resolve().parent

    if not app_dir.is_dir():
        raise SystemExit(f"{app_dir} is not here: run this from the repo root")

    SCRATCH_DIR.mkdir(parents=True, exist_ok=True)

    out_file = (SCRATCH_DIR / f"{language}-extracted.ts").resolve()
    out_file.unlink(missing_ok=True)

    with tempfile.TemporaryDirectory(prefix="gridplayer-ts-") as tmp:
        code = Path(tmp) / APP_MODULE

        # only the python matters to the extractor, and the copy is what gets
        # rewritten: the real sources must not move under anyone's feet
        for path in app_dir.rglob("*.py"):
            dst = code / path.relative_to(app_dir)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(path, dst)

        subprocess.run(
            [
                sys.executable,
                str(scripts_dir / "flatten_translate_calls.py"),
                str(code),
            ],
            check=True,
        )

        subprocess.run(
            [
                find_pylupdate(),
                "-noobsolete",
                *(str(path.relative_to(code)) for path in sorted(code.rglob("*.py"))),
                "-ts",
                str(out_file),
            ],
            check=True,
            cwd=code,
        )

    return out_file


def read_messages(ts_file: Path):
    """The messages of a catalog, in the order it holds them."""

    if not ts_file.is_file():
        return OrderedDict()

    contexts = OrderedDict()

    for context in ElementTree.parse(ts_file).getroot().findall("context"):
        name = context.findtext("name")
        messages = []

        for message in context.findall("message"):
            translation = message.find("translation")

            finished = translation is not None and (
                translation.get("type") != "unfinished"
            )

            messages.append(
                (message.findtext("source"), translation.text if finished else None)
            )

        contexts[name] = messages

    return contexts


def read_applied(paths) -> dict:
    """Translations handed in, keyed by (context, source)."""

    applied = {}

    for path in paths or []:
        for entry in json.loads(Path(path).read_text(encoding="utf-8")):
            applied[(entry["context"], entry["source"])] = entry["translation"]

    return applied


def complaint(source: str, translation: str) -> str | None:
    """Why this translation must not be written, or None."""

    if not translation.strip():
        return "empty"

    if sorted(PLACEHOLDER.findall(source)) != sorted(PLACEHOLDER.findall(translation)):
        return "placeholders differ"

    if source.count("\n") != translation.count("\n"):
        return "line breaks differ"

    if TAG.findall(source) != TAG.findall(translation):
        return "tags differ"

    if source.startswith(" ") != translation.startswith(" ") or source.endswith(
        " "
    ) != translation.endswith(" "):
        return "leading or trailing space differs"

    return None


def left_in_english(source: str, translation: str) -> bool:
    """Says the same thing, in a string that had words to translate.

    Not a refusal: VLC's own mode names, key names and the names of things
    nobody translates are the same in every language. It is said out loud so
    that a string somebody simply forgot to translate is visible.
    """

    return translation == source and bool(re.search(r"[A-Za-z]{3}", source))


def render(language: str, messages, translations) -> str:
    """The catalog as the repo keeps it: sorted, indented, no locations."""

    lines = [
        '<?xml version="1.0" encoding="utf-8"?>',
        "<!DOCTYPE TS>",
        f'<TS version="2.1" language="{language.replace("_", "-")}"'
        ' sourcelanguage="en">',
    ]

    for context in sorted(messages):
        lines.append("  <context>")
        lines.append(f"    <name>{escape(context)}</name>")

        for source, was in messages[context]:
            translated = translations.get((context, source)) or was

            lines.append("    <message>")
            lines.append(f"      <source>{quoted(source)}</source>")

            if translated is None:
                lines.append('      <translation type="unfinished"></translation>')
            else:
                lines.append(f"      <translation>{quoted(translated)}</translation>")

            lines.append("    </message>")

        lines.append("  </context>")

    lines.append("</TS>")

    return "\n".join(lines) + "\n"


def quoted(text: str) -> str:
    return escape(text, {'"': "&quot;"})


def counts(messages, translations):
    total = sum(len(entries) for entries in messages.values())
    done = 0

    for context, entries in messages.items():
        for source, was in entries:
            if (translations.get((context, source)) or was) is not None:
                done += 1

    return total, done


def main():
    # a Windows console is not UTF-8, and these reports are full of Chinese
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("language", help="catalog to update, e.g. zh_CN")
    parser.add_argument(
        "--apply",
        action="append",
        metavar="JSON",
        help="translations to fill in, as {context, source, translation}",
    )

    args = parser.parse_args()

    ts_file = LANGUAGES_DIR / f"{args.language}.ts"

    extracted = read_messages(extract(args.language))
    existing = read_messages(ts_file)
    applied = read_applied(args.apply)

    translations = {}
    refused = []
    english = []

    for context, entries in extracted.items():
        for source, _was in entries:
            key = (context, source)

            if key in applied:
                reason = complaint(source, applied[key])

                if reason:
                    refused.append((reason, context, source, applied[key]))
                    continue

                if left_in_english(source, applied[key]):
                    english.append((context, source))

                translations[key] = applied[key]

    # what the file already said, for the strings that are still there
    kept = {}

    for context, entries in existing.items():
        for source, was in entries:
            if was is not None:
                kept[(context, source)] = was

    translations = {**kept, **translations}

    total, done = counts(extracted, translations)

    ts_file.write_text(
        render(args.language, extracted, translations), encoding="utf-8", newline="\n"
    )

    print(f"{ts_file}: {total} strings, {done} translated, {total - done} unfinished")

    if refused:
        print(f"\n{len(refused)} translations refused, left unfinished:")

        for reason, context, source, translation in refused:
            print(f"  [{reason}] {context} / {source!r} -> {translation!r}")

    if english:
        print(f"\n{len(english)} left as they are, which may be on purpose:")

        for context, source in english:
            print(f"  {context} / {source!r}")

    # anything the file no longer has a use for, said out loud rather than
    # dropped quietly: a string the code stopped saying is a translation
    # somebody wrote that is now worth nothing
    in_code = {
        (context, source)
        for context, entries in extracted.items()
        for source, _was in entries
    }
    gone = sorted(set(kept) - in_code)

    if gone:
        print(f"\n{len(gone)} translated strings the code no longer has:")

        for context, source in gone[:20]:
            print(f"  {context} / {source!r}")

        if len(gone) > 20:
            print(f"  ... and {len(gone) - 20} more")


if __name__ == "__main__":
    sys.exit(main())
