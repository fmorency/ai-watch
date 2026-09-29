# Building and releasing

The [Release workflow](https://github.com/fmorency/ai-watch/blob/main/.github/workflows/release.yml) makes one native,
standalone executable for each supported target:

| Archive suffix | Runner / build baseline |
| --- | --- |
| `linux-x86_64` | Ubuntu 22.04, x86-64 |
| `linux-arm64` | Ubuntu 22.04, ARM64 |
| `macos-x86_64` | macOS 15, Intel |
| `macos-arm64` | macOS 14, Apple Silicon |

Each `.tar.gz` contains:

```text
ai-watch-v0.1.0-linux-x86_64/
  ai-watch                 # single executable, including Python
  ai-usage -> ai-watch      # one-shot command; no second binary
  examples/config.json
  README.md
  LICENSE
  CONTRIBUTING.md
  docs/RELEASING.md
  BUILD-INFO.json           # source commit and build tool versions
  licenses/                # Python, PyInstaller, and certificate-data licenses
```

`SHA256SUMS` accompanies the four archives. Packaging uses an explicit file list;
local configuration and credentials are never included.

## Publish a version

1. Set the same version in `pyproject.toml` and `ai_watch/__init__.py`.
2. Commit and push the changes, then wait for CI.
3. Create and push the matching version tag, for example:

   ```sh
   git tag -a v0.1.0 -m 'ai-watch v0.1.0'
   git push origin v0.1.0
   ```

The tag must exactly match `vMAJOR.MINOR.PATCH` and the package version. A mismatch
stops the workflow before builds begin. The workflow runs the tests, builds on
each native runner, and tests the extracted archives before collecting checksums.
Only a complete set of passing builds proceeds to publication.

Publication creates a draft, attaches the archives and checksum file, then
publishes it. A failed upload leaves a draft that a rerun can finish. Reruns
refuse to overwrite an already published release; ship a new version instead.

## Test the workflow without publishing

Use **Actions → Release → Run workflow**, or:

```sh
gh workflow run release.yml --ref main
```

Manual runs build and test all four archives and upload the combined
`ai-watch-release` workflow artifact, without creating a release or tag. Download
it from the completed run, or with `gh run download RUN_ID -n ai-watch-release`.
Workflow artifacts are kept for 14 days; published release assets persist.

## Build locally

Build on the operating system and architecture you want to distribute. Python
3.12 is used in CI; Python 3.12+ is required for the build scripts.

```sh
python3 -m venv .venv-build
.venv-build/bin/python -m pip install . -r requirements-build.txt
.venv-build/bin/python scripts/release.py build
```

The archive and its individual `.sha256` file appear in `dist/release/`.
The smoke check runs from outside the checkout, verifies the demo with an empty
PATH, checks the `ai-usage` alias, and exercises provider loading with synthetic
accounts. It makes no provider requests.

The executable uses [PyInstaller one-file mode](https://pyinstaller.org/en/stable/operating-mode.html).
It includes Python and certificate data, and unpacks internal libraries into a
temporary directory when run. It is not a static binary. Linux builds target
glibc-based distributions at least as new as Ubuntu 22.04 (glibc 2.35); Alpine's
musl environment is not supported. macOS builds are not Apple-notarized. The
listed macOS versions are the build and test baselines; older releases are not
tested.
