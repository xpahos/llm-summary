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


def _extract_json(content: str) -> str:
    """Strip markdown code fences and isolate the JSON object."""
    s = content.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s
        if s.endswith("```"):
            s = s[: -3]
        if s.lstrip().startswith("json"):
            s = s.lstrip()[4:]
    start, end = s.find("{"), s.rfind("}")
    if start != -1 and end != -1 and end > start:
        return s[start : end + 1]
    return s


_SYSTEM = (
    "You are a precise technical summarizer for the EDK II firmware project. "
    "Write compact, factual engineering summaries. No marketing language, no HTML, "
    "no markdown headings. Focus on what changed, why it matters, affected packages, "
    "review state, blockers and compatibility risks."
)


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
        guidance = (
            "Summarize this PR/issue as initial context (4-7 sentences) covering BOTH the code "
            "and the discussion. For a PR: what the change does and why (read the commit "
            "messages and the diff stats in files, not just the title/body), which packages/"
            "files it touches, and then summarize the conversation — the substantive points "
            "reviewers and the author raised in comments and reviews (objections, requested "
            "changes, decisions, and the reason it was ultimately merged or closed if known). "
            "For an issue: problem, affected area, symptoms/repro, current hypothesis, and the "
            "key points from the discussion. Do not ignore the comments: if the comments list "
            "is non-empty, the summary must reflect what was discussed.\n"
            "If a PR has reviews, do not merely say one user approved: state how many "
            "maintainer approvals exist (use merge_status.approval_count / approvals), whether "
            "further approval is still needed, whether the 'push' label is present "
            "(merge_status.has_push_label) and therefore whether it is ready to merge or "
            "queued (merge_status.ready_to_merge). edk2 only merges PRs that carry the 'push' "
            "label. Treat actors with is_bot=true (e.g. mergify[bot]) as automation, not "
            "human reviewers/commenters."
        )
        return self._chat(f"{guidance}\n\nDATA:\n{json.dumps(payload, default=str)}").strip()

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
        guidance = (
            "In 1-3 sentences, summarize what changed between the old and new PR head. "
            "Focus on what the author fixed, whether tests were updated, whether review "
            "feedback appears addressed, and which packages were touched. Use neutral "
            "wording (the head was updated); do not speculate about force-push vs rebase."
        )
        return self._chat(f"{guidance}\n\nDATA:\n{json.dumps(payload, default=str)}").strip()

    # --- task 3 ------------------------------------------------------------
    def update_object_summary(self, prev: str, event: dict[str, Any], obj: dict[str, Any]) -> str:
        payload = {
            "previous_summary": prev,
            "new_event": {
                "type": event.get("event_type"),
                "actor": event.get("actor"),
                "created_at": event.get("created_at"),
                "body": _trim(event.get("body"), 1500),
                "payload": _maybe_json(event.get("payload_json")),
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
        guidance = (
            "Update the rolling summary given the new event. Keep it compact and technical "
            "(4-7 sentences) and return only the updated summary text.\n"
            "IMPORTANT: do not discard the existing context. The previous_summary already "
            "captures what the change does and the prior discussion (comments, reviews, "
            "concerns, decisions) — preserve that and fold the new event into it, only dropping "
            "details that the event makes obsolete. A 'closed' or 'merged' event must NOT "
            "replace the summary with just 'the PR was closed/merged': keep the description of "
            "the code change, reflect the files/packages affected (object.changed_files / "
            "commits), keep the discussion, and add the outcome (and the reason, if "
            "discernible). For a 'commented' / 'reviewed' / 'review_comment' event, incorporate "
            "the substance of what was said. For 'pr_head_updated', fold in the diff_summary.\n"
            "If the event is a review or the object has merge_status, reflect the current "
            "review/merge state: number of maintainer approvals, whether more are needed, "
            "whether the 'push' label is present and the PR is ready to merge. The new_event "
            "payload may include is_bot=true (e.g. mergify[bot]); describe such actors as "
            "automation, not as a maintainer approving the PR."
        )
        return self._chat(f"{guidance}\n\nDATA:\n{json.dumps(payload, default=str)}").strip()

    # --- task 4 ------------------------------------------------------------
    def daily_view_model(self, payload: dict[str, Any]) -> DayViewModel:
        guidance = (
            "Produce a daily digest view model as STRICT JSON (no prose, no markdown). "
            "Schema: {date, repo, stats:{prs_updated,issues_active,merged,"
            "needs_attention}, highlights:[string], sections:[{id,title,items:[{kind,number,"
            "title,badges:[string],summary,activity:[string],status,changed_files:[string]}]}]}. "
            "The day overview is conveyed by stats + highlights; do not emit a separate headline. "
            "Group items into sections such as 'attention' (Needs attention), 'merged', "
            "'new' (New PRs/issues), 'updated', 'issues'. The 'summary' field is shown on the "
            "day overview, so make it CONCISE — one or two sentences capturing only the main "
            "point(s); the full expanded summary is rendered on the item's own page. Keep "
            "activity bullets terse. Omit url/local_path; they are filled in later.\n"
            "Each PR item includes merge_status and review_status_text. When describing review "
            "activity, do not just say someone approved: reflect the approval count, whether "
            "more approvals are needed, whether the 'push' label is present and the PR is ready "
            "to merge (edk2 merges only with the 'push' label). Ignore bot approvals "
            "(merge_status.bot_approvals, e.g. mergify[bot]) as human sign-off. A PR lacking "
            "the 'push' label or with changes requested is a good 'attention' candidate. "
            "(Authoritative badges and status are also applied automatically afterwards.)"
        )
        raw = self._chat(f"{guidance}\n\nDATA:\n{json.dumps(payload, default=str)}")
        return _parse_view_model(raw, payload)


def _maybe_json(value: str | None) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return value


def _parse_view_model(raw: str, payload: dict[str, Any]) -> DayViewModel:
    try:
        data = json.loads(_extract_json(raw))
        return DayViewModel.model_validate(data)
    except (json.JSONDecodeError, ValidationError) as exc:
        log.error("Failed to parse daily view model JSON: %s", exc)
        raise ValueError("LLM returned invalid daily view model JSON") from exc


def make_summarizer(config: Config) -> Summarizer:
    return OpenAISummarizer(config)
