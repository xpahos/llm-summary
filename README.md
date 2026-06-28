# llm-summary

A static daily summary generator for [`tianocore/edk2`](https://github.com/tianocore/edk2)
GitHub activity.

Each run fetches the previous day's PR/issue activity, stores state in SQLite, uses
[LangGraph](https://github.com/langchain-ai/langgraph) as a **deterministic** workflow
engine to drive LLM summarization, and emits a static HTML archive. GitHub is the source
of truth; SQLite holds current object snapshots, an event journal, rolling per-object
summaries, run state, and a cursor.

This is **not** an autonomous agent. LangGraph is used purely as a linear, deterministic
pipeline, and the LLM only summarizes structured input — it never fetches data, calls
tools, or writes files.

```text
GitHub API
  -> crawler            (PyGithub: search, fetch full PR/issue, compare)
  -> SQLite             (snapshots, event journal, rolling summaries, run state, cursor)
  -> LangGraph workflow (summarize events, build a daily view model)
  -> static HTML site   (Jinja2 templates, no JavaScript)
```

## Quick start (Docker)

```bash
cp .env.example .env        # fill in GITHUB_TOKEN and LLM_API_KEY
docker compose build
docker compose run --rm llm-summary      # runs `run-daily`
```

Generated HTML lands in `./site`, the SQLite database in `./data`. Three volumes are
mounted:

| Container path | Host path     | Purpose                                            |
| -------------- | ------------- | -------------------------------------------------- |
| `/data`        | `./data`      | SQLite database (+ optional raw payload dumps)     |
| `/site`        | `./site`      | Generated static HTML (serve this directly)        |
| `/config`      | `./config` ro | Read-only / ephemeral configuration                |

If `/config` is ephemeral, the entrypoint self-seeds `config.toml` from the bundled
template on startup; the app also runs with **no** config file at all, using env vars and
built-in defaults.

## CLI

```bash
python -m llm_summary.main run-daily        # automatic daily window (advances the cursor)
python -m llm_summary.main run-daily --date 2026-06-20            # one specific day
python -m llm_summary.main run-daily --from 2026-06-01 --to 2026-06-07  # inclusive range
python -m llm_summary.main init-db          # create the SQLite schema
python -m llm_summary.main render-latest    # re-render the most recent day from stored state
python -m llm_summary.main render-all       # re-render every stored day (no GitHub/LLM)
python -m llm_summary.main render-all --from 2026-06-10 --to 2026-06-15  # re-render a range
python -m llm_summary.main crawl --since 2026-06-27T00:00:00Z --until 2026-06-28T00:00:00Z
```

`run-daily` selects the window as follows:

- **no date flags** — the automatic daily window (previous successful `until` → start of
  today, UTC). This is the scheduler mode and the only one that **advances the cursor**.
  A run started just after `00:00 UTC` on day *D* summarizes the previous day *D-1*.
- **`--date YYYY-MM-DD`** — process exactly that UTC calendar day.
- **`--from YYYY-MM-DD --to YYYY-MM-DD`** — process every day in the inclusive range,
  oldest first, one daily page per day.

Explicit `--date` / `--from`/`--to` runs (and `crawl`) **do not** move the cursor, so
backfilling or re-running an old day is safe and won't disturb the daily schedule. All
windows are idempotent — re-processing a day inserts no duplicate events.

### Re-rendering without re-crawling

`render-latest` (most recent day) and `render-all` (every stored day, or a `--date` /
`--from`/`--to` subset) rebuild HTML purely from the saved view models in `daily_pages`
— **no GitHub calls, no LLM**. Use them to apply template/CSS/layout changes across the
archive. They can only surface fields already stored when each day was generated; data
added later needs a real `run-daily`. Navigational indexes are always rebuilt from the
full `daily_pages` table, so rendering a subset never breaks the year/month/root listings.

### Scheduling (cron)

Run `run-daily` (no date flags) shortly after midnight UTC; each run produces the
previous day's page and advances the cursor:

```cron
5 0 * * * cd /path/to/llm-summary && docker compose run --rm llm-summary run-daily >> cron.log 2>&1
```

An entry-point script named `llm-summary` is installed as well (e.g. `llm-summary
run-daily --date 2026-06-20`). A global `--config PATH` flag overrides the config file
location.

## How a run works

The pipeline is a fixed sequence of LangGraph nodes. Any node exception routes to
`fail_run`, which records the error and leaves the cursor unadvanced so the next run
retries the same window.

1. `load_window` — compute the `[since, until)` window, open a `runs` row.
2. `fetch_candidates` — search GitHub for PRs/issues updated in the window.
3. `sync_objects` — fetch full state, upsert snapshots, detect PR head-SHA changes
   (emitting a synthetic `pr_head_updated` event with compare data).
4. `fetch_activity` — normalize comments/reviews/timeline into the event journal.
5. `bootstrap_object_summaries` — build an initial rolling summary for newly seen objects.
6. `process_events` — fold each unprocessed event into its object's rolling summary.
7. `build_daily_view_model` — ask the LLM for a structured JSON view model (never HTML).
8. `render_static_site` — render the day page, per-object pages, and index pages.
9. `finish_run` — mark success and advance the cursor.

### Window semantics

The processing window is half-open `[since, until)`. For `run-daily`, `until` defaults to
the start of the current UTC day and `since` is the previous successful run's `until`
(or `until - default_bootstrap_days` on the first run). The cursor
(`state.github_last_successful_until`) only advances when a run succeeds.

Idempotency rests on a `UNIQUE(repo, external_id)` constraint plus `INSERT OR IGNORE`:
re-running the same window inserts zero new events.

## Output layout

Directory-based, number-based stable URLs (titles are never put in paths):

```text
site/
  index.html
  2026/index.html
  2026/06/index.html
  2026/06/28/index.html
  2026/06/28/pr/1234/index.html
  2026/06/28/issue/999/index.html
  assets/style.css
```

## Configuration

Priority is **environment variables > `config.toml` > built-in defaults**. See
[`config/config.example.toml`](config/config.example.toml) and
[`.env.example`](.env.example). Secrets should come from the environment; they are never
logged or baked into the image.

```toml
[github]
repo = "tianocore/edk2"      # token via GITHUB_TOKEN

[llm]
provider = "openai"
model = "gpt-4o"             # api_key via LLM_API_KEY
temperature = 0.2

[storage]
db_path = "/data/llm-summary.sqlite"
site_dir = "/site"

[crawler]
timezone = "UTC"
default_bootstrap_days = 1

[proxy]
url = ""                    # see "Corporate proxy" below
```

### Environment variables

| Variable                         | Overrides                          |
| -------------------------------- | ---------------------------------- |
| `GITHUB_TOKEN`                   | `[github] token`                   |
| `GITHUB_REPO`                    | `[github] repo`                    |
| `LLM_API_KEY` (`OPENAI_API_KEY`) | `[llm] api_key`                    |
| `LLM_PROVIDER`                   | `[llm] provider`                   |
| `LLM_MODEL`                      | `[llm] model`                      |
| `LLM_BASE_URL`                   | `[llm] base_url`                   |
| `LLM_TEMPERATURE`                | `[llm] temperature`                |
| `LLM_SUMMARY_DB`                 | `[storage] db_path`                |
| `LLM_SUMMARY_SITE`               | `[storage] site_dir`               |
| `LLM_SUMMARY_CONFIG`             | config file path                   |
| `CRAWLER_TIMEZONE`               | `[crawler] timezone`               |
| `CRAWLER_DEFAULT_BOOTSTRAP_DAYS` | `[crawler] default_bootstrap_days` |
| `LLM_SUMMARY_PROXY`              | `[proxy] url`                      |

### Corporate proxy (SOCKS5)

All outbound traffic (GitHub and the LLM API) can be routed through a proxy. Set one URL
via `[proxy] url` in `config.toml`, the `LLM_SUMMARY_PROXY` env var, or the standard
`ALL_PROXY` / `HTTPS_PROXY` variables:

```bash
LLM_SUMMARY_PROXY=socks5h://user:pass@proxy.corp:1080
```

Use the `socks5h://` scheme so hostnames are resolved on the proxy (usually required on
corporate networks). The URL is normalized per client — `requests`/PyGithub get
`socks5h`, while the LLM's `httpx` client gets `socks5` (which also resolves remotely).
Credentials embedded in the URL are redacted in logs.

> **Running in Docker?** A proxy on your host machine is **not** reachable as
> `localhost` from inside the container — that points at the container itself. Use
> `host.docker.internal` instead, e.g. `socks5h://host.docker.internal:1080`. The compose
> file maps `host.docker.internal` so this works on Linux as well as Docker Desktop.

## Data model (SQLite)

Created automatically on startup. Tables: `objects` (current PR/issue snapshots),
`events` (sequential activity journal with stable `external_id`), `object_summaries`
(rolling per-object summary), `runs` (run history), `state` (key/value cursor), and
`daily_pages` (generated-output tracking). See
[`schema.sql`](src/llm_summary/schema.sql).

## Development

```bash
pip install -e ".[dev]"
pytest
```

The test suite runs fully offline — GitHub and the LLM are replaced with fakes. It covers
schema creation, `first_seen_at` preservation on upsert, event de-duplication, exactly-once
PR head-update detection, same-window idempotency, renderer output paths, and proxy URL
normalization.

## Dependencies

PyGithub, LangGraph, langchain-openai, Jinja2, pydantic, python-dateutil, plus `PySocks`
and `httpx[socks]` for SOCKS proxy support. SQLite uses the Python standard library. No
web framework — this is a static site generator.
