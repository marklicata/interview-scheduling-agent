"""Fallbacks that keep scheduling moving when a fact is missing.

Every resolution step has a defined fallback. A fallback is always recorded as an *assumption* and disclosed to
the recruiter with the proposal ("I assumed Priya works 9-5 Pacific"), so it can be corrected (INT-04/05).
Fallbacks only fill gaps in *convenience* facts. They never:
- treat unknown availability as free (requirements 7.5, AVL-02): an unknown person instead yields a conditional
  option naming exactly what must be confirmed;
- create authority (INV-01) or relax a hard rule or the candidate's stated availability (7.3).

Resolution chains (first available wins):
- interviewer working hours: their working_hours -> acceptable_hours -> calendar-provider working hours
  (Outlook settings via Graph) -> policy default hours (09:00-17:00 local, every day; configurable)
- interviewer time zone: their time_zone -> working_hours.time_zone -> zone resolved from their location
  (supplied by the model from the case text) -> the plan/office time zone -> the recruiter's time zone
No fallback for session duration or format: they define the interview itself, so a missing value is a blocking
question for the recruiter (INT-05, CON-10), never a default.
- search window: the request -> now .. now + default_search_days
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from .timeutil import is_zone


def _env_days(name, default):
    raw = os.environ.get(name)
    return [d.strip() for d in raw.split(",") if d.strip()] if raw else default


@dataclass
class FallbackPolicy:
    working_hours_start: str = os.environ.get("SCHEDULING_DEFAULT_WORK_START", "09:00")
    working_hours_end: str = os.environ.get("SCHEDULING_DEFAULT_WORK_END", "17:00")
    working_days: list = field(default_factory=lambda: _env_days(
        "SCHEDULING_DEFAULT_WORK_DAYS", ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]))
    default_search_days: int = int(os.environ.get("SCHEDULING_DEFAULT_SEARCH_DAYS", "30"))
    # How far outside someone's working hours a near fit may ask them to stretch (their local time). Working hours
    # are never widened automatically: a time outside them is only offered as a conditional option for the
    # recruiter to check with that person.
    stretch_start: str = os.environ.get("SCHEDULING_STRETCH_START", "07:00")
    stretch_end: str = os.environ.get("SCHEDULING_STRETCH_END", "20:00")

    def working_hours(self) -> dict:
        return {"days": list(self.working_days), "start": self.working_hours_start, "end": self.working_hours_end}

    def describe_hours(self, tz: str) -> str:
        days = "every day" if len(self.working_days) == 7 else "/".join(self.working_days)
        return f"{self.working_hours_start}-{self.working_hours_end} {days} in {tz}"

    def resolve_time_zone(self, person: dict, inputs: dict, override: str | None = None) -> tuple:
        """Return (time_zone or None, assumption text or None)."""
        if is_zone(person.get("time_zone")):
            return person["time_zone"], None
        wh = person.get("working_hours")
        if isinstance(wh, dict) and is_zone(wh.get("time_zone")):
            return wh["time_zone"], None
        name = person.get("name") or person.get("id")
        if override and is_zone(override):
            where = person.get("location") or person.get("office") or "their location"
            return override, f"{name}'s time zone is not set; assumed {override} from {where}"
        plan_tz = (inputs.get("interview_plan") or {}).get("time_zone")
        if is_zone(plan_tz):
            return plan_tz, f"{name}'s time zone is not set; assumed the plan's time zone {plan_tz}"
        rec_tz = (inputs.get("recruiter") or {}).get("time_zone")
        if is_zone(rec_tz):
            return rec_tz, f"{name}'s time zone is not set; assumed the recruiter's time zone {rec_tz}"
        return None, None



DEFAULT_POLICY = FallbackPolicy()
