"""Resolve each participant's availability for a search window, with provenance.

Rules (requirements 4.1b, 7.1, 7.5; AVL-01..15):
- Candidate availability comes only from what the candidate stated (never a candidate calendar).
- Supplied interviewer windows are affirmative availability within their scope; no calendar check is required.
- Calendar observations: busy, oof, tentative (by default), private_busy and unknown-status events block time;
  free and working_elsewhere do not. Only provider status "ok" turns absence of events into free time.
- Unknown is never free. Missing working hours or time zones use the fallbacks in fallbacks.py, each recorded
  as a disclosed assumption.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import intervals as iv
from .fallbacks import DEFAULT_POLICY, FallbackPolicy
from .timeutil import WEEKDAYS, from_min, iso, is_zone, parse_ts, to_min, try_parse_ts, weekday_index, zone

INTERVIEW_RE = re.compile(r"\b(interview|phone screen|screening|panel|debrief loop)\b", re.I)
BLOCKING_STATUSES = {"busy", "oof", "tentative", "private_busy", "unknown", "out_of_office", "away"}
NON_BLOCKING_STATUSES = {"free", "working_elsewhere", "workingelsewhere"}


@dataclass
class Participant:
    id: str
    name: str
    kind: str  # candidate | interviewer
    time_zone: str | None
    email: str | None = None
    raw: dict = field(default_factory=dict)


class Directory:
    """Resolves ids, names and emails of the people named in a case."""

    def __init__(self, inputs: dict):
        self.people: dict = {}
        cands = []
        if isinstance(inputs.get("candidate"), dict):
            cands.append(inputs["candidate"])
        cands += [c for c in inputs.get("candidates") or [] if isinstance(c, dict)]
        for c in cands:
            pid = str(c.get("id") or "candidate")
            self.people[pid] = Participant(pid, str(c.get("name") or pid), "candidate", c.get("time_zone"),
                                           c.get("email"), c)
        for key in ("recruiter", "acting_user"):
            r = inputs.get(key)
            if isinstance(r, dict) and r.get("id") and str(r["id"]) not in self.people:
                email = r.get("email") or r.get("mailbox")
                self.people[str(r["id"])] = Participant(str(r["id"]), str(r.get("name") or r["id"]), "staff",
                                                        r.get("time_zone"), email, r)
        for i in inputs.get("interviewers") or []:
            if isinstance(i, dict) and i.get("id"):
                pid = str(i["id"])
                self.people[pid] = Participant(pid, str(i.get("name") or pid), "interviewer", i.get("time_zone"),
                                               i.get("email"), i)

    def resolve(self, value) -> str | None:
        if value is None:
            return None
        s = str(value).strip()
        if s in self.people:
            return s
        low = s.lower()
        matches = [p.id for p in self.people.values()
                   if low in {p.name.lower(), str(p.email or "").lower()} or p.name.lower().split()[0] == low]
        return matches[0] if len(matches) == 1 else None

    @property
    def candidate_ids(self) -> set:
        return {p.id for p in self.people.values() if p.kind == "candidate"}


def _windows(items) -> list:
    out = []
    for w in items or []:
        if isinstance(w, dict):
            s, e = try_parse_ts(w.get("start")), try_parse_ts(w.get("end"))
        elif isinstance(w, (list, tuple)) and len(w) == 2:
            s, e = try_parse_ts(w[0]), try_parse_ts(w[1])
        else:
            continue
        if s is not None and e is not None and e > s:
            out.append((to_min(s), to_min(e)))
    return iv.normalize(out)


def working_hour_intervals(wh, default_tz: str | None, window: tuple) -> tuple:
    """Expand working hours (dict or list of dicts) into intervals inside window. Returns (intervals, notes)."""
    notes = []
    specs = wh if isinstance(wh, list) else [wh]
    out = []
    ws, we = window
    for spec in specs:
        if not isinstance(spec, dict):
            continue
        tz = spec.get("time_zone") if is_zone(spec.get("time_zone")) else default_tz
        if not tz or not is_zone(tz):
            notes.append("working hours have no usable time zone; not applied")
            continue
        days = spec.get("days") or DEFAULT_POLICY.working_days
        day_idx = {weekday_index(d) for d in days} - {None}
        try:
            sh, sm = (int(x) for x in str(spec.get("start", "08:00")).split(":")[:2])
            eh, em = (int(x) for x in str(spec.get("end", "18:00")).split(":")[:2])
        except ValueError:
            notes.append(f"unreadable working hours {spec!r}; not applied")
            continue
        z = zone(tz)
        day = from_min(ws, tz).date() - timedelta(days=1)
        last = from_min(we, tz).date() + timedelta(days=1)
        while day <= last:
            if day.weekday() in day_idx:
                start = datetime(day.year, day.month, day.day, sh, sm, tzinfo=z)
                end = (datetime(day.year, day.month, day.day, 0, 0, tzinfo=z) + timedelta(days=1)
                       if (eh, em) == (24, 0) else datetime(day.year, day.month, day.day, eh, em, tzinfo=z))
                if end > start:
                    out.append((to_min(start), to_min(end)))
            day += timedelta(days=1)
    return iv.intersect(out, [window]), notes


def _fmt(intervals, tz) -> list:
    return [{"start": iso(from_min(s, tz)), "end": iso(from_min(e, tz))} for s, e in intervals]


@dataclass
class Availability:
    participant_id: str
    source: str  # stated | supplied | calendar | mixed | override | none
    free: list
    blocked: list = field(default_factory=list)   # [{start,end,status,subject?}] intervals in minutes
    coverage: str = "complete"                     # complete | partial | unknown
    notes: list = field(default_factory=list)
    assumptions: list = field(default_factory=list)
    time_zone: str | None = None                   # the participant's zone (for per-day rules such as caps)

    def summary(self, tz: str | None) -> dict:
        tz = tz if tz and is_zone(tz) else "UTC"
        return {
            "participant_id": self.participant_id,
            "source": self.source,
            "coverage": self.coverage,
            "free": _fmt(self.free, tz),
            "blocked": [{"start": iso(from_min(b["s"], tz)), "end": iso(from_min(b["e"], tz)),
                         "status": b["status"]} for b in self.blocked],
            "notes": self.notes,
            "assumptions": self.assumptions,
        }

    def status_at(self, s: int, e: int) -> str:
        """Why [s, e) is not free: the strongest blocking status, 'outside_hours', 'unknown', or 'free'."""
        if iv.contains(self.free, s, e):
            return "free"
        hits = [b["status"] for b in self.blocked if b["s"] < e and s < b["e"]]
        for st in ("oof", "busy", "private_busy", "tentative", "buffer", "unknown", "supplied_block"):
            if st in hits:
                return st
        if self.coverage == "unknown":
            return "unknown"
        return hits[0] if hits else "outside_available_hours"


def resolve_availability(inputs: dict, provider, directory: Directory, participant_ids, window: tuple,
                         overrides: dict | None = None, tentative_policy: str = "block",
                         policy: FallbackPolicy | None = None) -> dict:
    """Build Availability for each participant over window (minute interval)."""
    overrides = overrides or {}
    policy = policy or DEFAULT_POLICY
    out: dict = {}
    ws, we = window
    calendar_ids = []
    for pid in participant_ids:
        p = directory.people.get(pid)
        ov = overrides.get(pid) or {}
        if p is None:
            out[pid] = Availability(pid, "none", [], coverage="unknown", notes=["unknown participant"])
            continue
        if "windows" in ov:
            free = iv.intersect(_windows(ov["windows"]), [window])
            out[pid] = Availability(pid, "override", free, notes=[ov.get("reason") or "windows supplied in this case"])
        elif p.kind == "candidate":
            free = iv.intersect(_windows(p.raw.get("availability")), [window])
            if not p.raw.get("availability"):
                out[pid] = Availability(pid, "none", [], coverage="unknown",
                                        notes=["no structured candidate availability"])
            else:
                out[pid] = Availability(pid, "stated", free)
        else:
            src = str(p.raw.get("availability_source") or "calendar")
            supplied = _windows(p.raw.get("supplied_availability"))
            if src == "supplied" or (supplied and src not in ("calendar", "mixed")):
                cov = "complete" if supplied else "unknown"
                out[pid] = Availability(pid, "supplied", iv.intersect(supplied, [window]), coverage=cov,
                                        notes=[] if supplied else ["availability is supplied but no windows were given"])
            elif src == "none":
                out[pid] = Availability(pid, "none", [], coverage="unknown", notes=["no availability source"])
            else:
                calendar_ids.append(pid)
                out[pid] = Availability(pid, src, iv.intersect(supplied, [window]))
        for blk in ov.get("add_blocks") or []:
            bs, be = (_windows([blk]) or [(None, None)])[0]
            if bs is not None:
                out[pid].free = iv.subtract(out[pid].free, [(bs, be)])
                out[pid].blocked.append({"s": bs, "e": be, "status": blk.get("status", "supplied_block")})

    if calendar_ids:
        result = provider.get_schedule(calendar_ids, iso(from_min(ws)), iso(from_min(we)))
        by_id = {}
        for entry in result or []:
            rid = directory.resolve(entry.get("participant_id")) or entry.get("participant_id")
            by_id[rid] = entry
        for pid in calendar_ids:
            p = directory.people[pid]
            a = out[pid]
            ov = overrides.get(pid) or {}
            if "windows" in ov:
                continue
            entry = by_id.get(pid) or {"status": "unknown"}
            status = entry.get("status")
            if status != "ok":
                a.notes.append(f"calendar status {status}: {entry.get('detail') or 'no detail'}")
                if not a.free:
                    a.coverage = "unknown"
                else:
                    a.coverage = "partial"
                    a.source = "supplied"
                continue
            tz, tz_assumption = policy.resolve_time_zone(p.raw, inputs, ov.get("time_zone"))
            if tz_assumption:
                a.assumptions.append(tz_assumption)
            if tz is None:
                a.coverage = "unknown"
                a.notes.append("time zone unknown and no location, plan, or recruiter zone to fall back on")
                continue
            p.time_zone = tz
            a.time_zone = tz
            wh = ov.get("working_hours") or p.raw.get("working_hours") or p.raw.get("acceptable_hours")                 or entry.get("working_hours")  # e.g. Outlook working hours reported by Microsoft Graph
            if not wh:
                wh = policy.working_hours()
                a.assumptions.append(f"{p.name}'s working hours are not set; assumed {policy.describe_hours(tz)}")
            hours, notes = working_hour_intervals(wh, tz, window)
            a.notes += notes
            blocked = []
            for ev in entry.get("events") or []:
                s, e = try_parse_ts(ev.get("start")), try_parse_ts(ev.get("end"))
                if s is None or e is None:
                    continue
                st = str(ev.get("status") or "busy").lower()
                if st in NON_BLOCKING_STATUSES:
                    continue
                if st == "tentative" and tentative_policy == "allow":
                    a.blocked.append({"s": to_min(s), "e": to_min(e), "status": "tentative_allowed"})
                    continue
                blocked.append((to_min(s), to_min(e)))
                a.blocked.append({"s": to_min(s), "e": to_min(e),
                                  "status": st if st in BLOCKING_STATUSES else "busy",
                                  "interview": bool(INTERVIEW_RE.search(str(ev.get("subject") or "")))})
            cal_free = iv.subtract(hours, blocked)
            if a.free:  # mixed: supplied windows plus calendar; calendar conflicts inside supplied windows block
                conflicts = iv.intersect(a.free, blocked)
                if conflicts:
                    a.notes.append("calendar shows commitments inside supplied windows: "
                                   + ", ".join(f"{iso(from_min(s, tz))}-{iso(from_min(e, tz))}" for s, e in conflicts)
                                   + " (treated as blocked; reconcile with the recruiter)")
                a.free = iv.union(iv.subtract(a.free, blocked), cal_free)
                a.source = "mixed"
            else:
                a.free = cal_free
                a.source = "calendar"
    return out


def availability_window(inputs: dict, start=None, end=None, days: int = 30) -> tuple:
    now = parse_ts(inputs.get("now"))
    s = parse_ts(start) if start else now
    e = parse_ts(end) if end else s + timedelta(days=days)
    return (to_min(s), to_min(e))


__all__ = ["Directory", "Availability", "resolve_availability", "working_hour_intervals", "availability_window",
           "WEEKDAYS"]
