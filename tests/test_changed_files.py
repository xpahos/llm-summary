import json
from pathlib import Path

from llm_summary.graph import run_pipeline

from conftest import FakeGithubClient, FakeSummarizer, make_pr


class CapturingSummarizer(FakeSummarizer):
    """Records (event, obj) passed to update_object_summary."""

    def __init__(self):
        self.calls = []

    def update_object_summary(self, prev, event, obj):
        self.calls.append((event, obj))
        return super().update_object_summary(prev, event, obj)


def _merged(number):
    pr = make_pr(number)
    pr["state"] = "closed"
    pr["merged"] = 1
    pr["merged_at"] = "2026-06-27T15:00:00Z"
    return pr


def test_merged_existing_pr_keeps_changed_files(config, since_until):
    since, until = since_until
    objects = {("pr", 1234): make_pr(1234)}  # run 1: open PR (touches OvmfPkg/Foo.c)
    cap = CapturingSummarizer()

    run_pipeline(config, since=since, until=until, gh=FakeGithubClient(config.github.repo, objects), summarizer=cap)

    # run 2: the existing PR is merged.
    objects[("pr", 1234)] = _merged(1234)
    run_pipeline(config, since=since, until=until, gh=FakeGithubClient(config.github.repo, objects), summarizer=cap)

    # The merge update received the changed files (the merge event carries none itself).
    merge_calls = [obj for ev, obj in cap.calls if ev["event_type"] == "merged"]
    assert merge_calls, "expected a merged event to be processed"
    filenames = [f.get("filename") for f in (merge_calls[-1].get("files") or [])]
    assert "OvmfPkg/Foo.c" in filenames

    # And the rendered object page lists the changed file.
    page = Path(config.storage.site_dir) / "2026" / "06" / "27" / "pr" / "1234" / "index.html"
    assert "OvmfPkg/Foo.c" in page.read_text()


def test_new_commits_reach_the_update(config, since_until):
    since, until = since_until
    objects = {("pr", 1234): make_pr(1234, head_sha="abc123")}
    cap = CapturingSummarizer()

    run_pipeline(config, since=since, until=until, gh=FakeGithubClient(config.github.repo, objects), summarizer=cap)

    # New commits -> head SHA changes.
    objects[("pr", 1234)] = make_pr(1234, head_sha="def456")
    run_pipeline(config, since=since, until=until, gh=FakeGithubClient(config.github.repo, objects), summarizer=cap)

    head = [(ev, obj) for ev, obj in cap.calls if ev["event_type"] == "pr_head_updated"]
    assert head, "expected a pr_head_updated event"
    payload = json.loads(head[-1][0]["payload_json"])
    assert payload["commits"], "compare commits must be present"
    assert payload["files"], "compare files must be present"
    assert payload["diff_summary"]
