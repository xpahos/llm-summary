"""CLI entrypoint for llm_summary."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime, timedelta, timezone

from . import config as config_mod
from . import db as db_mod

log = logging.getLogger("llm_summary")


def _date_arg(value: str) -> str:
    """argparse type: validate a YYYY-MM-DD date string."""
    try:
        date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid date {value!r}, expected YYYY-MM-DD")
    return value


def resolve_days(
    date_arg: str | None, from_arg: str | None, to_arg: str | None
) -> list[date] | None:
    """Resolve the requested calendar days.

    Returns None for the default automatic daily run (no date flags), a single-item
    list for --date, or the inclusive [from, to] range. Raises ValueError on invalid
    flag combinations.
    """
    if date_arg and (from_arg or to_arg):
        raise ValueError("Use either --date or --from/--to, not both.")
    if bool(from_arg) ^ bool(to_arg):
        raise ValueError("--from and --to must be given together.")

    if date_arg:
        return [date.fromisoformat(date_arg)]
    if from_arg:
        start = date.fromisoformat(from_arg)
        end = date.fromisoformat(to_arg)
        if end < start:
            raise ValueError("--to must not be earlier than --from.")
        return [start + timedelta(days=i) for i in range((end - start).days + 1)]
    return None


def day_window(d: date) -> tuple[str, str]:
    """ISO-8601 half-open [since, until) bounds for a single UTC calendar day."""
    since = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    until = since + timedelta(days=1)
    return since.isoformat(), until.isoformat()


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def cmd_init_db(cfg: config_mod.Config, args: argparse.Namespace) -> int:
    conn = db_mod.connect(cfg.storage.db_path)
    try:
        db_mod.init_db(conn)
        log.info("Initialised database at %s", cfg.storage.db_path)
    finally:
        conn.close()
    return 0


def cmd_run_daily(cfg: config_mod.Config, args: argparse.Namespace) -> int:
    from .graph import run_pipeline

    try:
        days = resolve_days(args.date, args.from_date, args.to_date)
    except ValueError as exc:
        log.error("%s", exc)
        return 2

    # No date flags: the automatic daily window, which advances the cursor.
    if days is None:
        result = run_pipeline(cfg)
        if result.get("errors"):
            log.error("Run finished with errors: %s", result["errors"])
            return 1
        log.info("Run complete. Output paths: %d", len(result.get("output_paths", [])))
        return 0

    # Explicit single day or range: process each day, never moving the cursor.
    failed = 0
    for d in days:
        since, until = day_window(d)
        log.info("Processing %s", d.isoformat())
        result = run_pipeline(cfg, since=since, until=until, advance_cursor=False)
        if result.get("errors"):
            failed += 1
            log.error("Day %s finished with errors: %s", d.isoformat(), result["errors"])
    if failed:
        log.error("%d of %d day(s) failed", failed, len(days))
        return 1
    log.info("Processed %d day(s).", len(days))
    return 0


def cmd_crawl(cfg: config_mod.Config, args: argparse.Namespace) -> int:
    from .graph import run_pipeline

    result = run_pipeline(cfg, since=args.since, until=args.until, advance_cursor=False)
    if result.get("errors"):
        log.error("Run finished with errors: %s", result["errors"])
        return 1
    return 0


def cmd_render_latest(cfg: config_mod.Config, args: argparse.Namespace) -> int:
    from .renderer import render_latest

    conn = db_mod.connect(cfg.storage.db_path)
    try:
        db_mod.init_db(conn)
        paths = render_latest(conn, cfg)
    finally:
        conn.close()
    if not paths:
        log.warning("Nothing to render (no daily_pages found).")
        return 1
    log.info("Re-rendered %d page(s).", len(paths))
    return 0


def cmd_render_all(cfg: config_mod.Config, args: argparse.Namespace) -> int:
    from .renderer import render_all

    try:
        days = resolve_days(args.date, args.from_date, args.to_date)
    except ValueError as exc:
        log.error("%s", exc)
        return 2
    only_dates = {d.isoformat() for d in days} if days is not None else None

    conn = db_mod.connect(cfg.storage.db_path)
    try:
        db_mod.init_db(conn)
        paths = render_all(conn, cfg, only_dates=only_dates)
    finally:
        conn.close()
    if not paths:
        log.warning("Nothing to render (no matching daily_pages found).")
        return 1
    scope = "all stored days" if only_dates is None else f"{len(only_dates)} requested day(s)"
    log.info("Re-rendered %d page(s) across %s.", len(paths), scope)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="llm-summary")
    parser.add_argument(
        "--config",
        default=None,
        help="Path to config.toml (default: $LLM_SUMMARY_CONFIG).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="Create the SQLite schema.")

    run_daily = sub.add_parser(
        "run-daily",
        help="Run the digest pipeline. No date flags = automatic daily window.",
    )
    run_daily.add_argument(
        "--date",
        type=_date_arg,
        help="Process a specific calendar day (YYYY-MM-DD, UTC).",
    )
    run_daily.add_argument(
        "--from",
        dest="from_date",
        type=_date_arg,
        help="Start of an inclusive date range (YYYY-MM-DD); requires --to.",
    )
    run_daily.add_argument(
        "--to",
        dest="to_date",
        type=_date_arg,
        help="End of an inclusive date range (YYYY-MM-DD); requires --from.",
    )

    sub.add_parser("render-latest", help="Re-render the most recent daily page.")

    render_all = sub.add_parser(
        "render-all",
        help="Re-render stored days from saved data (no GitHub, no LLM). "
        "With no date flags, renders all stored days.",
    )
    render_all.add_argument("--date", type=_date_arg, help="Render a single day (YYYY-MM-DD).")
    render_all.add_argument(
        "--from", dest="from_date", type=_date_arg,
        help="Start of an inclusive date range (YYYY-MM-DD); requires --to.",
    )
    render_all.add_argument(
        "--to", dest="to_date", type=_date_arg,
        help="End of an inclusive date range (YYYY-MM-DD); requires --from.",
    )

    crawl = sub.add_parser("crawl", help="Run the pipeline over an explicit window.")
    crawl.add_argument("--since", required=True, help="ISO-8601 window start (inclusive).")
    crawl.add_argument("--until", required=True, help="ISO-8601 window end (exclusive).")

    return parser


_DISPATCH = {
    "init-db": cmd_init_db,
    "run-daily": cmd_run_daily,
    "render-latest": cmd_render_latest,
    "render-all": cmd_render_all,
    "crawl": cmd_crawl,
}


def main(argv: list[str] | None = None) -> int:
    _setup_logging()
    parser = build_parser()
    args = parser.parse_args(argv)
    cfg = config_mod.load_config(args.config)
    from . import net

    net.apply_env_proxy(cfg)
    handler = _DISPATCH[args.command]
    return handler(cfg, args)


if __name__ == "__main__":
    sys.exit(main())
