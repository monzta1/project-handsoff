"""#443: every usage line `handsoff commands` prints runs as printed.

The reference rendered the supervisor parser with argparse's default prog
(basename of argv[0]), so supervisor verbs read `usage: handsoff
regression-run ...`, which the top-level CLI rejects as an invalid choice.
"""
import contextlib
import io
import os
import sys
import unittest
from unittest import mock

from tests.test_handsoff_supervisor import ROOT, BIN

sys.path.insert(0, str(BIN))
import handsoff_cli as cli


def _run(argv: list[str]) -> tuple[int, str]:
    """Run handsoff_cli.main in-process as `handsoff <argv>` from the
    repository root; --help exits through SystemExit, which is the code."""
    out, err = io.StringIO(), io.StringIO()
    previous = os.getcwd()
    os.chdir(ROOT)
    try:
        with mock.patch.object(sys, "argv", ["handsoff", *argv]), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = cli.main()
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    finally:
        os.chdir(previous)
    return code, out.getvalue() + err.getvalue()


def _command_words(usage_line: str) -> list[str]:
    """The words before the first option, choice set, metavar or ellipsis."""
    words = []
    for token in usage_line.split()[1:]:
        if token[0] in "[-{<" or token.startswith("..."):
            break
        words.append(token)
    return words


def _first_usage(text: str) -> str:
    return next((line for line in text.splitlines() if line.startswith("usage:")), "")


class CommandsUsageLinesRun(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        code, cls.reference = _run(["commands"])
        assert code == 0, cls.reference[-2000:]
        cls.usage_lines = [line for line in cls.reference.splitlines() if line.startswith("usage:")]

    def test_reference_names_supervisor_verbs_with_their_prefix(self):
        self.assertIn("usage: handsoff supervisor regression-run", self.reference)
        self.assertIn("usage: handsoff supervisor advance", self.reference)
        self.assertNotIn("usage: handsoff regression-run", self.reference)
        # Both parser trees are rendered: the top-level verbs and the supervisor's.
        self.assertGreater(sum(" supervisor " in line for line in self.usage_lines), 50)

    def test_every_printed_usage_line_dispatches_with_help(self):
        self.assertGreater(len(self.usage_lines), 60)
        for line in self.usage_lines:
            words = _command_words(line)
            with self.subTest(usage=" ".join(words)):
                self.assertEqual(words[0], "handsoff", line)
                code, output = _run([*words[1:], "--help"])
                self.assertEqual(code, 0, output[-1000:])
                self.assertNotIn("invalid choice", output)
                printed = _command_words(_first_usage(output))
                self.assertEqual(printed[:len(words)], words, _first_usage(output))

    def test_supervisor_agent_and_fleet_help_name_their_prefix(self):
        for argv, prefix in ((["supervisor", "regression-run", "--help"], "usage: handsoff supervisor regression-run"),
                             (["supervisor", "--help"], "usage: handsoff supervisor "),
                             (["agent", "launch", "--help"], "usage: handsoff agent launch"),
                             (["fleet", "--help"], "usage: handsoff fleet ")):
            with self.subTest(argv=argv):
                code, output = _run(argv)
                self.assertEqual(code, 0, output[-1000:])
                self.assertTrue(_first_usage(output).startswith(prefix), _first_usage(output))

    def test_the_old_unprefixed_form_is_still_refused(self):
        code, output = _run(["regression-run", "--help"])
        self.assertEqual(code, 2)
        self.assertIn("invalid choice", output)


if __name__ == "__main__":
    unittest.main()
