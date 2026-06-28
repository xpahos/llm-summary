from datetime import date
from pathlib import Path

from llm_summary import db as db_mod
from llm_summary.renderer import render_all, render_day
from llm_summary.llm import DayViewModel, ViewItem, ViewSection, ViewStats


def _vm(d, number):
    return DayViewModel(
        date=d,
        repo="tianocore/edk2",
        sections=[ViewSection(id="merged", title="Merged", items=[ViewItem(kind="pr", number=number, title=f"PR {number}")])],
    )


def test_render_all_rerenders_every_stored_day(config):
    conn = db_mod.connect(config.storage.db_path)
    db_mod.init_db(conn)
    try:
        render_day(conn, config, _vm("2026-06-10", 1), date(2026, 6, 10), 1)
        render_day(conn, config, _vm("2026-06-11", 2), date(2026, 6, 11), 2)

        # Wipe the site, then rebuild purely from stored data (no LLM/GitHub).
        import shutil
        shutil.rmtree(config.storage.site_dir)

        paths = render_all(conn, config)
    finally:
        conn.close()

    site = Path(config.storage.site_dir)
    assert (site / "2026" / "06" / "10" / "index.html").is_file()
    assert (site / "2026" / "06" / "11" / "index.html").is_file()
    assert (site / "index.html").is_file()
    assert paths  # returned the written paths


def test_render_all_empty_db(config):
    conn = db_mod.connect(config.storage.db_path)
    db_mod.init_db(conn)
    try:
        assert render_all(conn, config) == []
    finally:
        conn.close()


def test_render_all_date_range_filter(config):
    conn = db_mod.connect(config.storage.db_path)
    db_mod.init_db(conn)
    try:
        for dd, num in [(10, 1), (11, 2), (12, 3)]:
            render_day(conn, config, _vm(f"2026-06-{dd}", num), date(2026, 6, dd), num)

        import shutil
        shutil.rmtree(config.storage.site_dir)

        # Only render 06-10 and 06-11.
        render_all(conn, config, only_dates={"2026-06-10", "2026-06-11"})
    finally:
        conn.close()

    site = Path(config.storage.site_dir)
    assert (site / "2026" / "06" / "10" / "index.html").is_file()
    assert (site / "2026" / "06" / "11" / "index.html").is_file()
    assert not (site / "2026" / "06" / "12" / "index.html").exists()  # outside range
    # Index still lists all stored days, including the unrendered one.
    assert "2026/06/12/" in (site / "index.html").read_text()
