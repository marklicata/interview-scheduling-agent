#!/usr/bin/env python3
"""Validate interview-scheduling evaluation test cases.

Usage:
    python validate_cases.py <file-or-glob> [<file-or-glob> ...] [--quiet]

Checks schema/enums, unique IDs, timestamp offsets, IANA time zones, offset
consistency inside blocks that declare a time_zone, weekday/date text,
local-time checks (including DST gaps/folds), authority/side-effect
consistency, and the optional machine-checkable scheduling oracle.
"""
from __future__ import annotations

import bisect
import glob
import itertools
import re
import sys
from datetime import date, datetime, timedelta, timezone
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
# Enumerations
# --------------------------------------------------------------------------------------

CATEGORIES = {
    "BAS": "basic_scheduling",
    "TZ": "cross_timezone",
    "CAL": "calendar_semantics",
    "SUP": "supplied_availability",
    "NLP": "availability_interpretation",
    "LOOP": "multi_session_loops",
    "GATE": "gated_sequential_rounds",
    "PNL": "panels_and_near_fit",
    "FMT": "formats_and_logistics",
    "STY": "interview_styles_and_industries",
    "CHG": "changes_and_rescheduling",
    "AUTH": "authority_and_autonomy",
    "EXE": "execution_and_recovery",
    "CX": "candidate_experience_and_accommodations",
    "POL": "policy_fairness_privacy",
    "ADV": "adversarial_and_robustness",
    "MEM": "memory_and_learning",
    "INF": "infeasible_and_limits",
    "VOL": "high_volume_and_multi_candidate",
    "COM": "communication_quality",
    "LTR": "later_scope_capabilities",
}
CATEGORY_TO_PREFIX = {v: k for k, v in CATEGORIES.items()}
DIFFICULTY = {"basic", "intermediate", "advanced", "adversarial"}
SEVERITY = {"critical", "high", "medium", "low"}
RELEASE_SCOPE = {"mvp", "later", "out_of_scope"}
INTERVIEW_TYPES = {
    "phone_screen", "virtual_1on1", "in_person_1on1", "virtual_loop", "in_person_loop", "hybrid_loop",
    "panel", "group_interview", "presentation", "case_interview", "technical_coding", "system_design",
    "take_home_review", "assessment_center", "superday", "campus_visit", "job_talk", "as_appropriate",
    "bar_raiser", "hiring_manager", "hr_round", "executive", "informal_meal", "site_tour", "gated_rounds",
    "work_trial", "simulation", "portfolio_review", "teaching_demo", "group_discussion", "other",
}
AUTHORITY_MODES = {
    "confirm_each_action",       # MVP default: propose, then act only on explicit confirmation
    "autonomous_grant_active",   # explicit configuration/chat grant is active; grant object required
    "grant_expired_or_revoked",  # a grant existed but no longer authorizes; grant object required
    "read_only_user",            # the user/organizer lacks booking permission
}
DISPOSITIONS = {
    "propose_options", "book", "reschedule", "cancel", "ask_clarification", "request_availability",
    "conditional_handoff", "report_infeasible", "report_incomplete", "refuse", "decline_unsupported",
    "hold_for_gate", "escalate", "recover_partial", "acknowledge_and_record", "update_memory",
}
EXECUTING_DISPOSITIONS = {"book", "reschedule", "cancel"}
NON_WRITING_DISPOSITIONS = {
    "propose_options", "ask_clarification", "request_availability", "conditional_handoff", "report_infeasible",
    "report_incomplete", "refuse", "decline_unsupported", "hold_for_gate", "update_memory",
}
ACTION_TYPES = {"create_event", "update_event", "cancel_event", "send_message", "create_hold", "publish", "other"}
ROLES = {
    "recruiter", "coordinator", "candidate", "interviewer", "hiring_manager", "admin", "system", "agent",
    "assistant_ea", "other",
}
CALENDAR_STATUSES = {"free", "busy", "tentative", "oof", "working_elsewhere", "unknown", "private_busy"}
REQUIRED_TOP = ["id", "title", "category", "description", "difficulty", "severity", "release_scope",
                "interview_types", "tags", "inputs", "expected"]
