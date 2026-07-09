from llm_summary import events as events_mod
from llm_summary.graph import run_pipeline

from conftest import FakeGithubClient, FakeSummarizer, make_pr


def _count_head_events(config):
    from llm_summary import db as db_mod

    c = db_mod.connect(config.storage.db_path)
    try:
        return c.execute(
            "SELECT COUNT(*) AS n FROM events WHERE event_type=?",
            (events_mod.PR_HEAD_UPDATED,),
        ).fetchone()["n"]
    finally:
        c.close()


def test_head_change_creates_exactly_one_event(config, since_until):
    since, until = since_until
    objects = {("pr", 1234): make_pr(number=1234, head_sha="abc123")}
    gh = FakeGithubClient(config.github.repo, objects)
    llm = FakeSummarizer()

    # Run 1: new object, head unchanged locally -> no head event.
    run_pipeline(config, since=since, until=until, gh=gh, summarizer=llm)
    assert _count_head_events(config) == 0
    assert gh.compare_calls == 0

    # Run 2: head SHA changes -> exactly one pr_head_updated event + one compare call.
    # A push always bumps updated_at on GitHub, so the fixture models that too.
    objects[("pr", 1234)] = make_pr(number=1234, head_sha="def456", updated="2026-06-27T13:00:00Z")
    run_pipeline(config, since=since, until=until, gh=gh, summarizer=llm)
    assert _count_head_events(config) == 1
    assert gh.compare_calls == 1

    # Run 3: same head SHA -> no new event (idempotent).
    run_pipeline(config, since=since, until=until, gh=gh, summarizer=llm)
    assert _count_head_events(config) == 1


class PayloadSpySummarizer(FakeSummarizer):
    """Records every payload handed to daily_view_model."""

    def __init__(self):
        self.payloads = []

    def daily_view_model(self, payload):
        self.payloads.append(payload)
        return super().daily_view_model(payload)


def test_head_update_does_not_leak_onto_later_day_pages(config):
    """A synthetic head-update belongs only to the run that detected it.

    Its seen_at is the run's wall-clock time, which always lies after any
    later window's `since` — selecting by seen_at would put day-1 force-push
    activity on every subsequent day's page.
    """
    objects = {("pr", 1234): make_pr(number=1234, head_sha="abc123")}
    gh = FakeGithubClient(config.github.repo, objects)
    llm = PayloadSpySummarizer()

    # Day 1: ingest the PR, then detect a head change on re-crawl.
    day1 = dict(since="2026-06-27T00:00:00+00:00", until="2026-06-28T00:00:00+00:00")
    run_pipeline(config, gh=gh, summarizer=llm, **day1)
    objects[("pr", 1234)] = make_pr(
        number=1234, head_sha="def456", updated="2026-06-27T13:00:00Z"
    )
    run_pipeline(config, gh=gh, summarizer=llm, **day1)
    day1_events = [
        e["type"] for item in llm.payloads[-1]["items"] for e in item["events"]
    ]
    assert events_mod.PR_HEAD_UPDATED in day1_events

    # Day 2: no activity at all — day 1's synthetic event must not reappear.
    gh.objects = {}
    run_pipeline(
        config,
        gh=gh,
        summarizer=llm,
        since="2026-06-28T00:00:00+00:00",
        until="2026-06-29T00:00:00+00:00",
    )
    assert llm.payloads[-1]["items"] == []
