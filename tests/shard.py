#!/usr/bin/env python3
"""Split tests/test_handsoff_supervisor.py across parallel CI jobs (#179).

    python3 tests/shard.py --index I --total N        run shard I of N
    python3 tests/shard.py --total N --plan           print the plan, run nothing

Every TestCase class in the file goes to exactly one shard. Classes are
weighted by their seconds in tests/shard_weights.json (measured from a CI
run's log timestamps; refresh it when the balance drifts) and dealt
greedily, heaviest first, to the lightest shard, so a new class with no
weight is costed at its test count times the median per-test time and
still lands somewhere. The shard runs the file itself with the class names
as unittest arguments, so `__main__` means what the tests expect.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SUITE = HERE / "test_handsoff_supervisor.py"
WEIGHTS = HERE / "shard_weights.json"


def classes_with_counts() -> dict[str, int]:
    sys.path.insert(0, str(HERE))
    saved = sys.argv
    sys.argv = [str(SUITE)]
    try:
        import test_handsoff_supervisor as module
    finally:
        sys.argv = saved
    out = {}
    for name in dir(module):
        obj = getattr(module, name)
        if isinstance(obj, type) and issubclass(obj, unittest.TestCase) and obj.__module__ == module.__name__:
            count = unittest.defaultTestLoader.loadTestsFromTestCase(obj).countTestCases()
            if count:
                out[name] = count
    return out


def plan(total: int) -> list[list[str]]:
    counts = classes_with_counts()
    weights = json.loads(WEIGHTS.read_text()) if WEIGHTS.exists() else {}
    known = [weights[c] / counts[c] for c in counts if c in weights and weights[c] > 0]
    per_test = statistics.median(known) if known else 1.0
    cost = {c: float(weights.get(c) or counts[c] * per_test) for c in counts}
    shards: list[list[str]] = [[] for _ in range(total)]
    load = [0.0] * total
    for name in sorted(counts, key=lambda c: (-cost[c], c)):
        i = min(range(total), key=lambda k: (load[k], k))
        shards[i].append(name)
        load[i] += cost[name]
    return shards


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--total", type=int, required=True)
    ap.add_argument("--index", type=int, help="0-based shard to run")
    ap.add_argument("--plan", action="store_true", help="print every shard and exit")
    a = ap.parse_args(argv)
    if a.total < 1:
        ap.error("--total must be at least 1")
    shards = plan(a.total)
    if a.plan or a.index is None:
        for i, names in enumerate(shards):
            print(f"shard {i}: {len(names)} classes: {' '.join(names)}")
        return 0
    if not 0 <= a.index < a.total:
        ap.error("--index must be from 0 to total-1")
    names = shards[a.index]
    print(f"shard {a.index} of {a.total}: {' '.join(names)}", flush=True)
    os.execv(sys.executable, [sys.executable, str(SUITE), *names])


if __name__ == "__main__":
    sys.exit(main())
