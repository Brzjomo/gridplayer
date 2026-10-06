# Contributing

## Setup

Dependencies:

- [uv](https://docs.astral.sh/uv/)
- [Just](https://github.com/casey/just) (task runner)
- [commit-and-tag-version](https://github.com/absolute-version/commit-and-tag-version) (for release workflow)
- [conventional-changelog](https://github.com/conventional-changelog/conventional-changelog)
- [VLC](https://www.videolan.org/)
- (Optional)[Deno JS Runtime](https://github.com/yt-dlp/yt-dlp/wiki/EJS)

## Dev install & run

```bash
git clone https://github.com/vzhd1701/gridplayer.git
cd gridplayer
uv sync
uv run gridplayer
```

## Local build on Windows

Run these from **Git Bash** (the build scripts are Bash and use `cygpath`).

Install dependencies:

```bash
choco install just zip innosetup
```

Build:

```bash
just build-win-package
```

That is the 64-bit package on a 64-bit Python. For the 32-bit one, use a 32-bit
Python and `BUILD_ARCH=win32 just build-win-package` (it has no browser
impersonation, see [pyproject.toml](pyproject.toml)).

The artifacts land in `dist/`: `GridPlayer-<version>-win64-install.exe`,
`GridPlayer-<version>-win64-portable.zip`, and the `GridPlayer/` folder
PyInstaller produced. How to get the toolchain, what `BUILD_ARCH` changes and
what a previous build leaves behind:
[dev_docs/build-and-release.md](dev_docs/build-and-release.md).

## Local build on MacOS

Install dependencies:

```bash
./scripts/macos/_init_local_env.sh
```

Build the native Apple Silicon package:

```bash
just build-macos-package-arm64
```

The default macOS build target is `arm64`. If you need an Intel build, run the same
scripts from a native `x86_64` Python environment with `BUILD_MACOS_ARCH=x86_64`.

## Local build on Ubuntu

Install dependencies:

```bash
sudo apt install appstream-util
```

When new resources are added, the compiled resources inside `gridplayer/resources` need to be updated

```bash
sudo apt install qttools5-dev-tools		# for lrelease
just generate-resources
```

Build:

```bash
just build-linux-appimage
```

## Release

1. State changes in `CHANGELOG.md` inside `## [Unreleased]` section. Use [Keep a Changelog](https://keepachangelog.com/en/1.0.0/) format.

As a guide, print changes from recent commits:

```bash
just changelog       # only important changes
just changelog-all   # full changelog
```

2. See what will be changed:

```bash
commit-and-tag-version --dry-run
```

3. Commit and create release tag:

```bash
commit-and-tag-version
```
