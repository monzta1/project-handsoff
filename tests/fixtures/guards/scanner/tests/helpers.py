"""Scanner fixture: a tests/ helper module that reads engine source."""
from pathlib import Path

HELPER_ENGINE = Path(__file__).resolve().parent.parent / "bin" / "handsoff_agent.py"


def engine_text():
    return (Path(__file__).resolve().parents[1] / "bin" / "handsoff_lib.py").read_text()


def read_path(path):
    return path.read_text()


def harmless():
    return "nothing read"
