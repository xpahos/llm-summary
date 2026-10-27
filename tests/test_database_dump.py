import shutil
import sqlite3
from contextlib import closing
from datetime import date
from pathlib import Path

import pytest

from llm_summary import db as db_mod
from llm_summary.graph import run_pipeline
from llm_summary.llm import DayViewModel
from llm_summary.renderer import render_all, render_day, render_latest

from conftest import FakeGithubClient, FakeSummarizer, make_issue


def test_pipeline_publishes_complete_database_and_root_download(config, since_until):
    since, until = since_until
    result = run_pipeline(
        config, since=since, until=until,
        gh=FakeGithubClient(config.github.repo, {("issue", 999): make_issue()}),
        summarizer=FakeSummarizer(),
    )
    assert not result.get("errors")
    site = Path(config.storage.site_dir)
    dump = site / "dump" / "llm-summary.sqlite"
    assert dump.is_file()
    assert str(dump) in result["output_paths"]
    with closing(sqlite3.connect(dump)) as snapshot:
        snapshot.row_factory = sqlite3.Row
        assert snapshot.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert snapshot.execute("SELECT number FROM objects").fetchone()[0] == 999
        for table in ("events", "object_summaries", "daily_pages"):
            assert snapshot.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] > 0
        assert snapshot.execute("SELECT status FROM runs").fetchone()[0] == "success"
        assert db_mod.get_state(snapshot, db_mod.STATE_LAST_UNTIL) == until

    root = (site / "index.html").read_text()
    assert 'href="dump/llm-summary.sqlite" download' in root
    assert "Snapshot updated" in root
    for page in (site / "2026").rglob("index.html"):
        assert "Download SQLite database" not in page.read_text()
    assert list((site / "dump").iterdir()) == [dump]


@pytest.mark.parametrize("mode", ["latest", "all", "subset"])
def test_rerender_exports_all_committed_data_including_wal(config, tmp_path, mode):
    with closing(db_mod.connect(config.storage.db_path)) as conn:
        db_mod.init_db(conn)
        conn.execute("PRAGMA wal_autocheckpoint=0")
        for day in (10, 11):
            render_day(conn, config, DayViewModel(date=f"2026-06-{day}", repo=config.github.repo),
                       date(2026, 6, day), None)
        db_mod.set_state(conn, "export_marker", "latest committed data")
        conn.commit()
        assert Path(config.storage.db_path + "-wal").stat().st_size > 0
        shutil.rmtree(config.storage.site_dir)
        if mode == "latest":
            paths = render_latest(conn, config)
        else:
            paths = render_all(conn, config, only_dates={"2026-06-10"} if mode == "subset" else None)
        dump = Path(config.storage.site_dir) / "dump" / "llm-summary.sqlite"
        assert dump in paths
        # Download only the .sqlite file; no source WAL or SHM is available.
        downloaded = tmp_path / "download.sqlite"
        shutil.copyfile(dump, downloaded)
        with closing(sqlite3.connect(downloaded)) as snapshot:
            snapshot.row_factory = sqlite3.Row
            assert snapshot.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert db_mod.get_state(snapshot, "export_marker") == "latest committed data"
            assert snapshot.execute("SELECT COUNT(*) FROM daily_pages").fetchone()[0] == 2


def test_failed_snapshot_replacement_preserves_previous_download(config, monkeypatch):
    with closing(db_mod.connect(config.storage.db_path)) as conn:
        db_mod.init_db(conn)
        render_day(conn, config, DayViewModel(date="2026-06-10", repo=config.github.repo),
                   date(2026, 6, 10), None)
        render_latest(conn, config)
        dump = Path(config.storage.site_dir) / "dump" / "llm-summary.sqlite"
        assert dump.is_file()
        previous = dump.read_bytes()
        db_mod.set_state(conn, "new_data", "new")
        conn.commit()
        original_replace = Path.replace

        def fail_dump_replace(path, target):
            if target == dump:
                raise OSError("cannot publish snapshot")
            return original_replace(path, target)

        monkeypatch.setattr(Path, "replace", fail_dump_replace)
        with pytest.raises(OSError, match="cannot publish snapshot"):
            render_latest(conn, config)
        assert dump.read_bytes() == previous
        assert list(dump.parent.iterdir()) == [dump]


def test_failed_pipeline_keeps_previous_snapshot(config, since_until):
    since, until = since_until
    gh = FakeGithubClient(config.github.repo, {("issue", 999): make_issue()})
    run_pipeline(config, since=since, until=until, gh=gh, summarizer=FakeSummarizer())
    dump = Path(config.storage.site_dir) / "dump" / "llm-summary.sqlite"
    assert dump.is_file()
    previous = dump.read_bytes()

    class FailingSummarizer(FakeSummarizer):
        def daily_view_model(self, payload):
            raise RuntimeError("summarization failed")

    result = run_pipeline(config, since=since, until=until, gh=gh, summarizer=FailingSummarizer())
    assert result.get("errors")
    assert dump.read_bytes() == previous
