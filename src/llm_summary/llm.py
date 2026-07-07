"""LLM summarization tasks via langchain-openai.

The LLM is used ONLY to summarize structured input. It never fetches data, calls
tools, or emits HTML. Task 4 (daily view model) returns strict JSON validated
against pydantic models; links are filled deterministically by the caller, not
trusted from the model.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Protocol

from pydantic import BaseModel, Field, ValidationError

from . import review
from .config import Config

log = logging.getLogger("llm_summary.llm")

_MAX_BODY = 4000
_MAX_ITEMS = 40


# --- view model schema ------------------------------------------------------

class ViewItem(BaseModel):
    kind: str
    number: int
    title: str = ""
    url: str = ""
    local_path: str = ""
    badges: list[str] = Field(default_factory=list)
    summary: str = ""           # concise, shown on the day page
    detail: str = ""            # expanded summary, shown on the per-object page
    activity: list[str] = Field(default_factory=list)
    status: str = ""
    changed_files: list[str] = Field(default_factory=list)


class ViewSection(BaseModel):
    id: str
    title: str
    items: list[ViewItem] = Field(default_factory=list)


class ViewStats(BaseModel):
    prs_updated: int = 0
    issues_active: int = 0
    merged: int = 0
    needs_attention: int = 0


class DayViewModel(BaseModel):
    date: str
    repo: str
    headline: str = ""
    stats: ViewStats = Field(default_factory=ViewStats)
    highlights: list[str] = Field(default_factory=list)
    sections: list[ViewSection] = Field(default_factory=list)


class Summarizer(Protocol):
    """Interface the pipeline depends on (so tests can substitute a fake)."""

    def initial_object_summary(self, obj: dict[str, Any]) -> str: ...
    def summarize_head_diff(self, old_sha: str, new_sha: str, compare: dict[str, Any]) -> str: ...
    def update_object_summary(self, prev: str, event: dict[str, Any], obj: dict[str, Any]) -> str: ...
    def daily_view_model(self, payload: dict[str, Any]) -> DayViewModel: ...


def _trim(text: str | None, limit: int = _MAX_BODY) -> str:
    if not text:
        return ""
    return text if len(text) <= limit else text[:limit] + " […]"


def _loads_first_json(content: str) -> Any:
    """Parse the first complete JSON object from content.

    Strips markdown code fences and any leading/trailing prose, then uses
    raw_decode so that content *after* the JSON object (extra prose, a second
    object, a trailing note) is ignored rather than raising 'Extra data'.
    """
    s = content.strip()
    if s.startswith("```"):
        s = s[3:]
        if s[:4].lower() == "json":
            s = s[4:]
        s = s.strip()
        if s.endswith("```"):
            s = s[:-3].strip()
    start = s.find("{")
    if start == -1:
        raise ValueError("no JSON object found in model output")
    obj, _end = json.JSONDecoder().raw_decode(s[start:])
    return obj


# Structured system prompt: stable role + house style + domain rules shared by every
# task. Task-specific objectives and output formats live in the per-task user message
# (see _compose), so these rules are stated once here instead of repeated per task.
_SYSTEM = (
    "<role>\n"
    "You are a precise technical summarizer for the EDK II (TianoCore edk2) firmware "
    "project. You write for engineers reading a quiet daily patch-review archive.\n"
    "</role>\n\n"
    "<style>\n"
    "- Factual and compact; no marketing language, praise, or filler.\n"
    "- Plain text only: no Markdown, no HTML, no headings, no bullet syntax unless asked.\n"
    "- Be concrete: name packages, file paths, reviewer logins, and short SHAs.\n"
    "- Never invent facts that are not present in the input.\n"
    "</style>\n\n"
    "<domain>\n"
    "- Focus on what changed, why it matters, affected packages, review state, blockers, "
    "and compatibility risks.\n"
    "- A PR is merged by Mergify only after a maintainer applies the 'push' label; without "
    "it the PR is not ready to merge, regardless of how many comments it has.\n"
    "- Treat automation accounts (is_bot=true, e.g. mergify[bot]) as automation — never as "
    "human reviewers or approvers.\n"
    "</domain>"
)


def _compose(task: str, payload: dict[str, Any], output: str | None = None) -> str:
    """Assemble a structured user message: <task>, <input>, optional <output>."""
    parts = [
        f"<task>\n{task.strip()}\n</task>",
        f"<input>\n{json.dumps(payload, default=str)}\n</input>",
    ]
    if output:
        parts.append(f"<output>\n{output.strip()}\n</output>")
    return "\n\n".join(parts)


class OpenAISummarizer:
    """Concrete Summarizer backed by langchain-openai's ChatOpenAI."""

    def __init__(self, config: Config):
        self._config = config.llm
        self._full_config = config
        self._client = None

    @property
    def client(self):
        if self._client is None:
            from langchain_openai import ChatOpenAI

            from . import net

            kwargs: dict[str, Any] = {
                "model": self._config.model,
                "temperature": self._config.temperature,
            }
            if self._config.api_key:
                kwargs["api_key"] = self._config.api_key
            if self._config.base_url:
                kwargs["base_url"] = self._config.base_url
            http_client = net.build_httpx_client(self._full_config)
            if http_client is not None:
                kwargs["http_client"] = http_client
                kwargs["http_async_client"] = net.build_httpx_async_client(self._full_config)
            self._client = ChatOpenAI(**kwargs)
        return self._client

    def _chat(self, user: str, system: str = _SYSTEM) -> str:
        resp = self.client.invoke([("system", system), ("human", user)])
        content = resp.content
        if isinstance(content, list):  # some providers return content parts
            content = "".join(part.get("text", "") if isinstance(part, dict) else str(part) for part in content)
        return content or ""

    # --- task 1 ------------------------------------------------------------
    def initial_object_summary(self, obj: dict[str, Any]) -> str:
        payload = {
            "kind": obj["kind"],
            "number": obj["number"],
            "title": obj.get("title"),
            "state": obj.get("state"),
            "author": obj.get("author"),
            "labels": obj.get("labels", []),
            "body": _trim(obj.get("body")),
            "base_ref": obj.get("base_ref"),
            "head_ref": obj.get("head_ref"),
            "files": [
                {
                    "filename": f.get("filename"),
                    "status": f.get("status"),
                    "additions": f.get("additions"),
                    "deletions": f.get("deletions"),
                }
                for f in obj.get("files", [])
            ][:80],
            "commits": [
                {
                    "author": c.get("author"),
                    "message": (c.get("message") or "").split("\n", 1)[0],
                }
                for c in obj.get("commits", [])
            ][:50],
            "comments": [
                {
                    "user": c.get("user"),
                    "is_bot": review.is_bot(c.get("user"), c.get("user_type")),
                    "body": _trim(c.get("body"), 800),
                }
                for c in obj.get("comments", [])
            ][:40],
            "reviews": [
                {
                    "user": r.get("user"),
                    "state": r.get("state"),
                    "is_bot": review.is_bot(r.get("user"), r.get("user_type")),
                    "body": _trim(r.get("body"), 600),
                }
                for r in obj.get("review_comments", []) + obj.get("reviews", [])
            ][:40],
            "merge_status": review.merge_status(obj),
        }
        task = (
            "Write the initial rolling summary for this PR/issue as context, covering BOTH the "
            "code and the discussion. For a PR: what the change does and why (read the commit "
            "messages and the per-file diff stats, not just the title/body), and which packages/"
            "files it touches; then summarize the conversation — the substantive points "
            "reviewers and the author raised (objections, requested changes, decisions, and the "
            "reason it was merged or closed if known). For an issue: problem, affected area, "
            "symptoms/repro, current hypothesis, and the key discussion points. If the comments "
            "list is non-empty, the summary MUST reflect what was discussed. State the review/"
            "merge status from merge_status: how many maintainer approvals (approval_count/"
            "approvals), whether more are needed, whether the 'push' label is present "
            "(has_push_label) and thus whether it is ready/queued to merge (ready_to_merge)."
        )
        output = "A single plain-text paragraph of 4-7 sentences. No headings or lists."
        return self._chat(_compose(task, payload, output)).strip()

    # --- task 2 ------------------------------------------------------------
    def summarize_head_diff(self, old_sha: str, new_sha: str, compare: dict[str, Any]) -> str:
        payload = {
            "old_head_sha": old_sha,
            "new_head_sha": new_sha,
            "commits": [
                {"sha": c.get("sha", "")[:8], "message": _trim(c.get("message"), 300)}
                for c in compare.get("commits", [])
            ][:40],
            "files": [
                {
                    "filename": f.get("filename"),
                    "status": f.get("status"),
                    "additions": f.get("additions"),
                    "deletions": f.get("deletions"),
                }
                for f in compare.get("files", [])
            ][:60],
        }
        task = (
            "Summarize what changed between the PR's old and new head: what the author fixed, "
            "whether tests were updated, whether review feedback appears addressed, and which "
            "packages were touched. Use neutral wording (the head was updated); do not "
            "speculate about force-push vs rebase."
        )
        output = "1-3 plain-text sentences."
        return self._chat(_compose(task, payload, output)).strip()

    # --- task 3 ------------------------------------------------------------
    def update_object_summary(self, prev: str, event: dict[str, Any], obj: dict[str, Any]) -> str:
        payload = {
            "previous_summary": prev,
            "new_event": {
                "type": event.get("event_type"),
                "actor": event.get("actor"),
                "created_at": event.get("created_at"),
                "body": _trim(event.get("body"), 1500),
                "payload": _event_payload_for_prompt(
                    event.get("event_type"), _maybe_json(event.get("payload_json"))
                ),
            },
            "object": {
                "kind": obj.get("kind"),
                "number": obj.get("number"),
                "title": obj.get("title"),
                "state": obj.get("state"),
                "merged": obj.get("merged"),
                "merge_status": obj.get("merge_status"),
                "changed_files": [
                    f.get("filename") for f in obj.get("files", []) if f.get("filename")
                ][:80],
                "commits": [
                    (c.get("message") or "").split("\n", 1)[0] for c in obj.get("commits", [])
                ][:50],
            },
        }
        task = (
            "Update the rolling summary by folding in the new event. Do NOT discard existing "
            "context: previous_summary already captures the code change and the prior "
            "discussion (comments, reviews, concerns, decisions) — preserve it and only drop "
            "details the event makes obsolete. A 'closed' or 'merged' event must NOT reduce the "
            "summary to 'the PR was closed/merged': keep the code-change description, reflect "
            "the affected files/packages (object.changed_files / commits), keep the discussion, "
            "and add the outcome (and the reason, if discernible). For 'commented' / 'reviewed' "
            "/ 'review_comment', incorporate the substance of what was said. For "
            "'pr_head_updated', fold in the diff_summary. Reflect the current review/merge state "
            "(approvals, whether more are needed, 'push' label / ready-to-merge)."
        )
        output = "A single plain-text paragraph of 4-7 sentences. Return only the updated summary."
        return self._chat(_compose(task, payload, output)).strip()

    # --- task 4 ------------------------------------------------------------
    def daily_view_model(self, payload: dict[str, Any]) -> DayViewModel:
        task = (
            "Produce a daily digest view model for the archive. Group items into sections such "
            "as 'attention' (needs attention), 'merged', 'new' (new PRs/issues), 'updated', "
            "'issues'. The day overview is conveyed by stats + highlights; do NOT emit a "
            "headline. Each item's 'summary' is shown on the day overview, so keep it CONCISE — "
            "one or two sentences, main point(s) only; the expanded summary is rendered on the "
            "item's own page. Keep activity bullets terse. When describing review activity, "
            "reflect the approval count, whether more approvals are needed, and whether the "
            "'push' label is present / ready to merge; ignore bot approvals "
            "(merge_status.bot_approvals) as human sign-off. A PR lacking 'push' or with changes "
            "requested is a good 'attention' candidate. (Links, badges, status, changed_files "
            "and section/item ordering are applied deterministically afterwards, so omit "
            "url/local_path.)"
        )
        output = (
            "STRICT JSON only — no prose, no Markdown, no code fences. Schema: "
            "{date, repo, stats:{prs_updated,issues_active,merged,needs_attention}, "
            "highlights:[string], sections:[{id,title,items:[{kind,number,title,"
            "badges:[string],summary,activity:[string],status,changed_files:[string]}]}]}"
        )
        raw = self._chat(_compose(task, payload, output))
        return _parse_view_model(raw, payload)


