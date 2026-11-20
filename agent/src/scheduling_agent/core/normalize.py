"""Normalize raw case inputs into a typed CaseNorm that every downstream module uses.

The agent was previously dropping ~90% of input fields, forcing the LLM to infer constraints
from raw JSON.  This module resolves every known field synonym up-front so the engine and
decision table always have the right values.  Unknown fields go to `unmodeled` for visibility.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Output type
# ---------------------------------------------------------------------------


@dataclass
class GateSpec:
    session_id: str
    required_round: str  # round_id whose pass result must exist
    gate_type: str = "book_after_pass"
    authorized_by: str = ""  # role that records the result


@dataclass
class CaseNorm:
    """Fully-resolved case facts.  Every field has a sensible default; nothing is None silently."""

    # --- engine constraints (ready to merge with Constraints dict) ---
    base_constraints: dict = field(default_factory=dict)

    # --- search window ---
    max_search_days: int = (
        30  # from policies.supported_envelope / scheduling_window_days
    )

    # --- gating ---
    gate_specs: list[GateSpec] = field(default_factory=list)  # one per gated session

    # --- capabilities ---
    holds_supported: bool = False

    # --- calendar coverage ---
    failed_coverage: list[dict] = field(
        default_factory=list
    )  # {participant_id, start, end}

    # --- case state ---
    existing_bookings: list[dict] = field(default_factory=list)
    prior_proposals: list[dict] = field(default_factory=list)
    round_results: list[dict] = field(default_factory=list)

    # --- observability ---
    unmodeled: list[dict] = field(
        default_factory=list
    )  # {path, value} for unknown keys


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _str(v) -> str:
    return str(v) if v is not None else ""


def _nested(d: dict, *keys, default=None):
    """Safe nested dict access:  _nested(d, 'a', 'b') == d.get('a', {}).get('b')."""
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k, default)
        if cur is None:
            return default
    return cur


def _record_unmodeled(unmodeled: list, path: str, value: Any) -> None:
    if value not in (None, {}, [], ""):
        unmodeled.append({"path": path, "value": value})


# ---------------------------------------------------------------------------
# Constraint normalization (break, buffer, cap, notice, envelope)
# ---------------------------------------------------------------------------


def _build_base_constraints(rules: dict, pol: dict, unmodeled: list) -> dict:
    out: dict = {}

    # --- minimum break between candidate sessions ---
    for key in (
        "break_between_sessions_min",
        "buffer_between_interviews_min",
        "min_break_between_sessions_min",
        "inter_session_buffer_min",
    ):
        v = _int(rules.get(key))
        if v is not None:
            out["min_break_min"] = v

    # --- maximum gap between sessions (same day) ---
    for key in (
        "max_gap_between_sessions_min",
        "max_gap_min",
        "max_gap_between_sessions_same_day_min",
        "max_break_between_sessions_min",
    ):
        v = _int(rules.get(key))
        if v is not None:
            out["max_break_min"] = v

    # back_to_back: max gap = min gap (or 0)
    if rules.get("back_to_back") is True:
        out.setdefault("max_break_min", out.get("min_break_min", 0))

    # --- candidate day span ---
    for key in (
        "max_candidate_day_min",
        "max_candidate_interview_day_min",
        "max_day_span_min",
    ):
        v = _int(rules.get(key))
        if v is not None:
            out["max_day_span_min"] = v

    # --- candidate daily minutes ---
    for key in ("max_candidate_minutes_per_day", "max_interview_minutes_per_day"):
        v = _int(rules.get(key))
        if v is not None:
            out["max_candidate_minutes_per_day"] = v
        v2 = _int(pol.get(key))
        if v2 is not None:
            out["max_candidate_minutes_per_day"] = v2

    # --- ordering ---
    if (
        str(rules.get("order", "")).lower() == "any"
        or rules.get("fixed_order") is False
    ):
        out["ordered"] = False

    # --- structural flags ---
    if rules.get("distinct_interviewers") is True:
        out["distinct_interviewers"] = True
    if rules.get("same_day") is True:
        out["same_day"] = True

    # --- interviewer daily cap ---
    raw_cap = (
        rules.get("interviewer_daily_cap")
        or pol.get("interviewer_daily_cap")
        or pol.get("max_interviews_per_interviewer_per_day")
        or _nested(pol, "interviewer_load", "max_interviews_per_day")
        or _nested(pol, "interviewer_load", "max")
        or _nested(pol, "interviewer_load", "daily_limit")
    )
    if isinstance(raw_cap, dict):
        raw_cap = (
            raw_cap.get("max_interviews") or raw_cap.get("max") or raw_cap.get("limit")
        )
    cap = _int(raw_cap)
    if cap:
        out["max_sessions_per_interviewer_per_day"] = cap

    # --- interviewer buffer ---
    buf = _int(
        pol.get("interviewer_buffer_min")
        or pol.get("interview_buffer_min")
        or rules.get("interviewer_buffer_min")
        or pol.get("buffer_between_interviewer_sessions_min")
    )
    if buf:
        out["interviewer_buffer_min"] = buf

    # --- minimum notice ---
    notice = _int(
        pol.get("minimum_notice_hours")
        or pol.get("min_notice_hours")
        or rules.get("minimum_notice_hours")
        or rules.get("min_notice_hours")
    )
    if notice is not None:
        out["min_notice_hours"] = notice

    return out


# ---------------------------------------------------------------------------
# Supported scheduling envelope
# ---------------------------------------------------------------------------


def _max_search_days(pol: dict) -> int:
    raw = (
        pol.get("supported_envelope")
        or pol.get("scheduling_window_days")
        or pol.get("search_window_days")
        or pol.get("max_scheduling_days")
    )
    v = _int(raw)
    return v if v and v > 0 else 30


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------


def _holds_supported(inputs: dict) -> bool:
    caps = inputs.get("capabilities")
    if not isinstance(caps, dict):
        return False
    holds = caps.get("temporary_holds") or caps.get("holds")
    return str(holds).lower() in ("available", "enabled", "true", "yes", "supported")


# ---------------------------------------------------------------------------
# Calendar coverage failures
# ---------------------------------------------------------------------------


def _failed_coverage(case_state: dict) -> list[dict]:
    out = []
    cov = case_state.get("calendar_coverage") or {}
    if isinstance(cov, dict):
        for entry in cov.get("failed") or []:
            if isinstance(entry, dict):
                out.append(entry)
    # Also look for coverage per-interviewer
    for iv_coverage in cov.get("participants") or []:
        if isinstance(iv_coverage, dict) and iv_coverage.get("status") != "ok":
            pid = iv_coverage.get("participant_id") or iv_coverage.get("id")
            if pid:
                out.append(
                    {
                        "participant_id": str(pid),
                        "status": iv_coverage.get("status", "unknown"),
                        "detail": iv_coverage.get("detail", ""),
                    }
                )
    return out


# ---------------------------------------------------------------------------
# Gating specs
# ---------------------------------------------------------------------------

_GATE_QUALIFIER_RE = re.compile(
    r"[\s_]+(?:pass(?:ed)?|recorded?|cleared?|approv(?:ed)?|authorized?|by|from|after)\b.*$",
    re.IGNORECASE,
)
_GATE_TYPE_ALIASES = {"book_after_pass", "gate", "hold_for_pass", "require_pass"}


def _parse_gate_specs(interview_plan: dict) -> list[GateSpec]:
    specs = []
    for session in interview_plan.get("sessions") or []:
        if not isinstance(session, dict):
            continue
        sid = str(session.get("id") or session.get("session_id") or "")
        if not sid:
            continue

        gating = (
            session.get("gating_policy")
            or session.get("gate")
            or session.get("requires_pass")
        )
        if gating is None:
            continue

        if isinstance(gating, str):
            stripped = gating.strip()
            if stripped.lower() in ("none", "null", ""):
                continue
            round_id = _GATE_QUALIFIER_RE.sub("", stripped).strip()
            if not round_id or round_id.lower() in ("pass", "passed", "cleared"):
                continue
            stripped = round_id
            gating = {"required_round": stripped, "type": "book_after_pass"}

        if not isinstance(gating, dict) or not gating:
            continue

        gate_type = str(gating.get("type", "book_after_pass")).lower()
        if gate_type not in _GATE_TYPE_ALIASES:
            continue

        required_id = (
            gating.get("required_round")
            or gating.get("round_id")
            or gating.get("requires_pass")
        )
        if not required_id:
            continue

        authorized_by = (
            gating.get("authorized_by")
            or gating.get("authorized_recorder")
            or "the authorized recorder named in the plan"
        )
        specs.append(
            GateSpec(
                session_id=sid,
                required_round=str(required_id),
                gate_type=gate_type,
                authorized_by=str(authorized_by),
            )
        )
    return specs


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

_KNOWN_PLAN_RULE_KEYS = {
    "break_between_sessions_min",
    "buffer_between_interviews_min",
    "min_break_between_sessions_min",
    "inter_session_buffer_min",
    "max_gap_between_sessions_min",
    "max_gap_min",
    "max_gap_between_sessions_same_day_min",
    "max_break_between_sessions_min",
    "back_to_back",
    "max_candidate_day_min",
    "max_candidate_interview_day_min",
    "max_day_span_min",
    "max_candidate_minutes_per_day",
    "max_interview_minutes_per_day",
    "order",
    "fixed_order",
    "distinct_interviewers",
    "same_day",
    "interviewer_daily_cap",
    "interviewer_buffer_min",
    "minimum_notice_hours",
    "min_notice_hours",
    "tentative_policy",
    "tentative_blocks_by_default",
    "gating_policy",  # top-level, not per-session
}

_KNOWN_POLICY_KEYS = {
    "interviewer_daily_cap",
    "max_interviews_per_interviewer_per_day",
    "interviewer_load",
    "interviewer_buffer_min",
    "interview_buffer_min",
    "minimum_notice_hours",
    "min_notice_hours",
    "supported_envelope",
    "scheduling_window_days",
    "search_window_days",
    "max_scheduling_days",
    "calendar_status_policy",
    "max_candidate_minutes_per_day",
    "max_interview_minutes_per_day",
    "buffer_between_interviewer_sessions_min",
}


def normalize(inputs: dict) -> CaseNorm:
    """Resolve all known field synonyms.  Unknown fields land in norm.unmodeled."""
    unmodeled: list[dict] = []

    plan: dict = inputs.get("interview_plan") or {}
    _raw_rules = plan.get("rules")
    rules: dict = dict(_raw_rules) if isinstance(_raw_rules, dict) else {}
    _raw_pol = inputs.get("policies")
    pol: dict = dict(_raw_pol) if isinstance(_raw_pol, dict) else {}
    case_state: dict = inputs.get("case_state") or {}

    # Constraints
    base_constraints = _build_base_constraints(rules, pol, unmodeled)

    # Search envelope
    max_search_days = _max_search_days(pol)

    # Gate specs
    gate_specs = _parse_gate_specs(plan)

    # Capabilities
    holds_supported = _holds_supported(inputs)

    # Calendar coverage failures
    failed_coverage = _failed_coverage(case_state)

    # Existing state
    existing_bookings = list(case_state.get("existing_bookings") or [])
    prior_proposals = list(case_state.get("prior_proposals") or [])
    round_results = list(case_state.get("round_results") or [])

    # Record unmodeled keys so they remain visible
    if isinstance(rules, dict):
        for k, v in rules.items():
            if k not in _KNOWN_PLAN_RULE_KEYS:
                _record_unmodeled(unmodeled, f"interview_plan.rules.{k}", v)
    if isinstance(pol, dict):
        for k, v in pol.items():
            if k not in _KNOWN_POLICY_KEYS:
                _record_unmodeled(unmodeled, f"policies.{k}", v)

    return CaseNorm(
        base_constraints=base_constraints,
        max_search_days=max_search_days,
        gate_specs=gate_specs,
        holds_supported=holds_supported,
        failed_coverage=failed_coverage,
        existing_bookings=existing_bookings,
        prior_proposals=prior_proposals,
        round_results=round_results,
        unmodeled=unmodeled,
    )
