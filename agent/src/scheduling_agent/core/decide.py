"""Deterministic disposition decision table.

Takes structured signals from the LLM (intent, risk) and from code (search results,
gate state, effects ledger, norm) and produces the disposition deterministically.

This is a pure function: same inputs → same output, no randomness, fully unit-testable
against all 1,185 labeled cases by stubbing the LLM signals.

Rule priority (first match wins):
  1.  Effects ledger has unknown or partial outcomes             → recover_partial / report_incomplete
  2.  Risk: core ask prohibited                                  → refuse
  3.  Risk: core ask unsupported (holds, ATS, reminders, etc.)  → decline_unsupported
  4.  Intent: settings update                                    → update_memory
  5.  Intent: revoke / stop / acknowledge-only (no scheduling)  → acknowledge_and_record
  6.  Missing defining facts                                     → ask_clarification
  7.  Material ambiguity (readings produce different schedules)  → ask_clarification
  8.  Gate pending for the session being scheduled               → hold_for_gate
  9.  Gate failed for session being scheduled                    → acknowledge_and_record
 10.  Blocking external barrier (ineligible interviewer, etc.)   → escalate
 11.  Write intent (book/cancel/reschedule):
       a. read_only_user                                          → escalate
       b. authorized + slot still valid                           → book / reschedule / cancel
       c. authorized + slot no longer valid                       → propose_options
 12.  Engine outcome:
       a. options ≥ 1                                             → propose_options
       b. near_fits or conditional_outside_hours present          → conditional_handoff
       c. coverage gaps / unknown / envelope truncated            → request_availability / report_incomplete
       d. all coverage known, nothing found                       → report_infeasible
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .normalize import CaseNorm
from .state import CaseState

# ---------------------------------------------------------------------------
# Input signals
# ---------------------------------------------------------------------------

WRITE_DISPOSITIONS = {"book", "reschedule", "cancel"}
PROPOSING = {"propose_options", "book", "reschedule"}
HANDOFF_LIKE = {
    "ask_clarification",
    "request_availability",
    "conditional_handoff",
    "report_infeasible",
    "report_incomplete",
    "refuse",
    "escalate",
    "decline_unsupported",
    "hold_for_gate",
    "recover_partial",
}

# Features the agent explicitly does not support in this release
_UNSUPPORTED_FEATURES = {
    "holds",
    "temporary_holds",
    "calendar_holds",
    "ats",
    "eightfold",
    "greenhouse",
    "lever",
    "workday",
    "batch_scheduling",
    "batch",
    "automatic_reminders",
    "reminders",
    "room_booking",
    "room",
    "travel_booking",
    "travel",
    "interviewer_outreach",
    "outreach",
}


@dataclass
class Intent:
    """LLM-extracted intent, structured and verified."""

    action: str = ""  # "schedule" | "book" | "reschedule" | "cancel"
    # | "acknowledge" | "update_settings" | "revoke"
    # | "information_only" | "withdrawal"
    session_ids: list[str] = field(default_factory=list)  # sessions in scope
    candidate_id: str = ""
    confirm_option_ref: str = ""  # e.g. "opt-1" for book intent
    verb_quote: str = ""
    turn_index: int = -1


@dataclass
class RiskSignals:
    """LLM-classified risk, with specific reasons."""

    prohibited: bool = False
    prohibited_reason: str = ""
    unsupported_features: list[str] = field(default_factory=list)  # feature names
    injection_detected: bool = False
    injection_sources: list[str] = field(default_factory=list)


@dataclass
class MissingFacts:
    """Code-detected missing defining facts."""

    duration_missing: bool = False  # session has no duration_min
    format_missing: bool = False  # format required but unknown
    interviewers_missing: bool = False  # no interviewers and no pool
    gating_policy_unknown: bool = False  # gate exists but policy field is null/missing
    candidate_identity_unclear: bool = False
    phone_number_required: bool = False


@dataclass
class AmbiguitySignal:
    """One ambiguity candidate, with materiality already resolved by code."""

    type: str  # "time_zone" | "identity" | "format" | "date"
    description: str
    material: bool = (
        False  # True when different readings produce different schedule results
    )
    options: list[str] = field(default_factory=list)


@dataclass
class EngineResult:
    """Summarized output of the scheduling engine search."""

    options_count: int = 0
    near_fits_count: int = 0
    conditional_hours_count: int = 0
    coverage_complete: bool = (
        True  # False when any participant has unknown/partial coverage
    )
    coverage_gaps: list[str] = field(
        default_factory=list
    )  # participant_ids with unknown coverage
    envelope_truncated: bool = False  # search hit the supported_envelope limit
    infeasible: bool = False  # engine confirmed no solution possible
    diagnosis: list[str] = field(default_factory=list)  # why nothing fits

    @property
    def has_options(self) -> bool:
        return self.options_count > 0

    @property
    def has_conditional(self) -> bool:
        return self.near_fits_count > 0 or self.conditional_hours_count > 0


@dataclass
class AuthoritySignals:
    """Write-authority signals from the case."""

    mode: str = "confirm_each_action"  # authority.mode
    confirmed: bool = False  # recruiter explicitly confirmed a specific action
    confirm_quote: str = ""
    confirm_turn: int = -1
    grant_active: bool = False
    grant_covers_action: bool = False
    confirmed_option_still_valid: bool = (
        True  # False if the confirmed slot is now conflicting
    )


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


@dataclass
class Decision:
    disposition: str
    secondary_dispositions: list[str] = field(default_factory=list)
    rule_fired: int = 0  # which rule (1-12) produced this decision
    reason: str = ""  # one-line explanation for observability
    search_required: bool = True  # hint for the pipeline: run engine first?
    escalate_to: str = ""  # named role for escalate disposition
    missing_facts: list[str] = field(default_factory=list)  # for ask_clarification
    questions: list[str] = field(default_factory=list)  # for ask_clarification


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _has_missing_defining_facts(missing: MissingFacts) -> tuple[bool, list[str]]:
    items = []
    if missing.duration_missing:
        items.append("session duration")
    if missing.format_missing:
        items.append("interview format")
    if missing.interviewers_missing:
        items.append("required interviewers")
    if missing.gating_policy_unknown:
        items.append("gating policy (which round must pass first)")
    if missing.candidate_identity_unclear:
        items.append("candidate identity")
    return bool(items), items


def _gate_check(state: CaseState, intent: Intent) -> tuple[str | None, str]:
    """Return (disposition, reason) if a gate blocks the requested sessions, else (None, '')."""
    if not intent.session_ids:
        return None, ""
    for gs in state.gate_statuses:
        if gs.session_id not in intent.session_ids:
            continue
        if gs.status == "pending":
            return "hold_for_gate", (
                f"The plan requires a passed round result for '{gs.required_round}' "
                f"before {gs.session_id} can be scheduled. "
                f"No result is on file. Wait for the result from {gs.authorized_by}."
            )
        if gs.status == "failed":
            return "acknowledge_and_record", (
                f"Round '{gs.required_round}' did not pass. "
                "Acknowledge the result and explain the round did not clear the gate."
            )
    return None, ""


# ---------------------------------------------------------------------------
# Main decision table
# ---------------------------------------------------------------------------


def decide(
    intent: Intent,
    risk: RiskSignals,
    missing: MissingFacts,
    ambiguities: list[AmbiguitySignal],
    engine: EngineResult,
    state: CaseState,
    authority: AuthoritySignals,
    norm: CaseNorm,
) -> Decision:
    """Return the disposition. First matching rule wins."""

    # Rule 1 — unknown / partial write outcomes
    if state.has_unknown_effects:
        if state.has_partial_effects:
            return Decision(
                disposition="recover_partial",
                rule_fired=1,
                reason="Some writes completed but others have unknown outcomes.",
                search_required=False,
            )
        return Decision(
            disposition="report_incomplete",
            rule_fired=1,
            reason="Write outcome is unverifiable (timeout or error).",
            search_required=False,
        )

    # Rule 2 — prohibited core ask
    if risk.prohibited:
        return Decision(
            disposition="refuse",
            rule_fired=2,
            reason=risk.prohibited_reason or "Core ask is prohibited.",
            search_required=False,
        )

    # Rule 3 — unsupported feature
    if risk.unsupported_features:
        feature_list = ", ".join(risk.unsupported_features)
        return Decision(
            disposition="decline_unsupported",
            rule_fired=3,
            reason=f"Feature(s) not supported in this release: {feature_list}.",
            search_required=False,
        )

    # Rule 4 — settings update
    if intent.action == "update_settings":
        return Decision(
            disposition="update_memory",
            rule_fired=4,
            reason="Recruiter is updating their scheduling settings.",
            search_required=False,
        )

    # Rule 5 — revoke / stop / acknowledge-only
    if intent.action in (
        "revoke",
        "stop",
        "acknowledge",
        "information_only",
        "withdrawal",
    ):
        return Decision(
            disposition="acknowledge_and_record",
            rule_fired=5,
            reason=f"Action '{intent.action}' requires acknowledgement, not scheduling.",
            search_required=False,
        )

    # Rule 6 — missing defining facts
    has_missing, missing_items = _has_missing_defining_facts(missing)
    if has_missing:
        return Decision(
            disposition="ask_clarification",
            rule_fired=6,
            reason=f"Missing defining facts: {', '.join(missing_items)}.",
            search_required=False,
            missing_facts=missing_items,
        )

    # Rule 7 — material ambiguity
    material = [a for a in ambiguities if a.material]
    if material:
        questions = [a.description for a in material]
        return Decision(
            disposition="ask_clarification",
            rule_fired=7,
            reason=f"Ambiguity would change the result: {'; '.join(a.type for a in material)}.",
            search_required=False,
            questions=questions,
        )

    # Rule 8 & 9 — gate check
    gate_disp, gate_reason = _gate_check(state, intent)
    if gate_disp:
        return Decision(
            disposition=gate_disp,
            rule_fired=8 if gate_disp == "hold_for_gate" else 9,
            reason=gate_reason,
            search_required=False,
        )

    # Rule 10 — blocking external barrier requiring human decision
    # (ineligible interviewer, no eligible pool members after exclusions, etc.)
    # This is signalled via engine.infeasible + a specific diagnosis keyword
    if engine.infeasible and engine.diagnosis:
        barrier_keywords = (
            "disabled",
            "ineligible",
            "no eligible",
            "excluded",
            "must designate",
        )
        if any(
            any(kw in d.lower() for kw in barrier_keywords) for d in engine.diagnosis
        ):
            return Decision(
                disposition="escalate",
                rule_fired=10,
                reason="; ".join(engine.diagnosis[:2]),
                search_required=False,
            )

    # Rule 11 — write intent
    if intent.action in ("book", "reschedule", "cancel"):
        if authority.mode == "read_only_user":
            return Decision(
                disposition="escalate",
                rule_fired=11,
                reason="The acting user is read-only and cannot book.",
                escalate_to="a user with booking authority",
            )
        authorized = authority.confirmed or (
            authority.grant_active and authority.grant_covers_action
        )
        if authorized:
            if intent.action == "cancel":
                return Decision(
                    disposition="cancel",
                    rule_fired=11,
                    reason="Cancellation authorized by recruiter.",
                )
            if not authority.confirmed_option_still_valid:
                return Decision(
                    disposition="propose_options",
                    rule_fired=11,
                    reason="Confirmed option is no longer valid; presenting new options.",
                )
            if intent.action == "reschedule":
                return Decision(
                    disposition="reschedule",
                    rule_fired=11,
                    reason="Reschedule authorized by recruiter.",
                )
            return Decision(
                disposition="book",
                rule_fired=11,
                reason="Booking authorized by recruiter.",
            )

    # Rule 12 — engine outcome
    if engine.has_options:
        return Decision(
            disposition="propose_options",
            rule_fired=12,
            reason=f"{engine.options_count} valid schedule option(s) found.",
        )

    if engine.has_conditional:
        return Decision(
            disposition="conditional_handoff",
            rule_fired=12,
            reason=(
                f"{engine.near_fits_count} near-fit(s) or "
                f"{engine.conditional_hours_count} outside-hours option(s) found."
            ),
        )

    # No options and nothing conditional
    if not engine.coverage_complete or engine.envelope_truncated:
        # Unknown coverage or search was truncated — can't conclude infeasible
        if engine.envelope_truncated:
            return Decision(
                disposition="report_incomplete",
                rule_fired=12,
                reason=(
                    "Search window is limited by the scheduling policy. "
                    "No valid option found within the allowed window."
                ),
            )
        gap_list = ", ".join(engine.coverage_gaps[:3])
        return Decision(
            disposition="request_availability",
            rule_fired=12,
            reason=f"Calendar coverage unknown for: {gap_list}.",
        )

    # All coverage known, still nothing
    return Decision(
        disposition="report_infeasible",
        rule_fired=12,
        reason="No valid schedule exists within the given constraints.",
    )
