from datetime import date
from pathlib import Path

from llm_summary import db as db_mod
from llm_summary.renderer import render_day
from llm_summary.llm import DayViewModel, ViewItem, ViewSection, ViewStats


def _view_model():
    return DayViewModel(
        date="2026-06-27",
        repo="tianocore/edk2",
        headline="edk2 daily",
        stats=ViewStats(prs_updated=1, issues_active=1, merged=0, needs_attention=1),
        highlights=["A highlight."],
        sections=[
            ViewSection(
                id="updated",
                title="Updated",
                items=[
                    ViewItem(
                        kind="pr",
                        number=1234,
                        title="SMBIOS handle range",
                        url="https://github.com/tianocore/edk2/pull/1234",
                        local_path="pr/1234/",
                        summary="Adds a handle range.",
                        activity=["opened", "reviewed"],
                        changed_files=["OvmfPkg/Foo.c"],
                    ),
                    ViewItem(
                        kind="issue",
                        number=999,
                        title="Boot hang",
                        local_path="issue/999/",
                        summary="Investigating.",
                    ),
                ],
            )
        ],
    )


def test_render_day_creates_expected_paths(config):
    conn = db_mod.connect(config.storage.db_path)
    db_mod.init_db(conn)
    try:
        paths = render_day(conn, config, _view_model(), date(2026, 6, 27), run_id=1)
    finally:
        conn.close()

    site = Path(config.storage.site_dir)
    expected = [
        site / "2026" / "06" / "27" / "index.html",
        site / "2026" / "06" / "27" / "pr" / "1234" / "index.html",
        site / "2026" / "06" / "27" / "issue" / "999" / "index.html",
        site / "assets" / "style.css",
        site / "index.html",
        site / "2026" / "index.html",
        site / "2026" / "06" / "index.html",
    ]
    for p in expected:
        assert p.is_file(), f"missing {p}"

    day_html = (site / "2026" / "06" / "27" / "index.html").read_text()
    assert "style.css" in day_html
    assert "pr/1234/" in day_html  # per-object link
    assert "SMBIOS handle range" in day_html


def test_headline_is_not_rendered(config):
    conn = db_mod.connect(config.storage.db_path)
    db_mod.init_db(conn)
    vm = _view_model()
    vm.headline = "EDK II project updates include merged PRs and several closed as superseded."
    try:
        render_day(conn, config, vm, date(2026, 6, 27), run_id=1)
    finally:
        conn.close()

    html = (Path(config.storage.site_dir) / "2026" / "06" / "27" / "index.html").read_text()
    assert "<h1>edk2 daily</h1>" in html  # stable short heading
    # The headline field is no longer surfaced anywhere on the page.
    assert "EDK II project updates" not in html
