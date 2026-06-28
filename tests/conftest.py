"""Shared fixtures and fakes for llm_summary tests (no network, no real LLM)."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone

import pytest

from llm_summary import db as db_mod
from llm_summary.config import (
    Config,
    CrawlerConfig,
    GithubConfig,
    LLMConfig,
    StorageConfig,
)
from llm_summary.llm import DayViewModel, ViewItem, ViewSection, ViewStats

REPO = "tianocore/edk2"
SINCE = "2026-06-27T00:00:00+00:00"
UNTIL = "2026-06-28T00:00:00+00:00"


def make_issue(number=999, created="2026-06-27T08:00:00Z", comments=None):
    return {
        "repo": REPO,
        "kind": "issue",
        "number": number,
        "title": f"Issue {number}",
        "body": "Something is broken.",
        "state": "open",
        "author": "reporter",
        "url": f"https://github.com/{REPO}/issues/{number}",
        "created_at": created,
        "updated_at": "2026-06-27T09:00:00Z",
        "closed_at": None,
        "labels": ["bug"],
        "head_sha": None,
        "base_ref": None,
        "head_ref": None,
        "merged": 0,
        "merged_at": None,
        "comments": comments
        or [
            {
                "id": 5001,
                "user": "maintainer",
                "created_at": "2026-06-27T10:00:00Z",
                "body": "Can you provide a repro?",
                "url": f"https://github.com/{REPO}/issues/{number}#c1",
            }
        ],
        "reviews": [],
        "review_comments": [],
        "commits": [],
        "files": [],
        "checks": [],
        "timeline": [],
        "raw_json": json.dumps({"number": number}),
    }


def make_pr(number=1234, created="2026-06-27T07:00:00Z", head_sha="abc123", labels=None, reviews=None):
    return {
        "repo": REPO,
        "kind": "pr",
        "number": number,
        "title": f"PR {number}",
        "body": "Adds a dedicated handle range.",
        "state": "open",
        "author": "contributor",
        "author_type": "User",
        "url": f"https://github.com/{REPO}/pull/{number}",
        "created_at": created,
        "updated_at": "2026-06-27T12:00:00Z",
        "closed_at": None,
        "labels": ["OvmfPkg"] if labels is None else labels,
        "head_sha": head_sha,
        "base_ref": "master",
        "head_ref": "feature",
        "merged": 0,
        "merged_at": None,
        "comments": [],
        "reviews": reviews if reviews is not None else [
            {
                "id": 7001,
                "user": "reviewer",
                "user_type": "User",
                "state": "CHANGES_REQUESTED",
                "body": "Please add tests.",
                "created_at": "2026-06-27T11:00:00Z",
                "url": f"https://github.com/{REPO}/pull/{number}#r1",
            }
        ],
        "review_comments": [],
        "commits": [{"sha": head_sha, "message": "Initial", "author": "contributor"}],
        "files": [{"filename": "OvmfPkg/Foo.c", "status": "modified", "additions": 12, "deletions": 4}],
        "checks": [],
        "timeline": [],
        "raw_json": json.dumps({"number": number}),
    }


class FakeGithubClient:
    def __init__(self, repo, objects):
        self.repo_name = repo
        self.objects = objects  # {(kind, number): obj dict}
        self.compare_calls = 0

    def candidates(self):
        refs = []
        for (kind, number), obj in self.objects.items():
            refs.append(
                {
                    "repo": self.repo_name,
                    "kind": kind,
                    "number": number,
                    "url": obj["url"],
                    "created_at": obj["created_at"],
                    "updated_at": obj["updated_at"],
                }
            )
        return refs

    def search_candidates(self, since, until):
        return self.candidates()

    def fetch_object(self, kind, number):
        return copy.deepcopy(self.objects[(kind, number)])

    def compare(self, base_sha, head_sha):
        self.compare_calls += 1
        return {
            "compare_url": f"https://github.com/{self.repo_name}/compare/{base_sha}...{head_sha}",
            "commits": [{"sha": head_sha, "message": "Update", "author": "contributor"}],
            "files": [{"filename": "OvmfPkg/Foo.c", "status": "modified", "additions": 3, "deletions": 1, "patch": "@@"}],
        }


class FakeSummarizer:
    """Deterministic, offline Summarizer used in tests."""

    def initial_object_summary(self, obj):
        return f"Initial summary for {obj['kind']} #{obj['number']}."

    def summarize_head_diff(self, old_sha, new_sha, compare):
        return f"Author updated the PR from {old_sha} to {new_sha}."

    def update_object_summary(self, prev, event, obj):
        return f"{prev} [{event['event_type']}]".strip()

    def daily_view_model(self, payload):
        items = []
        merged = 0
        for it in payload["items"]:
            if it.get("merged"):
                merged += 1
            items.append(
                ViewItem(
                    kind=it["kind"],
                    number=it["number"],
                    title=it.get("title") or "",
                    # Concise day-page summary, deliberately distinct from the full
                    # rolling summary so the day/object split is testable.
                    summary=f"MAINPOINT {it['number']}",
                    activity=[e["type"] for e in it.get("events", [])],
                )
            )
        prs = sum(1 for it in payload["items"] if it["kind"] == "pr")
        issues = sum(1 for it in payload["items"] if it["kind"] == "issue")
        return DayViewModel(
            date=payload["date"],
            repo=payload["repo"],
            headline="edk2 daily",
            stats=ViewStats(prs_updated=prs, issues_active=issues, merged=merged, needs_attention=0),
            highlights=["Test highlight."],
            sections=[ViewSection(id="updated", title="Updated", items=items)],
        )


@pytest.fixture
def conn():
    c = db_mod.connect(":memory:")
    db_mod.init_db(c)
    yield c
    c.close()


@pytest.fixture
def config(tmp_path):
    return Config(
        github=GithubConfig(token="x", repo=REPO),
        llm=LLMConfig(provider="openai", api_key="x", model="gpt-4o"),
        storage=StorageConfig(
            db_path=str(tmp_path / "db.sqlite"),
            site_dir=str(tmp_path / "site"),
        ),
        crawler=CrawlerConfig(timezone="UTC", default_bootstrap_days=1),
    )


@pytest.fixture
def since_until():
    return SINCE, UNTIL


@pytest.fixture
def window_dts():
    return (
        datetime.fromisoformat(SINCE).astimezone(timezone.utc),
        datetime.fromisoformat(UNTIL).astimezone(timezone.utc),
    )
