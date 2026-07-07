"""Crawler logic: window computation, object upsert, PR head-update detection."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from dateutil import parser as date_parser

from . import events as events_mod
from .config import Config
from .db import STATE_LAST_UNTIL, get_state, utcnow_iso

log = logging.getLogger("llm_summary.crawler")


def _parse(ts: str) -> datetime:
    dt = date_parser.isoparse(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def compute_window(
    conn: sqlite3.Connection,
    config: Config,
    since: str | None = None,
    until: str | None = None,
) -> tuple[datetime, datetime]:
    """Determine the half-open processing window [since, until).

    Defaults (run-daily): until = start of current UTC day; since = previous
    successful run's until, or until - default_bootstrap_days on first run.
    """
    now = datetime.now(timezone.utc)
    start_today = now.replace(hour=0, minute=0, second=0, microsecond=0)

    until_dt = _parse(until) if until else start_today

    if since:
        since_dt = _parse(since)
    else:
        last = get_state(conn, STATE_LAST_UNTIL)
        if last:
            since_dt = _parse(last)
        else:
            since_dt = until_dt - timedelta(days=config.crawler.default_bootstrap_days)

    return since_dt, until_dt


_UPSERT_SQL = """
INSERT INTO objects
    (repo, kind, number, title, body, state, author, url,
     created_at, updated_at, closed_at,
     head_sha, base_ref, head_ref, merged, merged_at,
     first_seen_at, last_seen_at, raw_json, snapshot_json)
VALUES
    (:repo, :kind, :number, :title, :body, :state, :author, :url,
     :created_at, :updated_at, :closed_at,
     :head_sha, :base_ref, :head_ref, :merged, :merged_at,
     :first_seen_at, :last_seen_at, :raw_json, :snapshot_json)
ON CONFLICT(repo, kind, number) DO UPDATE SET
    title=excluded.title,
    body=excluded.body,
    state=excluded.state,
    author=excluded.author,
    url=excluded.url,
    created_at=excluded.created_at,
    updated_at=excluded.updated_at,
    closed_at=excluded.closed_at,
    head_sha=excluded.head_sha,
    base_ref=excluded.base_ref,
    head_ref=excluded.head_ref,
    merged=excluded.merged,
    merged_at=excluded.merged_at,
    last_seen_at=excluded.last_seen_at,
    raw_json=excluded.raw_json,
    snapshot_json=excluded.snapshot_json
"""


def upsert_object(conn: sqlite3.Connection, obj: dict[str, Any]) -> dict[str, Any]:
    """Upsert an object snapshot.

    Preserves first_seen_at if the row already exists; always bumps last_seen_at.
    Returns metadata: {is_new_local, old_head_sha, new_head_sha}.
    """
    prev = conn.execute(
        "SELECT first_seen_at, head_sha FROM objects WHERE repo=? AND kind=? AND number=?",
        (obj["repo"], obj["kind"], obj["number"]),
    ).fetchone()

    now = utcnow_iso()
    is_new_local = prev is None
    first_seen_at = now if is_new_local else prev["first_seen_at"]
    old_head_sha = None if is_new_local else prev["head_sha"]

    row = {
        "repo": obj["repo"],
        "kind": obj["kind"],
        "number": obj["number"],
        "title": obj.get("title", ""),
        "body": obj.get("body"),
        "state": obj.get("state", "unknown"),
        "author": obj.get("author"),
        "url": obj.get("url", ""),
        "created_at": obj.get("created_at"),
        "updated_at": obj.get("updated_at"),
        "closed_at": obj.get("closed_at"),
        "head_sha": obj.get("head_sha"),
        "base_ref": obj.get("base_ref"),
        "head_ref": obj.get("head_ref"),
        "merged": 1 if obj.get("merged") else 0,
        "merged_at": obj.get("merged_at"),
        "first_seen_at": first_seen_at,
        "last_seen_at": now,
        "raw_json": obj.get("raw_json"),
        # The full normalized dict, so later runs can rebuild the object without
        # re-fetching it from GitHub (raw_json is excluded — it's stored above).
        "snapshot_json": json.dumps(
            {k: v for k, v in obj.items() if k != "raw_json"}, default=str
        ),
    }
    conn.execute(_UPSERT_SQL, row)

    return {
        "is_new_local": is_new_local,
        "old_head_sha": old_head_sha,
        "new_head_sha": obj.get("head_sha"),
    }


def build_head_update_event(
    obj: dict[str, Any],
    old_sha: str,
    new_sha: str,
    compare: dict[str, Any],
    diff_summary: str,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Construct a synthetic pr_head_updated event with compare payload."""
    payload = {
        "old_head_sha": old_sha,
        "new_head_sha": new_sha,
        "compare_url": compare.get("compare_url"),
        "commits": compare.get("commits", []),
        "files": compare.get("files", []),
        "diff_summary": diff_summary,
    }
    return events_mod._event(
        obj,
        events_mod.PR_HEAD_UPDATED,
        events_mod.eid_head_update(obj["repo"], obj["number"], old_sha, new_sha),
        actor=obj.get("author"),
        created_at=created_at or obj.get("updated_at") or utcnow_iso(),
        title=obj.get("title"),
        body=diff_summary,
        url=compare.get("compare_url") or obj.get("url"),
        payload_json=json.dumps(payload),
    )


def head_changed(meta: dict[str, Any]) -> bool:
    """True when both old/new head SHAs exist and differ."""
    old, new = meta.get("old_head_sha"), meta.get("new_head_sha")
    return bool(old) and bool(new) and old != new
