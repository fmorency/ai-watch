"""Build and package native executables; also used by release CI."""

import argparse
import hashlib
import importlib.metadata
import json
import platform
import re
import shutil
import subprocess
import sys
import sysconfig
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ai_watch import __version__  # noqa: E402

TARGETS = ("linux-x86_64", "linux-arm64", "macos-x86_64", "macos-arm64")


def release_tag(requested=None):
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", __version__):
        raise ValueError("release versions must use MAJOR.MINOR.PATCH")
    if importlib.metadata.version("ai-watch") != __version__:
        raise ValueError("pyproject.toml and ai_watch/__init__.py versions differ; reinstall with pip install .")
    tag = f"v{__version__}"
    if requested and requested != tag:
        raise ValueError(f"tag {requested!r} does not match project version {tag}")
    return tag


def native_target():
    system = {"Linux": "linux", "Darwin": "macos"}.get(platform.system())
    arch = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine().lower())
    target = f"{system}-{arch}"
    if target not in TARGETS:
        raise ValueError("standalone builds require Linux or macOS on x86-64 or ARM64")
    return target


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def include_licenses(directory):
    directory.mkdir()
    python_license = Path(sysconfig.get_path("stdlib")) / "LICENSE.txt"
    shutil.copyfile(python_license, directory / "Python.txt")
    for name in ("pyinstaller", "certifi"):
        distribution = importlib.metadata.distribution(name)
        licenses = [p for p in distribution.files or []
                    if p.name.upper().startswith(("LICENSE", "COPYING"))]
        if not licenses:
            raise ValueError(f"no license found for bundled component {name}")
        for entry in licenses:
            shutil.copyfile(distribution.locate_file(entry), directory / f"{name}-{entry.name}")


def package(binary, tag, target, output):
    output.mkdir(parents=True, exist_ok=True)
    name = f"ai-watch-{tag}-{target}"
    archive = output / f"{name}.tar.gz"
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    with tempfile.TemporaryDirectory(prefix="ai-watch-package-") as temporary:
        bundle = Path(temporary) / name
        bundle.mkdir()
        shutil.copyfile(binary, bundle / "ai-watch")
        (bundle / "ai-watch").chmod(0o755)
        (bundle / "ai-usage").symlink_to("ai-watch")
        # Explicit allowlist: never collect local config, caches, or credentials.
        for relative in ("README.md", "LICENSE", "CONTRIBUTING.md", "docs/RELEASING.md", "examples/config.json"):
            destination = bundle / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / relative, destination)
        include_licenses(bundle / "licenses")
        metadata = {"version": tag, "target": target, "commit": commit,
                    "python": platform.python_version(),
                    "pyinstaller": importlib.metadata.version("pyinstaller"),
                    "certifi": importlib.metadata.version("certifi")}
        (bundle / "BUILD-INFO.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
        with tarfile.open(archive, "w:gz", dereference=False) as stream:
            stream.add(bundle, arcname=name)
    archive.with_name(archive.name + ".sha256").write_text(f"{sha256(archive)}  {archive.name}\n", encoding="ascii")
    return archive


def check_archive(archive):
    # Check the actual extracted deliverable, including the alias and example.
    with tempfile.TemporaryDirectory(prefix="ai-watch-extract-") as temporary:
        with tarfile.open(archive, "r:gz") as stream:
            stream.extractall(temporary, filter="data")
        bundle, = Path(temporary).iterdir()
        for relative in ("ai-watch", "ai-usage", "README.md", "LICENSE", "BUILD-INFO.json", "examples/config.json"):
            if not (bundle / relative).is_file():
                raise ValueError(f"archive is missing {relative}")
        if not (bundle / "ai-usage").is_symlink() or (bundle / "ai-usage").readlink() != Path("ai-watch"):
            raise ValueError("ai-usage must be a relative symlink to the single executable")
        json.loads((bundle / "examples/config.json").read_text())
        subprocess.run([sys.executable, str(ROOT / "scripts/smoke_binary.py"), str(bundle / "ai-watch")], check=True)


def build(target, tag):
    if target != native_target():
        raise ValueError(f"cannot build {target} on {native_target()}; use a native runner")
    work = ROOT / "build" / "standalone" / target
    work.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--noupx",
        "--name", "ai-watch", "--paths", str(ROOT), "--collect-data", "certifi",
        "--distpath", str(work / "binary"), "--workpath", str(work / "work"),
        "--specpath", str(work), str(ROOT / "scripts/frozen_entry.py"),
    ], cwd=ROOT, check=True)
    archive = package(work / "binary/ai-watch", tag, target, ROOT / "dist/release")
    check_archive(archive)
    print(archive)


def checksums(directory, tag):
    if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", tag):
        raise ValueError("invalid release tag")
    expected = {f"ai-watch-{tag}-{target}.tar.gz" for target in TARGETS}
    archives = sorted(directory.glob("*.tar.gz"))
    if {p.name for p in archives} != expected:
        raise ValueError("expected exactly one archive for each of the four release targets")
    lines = []
    for archive in archives:
        line = f"{sha256(archive)}  {archive.name}\n"
        if archive.with_name(archive.name + ".sha256").read_text(encoding="ascii") != line:
            raise ValueError(f"checksum mismatch for {archive.name}")
        lines.append(line)
    (directory / "SHA256SUMS").write_text("".join(lines), encoding="ascii")
    print("Verified all four archives and wrote SHA256SUMS")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    version = commands.add_parser("version", help="validate source/package/tag versions")
    version.add_argument("--tag")
    version.add_argument("--github-output", type=Path)
    build_parser = commands.add_parser("build", help="build, package, and smoke-test the native executable")
    build_parser.add_argument("--target", choices=TARGETS, default=native_target())
    build_parser.add_argument("--tag")
    sums = commands.add_parser("checksums", help="verify all platform archives and write SHA256SUMS")
    sums.add_argument("directory", type=Path)
    sums.add_argument("--tag", required=True)
    args = parser.parse_args()
    try:
        if args.command == "version":
            tag = release_tag(args.tag)
            print(tag)
            if args.github_output:
                with args.github_output.open("a", encoding="utf-8") as stream:
                    stream.write(f"tag={tag}\n")
        elif args.command == "build":
            build(args.target, release_tag(args.tag))
        else:
            checksums(args.directory, args.tag)
    except (OSError, ValueError, importlib.metadata.PackageNotFoundError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"release: {exc}\n")


if __name__ == "__main__":
    main()
