import json
from pathlib import Path

DECLARED_WRITES = ("status.json",)


def save_status(root: Path, status: dict):
    (root / "status.json").write_text(json.dumps(status))


def save_cache(root: Path, cache: dict):
    (root / "cache.json").write_text(json.dumps(cache))
