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

import glob
import os
import re
import sys
from datetime import date, datetime, timezone

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from case_oracle import (  # noqa: E402,F401  (shared with the evaluation harness; re-exported)
    HHMM_RE,
    WD_NAMES,
    WEEKDAYS,
    CaseLoader,
    Oracle,
    OracleError,
    fmt_offset,
    from_min,
    get_tz,
    load_yaml,
    parse_aware,
    parse_example,
    parse_interval,
    to_min,
)


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
# Oracle checks (the Oracle itself lives in case_oracle.py)
# --------------------------------------------------------------------------------------


def check_oracle(case, rep: Reporter):
    cid = case.get("id", "?")
    spec = case["oracle"]
    if not isinstance(spec, dict):
        rep.err(cid, "oracle must be a mapping")
        return
    try:
        o = Oracle(spec)
    except OracleError as exc:
        rep.err(cid, str(exc))
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
