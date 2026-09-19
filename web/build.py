#!/usr/bin/env python3
"""#163: assemble web/dist for Cloudflare Pages from this repository.

Copies, byte for byte: web/public/* (the page and its phone stylesheet) and
fleet/metrics.js and fleet/styles.css (the Fleet code the page reuses). The
Pages Functions stay in web/functions, which wrangler picks up beside the
deploy directory when run from web/. No build tool, no bundling.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
DIST = WEB / "dist"
COPIES = (
    (WEB / "public" / "index.html", DIST / "index.html"),
    (WEB / "public" / "phone.css", DIST / "phone.css"),
    (ROOT / "fleet" / "metrics.js", DIST / "metrics.js"),
    (ROOT / "fleet" / "styles.css", DIST / "styles.css"),
)


def build(dist: Path = DIST) -> list[Path]:
    if dist.exists():
        shutil.rmtree(dist)
    dist.mkdir(parents=True)
    written = []
    for source, target in COPIES:
        target = dist / target.relative_to(DIST)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        written.append(target)
    return written


def main() -> int:
    for path in build():
        print(path.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
