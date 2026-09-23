#!/usr/bin/env python3
"""Deployed-release identity check (v0.3.25 field notes, REQ-006).

Proves that the release this tree declares is the one published and
installed, per the README's "Cutting a release" procedure: the annotated tag
on origin resolves to a commit origin main carries whose tree declares the
same version in pyproject.toml and handsoff-runtime.json; the public GitHub
release carries the wheel whose sha256 equals the locally built dist wheel
(the asset URL is the one INSTALL.md prints); and the dedicated-venv
installation is that published wheel, member for member, with the engine
manifest identity `version --json` reports. Read-only: nothing here fetches
into or otherwise changes the checkout, and no GitHub credential is needed
(the tag is read over git and the release over the public API).
"""
import hashlib
import io
import json
import re
import subprocess
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True,
                           text=True, check=True).stdout.strip())
HANDSOFF = str(Path.home() / ".local" / "bin" / "handsoff")
VENV = (Path.home() / ".local" / "share" / "handsoff" / "venv").resolve()


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def operation(op_id: str, name: str, state: str) -> None:
    """External-call telemetry in the shape the role prompts use; never evidence."""
    print("HANDSOFF_OPERATION: " + json.dumps({"operation_id": op_id, "dependency": "github_api", "operation": name,
                                               "state": state, "attempt": 1, "timeout_seconds": 120}))


def fetch_bytes(url: str, op_id: str, name: str, timeout: int = 60) -> bytes:
    operation(op_id, name, "started")
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            payload = response.read()
    except Exception:
        operation(op_id, name, "failed")
        raise
    operation(op_id, name, "succeeded")
    return payload


def fetch_json(url: str, op_id: str, name: str) -> dict:
    return json.loads(fetch_bytes(url, op_id, name).decode("utf-8"))


tag = json.loads((ROOT / "handsoff-runtime.json").read_text(encoding="utf-8"))["version"]
assert re.fullmatch(r"v\d+\.\d+\.\d+", tag), tag
version = tag[1:]
pyproject_version = re.search(r'^version = "([^"]+)"$', (ROOT / "pyproject.toml").read_text(encoding="utf-8"), re.M).group(1)
assert pyproject_version == version, (pyproject_version, version)
wheel_name = f"project_handsoff-{version}-py3-none-any.whl"
remote = git("remote", "get-url", "origin")
slug = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$", remote).group(1)

# 1. The tag exists on origin, is annotated, and its commit is on origin main
#    with a tree that declares this same version. Ancestry is checked from
#    objects already in this checkout when they are present (a push from here
#    leaves them), otherwise through the public compare API; nothing is fetched.
operation("op-ghref", "ls_remote", "started")
try:
    listed = git("ls-remote", "origin", f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}", "refs/heads/main")
except Exception:
    operation("op-ghref", "ls_remote", "failed")
    raise
