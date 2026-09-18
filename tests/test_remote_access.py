"""#124: exact public origins for Mission Control and Fleet."""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))
sys.path.insert(0, str(ROOT / "tests"))
import handsoff_lib as lib  # noqa: E402
import handsoff_fleet as fleet  # noqa: E402
from test_handsoff_supervisor import HandsoffTestCase  # noqa: E402


class OriginRulesTests(unittest.TestCase):
    def test_public_origins_are_canonicalised_and_validated(self):
        self.assertEqual(lib.normalize_public_origins(["https://Mac.tailnet.ts.net:443", "http://box.lan:8080"], "x"),
                         ["https://mac.tailnet.ts.net", "http://box.lan:8080"])
        for bad in (["https://mac.ts.net/dash"], ["ftp://x"], ["https://u:p@x"], ["https://x?y=1"], [""], "https://x"):
            with self.assertRaises(lib.HandsoffError):
                lib.normalize_public_origins(bad, "x")

    def test_loopback_on_own_port_always_allowed(self):
        self.assertTrue(lib.origin_allowed("http://127.0.0.1:8766", 8766, []))
        self.assertTrue(lib.origin_allowed("http://localhost:8766", 8766, []))
        self.assertFalse(lib.origin_allowed("http://127.0.0.1:8767", 8766, []))
        self.assertFalse(lib.origin_allowed(None, 8766, []))

    def test_configured_origin_matches_exactly_and_near_misses_are_refused(self):
        public = lib.normalize_public_origins(["https://mac.tailnet.ts.net"], "x")
        self.assertTrue(lib.origin_allowed("https://mac.tailnet.ts.net", 8766, public))
        self.assertTrue(lib.origin_allowed("https://MAC.tailnet.ts.net:443", 8766, public))
        for near in ("http://mac.tailnet.ts.net", "https://mac.tailnet.ts.net:8766", "https://mac.tailnet.ts.net.evil.com",
                     "https://evil.com/https://mac.tailnet.ts.net", "https://mac.tailnet.ts.net/dash"):
            self.assertFalse(lib.origin_allowed(near, 8766, public), near)

    def test_fleet_reads_its_origins_from_the_environment(self):
        with mock.patch.dict(os.environ, {"HANDSOFF_PUBLIC_ORIGINS": "https://a.example, http://b.example:9000"}):
            self.assertEqual(lib.fleet_public_origins(), ["https://a.example", "http://b.example:9000"])
        with mock.patch.dict(os.environ, {"HANDSOFF_PUBLIC_ORIGINS": ""}):
            self.assertEqual(lib.fleet_public_origins(), [])


class ProjectConfigTests(HandsoffTestCase):
    def test_dashboard_public_origins_are_read_from_the_toml(self):
        self.init("Remote")
        toml = self.tmp / "handsoff.toml"
        toml.write_text(toml.read_text() + '\n[dashboard]\npublic_origins = ["https://mac.tailnet.ts.net"]\n')
        self.assertEqual(lib.load_config(self.tmp)["public_origins"], ["https://mac.tailnet.ts.net"])
        toml.write_text(toml.read_text().replace('public_origins = ["https://mac.tailnet.ts.net"]', 'public_origins = ["https://mac.tailnet.ts.net/x"]'))
        with self.assertRaises(lib.HandsoffError):
            lib.load_config(self.tmp)

    def test_default_has_no_public_origins(self):
        self.init("Remote default")
        self.assertEqual(lib.load_config(self.tmp)["public_origins"], [])


class FleetLinkTests(unittest.TestCase):
    def test_public_request_rewrites_the_dashboard_link(self):
        project = {"dashboard_url": "http://127.0.0.1:8766/", "name": "p"}
        rewritten = fleet._public_dashboard_url(project, "https://mac.tailnet.ts.net")
        self.assertEqual(rewritten["dashboard_url"], "https://mac.tailnet.ts.net:8766/")
        self.assertEqual(fleet._public_dashboard_url(project, None)["dashboard_url"], "http://127.0.0.1:8766/")
        self.assertEqual(fleet._public_dashboard_url({"dashboard_url": None}, "https://x")["dashboard_url"], None)

    def test_docs_describe_the_two_supported_setups_and_the_missing_login(self):
        text = (ROOT / "docs" / "REMOTE-ACCESS.md").read_text(encoding="utf-8")
        for needle in ("tailscale serve", "Cloudflare Tunnel", "Access", "no login", "public_origins", "HANDSOFF_PUBLIC_ORIGINS"):
            self.assertIn(needle, text)
        self.assertNotIn("—", text)


if __name__ == "__main__":
    unittest.main()
