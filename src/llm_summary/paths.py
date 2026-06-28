"""Static-site path builders. Directory-based, number-based stable URLs."""

from __future__ import annotations

from datetime import date
from pathlib import Path


def _date_parts(d: date) -> tuple[str, str, str]:
    return f"{d.year:04d}", f"{d.month:02d}", f"{d.day:02d}"


def day_dir(site_dir: str | Path, d: date) -> Path:
    y, m, day = _date_parts(d)
    return Path(site_dir) / y / m / day


def day_index(site_dir: str | Path, d: date) -> Path:
    return day_dir(site_dir, d) / "index.html"


def object_dir(site_dir: str | Path, d: date, kind: str, number: int) -> Path:
    # kind is 'pr' or 'issue'
    return day_dir(site_dir, d) / kind / str(number)


def object_index(site_dir: str | Path, d: date, kind: str, number: int) -> Path:
    return object_dir(site_dir, d, kind, number) / "index.html"


def year_index(site_dir: str | Path, d: date) -> Path:
    y, _, _ = _date_parts(d)
    return Path(site_dir) / y / "index.html"


def month_index(site_dir: str | Path, d: date) -> Path:
    y, m, _ = _date_parts(d)
    return Path(site_dir) / y / m / "index.html"


def root_index(site_dir: str | Path) -> Path:
    return Path(site_dir) / "index.html"


def assets_dir(site_dir: str | Path) -> Path:
    return Path(site_dir) / "assets"


def day_url(d: date) -> str:
    y, m, day = _date_parts(d)
    return f"/{y}/{m}/{day}/"


def object_url(d: date, kind: str, number: int) -> str:
    return f"{day_url(d)}{kind}/{number}/"
