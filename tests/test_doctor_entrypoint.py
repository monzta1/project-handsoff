"""E1 review: `doctor --prompts` and `doctor --probe ADAPTER` reach the
public `handsoff doctor` (handsoff_cli, the installed console entry), not
only the Supervisor's doctor, and say the same thing through both."""
import contextlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

BIN = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(BIN))
import handsoff_cli as cli  # noqa: E402
from tests.engine_patch import patch_engine  # noqa: E402

PROBE_STUB = {"probes": {"codex": {"default": {"class": "available", "detail": "", "tokens": 1,
                                               "enforcement": "native_rollout_meter"}}},
              "probe_summary": "available"}


class PublicDoctorTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="handsoff-test-doctor-entry-")).resolve()
        self.addCleanup(shutil.rmtree, self.root, True)
        cli.init_project(self.root, None)

    def _main(self, *argv):
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["handsoff", *argv]), contextlib.redirect_stdout(out):
            code = cli.main()
        return code, out.getvalue()

    def test_handsoff_doctor_prompts_matches_the_supervisor_doctor(self):
        public = subprocess.run([sys.executable, str(BIN / "handsoff_cli.py"), "doctor", str(self.root), "--prompts"],
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(public.returncode, 0, public.stderr)
        report = json.loads(public.stdout)
        self.assertTrue(report["prompts"]["ok"])
        self.assertNotIn("probes", report)
        supervisor = subprocess.run([sys.executable, str(BIN / "handsoff_supervisor.py"), "--root", str(self.root),
                                     "doctor", "--prompts"], capture_output=True, text=True, timeout=120)
        self.assertEqual(supervisor.returncode, 0, supervisor.stderr)
        self.assertEqual(public.stdout, supervisor.stdout)

    def test_handsoff_doctor_probe_runs_the_named_adapter_only(self):
        with patch_engine("probe_adapters", return_value=PROBE_STUB) as probe, \
                patch_engine("adapter_preflight") as preflight:
            code, out = self._main("doctor", str(self.root), "--probe", "codex")
        self.assertEqual(code, 0)
        self.assertEqual(probe.call_count, 1)
        self.assertEqual(probe.call_args.args[1], "codex")
        preflight.assert_not_called()
        self.assertEqual(json.loads(out), PROBE_STUB)

    def test_an_unavailable_probe_exits_nonzero(self):
        stub = {"probes": {"claude": {"default": {"class": "timeout"}}}, "probe_summary": "unavailable"}
        with patch_engine("probe_adapters", return_value=stub):
            code, out = self._main("doctor", str(self.root), "--probe", "claude")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["probe_summary"], "unavailable")

    def test_the_public_parser_takes_both_flags(self):
        args = cli.build_parser().parse_args(["doctor", "--prompts", "--probe", "all"])
        self.assertEqual((args.prompts, args.probe), (True, "all"))
        args = cli.build_parser().parse_args(["doctor"])
        self.assertEqual((args.prompts, args.probe), (False, None))


if __name__ == "__main__":
    unittest.main()