def _maybe_json(value: str | None) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return value


def _event_payload_for_prompt(event_type: str | None, payload: Any) -> Any:
    """Compact an event payload before sending it to the LLM.

    pr_head_updated payloads embed the full compare data (commits + files + raw
    patches) and can reach >1 MB, which blows the model's context window. The diff
    was already summarized into diff_summary at creation time, so we send that plus
    light metadata — never the raw commits/files/patches.
    """
    if not isinstance(payload, dict):
        return payload
    if event_type == "pr_head_updated":
        files = payload.get("files") or []
        commits = payload.get("commits") or []
        return {
            "old_head_sha": payload.get("old_head_sha"),
            "new_head_sha": payload.get("new_head_sha"),
            "compare_url": payload.get("compare_url"),
            "diff_summary": payload.get("diff_summary"),
            "commit_count": len(commits),
            "file_count": len(files),
            "files": [
                f.get("filename") for f in files if isinstance(f, dict) and f.get("filename")
            ][:40],
        }
    return payload


def _parse_view_model(raw: str, payload: dict[str, Any]) -> DayViewModel:
    try:
        data = _loads_first_json(raw)
        return DayViewModel.model_validate(data)
    except (json.JSONDecodeError, ValidationError, ValueError) as exc:
        log.error("Failed to parse daily view model JSON: %s", exc)
        raise ValueError("LLM returned invalid daily view model JSON") from exc


def make_summarizer(config: Config) -> Summarizer:
    return OpenAISummarizer(config)
