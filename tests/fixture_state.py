"""Put a deliberately malformed document on disk, without a transition.

Since #284 criterion 3, `commit` validates the status and acceptance registry
it is about to persist and refuses an invalid one. That is the point: no
transition can write a state nothing checked.

A fixture that needs a corrupt or partial document on disk -- to prove a
reader tolerates it, or that a gate refuses it -- is not performing a
transition and must not pretend to. It writes the file, which is also how a
corrupt file really arrives: a hand edit, an interrupted process, an older
engine version. Using this helper keeps that distinction visible in the test
instead of hiding it behind a commit that no longer means what it did.
"""
import json
import sys
from pathlib import Path

BIN = Path(__file__).resolve().parent.parent / "bin"
if str(BIN) not in sys.path:
    sys.path.insert(0, str(BIN))

import handsoff_lib as lib  # noqa: E402


def _write(path, document):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return document


def reanchor(root, cfg, message="fixture placed a document directly"):
    """Append an event so the chain describes the files as they now are.

    The event log records each document's sha256, and `verify_event_log`
    refuses when the latest event disagrees with the file. A fixture that only
    writes the file leaves that disagreement behind and every command refuses
    with a drift error before reaching what the test is about. Appending an
    event afterwards is what a legacy file followed by a real transition looks
    like -- the chain stays intact and the document is the one written here.
    """
    root = Path(root)
    with lib.project_lock(root):
        return lib.append_event(root, cfg, "fixture_state", message)


def force_status(root, cfg, status, anchor=False):
    """Write `status` to the run's status file, valid or not."""
    _write(lib.status_path(Path(root), cfg), status)
    if anchor:
        reanchor(root, cfg)
    return status


def force_acceptance(root, cfg, acceptance, anchor=False):
    """Write `acceptance` to the run's acceptance registry, valid or not."""
    _write(lib.acceptance_path(Path(root), cfg), acceptance)
    if anchor:
        reanchor(root, cfg)
    return acceptance


def compatible_pin():
    """The project pin that accepts the engine in this checkout.

    Derived from handsoff-runtime.json rather than written out, because a pin
    names the major and minor exactly (`version_satisfies` compares both), so
    every MINOR bump invalidates a hardcoded one. v0.4.0 was the first minor
    bump and found the literal `0.3.*` in thirty fixtures plus a hygiene test
    asserting that exact text, which is the fixture-coupling tax this project
    keeps paying. Computed here, the next bump costs nothing.
    """
    manifest = json.loads((BIN.parent / "handsoff-runtime.json").read_text(encoding="utf-8"))
    version = str(manifest["version"]).lstrip("v")
    major, minor = version.split(".")[:2]
    return f"{major}.{minor}.*"


def write_version_pin(root, pin=None):
    """Write the engine pin a fixture project needs. Defaults to the
    compatible line for this checkout."""
    path = Path(root) / ".handsoff-version"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{pin or compatible_pin()}\n", encoding="utf-8")
    return path
