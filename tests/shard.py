#!/usr/bin/env python3
"""Split the test tree across parallel CI jobs (#179).

    python3 tests/shard.py --index I --total N          run shard I of N of test_handsoff_supervisor.py
    python3 tests/shard.py --total N --plan             print that plan, run nothing
    python3 tests/shard.py --modules --index I --total N   run shard I of N of every OTHER tests/test_*.py module
    python3 tests/shard.py --modules --total N --plan

Every TestCase class in test_handsoff_supervisor.py goes to exactly one
shard. Classes are weighted by their seconds in tests/shard_weights.json
(measured from a CI run's log timestamps; refresh it when the balance
drifts) and dealt greedily, heaviest first, to the lightest shard, so a new
class with no weight is costed at its test count times the median per-test
time and still lands somewhere. The shard runs the file itself with the
class names as unittest arguments, so `__main__` means what the tests
expect.

With --modules the unit is a module: every tests/test_*.py except the
supervisor file (which has its own shards) and the scripts that are not
unittest modules (MODULE_SCRIPTS), weighted by tests/shard_module_weights.json
and run one process per module (`python3 -m unittest tests.<name>`). Together the two
plans are the whole tree; `tests` on main requires both.
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
MODULE_WEIGHTS = HERE / "shard_module_weights.json"
#: tests/test_*.py files that are scripts with a main(), not unittest modules
MODULE_SCRIPTS = {"test_generic_dropin"}


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


def modules() -> list[str]:
    return sorted(p.stem for p in HERE.glob("test_*.py")
                  if p.name != SUITE.name and p.stem not in MODULE_SCRIPTS)


def _deal(units: dict[str, float], total: int) -> list[list[str]]:
    shards: list[list[str]] = [[] for _ in range(total)]
    load = [0.0] * total
    for name in sorted(units, key=lambda c: (-units[c], c)):
        i = min(range(total), key=lambda k: (load[k], k))
        shards[i].append(name)
        load[i] += units[name]
    return shards


def module_plan(total: int) -> list[list[str]]:
    weights = json.loads(MODULE_WEIGHTS.read_text()) if MODULE_WEIGHTS.exists() else {}
    known = [w for w in weights.values() if isinstance(w, (int, float)) and w > 0]
    default = statistics.median(known) if known else 10.0
    cost = {m: float(weights[m]) if m in weights else default for m in modules()}
    return _deal(cost, total)


def plan(total: int) -> list[list[str]]:
    counts = classes_with_counts()
    weights = json.loads(WEIGHTS.read_text()) if WEIGHTS.exists() else {}
    known = [weights[c] / counts[c] for c in counts if c in weights and weights[c] > 0]
    per_test = statistics.median(known) if known else 1.0
    cost = {c: float(weights[c]) if c in weights else counts[c] * per_test for c in counts}
    return _deal(cost, total)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--total", type=int, required=True)
    ap.add_argument("--index", type=int, help="0-based shard to run")
    ap.add_argument("--plan", action="store_true", help="print every shard and exit")
    ap.add_argument("--modules", action="store_true", help="shard the other test modules instead of the supervisor file")
    a = ap.parse_args(argv)
    if a.total < 1:
        ap.error("--total must be at least 1")
    shards = module_plan(a.total) if a.modules else plan(a.total)
    if a.plan or a.index is None:
        for i, names in enumerate(shards):
            print(f"shard {i}: {len(names)} units: {' '.join(names)}")
        return 0
    if not 0 <= a.index < a.total:
        ap.error("--index must be from 0 to total-1")
    names = shards[a.index]
    print(f"shard {a.index} of {a.total}: {' '.join(names)}", flush=True)
    if a.modules:
        # One process per module, as [checks] runs them: loading several of
        # these modules into one interpreter is not how they were written
        # (one of them leaves state behind that makes another loop), and a
        # module's own process is what its evidence came from.
        import subprocess
        failed = []
        for name in names:
            print(f"\n=== tests.{name}", flush=True)
            result = subprocess.run([sys.executable, "-m", "unittest", f"tests.{name}"], cwd=str(HERE.parent))
            if result.returncode != 0:
                failed.append(name)
        print(f"\nshard {a.index} of {a.total}: {len(names) - len(failed)} of {len(names)} modules OK"
              + (f"; FAILED: {' '.join(failed)}" if failed else ""), flush=True)
        return 1 if failed else 0
    os.execv(sys.executable, [sys.executable, str(SUITE), *names])


if __name__ == "__main__":
    sys.exit(main())