operation("op-ghref", "ls_remote", "succeeded")
refs = dict(reversed(line.split("\t")) for line in listed.splitlines())
assert f"refs/tags/{tag}" in refs, f"tag {tag} is not on origin: {refs}"
assert f"refs/tags/{tag}^{{}}" in refs, f"tag {tag} on origin is not annotated: {refs}"
tag_commit = refs[f"refs/tags/{tag}^{{}}"]
origin_main = refs["refs/heads/main"]
have_objects = all(subprocess.run(["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=ROOT, capture_output=True).returncode == 0
                   for sha in (tag_commit, origin_main))
if have_objects:
    ancestry = subprocess.run(["git", "merge-base", "--is-ancestor", tag_commit, origin_main], cwd=ROOT, capture_output=True).returncode == 0
    assert f'version = "{version}"' in git("show", f"{tag_commit}:pyproject.toml"), f"{tag} commit does not declare {version} in pyproject.toml"
    assert json.loads(git("show", f"{tag_commit}:handsoff-runtime.json"))["version"] == tag, f"{tag} commit manifest is not {tag}"
    ancestry_source = "local objects"
else:
    compare = fetch_json(f"https://api.github.com/repos/{slug}/compare/{tag_commit}...{origin_main}", "op-ghcmp", "compare_commits")
    ancestry = compare.get("status") in {"identical", "ahead"}
    for index, (path, check) in enumerate((("pyproject.toml", lambda text: f'version = "{version}"' in text),
                                           ("handsoff-runtime.json", lambda text: json.loads(text)["version"] == tag))):
        text = fetch_bytes(f"https://raw.githubusercontent.com/{slug}/{tag_commit}/{path}", f"op-ghraw{index}", "get_tagged_file").decode("utf-8")
        assert check(text), f"{tag} commit does not declare {tag} in {path}"
    ancestry_source = "compare API"
assert ancestry, f"tag {tag} commit {tag_commit} is not on origin main {origin_main}"

# 2. The public GitHub release carries the wheel, and it is byte-for-byte the
#    local build.
local_wheel = ROOT / "dist" / wheel_name
assert local_wheel.is_file(), f"local wheel missing: {local_wheel} (python3 -m build --wheel)"
release = fetch_json(f"https://api.github.com/repos/{slug}/releases/tags/{tag}", "op-ghrel", "get_release_by_tag")
assets = release.get("assets", [])
if not assets and release.get("assets_url"):
    assets = fetch_json(release["assets_url"], "op-ghassets", "list_release_assets")
asset = next((item for item in assets if item.get("name") == wheel_name), None)
assert asset is not None, f"release {tag} has no asset {wheel_name}: {[a.get('name') for a in assets]}"
expected_url = f"https://github.com/{slug}/releases/download/{tag}/{wheel_name}"
assert asset["browser_download_url"] == expected_url, (asset["browser_download_url"], expected_url)
published = fetch_bytes(expected_url, "op-ghdl", "download_release_asset", timeout=120)
published_sha256 = sha256(published)
assert published_sha256 == sha256(local_wheel.read_bytes()), "published wheel differs from the local build"

# 3. The dedicated-venv installation IS the published wheel: every module and
#    data member installed from it is byte-identical, and the engine manifest
#    identity the CLI reports is the published manifest's digest. A different
#    artifact that merely claims the same version cannot pass this.
identity = json.loads(subprocess.run([HANDSOFF, "version", "--json"], capture_output=True, text=True, check=True).stdout)
assert identity["version"] == tag, (identity["version"], tag)
assert identity["source"] == "installed-engine", identity
source_root = Path(identity["source_root"]).resolve()
assert VENV in source_root.parents, identity["source_root"]
venv_python = VENV / "bin" / "python"
site_packages = Path(subprocess.run([str(venv_python), "-c", "import handsoff_lib; print(handsoff_lib.__file__)"],
                                    capture_output=True, text=True, check=True).stdout.strip()).resolve().parent
assert VENV in site_packages.parents, site_packages
data_prefix = f"project_handsoff-{version}.data/data/share/handsoff/"
compared = 0
with zipfile.ZipFile(io.BytesIO(published)) as wheel:
    members = [name for name in wheel.namelist() if not name.endswith("/")]
    assert any(name.startswith(data_prefix) for name in members), f"published wheel ships no share/handsoff data: {members[:8]}"
    for name in members:
        if ".dist-info/" in name:
            continue
        if name.startswith(data_prefix):
            installed = source_root / name[len(data_prefix):]
        elif "/" in name:
            raise AssertionError(f"unexpected wheel member layout: {name}")
        else:
            installed = site_packages / name
        assert installed.is_file(), f"published wheel member {name} is not installed at {installed}"
        assert installed.read_bytes() == wheel.read(name), f"installed {installed} differs from the published wheel member {name}"
        compared += 1
    manifest_member = data_prefix + "handsoff-runtime.json"
    assert sha256(wheel.read(manifest_member)) == identity["manifest_sha256"], "installed manifest identity is not the published manifest"
assert compared >= 12, compared

print(f"LIVE_RELEASE_OK tag={tag} commit={tag_commit[:12]} on_origin_main=yes({ancestry_source}) release_asset={wheel_name} "
      f"sha256={published_sha256[:16]} installed={identity['version']} installed_members_identical={compared} "
      f"manifest_sha256={identity['manifest_sha256'][:16]}")
