from llm_summary import db as db_mod
from llm_summary.db import STATE_LAST_UNTIL, get_state
from llm_summary.graph import run_pipeline

from conftest import FakeGithubClient, FakeSummarizer, make_issue, make_pr


def _event_count(config):
    c = db_mod.connect(config.storage.db_path)
    try:
        return c.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
    finally:
        c.close()


def test_rerun_same_window_is_idempotent(config, since_until):
    since, until = since_until
    objects = {
        ("pr", 1234): make_pr(number=1234),
        ("issue", 999): make_issue(number=999),
    }
    gh = FakeGithubClient(config.github.repo, objects)
    llm = FakeSummarizer()

    run_pipeline(config, since=since, until=until, gh=gh, summarizer=llm)
    n1 = _event_count(config)
    assert n1 > 0

    run_pipeline(config, since=since, until=until, gh=gh, summarizer=llm)
    n2 = _event_count(config)
    assert n2 == n1  # no duplicate events


def test_cursor_advances_on_success(config, since_until):
    since, until = since_until
    objects = {("issue", 999): make_issue(number=999)}
    gh = FakeGithubClient(config.github.repo, objects)
    run_pipeline(config, since=since, until=until, gh=gh, summarizer=FakeSummarizer())

    c = db_mod.connect(config.storage.db_path)
    try:
        assert get_state(c, STATE_LAST_UNTIL) == until
        run = c.execute("SELECT status FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        assert run["status"] == "success"
    finally:
        c.close()


def test_cursor_not_advanced_on_failure(config, since_until):
    since, until = since_until

    class BoomGithub(FakeGithubClient):
        def fetch_object(self, kind, number):
            raise RuntimeError("boom")

    gh = BoomGithub(config.github.repo, {("issue", 999): make_issue(number=999)})
    state = run_pipeline(config, since=since, until=until, gh=gh, summarizer=FakeSummarizer())
    assert state.get("errors")

    c = db_mod.connect(config.storage.db_path)
    try:
        assert get_state(c, STATE_LAST_UNTIL) is None  # cursor untouched
        run = c.execute("SELECT status FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        assert run["status"] == "failed"
    finally:
        c.close()
