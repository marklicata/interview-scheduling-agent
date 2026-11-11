"""Recruiter-controlled scheduling settings, changed by talking to the agent (CFG-02, CFG-05, LRN-07).

Recruiters never edit code or files: they say "assume 8 to 4 when someone's hours aren't set" or "only show me two
options", and the agent updates their settings. Hosts store settings per recruiter and pass them in each turn as
inputs["recruiter"]["settings"]; the agent returns changes in the response's "recruiter_settings".

Rules:
- Settings are personal preferences of the acting recruiter. They never grant authority to book or send (CFG-03).
- They cannot loosen organizational policy or hard rules (CFG-05): e.g. minimum notice can be raised above policy but
  not lowered below it. Policy always wins.
- Only the acting recruiter can change their own settings, from their own message (not candidates, interviewers,
  emails, or calendar content).
"""
from __future__ import annotations

import re
from dataclasses import replace

from .fallbacks import DEFAULT_POLICY, FallbackPolicy
from .timeutil import weekday_index

HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

# key -> (description, default getter, validator returning (value, error))
SETTINGS_DOC = {
    "default_working_hours": "Hours assumed for anyone whose working hours aren't set: {start: HH:MM, end: HH:MM, "
                             "days: [Mon..Sun]} in their own time zone.",
    "stretch_window": "How far outside someone's working hours a near fit may ask them to meet: {start, end} in "
                      "their local time.",
    "search_days": "How many days ahead to search when the request doesn't say (1-90).",
    "max_options": "How many valid options to show (1-3).",
    "min_notice_hours": "Minimum hours between now and an interview (can be raised above policy, never lowered).",
    "preferred_time_of_day": "Which valid options to rank first: morning | afternoon | any.",
    "time_display": "Which zone to show first: candidate_first | recruiter_first.",
}
SETTING_KEYS = tuple(SETTINGS_DOC)


def defaults(policy: FallbackPolicy | None = None) -> dict:
    p = policy or DEFAULT_POLICY
    return {
        "default_working_hours": {"start": p.working_hours_start, "end": p.working_hours_end,
                                  "days": list(p.working_days)},
        "stretch_window": {"start": p.stretch_start, "end": p.stretch_end},
        "search_days": p.default_search_days,
        "max_options": 3,
        "min_notice_hours": 0,
        "preferred_time_of_day": "any",
        "time_display": "candidate_first",
    }


def _hours(v, name):
    if not isinstance(v, dict) or not HHMM.match(str(v.get("start", ""))) or not HHMM.match(str(v.get("end", ""))):
        return None, f"{name} needs start and end as HH:MM (24-hour)"
    if v["end"] <= v["start"]:
        return None, f"{name} end must be after start"
    return {"start": v["start"], "end": v["end"]}, None


def validate(key: str, value, policy_min_notice: float = 0) -> tuple:
    """Return (normalized value, error)."""
    if key == "default_working_hours":
        out, err = _hours(value, key)
        if err:
            return None, err
        days = value.get("days") or ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        idx = [weekday_index(d) for d in days]
        if None in idx or not idx:
            return None, "days must be weekday names like Mon, Tue"
        names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        out["days"] = [names[i] for i in sorted(set(idx))]
        return out, None
    if key == "stretch_window":
        return _hours(value, key)
    if key == "search_days":
        try:
            v = int(value)
        except (TypeError, ValueError):
            return None, "search_days must be a whole number of days"
        return (v, None) if 1 <= v <= 90 else (None, "search_days must be between 1 and 90")
    if key == "max_options":
        try:
            v = int(value)
        except (TypeError, ValueError):
            return None, "max_options must be 1, 2, or 3"
        return (v, None) if 1 <= v <= 3 else (None, "max_options must be 1, 2, or 3")
    if key == "min_notice_hours":
        try:
            v = float(value)
        except (TypeError, ValueError):
            return None, "min_notice_hours must be a number of hours"
        if v < 0:
            return None, "min_notice_hours can't be negative"
        if v < policy_min_notice:
            return None, (f"organizational policy requires at least {policy_min_notice} hours of notice; a personal "
                          "setting can raise that but not lower it")
        return v, None
    if key == "preferred_time_of_day":
        v = str(value).lower()
        return (v, None) if v in ("morning", "afternoon", "any") else (None, "use morning, afternoon, or any")
    if key == "time_display":
        v = str(value).lower()
        return (v, None) if v in ("candidate_first", "recruiter_first") else \
            (None, "use candidate_first or recruiter_first")
    return None, f"unknown setting {key!r}; available: {list(SETTING_KEYS)}"


def effective(saved: dict | None, policy: FallbackPolicy | None = None) -> dict:
    """Defaults overlaid with the recruiter's valid saved settings (invalid saved values are ignored)."""
    out = defaults(policy)
    for k, v in (saved or {}).items():
        if k in SETTING_KEYS:
            norm, err = validate(k, v)
            if err is None:
                out[k] = norm
    return out


def apply_to_policy(settings: dict, policy: FallbackPolicy | None = None) -> FallbackPolicy:
    p = policy or DEFAULT_POLICY
    wh = settings["default_working_hours"]
    st = settings["stretch_window"]
    return replace(p, working_hours_start=wh["start"], working_hours_end=wh["end"], working_days=list(wh["days"]),
                   stretch_start=st["start"], stretch_end=st["end"], default_search_days=int(settings["search_days"]))
