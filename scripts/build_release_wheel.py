#!/usr/bin/env python3
"""Build the release wheel for one commit, reproducibly (#403).

Usage: python3 scripts/build_release_wheel.py COMMIT [--out DIR]

The commit's tracked tree is exported with `git archive` into a fresh
temporary directory, so nothing from the checkout (build/, *.egg-info, dist/,
untracked or modified files, file times) reaches the build. `pip wheel
--no-deps` then runs there with SOURCE_DATE_EPOCH set to the commit's
committer timestamp, which fixes every zip entry's time. Two builds of one
commit, from any two checkouts, are byte-identical; the live release smoke
rebuilds the tag this way and compares with the published asset.

Maintainer tooling: not part of the wheel.
"""
import argparse
import hashlib
import io
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path


def git(repo: Path, *args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, check=True).stdout


def build(commit: str, out_dir: Path, repo: Path | None = None) -> Path:
    """Build COMMIT's wheel into OUT_DIR and return the wheel's path."""
    repo = Path(repo or Path.cwd())
    sha = git(repo, "rev-parse", "--verify", f"{commit}^{{commit}}").decode().strip()
    epoch = git(repo, "show", "-s", "--format=%ct", sha).decode().strip()
    archive = git(repo, "archive", "--format=tar", sha)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="handsoff-wheel-") as scratch:
        source, wheels = Path(scratch) / "source", Path(scratch) / "wheels"
        source.mkdir()
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            if hasattr(tarfile, "data_filter"):
                tar.extractall(source, filter="data")
            else:
                tar.extractall(source)
        env = {**os.environ, "SOURCE_DATE_EPOCH": epoch}
        subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-deps", "--quiet", "-w", str(wheels), str(source)],
                       cwd=scratch, env=env, check=True, stdout=subprocess.DEVNULL)
        built = sorted(wheels.glob("*.whl"))
        if len(built) != 1:
            raise RuntimeError(f"expected one wheel, pip wrote {[path.name for path in built]}")
        target = out_dir / built[0].name
        shutil.copyfile(built[0], target)
    return target


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _members(wheel: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(wheel)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def compare_wheels(published: bytes, built: bytes) -> None:
    """Return when the two wheels are byte-identical; otherwise raise naming
    both sha256 values and whether the unpacked contents match."""
    if published == built:
        return
    contents = "contents match" if _members(published) == _members(built) else "contents differ"
    raise AssertionError(f"published wheel sha256 {sha256(published)} differs from the built wheel sha256 "
                         f"{sha256(built)}; unpacked {contents}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the release wheel for one commit, reproducibly.")
    parser.add_argument("commit")
    parser.add_argument("--out", default="dist", help="directory for the wheel (default: dist)")
    args = parser.parse_args(argv)
    wheel = build(args.commit, Path(args.out))
    print(wheel)
    print(sha256(wheel.read_bytes()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
