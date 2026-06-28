from llm_summary import db as db_mod

EXPECTED = {"objects", "events", "object_summaries", "runs", "state", "daily_pages"}


def test_init_db_creates_tables(conn):
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    names = {r["name"] for r in rows}
    assert EXPECTED.issubset(names)


def test_init_db_is_idempotent():
    c = db_mod.connect(":memory:")
    db_mod.init_db(c)
    db_mod.init_db(c)  # second call must not raise
    rows = c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    assert EXPECTED.issubset({r["name"] for r in rows})
    c.close()
