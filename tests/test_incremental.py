"""Second-run behavior: skip already-ingested objects, --force-update, and
comment-based candidate discovery (search misses review-comment-only activity)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from llm_summary import db as db_mod
from llm_summary.graph import run_pipeline
from llm_summary.github_client import GithubClient

from conftest import REPO, FakeGithubClient, FakeSummarizer, make_pr


def _events(config, **where):
    c = db_mod.connect(config.storage.db_path)
    try:
        clauses = " AND ".join(f"{k}=?" for k in where) or "1=1"
        return [
            dict(r)
            for r in c.execute(
                f"SELECT * FROM events WHERE {clauses}", tuple(where.values())
            )
        ]
    finally:
        c.close()


# --- skip / force-update -----------------------------------------------------

def test_second_run_skips_unchanged_objects(config, since_until):
    since, until = since_until
    gh = FakeGithubClient(config.github.repo, {("pr", 1234): make_pr(1234)})
    llm = FakeSummarizer()

    run_pipeline(config, since=since, until=until, gh=gh, summarizer=llm)
    assert gh.fetch_calls == 1

    # Same window, nothing changed upstream: the object must not be re-fetched.
    state = run_pipeline(config, since=since, until=until, gh=gh, summarizer=llm)
    assert not state.get("errors")
    assert gh.fetch_calls == 1


def test_second_run_fetches_changed_objects(config, since_until):
    since, until = since_until
    objects = {("pr", 1234): make_pr(1234)}
    gh = FakeGithubClient(config.github.repo, objects)
    llm = FakeSummarizer()

    run_pipeline(config, since=since, until=until, gh=gh, summarizer=llm)
    assert gh.fetch_calls == 1

    # New upstream activity bumps updated_at: the object must be re-fetched.
    objects[("pr", 1234)] = make_pr(1234, updated="2026-06-27T14:00:00Z")
    run_pipeline(config, since=since, until=until, gh=gh, summarizer=llm)
    assert gh.fetch_calls == 2


def test_force_update_refetches_everything(config, since_until):
    since, until = since_until
    gh = FakeGithubClient(config.github.repo, {("pr", 1234): make_pr(1234)})
    llm = FakeSummarizer()

    run_pipeline(config, since=since, until=until, gh=gh, summarizer=llm)
    run_pipeline(config, since=since, until=until, gh=gh, summarizer=llm, force_update=True)
    assert gh.fetch_calls == 2


def test_skipped_object_keeps_page_fidelity(config, since_until):
    """A skipped object is rebuilt from its stored snapshot, so the rendered
    page keeps merge status and changed files on the second run."""
    since, until = since_until
    pr = make_pr(
        1234,
        labels=["push"],
        reviews=[
            {
                "id": 7001,
                "user": "maintainer",
                "user_type": "User",
                "state": "APPROVED",
                "body": "LGTM",
                "created_at": "2026-06-27T11:00:00Z",
                "url": f"https://github.com/{REPO}/pull/1234#r1",
            }
        ],
    )
    gh = FakeGithubClient(config.github.repo, {("pr", 1234): pr})
    llm = FakeSummarizer()

    run_pipeline(config, since=since, until=until, gh=gh, summarizer=llm)
    run_pipeline(config, since=since, until=until, gh=gh, summarizer=llm)
    assert gh.fetch_calls == 1  # second run reused the snapshot

    c = db_mod.connect(config.storage.db_path)
    try:
        payload = json.loads(
            c.execute("SELECT payload_json FROM daily_pages").fetchone()["payload_json"]
        )
    finally:
        c.close()
    items = [it for s in payload["sections"] for it in s["items"]]
    assert items and items[0]["number"] == 1234
    assert "OvmfPkg/Foo.c" in items[0]["changed_files"]
    assert "push" in items[0]["badges"]  # merge status survived the skip


def test_skip_backfills_missing_window_events_from_snapshot(config):
    """A skipped object still flows through event normalization, so a window
    whose events were never ingested gets them from the stored snapshot."""
    day1 = ("2026-06-26T00:00:00+00:00", "2026-06-27T00:00:00+00:00")
    day2 = ("2026-06-27T00:00:00+00:00", "2026-06-28T00:00:00+00:00")
    pr = make_pr(1234)
    pr["comments"] = [
        {
            "id": 5001,
            "user": "alice",
            "created_at": "2026-06-26T10:00:00Z",  # day 1
            "body": "Early comment.",
            "url": f"https://github.com/{REPO}/pull/1234#c1",
        }
    ]
    gh = FakeGithubClient(config.github.repo, {("pr", 1234): pr})
    llm = FakeSummarizer()

    # Day-2 run first: ingests the object, but only day-2 events (none here).
    run_pipeline(config, since=day2[0], until=day2[1], gh=gh, summarizer=llm)
    assert not _events(config, event_type="commented")

    # Day-1 backfill: object unchanged -> skipped, yet the day-1 comment must
    # be recovered from the snapshot.
    run_pipeline(config, since=day1[0], until=day1[1], gh=gh, summarizer=llm)
    assert gh.fetch_calls == 1
    assert len(_events(config, event_type="commented")) == 1


def test_known_activity_not_in_db_forces_fetch(config, since_until):
    """A candidate carrying discovered comment activity that is missing from the
    events table (e.g. a previously failed sub-fetch) must be re-fetched."""
    since, until = since_until

    class AnnotatingGithub(FakeGithubClient):
        def discover_candidates(self, s, u):
            refs = self.search_candidates(s, u)
            for ref in refs:
                ref["activity"] = [{"type": "review_comment", "id": 424242}]
            return refs

    pr = make_pr(1234)
    gh = AnnotatingGithub(config.github.repo, {("pr", 1234): pr})
    llm = FakeSummarizer()

    run_pipeline(config, since=since, until=until, gh=gh, summarizer=llm)
    assert gh.fetch_calls == 1

    # Comment 424242 was never ingested (not in the object's review_comments),
    # so the skip check must keep failing and the object must be re-fetched.
    run_pipeline(config, since=since, until=until, gh=gh, summarizer=llm)
    assert gh.fetch_calls == 2

    # Once the fetched object actually contains it, the next run can skip again.
    pr["review_comments"] = [
        {
            "id": 424242,
            "user": "bob",
            "user_type": "User",
            "body": "Inline note.",
            "path": "OvmfPkg/Foo.c",
            "created_at": "2026-06-27T11:30:00Z",
            "url": f"https://github.com/{REPO}/pull/1234#rc1",
        }
    ]
    run_pipeline(config, since=since, until=until, gh=gh, summarizer=llm)
    assert gh.fetch_calls == 3
    run_pipeline(config, since=since, until=until, gh=gh, summarizer=llm)
    assert gh.fetch_calls == 3


# --- comment-based discovery -------------------------------------------------

class _FakeUser:
    def __init__(self, login):
        self.login = login


class _FakeComment:
    def __init__(self, cid, created_at, raw):
        self.id = cid
        self.created_at = created_at
        self.raw_data = raw
        self.user = _FakeUser("someone")


class _FakeIssue:
    def __init__(self, raw):
        self.raw_data = raw


class _FakeRepo:
    """Stands in for PyGithub's Repository in discovery tests."""

    def __init__(self, pulls_comments, issues_comments, issues):
        self._pulls_comments = pulls_comments
        self._issues_comments = issues_comments
        self._issues = issues

    def get_pulls_comments(self, **kw):
        return self._pulls_comments

    def get_issues_comments(self, **kw):
        return self._issues_comments

    def get_issue(self, number):
        return self._issues[number]


