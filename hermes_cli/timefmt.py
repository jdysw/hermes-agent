"""Small shared time-formatting helpers for CLI output."""

from __future__ import annotations

import logging
import math
import time as _time
import unicodedata
from datetime import datetime
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Epoch-seconds window a stored timestamp must fall in to be trusted: 1970 .. ~2103 (inside 32-bit
# ``time_t`` so ``fromtimestamp`` accepts it on every platform). SQLite dynamic typing lets a TEXT
# cell, ``inf``/``nan`` or a garbage double (``8.4e252`` salvaged from a damaged page) sit in a REAL
# column; ``datetime.fromtimestamp`` then raises and one bad row killed the whole listing, export
# or report (#102399, #102352, #99959).
EPOCH_MIN = 0.0
EPOCH_MAX = 4_200_000_000.0


def coerce_epoch(value: Any, *, session_id: Optional[str] = None, field: str = "timestamp") -> Optional[float]:
    """A stored timestamp cell as float epoch seconds, or ``None`` when it cannot be trusted.

    Numbers, numeric strings and ``datetime`` are accepted; anything else, non-finite values and
    values outside ``EPOCH_MIN..EPOCH_MAX`` return ``None`` after a WARNING naming the session so
    the corrupt row can be found. ``None``/``""`` mean "unset" and stay silent. Every reader that
    renders a row timestamp goes through here (a bad row degrades to one ``?`` cell, never a dead
    command) and every writer uses it to refuse persisting a new bad row.
    """
    if value is None or value == "":
        return None
    try:
        ts = float(value.timestamp()) if isinstance(value, datetime) else float(value)
    except (TypeError, ValueError):
        ts = math.nan
    if not (EPOCH_MIN <= ts <= EPOCH_MAX):  # also False for nan
        logger.warning("Ignoring corrupt %s %r%s", field, value, f" on session {session_id}" if session_id else "")
        return None
    return ts


def relative_time(ts, *, session_id: Optional[str] = None) -> str:
    """Format a timestamp as relative time (e.g., '2h ago', 'yesterday'); ``?`` when unset or corrupt."""
    if not ts or (ts := coerce_epoch(ts, session_id=session_id, field="last_active")) is None:
        return "?"
    delta = _time.time() - ts
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta / 60)}m ago"
    if delta < 86400:
        return f"{int(delta / 3600)}h ago"
    if delta < 172800:
        return "yesterday"
    if delta < 604800:
        return f"{int(delta / 86400)}d ago"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def display_width(text: str) -> int:
    """终端显示宽度：CJK/全角字符占 2 列，其余占 1 列。

    ``f"{x:<10}"`` 按字符数补齐，而一个汉字占 2 个终端格 —— 中文表格直接用
    字符数补齐必然错位，所以渲染中文列的代码统一走这里。
    """
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def pad_display(text: str, width: int, align: str = "left") -> str:
    """按显示宽度把 ``text`` 补齐到 ``width`` 列（一个汉字算 2 列）。

    ``align`` 取 ``left``（默认）/ ``right`` / ``center``。
    """
    fill = " " * max(0, width - display_width(text))
    if align == "right":
        return fill + text
    if align == "center":
        half = len(fill) // 2
        return fill[:half] + text + fill[half:]
    return text + fill
