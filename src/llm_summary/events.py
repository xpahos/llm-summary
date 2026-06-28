"""Event normalization and stable external_id construction.

Events are inserted with INSERT OR IGNORE keyed on UNIQUE(repo, external_id),
which is the sole idempotency mechanism. Only events whose created_at falls in
the half-open window [since, until) are emitted, except synthetic events the
pipeline creates intentionally (e.g. pr_head_updated).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any, Iterable

from . import review as review_mod
from .db import utcnow_iso

# Event type constants.
OPENED = "opened"
CLOSED = "closed"
MERGED = "merged"
COMMENTED = "commented"
REVIEWED = "reviewed"
REVIEW_COMMENT = "review_comment"
LABELED = "labeled"
UNLABELED = "unlabeled"
REOPENED = "reopened"
PR_HEAD_UPDATED = "pr_head_updated"

# Timeline GitHub event names we normalize.
_TIMELINE_KEEP = {
    "labeled": LABELED,
    "unlabeled": UNLABELED,
    "reopened": REOPENED,
    "assigned": "assigned",
    "review_requested": "review_requested",
}


def in_window(ts: str | None, since: datetime, until: datetime) -> bool:
    """True if ISO timestamp ts is within the half-open window [since, until)."""
    if not ts:
        return False
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return since <= dt < until


# --- external_id builders ---------------------------------------------------

def eid_lifecycle(repo: str, kind: str, number: int, event_type: str) -> str:
    return f"github:{repo}:{kind}:{number}:{event_type}"


def eid_comment(repo: str, comment_id: int) -> str:
    return f"github:{repo}:comment:{comment_id}"


def eid_review(repo: str, review_id: int) -> str:
    return f"github:{repo}:review:{review_id}"


def eid_review_comment(repo: str, comment_id: int) -> str:
    return f"github:{repo}:review_comment:{comment_id}"


def eid_timeline(repo: str, event_id: Any) -> str:
    return f"github:{repo}:timeline:{event_id}"


def eid_head_update(repo: str, number: int, old_sha: str, new_sha: str) -> str:
    return f"github:{repo}:pr:{number}:head:{old_sha}:{new_sha}"


# --- normalization -----------------------------------------------------------

def _event(obj: dict, event_type: str, external_id: str, **kw) -> dict[str, Any]:
    return {
        "repo": obj["repo"],
        "object_kind": obj["kind"],
        "object_number": obj["number"],
        "event_type": event_type,
        "external_id": external_id,
        "actor": kw.get("actor"),
        "created_at": kw.get("created_at"),
        "title": kw.get("title"),
        "body": kw.get("body"),
        "url": kw.get("url"),
        "payload_json": kw.get("payload_json"),
    }


def normalize_object_events(
    obj: dict[str, Any], since: datetime, until: datetime
) -> list[dict[str, Any]]:
    """Produce daily events for an object, filtered to [since, until).

    Does NOT include pr_head_updated (the crawler emits that synthetically with
    compare data).
    """
    repo, kind, number = obj["repo"], obj["kind"], obj["number"]
    events: list[dict[str, Any]] = []

    # Lifecycle: opened.
    if in_window(obj.get("created_at"), since, until):
        events.append(
            _event(
                obj,
                OPENED,
                eid_lifecycle(repo, kind, number, OPENED),
                actor=obj.get("author"),
                created_at=obj.get("created_at"),
                title=obj.get("title"),
                url=obj.get("url"),
            )
        )

    # Lifecycle: merged (preferred) / closed.
    if kind == "pr" and obj.get("merged") and in_window(obj.get("merged_at"), since, until):
        events.append(
            _event(
                obj,
                MERGED,
                eid_lifecycle(repo, kind, number, MERGED),
                created_at=obj.get("merged_at"),
                title=obj.get("title"),
                url=obj.get("url"),
            )
        )
    elif in_window(obj.get("closed_at"), since, until):
        events.append(
            _event(
                obj,
                CLOSED,
                eid_lifecycle(repo, kind, number, CLOSED),
                created_at=obj.get("closed_at"),
                title=obj.get("title"),
                url=obj.get("url"),
            )
        )

    # Comments.
    for c in obj.get("comments", []):
        if in_window(c.get("created_at"), since, until):
            events.append(
                _event(
                    obj,
                    COMMENTED,
                    eid_comment(repo, c["id"]),
                    actor=c.get("user"),
                    created_at=c.get("created_at"),
                    body=c.get("body"),
                    url=c.get("url"),
                )
            )

    # Reviews (PR).
    for r in obj.get("reviews", []):
        if in_window(r.get("created_at"), since, until):
            events.append(
                _event(
                    obj,
                    REVIEWED,
                    eid_review(repo, r["id"]),
                    actor=r.get("user"),
                    created_at=r.get("created_at"),
                    body=r.get("body"),
                    url=r.get("url"),
                    payload_json=json.dumps(
                        {
                            "state": r.get("state"),
                            "is_bot": review_mod.is_bot(r.get("user"), r.get("user_type")),
                        }
                    ),
                )
            )

    # Review comments (PR).
    for c in obj.get("review_comments", []):
        if in_window(c.get("created_at"), since, until):
            events.append(
                _event(
                    obj,
                    REVIEW_COMMENT,
                    eid_review_comment(repo, c["id"]),
                    actor=c.get("user"),
                    created_at=c.get("created_at"),
                    body=c.get("body"),
                    url=c.get("url"),
                    payload_json=json.dumps({"path": c.get("path")}),
                )
            )

    # Timeline (labeled/unlabeled/reopened/assigned/review_requested).
    for ev in obj.get("timeline", []):
        mapped = _TIMELINE_KEEP.get(ev.get("event") or "")
        if not mapped:
            continue
        if not in_window(ev.get("created_at"), since, until):
            continue
        if ev.get("event_id") is None:
            continue
        events.append(
            _event(
                obj,
                mapped,
                eid_timeline(repo, ev["event_id"]),
                actor=ev.get("actor"),
                created_at=ev.get("created_at"),
                body=ev.get("label"),
                url=ev.get("url"),
            )
        )

    return events


# --- insertion ---------------------------------------------------------------

_INSERT_SQL = """
INSERT OR IGNORE INTO events
    (repo, object_kind, object_number, event_type, external_id,
     actor, created_at, seen_at, title, body, url, payload_json, processed)
VALUES
    (:repo, :object_kind, :object_number, :event_type, :external_id,
     :actor, :created_at, :seen_at, :title, :body, :url, :payload_json, 0)
"""


def insert_events(conn: sqlite3.Connection, events: Iterable[dict[str, Any]]) -> list[int]:
    """Insert events idempotently. Returns ids of rows actually inserted."""
    seen_at = utcnow_iso()
    inserted: list[int] = []
    for ev in events:
        row = dict(ev)
        row.setdefault("payload_json", None)
        row["seen_at"] = seen_at
        cur = conn.execute(_INSERT_SQL, row)
        if cur.rowcount == 1:
            inserted.append(cur.lastrowid)
    return inserted
