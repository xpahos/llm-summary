-- llm-summary SQLite schema. All statements are idempotent (IF NOT EXISTS).

CREATE TABLE IF NOT EXISTS objects (
    repo TEXT NOT NULL,
    kind TEXT NOT NULL,              -- pr | issue
    number INTEGER NOT NULL,

    title TEXT NOT NULL,
    body TEXT,
    state TEXT NOT NULL,
    author TEXT,
    url TEXT NOT NULL,

    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    closed_at TEXT,

    -- PR-only fields
    head_sha TEXT,
    base_ref TEXT,
    head_ref TEXT,
    merged INTEGER DEFAULT 0,
    merged_at TEXT,

    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,

    raw_json TEXT,

    PRIMARY KEY(repo, kind, number)
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    repo TEXT NOT NULL,
    object_kind TEXT NOT NULL,       -- pr | issue
    object_number INTEGER NOT NULL,

    event_type TEXT NOT NULL,
    external_id TEXT NOT NULL,

    actor TEXT,
    created_at TEXT NOT NULL,
    seen_at TEXT NOT NULL,

    title TEXT,
    body TEXT,
    url TEXT,

    payload_json TEXT,

    processed INTEGER DEFAULT 0,

    UNIQUE(repo, external_id)
);

CREATE INDEX IF NOT EXISTS idx_events_window
    ON events(repo, created_at, id);

CREATE INDEX IF NOT EXISTS idx_events_unprocessed
    ON events(processed, created_at, id);

CREATE TABLE IF NOT EXISTS object_summaries (
    repo TEXT NOT NULL,
    object_kind TEXT NOT NULL,
    object_number INTEGER NOT NULL,

    summary TEXT NOT NULL,
    updated_at TEXT NOT NULL,

    last_event_id INTEGER,
    input_hash TEXT,

    PRIMARY KEY(repo, object_kind, object_number)
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    since TEXT NOT NULL,
    until TEXT NOT NULL,
    status TEXT NOT NULL,
    error TEXT
);

CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_pages (
    date TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    run_id INTEGER,
    payload_json TEXT
);
