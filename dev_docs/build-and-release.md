# Build and release

Packaging for each platform, CI, and the release procedure.

## What gets shipped

| Target | Produced by | Artifact |
| --- | --- | --- |
| Python package | `uv build` | sdist + wheel on PyPI |
| Windows | PyInstaller + Inno Setup + zip | `-win64-install.exe`, `-win64-portable.zip`, 32-bit equivalents |
| macOS | PyInstaller + `create-dmg` | `.dmg`, per-architecture |
| Linux AppImage | `linuxdeploy`-style script + Docker | `.AppImage` |
| Linux Snap | `snapcraft` | snap in the Snap Store |
| Linux Flatpak | `flatpak-builder` | Flathub manifest |
| Windows Chocolatey | `choco pack` | `.nupkg` |

CI builds **x86 and x64 on Windows**, **x64 and arm64 on macOS**, and x64 on
Linux. macOS default is `arm64`.

## Versioning

The version lives in **two places that must stay in step**, and both are rewritten
by the release tooling:

* `pyproject.toml` → `version = "0.5.5"`
* `gridplayer/version.py` → `__version__` and `__version_date__`

`gridplayer/version.py` also holds the identity constants used everywhere else:

```python
__app_name__ = "GridPlayer"
__display_name__ = "GridPlayer"
__author_name__ = "vzhd1701"
__author_contact__ = "vzhd1701@gmail.com"
__app_id__ = "com.vzhd1701.gridplayer"
__version__ = "0.5.5"
__version_date__ = "2026-07-24"
__app_url__ = "https://github.com/vzhd1701/gridplayer"
__app_license_url__ = "..."
__app_bugtracker_url__ = "..."
```

These are read by `scripts/init_app_vars.sh` with `sed` to export `APP_*`
environment variables for every build script, and by the application itself
(`main/init_app_env.py`, `dialogs/about.py`, `main/init_cli.py`). **`__app_id__`
matters functionally**, not just for packaging: it is the Windows AppUserModelID
and the macOS bundle identifier, so changing it orphans users' data directories
and taskbar pins.

`README.md` also contains the version, in the download URLs. It is rewritten by
the release tool too.

### Dev snapshots

`scripts/stamp_dev_version.py` (run by `just stamp-dev-version`) rewrites
`__version__` to `X.Y.Z.dev.<7-char sha>` and `__version_date__` to the commit
date. It is **idempotent in a specific way**: the regex matches `X.Y.Z` followed by
anything, so stamping an already-stamped file starts over from `X.Y.Z` rather than
appending a second suffix. Every CI job building the same commit therefore stamps
an identical version.

`init_app_vars.sh` then branches on the `.dev.` marker:

| | Dev snapshot | Release |
| --- | --- | --- |
| `APP_FILE_VERSION` | `dev` | `X.Y.Z` |
| `APP_CHANGELOG_VERSION` | `unreleased` | `X.Y.Z` |
| `APP_UPDATE_RELEASE` | `continuous` | `latest` |
| CDN root | `@vX.Y.Z` of the **last release** | `@vX.Y.Z` |

The point (comment at `init_app_vars.sh:130`) is that a "continuous" pre-release
has **fixed file names**, so its download links never change, it takes its notes
from unreleased changes, and AppImage updates come from that pre-release rather
than from the latest stable release.

`APP_CDN_URL_ROOT` deliberately borrows the last release's tag, because a dev
snapshot has no tag of its own.

## Prerequisites

| Tool | Needed for |
| --- | --- |
| `uv` | Python package build, requirements export |
| `just` | All the recipes |
| Git + Bash | the build scripts are Bash, including on Windows (Git Bash / MSYS) |
| Docker | AppImage build (it builds in a container) |
| `choco install zip innosetup` | Windows packaging |
| `snapcraft`, `flatpak-builder` | Linux packaging |
| `gsed`, `create-dmg` | macOS packaging (`./scripts/macos/_init_local_env.sh` installs them) |
| `commit-and-tag-version`, `conventional-changelog` | the release workflow |
| `qttools5-dev-tools` (`lrelease`), `pyuic5`, `pyrcc5` | regenerating resources and UI |

## Build recipes

All from the `justfile`:

```bash
just build                          # uv build: sdist + wheel
just build-wheel
just build-sdist

just generate-ui                    # → gridplayer/**/*_ui.py
just generate-resources             # → gridplayer/resources/**

just build-win-pyinstaller
just build-win-package              # + installer and portable zip
just build-win-chocolatey

just build-macos-pyinstaller        # x86_64 by default
just build-macos-package
just build-macos-pyinstaller-arm64
just build-macos-package-arm64

just build-linux-meta
just build-linux-appimage
just build-linux-snap
just build-linux-flatpak

just clean                          # rm -rf dist build
just clean-pyinstaller-dist
```

