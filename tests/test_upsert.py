from llm_summary import crawler as crawler_mod

from conftest import make_pr, make_issue


def _get(conn, kind, number):
    return conn.execute(
        "SELECT * FROM objects WHERE kind=? AND number=?", (kind, number)
    ).fetchone()


def test_upsert_preserves_first_seen_at(conn):
    obj = make_issue(number=10)
    meta1 = crawler_mod.upsert_object(conn, obj)
    assert meta1["is_new_local"] is True
    row1 = _get(conn, "issue", 10)
    first_seen = row1["first_seen_at"]

    # Re-upsert with a changed title; first_seen_at must be preserved.
    obj2 = make_issue(number=10)
    obj2["title"] = "Updated title"
    meta2 = crawler_mod.upsert_object(conn, obj2)
    assert meta2["is_new_local"] is False
    row2 = _get(conn, "issue", 10)
    assert row2["first_seen_at"] == first_seen
    assert row2["title"] == "Updated title"
    assert row2["last_seen_at"] >= first_seen


def test_upsert_reports_head_sha_delta(conn):
    pr = make_pr(number=20, head_sha="aaa")
    meta1 = crawler_mod.upsert_object(conn, pr)
    assert meta1["is_new_local"] is True
    assert meta1["old_head_sha"] is None
    assert meta1["new_head_sha"] == "aaa"

    pr2 = make_pr(number=20, head_sha="bbb")
    meta2 = crawler_mod.upsert_object(conn, pr2)
    assert meta2["old_head_sha"] == "aaa"
    assert meta2["new_head_sha"] == "bbb"
    assert crawler_mod.head_changed(meta2) is True
    assert crawler_mod.head_changed(meta1) is False
