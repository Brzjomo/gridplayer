"""The build scripts, and the little they ask the machine for.

A Windows build used to want four things that are not on a Windows checkout --
`just`, `wget`, `zip` and a `python3` to make its virtualenv with -- and three
of them have a stand-in that is already there: `python`, `curl`, and Python's
own `zipfile`. The scripts fall back to those, and this holds them to it, since
the fallback is the only path a Windows checkout has.

Run through bash, which is what the build scripts are written in: on Windows
that is Git Bash specifically, and `System32\\bash.exe` is WSL, which is not
where they run and has none of the tools they use.
"""

import os
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent


def _bash() -> str | None:
    if os.name != "nt":
        return shutil.which("bash")

    for program_files in ("ProgramFiles", "ProgramFiles(x86)", "LocalAppData"):
        base = os.environ.get(program_files)

        if not base:
            continue

        for relative in ("Git/bin/bash.exe", "Programs/Git/bin/bash.exe"):
            candidate = Path(base) / relative

            if candidate.exists():
                return str(candidate)

    return None


BASH = _bash()

needs_bash = pytest.mark.skipif(
    BASH is None, reason="the build scripts are bash, and there is no bash here"
)


def _run(source: str) -> subprocess.CompletedProcess:
    """Run a snippet with the build scripts sourced, from the checkout.

    Every path crosses as a forward-slashed one -- a Windows backslash is an
    escape to bash -- and a path the snippet prints back is printed the way
    Python wants it, since the two do not agree on Windows.
    """

    script = Path(tempfile.gettempdir()) / "gp_build_check.sh"
    checkout = str(ROOT).replace(os.sep, "/")

    script.write_text(
        "set -e\n"
        f"cd '{checkout}'\n"
        "source scripts/init_app_vars.sh\n"
        'as_path() { if command -v cygpath &> /dev/null; then cygpath -w "$1";'
        ' else printf "%s\\n" "$1"; fi; }\n' + source,
        encoding="utf-8",
    )

    try:
        return subprocess.run(
            [BASH, str(script).replace(os.sep, "/")],
            capture_output=True,
            text=True,
            timeout=120,
        )
    finally:
        script.unlink(missing_ok=True)