Dependency chains worth knowing:

* `build-win-package` → `build-win-pyinstaller` → `build-requirements`.
* `build-linux-appimage` → `build-wheel` + `build-linux-meta`.
* `build-linux-flatpak` additionally needs `build-requirements` and
  `build-sdist`, and has a `-git` variant that builds from a Git checkout.
* `build-requirements` writes `build/requirements.txt` via
  `uv pip compile --universal`, and only if the file does not already exist
  (so `just clean` is how you refresh it).

### `scripts/init_app_vars.sh`

Every build script sources this first. It:

* Defines `die`, a `realpath` fallback, and a `sed` wrapper around `gsed` on
  macOS (`gsed` is required there).
* Exports `ROOT_DIR`, `RESOURCES_DIR`, `BUILD_DIR`, `DIST_DIR`, `SCRIPTS_DIR`,
  `APP_MODULE`, `APP_BASE_DIR`, `APP_REPO_SLUG`.
* Reads every `__*__` constant out of `gridplayer/version.py` into `APP_*`.
* **Validates the version** against `^[0-9]+\.[0-9]+\.[0-9]+` and dies otherwise,
  then exports `APP_VERSION_NUMERIC` from the match.
* Computes the dev/release branch described above.
* Provides `replace_app_vars` / `copy_with_app_vars`, which substitute `{APP_*}`
  placeholders in template files — that is how `.spec`, `.iss`, `.desktop`,
  `.appdata.xml` and the Chocolatey `.nuspec` get real values. **It dies if any
  `APP_*` variable used in a template is empty**, which is the intended
  fail-fast behaviour.
* Provides `init_venv` / `activate_venv` for build-time virtualenvs.

### Windows: `scripts/pyinstaller/build_win.sh`

Notable details:

* Pins `PYINSTALLER_VERSION` and a specific VLC release
  (`https://get.videolan.org/vlc/3.0.24/<arch>/vlc-3.0.24-<arch>.zip`), so a
  Windows build embeds a known VLC rather than relying on a system install.
* Sets `PIP_NO_BINARY=pydantic` — "Reduce size by installing src version of
  pydantic".
* **32-bit special case:** `curl-cffi` has no 32-bit Windows wheel and its sdist
  builds against a libcurl DLL nothing bundles, so
  `requirements-win32.txt` is generated by grepping `curl-cffi` out of the
  requirement list. **Browser impersonation is therefore absent from the 32-bit
  build.** `pyproject.toml:40` explains the same thing from the dependency side.
* Embeds `libVLC` by unzipping the VLC release into `$BUILD_DIR/libVLC` and
  copying **only the plugin directories VLC needs** — access, audio_filter,
  audio_mixer, codec, demux, misc, packetizer, stream_filter, video_chroma,
  video_output, d3d9, d3d11, plus one audio output (directsound).
* Three plugins get an explicit comment because their absence is silent:

  > Subtitles need both of these, and neither says so when it is missing: the
  > track is decoded and selected, and nothing is ever drawn. blend composites
  > the subpicture onto the frame, scale fits bitmap subtitles (DVD, PGS) to it.

  Along with `video_filter/libtransform_plugin.dll` (for the transform
  features) and `text_renderer` (freetype, for text subtitles).
* Reading a snippet of sound — what **Align By Sound** does — needs three more:
  `stream_out` (the `#transcode` and `#std` modules that write the snippet),
  `mux/libmux_wav_plugin.dll` (the wav it is written as) and
  `access_output/libaccess_output_file_plugin.dll` (the file it is written to).
  None of the three is needed to play a video, and **leaving out any one of
  them was measured to leave the probe reading nothing at all** in a payload
  built from this script's own list, with the dialog saying nothing about it.
* Runs `vlc-cache-gen.exe` to build `plugins.dat`, which is why
  `InstanceVLC.init_options` can add `--no-plugins-scan` — but only when the
  cache is a real one: a cache written for an empty directory is 24 bytes, and
  the app then tells VLC not to look for plugins it does not have, so **no
  video plays at all** while everything else about the app looks healthy.
  `MIN_PLUGIN_CACHE_BYTES` (`vlc_player/instance.py`) is what refuses a stub;
  `tests/test_vlc_plugin_cache.py` covers it.
* The whole `libVLC` directory is finally moved into `dist/$APP_NAME/libVLC`,
  which is exactly where `libvlc._get_embed_vlc_root` looks for a frozen build.

