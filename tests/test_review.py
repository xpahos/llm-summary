import json

from llm_summary import db as db_mod
from llm_summary import review
from llm_summary.graph import run_pipeline

from conftest import FakeGithubClient, FakeSummarizer, make_pr


def _review(user, state, ts="2026-06-27T11:00:00Z", user_type="User", rid=None):
    return {
        "id": rid if rid is not None else abs(hash((user, state, ts))) % 100000,
        "user": user,
        "user_type": user_type,
        "state": state,
        "body": "",
        "created_at": ts,
        "url": "u",
    }


# --- bot detection ----------------------------------------------------------

def test_is_bot():
    assert review.is_bot("mergify[bot]")
    assert review.is_bot("mergify")              # known automation login
    assert review.is_bot("dependabot[bot]")
    assert review.is_bot("anything", "Bot")      # GitHub user type
    assert not review.is_bot("alice")
    assert not review.is_bot("alice", "User")
    assert not review.is_bot(None)


# --- merge status -----------------------------------------------------------

def test_issue_has_no_merge_status():
    assert review.merge_status({"kind": "issue"}) is None


def test_human_approval_without_push_label():
    pr = make_pr(reviews=[_review("alice", "APPROVED")], labels=["OvmfPkg"])
    ms = review.merge_status(pr)
    assert ms["approval_count"] == 1
    assert ms["approvals"] == ["alice"]
    assert ms["has_push_label"] is False
    assert ms["ready_to_merge"] is False
    assert "push" in review.describe(ms)


def test_push_label_makes_it_ready():
    pr = make_pr(reviews=[_review("alice", "APPROVED")], labels=["push", "OvmfPkg"])
    ms = review.merge_status(pr)
    assert ms["has_push_label"] is True
    assert ms["ready_to_merge"] is True
    assert "push" in review.badges(ms) and "ready to merge" in review.badges(ms)


def test_bot_approval_not_counted():
    pr = make_pr(reviews=[_review("mergify[bot]", "APPROVED", user_type="Bot")], labels=["OvmfPkg"])
    ms = review.merge_status(pr)
    assert ms["approval_count"] == 0
    assert ms["bot_approvals"] == ["mergify[bot]"]
    assert "needs review" in review.badges(ms)
    assert "no maintainer approvals yet" in review.describe(ms)


def test_latest_review_state_wins():
    # alice approves, then later requests changes -> changes_requested wins.
    pr = make_pr(
        reviews=[
            _review("alice", "APPROVED", ts="2026-06-27T10:00:00Z", rid=1),
            _review("alice", "CHANGES_REQUESTED", ts="2026-06-27T12:00:00Z", rid=2),
        ],
        labels=["OvmfPkg"],
    )
    ms = review.merge_status(pr)
    assert ms["approval_count"] == 0
    assert ms["changes_requested"] == ["alice"]


# --- end-to-end: facts land in the rendered view model ----------------------

def _vm_item(config, number):
    c = db_mod.connect(config.storage.db_path)
    try:
        row = c.execute("SELECT payload_json FROM daily_pages ORDER BY date DESC LIMIT 1").fetchone()
    finally:
        c.close()
    vm = json.loads(row["payload_json"])
    for section in vm["sections"]:
        for item in section["items"]:
            if item["number"] == number:
                return item
    raise AssertionError("item not found in view model")


def test_pipeline_enforces_push_and_bot_facts(config, since_until):
    since, until = since_until
    objects = {
        ("pr", 1234): make_pr(1234, labels=["push", "OvmfPkg"], reviews=[_review("alice", "APPROVED")]),
        ("pr", 1235): make_pr(1235, labels=["OvmfPkg"], reviews=[_review("mergify[bot]", "APPROVED", user_type="Bot")]),
    }
    gh = FakeGithubClient(config.github.repo, objects)
    run_pipeline(config, since=since, until=until, gh=gh, summarizer=FakeSummarizer())

    ready = _vm_item(config, 1234)
    assert "push" in ready["badges"] and "ready to merge" in ready["badges"]
    assert "queued for merge" in ready["status"]

    botted = _vm_item(config, 1235)
    assert "needs review" in botted["badges"]
    assert "no maintainer approvals yet" in botted["status"]