REF_RE = re.compile(
    r"^(?:(?:INT|CON|AVL|SCH|ACT|COM|CHG|UX|EVL|ADM|CRD|PUB|CFG|LRN)-\d{2}|EC-\d{3}|GS-\d{3}|INV-\d{2}|J\d{1,2})$"
)
ID_RE = re.compile(r"^([A-Z]{2,4})-(\d{3})$")
SNAKE_RE = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*$")
ISO_FULL_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})?$")
LOCAL_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$")
HHMM_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$|^24:00$")

WEEKDAYS = {
    "monday": 0, "mon": 0, "tuesday": 1, "tues": 1, "tue": 1, "wednesday": 2, "wed": 2,
    "thursday": 3, "thurs": 3, "thur": 3, "thu": 3, "friday": 4, "fri": 4, "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}
MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3, "april": 4, "apr": 4,
    "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7, "august": 8, "aug": 8, "september": 9,
    "sept": 9, "sep": 9, "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}
WD_ALT = "Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday|Mon|Tues|Tue|Wed|Thurs|Thur|Thu|Fri|Sat|Sun"
MON_ALT = ("January|February|March|April|May|June|July|August|September|October|November|December|"
           "Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept|Sep|Oct|Nov|Dec")
WD_MONTH_DAY = re.compile(rf"\b({WD_ALT})\.?,?\s+({MON_ALT})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?:,?\s+(\d{{4}}))?")
WD_DAY_MONTH = re.compile(rf"\b({WD_ALT})\.?,?\s+(\d{{1,2}})(?:st|nd|rd|th)?\s+({MON_ALT})\b\.?(?:,?\s+(\d{{4}}))?")
WD_ISO = re.compile(rf"\b({WD_ALT})\.?,?\s+(\d{{4}})-(\d{{2}})-(\d{{2}})\b")
ISO_WD = re.compile(rf"\b(\d{{4}})-(\d{{2}})-(\d{{2}})\s*\(({WD_ALT})\.?\)")
WD_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------


class Reporter:
    def __init__(self):
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def err(self, cid: str, msg: str):
        self.errors.append(f"[{cid}] {msg}")

    def warn(self, cid: str, msg: str):
        self.warnings.append(f"[{cid}] {msg}")


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


def walk(node, path=""):
    if isinstance(node, dict):
        for k, v in node.items():
            p = f"{path}.{k}" if path else str(k)
            yield p, k, v, node
            yield from walk(v, p)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            p = f"{path}[{i}]"
            yield p, None, v, node
            yield from walk(v, p)


def is_naive_allowed(key, path: str) -> bool:
    k = str(key or "")
    return k.endswith("local") or "local_time_checks" in path


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
    def __init__(self, spec, cid, rep: Reporter):
        self.cid, self.rep, self.ok = cid, rep, True
        self.spec = spec
        self._base = None
        try:
            self._build()
        except (KeyError, ValueError, TypeError) as exc:
            self.ok = False
            rep.err(cid, f"oracle: {exc!r}")

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
            seen = {}
            for sid in starts:
                for p in list(self.sessions[sid]["att"]) + list(people.get(sid, [])):
                    if p in self.candidate_ids:
                        continue
                    if p in seen:
                        problems.append(f"interviewer {p} used in {seen[p]} and {sid}")
                    seen.setdefault(p, sid)
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


def check_oracle(case, rep: Reporter):
    cid = case.get("id", "?")
    spec = case["oracle"]
    if not isinstance(spec, dict):
        rep.err(cid, "oracle must be a mapping")
        return
    o = Oracle(spec, cid, rep)
    if not o.ok:
        return
    try:
        for i, ex in enumerate(spec.get("valid_examples") or []):
            starts, people = parse_example(ex)
            problems = o.check_assignment(starts, people)
            if problems:
                rep.err(cid, f"oracle valid_examples[{i}] is invalid: {'; '.join(problems)}")
        for i, ex in enumerate(spec.get("invalid_examples") or []):
            starts, people = parse_example(ex)
            if not o.check_assignment(starts, people):
                rep.err(cid, f"oracle invalid_examples[{i}] actually satisfies all oracle constraints")
        expect = spec.get("expect_feasible")
        if expect not in (True, False):
            rep.err(cid, "oracle.expect_feasible must be true or false")
        elif spec.get("check_feasibility", True):
            sol = o.search()
            if expect and not sol:
                rep.err(cid, "oracle expects feasible, but no valid schedule exists on the search grid")
            if not expect and sol:
                shown = {k: from_min(v).isoformat() for k, v in sol.items()}
                rep.err(cid, f"oracle expects infeasible, but found {shown}")
        if expect is True and not spec.get("valid_examples"):
            rep.err(cid, "feasible oracle cases must include at least one valid_examples entry")
        for sid, bounds in (spec.get("valid_start_bounds") or {}).items():
            if sid not in o.sessions:
                rep.err(cid, f"valid_start_bounds references unknown session {sid}")
                continue
            feasible = [t for t in o.base_starts()[sid] if o.search(fixed={sid: t})]
            if not feasible:
                rep.err(cid, f"valid_start_bounds for {sid}: no feasible starts")
                continue
            for key, actual in (("earliest", min(feasible)), ("latest", max(feasible))):
                if key in bounds:
                    want = to_min(parse_aware(bounds[key]))
                    if want != actual:
                        rep.err(cid, f"valid_start_bounds {sid}.{key}={bounds[key]} but computed "
                                     f"{from_min(actual).isoformat()} on a {o.gran}-minute grid")
    except (KeyError, ValueError, TypeError) as exc:
        rep.err(cid, f"oracle: {exc!r}")


# --------------------------------------------------------------------------------------
# Case checks
# --------------------------------------------------------------------------------------


def check_local_time_checks(case, rep):
    cid = case.get("id", "?")
    for i, chk in enumerate(case.get("local_time_checks") or []):
        if not isinstance(chk, dict):
            rep.err(cid, f"local_time_checks[{i}] must be a mapping")
            continue
        tz = get_tz(chk.get("time_zone"))
        if tz is None:
            rep.err(cid, f"local_time_checks[{i}] invalid time_zone {chk.get('time_zone')!r}")
            continue
        try:
            if "instant" in chk:
                dt = parse_aware(chk["instant"]).astimezone(tz)
                if "expected_local" in chk:
                    want = str(chk["expected_local"])
                    if not LOCAL_RE.match(want):
                        rep.err(cid, f"local_time_checks[{i}].expected_local must be YYYY-MM-DDTHH:MM")
                    elif dt.strftime("%Y-%m-%dT%H:%M") != want:
                        rep.err(cid, f"local_time_checks[{i}] {chk['instant']} in {chk['time_zone']} is "
                                     f"{dt.strftime('%Y-%m-%dT%H:%M')}, not {want}")
                if "expected_weekday" in chk and WD_NAMES[dt.weekday()].lower() != str(chk["expected_weekday"]).lower():
                    rep.err(cid, f"local_time_checks[{i}] weekday is {WD_NAMES[dt.weekday()]}, not {chk['expected_weekday']}")
                if "expected_utc_offset" in chk and fmt_offset(dt.utcoffset()) != str(chk["expected_utc_offset"]):
                    rep.err(cid, f"local_time_checks[{i}] offset is {fmt_offset(dt.utcoffset())}, "
                                 f"not {chk['expected_utc_offset']}")
            elif "local" in chk:
                local = datetime.fromisoformat(str(chk["local"]))
                d0 = local.replace(tzinfo=tz, fold=0)
                d1 = local.replace(tzinfo=tz, fold=1)
                rt = d0.astimezone(timezone.utc).astimezone(tz).replace(tzinfo=None)
                if rt != local:
                    actual = "nonexistent"
                elif d0.utcoffset() != d1.utcoffset():
                    actual = "ambiguous"
                else:
                    actual = "valid"
                want = chk.get("expect")
                if want not in {"nonexistent", "ambiguous", "valid"}:
                    rep.err(cid, f"local_time_checks[{i}].expect must be nonexistent|ambiguous|valid")
                elif want != actual:
                    rep.err(cid, f"local_time_checks[{i}] {chk['local']} in {chk['time_zone']} is {actual}, not {want}")
            else:
                rep.err(cid, f"local_time_checks[{i}] needs instant or local")
        except ValueError as exc:
            rep.err(cid, f"local_time_checks[{i}]: {exc}")


def _year_candidates(case):
    try:
        y = parse_aware(case["inputs"]["now"]).year
    except Exception:  # noqa: BLE001
        y = 2026
    return [y, y + 1]


def _check_weekday(cid, rep, wd, y, m, d, text, years):
    want = WEEKDAYS[wd.lower().rstrip(".")]
    ys = [int(y)] if y else years
    ok = False
    for yy in ys:
        try:
            ok = ok or date(yy, m, d).weekday() == want
        except ValueError:
            pass
    if not ok:
        snippet = text if len(text) < 160 else text[:157] + "..."
        yr = f"-{y}" if y else f" (years tried {ys})"
        rep.err(cid, f"weekday/date mismatch: '{wd}' is not {m:02d}-{d:02d}{yr} in: {snippet}")


def check_text_weekdays(case, rep):
    cid = case.get("id", "?")
    if "weekday_text" in (case.get("lint_ignore") or []):
        return
    years = _year_candidates(case)
    for _, _, v, _ in walk(case):
        if not isinstance(v, str) or len(v) < 6:
            continue
        for m in WD_MONTH_DAY.finditer(v):
            _check_weekday(cid, rep, m.group(1), m.group(4), MONTHS[m.group(2).lower()], int(m.group(3)), v, years)
        for m in WD_DAY_MONTH.finditer(v):
            _check_weekday(cid, rep, m.group(1), m.group(4), MONTHS[m.group(3).lower()], int(m.group(2)), v, years)
        for m in WD_ISO.finditer(v):
            _check_weekday(cid, rep, m.group(1), m.group(2), int(m.group(3)), int(m.group(4)), v, years)
        for m in ISO_WD.finditer(v):
            _check_weekday(cid, rep, m.group(4), m.group(1), int(m.group(2)), int(m.group(3)), v, years)


def _check_offset(value, tz, cid, rep, path):
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return
    if dt.tzinfo is None or value.endswith("Z"):
        return
    actual = dt.astimezone(tz).utcoffset()
    if dt.utcoffset() != actual:
        rep.err(cid, f"{path}: {value} uses offset {fmt_offset(dt.utcoffset())}, but {tz.key} is "
                     f"{fmt_offset(actual)} at that instant (use that local offset or Z, or set offset_check: false)")


def _offset_scan(node, tz, cid, rep, path):
    if isinstance(node, dict):
        if node.get("offset_check") is False:
            return
        own = get_tz(node.get("time_zone")) if "time_zone" in node else None
        cur = own or tz
        for k, v in node.items():
            p = f"{path}.{k}" if path else str(k)
            if k in {"start", "end"} and isinstance(v, str) and ISO_FULL_RE.match(v) and cur is not None:
                _check_offset(v, cur, cid, rep, p)
            elif k in {"local_time_checks", "oracle", "expected", "conversation"}:
                continue
            else:
                _offset_scan(v, cur, cid, rep, p)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            _offset_scan(v, tz, cid, rep, f"{path}[{i}]")


def check_timestamps_and_zones(case, rep):
    cid = case.get("id", "?")
    for path, key, v, parent in walk(case):
        if key == "time_zone" and get_tz(v) is None:
            rep.err(cid, f"{path}: invalid IANA time zone {v!r} (use time_zone_raw for user-written labels)")
        if isinstance(v, str) and ISO_FULL_RE.match(v):
            try:
                dt = datetime.fromisoformat(v)
            except ValueError as exc:
                rep.err(cid, f"{path}: bad timestamp {v!r}: {exc}")
                continue
            if dt.tzinfo is None and not is_naive_allowed(key, path):
                rep.err(cid, f"{path}: timestamp {v!r} needs an explicit UTC offset or Z")
        if isinstance(v, (int, float)) and not isinstance(v, bool) and key in {"start", "end", "at", "local", "time"}:
            rep.err(cid, f"{path}: numeric value for a time field; quote times as strings")
    _offset_scan(case.get("inputs") or {}, None, cid, rep, "inputs")
    o = case.get("oracle") or {}
    if isinstance(o, dict):
        for name, p in (o.get("participants") or {}).items():
            tz = get_tz((p or {}).get("time_zone"))
            if tz is None or (p or {}).get("offset_check") is False:
                continue
            for key in ("available", "busy"):
                for i, item in enumerate((p or {}).get(key) or []):
                    try:
                        s, e = parse_interval(item)
                    except ValueError:
                        continue
                    for val in (s, e):
                        if isinstance(val, str) and ISO_FULL_RE.match(val):
                            _check_offset(val, tz, cid, rep, f"oracle.participants.{name}.{key}[{i}]")


def check_case(case, rep: Reporter, seen_ids: dict, source: str):
    if not isinstance(case, dict):
        rep.err("?", f"{source}: test case must be a mapping")
        return
    cid = str(case.get("id", "?"))
    for key in REQUIRED_TOP:
        if key not in case:
            rep.err(cid, f"missing required field '{key}'")
    m = ID_RE.match(cid)
    if not m:
        rep.err(cid, "id must look like PREFIX-001")
    else:
        prefix = m.group(1)
        if prefix not in CATEGORIES:
            rep.err(cid, f"unknown id prefix {prefix}")
        elif case.get("category") != CATEGORIES[prefix]:
            rep.err(cid, f"category must be {CATEGORIES[prefix]} for prefix {prefix}")
    if cid in seen_ids:
        rep.err(cid, f"duplicate id (also in {seen_ids[cid]})")
    seen_ids[cid] = source
    if case.get("category") not in CATEGORY_TO_PREFIX:
        rep.err(cid, f"unknown category {case.get('category')!r}")
    for field, allowed in (("difficulty", DIFFICULTY), ("severity", SEVERITY), ("release_scope", RELEASE_SCOPE)):
        if case.get(field) not in allowed:
            rep.err(cid, f"{field} must be one of {sorted(allowed)}")
    if not isinstance(case.get("title"), str) or len(case.get("title", "")) < 8:
        rep.err(cid, "title must be a descriptive string")
    if not isinstance(case.get("description"), str) or len(case.get("description", "")) < 30:
        rep.err(cid, "description must explain what is tested (>= 30 chars)")
    itypes = case.get("interview_types")
    if not isinstance(itypes, list) or not itypes:
        rep.err(cid, "interview_types must be a non-empty list")
    else:
        for t in itypes:
            if t not in INTERVIEW_TYPES:
                rep.err(cid, f"unknown interview_type {t!r}")
    tags = case.get("tags")
    if not isinstance(tags, list) or not tags:
        rep.err(cid, "tags must be a non-empty list")
    else:
        for t in tags:
            if not isinstance(t, str) or not SNAKE_RE.match(t):
                rep.err(cid, f"tag {t!r} must be snake_case")
    for r in case.get("requirement_refs") or []:
        if not isinstance(r, str) or not REF_RE.match(r):
            rep.err(cid, f"requirement_refs entry {r!r} is not a known reference format")

    inputs = case.get("inputs") if isinstance(case.get("inputs"), dict) else {}
    if not inputs:
        rep.err(cid, "inputs must be a non-empty mapping")
    now = inputs.get("now")
    try:
        now_dt = parse_aware(now)
        rtz = get_tz((inputs.get("recruiter") or {}).get("time_zone"))
        if rtz and not str(now).endswith("Z") and now_dt.utcoffset() != now_dt.astimezone(rtz).utcoffset():
            rep.err(cid, f"inputs.now {now} offset does not match recruiter time zone {rtz.key}")
    except Exception:  # noqa: BLE001
        rep.err(cid, "inputs.now must be an ISO timestamp with offset")
    auth = inputs.get("authority") or {}
    mode = auth.get("mode")
    if mode not in AUTHORITY_MODES:
        rep.err(cid, f"inputs.authority.mode must be one of {sorted(AUTHORITY_MODES)}")
    if mode in {"autonomous_grant_active", "grant_expired_or_revoked"}:
        grant = auth.get("grant")
        if not isinstance(grant, dict) or grant.get("route") not in {"configuration", "chat"} or not grant.get("scope"):
            rep.err(cid, "authority.grant requires route: configuration|chat and a non-empty scope")
    for i, turn in enumerate(inputs.get("conversation") or []):
        if not isinstance(turn, dict) or "text" not in turn:
            rep.err(cid, f"conversation[{i}] must have text")
            continue
        if turn.get("role") not in ROLES:
            rep.err(cid, f"conversation[{i}].role must be one of {sorted(ROLES)}")
    for path, key, v, parent in walk(inputs):
        if key == "status" and isinstance(parent, dict) and "start" in parent and "end" in parent \
                and v not in CALENDAR_STATUSES:
            rep.err(cid, f"inputs.{path}: calendar status {v!r} must be one of {sorted(CALENDAR_STATUSES)}")

    exp = case.get("expected") if isinstance(case.get("expected"), dict) else {}
    disp = exp.get("disposition")
    if disp not in DISPOSITIONS:
        rep.err(cid, f"expected.disposition must be one of {sorted(DISPOSITIONS)}")
    for d in exp.get("secondary_dispositions") or []:
        if d not in DISPOSITIONS:
            rep.err(cid, f"secondary disposition {d!r} unknown")
    for field in ("must", "must_not"):
        val = exp.get(field)
        if not isinstance(val, list) or not val or not all(isinstance(x, str) and x.strip() for x in val):
            rep.err(cid, f"expected.{field} must be a non-empty list of assertion strings")
    se = exp.get("side_effects")
    if not isinstance(se, dict) or se.get("external_writes") not in {"none", "as_listed"}:
        rep.err(cid, "expected.side_effects.external_writes must be none|as_listed")
    else:
        actions = se.get("actions") or []
        if se["external_writes"] == "none" and actions:
            rep.err(cid, "side_effects.actions must be empty when external_writes is none")
        if se["external_writes"] == "as_listed":
            if not actions:
                rep.err(cid, "side_effects.actions must list the expected writes")
            for a in actions:
                if not isinstance(a, dict) or a.get("type") not in ACTION_TYPES:
                    rep.err(cid, f"side effect action type must be one of {sorted(ACTION_TYPES)}")
            if mode == "read_only_user":
                rep.err(cid, "read_only_user cases cannot expect external writes")
            if mode == "grant_expired_or_revoked" and not any(
                isinstance(t, dict) and t.get("confirmation") is True for t in inputs.get("conversation") or []
            ):
                rep.err(cid, "expired/revoked grant cases with writes need a fresh confirmation turn")
            if mode == "confirm_each_action" and not any(
                isinstance(t, dict) and t.get("confirmation") is True for t in inputs.get("conversation") or []
            ):
                rep.err(cid, "confirm_each_action cases with writes need a conversation turn marked confirmation: true")
            if case.get("release_scope") == "out_of_scope":
                rep.err(cid, "out_of_scope cases cannot expect writes")
        if disp in EXECUTING_DISPOSITIONS and se["external_writes"] != "as_listed":
            rep.err(cid, f"disposition {disp} requires external_writes: as_listed")
        if disp in NON_WRITING_DISPOSITIONS and se["external_writes"] == "as_listed" \
                and not (set(exp.get("secondary_dispositions") or []) & EXECUTING_DISPOSITIONS):
            rep.err(cid, f"disposition {disp} cannot expect writes unless a secondary disposition is book/reschedule/cancel")

    check_timestamps_and_zones(case, rep)
    check_text_weekdays(case, rep)
    check_local_time_checks(case, rep)
    if case.get("oracle") is not None:
        check_oracle(case, rep)


def iter_files(args):
    files = []
    for a in args:
        matches = glob.glob(a)
        files.extend(sorted(matches) if matches else [a])
    return files


def collect_cases(paths, rep: Reporter):
    seen: dict = {}
    cases = []
    for path in paths:
        try:
            doc = load_yaml(path)
        except (OSError, yaml.YAMLError) as exc:
            rep.err("FILE", f"{path}: {exc}")
            continue
        items = doc.get("test_cases") if isinstance(doc, dict) else None
        if not isinstance(items, list):
            rep.err("FILE", f"{path}: top-level 'test_cases' list is required")
            continue
        for case in items:
            check_case(case, rep, seen, path)
            cases.append(case)
    return cases


def main(argv):
    quiet = "--quiet" in argv
    args = [a for a in argv if not a.startswith("--")]
    if not args:
        print(__doc__)
        return 2
    rep = Reporter()
    cases = collect_cases(iter_files(args), rep)
    by_cat: dict = {}
    oracle_count = 0
    for c in cases:
        if isinstance(c, dict):
            by_cat[c.get("category", "?")] = by_cat.get(c.get("category", "?"), 0) + 1
            oracle_count += 1 if c.get("oracle") else 0
    if not quiet:
        for w in rep.warnings:
            print("WARN ", w)
    for e in rep.errors:
        print("ERROR", e)
    print(f"\n{len(cases)} cases; {oracle_count} with oracle; {len(rep.errors)} errors; {len(rep.warnings)} warnings")
    for cat, n in sorted(by_cat.items()):
        print(f"  {cat}: {n}")
    return 1 if rep.errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
