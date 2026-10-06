"""The dev run script, and the promises dev_docs/run-and-test.md makes for it.

A checkout's one command for running the app or the suite is a convenience
that works by being predictable: it finds VLC where it said it would, it sets
the two variables python-vlc needs together, and it does not carry a path from
somebody's machine. Each of those is checkable without running anything, which
is the point -- the failures they prevent show up on a machine the author does
not have.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "run_dev.cmd"
DOC = ROOT / "dev_docs" / "run-and-test.md"


@pytest.fixture(scope="module")
def script() -> str:
    assert SCRIPT.is_file(), f"{SCRIPT} is missing"

    return SCRIPT.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def doc() -> str:
    assert DOC.is_file(), f"{DOC} is missing"

    return DOC.read_text(encoding="utf-8")


class TestTheDevRunScript:
    def test_the_document_names_the_script(self, doc):
        assert "run_dev.cmd" in doc

    @pytest.mark.parametrize(
        "rung",
        [
            # 1. the environment, which wins
            "PYTHON_VLC_LIB_PATH",
            "PYTHON_VLC_MODULE_PATH",
            # 2. a VLC unpacked anywhere
            "GRIDPLAYER_VLC_DIR",
            # 3. a normal installation
            "VideoLAN\\VLC",
            # 4. what the packaging script unpacks
            "build\\vlc-",
        ],
    )
    def test_vlc_is_looked_for_at_every_rung_of_the_ladder(self, script, doc, rung):
        assert rung in script, f"the script no longer looks in {rung}"
        assert rung in doc, f"the document no longer tells anyone about {rung}"

    def test_both_vlc_variables_are_set_together(self, script):
        """A library with no plugin directory loads, and then reads nothing.

        That is the failure mode the whole ladder exists for, so the pair is
        set from one directory rather than one at a time from wherever.
        """

        assert 'set "PYTHON_VLC_LIB_PATH=%VLC_DIR%\\libvlc.dll"' in script
        assert 'set "PYTHON_VLC_MODULE_PATH=%VLC_DIR%\\plugins"' in script

    def test_the_only_absolute_path_in_it_is_the_placeholder(self, script):
        """It has to work on a machine nobody has seen."""

        assert re.findall(r"[A-Za-z]:\\[^\s\"']*", script) == ["C:\\path\\to\\vlc"]

    def test_it_is_lf_ended_and_label_free(self):
        """`cmd` reads a batch file with LF endings badly around labels.

        `.gitattributes` gives every file here LF, so a `goto` or a
        `call :label` is a script that works for its author and stops working
        once it is committed.
        """

        raw = SCRIPT.read_bytes()

        assert b"\r\n" not in raw
        assert "goto" not in raw.decode("utf-8").lower()
        assert "call :" not in raw.decode("utf-8").lower()

    def test_both_jobs_are_what_it_runs(self, script):
        assert "--tests" in script
        assert "uv run pytest" in script
        assert "uv run gridplayer" in script