def _window():
    return (
        datetime(2026, 7, 6, tzinfo=timezone.utc),
        datetime(2026, 7, 7, tzinfo=timezone.utc),
    )


def test_discover_adds_prs_search_missed():
    """Review-comment-only activity (updated_at not bumped) and search-index
    staleness are both recovered from the repo-wide comment listings."""
    since, until = _window()
    client = GithubClient("", REPO)
    client.search_candidates = lambda s, u: []  # search found nothing
    client._repo = _FakeRepo(
        pulls_comments=[
            _FakeComment(
                901,
                datetime(2026, 7, 6, 20, 31, tzinfo=timezone.utc),
                {"pull_request_url": f"https://api.github.com/repos/{REPO}/pulls/12697"},
            ),
            # Outside the window: an edit of an old comment, must be ignored.
            _FakeComment(
                902,
                datetime(2026, 7, 2, 9, 0, tzinfo=timezone.utc),
                {"pull_request_url": f"https://api.github.com/repos/{REPO}/pulls/12000"},
            ),
        ],
        issues_comments=[
            _FakeComment(
                801,
                datetime(2026, 7, 6, 2, 3, tzinfo=timezone.utc),
                {"issue_url": f"https://api.github.com/repos/{REPO}/issues/12681"},
            ),
        ],
        issues={12681: _FakeIssue({"number": 12681, "pull_request": {"url": "x"}})},
    )

    refs = client.discover_candidates(since, until)
    by_number = {r["number"]: r for r in refs}
    assert set(by_number) == {12697, 12681}
    assert by_number[12697]["kind"] == "pr"
    assert by_number[12697]["activity"] == [{"type": "review_comment", "id": 901}]
    assert by_number[12681]["kind"] == "pr"  # resolved via get_issue
    assert by_number[12681]["activity"] == [{"type": "comment", "id": 801}]


def test_discover_annotates_existing_search_candidates():
    """Comment activity on a PR search already found is attached to the existing
    candidate (so the skip check can verify those comments were ingested)."""
    since, until = _window()
    client = GithubClient("", REPO)
    search_ref = {
        "repo": REPO,
        "kind": "pr",
        "number": 12379,
        "url": f"https://github.com/{REPO}/pull/12379",
        "created_at": "2026-04-01T13:58:40Z",
        "updated_at": "2026-07-06T07:40:03Z",
    }
    client.search_candidates = lambda s, u: [search_ref]
    client._repo = _FakeRepo(
        pulls_comments=[
            _FakeComment(
                903,
                datetime(2026, 7, 6, 14, 55, tzinfo=timezone.utc),
                {"pull_request_url": f"https://api.github.com/repos/{REPO}/pulls/12379"},
            ),
        ],
        issues_comments=[],
        issues={},
    )

    refs = client.discover_candidates(since, until)
    assert len(refs) == 1
    assert refs[0]["activity"] == [{"type": "review_comment", "id": 903}]