@needs_bash
class TestWhatAMachineHasToHave:
    def test_the_interpreter_a_build_is_made_with_is_one_that_is_here(self):
        """`python3` is not on Windows, and the venv used to be made with it."""

        done = _run("build_python\n")

        assert done.returncode == 0, done.stderr

        name = done.stdout.strip()

        assert name, "no interpreter at all was named"

        found = subprocess.run(
            [BASH, "-c", f'command -v "{name}"'],
            capture_output=True,
            text=True,
        )

        assert found.returncode == 0, f"{name} is not on PATH"

    def test_an_interpreter_that_will_not_run_is_passed_over(self):
        """`command -v python3` finds the Windows Store's redirector on a
        checkout whose Store Python is turned off. It is not an interpreter: it
        exits 49 and prints nothing, and a build that takes it for one dies at
        the first thing it runs -- measured here, `zip_dir` on a Windows
        checkout, with nothing on stdout or stderr to say why.
        """

        done = _run(
            'stub="$(mktemp -d)"\n'
            "printf '#!/bin/sh\\nexit 49\\n' > \"$stub/python3\"\n"
            'chmod +x "$stub/python3"\n'
            'chosen="$(PATH="$stub:$PATH" build_python)"\n'
            'printf "%s\\n" "$chosen"\n'
        )

        assert done.returncode == 0, done.stderr

        chosen = done.stdout.strip()

        assert chosen != "python3", "the stub on PATH was taken for an interpreter"

        if chosen:
            runs = subprocess.run(
                [BASH, "-c", f'"{chosen}" -c ""'],
                capture_output=True,
                text=True,
            )

            assert runs.returncode == 0, f"{chosen} does not run"

    def test_the_requirements_a_build_installs_are_compiled_when_they_are_missing(
        self,
    ):
        """`scripts/build_win.cmd` does not go through `just`, and
        `just build-requirements` is what used to compile this file. Without it
        the build died at `pip install -r` naming a path nothing had written --
        the one step of a build that no script did at all.
        """

        done = _run(
            'stub="$(mktemp -d)"\n'
            "cat > \"$stub/uv\" <<'STUB'\n"
            "#!/bin/sh\n"
            'printf "%s\\n" "$@" > "$(dirname "$0")/args"\n'
            'printf "called\\n" >> "$(dirname "$0")/calls"\n'
            "while [ $# -gt 0 ]; do\n"
            '    if [ "$1" = "-o" ]; then printf "stub==1\\n" > "$2"; fi\n'
            "    shift\n"
            "done\n"
            "STUB\n"
            'chmod +x "$stub/uv"\n'
            'BUILD_DIR="$(mktemp -d)"\n'
            'PATH="$stub:$PATH" make_requirements\n'
            'PATH="$stub:$PATH" make_requirements\n'
            'printf "calls=%s\\n" "$(grep -c called "$stub/calls")"\n'
            'printf "requirements=%s\\n" "$(cat "$BUILD_DIR/requirements.txt")"\n'
            'cat "$stub/args"\n'
        )

        assert done.returncode == 0, done.stderr

        # asked for once and kept: a second build reuses what was resolved
        assert "calls=1" in done.stdout
        assert "requirements=stub==1" in done.stdout
        assert "pyproject.toml" in done.stdout
        assert "--universal" in done.stdout

    def test_a_download_already_made_is_left_alone(self):
        """A second build reuses the VLC it embedded last time rather than
        fetching 83 MB again."""

        done = _run(
            'file="$(mktemp -d)/already.zip"\n'
            "printf 'not a zip\\n' > \"$file\"\n"
            'download https://example.invalid/vlc.zip "$file"\n'
            'cat "$file"\n'
        )

        assert done.returncode == 0, done.stderr
        assert done.stdout.strip() == "not a zip"

    def test_inno_setup_is_taken_from_the_environment_where_it_is_set(self):
        """It is not on PATH however it was installed, so the environment gets
        the first say -- and a bad path says so rather than being ignored."""

        done = _run("ISCC=/bin/echo find_iscc\n")

        assert done.returncode == 0, done.stderr
        assert done.stdout.strip() == "/bin/echo"

        bad = _run("ISCC=/nowhere/iscc find_iscc\n")

        assert bad.returncode != 0
        assert "not an executable" in bad.stderr

    def test_a_build_environment_that_cannot_import_qt_is_refused(self):
        """A venv holding PyQt5 built for another Python freezes into an
        application that dies on its first import, and PyInstaller collects no
        sip module and reports nothing. The build asks first, and names the
        directory to remove."""

        done = _run(
            'venv="$(mktemp -d)/venv"\n'
            'mkdir -p "$venv/bin"\n'
            "printf '#!/bin/sh\\nexit 1\\n' > \"$venv/bin/python\"\n"
            'chmod +x "$venv/bin/python"\n'
            'check_pyqt "$venv"\n'
        )

        assert done.returncode != 0
        assert "cannot import PyQt5" in done.stderr
        assert "rm -rf" in done.stderr


@needs_bash
class TestZippingWithoutZip:
    def test_the_directory_the_portable_build_looks_for_survives(self):
        """The portable build is a zip with an empty directory in it -- the
        data directory it looks for beside the executable -- and an empty
        directory is the one thing a careless walk leaves out.

        Which of the two zips made it is whatever the machine has: Windows
        checkouts have none, so this is the Python fallback there, and a
        machine with `zip` installed covers that path instead.
        """

        done = _run(
            'tree="$(mktemp -d)"\n'
            'mkdir -p "$tree/App/portable_data" "$tree/App/sub"\n'
            'echo hello > "$tree/App/file.txt"\n'
            'cd "$tree" && zip_dir out.zip App\n'
            'as_path "$tree/out.zip"\n'
        )

        assert done.returncode == 0, done.stderr

        with zipfile.ZipFile(done.stdout.strip()) as archive:
            names = archive.namelist()

            assert archive.read("App/file.txt") == b"hello\n"

        assert "App/portable_data/" in names
        assert "App/sub/" in names
