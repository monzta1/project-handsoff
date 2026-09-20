"""#165 #167 #166: the [features] switches are read, defaulted, validated,
hashed into the governance policy, written from Mission Control through
the same locked path as the agent matrix, and audited."""
import http.client
import json
import shutil
import sys
import threading
import unittest
from pathlib import Path

from tests.test_handsoff_supervisor import BIN, HandsoffTestCase

sys.path.insert(0, str(BIN))
import handsoff_dashboard as dashboard  # noqa: E402
import handsoff_lib as lib  # noqa: E402


class FeatureConfigTests(HandsoffTestCase):
    def _toml(self):
        return self.tmp / "handsoff.toml"

    def _append(self, text):
        self._toml().write_text(self._toml().read_text() + text)

    def test_defaults_when_the_table_is_absent(self):
        cfg = lib.load_config(self.tmp)
        self.assertEqual(cfg["features"], {"failing_first": False, "launch_rules": True, "ticket_lock": True})
        self.assertEqual({name: item["enabled"] for name, item in lib.features_view(cfg).items()}, cfg["features"])
        for name, item in lib.features_view(cfg).items():
            self.assertEqual(item["default"], lib.FEATURES[name][0])
            self.assertTrue(item["description"])

    def test_explicit_values_are_read_and_unknown_or_non_boolean_refused(self):
        self._append('\n[features]\nfailing_first = true\nticket_lock = false\n')
        cfg = lib.load_config(self.tmp)
        self.assertEqual(cfg["features"], {"failing_first": True, "launch_rules": True, "ticket_lock": False})
        self.assertTrue(lib.feature_enabled(cfg, "failing_first"))
        self.assertFalse(lib.feature_enabled(cfg, "ticket_lock"))
        with self.assertRaisesRegex(lib.HandsoffError, "unknown workflow feature"):
            lib.feature_enabled(cfg, "sparkles")
        self._append('sparkles = true\n')
        with self.assertRaisesRegex(lib.HandsoffError, r"unknown \[features\] key.*sparkles"):
            lib.load_config(self.tmp)
        self._toml().write_text(self._toml().read_text().replace("sparkles = true\n", "").replace("ticket_lock = false", 'ticket_lock = "no"'))
        with self.assertRaisesRegex(lib.HandsoffError, "features.ticket_lock must be boolean"):
            lib.load_config(self.tmp)

    def test_a_switch_at_its_default_keeps_the_governance_hash_and_a_flip_changes_it(self):
        base = lib.config_hash(lib.load_config(self.tmp))
        self._append('\n[features]\nfailing_first = false\nlaunch_rules = true\nticket_lock = true\n')
        self.assertEqual(lib.config_hash(lib.load_config(self.tmp)), base, "explicit defaults are the same policy")
        self._toml().write_text(self._toml().read_text().replace("failing_first = false", "failing_first = true"))
        self.assertNotEqual(lib.config_hash(lib.load_config(self.tmp)), base, "a flipped switch is a different policy")

    def test_update_feature_settings_patches_only_the_table_and_refuses_bad_payloads(self):
        original = self._toml().read_text()
        result = lib.update_feature_settings(self.tmp, {"failing_first": True, "launch_rules": False, "ticket_lock": True})
        self.assertEqual(result, {"features": {"failing_first": True, "launch_rules": False, "ticket_lock": True}})
        text = self._toml().read_text()
        self.assertTrue(text.startswith(original.rstrip("\n")), "everything before the table is untouched")
        self.assertIn("[features]\n", text)
        self.assertIn("failing_first = true\n", text)
        self.assertEqual(lib.load_config(self.tmp)["features"], result["features"])
        # a second save rewrites the values in place, no second table
        lib.update_feature_settings(self.tmp, {"failing_first": False, "launch_rules": True, "ticket_lock": False})
        text = self._toml().read_text()
        self.assertEqual(text.count("[features]"), 1)
        self.assertEqual(lib.load_config(self.tmp)["features"], {"failing_first": False, "launch_rules": True, "ticket_lock": False})
        for bad in ({"failing_first": True}, {"failing_first": "yes", "launch_rules": True, "ticket_lock": True},
                    {"failing_first": True, "launch_rules": True, "ticket_lock": True, "extra": True}, [], "x"):
            with self.assertRaises(lib.HandsoffError):
                lib.update_feature_settings(self.tmp, bad)
        self.assertEqual(lib.load_config(self.tmp)["features"], {"failing_first": False, "launch_rules": True, "ticket_lock": False})


class FeatureSettingsEndpointTests(HandsoffTestCase):
    def setUp(self):
        super().setUp()
        shutil.copytree(BIN.parent / "prompts", self.tmp / "prompts")
        (self.tmp / ".handsoff-version").write_text("0.3.*\n")

    def _serve(self):
        try:
            server = dashboard.DashboardServer(("127.0.0.1", 0), self.tmp)
        except PermissionError:
            self.skipTest("managed test environment disallows loopback binds")
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server

    def _post(self, server, path, body, origin=True):
        host, port = server.server_address[:2]
        connection = http.client.HTTPConnection(host, port, timeout=5)
        headers = {"Content-Type": "application/json"}
        if origin:
            headers["Origin"] = f"http://{host}:{port}"
        else:
            headers["Origin"] = "https://elsewhere.example"
        connection.request("POST", path, body=json.dumps(body), headers=headers)
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        connection.close()
        return response.status, payload

    def test_snapshot_carries_features_and_the_endpoint_round_trips_and_audits(self):
        self.init()
        snapshot = dashboard.build_snapshot(self.tmp)
        self.assertEqual({k: v["enabled"] for k, v in snapshot["settings"]["features"].items()},
                         {"failing_first": False, "launch_rules": True, "ticket_lock": True})
        server = self._serve()
        try:
            status, payload = self._post(server, "/api/settings/features",
                                         {"failing_first": True, "launch_rules": True, "ticket_lock": False})
            self.assertEqual(status, 200, payload)
            self.assertTrue(payload["ok"])
            self.assertEqual({k: v["enabled"] for k, v in payload["features"].items()},
                             {"failing_first": True, "launch_rules": True, "ticket_lock": False})
            self.assertEqual(lib.load_config(self.tmp)["features"],
                             {"failing_first": True, "launch_rules": True, "ticket_lock": False})
            events = [json.loads(line) for line in (self.tmp / "handsoff-events.jsonl").read_text().splitlines()]
            audit = [e for e in events if e["kind"] == "features_updated"]
            self.assertEqual(len(audit), 1)
            self.assertEqual(audit[0]["features"], {"failing_first": True, "launch_rules": True, "ticket_lock": False})
            # refusals: unknown key, non-boolean, wrong origin; nothing changes
            for body in ({"failing_first": True, "launch_rules": True, "ticket_lock": False, "x": True},
                         {"failing_first": "true", "launch_rules": True, "ticket_lock": False}):
                status, payload = self._post(server, "/api/settings/features", body)
                self.assertEqual(status, 400, payload)
                self.assertFalse(payload["ok"])
            status, payload = self._post(server, "/api/settings/features",
                                         {"failing_first": False, "launch_rules": True, "ticket_lock": True}, origin=False)
            self.assertEqual(status, 403)
            self.assertEqual(lib.load_config(self.tmp)["features"],
                             {"failing_first": True, "launch_rules": True, "ticket_lock": False})
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
