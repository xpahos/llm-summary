"""GithubClient._comments must fetch PR *conversation* comments, not diff comments."""

from datetime import datetime, timezone

from llm_summary.github_client import GithubClient


class _User:
    def __init__(self, login, type="User"):
        self.login = login
        self.type = type


class _Comment:
    def __init__(self, cid, login, body):
        self.id = cid
        self.user = _User(login)
        self.created_at = datetime(2026, 6, 1, tzinfo=timezone.utc)
        self.body = body
        self.html_url = f"https://example/{cid}"


class _PR:
    """Mimics PyGithub PullRequest: get_comments() == review/diff comments."""

    def get_issue_comments(self):
        return [_Comment(1, "alice", "conversation comment")]

    def get_comments(self):
        return [_Comment(2, "bob", "diff comment")]


class _Issue:
    """Mimics PyGithub Issue: get_comments() == conversation comments."""

    def get_comments(self):
        return [_Comment(3, "carol", "issue comment")]


def test_pr_uses_issue_comments():
    gc = GithubClient("token", "owner/repo")
    comments = gc._comments(_PR())
    assert [c["body"] for c in comments] == ["conversation comment"]
    assert comments[0]["id"] == 1  # not the diff comment (id 2)


def test_issue_uses_get_comments():
    gc = GithubClient("token", "owner/repo")
    comments = gc._comments(_Issue())
    assert [c["body"] for c in comments] == ["issue comment"]


class _TimelineEvent:
    def __init__(self):
        self.id = 42
        self.event = "labeled"
        self.actor = _User("maintainer")
        self.created_at = datetime(2026, 6, 1, tzinfo=timezone.utc)
        self.label = {"name": "push"}
        self.html_url = "https://example/t42"


class _IssueView:
    """The Issue facade of a PR: this is where get_timeline() lives."""

    number = 1234

    def get_timeline(self):
        return [_TimelineEvent()]


class _PRWithIssue(_PR):
    """Mimics PyGithub PullRequest: no get_timeline(), but as_issue() works."""

    def as_issue(self):
        return _IssueView()


def test_pr_timeline_via_issue_facade():
    """PullRequest has no get_timeline(); the timeline must come from as_issue()."""
    gc = GithubClient("token", "owner/repo")
    timeline = gc._pr_timeline(_PRWithIssue(), 1234)
    assert [ev["event"] for ev in timeline] == ["labeled"]
    assert timeline[0]["label"] == "push"
