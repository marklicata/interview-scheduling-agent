"""Small helpers shared by harness modules."""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone


def parse_ts(value) -> datetime:
    """Parse an ISO 8601 timestamp that must carry a UTC offset or Z."""
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if text.endswith("Z") or text.endswith("z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        raise ValueError(f"timestamp has no UTC offset: {value}")
    return dt


def try_parse_ts(value):
    try:
        return parse_ts(value)
    except (TypeError, ValueError):
        return None


def iso(dt: datetime) -> str:
    return dt.isoformat()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def jsonable(obj):
    """Return a JSON-safe deep copy (unknown objects become strings)."""
    return json.loads(json.dumps(obj, default=_default, ensure_ascii=False))


def _default(o):
    if hasattr(o, "to_dict"):
        return o.to_dict()
    if isinstance(o, (set, frozenset, tuple)):
        return list(o)
    if isinstance(o, datetime):
        return o.isoformat()
    return str(o)


def wilson(k: int, n: int, z: float = 1.96):
    """Wilson score interval for k successes in n trials; None when n == 0."""
    if n <= 0:
        return None
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def pct(x) -> str:
    return "N/A" if x is None else f"{100 * x:.1f}%"
