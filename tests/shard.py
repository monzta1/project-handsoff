#!/usr/bin/env python3
"""Collect and split the complete unittest tree across parallel jobs.

    python3 tests/shard.py --index I --total N          run shard I of N of test_handsoff_supervisor.py
    python3 tests/shard.py --total N --plan             print that plan, run nothing
    python3 tests/shard.py --modules --index I --total N   run shard I of N of every OTHER tests/test_*.py module
    python3 tests/shard.py --modules --total N --plan
    python3 tests/shard.py --all --inventory            print the versioned complete test-id inventory
    python3 tests/shard.py --all --index I --total 5    run one count-balanced inventory shard

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

With --all, collection happens in one clean process per module.  Each shard
is balanced by exact test count, and execution also starts a separate process
per module so modules with incompatible import-time state never share an
interpreter.  This is the common local, CI, and release inventory for #276.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SUITE = HERE / "test_handsoff_supervisor.py"
WEIGHTS = HERE / "shard_weights.json"
MODULE_WEIGHTS = HERE / "shard_module_weights.json"
#: tests/test_*.py files that are scripts with a main(), not unittest modules
MODULE_SCRIPTS = {"test_generic_dropin"}
INVENTORY_SCHEMA_VERSION = 1
INVENTORY_MARKER = "HANDSOFF_INVENTORY_JSON="

COLLECT_CODE = (
    "import json, sys, unittest; sys.path.insert(0, '.'); "
    "suite=unittest.TestLoader().loadTestsFromName(sys.argv[1]); "
    "walk=lambda s: [t for x in s for t in (walk(x) if isinstance(x, unittest.TestSuite) else [x])]; "
    "tests=walk(suite); bad=[t.id() for t in tests if t.__class__.__name__ == '_FailedTest']; "
    "print('HANDSOFF_INVENTORY_JSON='+json.dumps({'ids':[t.id() for t in tests], 'bad':bad}))"
)


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


def all_modules() -> list[str]:
    """The authoritative unittest module universe, including the large suite."""
    return (["test_handsoff_supervisor"] if SUITE.is_file() else []) + modules()


def _collect_module_ids(name: str) -> list[str]:
    module = f"tests.{name}"
    result = subprocess.run(
        [sys.executable, "-c", COLLECT_CODE, module], cwd=str(HERE.parent),
        capture_output=True, text=True, env={**os.environ, "HANDSOFF_SKIP_PREFLIGHT": "1"},
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise RuntimeError(f"collection failed for {module}: {detail[-1] if detail else 'no output'}")
    line = next((line for line in reversed(result.stdout.splitlines())
                 if line.startswith(INVENTORY_MARKER)), None)
    if line is None:
        raise RuntimeError(f"collection failed for {module}: no inventory")
    payload = json.loads(line[len(INVENTORY_MARKER):])
    if payload.get("bad"):
        raise RuntimeError(f"collection failed for {module}: {', '.join(payload['bad'])}")
    ids = payload.get("ids")
    if not isinstance(ids, list) or not ids:
        raise RuntimeError(f"collection failed for {module}: no tests")
    return ids


def all_test_ids() -> list[str]:
    """Collect every unittest ID without importing two modules together."""
    ids = [test_id for module in all_modules() for test_id in _collect_module_ids(module)]
    duplicates = sorted({test_id for test_id in ids if ids.count(test_id) > 1})
    if duplicates:
        raise RuntimeError(f"duplicate test id(s): {', '.join(duplicates)}")
    return sorted(ids)


def all_plan(total: int, ids: list[str] | None = None) -> list[list[str]]:
    ordered = sorted(ids if ids is not None else all_test_ids())
    base, remainder = divmod(len(ordered), total)
    result = []
    offset = 0
    for index in range(total):
        size = base + (1 if index < remainder else 0)
        result.append(ordered[offset:offset + size])
        offset += size
    return result


def _module_for_id(test_id: str, names: list[str]) -> str:
    matches = [name for name in names if test_id.startswith(f"tests.{name}.")]
    if not matches:
        raise RuntimeError(f"test id has no inventory module: {test_id}")
    return max(matches, key=len)


def run_all_shard(ids: list[str], index: int, total: int) -> int:
    """Run a count-balanced shard, keeping every module in its own process."""
    names = all_modules()
    grouped: dict[str, list[str]] = {}
    for test_id in ids:
        grouped.setdefault(_module_for_id(test_id, names), []).append(test_id)
    failed = []
    for name in names:
        selected = grouped.get(name, [])
        if not selected:
            continue
        print(f"\n=== tests.{name} ({len(selected)} tests)", flush=True)
        result = subprocess.run(
            [sys.executable, "-m", "unittest", "-v", *selected], cwd=str(HERE.parent),
            env={**os.environ, "HANDSOFF_SKIP_PREFLIGHT": "1"},
        )
        if result.returncode != 0:
            failed.append(name)
    print(f"\nshard {index} of {total}: {len(ids)} tests"
          + (f"; FAILED MODULES: {' '.join(failed)}" if failed else " OK"), flush=True)
    return 1 if failed else 0


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
    ap.add_argument("--total", type=int)
    ap.add_argument("--index", type=int, help="0-based shard to run")
    ap.add_argument("--plan", action="store_true", help="print every shard and exit")
    ap.add_argument("--modules", action="store_true", help="shard the other test modules instead of the supervisor file")
    ap.add_argument("--all", action="store_true", help="use the complete module-isolated unittest inventory")
    ap.add_argument("--inventory", action="store_true", help="print the --all test-id inventory as JSON")
    ap.add_argument("--run-test-ids", nargs="+",
                    help="run coordinator-assigned --all test IDs without recollecting the inventory")
    a = ap.parse_args(argv)
    if a.inventory:
        if not a.all:
            ap.error("--inventory requires --all")
        ids = all_test_ids()
        print(INVENTORY_MARKER + json.dumps({
            "schema_version": INVENTORY_SCHEMA_VERSION,
            "modules": [f"tests.{name}" for name in all_modules()],
            "test_count": len(ids), "test_ids": ids,
        }, sort_keys=True))
        return 0
    if a.run_test_ids:
        if not a.all:
            ap.error("--run-test-ids requires --all")
        if a.total is None or a.index is None:
            ap.error("--run-test-ids requires --total and --index")
        if len(a.run_test_ids) != len(set(a.run_test_ids)):
            ap.error("--run-test-ids contains duplicates")
        return run_all_shard(sorted(a.run_test_ids), a.index, a.total)
    if a.total is None:
        ap.error("--total is required unless --inventory is used")
    if a.total < 1:
        ap.error("--total must be at least 1")
    if a.all and a.modules:
        ap.error("--all and --modules are mutually exclusive")
    shards = all_plan(a.total) if a.all else (module_plan(a.total) if a.modules else plan(a.total))
    if a.plan or a.index is None:
        for i, names in enumerate(shards):
            print(f"shard {i}: {len(names)} units: {' '.join(names)}")
        return 0
    if not 0 <= a.index < a.total:
        ap.error("--index must be from 0 to total-1")
    names = shards[a.index]
    print(f"shard {a.index} of {a.total}: {' '.join(names)}", flush=True)
    if a.all:
        return run_all_shard(names, a.index, a.total)
    if a.modules:
        # One process per module, as [checks] runs them: loading several of
        # these modules into one interpreter is not how they were written
        # (one of them leaves state behind that makes another loop), and a
        # module's own process is what its evidence came from.
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
