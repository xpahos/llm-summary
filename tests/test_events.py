from llm_summary import events as events_mod

from conftest import make_issue, make_pr


def test_duplicate_events_ignored(conn, window_dts):
    since, until = window_dts
    obj = make_pr(number=30)
    evs = events_mod.normalize_object_events(obj, since, until)
    assert evs, "expected at least one event in window"

    first = events_mod.insert_events(conn, evs)
    second = events_mod.insert_events(conn, evs)  # same external_ids
    assert len(first) == len(evs)
    assert second == []  # all ignored

    total = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
    assert total == len(evs)


def test_window_filtering_excludes_outside_events(conn, window_dts):
    since, until = window_dts
    # Comment before the window must be excluded.
    obj = make_issue(
        number=31,
        created="2026-06-25T00:00:00Z",  # opened before window
        comments=[
            {
                "id": 1,
                "user": "u",
                "created_at": "2026-06-20T00:00:00Z",  # before window
                "body": "old",
                "url": "x",
            },
            {
                "id": 2,
                "user": "u",
                "created_at": "2026-06-27T10:00:00Z",  # inside window
                "body": "new",
                "url": "y",
            },
        ],
    )
    evs = events_mod.normalize_object_events(obj, since, until)
    types = [(e["event_type"], e["external_id"]) for e in evs]
    # opened excluded (created before window); only the in-window comment remains.
    assert all(t[0] != events_mod.OPENED for t in types)
    assert any(e["external_id"] == events_mod.eid_comment(obj["repo"], 2) for e in evs)
    assert not any(e["external_id"] == events_mod.eid_comment(obj["repo"], 1) for e in evs)


def test_opened_event_in_window(conn, window_dts):
    since, until = window_dts
    obj = make_pr(number=32, created="2026-06-27T07:00:00Z")
    evs = events_mod.normalize_object_events(obj, since, until)
    assert any(e["event_type"] == events_mod.OPENED for e in evs)
