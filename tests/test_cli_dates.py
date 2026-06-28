from datetime import date

import pytest

from llm_summary import db as db_mod
from llm_summary.db import STATE_LAST_UNTIL, get_state
from llm_summary.graph import run_pipeline
from llm_summary.main import day_window, resolve_days

from conftest import FakeGithubClient, FakeSummarizer, make_issue


# --- resolve_days -----------------------------------------------------------

def test_resolve_days_default_is_none():
    assert resolve_days(None, None, None) is None


def test_resolve_days_single():
    assert resolve_days("2026-06-20", None, None) == [date(2026, 6, 20)]


def test_resolve_days_range_inclusive():
    days = resolve_days(None, "2026-06-01", "2026-06-03")
    assert days == [date(2026, 6, 1), date(2026, 6, 2), date(2026, 6, 3)]


def test_resolve_days_rejects_date_and_range():
    with pytest.raises(ValueError):
        resolve_days("2026-06-20", "2026-06-01", "2026-06-03")


def test_resolve_days_requires_both_range_ends():
    with pytest.raises(ValueError):
        resolve_days(None, "2026-06-01", None)


def test_resolve_days_rejects_reversed_range():
    with pytest.raises(ValueError):
        resolve_days(None, "2026-06-03", "2026-06-01")


def test_day_window():
    since, until = day_window(date(2026, 6, 27))
    assert since == "2026-06-27T00:00:00+00:00"
    assert until == "2026-06-28T00:00:00+00:00"


# --- cursor behaviour for explicit windows ----------------------------------

def test_explicit_window_does_not_advance_cursor(config):
    since, until = day_window(date(2026, 6, 27))
    gh = FakeGithubClient(config.github.repo, {("issue", 999): make_issue(999)})
    run_pipeline(config, since=since, until=until, advance_cursor=False, gh=gh, summarizer=FakeSummarizer())

    c = db_mod.connect(config.storage.db_path)
    try:
        assert get_state(c, STATE_LAST_UNTIL) is None  # backfill must not move the cursor
        run = c.execute("SELECT status FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        assert run["status"] == "success"
    finally:
        c.close()
