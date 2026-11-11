"""Time-zone and timestamp utilities. All instants are timezone-aware datetimes."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_WEEKDAY_ALIASES = {
    "monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1, "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3, "friday": 4, "fri": 4, "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}


def parse_ts(value) -> datetime:
    """Parse an ISO 8601 timestamp that carries a UTC offset. Naive timestamps are rejected."""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        s = value.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
    else:
        raise TypeError(f"expected an ISO 8601 string, got {type(value).__name__}")
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(f"timestamp {value!r} has no UTC offset")
    return dt


def try_parse_ts(value):
    try:
        return parse_ts(value)
    except (TypeError, ValueError):
        return None


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="minutes") if dt.second == 0 and dt.microsecond == 0 else dt.isoformat()


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(str(name))
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"unknown IANA time zone {name!r}") from exc


def is_zone(name) -> bool:
    try:
        zone(name)
        return True
    except ValueError:
        return False


def fmt_offset(td: timedelta) -> str:
    total = int(td.total_seconds()) // 60
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    return f"{sign}{total // 60:02d}:{total % 60:02d}"


def stated_time(instant, time_zone: str) -> dict:
    """The contract's StatedTime for an instant shown in a zone (computed, never typed by the model)."""
    dt = parse_ts(instant).astimezone(zone(time_zone))
    return {
        "instant": iso(parse_ts(instant)),
        "time_zone": time_zone,
        "local": dt.strftime("%Y-%m-%dT%H:%M"),
        "weekday": WEEKDAYS[dt.weekday()],
        "utc_offset": fmt_offset(dt.utcoffset()),
    }


def describe_local(instant, time_zone: str) -> dict:
    """Human-oriented local rendering used by the model to write messages."""
    dt = parse_ts(instant).astimezone(zone(time_zone))
    hour = dt.strftime("%I").lstrip("0") or "12"
    return {
        **stated_time(instant, time_zone),
        "abbreviation": dt.tzname(),
        "date_text": f"{dt.strftime('%A')}, {dt.strftime('%B')} {dt.day}, {dt.year}",
        "time_text": f"{hour}:{dt.strftime('%M')} {'a.m.' if dt.hour < 12 else 'p.m.'}",
    }


def local_to_instant(local_date: str, local_time: str, time_zone: str) -> dict:
    """Resolve a wall-clock time in a zone, detecting DST gaps (nonexistent) and folds (ambiguous)."""
    tz = zone(time_zone)
    d = date.fromisoformat(local_date)
    t = time.fromisoformat(local_time)
    naive = datetime.combine(d, t)
    first = naive.replace(tzinfo=tz, fold=0)
    second = naive.replace(tzinfo=tz, fold=1)
    candidates = []
    for cand in (first, second):
        roundtrip = cand.astimezone(timezone.utc).astimezone(tz)
        if roundtrip.replace(tzinfo=None) == naive and all(c.utcoffset() != cand.utcoffset() for c in candidates):
            candidates.append(cand)
    if not candidates:
        return {"status": "nonexistent", "detail": f"{local_date} {local_time} does not exist in {time_zone} "
                                                  "(daylight-saving gap); ask which time is meant."}
    if len(candidates) > 1:
        return {"status": "ambiguous", "instants": [iso(c) for c in candidates],
                "detail": f"{local_date} {local_time} occurs twice in {time_zone} (daylight-saving fold); "
                          "ask which offset is meant."}
    return {"status": "ok", "instant": iso(candidates[0]), **stated_time(candidates[0], time_zone)}


def weekday_index(name) -> int | None:
    if isinstance(name, int):
        return name % 7
    return _WEEKDAY_ALIASES.get(str(name).strip().lower())


def to_min(dt: datetime) -> int:
    """Minutes since the Unix epoch (UTC) - the engine's integer time axis."""
    return int(dt.timestamp() // 60)


def from_min(m: int, tz: str | None = None) -> datetime:
    dt = datetime.fromtimestamp(m * 60, tz=timezone.utc)
    return dt.astimezone(zone(tz)) if tz else dt
