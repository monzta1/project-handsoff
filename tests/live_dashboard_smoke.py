#!/usr/bin/env python3
"""Narrow deployed-system check for the local Mission Control instance."""
import json
import urllib.request


with urllib.request.urlopen("http://127.0.0.1:8771/api/dashboard", timeout=5) as response:
    assert response.status == 200
    snapshot = json.load(response)

assert snapshot["initialized"] is True
assert snapshot["audit"]["healthy"] is True
assert snapshot["work_items"]["multi"] is True
assert len(snapshot["work_items"]["items"]) == 5
assert snapshot["acceptance"]["passing"] == snapshot["acceptance"]["total"] == 13
print("HANDSOFF_LIVE_DASHBOARD_OK")
