"""Static-site renderer: Jinja2 templates -> HTML archive under site_dir."""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

from . import paths as paths_mod
from .config import Config, base_dir
from .db import utcnow_iso
from .llm import DayViewModel

log = logging.getLogger("llm_summary.renderer")

# Number of most-recent days listed on the root index page.
RECENT_DAYS = 7


def _env():
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    templates = base_dir() / "templates"
    return Environment(
        loader=FileSystemLoader(str(templates)),
        autoescape=select_autoescape(["html", "xml"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )


def _prefix(depth: int) -> str:
    return "../" * depth


def _breadcrumbs(prefix: str, d: date, leaf: str | None = None) -> list[dict[str, str]]:
    y, m, day = f"{d.year:04d}", f"{d.month:02d}", f"{d.day:02d}"
    crumbs = [
        {"label": "llm-summary", "href": f"{prefix}index.html"},
        {"label": y, "href": f"{prefix}{y}/index.html"},
        {"label": m, "href": f"{prefix}{y}/{m}/index.html"},
        {"label": day, "href": f"{prefix}{y}/{m}/{day}/index.html"},
    ]
    if leaf:
        crumbs.append({"label": leaf, "href": ""})
    return crumbs


def _write(path: Path, html: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")
    return path


def _copy_assets(site_dir: str | Path) -> None:
    src = base_dir() / "assets" / "style.css"
    dst = paths_mod.assets_dir(site_dir) / "style.css"
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_file():
        shutil.copyfile(src, dst)


def render_day(
    conn: sqlite3.Connection,
    config: Config,
    view_model: DayViewModel,
    d: date,
    run_id: int | None,
) -> list[Path]:
    """Render the day page, per-object pages, indexes and assets. Returns paths."""
    site_dir = config.storage.site_dir
    env = _env()
    written: list[Path] = []

    _copy_assets(site_dir)

    # Day page (depth 3).
    day_prefix = _prefix(3)
    day_html = env.get_template("day.html").render(
        asset_href=f"{day_prefix}assets/style.css",
        breadcrumbs=_breadcrumbs(day_prefix, d),
        vm=view_model,
        d=d,
    )
    written.append(_write(paths_mod.day_index(site_dir, d), day_html))

    # Per-object pages (depth 5).
    obj_prefix = _prefix(5)
    obj_template = env.get_template("object.html")
    for section in view_model.sections:
        for item in section.items:
            html = obj_template.render(
                asset_href=f"{obj_prefix}assets/style.css",
                breadcrumbs=_breadcrumbs(obj_prefix, d, f"{item.kind} #{item.number}"),
                item=item,
                d=d,
            )
            written.append(
                _write(paths_mod.object_index(site_dir, d, item.kind, item.number), html)
            )

    # Record in daily_pages (payload = full view model for re-render).
    conn.execute(
        """
        INSERT INTO daily_pages(date, path, generated_at, run_id, payload_json)
        VALUES (?,?,?,?,?)
        ON CONFLICT(date) DO UPDATE SET
            path=excluded.path,
            generated_at=excluded.generated_at,
            run_id=excluded.run_id,
            payload_json=excluded.payload_json
        """,
        (
            d.isoformat(),
            paths_mod.day_url(d),
            utcnow_iso(),
            run_id,
            view_model.model_dump_json(),
        ),
    )
    conn.commit()

    # Rebuild navigational indexes from daily_pages.
    written += _render_indexes(conn, config, env)
    log.info("Rendered %d page(s) for %s", len(written), d.isoformat())
    return written


def _render_indexes(conn: sqlite3.Connection, config: Config, env) -> list[Path]:
    site_dir = config.storage.site_dir
    rows = conn.execute(
        "SELECT date, path FROM daily_pages ORDER BY date DESC"
    ).fetchall()
    days = [dict(r) for r in rows]
    written: list[Path] = []
    index_tmpl = env.get_template("index.html")

    # Root index: list of years.
    years = sorted({r["date"][:4] for r in days}, reverse=True)
    root_links = [{"label": y, "href": f"{y}/index.html"} for y in years]
    recent = [{"label": r["date"], "href": f"{r['date'][:4]}/{r['date'][5:7]}/{r['date'][8:10]}/index.html"} for r in days[:RECENT_DAYS]]
    written.append(
        _write(
            paths_mod.root_index(site_dir),
            index_tmpl.render(
                asset_href="assets/style.css",
                breadcrumbs=[{"label": "llm-summary", "href": ""}],
                title="llm-summary",
                subtitle=f"{config.github.repo} daily archive",
                links=root_links,
                recent=recent,
            ),
        )
    )

    # Year and month indexes.
    by_year: dict[str, set[str]] = {}
    by_month: dict[tuple[str, str], list[str]] = {}
    for r in days:
        y, m, dd = r["date"][:4], r["date"][5:7], r["date"][8:10]
        by_year.setdefault(y, set()).add(m)
        by_month.setdefault((y, m), []).append(dd)

    for y, months in by_year.items():
        links = [{"label": f"{y}-{m}", "href": f"{m}/index.html"} for m in sorted(months, reverse=True)]
        written.append(
            _write(
                Path(site_dir) / y / "index.html",
                index_tmpl.render(
                    asset_href="../assets/style.css",
                    breadcrumbs=[
                        {"label": "llm-summary", "href": "../index.html"},
                        {"label": y, "href": ""},
                    ],
                    title=y,
                    subtitle=f"{config.github.repo} — {y}",
                    links=links,
                    recent=[],
                ),
            )
        )

    for (y, m), dds in by_month.items():
        links = [
            {"label": f"{y}-{m}-{dd}", "href": f"{dd}/index.html"}
            for dd in sorted(set(dds), reverse=True)
        ]
        written.append(
            _write(
                Path(site_dir) / y / m / "index.html",
                index_tmpl.render(
                    asset_href="../../assets/style.css",
                    breadcrumbs=[
                        {"label": "llm-summary", "href": "../../index.html"},
                        {"label": y, "href": f"../index.html"},
                        {"label": m, "href": ""},
                    ],
                    title=f"{y}-{m}",
                    subtitle=f"{config.github.repo} — {y}-{m}",
                    links=links,
                    recent=[],
                ),
            )
        )

    return written


def render_latest(conn: sqlite3.Connection, config: Config) -> list[Path]:
    """Re-render the most recent day from its stored view model."""
    row = conn.execute(
        "SELECT date, run_id, payload_json FROM daily_pages ORDER BY date DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return []
    vm = DayViewModel.model_validate(json.loads(row["payload_json"]))
    d = date.fromisoformat(row["date"])
    return render_day(conn, config, vm, d, row["run_id"])
