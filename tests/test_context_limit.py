"""Regression tests for the context_length_exceeded wedge (oversized head payloads)."""

import json

from llm_summary import db as db_mod
from llm_summary.graph import run_pipeline
from llm_summary.llm import _event_payload_for_prompt

from conftest import FakeGithubClient, FakeSummarizer, make_pr


def test_head_payload_compacted_drops_patches_and_files():
    # Simulate a stored pr_head_updated payload with huge commits/files.
    payload = {
        "old_head_sha": "aaa",
        "new_head_sha": "bbb",
        "compare_url": "https://example/compare",
        "diff_summary": "Fixed the thing.",
        "commits": [{"sha": "x" * 40, "message": "m" * 5000} for _ in range(50)],
        "files": [{"filename": f"Pkg/F{i}.c", "patch": "@@" + "x" * 100000} for i in range(30)],
    }
    out = _event_payload_for_prompt("pr_head_updated", payload)
    blob = json.dumps(out)
    assert "diff_summary" in out and out["diff_summary"] == "Fixed the thing."
    assert out["commit_count"] == 50 and out["file_count"] == 30
    assert "patch" not in blob              # raw patches never sent
    assert len(blob) < 5000                 # compact regardless of input size


def test_non_head_payload_passthrough():
    assert _event_payload_for_prompt("reviewed", {"state": "APPROVED", "is_bot": False}) == {
        "state": "APPROVED",
        "is_bot": False,
    }
    assert _event_payload_for_prompt("commented", None) is None


class _BoomOnHeadSummarizer(FakeSummarizer):
    """Raises only when folding a pr_head_updated event (simulates a poison event)."""

    def update_object_summary(self, prev, event, obj):
        if event.get("event_type") == "pr_head_updated":
            raise RuntimeError("context_length_exceeded (simulated)")
        return super().update_object_summary(prev, event, obj)


def _unprocessed(config):
    c = db_mod.connect(config.storage.db_path)
    try:
        return c.execute("SELECT COUNT(*) FROM events WHERE processed=0").fetchone()[0]
    finally:
        c.close()


def test_one_poison_event_does_not_wedge_the_run(config, since_until):
    since, until = since_until
    # Run 1: PR seen at head abc (new, no head event yet).
    objects = {("pr", 1234): make_pr(1234, head_sha="abc123")}
    gh = FakeGithubClient(config.github.repo, objects)
    run_pipeline(config, since=since, until=until, gh=gh, summarizer=_BoomOnHeadSummarizer())

    # Run 2: head changes -> a pr_head_updated event whose update will "blow up".
    # A push always bumps updated_at on GitHub, so the fixture models that too.
    objects[("pr", 1234)] = make_pr(1234, head_sha="def456", updated="2026-06-27T13:00:00Z")
    state = run_pipeline(
        config, since=since, until=until,
        gh=FakeGithubClient(config.github.repo, objects),
        summarizer=_BoomOnHeadSummarizer(),
    )

    # The run must still succeed (no errors bubbled up) despite the poison event,
    # and the non-poison events must have been processed (only the head event left).
    assert not state.get("errors")
    assert _unprocessed(config) == 1  # just the failing pr_head_updated event
