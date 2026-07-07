"""GitHub client abstraction over PyGithub.

Returns plain dicts (never PyGithub objects) so the rest of the pipeline is easy
to test with a fake client. All network access is funnelled through here.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from .events import in_window as _in_window

log = logging.getLogger("llm_summary.github")


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _safe(fn, default, what: str = ""):
    """Call a zero-arg function, returning default on any error (sub-resource fetch).

    A failure here silently degrades the fetched object (e.g. an empty review list),
    so always log what was being fetched to make the gap diagnosable.
    """
    try:
        return fn()
    except Exception as exc:  # pragma: no cover - network failure path
        log.warning("sub-resource fetch failed (%s): %s", what or "unknown", exc)
        return default


class GithubClient:
    def __init__(self, token: str, repo: str):
        self._token = token
        self.repo_name = repo
        self._gh = None
        self._repo = None

    # --- lazy PyGithub handles ---------------------------------------------
    @property
    def repo(self):
        if self._repo is None:
            from github import Auth, Github

            self._gh = Github(auth=Auth.Token(self._token)) if self._token else Github()
            self._repo = self._gh.get_repo(self.repo_name)
        return self._repo

    # --- candidate search ---------------------------------------------------
    def search_candidates(self, since: datetime, until: datetime) -> list[dict[str, Any]]:
        """Search issues+PRs updated within [since, until) (day-granular query).

        GitHub's `updated:` qualifier is day-granular, so we query the inclusive
        day range and let callers filter precisely by timestamp. The issues-search
        endpoint now requires an `is:issue` / `is:pull-request` qualifier and the
        two cannot be combined in one query, so we run one search per kind.
        """
        d_since = since.astimezone(timezone.utc).date().isoformat()
        d_until = until.astimezone(timezone.utc).date().isoformat()
        base = f"repo:{self.repo_name} updated:{d_since}..{d_until}"

        candidates: list[dict[str, Any]] = []
        seen: set[tuple[str, int]] = set()
        for kind, qualifier in (("issue", "is:issue"), ("pr", "is:pull-request")):
            query = f"{base} {qualifier}"
            log.info("GitHub search: %s", query)
            for issue in self._search(query):
                key = (kind, issue.number)
                if key in seen:
                    continue
                seen.add(key)
                candidates.append(
                    {
                        "repo": self.repo_name,
                        "kind": kind,
                        "number": issue.number,
                        "url": issue.html_url,
                        "created_at": _iso(issue.created_at),
                        "updated_at": _iso(issue.updated_at),
                    }
                )
        return candidates

    def discover_candidates(self, since: datetime, until: datetime) -> list[dict[str, Any]]:
        """Search-based candidates supplemented by repo-wide comment activity.

        The search index alone is not sufficient: inline review comments do not
        bump a PR's updated_at (so the PR never matches the `updated:` range),
        and the index itself can lag behind the live data. Listing the repo's
        review/issue comments directly catches both cases. Each candidate also
        carries the comment activity discovered for it ("activity"), so callers
        can verify those comments are already ingested before skipping a fetch.
        """
        candidates = self.search_candidates(since, until)
        by_number: dict[int, dict[str, Any]] = {c["number"]: c for c in candidates}

        for act in self._comment_activity(since, until):
            existing = by_number.get(act["number"])
            if existing is not None:
                existing.setdefault("activity", []).append(
                    {"type": act["type"], "id": act["id"]}
                )
                continue
            kind = act["kind"] or self._resolve_kind(act["number"])
            if kind is None:
                continue
            path = "pull" if kind == "pr" else "issues"
            candidate = {
                "repo": self.repo_name,
                "kind": kind,
                "number": act["number"],
                "url": f"https://github.com/{self.repo_name}/{path}/{act['number']}",
                "created_at": None,
                "updated_at": None,
                "activity": [{"type": act["type"], "id": act["id"]}],
            }
            by_number[act["number"]] = candidate
            candidates.append(candidate)
            log.info(
                "Comment activity revealed %s #%s missing from search results",
                kind, act["number"],
            )
        return candidates

    def _comment_activity(self, since: datetime, until: datetime) -> list[dict[str, Any]]:
        """Repo-wide review/issue comments created within [since, until).

        The `since` filter is server-side (on comment updated_at); creation time
        is filtered here so edits of old comments don't produce candidates.
        """
        out: list[dict[str, Any]] = []

        review_comments = _safe(
            lambda: list(self.repo.get_pulls_comments(sort="created", since=since)),
            [],
            what=f"{self.repo_name} pulls comments since {since:%Y-%m-%d}",
        )
        for c in review_comments:
            raw = c.raw_data
            created = _iso(c.created_at)
            pr_url = raw.get("pull_request_url") or ""
            if not pr_url or not _in_window(created, since, until):
                continue
            out.append(
                {
                    "number": int(pr_url.rsplit("/", 1)[1]),
                    "kind": "pr",
                    "type": "review_comment",
                    "id": c.id,
                    "created_at": created,
                }
            )

        issue_comments = _safe(
            lambda: list(self.repo.get_issues_comments(sort="created", since=since)),
            [],
            what=f"{self.repo_name} issues comments since {since:%Y-%m-%d}",
        )
        for c in issue_comments:
            raw = c.raw_data
            created = _iso(c.created_at)
            issue_url = raw.get("issue_url") or ""
            if not issue_url or not _in_window(created, since, until):
                continue
            out.append(
                {
                    "number": int(issue_url.rsplit("/", 1)[1]),
                    "kind": None,  # issue vs PR resolved lazily, only when needed
                    "type": "comment",
                    "id": c.id,
                    "created_at": created,
                }
            )
        return out

    def _resolve_kind(self, number: int) -> str | None:
        """Whether #number is a PR or an issue (the issue-comments API can't tell)."""
        issue = _safe(
            lambda: self.repo.get_issue(number), None, what=f"issue #{number} kind lookup"
        )
        if issue is None:
            return None
        return "pr" if issue.raw_data.get("pull_request") else "issue"

    def _search(self, query: str):
        return self._gh_handle().search_issues(query=query)

    def _gh_handle(self):
        # Ensure repo/_gh are initialised.
        _ = self.repo
        return self._gh

    # --- full object fetch --------------------------------------------------
    def fetch_object(self, kind: str, number: int) -> dict[str, Any]:
        if kind == "pr":
            return self._fetch_pr(number)
        return self._fetch_issue(number)

    def _common_issue_fields(self, issue) -> dict[str, Any]:
        return {
            "repo": self.repo_name,
            "number": issue.number,
            "title": issue.title or "",
            "body": issue.body or "",
            "state": issue.state,
            "author": getattr(issue.user, "login", None),
            "author_type": getattr(issue.user, "type", None),
            "url": issue.html_url,
            "created_at": _iso(issue.created_at),
            "updated_at": _iso(issue.updated_at),
            "closed_at": _iso(getattr(issue, "closed_at", None)),
            "labels": [lbl.name for lbl in _safe(lambda: list(issue.labels), [], what=f"labels #{getattr(issue, 'number', '?')}")],
        }

    def _comments(self, issue) -> list[dict[str, Any]]:
        # The conversation comments. For a PR, get_comments() returns review (diff)
        # comments — the conversation lives on get_issue_comments(); for an Issue,
        # get_comments() is the conversation and get_issue_comments() does not exist.
        getter = getattr(issue, "get_issue_comments", None) or issue.get_comments
        out = []
        for c in _safe(lambda: list(getter()), [], what=f"comments #{getattr(issue, 'number', '?')}"):
            out.append(
                {
                    "id": c.id,
                    "user": getattr(c.user, "login", None),
                    "user_type": getattr(c.user, "type", None),
                    "created_at": _iso(c.created_at),
                    "body": c.body or "",
                    "url": c.html_url,
                }
            )
        return out

    def _timeline(self, issue) -> list[dict[str, Any]]:
        out = []
        for ev in _safe(lambda: list(issue.get_timeline()), [], what=f"timeline #{getattr(issue, 'number', '?')}"):
            label = None
            raw_label = getattr(ev, "label", None)
            if isinstance(raw_label, dict):
                label = raw_label.get("name")
            elif raw_label is not None:
                label = getattr(raw_label, "name", None)
            out.append(
                {
                    "event_id": getattr(ev, "id", None) or getattr(ev, "node_id", None),
                    "event": getattr(ev, "event", None),
                    "actor": getattr(getattr(ev, "actor", None), "login", None),
                    "created_at": _iso(getattr(ev, "created_at", None)),
                    "label": label,
                    "url": getattr(ev, "html_url", None),
                }
            )
        return out

    def _fetch_issue(self, number: int) -> dict[str, Any]:
        issue = self.repo.get_issue(number)
        data = self._common_issue_fields(issue)
        data.update(
            {
                "kind": "issue",
                "head_sha": None,
                "base_ref": None,
                "head_ref": None,
                "merged": 0,
                "merged_at": None,
                "comments": self._comments(issue),
                "reviews": [],
                "review_comments": [],
                "commits": [],
                "files": [],
                "checks": [],
                "timeline": self._timeline(issue),
                "raw_json": json.dumps(_safe(lambda: issue.raw_data, {}, what=f"raw #{number}"), default=str),
            }
        )
        return data

    def _fetch_pr(self, number: int) -> dict[str, Any]:
        pr = self.repo.get_pull(number)
        data = self._common_issue_fields(pr)
        head_sha = getattr(getattr(pr, "head", None), "sha", None)
        reviews = []
        for r in _safe(lambda: list(pr.get_reviews()), [], what=f"reviews #{number}"):
            reviews.append(
                {
                    "id": r.id,
                    "user": getattr(r.user, "login", None),
                    "user_type": getattr(r.user, "type", None),
                    "state": r.state,
                    "body": r.body or "",
                    "created_at": _iso(getattr(r, "submitted_at", None)),
                    "url": getattr(r, "html_url", None),
                }
            )
        review_comments = []
        for c in _safe(lambda: list(pr.get_review_comments()), [], what=f"review comments #{number}"):
            review_comments.append(
                {
                    "id": c.id,
                    "user": getattr(c.user, "login", None),
                    "user_type": getattr(c.user, "type", None),
                    "body": c.body or "",
                    "path": getattr(c, "path", None),
                    "created_at": _iso(c.created_at),
                    "url": c.html_url,
                }
            )
        commits = []
        for cm in _safe(lambda: list(pr.get_commits()), [], what=f"commits #{number}"):
            commits.append(
                {
                    "sha": cm.sha,
                    "message": getattr(cm.commit, "message", ""),
                    "author": getattr(getattr(cm.commit, "author", None), "name", None),
                }
            )
        files = []
        for f in _safe(lambda: list(pr.get_files()), [], what=f"files #{number}"):
            files.append(
                {
                    "filename": f.filename,
                    "status": f.status,
                    "additions": f.additions,
                    "deletions": f.deletions,
                }
            )
        data.update(
            {
                "kind": "pr",
                "head_sha": head_sha,
                "base_ref": getattr(getattr(pr, "base", None), "ref", None),
                "head_ref": getattr(getattr(pr, "head", None), "ref", None),
                "merged": 1 if getattr(pr, "merged", False) else 0,
                "merged_at": _iso(getattr(pr, "merged_at", None)),
                "comments": self._comments(pr),
                "reviews": reviews,
                "review_comments": review_comments,
                "commits": commits,
                "files": files,
                "checks": self._check_runs(head_sha) if head_sha else [],
                "timeline": self._timeline(pr),
                "raw_json": json.dumps(_safe(lambda: pr.raw_data, {}, what=f"raw #{number}"), default=str),
            }
        )
        return data

    def _check_runs(self, sha: str) -> list[dict[str, Any]]:
        out = []
        commit = _safe(lambda: self.repo.get_commit(sha), None)
        if commit is None:
            return out
        for cr in _safe(lambda: list(commit.get_check_runs()), []):
            out.append(
                {
                    "id": cr.id,
                    "name": cr.name,
                    "status": cr.status,
                    "conclusion": cr.conclusion,
                    "url": getattr(cr, "html_url", None),
                }
            )
        return out

    # --- compare ------------------------------------------------------------
    def compare(self, base_sha: str, head_sha: str) -> dict[str, Any]:
        cmp = self.repo.compare(base_sha, head_sha)
        commits = []
        for cm in _safe(lambda: list(cmp.commits), []):
            commits.append(
                {
                    "sha": cm.sha,
                    "message": getattr(cm.commit, "message", ""),
                    "author": getattr(getattr(cm.commit, "author", None), "name", None),
                }
            )
        files = []
        for f in _safe(lambda: list(cmp.files), []):
            files.append(
                {
                    "filename": f.filename,
                    "status": f.status,
                    "additions": f.additions,
                    "deletions": f.deletions,
                    # Intentionally omit the raw patch: it is not used for the diff
                    # summary and bloats the stored event payload (seen >1 MB),
                    # which can blow the LLM context window when folded in later.
                }
            )
        return {
            "compare_url": getattr(cmp, "html_url", None),
            "commits": commits,
            "files": files,
        }