**If you add a VLC feature that needs a plugin, add its directory to this list.**
The failure mode is a feature that silently does nothing in shipped builds.

`tests/test_sync_audio.py::TestWhatAPackagedBuildMustCarry` and
`tests/test_subtitles_are_reachable.py::TestTheBuildsShipWhatDrawsThem` hold the
list to the script, so dropping one of these lines fails the suite rather than
the release.

### macOS

`build_mac.sh` uses `pyinstaller_mac.spec` and `mime_vlc.plist`;
`build_dmg.sh` produces the disk image. Architecture is selected by
`BUILD_MACOS_ARCH` (`arm64` or `x86_64`), normalised by
`normalize_macos_arch`, and the artifact gets an `arm64`/`intel64` suffix from
`macos_arch_suffix`.

Because one process cannot draw into another's window on macOS, the hardware
`_sp` driver is the only option there and `VideoDriverManager` rewrites the
setting accordingly — see [playback-and-vlc.md](playback-and-vlc.md).

### Linux

* `linux_meta/build.sh` generates `.desktop`, AppStream `.appdata.xml` and mime
  XML from the templates, using `inject_changelog.py` to fold the changelog in.
* AppImage builds inside Docker (`appimage/build_docker.sh`) and uses `AppRun.c`.
* Snap uses `snapcraft.yaml` and `apt_dl.sh`, with
  `scripts/_helpers/blacklist_snap.txt` excluding files.
* Flatpak has three manifests: `app.yml` (released), `app_git.yml` (Git), and
  `app_local.yml` (local), plus a separate `libvlc/libvlc.yml` that builds VLC
  from source with patches in that directory.

`scripts/_helpers/blacklist_*.txt` files list files to strip, applied by
`blacklist_clean.sh`.

## CI

| Workflow | Trigger | Does |
| --- | --- | --- |
| `test.yml` | push to master/develop, every PR | lint (ruff check, ruff format --check, rumdl check) and pytest on Python 3.10 and 3.14. |
| `build.yml` | called by the release workflows | builds every artifact. |
| `release_github.yml` | tag push `v[0-9]+.[0-9]+.[0-9]+`, or manual dispatch | builds, then creates a **draft** GitHub release with extracted release notes. |
| `release_pypi.yml` | — | publishes to PyPI. |
| `release_snap.yml`, `release_flatpak.yml` | — | store publication. |
| `prerelease_github.yml` | — | the "continuous" dev pre-release. |
| `update_crowdin.yml` | — | pushes translation sources. |

`build.yml` structure: a `build_python` job stamps the dev version, exports
requirements, builds the sdist/wheel, and uploads `release_dist_reqs` and
`release_dist_python`. Every platform job then `needs: build_python` and downloads
the requirements artifact — so the platform builds install from a **frozen,
universal requirements file** rather than resolving independently. That is what
keeps a single release's artifacts consistent.

Note that `build.yml` runs `just stamp-dev-version` on **every** build, including
tagged releases; the stamp only takes effect for the artifact's version string,
and a release's real version comes from the tag and the committed file.

`release_github.yml` creates the release as a **draft**, so a human publishes it.

## Release procedure

From `CONTRIBUTING.md`, with the mechanics filled in:

