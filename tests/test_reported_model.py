"""#306: the provider's own word on which model ran, for every adapter.

`reported_model` was null for every Codex session, because the parser
refused any adapter that was not Claude and was only consulted on JSON
lines, while Codex prints its model in a plain-text banner. Model
reconciliation was therefore inert for the entire default crew, which sets
architect, implementer and reviewer to codex.

Nothing here infers a model. A session whose adapter printed no model
records null, and that is a different fact from a mismatch.
"""
import sys
import unittest
from pathlib import Path

BIN = Path(__file__).resolve().parent.parent / "bin"
sys.path.insert(0, str(BIN))

import handsoff_lib as lib  # noqa: E402

#: The banner Codex v0.153.4 prints, verbatim from session
#: hs-978fd700da4d42b3b9ca70545bc0ceb5 on 2026-09-23.
CODEX_BANNER = """2026-09-23T23:44:09.613619Z ERROR codex_models_manager::manager: failed to refresh available models
OpenAI Codex v0.153.4
--------
workdir: /private/var/folders/q4/xzn7tg717pgc6j0nys376qwr0000gn/T/handsoff-reviewer-7zx6wxbb
model: gpt-5.6-luna
provider: openai
approval: never
sandbox: workspace-write [workdir, /tmp, $TMPDIR]
reasoning effort: high
reasoning summaries: none
session id: 01a0d0a7-bfaf-7091-ad2b-961b25dbe238
--------
user
"""


def feed(adapter, text):
    watcher = lib.UsageWatcher(adapter)
    for line in text.splitlines():
        watcher.feed(line)
    return watcher


class TheCodexBannerIsRead(unittest.TestCase):

    def test_a_codex_session_records_the_model_its_banner_printed(self):
        self.assertEqual(feed("codex", CODEX_BANNER).reported_model, "gpt-5.6-luna")

    def test_a_failed_session_still_records_it(self):
        """The banner precedes the work, so a session that later exhausts its
        budget has already reported its model. Both sessions on 2026-09-23
        recorded null; both should have recorded gpt-5.6-luna."""
        exhausted = CODEX_BANNER + (
            "Please review the packet.\n"
            "ERROR: shared rollout token budget exhausted\n"
            "tokens used\n49,264\n"
        )
        watcher = feed("codex", exhausted)
        self.assertEqual(watcher.reported_model, "gpt-5.6-luna")
        self.assertEqual(watcher.usage["tokens_total"], 49264)

    def test_the_model_and_the_usage_are_captured_independently(self):
        watcher = feed("codex", CODEX_BANNER)
        self.assertEqual(watcher.reported_model, "gpt-5.6-luna")
        self.assertIsNone(watcher.usage, "no usage was printed, so none is recorded")


class ThePacketCannotSpoofIt(unittest.TestCase):

    def test_a_model_line_in_the_task_text_is_ignored(self):
        """The packet is echoed after the banner closes and routinely
        discusses model names that were never used."""
        spoof = CODEX_BANNER + "The proposal says model: claude-opus-5 should be considered.\n"
        self.assertEqual(feed("codex", spoof).reported_model, "gpt-5.6-luna")

    def test_a_model_line_before_the_banner_opens_is_ignored(self):
        early = "model: not-the-real-one\n" + CODEX_BANNER
        self.assertEqual(feed("codex", early).reported_model, "gpt-5.6-luna")

    def test_the_first_banner_field_wins(self):
        twice = CODEX_BANNER.replace("provider: openai", "model: a-second-value\nprovider: openai")
        self.assertEqual(feed("codex", twice).reported_model, "gpt-5.6-luna")


class AMalformedBannerReportsNothing(unittest.TestCase):

    def test_a_model_beginning_with_a_dash_is_refused(self):
        bad = CODEX_BANNER.replace("model: gpt-5.6-luna", "model: -rf")
        self.assertIsNone(feed("codex", bad).reported_model)

    def test_a_banner_with_no_model_field_records_null(self):
        bad = CODEX_BANNER.replace("model: gpt-5.6-luna\n", "")
        self.assertIsNone(feed("codex", bad).reported_model)

    def test_an_absent_banner_records_null(self):
        self.assertIsNone(feed("codex", "just some output\ntokens used\n10\n").reported_model)


class OtherAdaptersAreUnchanged(unittest.TestCase):

    def test_claude_stream_json_still_reports_from_the_init_event(self):
        stream = '{"type":"system","subtype":"init","model":"claude-opus-5"}'
        self.assertEqual(feed("claude", stream).reported_model, "claude-opus-5")

    def test_claude_prefers_the_final_model_usage_accounting(self):
        stream = ('{"type":"system","subtype":"init","model":"claude-opus-5"}\n'
                  '{"modelUsage":{"claude-sonnet-5":{"inputTokens":1}}}')
        self.assertEqual(feed("claude", stream).reported_model, "claude-sonnet-5")

    def test_claude_does_not_read_a_codex_banner(self):
        self.assertIsNone(feed("claude", CODEX_BANNER).reported_model)

    def test_an_adapter_with_no_known_shape_reports_nothing(self):
        self.assertIsNone(feed("some-future-adapter", CODEX_BANNER).reported_model)

    def test_an_unset_adapter_reports_nothing(self):
        self.assertIsNone(feed(None, CODEX_BANNER).reported_model)


class ReconciliationNowRunsForCodex(unittest.TestCase):
    """The point of capturing the model: `_adaptive_model_reconciliation`
    short-circuits on a null, so before this fix every Codex session was
    permanently `pending_verification` and a mismatch could never surface."""

    def session(self, reported):
        return {"adapter": "codex", "reported_model": reported,
                "adaptive_routing": {"model": "gpt-5.6-luna", "tier": "PREMIUM",
                                     "profile": {"adapter": "codex", "model": "gpt-5.6-luna"}}}

    def test_a_matching_model_reconciles_as_matched(self):
        result = lib._adaptive_model_reconciliation(self.session("gpt-5.6-luna"))
        self.assertEqual(result["consistency"], "matched")

    def test_a_null_is_still_pending_verification(self):
        result = lib._adaptive_model_reconciliation(self.session(None))
        self.assertEqual(result["consistency"], "pending_verification")

    def test_a_captured_model_no_longer_leaves_the_session_pending(self):
        self.assertNotEqual(
            lib._adaptive_model_reconciliation(self.session("gpt-5.6-luna"))["consistency"],
            "pending_verification",
        )

    def test_the_assignment_view_names_the_adapter_as_the_source(self):
        view = lib._agent_assignment({
            "adapter": "codex", "reported_model": "gpt-5.6-luna", "role": "reviewer",
            "requested_model": "default",
        })
        self.assertEqual(view["model"], "gpt-5.6-luna")
        self.assertEqual(view["model_source"], "adapter_reported")

    def test_without_a_reported_model_the_source_is_not_adapter_reported(self):
        view = lib._agent_assignment({
            "adapter": "codex", "reported_model": None, "role": "reviewer",
            "requested_model": "default",
        })
        self.assertNotEqual(view["model_source"], "adapter_reported")


if __name__ == "__main__":
    unittest.main()
