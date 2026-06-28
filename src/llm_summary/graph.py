"""LangGraph workflow: a deterministic, linear pipeline.

State is intentionally tiny (refs, ids, paths). Large payloads live in SQLite or
on the transient Pipeline context object, never in graph state. Any node
exception routes the graph to fail_run, which leaves the cursor unadvanced.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from datetime import date, datetime
from typing import Any, Callable, TypedDict

from . import crawler as crawler_mod
from . import events as events_mod
from .config import Config
from .db import (
    STATE_LAST_UNTIL,
    connect,
    init_db,
    set_state,
    transaction,
    utcnow_iso,
)
from . import review as review_mod
from .github_client import GithubClient
from .llm import Summarizer, make_summarizer

log = logging.getLogger("llm_summary.graph")


def _concise(text: str, max_sentences: int = 2, limit: int = 320) -> str:
    """First sentence(s) of a longer summary, as a concise day-page fallback."""
    text = " ".join(text.split())
    if not text:
        return ""
    sentences: list[str] = []
    buf = ""
    for ch in text:
        buf += ch
        if ch in ".!?":
            sentences.append(buf.strip())
            buf = ""
            if len(sentences) >= max_sentences:
                break
    if buf.strip():
        sentences.append(buf.strip())
    out = " ".join(sentences).strip()
    return out if len(out) <= limit else out[:limit].rstrip() + "…"


class DigestState(TypedDict, total=False):
    run_id: int
    repo: str
    since: str
    until: str
    candidate_refs: list[dict]
    synced_object_refs: list[dict]
    event_ids: list[int]
    output_paths: list[str]
    errors: list[str]


class Pipeline:
    """Holds shared run context for the graph nodes (not stored in graph state)."""

    def __init__(
        self,
        config: Config,
        conn: sqlite3.Connection,
        gh: GithubClient,
        summarizer: Summarizer,
        since: str | None = None,
        until: str | None = None,
        advance_cursor: bool = True,
    ):
        self.config = config
        self.conn = conn
        self.gh = gh
        self.llm = summarizer
        self._since_arg = since
        self._until_arg = until
        self.advance_cursor = advance_cursor

        self.since_dt: datetime | None = None
        self.until_dt: datetime | None = None
        self.day: date | None = None
        self.object_cache: dict[tuple[str, int], dict] = {}
        self.newly_seen: set[tuple[str, int]] = set()
        self.view_model = None

    # --- nodes -------------------------------------------------------------
    def load_window(self, state: DigestState) -> DigestState:
        init_db(self.conn)
        since_dt, until_dt = crawler_mod.compute_window(
            self.conn, self.config, self._since_arg, self._until_arg
        )
        self.since_dt, self.until_dt = since_dt, until_dt
        self.day = since_dt.date()
        since_iso = since_dt.isoformat()
        until_iso = until_dt.isoformat()

        cur = self.conn.execute(
            "INSERT INTO runs(started_at, since, until, status) VALUES(?,?,?,?)",
            (utcnow_iso(), since_iso, until_iso, "running"),
        )
        self.conn.commit()
        run_id = cur.lastrowid
        log.info("Window [%s, %s) run_id=%s", since_iso, until_iso, run_id)
        return {
            "run_id": run_id,
            "repo": self.config.github.repo,
            "since": since_iso,
            "until": until_iso,
            "candidate_refs": [],
            "synced_object_refs": [],
            "event_ids": [],
            "output_paths": [],
        }

    def fetch_candidates(self, state: DigestState) -> DigestState:
        candidates = self.gh.search_candidates(self.since_dt, self.until_dt)
        log.info("Found %d candidate(s)", len(candidates))
        return {"candidate_refs": candidates}

    def sync_objects(self, state: DigestState) -> DigestState:
        synced: list[dict] = []
        event_ids = list(state.get("event_ids", []))
        with transaction(self.conn):
            for ref in state.get("candidate_refs", []):
                kind, number = ref["kind"], ref["number"]
                obj = self.gh.fetch_object(kind, number)
                self.object_cache[(kind, number)] = obj
                meta = crawler_mod.upsert_object(self.conn, obj)
                if meta["is_new_local"]:
                    self.newly_seen.add((kind, number))

                if kind == "pr" and crawler_mod.head_changed(meta):
                    event_ids += self._emit_head_update(obj, meta)

                synced.append(
                    {"repo": ref["repo"], "kind": kind, "number": number, "url": ref.get("url")}
                )
        log.info("Synced %d object(s); %d newly seen", len(synced), len(self.newly_seen))
        return {"synced_object_refs": synced, "event_ids": event_ids}

    def _emit_head_update(self, obj: dict, meta: dict) -> list[int]:
        old_sha, new_sha = meta["old_head_sha"], meta["new_head_sha"]
        compare = self.gh.compare(old_sha, new_sha)
        diff_summary = self.llm.summarize_head_diff(old_sha, new_sha, compare)
        event = crawler_mod.build_head_update_event(obj, old_sha, new_sha, compare, diff_summary)
        return events_mod.insert_events(self.conn, [event])

    def fetch_activity(self, state: DigestState) -> DigestState:
        event_ids = list(state.get("event_ids", []))
        with transaction(self.conn):
            for (kind, number), obj in self.object_cache.items():
                normalized = events_mod.normalize_object_events(
                    obj, self.since_dt, self.until_dt
                )
                event_ids += events_mod.insert_events(self.conn, normalized)
        log.info("Inserted %d activity event(s) this run", len(event_ids))
        return {"event_ids": event_ids}

    def bootstrap_object_summaries(self, state: DigestState) -> DigestState:
        with transaction(self.conn):
            for (kind, number) in sorted(self.newly_seen):
                obj = self.object_cache.get((kind, number))
                if obj is None:
                    continue
                summary = self.llm.initial_object_summary(obj)
                self._save_summary(obj["repo"], kind, number, summary, last_event_id=None)
        return {}

    def process_events(self, state: DigestState) -> DigestState:
        rows = self.conn.execute(
            "SELECT * FROM events WHERE processed = 0 ORDER BY created_at, id"
        ).fetchall()
        with transaction(self.conn):
            for ev in rows:
                ev = dict(ev)
                obj = self._object_snapshot(ev["repo"], ev["object_kind"], ev["object_number"])
                ms = self._merge_status_for(ev["object_kind"], ev["object_number"])
                if ms is not None:
                    obj = {**obj, "merge_status": ms}
                prev = self._load_summary(ev["repo"], ev["object_kind"], ev["object_number"])
                updated = self.llm.update_object_summary(prev or "", ev, obj)
                self._save_summary(
                    ev["repo"],
                    ev["object_kind"],
                    ev["object_number"],
                    updated,
                    last_event_id=ev["id"],
                )
                self.conn.execute("UPDATE events SET processed = 1 WHERE id = ?", (ev["id"],))
        log.info("Processed %d event(s)", len(rows))
        return {}

    def build_daily_view_model(self, state: DigestState) -> DigestState:
        payload = self._view_model_payload(state)
        vm = self.llm.daily_view_model(payload)
        self._fill_links(vm)
        self.view_model = vm
        return {}

    def render_static_site(self, state: DigestState) -> DigestState:
        from .renderer import render_day

        paths = render_day(self.conn, self.config, self.view_model, self.day, state["run_id"])
        return {"output_paths": [str(p) for p in paths]}

    def finish_run(self, state: DigestState) -> DigestState:
        with transaction(self.conn):
            self.conn.execute(
                "UPDATE runs SET finished_at = ?, status = ? WHERE id = ?",
                (utcnow_iso(), "success", state["run_id"]),
            )
            # Only the automatic daily run advances the cursor; explicit
            # date/range/crawl runs leave it untouched so backfills don't move it.
            if self.advance_cursor:
                set_state(self.conn, STATE_LAST_UNTIL, state["until"])
        if self.advance_cursor:
            log.info("Run %s succeeded; cursor advanced to %s", state["run_id"], state["until"])
        else:
            log.info("Run %s succeeded (explicit window; cursor unchanged)", state["run_id"])
        return {}

    def fail_run(self, state: DigestState) -> DigestState:
        error = "; ".join(state.get("errors", [])) or "unknown error"
        run_id = state.get("run_id")
        if run_id is not None:
            with transaction(self.conn):
                self.conn.execute(
                    "UPDATE runs SET finished_at = ?, status = ?, error = ? WHERE id = ?",
                    (utcnow_iso(), "failed", error, run_id),
                )
        log.error("Run failed (cursor NOT advanced): %s", error)
        return {}

    # --- helpers -----------------------------------------------------------
    def _object_snapshot(self, repo: str, kind: str, number: int) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM objects WHERE repo=? AND kind=? AND number=?",
            (repo, kind, number),
        ).fetchone()
        return dict(row) if row else {"repo": repo, "kind": kind, "number": number}

    def _load_summary(self, repo: str, kind: str, number: int) -> str | None:
        row = self.conn.execute(
            "SELECT summary FROM object_summaries WHERE repo=? AND object_kind=? AND object_number=?",
            (repo, kind, number),
        ).fetchone()
        return row["summary"] if row else None

    def _save_summary(
        self, repo: str, kind: str, number: int, summary: str, last_event_id: int | None
    ) -> None:
        input_hash = hashlib.sha256(summary.encode("utf-8")).hexdigest()[:16]
        self.conn.execute(
            """
            INSERT INTO object_summaries
                (repo, object_kind, object_number, summary, updated_at, last_event_id, input_hash)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(repo, object_kind, object_number) DO UPDATE SET
                summary=excluded.summary,
                updated_at=excluded.updated_at,
                last_event_id=excluded.last_event_id,
                input_hash=excluded.input_hash
            """,
            (repo, kind, number, summary, utcnow_iso(), last_event_id, input_hash),
        )

    def _view_model_payload(self, state: DigestState) -> dict[str, Any]:
        since_iso, until_iso = state["since"], state["until"]
        rows = self.conn.execute(
            "SELECT * FROM events WHERE repo=? AND created_at >= ? AND created_at < ? "
            "ORDER BY created_at, id",
            (state["repo"], since_iso, until_iso),
        ).fetchall()
        # Include synthetic head-update events even if their created_at sits at run time.
        head_rows = self.conn.execute(
            "SELECT * FROM events WHERE repo=? AND event_type=? AND seen_at >= ?",
            (state["repo"], events_mod.PR_HEAD_UPDATED, state["since"]),
        ).fetchall()

        by_key: dict[tuple[str, int], dict[str, Any]] = {}
        for ev in [dict(r) for r in rows] + [dict(r) for r in head_rows]:
            key = (ev["object_kind"], ev["object_number"])
            entry = by_key.setdefault(
                key,
                {
                    "kind": ev["object_kind"],
                    "number": ev["object_number"],
                    "events": [],
                },
            )
            entry["events"].append(
                {
                    "type": ev["event_type"],
                    "actor": ev["actor"],
                    "created_at": ev["created_at"],
                    "body": (ev.get("body") or "")[:500],
                }
            )

        items = []
        for (kind, number), entry in by_key.items():
            obj = self._object_snapshot(state["repo"], kind, number)
            summary = self._load_summary(state["repo"], kind, number) or ""
            ms = self._merge_status_for(kind, number)
            items.append(
                {
                    "kind": kind,
                    "number": number,
                    "title": obj.get("title"),
                    "state": obj.get("state"),
                    "merged": obj.get("merged"),
                    "summary": summary,
                    "events": entry["events"],
                    "merge_status": ms,
                    "review_status_text": review_mod.describe(ms),
                }
            )

        return {
            "date": self.day.isoformat() if self.day else since_iso[:10],
            "repo": state["repo"],
            "window": {"since": since_iso, "until": until_iso},
            "items": items,
        }

    def _merge_status_for(self, kind: str, number: int) -> dict[str, Any] | None:
        """Compute PR merge/review status from the full object fetched this run."""
        obj = self.object_cache.get((kind, number))
        return review_mod.merge_status(obj) if obj else None

    def _fill_links(self, vm) -> None:
        """Deterministically finalize each item: links plus review/merge facts.

        Links and merge-readiness badges/status are set from authoritative data,
        never trusted from the LLM, so the 'push' label, approval count and bot
        handling are always reported correctly.
        """
        for section in vm.sections:
            for item in section.items:
                item.local_path = f"{item.kind}/{item.number}/"
                obj = self._object_snapshot(vm.repo, item.kind, item.number)
                if obj.get("url"):
                    item.url = obj["url"]

                # The full rolling summary (discussion + code context) is the expanded
                # text for the per-object page; the day page keeps the concise summary.
                rolling = self._load_summary(vm.repo, item.kind, item.number)
                if rolling:
                    item.detail = rolling
                    if not item.summary.strip():
                        item.summary = _concise(rolling)

                ms = self._merge_status_for(item.kind, item.number)
                if ms is None:
                    continue
                # Merge deterministic badges in without dropping the LLM's own.
                for badge in review_mod.badges(ms):
                    if badge not in item.badges:
                        item.badges.append(badge)
                # Always state the authoritative merge/review status.
                item.status = review_mod.describe(ms)


# --- graph construction ------------------------------------------------------

_WORK_ORDER = [
    "load_window",
    "fetch_candidates",
    "sync_objects",
    "fetch_activity",
    "bootstrap_object_summaries",
    "process_events",
    "build_daily_view_model",
    "render_static_site",
]


def _wrap(name: str, fn: Callable[[DigestState], DigestState]) -> Callable[[DigestState], DigestState]:
    def node(state: DigestState) -> DigestState:
        try:
            return fn(state)
        except Exception as exc:  # noqa: BLE001 - convert to routed failure
            log.exception("Node %s failed", name)
            return {"errors": list(state.get("errors", [])) + [f"{name}: {exc}"]}

    return node


def build_graph(pipeline: Pipeline):
    from langgraph.graph import END, StateGraph

    g = StateGraph(DigestState)
    methods = {
        "load_window": pipeline.load_window,
        "fetch_candidates": pipeline.fetch_candidates,
        "sync_objects": pipeline.sync_objects,
        "fetch_activity": pipeline.fetch_activity,
        "bootstrap_object_summaries": pipeline.bootstrap_object_summaries,
        "process_events": pipeline.process_events,
        "build_daily_view_model": pipeline.build_daily_view_model,
        "render_static_site": pipeline.render_static_site,
        "finish_run": pipeline.finish_run,
        "fail_run": pipeline.fail_run,
    }
    for name, fn in methods.items():
        g.add_node(name, _wrap(name, fn))

    g.set_entry_point("load_window")

    def make_router(next_node: str):
        def router(state: DigestState) -> str:
            return "fail_run" if state.get("errors") else next_node

        return router

    for i, name in enumerate(_WORK_ORDER):
        next_node = _WORK_ORDER[i + 1] if i + 1 < len(_WORK_ORDER) else "finish_run"
        g.add_conditional_edges(
            name, make_router(next_node), {"fail_run": "fail_run", next_node: next_node}
        )

    g.add_edge("finish_run", END)
    g.add_edge("fail_run", END)
    return g.compile()


def run_pipeline(
    config: Config,
    since: str | None = None,
    until: str | None = None,
    advance_cursor: bool = True,
    gh: GithubClient | None = None,
    summarizer: Summarizer | None = None,
) -> DigestState:
    """Run the full daily pipeline. Returns the final graph state.

    advance_cursor=False leaves github_last_successful_until untouched (used for
    explicit single-date, range and crawl runs so they don't move the scheduler).
    """
    from . import net

    net.apply_env_proxy(config)
    conn = connect(config.storage.db_path)
    try:
        gh = gh or GithubClient(config.github.token, config.github.repo)
        summarizer = summarizer or make_summarizer(config)
        pipeline = Pipeline(
            config, conn, gh, summarizer, since=since, until=until, advance_cursor=advance_cursor
        )
        graph = build_graph(pipeline)
        final: DigestState = graph.invoke({"repo": config.github.repo, "errors": []})
        return final
    finally:
        conn.close()
