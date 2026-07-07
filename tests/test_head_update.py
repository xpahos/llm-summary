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
