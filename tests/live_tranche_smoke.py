#!/usr/bin/env python3
"""Narrow deployed-system check for the #33 to #40 tranche.

Proves two things about the deployed result: the pushed commit is what
origin/main serves, and the persistent local Mission Control instance is
running the new code (it answers with the fields the tranche added).
"""
import json
import subprocess
import urllib.request

ROOT = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True,
                      check=True).stdout.strip()
subprocess.run(["git", "fetch", "-q", "origin"], cwd=ROOT, check=True)
head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True,
                      check=True).stdout.strip()
remote = subprocess.run(["git", "rev-parse", "origin/main"], cwd=ROOT, capture_output=True,
                        text=True, check=True).stdout.strip()
assert head == remote, f"HEAD {head[:12]} is not origin/main {remote[:12]}"

with urllib.request.urlopen("http://127.0.0.1:8765/api/dashboard", timeout=5) as response:
    assert response.status == 200
    snapshot = json.load(response)

assert snapshot["initialized"] is True
assert snapshot["status"]["deployment_approved"]
assert snapshot["live"]["state"]                               # #33
assert "design_review_attempts" in snapshot["policy"]          # #35
assert "design_reviewer_selection" in snapshot["policy"]       # #37
assert "design_evidence" in snapshot                           # #38
assert "design_review_packet" in snapshot                      # #36
assert "crew" in snapshot["settings"]                          # #39
print("HANDSOFF_LIVE_TRANCHE_OK")
