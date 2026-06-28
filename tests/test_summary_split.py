from pathlib import Path

from llm_summary.graph import _concise, run_pipeline

from conftest import FakeGithubClient, FakeSummarizer, make_pr


def test_concise_takes_first_sentences():
    assert _concise("One. Two. Three.") == "One. Two."
    assert _concise("Single sentence only") == "Single sentence only"
    assert _concise("") == ""


def test_concise_respects_limit():
    long = "word " * 200
    out = _concise(long, max_sentences=2, limit=50)
    assert len(out) <= 51 and out.endswith("…")


def test_day_is_concise_object_is_expanded(config, since_until):
    since, until = since_until
    gh = FakeGithubClient(config.github.repo, {("pr", 1234): make_pr(1234)})
    run_pipeline(config, since=since, until=until, gh=gh, summarizer=FakeSummarizer())

    site = Path(config.storage.site_dir)
    day = (site / "2026" / "06" / "27" / "index.html").read_text()
    obj = (site / "2026" / "06" / "27" / "pr" / "1234" / "index.html").read_text()

    # Day page shows the concise summary, not the full rolling text.
    assert "MAINPOINT 1234" in day
    assert "Initial summary for pr #1234" not in day

    # Object page shows the expanded rolling summary.
    assert "Initial summary for pr #1234" in obj