1. **Write the changelog.** State changes in `CHANGELOG.md` under
   `## [Unreleased]`, in [Keep a Changelog](https://keepachangelog.com/en/1.0.0/)
   format. To see what to write:

   ```bash
   just changelog       # only important changes
   just changelog-all   # full changelog
   ```

   Both run `conventional-changelog` over the commit history, so **commit messages
   matter** — this project uses Conventional Commits.

2. **Dry run the version bump:**

   ```bash
   just release-dry            # commit-and-tag-version --dry-run
   ```

3. **Bump and tag:**

   ```bash
   just release               # commit-and-tag-version
   ```

4. **Push the tag** (`vX.Y.Z`) to trigger `release_github.yml`.

5. **Publish the draft release** on GitHub once the artifacts look right.

### What `just release` actually does

`commit-and-tag-version` is configured by `.versionrc.js`, which is worth reading
because it does more than bump a number:

* **`packageFiles`**: `pyproject.toml`, via a regex updater.
* **`bumpFiles`**: the same plus `gridplayer/version.py` (updating *both*
  `__version__` and `__version_date__`, the date being today in `YYYY-MM-DD`) and
  `README.md` (every `\d+\.\d+\.\d+`, which rewrites the download URLs).
* **`skip.changelog: true`** — `commit-and-tag-version` does *not* generate the
  changelog; `keepachangelog` does, from the `Unreleased` section you wrote.
* **`commitAll: true`** and **`sign: true`** — it commits everything and signs the
  commit and tag. Signing requires a configured GPG/SSH key or the release fails.
* **`postbump` script**, run in order:
  1. `uv lock` — refresh the lockfile for the new version,
  2. read the version back out of `gridplayer/version.py`,
  3. `uv run --frozen keepachangelog release "$NEW_VERSION"` — close the
     `Unreleased` section and open a new one,
  4. `rumdl fmt CHANGELOG.md` — format the markdown,
  5. `git add CHANGELOG.md uv.lock`.

So the release commit contains the version bump, the changelog, and the lockfile.

### Pre-commit hooks

`.pre-commit-config.yaml` runs hygiene hooks (`check-vcs-permalinks`,
`end-of-file-fixer`, `trailing-whitespace`, `mixed-line-ending --fix=lf`,
`check-toml`, `check-yaml`, `no-commit-to-branch`) and three local hooks that
**rewrite files**:

* `ruff format`
* `rumdl fmt`
* `ruff check` (with `pass_filenames: false`, so it checks the whole tree)

`no-commit-to-branch` is what stops an accidental commit straight to `master`.

The comment at the top of the file notes the commit-msg hook needs
`pre-commit install --hook-type commit-msg` separately.

## Reproducibility notes

* **VLC is pinned per platform**, not floating: `3.0.24` on Windows, built from
  source for Flatpak, the system/runtime copy elsewhere. A VLC upgrade is a real
  change and should be tested against the subtitle, transform and hardware paths.
* **PyInstaller is pinned** (`6.22.3`).
* **Requirements are resolved once** in CI and shared by all platform jobs.
* **The dev version derives from the commit**, so the same commit always stamps
  the same string.
* `uv.lock` is committed and CI uses `--locked`/`--frozen`, so a dependency
  resolution cannot drift silently.
* `pyproject.toml` pins `PyQt5==5.15.11` exactly, and pins **different `PyQt5-Qt5`
  builds per platform** (`5.15.2` on Windows, `5.15.19` elsewhere) — a platform
  quirk that must be preserved.

## Gotchas

* **Do not hand-edit `gridplayer/resources/`.** It is generated; regenerate with
  `just generate-resources`. The release builds copy it verbatim.
* **A new VLC-dependent feature may need a plugin added to `build_win.sh`**, or it
  will work in development and silently fail in shipped Windows builds.
* **`__app_id__` is load-bearing** — changing it moves user data and breaks
  taskbar/bundle identity.
* **The build scripts are Bash.** On Windows run them from Git Bash; the `just`
  recipes call `./scripts/...` directly.
* **`build-requirements` is cached** by the existence of
  `build/requirements.txt`. If you change dependencies and the build seems to
  ignore it, `just clean` first.
* **`init_app_vars.sh` dies on an unset `APP_*` in a template.** If you add a
  placeholder to a `.spec`/`.iss`/`.desktop` file, the variable must exist.
* **`postbump` runs `uv lock`, which needs network access.** An offline release
  attempt fails at that step, after the files are already rewritten.
* **Signing is on by default** (`sign: true`); a machine without a signing key
  cannot complete `just release`.
* **`rqumdl`/`rumdl` only excludes `LICENSE.md`** and disables `MD013`
  (line length). Long lines are fine; broken link syntax is not.

## Key files

| File | Role |
| --- | --- |
| `justfile` | Every build, generate and release recipe. |
| `pyproject.toml` | Package metadata, dependencies, build backend. |
| `gridplayer/version.py` | Version and identity constants. |
| `.versionrc.js` | The release bump configuration and post-bump steps. |
| `scripts/init_app_vars.sh` | `APP_*` variables, template substitution. |
| `scripts/stamp_dev_version.py` | Dev snapshot versioning. |
| `scripts/pyinstaller/build_win.sh`, `build_mac.sh`, `*.spec` | PyInstaller builds. |
| `scripts/windows/`, `scripts/macos/`, `scripts/chocolatey/` | Installers and packages. |
| `scripts/appimage/`, `scripts/snap/`, `scripts/flatpak/`, `scripts/linux_meta/` | Linux packaging. |
| `scripts/qt_resources/` | UI and resource generation. |
| `.github/workflows/build.yml` | The artifact build matrix. |
| `.github/workflows/release_github.yml` | Tag → draft release. |
| `.github/workflows/test.yml` | Lint and test. |
| `CHANGELOG.md` | The unreleased section the release promotes. |
