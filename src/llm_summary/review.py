"""PR review aggregation, bot detection, and merge-readiness signals.

edk2 merges via Mergify once a maintainer applies the ``push`` label, so merge
readiness is driven by that label plus maintainer (non-bot) approvals. Automation
accounts such as ``mergify[bot]`` must not be counted as human reviewers.
"""

from __future__ import annotations

from typing import Any

# edk2 is merged by Mergify when a maintainer applies this label.
PUSH_LABEL = "push"

# Logins (lowercased) known to be automation in the edk2 workflow. The generic
# "[bot]" suffix / GitHub user type checks cover most cases; this catches the rest.
_KNOWN_BOTS = {
    "mergify",
    "mergify[bot]",
    "dependabot",
    "dependabot[bot]",
    "github-actions",
    "github-actions[bot]",
    "tianocore-assign",
    "tianocore-assign[bot]",
}

# Review states that express a decision (COMMENTED / PENDING are ignored).
_DECISIVE = {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}


def is_bot(login: str | None, user_type: str | None = None) -> bool:
    """Heuristically decide whether an actor is automation rather than a person."""
    if user_type and str(user_type).lower() == "bot":
        return True
    if not login:
        return False
    low = login.lower()
    return low.endswith("[bot]") or low in _KNOWN_BOTS


def _latest_decisive_reviews(reviews: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Keep only each reviewer's most recent decisive review."""
    latest: dict[str, dict[str, Any]] = {}
    for r in reviews:
        state = (r.get("state") or "").upper()
        if state not in _DECISIVE:
            continue
        login = r.get("user") or ""
        ts = r.get("created_at") or ""
        cur = latest.get(login)
        if cur is None or ts >= cur["created_at"]:
            latest[login] = {
                "created_at": ts,
                "state": state,
                "is_bot": is_bot(login, r.get("user_type")),
            }
    return latest


def merge_status(obj: dict[str, Any]) -> dict[str, Any] | None:
    """Compute merge-readiness signals for a PR (None for issues)."""
    if obj.get("kind") != "pr":
        return None

    labels = {str(label).lower() for label in obj.get("labels", [])}
    has_push = PUSH_LABEL in labels
    latest = _latest_decisive_reviews(obj.get("reviews", []))

    approvals = sorted(u for u, v in latest.items() if v["state"] == "APPROVED" and not v["is_bot"])
    bot_approvals = sorted(u for u, v in latest.items() if v["state"] == "APPROVED" and v["is_bot"])
    changes_requested = sorted(
        u for u, v in latest.items() if v["state"] == "CHANGES_REQUESTED" and not v["is_bot"]
    )
    merged = bool(obj.get("merged"))
    state = obj.get("state")
    ready = bool(has_push and not merged and state != "closed")

    return {
        "approvals": approvals,
        "approval_count": len(approvals),
        "bot_approvals": bot_approvals,
        "changes_requested": changes_requested,
        "has_push_label": has_push,
        "ready_to_merge": ready,
        "merged": merged,
    }


def describe(ms: dict[str, Any] | None) -> str:
    """One-line human phrasing of the merge status."""
    if not ms:
        return ""
    if ms["merged"]:
        return "Merged."

    parts: list[str] = []
    if ms["approval_count"]:
        plural = "s" if ms["approval_count"] != 1 else ""
        parts.append(f"{ms['approval_count']} maintainer approval{plural} ({', '.join(ms['approvals'])})")
    else:
        parts.append("no maintainer approvals yet")

    if ms["changes_requested"]:
        parts.append(f"changes requested by {', '.join(ms['changes_requested'])}")
    if ms["bot_approvals"]:
        parts.append(f"automated review from {', '.join(ms['bot_approvals'])} (bot, not counted)")

    if ms["has_push_label"]:
        parts.append("carries the 'push' label and is queued for merge")
    else:
        parts.append("still needs the 'push' label (maintainer sign-off) before it can merge")

    return "; ".join(parts) + "."


def badges(ms: dict[str, Any] | None) -> list[str]:
    """Short status badges derived from the merge status."""
    if not ms:
        return []
    if ms["merged"]:
        return ["merged"]
    out: list[str] = []
    if ms["has_push_label"]:
        out += ["push", "ready to merge"]
    if ms["changes_requested"]:
        out.append("changes requested")
    elif ms["approval_count"]:
        out.append("approved")
    elif not ms["has_push_label"]:
        out.append("needs review")
    return out
