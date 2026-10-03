#!/usr/bin/env python3
"""Engine-applied mutation proof: does this test notice when the code breaks.

#349. Every other gate in this engine checks that a record exists. A `[checks]`
command exiting zero makes a criterion `passing`, and nothing asks whether that
command would fail if the behaviour it names were deleted. A test that imports a
module and asserts nothing satisfies the same gate as one that pins a refusal.

That gap is measurable. In one day against v0.4.0: five design-critique rounds
found eleven acceptance criteria a no-op implementation would have satisfied; a
hand mutation of freshly written tests found two escapes in nine attempts; 115
of 542 recorded sessions were reviewer approvals carrying `tests_executed: no`,
which the review gate accepted; and stubbing one refusal in
`validate_status_schema` to `return []` passed the entire 1,727-test suite.

Four symptoms, one cause. This module removes the behaviour and requires the
criterion's own test to fail.

**The engine performs the mutation, never the author.** An author reporting "I
mutation-tested it" is making exactly the kind of unverified claim this exists
to stop, so the mutation is applied by the same code that records the evidence.

**It never touches the working tree.** The proof runs in a copy. A mutation that
escaped into a real checkout would be the worst possible failure of a tool whose
job is trust, so the copy is the only thing ever edited, and the engine's own
source plus the target file are digested before and after and compared.

Layer: directly above core, which is all it imports. It reads a tree and runs a command; it writes no
engine state and decides no gate. The caller records what it returns.
"""
from __future__ import annotations

import ast
import hashlib
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from handsoff_core import HandsoffError

#: Bumped when the neutralisation below changes meaning, so a recorded proof
#: can be told apart from one produced by different surgery.
MUTATION_VERSION = 1

#: Never copied into the proof tree. `.git` alone is most of the bytes, and a
#: previous run's coverage or build output cannot affect the result.
EXCLUDED = ("*.pyc", "__pycache__", ".git", ".coverage-data", ".coverage",
            ".coverage.*", "node_modules", "dist", "build", ".handsoff-archive",
            ".handsoff.lock", ".handsoff-regression.json")

#: How long ONE run may take. Three of the five runs are the criterion's own
#: command, so this is the test suite's budget, not the mutation's.
DEFAULT_TIMEOUT = 900


def _definitions(tree: ast.AST, symbol: str) -> list:
    """Every function named `symbol`, at any nesting depth.

    ONE definition of "defined", used by both `neutralize` and
    `where_defined`. A review found them disagreeing: `neutralize` walked the
    whole tree and would happily mutate a method, while `where_defined` read
    only `tree.body`, so the re-export hint could never locate a symbol that
    exists as a method. A refusal that cannot say where the symbol lives is the
    thing REQ-012 exists to prevent, so the two now ask the same question.
    """
    return [node for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == symbol]


def _parse(source: str, where: str) -> ast.AST:
    """Parse, or refuse with the file named.

    A review pointed `--target` at `dashboard/app.js`, which `_source_digest`
    itself treats as engine source, and `ast.parse` raised SyntaxError straight
    out of the CLI as a traceback. REQ-012 promises every refusal carries a
    reason an author can act on; a traceback is not one.
    """
    try:
        return ast.parse(source)
    except SyntaxError as exc:
        raise HandsoffError(
            f"mutation proof: {where} is not parseable Python (line {exc.lineno}: {exc.msg}). "
            "The target must be the Python file defining the symbol; a JavaScript or data "
            "file cannot be mutated by this engine.") from exc


def neutralize(source: str, symbol: str, where: str = "the target") -> str:
    """Return `source` with the named function's body replaced.

    The signature, decorators and docstring position stay intact so the module
    still imports and every caller still resolves; only the behaviour is
    removed. That is the mutation a test must notice.

    Chosen over deleting the function because deletion raises AttributeError or
    ImportError at collection, which fails a test suite for a reason that has
    nothing to do with whether the test asserts anything.

    A name defined more than once is refused rather than guessed. The first
    version took the first match in walk order, so a nested helper sharing a
    name with a top-level function decided the mutation silently, and the
    recorded proof named a symbol that is not the one that was neutralised.
    """
    tree = _parse(source, where)
    lines = source.splitlines(keepends=True)
    found = _definitions(tree, symbol)
    if len(found) > 1:
        places = ", ".join(f"line {node.lineno}" for node in found)
        raise HandsoffError(
            f"mutation proof: {symbol!r} is defined {len(found)} times in {where} "
            f"({places}); which one the proof neutralised could not be recorded honestly. "
            "Rename one, or name a symbol defined once.")
    for node in found:
        first = node.body[0]
        # A docstring is not behaviour; keep it so the mutation is readable in
        # a diff, and neutralise everything after it.
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
                and isinstance(first.value.value, str):
            start = first.end_lineno
        else:
            start = first.lineno - 1
        indent = " " * (first.col_offset)
        replacement = f"{indent}return None  # handsoff mutation proof: body removed\n"
        return "".join(lines[:start] + [replacement] + lines[node.end_lineno:])
    raise HandsoffError(
        f"mutation proof: no function named {symbol!r} in {where}; "
        "name the function whose behaviour the criterion claims to prove")


