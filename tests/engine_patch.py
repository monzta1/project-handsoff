"""Patch an engine symbol on every module that binds it.

Since the monolith was split, `from handsoff_core import status_path` gives
each importing module its OWN binding. `mock.patch.object(lib, "status_path")`
replaces only the monolith's, so any module that imported the name keeps
calling the real one. Whether that matters depends on which call site the
test happens to drive, which no static check can decide -- and two tests
shipped green while silently exercising the real path: one patched
`lib.commit` while the commit ran inside handsoff_agent_runtime, and the
sleep tests patched `lib._read_pmset_log` while handsoff_projection called
its own binding and read the operator's real pmset log (281 intervals where
the fixture supplied 1).

`patch_engine` replaces the name wherever it is bound, so the patch cannot
be inert. A test that wants only one module's binding replaced should say so
by patching that module directly, which names the intent in the test.
"""
import contextlib
import sys
from unittest import mock


def engine_modules():
    """Every imported handsoff module, monolith first.

    Read from sys.modules rather than a hardcoded list so a module added by a
    later extraction is covered without editing this file -- the failure mode
    being guarded against is exactly a binding nobody remembered to include.
    """
    holders = [module for name, module in sorted(sys.modules.items())
               if name.startswith("handsoff_") and module is not None]
    holders.sort(key=lambda m: m.__name__ != "handsoff_lib")
    return holders


@contextlib.contextmanager
def patch_engine(name, *new, **kwargs):
    """Patch `name` on every engine module that binds it, as one replacement.

    Takes the same arguments as mock.patch.object after the attribute, so a
    positional replacement (`patch_engine("KEYS", tuple(...))`) and the
    keyword forms (`return_value=`, `side_effect=`, `wraps=`) all carry over.
    """
    holders = [m for m in engine_modules() if name in vars(m)]
    if not holders:
        raise AttributeError(
            f"no imported handsoff module binds {name!r}; patch_engine would be a no-op")
    with contextlib.ExitStack() as stack:
        replacement = stack.enter_context(mock.patch.object(holders[0], name, *new, **kwargs))
        for module in holders[1:]:
            stack.enter_context(mock.patch.object(module, name, replacement))
        yield replacement
