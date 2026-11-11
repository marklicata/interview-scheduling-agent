"""Deterministic schedule search, validation, and near-fit analysis.

The model never invents a "valid" schedule: options presented as valid come from `search` or pass `check`.
Hard constraints (requirements 7.2, 7.4, CON-01..12, SCH-01..08):
- every required session, full duration, inside every required attendee's available time;
- a panel needs all of its interviewers for the whole interval; a pool needs `count` distinct eligible members;
- the candidate attends sessions one at a time, in the required order, with required breaks;
- an interviewer is never in two overlapping sessions (plus optional interviewer buffer);
- locked/fixed sessions keep their time; minimum notice and overall bounds are respected.
Exact boundaries are kept (a 09:07 start is valid if availability allows it).
Ranking among valid schedules follows the default preference order in requirements 7.6.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field

from . import intervals as iv
from .timeutil import WEEKDAYS, from_min, is_zone, iso, parse_ts, to_min

MAX_COMPLETE = 400
MAX_STARTS_PER_SESSION = 6


@dataclass
class SessionSpec:
    session_id: str
    duration_min: int
    interviewers: list = field(default_factory=list)  # all required for the full interval
    pool: list = field(default_factory=list)          # eligible members
    pool_count: int = 0
    order: int | None = None
    format: str | None = None
    windows: list = field(default_factory=list)       # optional session-specific allowed windows (minutes)
    fixed_start: int | None = None                    # locked session


@dataclass
class Constraints:
    granularity_min: int = 5
    min_break_min: int = 0
    max_break_min: int | None = None
    same_day: bool = False
    ordered: bool = True
    not_before: int | None = None
    not_after: int | None = None
    interviewer_buffer_min: int = 0
    max_sessions_per_interviewer_per_day: int | None = None
    max_candidate_minutes_per_day: int | None = None
    max_day_span_min: int | None = None              # candidate's day: first start to last end
    distinct_interviewers: bool = False
    preferred_time_of_day: str = "any"               # morning | afternoon | any (ranking only)
    day_time_zone: str = "UTC"


def parse_spec(sessions: list, constraints: dict | None, now_min: int, day_tz: str) -> tuple:
    specs = []
    for i, s in enumerate(sessions or []):
        pool = s.get("pool") or {}
        if isinstance(pool, list):
            pool = {"from": pool, "count": 1}
        windows = []
        for w in s.get("windows") or []:
            windows.append((to_min(parse_ts(w["start"])), to_min(parse_ts(w["end"]))))
        specs.append(SessionSpec(
            session_id=str(s["session_id"]),
            duration_min=int(s["duration_min"]),
            interviewers=[str(x) for x in s.get("interviewers") or []],
            pool=[str(x) for x in pool.get("from") or []],
            pool_count=int(pool.get("count") or (1 if pool.get("from") else 0)),
            order=s.get("order"),
            format=s.get("format"),
            windows=iv.normalize(windows),
            fixed_start=to_min(parse_ts(s["fixed_start"])) if s.get("fixed_start") else None,
        ))
    c = dict(constraints or {})
    cons = Constraints(
        granularity_min=max(1, int(c.get("granularity_min") or 5)),
        min_break_min=int(c.get("min_break_min") or 0),
        max_break_min=c.get("max_break_min"),
        same_day=bool(c.get("same_day", False)),
        ordered=bool(c.get("ordered", any(s.order is not None for s in specs) or len(specs) <= 1)),
        not_before=to_min(parse_ts(c["not_before"])) if c.get("not_before") else None,
        not_after=to_min(parse_ts(c["not_after"])) if c.get("not_after") else None,
        interviewer_buffer_min=int(c.get("interviewer_buffer_min") or 0),
        max_sessions_per_interviewer_per_day=c.get("max_sessions_per_interviewer_per_day"),
        max_candidate_minutes_per_day=c.get("max_candidate_minutes_per_day"),
        max_day_span_min=c.get("max_day_span_min"),
        distinct_interviewers=bool(c.get("distinct_interviewers", False)),
        preferred_time_of_day=str(c.get("preferred_time_of_day") or "any"),
        day_time_zone=day_tz if is_zone(day_tz) else "UTC",
    )
    if c.get("min_notice_hours"):
        floor = now_min + int(float(c["min_notice_hours"]) * 60)
        cons.not_before = max(cons.not_before or floor, floor)
    else:
        cons.not_before = max(cons.not_before or now_min, now_min)
    return specs, cons


class Engine:
    def __init__(self, availability: dict, candidate_id: str, specs: list, cons: Constraints):
        self.av = availability
        self.cand = candidate_id
        self.specs = specs
        self.cons = cons
        if cons.interviewer_buffer_min:
            self._buffer_existing_commitments(cons.interviewer_buffer_min)

    def _buffer_existing_commitments(self, buf: int) -> None:
        """Keep `buf` minutes clear before and after each interviewer's existing commitments (buffer policies)."""
        interviewers = {p for s in self.specs for p in s.interviewers + s.pool}
        for p in interviewers:
            a = self.av.get(p)
            if a is None:
                continue
            pads = []
            for b in list(a.blocked):
                if b["status"] in ("tentative_allowed", "buffer"):
                    continue
                pads += [(b["s"] - buf, b["s"]), (b["e"], b["e"] + buf)]
            pads = iv.normalize(pads)
            removed = iv.intersect(a.free, pads)
            if removed:
                a.free = iv.subtract(a.free, pads)
                a.blocked += [{"s": s, "e": e, "status": "buffer"} for s, e in removed]

    # ------------------------------------------------------------------ helpers
    def _free(self, pid):
        a = self.av.get(pid)
        return a.free if a else []

    def _day(self, m: int):
        return from_min(m, self.cons.day_time_zone).date()

    def _base_intervals(self, spec: SessionSpec) -> list:
        free = self._free(self.cand)
        for p in spec.interviewers:
            free = iv.intersect(free, self._free(p))
        if spec.windows:
            free = iv.intersect(free, spec.windows)
        lo = self.cons.not_before if self.cons.not_before is not None else -10 ** 12
        hi = self.cons.not_after if self.cons.not_after is not None else 10 ** 12
        return iv.intersect(free, [(lo, hi)])

    def _starts(self, intervals, d: int, earliest: int, latest: int | None) -> list:
        g = self.cons.granularity_min
        out = []
        for a, b in intervals:
            lo = max(a, earliest)
            hi = b - d
            if latest is not None:
                hi = min(hi, latest)
            if lo > hi:
                continue
            pts = {lo, hi}
            first = -(-lo // g) * g
            pts.update(range(first, hi + 1, g))
            out.extend(sorted(pts))
        return out

    def _pool_pick(self, spec, s, e, busy):
        if not spec.pool_count:
            return []
        chosen = []
        for p in spec.pool:
            if p in spec.interviewers:
                continue
            if iv.contains(self._free(p), s, e) and not self._person_busy(p, s, e, busy):
                chosen.append(p)
            if len(chosen) == spec.pool_count:
                return chosen
        return None

    def _person_busy(self, p, s, e, busy) -> bool:
        if self.cons.distinct_interviewers and busy.get(p):
            return True
        buf = self.cons.interviewer_buffer_min
        return any(bs - buf < e and s < be + buf for bs, be in busy.get(p, []))

    def _person_day(self, p, m: int):
        a = self.av.get(p)
        tz = a.time_zone if a is not None and a.time_zone else self.cons.day_time_zone
        return from_min(m, tz).date()

    def existing_interviews(self, p, day) -> int:
        """Interviews already on the person's calendar that day (their local calendar day)."""
        a = self.av.get(p)
        if a is None:
            return 0
        return sum(1 for b in a.blocked if b.get("interview") and self._person_day(p, b["s"]) == day)

    def _caps_ok(self, people, s, busy) -> bool:
        """Daily interview cap per interviewer, counted on the interviewer's own local calendar day, including
        interviews already on their calendar."""
        cap = self.cons.max_sessions_per_interviewer_per_day
        if not cap:
            return True
        for p in people:
            day = self._person_day(p, s)
            planned = sum(1 for bs, _ in busy.get(p, []) if self._person_day(p, bs) == day)
            if self.existing_interviews(p, day) + planned >= int(cap):
                return False
        return True

    # ------------------------------------------------------------------ search
    def search(self, max_options: int = 3) -> dict:
        if not self.specs:
            return {"options": [], "problem": "no sessions to schedule"}
        orders = [sorted(self.specs, key=lambda s: (s.order if s.order is not None else 10 ** 6))]
        if not self.cons.ordered and 1 < len(self.specs) <= 4:
            orders = [list(p) for p in itertools.permutations(self.specs)]
        complete = []
        base = {s.session_id: self._base_intervals(s) for s in self.specs}
        for seq in orders:
            self._dfs(seq, 0, [], {}, base, complete)
            if len(complete) >= MAX_COMPLETE:
                break
        ranked = self._rank(complete)
        chosen, seen_days = [], set()

        def tod(opt):  # local time of day of the first session, in minutes
            d = from_min(opt[0]["s"], self.cons.day_time_zone)
            return d.hour * 60 + d.minute

        # Prefer distinct days, and among those, different times of day (a real choice, not 8:00 three days
        # running); the first option is always the best-ranked schedule.
        for need_new_time in (True, False):
            for opt in ranked:
                key = tuple(sorted({self._day(a["s"]) for a in opt}))
                if key in seen_days or len(chosen) == max_options:
                    continue
                if need_new_time and chosen and any(abs(tod(opt) - tod(c)) < 120 for c in chosen):
                    continue
                seen_days.add(key)
                chosen.append(opt)
            if len(chosen) == max_options:
                break
        if len(chosen) < max_options:
            # Same-day alternatives must be meaningfully different: starts at least one interview-length apart
            # (11:00 / 11:05 / 11:10 is one choice, not three).
            gap = max(30, min(s.duration_min for s in self.specs))
            for opt in ranked:
                if opt not in chosen and all(abs(opt[0]["s"] - c[0]["s"]) >= gap for c in chosen):
                    chosen.append(opt)
                if len(chosen) == max_options:
                    break
        return {"options": [self._render(o) for o in chosen], "valid_schedules_found": len(complete),
                "search_exhausted": len(complete) < MAX_COMPLETE}

    def _dfs(self, seq, k, assigned, busy, base, complete):
        if len(complete) >= MAX_COMPLETE:
            return
        if k == len(seq):
            complete.append(list(assigned))
            return
        spec = seq[k]
        d = spec.duration_min
        if assigned:
            prev_end = assigned[-1]["e"]
            earliest = prev_end + self.cons.min_break_min
            latest = prev_end + int(self.cons.max_break_min) if self.cons.max_break_min is not None else None
        else:
            earliest, latest = -10 ** 12, None
        if spec.fixed_start is not None:
            starts = [spec.fixed_start] if spec.fixed_start >= earliest else []
        else:
            starts = self._starts(base[spec.session_id], d, earliest, latest)
        tried = 0
        first_day = self._day(assigned[0]["s"]) if assigned else None
        for s in starts:
            e = s + d
            if spec.fixed_start is None and not iv.contains(base[spec.session_id], s, e):
                continue
            if self.cons.same_day and first_day is not None and self._day(s) != first_day:
                if self._day(s) > first_day:
                    break
                continue
            if any(self._person_busy(p, s, e, busy) for p in spec.interviewers):
                continue
            pool = self._pool_pick(spec, s, e, busy)
            if pool is None:
                continue
            people = spec.interviewers + pool
            if not self._caps_ok(people, s, busy):
                continue
            if self.cons.max_candidate_minutes_per_day:
                day_total = sum(a["e"] - a["s"] for a in assigned if self._day(a["s"]) == self._day(s)) + d
                if day_total > int(self.cons.max_candidate_minutes_per_day):
                    continue
            if self.cons.max_day_span_min:
                same = [a for a in assigned if self._day(a["s"]) == self._day(s)]
                if same and max(e, *(a["e"] for a in same)) - min(s, *(a["s"] for a in same)) > int(
                        self.cons.max_day_span_min):
                    continue
            assigned.append({"spec": spec, "s": s, "e": e, "people": people})
            for p in people:
                busy.setdefault(p, []).append((s, e))
            n_before = len(complete)
            self._dfs(seq, k + 1, assigned, busy, base, complete)
            for p in people:
                busy[p].pop()
            assigned.pop()
            if len(complete) > n_before:
                tried += 1
            if k > 0 and tried >= MAX_STARTS_PER_SESSION:
                break
            if len(complete) >= MAX_COMPLETE:
                return

    def _rank(self, complete):
        def key(opt):
            days = {self._day(a["s"]) for a in opt}
            span = sum(max(a["e"] for a in opt if self._day(a["s"]) == d) -
                       min(a["s"] for a in opt if self._day(a["s"]) == d) for d in days)
            pref = self.cons.preferred_time_of_day
            hour = from_min(opt[0]["s"], self.cons.day_time_zone).hour
            miss = 0 if pref == "any" else int((hour < 12) != (pref == "morning"))
            return (len(days), miss, span, opt[-1]["e"], opt[0]["s"])
        uniq, seen = [], set()
        for opt in sorted(complete, key=key):
            sig = tuple((a["spec"].session_id, a["s"], tuple(sorted(a["people"]))) for a in opt)
            if sig not in seen:
                seen.add(sig)
                uniq.append(opt)
        return uniq

    def _render(self, opt) -> dict:
        sessions = sorted(({"session_id": a["spec"].session_id, "start": iso(from_min(a["s"], self.cons.day_time_zone)),
                            "end": iso(from_min(a["e"], self.cons.day_time_zone)), "interviewers": list(a["people"]),
                            **({"format": a["spec"].format} if a["spec"].format else {})} for a in opt),
                          key=lambda x: x["start"])
        return {"sessions": sessions, "candidate_days": len({self._day(a["s"]) for a in opt})}

    # ------------------------------------------------------------------ validation
    def check(self, assignments: list) -> dict:
        """Validate a concrete schedule [{session_id, start, end, interviewers}]."""
        problems = []
        by_id = {s.session_id: s for s in self.specs}
        rows = []
        for a in assignments:
            sid = str(a.get("session_id"))
            spec = by_id.get(sid)
            if spec is None:
                problems.append(f"{sid}: not a session in the plan being checked")
                continue
            s, e = to_min(parse_ts(a["start"])), to_min(parse_ts(a["end"]))
            rows.append((spec, s, e, [str(x) for x in a.get("interviewers") or []]))
        missing = set(by_id) - {r[0].session_id for r in rows}
        if missing:
            problems.append(f"missing required sessions: {sorted(missing)}")
        rows.sort(key=lambda r: r[1])
        for spec, s, e, people in rows:
            if e - s != spec.duration_min:
                problems.append(f"{spec.session_id}: lasts {e - s} min; the plan requires {spec.duration_min}")
            if spec.fixed_start is not None and s != spec.fixed_start:
                problems.append(f"{spec.session_id}: is locked at {iso(from_min(spec.fixed_start, self.cons.day_time_zone))}")
            if self.cons.not_before is not None and s < self.cons.not_before:
                problems.append(f"{spec.session_id}: starts before the earliest allowed time (now/minimum notice)")
            if self.cons.not_after is not None and e > self.cons.not_after:
                problems.append(f"{spec.session_id}: ends after the latest allowed time")
            if spec.windows and not iv.contains(spec.windows, s, e):
                problems.append(f"{spec.session_id}: outside the session's allowed window")
            for p in [self.cand] + spec.interviewers:
                st = self.av[p].status_at(s, e) if p in self.av else "unknown"
                if st != "free":
                    problems.append(f"{spec.session_id}: {p} is not available ({st})")
            for p in spec.interviewers:
                if p not in people:
                    problems.append(f"{spec.session_id}: required interviewer {p} is missing")
            if spec.pool_count:
                chosen = [p for p in people if p in spec.pool]
                if len(set(chosen)) != spec.pool_count:
                    problems.append(f"{spec.session_id}: needs exactly {spec.pool_count} from pool {spec.pool}; "
                                    f"got {chosen}")
                for p in chosen:
                    st = self.av[p].status_at(s, e) if p in self.av else "unknown"
                    if st != "free":
                        problems.append(f"{spec.session_id}: pool member {p} is not available ({st})")
        for (a, b) in zip(rows, rows[1:]):
            if b[1] < a[2] + self.cons.min_break_min:
                problems.append(f"{a[0].session_id} -> {b[0].session_id}: candidate needs "
                                f"{self.cons.min_break_min} min between sessions")
            if self.cons.max_break_min is not None and self._day(a[1]) == self._day(b[1]) \
                    and b[1] - a[2] > int(self.cons.max_break_min):
                problems.append(f"{a[0].session_id} -> {b[0].session_id}: gap exceeds {self.cons.max_break_min} min")
        if self.cons.ordered:
            ordered = sorted(rows, key=lambda r: (r[0].order if r[0].order is not None else 10 ** 6))
            if [r[0].session_id for r in ordered] != [r[0].session_id for r in rows] and \
                    any(r[0].order is not None for r in rows):
                problems.append("sessions are not in the required order")
        if self.cons.max_day_span_min:
            for day in {self._day(r[1]) for r in rows}:
                same = [r for r in rows if self._day(r[1]) == day]
                span = max(r[2] for r in same) - min(r[1] for r in same)
                if span > int(self.cons.max_day_span_min):
                    problems.append(f"the candidate's day on {day} spans {span} min; the limit is "
                                    f"{self.cons.max_day_span_min}")
        if self.cons.same_day and len({self._day(r[1]) for r in rows}) > 1:
            problems.append("sessions must be on the same day")
        people_times = {}
        for spec, s, e, people in rows:
            for p in people:
                if self.cons.distinct_interviewers and people_times.get(p):
                    problems.append(f"{p} is in more than one session; the plan requires distinct interviewers")
                for (ps, pe, other) in people_times.get(p, []):
                    buf = self.cons.interviewer_buffer_min
                    if ps - buf < e and s < pe + buf:
                        problems.append(f"{p} is in overlapping sessions {other} and {spec.session_id}")
                people_times.setdefault(p, []).append((s, e, spec.session_id))
        return {"valid": not problems, "problems": problems}

    # ------------------------------------------------------------------ near fits
    def outside_hours_options(self, stretch=("07:00", "20:00"), limit: int = 2) -> list:
        """Schedules that would work if ONE person met outside their working hours (set in Outlook or the assumed
        default), never over their actual commitments. These are conditional: the recruiter asks that person.
        Only for calendar-based availability; outside supplied windows is unknown, not "outside hours"."""
        from .availability import working_hour_intervals  # local import avoids a cycle

        cand_free = self._free(self.cand)
        if not cand_free:
            return []
        span = (min(s for s, _ in cand_free), max(e for _, e in cand_free))
        people = []
        for spec in self.specs:
            for p in spec.interviewers + spec.pool:
                if p not in people:
                    people.append(p)
        found = []
        for p in people:
            a = self.av.get(p)
            if a is None or a.coverage == "unknown" or a.source not in ("calendar", "mixed") or not a.time_zone:
                continue
            envelope, _ = working_hour_intervals({"days": list(WEEKDAYS), "start": stretch[0], "end": stretch[1]},
                                                 a.time_zone, span)
            commitments = [(b["s"], b["e"]) for b in a.blocked if b["status"] != "tentative_allowed"]
            stretched = iv.subtract(envelope, commitments)
            if not iv.subtract(stretched, a.free):
                continue
            original = a.free
            a.free = iv.union(original, stretched)
            try:
                res = self.search(max_options=1)
            finally:
                a.free = original
            for o in res["options"]:
                outside = [s for s in o["sessions"] if p in s.get("interviewers", [])
                           and not iv.contains(original, to_min(parse_ts(s["start"])), to_min(parse_ts(s["end"])))]
                if outside:
                    assumed = any("working hours are not set" in x for x in a.assumptions)
                    found.append({
                        "status": "conditional",
                        "sessions": o["sessions"],
                        "blocking_participant": p,
                        "reason": "outside_working_hours",
                        "working_hours_source": "assumed default (not set in Outlook)" if assumed else "their settings",
                        "outside_sessions": [{"session_id": s["session_id"], "start": s["start"], "end": s["end"],
                                              "local_start_for_them": iso(from_min(to_min(parse_ts(s["start"])),
                                                                                   a.time_zone))}
                                             for s in outside],
                        "ask": f"Ask {p} whether this time outside their working hours is OK.",
                    })
        found.sort(key=lambda x: x["sessions"][0]["start"])
        return found[:limit]

    def near_fits(self, limit: int = 5) -> list:
        """Slots where the candidate and all but one required interviewer are free (per session)."""
        out = []
        for spec in self.specs:
            unknown = [p for p in spec.interviewers if p in self.av and self.av[p].coverage == "unknown"]
            if len(spec.interviewers) < 2 and not unknown:
                continue
            cand_free = iv.intersect(self._free(self.cand), [(self.cons.not_before or -10 ** 12,
                                                               self.cons.not_after or 10 ** 12)])
            for blocker in spec.interviewers:
                others = [p for p in spec.interviewers if p != blocker]
                free = cand_free
                for p in others:
                    free = iv.intersect(free, self._free(p))
                for s in self._starts(free, spec.duration_min, -10 ** 12, None)[:200]:
                    e = s + spec.duration_min
                    a = self.av.get(blocker)
                    status = a.status_at(s, e) if a else "unknown"
                    if status == "free":
                        continue
                    out.append({"session_id": spec.session_id, "start": iso(from_min(s, self.cons.day_time_zone)),
                                "end": iso(from_min(e, self.cons.day_time_zone)), "blocking_participant": blocker,
                                "observed_status": status, "available": [self.cand] + others})
                    break
        out.sort(key=lambda x: (0 if x["observed_status"] == "tentative" else 1, x["start"]))
        return out[:limit]

    def explain(self, limit: int = 12) -> list:
        """Real reasons why parts of the candidate's available time are unusable, per interviewer (SCH-03).

        Each entry: who, the interval (in the participant's zone when known), and the observed reason:
        busy / oof / tentative / private_busy / buffer (a buffer policy around an existing commitment) / unknown /
        outside_working_hours / outside_supplied_availability, plus shared free time too short for the session.
        """
        cand = self._free(self.cand)
        lo = self.cons.not_before if self.cons.not_before is not None else -10 ** 12
        hi = self.cons.not_after if self.cons.not_after is not None else 10 ** 12
        cand = iv.intersect(cand, [(lo, hi)])
        people = []
        for spec in self.specs:
            for p in spec.interviewers + spec.pool:
                if p not in people:
                    people.append(p)
        out = []
        for p in people:
            a = self.av.get(p)
            if a is None:
                continue
            tz = self.cons.day_time_zone
            gaps = iv.subtract(cand, a.free)
            for gs, ge in gaps:
                covered = []
                for b in sorted(a.blocked, key=lambda b: b["s"]):
                    s, e = max(gs, b["s"]), min(ge, b["e"])
                    if s < e:
                        covered.append((s, e))
                        out.append({"participant": p, "start": iso(from_min(s, tz)), "end": iso(from_min(e, tz)),
                                    "reason": b["status"]})
                rest_reason = ("unknown" if a.coverage == "unknown" else
                               "outside_supplied_availability" if a.source in ("supplied", "override") else
                               "outside_working_hours")
                for s, e in iv.subtract([(gs, ge)], covered):
                    out.append({"participant": p, "start": iso(from_min(s, tz)), "end": iso(from_min(e, tz)),
                                "reason": rest_reason})
        # Days on which an interviewer has already reached the daily interview cap.
        cap = self.cons.max_sessions_per_interviewer_per_day
        if cap:
            seen = set()
            for p in people:
                for s, _ in cand:
                    day = self._person_day(p, s)
                    n = self.existing_interviews(p, day)
                    if n >= int(cap) and (p, day) not in seen:
                        seen.add((p, day))
                        out.append({"participant": p, "start": iso(from_min(s, self.cons.day_time_zone)),
                                    "end": "", "reason": f"daily interview cap reached on {day.isoformat()} "
                                                          f"(their local day): {n} of {cap} already booked"})
        # Time when everyone is free, but in pieces too short for the session.
        for spec in self.specs:
            common = cand
            for p in spec.interviewers:
                common = iv.intersect(common, self._free(p))
            for s, e in common:
                if e - s < spec.duration_min:
                    out.append({"participant": "everyone", "session_id": spec.session_id,
                                "start": iso(from_min(s, self.cons.day_time_zone)),
                                "end": iso(from_min(e, self.cons.day_time_zone)),
                                "reason": f"free together for only {e - s} min; the session needs {spec.duration_min}"})
        out.sort(key=lambda x: x["start"])
        return out[:limit]

    def diagnose(self) -> list:
        """Explain why nothing fits: pairwise overlap of the candidate with each required person."""
        notes = []
        cand_free = self._free(self.cand)
        if not cand_free:
            notes.append(f"{self.cand} has no available time in the search window")
        for spec in self.specs:
            for p in spec.interviewers:
                a = self.av.get(p)
                if a is None or a.coverage == "unknown":
                    notes.append(f"{spec.session_id}: availability for {p} is unknown (not free, not busy)")
                elif not iv.intersect(cand_free, a.free):
                    notes.append(f"{spec.session_id}: {p} has no time overlapping the candidate's availability")
                elif not any(e - s >= spec.duration_min for s, e in iv.intersect(cand_free, a.free)):
                    notes.append(f"{spec.session_id}: {p} overlaps the candidate but never for "
                                 f"{spec.duration_min} continuous minutes")
            if spec.pool_count:
                ok = [p for p in spec.pool if iv.intersect(cand_free, self._free(p))]
                if len(ok) < spec.pool_count:
                    notes.append(f"{spec.session_id}: only {len(ok)} of pool {spec.pool} overlap the candidate; "
                                 f"{spec.pool_count} needed")
        return notes
