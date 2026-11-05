#!/usr/bin/env python3
"""Shared case loading, time helpers, and the scheduling oracle.

Used by validate_cases.py (dataset checks) and by the evaluation harness (grading agent schedules).
The oracle models time only: intervals are half-open [start, end) in integer UTC minutes.
"""
from __future__ import annotations

import bisect
import itertools
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

# --------------------------------------------------------------------------------------
# YAML loading: keep timestamps as strings and never parse 9:30 as a base-60 integer.
# --------------------------------------------------------------------------------------


class CaseLoader(yaml.SafeLoader):
    pass


_REMOVED = {"tag:yaml.org,2002:timestamp", "tag:yaml.org,2002:int", "tag:yaml.org,2002:float"}
CaseLoader.yaml_implicit_resolvers = {
    key: [(tag, rx) for tag, rx in resolvers if tag not in _REMOVED]
    for key, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
CaseLoader.add_implicit_resolver(
    "tag:yaml.org,2002:int",
    re.compile(r"^(?:[-+]?0b[0-1_]+|[-+]?0[0-7_]+|[-+]?(?:0|[1-9][0-9_]*)|[-+]?0x[0-9a-fA-F_]+)$"),
    list("-+0123456789"),
)
CaseLoader.add_implicit_resolver(
    "tag:yaml.org,2002:float",
    re.compile(
        r"^(?:[-+]?(?:[0-9][0-9_]*)\.[0-9_]*(?:[eE][-+][0-9]+)?|\.[0-9][0-9_]*(?:[eE][-+][0-9]+)?"
        r"|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN))$"
    ),
    list("-+0123456789."),
)


def load_yaml(path: str):
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.load(fh, Loader=CaseLoader)


# --------------------------------------------------------------------------------------
# Time helpers
# --------------------------------------------------------------------------------------

WEEKDAYS = {
    "monday": 0, "mon": 0, "tuesday": 1, "tues": 1, "tue": 1, "wednesday": 2, "wed": 2,
    "thursday": 3, "thurs": 3, "thur": 3, "thu": 3, "friday": 4, "fri": 4, "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}
WD_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
HHMM_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$|^24:00$")

_TZ_CACHE: dict = {}


def get_tz(name):
    if not isinstance(name, str):
        return None
    if name not in _TZ_CACHE:
        try:
            _TZ_CACHE[name] = ZoneInfo(name) if ("/" in name or name == "UTC") else None
        except (ZoneInfoNotFoundError, ValueError):
            _TZ_CACHE[name] = None
    return _TZ_CACHE[name]


def parse_aware(value) -> datetime:
    dt = datetime.fromisoformat(str(value))
    if dt.tzinfo is None:
        raise ValueError(f"timestamp has no UTC offset: {value}")
    return dt


def to_min(dt: datetime) -> int:
    if dt.second or dt.microsecond:
        raise ValueError(f"oracle timestamps must be whole minutes: {dt.isoformat()}")
    return int(dt.timestamp()) // 60


def from_min(m: int) -> datetime:
    return datetime.fromtimestamp(m * 60, tz=timezone.utc)


def fmt_offset(td: timedelta) -> str:
    total = int(td.total_seconds()) // 60
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    return f"{sign}{total // 60:02d}:{total % 60:02d}"


# --------------------------------------------------------------------------------------
# Interval utilities (integer minutes, half-open [start, end))
# --------------------------------------------------------------------------------------


def norm(ints):
    ints = sorted((s, e) for s, e in ints if e > s)
    out = []
    for s, e in ints:
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def intersect(a, b):
    a, b = norm(a), norm(b)
    i = j = 0
    out = []
    while i < len(a) and j < len(b):
        s, e = max(a[i][0], b[j][0]), min(a[i][1], b[j][1])
        if s < e:
            out.append((s, e))
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return out


def subtract(a, b):
    out = []
    b = norm(b)
    for s, e in norm(a):
        cur = s
        for bs, be in b:
            if be <= cur or bs >= e:
                continue
            if bs > cur:
                out.append((cur, bs))
            cur = max(cur, be)
            if cur >= e:
                break
        if cur < e:
            out.append((cur, e))
    return out


class FreeSet:
    def __init__(self, intervals):
        self.intervals = norm(intervals)
        self.starts = [x[0] for x in self.intervals]

    def covers(self, s, e) -> bool:
        idx = bisect.bisect_right(self.starts, s) - 1
        return idx >= 0 and self.intervals[idx][1] >= e


# --------------------------------------------------------------------------------------
# Oracle
# --------------------------------------------------------------------------------------


class OracleError(ValueError):
    """The oracle specification cannot be interpreted."""


def parse_interval(item):
    if isinstance(item, dict):
        return item.get("start"), item.get("end")
    if isinstance(item, (list, tuple)) and len(item) == 2:
        return item[0], item[1]
    raise ValueError(f"interval must be {{start, end}} or [start, end]: {item!r}")


def interval_list(items):
    out = []
    for item in items or []:
        s, e = parse_interval(item)
        out.append((to_min(parse_aware(s)), to_min(parse_aware(e))))
    return out


def working_hour_intervals(spec, window):
    specs = spec if isinstance(spec, list) else [spec]
    out = []
    for wh in specs:
        tz = get_tz(wh.get("time_zone"))
        if tz is None:
            raise ValueError(f"working_hours needs a valid time_zone: {wh!r}")
        days = {WEEKDAYS[str(d).lower()] for d in wh.get("days", ["Mon", "Tue", "Wed", "Thu", "Fri"])}
        st, en = str(wh["start"]), str(wh["end"])
        if not HHMM_RE.match(st) or not HHMM_RE.match(en) or st == "24:00":
            raise ValueError(f"working_hours start/end must be quoted 'HH:MM' strings: {wh!r}")
        sh, sm = map(int, st.split(":"))
        eh, em = map(int, en.split(":"))
        first = from_min(window[0]).astimezone(tz).date() - timedelta(days=1)
        last = from_min(window[1]).astimezone(tz).date() + timedelta(days=1)
        d = first
        while d <= last:
            if d.weekday() in days:
                sdt = datetime(d.year, d.month, d.day, sh, sm, tzinfo=tz)
                if eh == 24:
                    nd = d + timedelta(days=1)
                    edt = datetime(nd.year, nd.month, nd.day, 0, 0, tzinfo=tz)
                else:
                    edt = datetime(d.year, d.month, d.day, eh, em, tzinfo=tz)
                    if edt <= sdt:
                        nd = d + timedelta(days=1)
                        edt = datetime(nd.year, nd.month, nd.day, eh, em, tzinfo=tz)
                out.append((to_min(sdt.astimezone(timezone.utc)), to_min(edt.astimezone(timezone.utc))))
            d += timedelta(days=1)
    return norm(out)


class Oracle:
    """Machine-checkable schedule semantics for one case (see AUTHORING_GUIDE.md section 4)."""

    def __init__(self, spec):
        if not isinstance(spec, dict):
            raise OracleError("oracle must be a mapping")
        self.spec = spec
        self._base = None
        try:
            self._build()
        except (KeyError, ValueError, TypeError) as exc:
            raise OracleError(f"oracle: {exc!r}") from exc

    def _build(self):
        s = self.spec
        self.gran = int(s.get("granularity_min", 5))
        if self.gran < 1:
            raise ValueError("granularity_min must be >= 1")
        win = s["search_window"]
        self.window = (to_min(parse_aware(win["start"])), to_min(parse_aware(win["end"])))
        if self.window[1] <= self.window[0]:
            raise ValueError("search_window end must be after start")
        if (self.window[1] - self.window[0]) / self.gran > 20000:
            raise ValueError("search grid too large; narrow search_window or raise granularity_min")
        self.candidate_ids = set(s.get("candidate_ids") or ["candidate"])
        self.free = {}
        for name, p in (s.get("participants") or {}).items():
            p = p or {}
            base = interval_list(p["available"]) if "available" in p else [self.window]
            free = intersect(base, [self.window])
            if p.get("working_hours"):
                free = intersect(free, working_hour_intervals(p["working_hours"], self.window))
            free = subtract(free, interval_list(p.get("busy")))
            self.free[name] = FreeSet(free)
        if not self.free:
            raise ValueError("oracle.participants is required")
        self.sessions = {}
        for sess in s["sessions"]:
            sid = sess["id"]
            attendees = list(sess.get("attendees") or [])
            pool = sess.get("pool")
            if not attendees and not pool:
                raise ValueError(f"session {sid} needs attendees and/or pool")
            for a in attendees + list((pool or {}).get("from", [])):
                if a not in self.free:
                    raise ValueError(f"session {sid} references unknown participant {a}")
            self.sessions[sid] = {"d": int(sess["duration_min"]), "att": attendees, "pool": pool}
        c = s.get("constraints") or {}
        self.order = c.get("order")
        if self.order is not None and sorted(self.order) != sorted(self.sessions):
            raise ValueError("constraints.order must list every session id exactly once")
        if self.order is None and len(self.sessions) > 6:
            raise ValueError("oracle without constraints.order supports at most 6 sessions")
        self.min_gap = int(c.get("min_gap_min", 0))
        self.max_gap = int(c["max_gap_min"]) if c.get("max_gap_min") is not None else None
        self.max_span = int(c["max_total_span_min"]) if c.get("max_total_span_min") is not None else None
        self.same_day_tz = None
        if c.get("same_local_day"):
            self.same_day_tz = get_tz(c["same_local_day"].get("time_zone"))
            if self.same_day_tz is None:
                raise ValueError("same_local_day.time_zone must be a valid IANA zone")
        self.align = None
        if c.get("start_alignment"):
            tz = get_tz(c["start_alignment"].get("time_zone"))
            if tz is None:
                raise ValueError("start_alignment.time_zone must be a valid IANA zone")
            self.align = (int(c["start_alignment"]["minutes"]), tz)
        self.distinct = bool(c.get("distinct_interviewers", False))

    def aligned(self, t):
        if not self.align:
            return True
        minutes, tz = self.align
        local = from_min(t).astimezone(tz)
        return (local.hour * 60 + local.minute) % minutes == 0

    def session_ok(self, sid, t, people=None):
        sess = self.sessions[sid]
        e = t + sess["d"]
        if t < self.window[0] or e > self.window[1]:
            return False, "outside search window"
        for a in sess["att"]:
            if not self.free[a].covers(t, e):
                return False, f"{a} unavailable"
        pool = sess["pool"]
        if pool:
            k = int(pool.get("count", 1))
            if people is None:
                if sum(1 for p in pool["from"] if self.free[p].covers(t, e)) < k:
                    return False, "insufficient available pool members"
            else:
                if len(set(people)) != k or any(p not in pool["from"] for p in people):
                    return False, f"pool assignment must name {k} distinct members of {pool['from']}"
                for p in people:
                    if not self.free[p].covers(t, e):
                        return False, f"{p} unavailable"
        if not self.aligned(t):
            return False, "start not aligned"
        return True, ""

    def local_date(self, t):
        return from_min(t).astimezone(self.same_day_tz).date()

    def _distinct_problems(self, starts: dict, people: dict) -> list:
        problems = []
        seen = {}
        for sid in starts:
            for p in list(self.sessions[sid]["att"]) + list(people.get(sid, [])):
                if p in self.candidate_ids:
                    continue
                if p in seen:
                    problems.append(f"interviewer {p} used in {seen[p]} and {sid}")
                seen.setdefault(p, sid)
        return problems

    def check_assignment(self, starts: dict, people: dict) -> list:
        problems = []
        if sorted(starts) != sorted(self.sessions):
            return [f"example must specify every session {sorted(self.sessions)}"]
        for sid, t in starts.items():
            if self.sessions[sid]["pool"] and sid not in people:
                problems.append(f"{sid}: pool session example must list interviewers")
                continue
            ok, why = self.session_ok(sid, t, people.get(sid) if self.sessions[sid]["pool"] else None)
            if not ok:
                problems.append(f"{sid}: {why}")
        seq = self.order if self.order else sorted(starts, key=lambda x: starts[x])
        for a, b in zip(seq, seq[1:]):
            gap = starts[b] - (starts[a] + self.sessions[a]["d"])
            if gap < self.min_gap:
                problems.append(f"gap {a}->{b} {gap}m < min {self.min_gap}m")
            if self.max_gap is not None and gap > self.max_gap:
                problems.append(f"gap {a}->{b} {gap}m > max {self.max_gap}m")
        first = min(starts.values())
        last = max(starts[s] + self.sessions[s]["d"] for s in starts)
        if self.max_span is not None and last - first > self.max_span:
            problems.append(f"span {last - first}m > max {self.max_span}m")
        if self.same_day_tz:
            dates = {self.local_date(starts[s]) for s in starts}
            dates |= {self.local_date(starts[s] + self.sessions[s]["d"] - 1) for s in starts}
            if len(dates) > 1:
                problems.append("sessions are not on one local day")
        if self.distinct:
            problems.extend(self._distinct_problems(starts, people))
        return problems

    def check_sessions(self, starts: dict, people: dict) -> list:
        """Check each given session on its own (availability, window, pool, alignment, distinctness)."""
        problems = []
        for sid, t in starts.items():
            if sid not in self.sessions:
                problems.append(f"unknown oracle session {sid}")
                continue
            if self.sessions[sid]["pool"] and sid not in people:
                problems.append(f"{sid}: pool session must list its interviewers")
                continue
            ok, why = self.session_ok(sid, t, people.get(sid) if self.sessions[sid]["pool"] else None)
            if not ok:
                problems.append(f"{sid}: {why}")
        if self.distinct:
            problems.extend(self._distinct_problems({s: t for s, t in starts.items() if s in self.sessions}, people))
        return problems

    def check_partial(self, starts: dict, people: dict) -> list:
        """Check an assignment that may cover only some sessions.

        Full assignments use check_assignment. For a partial assignment every given session must be
        valid on its own and the remaining sessions must have a valid completion around the fixed
        starts (the same search the dataset validator uses). Returns problems; empty means valid.
        """
        unknown = sorted(s for s in starts if s not in self.sessions)
        if unknown:
            return [f"unknown oracle session(s) {unknown}"]
        if not starts:
            return ["no sessions to check"]
        if sorted(starts) == sorted(self.sessions):
            return self.check_assignment(starts, people)
        problems = self.check_sessions(starts, people)
        if problems:
            return problems
        if self.search(fixed=starts) is None:
            problems.append(
                f"sessions {sorted(starts)} cannot be completed into a valid schedule for {sorted(self.sessions)} "
                "(order, gap, span, same-day, or availability constraints)"
            )
        return problems

    def base_starts(self):
        if self._base is None:
            self._base = {}
            for sid, sess in self.sessions.items():
                d, out, t = sess["d"], [], self.window[0]
                while t + d <= self.window[1]:
                    if self.session_ok(sid, t)[0]:
                        out.append(t)
                    t += self.gran
                self._base[sid] = out
        return self._base

    def search(self, fixed=None):
        fixed = fixed or {}
        base = {}
        for sid, lst in self.base_starts().items():
            if sid in fixed:
                base[sid] = [fixed[sid]] if self.session_ok(sid, fixed[sid])[0] else []
            else:
                base[sid] = lst
        if any(not v for v in base.values()):
            return None
        orders = [self.order] if self.order else list(itertools.permutations(self.sessions))
        day_filters = [None]
        if self.same_day_tz:
            day_filters = sorted({self.local_date(t) for lst in base.values() for t in lst})
        for day in day_filters:
            if day is None:
                valid = base
            else:
                valid = {sid: [t for t in lst if self.local_date(t) == day
                               and self.local_date(t + self.sessions[sid]["d"] - 1) == day]
                         for sid, lst in base.items()}
            for order in orders:
                sol = self._dfs(list(order), valid)
                if sol:
                    return sol
        return None

    def _dfs(self, order, valid):
        memo = {}
        durs = {s: self.sessions[s]["d"] for s in order}

        def rec(i, prev_end, first):
            if i == len(order):
                return {}
            key = (i, prev_end, first if self.max_span is not None else None)
            if key in memo:
                return memo[key]
            sid = order[i]
            starts = valid[sid]
            lo = prev_end + self.min_gap if i else -(10 ** 12)
            hi = prev_end + self.max_gap if (i and self.max_gap is not None) else 10 ** 12
            j = bisect.bisect_left(starts, lo)
            result = None
            while j < len(starts) and starts[j] <= hi:
                t = starts[j]
                f = t if i == 0 else first
                if self.max_span is not None and t + durs[sid] - f > self.max_span:
                    if i:
                        break
                    j += 1
                    continue
                rest = rec(i + 1, t + durs[sid], f)
                if rest is not None:
                    result = {sid: t, **rest}
                    break
                j += 1
            memo[key] = result
            return result

        return rec(0, 0, 0)


def parse_example(ex):
    starts, people = {}, {}
    for sid, val in (ex or {}).items():
        if sid in {"note", "notes"}:
            continue
        if isinstance(val, dict):
            starts[sid] = to_min(parse_aware(val["start"]))
            if "interviewers" in val:
                people[sid] = list(val["interviewers"])
        else:
            starts[sid] = to_min(parse_aware(val))
    return starts, people
