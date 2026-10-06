"""Strings `pylupdate5` will never see, and the files that may not add any.

`pylupdate5` reads a call's text only when that text begins on the same
source line as the context. Python joins adjacent literals long before
anything else sees them, so a call whose text begins on the next line is
perfectly ordinary code, passes every other test here, and extracts nothing
at all -- it shows in English in every language for ever. Measured when the
alignment dialog was reworked: 100 calls in the package were like that,
including the whole of the settings form.

`ruff format` is what puts the text there: any call that does not fit on one
line gets its arguments split, and the text ends up on a line of its own.
The cure is to keep the call as a statement of its own with the text starting
beside the context, and to hold the formatter off with `# fmt: skip` on the
statement's last line -- a marker on a line of its own is refused by RUF028.

The two files this feature added are held to none at all. The rest are held
to not getting worse, until somebody sweeps them.
"""

import ast
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[1] / "gridplayer"

TRANSLATORS = {"translate", "tr"}

# What this feature's own files may have: nothing unreachable.
FEATURE_FILES = (
    Path("dialogs") / "align_videos.py",
    Path("widgets") / "sync_strip.py",
)

# Calls that were already unreachable when this was written and are not this
# change's to fix. The number may only go down: a new one is a string that no
# translator will ever be given.
UNREACHABLE_AT_WRITING = 82


def unreachable_calls(path: Path) -> list[int]:
    """Lines of calls whose text begins after the line the context is on."""

    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)

        if name not in TRANSLATORS or len(node.args) < 2:
            continue

        context, text = node.args[:2]

        if not isinstance(context, ast.Constant) or not isinstance(text, ast.Constant):
            continue

        if text.lineno > context.end_lineno:
            found.append(text.lineno)

    return found


def all_unreachable() -> dict[str, list[int]]:
    found = {}

    for path in sorted(PACKAGE.rglob("*.py")):
        # the vendored binding is not this project's to translate
        if path.name == "vlc.py":
            continue

        lines = unreachable_calls(path)

        if lines:
            found[str(path.relative_to(PACKAGE))] = lines

    return found


@pytest.mark.parametrize("relative_path", FEATURE_FILES)
def test_the_alignment_dialog_leaves_nothing_unreachable(relative_path):
    """Every string in it has to be one a translator can be handed."""

    lines = unreachable_calls(PACKAGE / relative_path)

    assert not lines, f"{relative_path}: unreachable at lines {lines}"


def test_no_more_calls_are_out_of_reach_than_were():
    found = all_unreachable()
    total = sum(len(lines) for lines in found.values())

    listing = "\n".join(
        f"  {name}: {', '.join(str(n) for n in lines)}"
        for name, lines in sorted(found.items())
    )

    assert total <= UNREACHABLE_AT_WRITING, (
        f"{total} calls hand pylupdate5 more than one literal, and"
        f" {UNREACHABLE_AT_WRITING} did when this was written:\n{listing}"
    )
