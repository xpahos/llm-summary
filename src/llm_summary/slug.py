"""Minimal slug helper. Paths use stable numbers, so this is rarely needed."""

from __future__ import annotations

import re

_slug_re = re.compile(r"[^a-z0-9]+")


def slugify(text: str, max_len: int = 60) -> str:
    s = _slug_re.sub("-", text.lower()).strip("-")
    return s[:max_len].strip("-")