def where_defined(root: Path, symbol: str) -> list[str]:
    """Files under bin/ that actually define `symbol`.

    Needed because this engine re-exports almost everything: the monolith names
    hundreds of symbols it no longer defines, so an author naming a symbol and
    the file they found it in will frequently name a re-export. Without this the
    refusal is correct and useless.
    """
    found = []
    for path in sorted((Path(root) / "bin").glob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        if _definitions(tree, symbol):
            found.append(f"bin/{path.name}")
    return found


def _source_digest(root: Path, target: Path | None = None) -> str:
    """A digest of the SOURCE a mutation could escape into.

    Deliberately not the whole tree. The first version digested everything and
    refused a legitimate proof, because a live run and its dashboard write
    engine state (`handsoff-status.json`, liveness and beacon files) throughout,
    so the tree changes for reasons that have nothing to do with the mutation.
    What matters is that no source file was altered, which is exactly what an
    escape would do.

    `target` closes the gap a review found between this scope and the module's
    own claim. The engine's own source is `bin/**/*.py` and `dashboard/**/*.js`,
    but nothing restricts `--target` to those, so a proof against a file
    elsewhere -- `tests/`, a project's own package -- was covered by nothing: an
    escape into it returned normally with the real file mutated on disk.
    Including the target itself makes the guarantee true for every target the
    CLI accepts, rather than only for the ones it was designed around.
    """
    digest = hashlib.sha256()
    covered = sorted(set(
        list((root / "bin").rglob("*.py"))
        + list((root / "dashboard").rglob("*.js"))
        + ([target] if target is not None and target.is_file() else [])
    ))
    for path in covered:
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def _import_probe(target: str) -> str:
    """A command that imports the target module and nothing else.

    Used differentially: run against the unmutated copy and the mutated one.
    If the unmutated import already fails -- a module that is not importable
    standalone, a missing optional dependency -- the check is inconclusive and
    is skipped, so an environment quirk can never produce a false refusal.
    Only "imported before, does not import after" is a verdict.
    """
    module = Path(target)
    directory = module.parent.as_posix() or "."
    return (f"{shlex.quote(sys.executable)} -c "
            + shlex.quote(
                "import importlib.util, sys; "
                f"sys.path.insert(0, {directory!r}); "
                f"spec = importlib.util.spec_from_file_location('handsoff_mutation_probe', {target!r}); "
                "module = importlib.util.module_from_spec(spec); "
                "spec.loader.exec_module(module)"))


def _run(command: str, cwd: Path, timeout: int) -> dict:
    try:
        done = subprocess.run(command, shell=True, cwd=str(cwd), capture_output=True,
                              text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"exit_code": None, "timed_out": True, "tail": f"timed out after {timeout}s"}
    tail = (done.stdout + done.stderr)[-2000:]
    return {"exit_code": done.returncode, "timed_out": False, "tail": tail}


def prove(root: Path, *, command: str, target: str, symbol: str,
          timeout: int = DEFAULT_TIMEOUT, runner=_run) -> dict:
    """Prove `command` fails when `symbol` in `target` stops working.

    Returns the record either way; the caller decides the gate. `ok` requires
    four things, each ruling out a different way the result could be hollow:

    - the command PASSED on a clean copy, which rules out a suite that was
      already red and would "detect" every mutation including this one;
    - it passed again on a SECOND clean copy, which rules out a command that
      is not reproducible. A review forged a proof for an arbitrary symbol by
      making the target raise on a second import: the first run consumed a
      one-shot resource, every later run failed for that reason, and the
      failure was read as detection;
    - the target still IMPORTS with the symbol neutralised, which rules out
      the mutation having broken loading rather than behaviour. A review forged
      a proof with a function called at module scope that no test mentions;
    - and the command FAILED on a clean copy carrying the mutation, which rules
      out a test that executes the code and asserts nothing.

    Every run gets its OWN fresh copy. Sharing one copy was what made the
    second forgery possible, and it also meant a suite that writes into its
    tree changed the conditions of the run that followed it.

    **What this establishes, exactly.** That the criterion's tests FAIL when
    the named behaviour is removed. That includes failing because the suite
    could not run at all: a review showed a symbol used in a test file's
    module-level code, where neutralising it breaks collection rather than an
    assertion. The tests do detect its removal, which is the property recorded
    here, but detection by crash is weaker than detection by assertion and this
    proof does not distinguish them. Telling them apart means parsing an
    arbitrary runner's output, which would be a guess dressed as a gate. The
    limit is stated rather than hidden: a criterion is still only as good as
    the assertions behind it, and this gate raises the floor from "the command
    exited zero" to "the command goes red without this behaviour".
    """
    root = Path(root).resolve()
    target_path = (root / target).resolve()
    if not target_path.is_file():
        raise HandsoffError(f"mutation proof: no such target file: {target}")
    try:
        target_path.relative_to(root)
    except ValueError:
        raise HandsoffError("mutation proof: the target must be inside the project root")

    try:
        try:
            original = target_path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError) as exc:
            # A review reached this with a binary file and got a decode
            # traceback. Every way into the proof has to come out as a
            # refusal in words; REQ-012 says so without exceptions.
            raise HandsoffError(
                f"mutation proof: {target} could not be read as UTF-8 text "
                f"({exc.__class__.__name__}); the target must be the Python source file "
                "defining the symbol") from exc
        mutated_source = neutralize(original, symbol, where=target)
    except HandsoffError as exc:
        elsewhere = [name for name in where_defined(root, symbol) if name != target]
        raise HandsoffError(
            f"{exc}" + (f". It is defined in {', '.join(elsewhere)}; the target names a "
                        "re-export, not the definition." if elsewhere else "")) from exc

    before_digest = _source_digest(root, target_path)
    with tempfile.TemporaryDirectory(prefix="handsoff-mutation-") as scratch:
        def run_on_fresh_copy(what: str, mutate: bool) -> dict:
            """One run, on a tree nothing else has touched.

            The copies are not shared. A suite that writes into its own tree,
            binds a port, or refuses a second import changed the conditions of
            every run after it, which is how a review forged a proof for an
            arbitrary symbol.
            """
            copy = Path(scratch) / f"{root.name}-{what}"
            shutil.copytree(root, copy, symlinks=True,
                            ignore=shutil.ignore_patterns(*EXCLUDED))
            if mutate:
                (copy / Path(target)).write_text(mutated_source, encoding="utf-8")
            return runner(command if what.startswith("run") else _import_probe(target),
                          copy, timeout)

        baseline = run_on_fresh_copy("run-baseline", mutate=False)
        control = run_on_fresh_copy("run-control", mutate=False)
        import_before = run_on_fresh_copy("import-before", mutate=False)
        import_after = run_on_fresh_copy("import-after", mutate=True)
        mutated = run_on_fresh_copy("run-mutated", mutate=True)

    broke_import = (import_before["exit_code"] == 0 and import_after["exit_code"] != 0)
    reproducible = (baseline["exit_code"] == 0) == (control["exit_code"] == 0)

    after_digest = _source_digest(root, target_path)
    if before_digest != after_digest:
        raise HandsoffError(
            "mutation proof: a source file under bin/ or dashboard/, or the target itself, changed during the "
            "proof. The mutation is applied only inside a throwaway copy, so this means "
            "something wrote to the real checkout; refusing to record the evidence.")

    passed_before = baseline["exit_code"] == 0
    failed_after = mutated["exit_code"] not in (0, None)
    record = {
        "mutation_version": MUTATION_VERSION,
        "command": command, "target": target, "symbol": symbol,
        "baseline_exit_code": baseline["exit_code"],
        "control_exit_code": control["exit_code"],
        "mutated_exit_code": mutated["exit_code"],
        "passed_before": passed_before,
        "reproducible": reproducible,
        "failed_after": failed_after,
        "import_intact": not broke_import,
        "ok": bool(passed_before and reproducible and failed_after and not broke_import),
        "source_digest": before_digest,
    }
    if not passed_before:
        # The parentheses matter. Without them `a + b if cond else ""` reads as
        # `(a + b) if cond else ""`, so a command that printed nothing -- `true
        # && false`, or any quiet runner -- produced an EMPTY refusal and the
        # engine blocked with no reason. A gate that refuses without saying why
        # is the same defect as one that approves without checking.
        detail = baseline["tail"].strip()
        record["refusal"] = (
            f"the command failed against the unmutated tree (exit {baseline['exit_code']}), "
            "so it cannot show anything about the mutation"
            + (f": {detail.splitlines()[-1][:200]}" if detail else
               " (it printed nothing; run it yourself to see why)"))
    elif not reproducible:
        record["refusal"] = (
            f"the command passed on one clean copy and exited {control['exit_code']} on a second "
            "clean copy with no mutation applied, so it is not reproducible and a failure after "
            "the mutation proves nothing. A review forged a proof for an arbitrary symbol this "
            "way, with a target that refuses a second import. Make the command independent of "
            "state the previous run left behind.")
    elif broke_import:
        record["refusal"] = (
            f"neutralising {symbol} stopped {target} from importing at all, so the command's "
            "failure says nothing about whether the test asserts the behaviour: it would fail "
            "the same way for any mutation of any symbol in this file. A review reproduced "
            "exactly this to make an irrelevant symbol look proved. Name a symbol whose "
            "behaviour the test asserts, not one the module calls while loading.")
    elif not failed_after:
        record["refusal"] = (
            f"the command still passed with {symbol} neutralised in {target}, so it does "
            "not notice when that behaviour stops working. The test executes the code "
            "and asserts nothing that depends on it.")
    return record
