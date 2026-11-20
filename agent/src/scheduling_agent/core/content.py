"""Build a ResponsePlan for the LLM to compose into a message.

The ResponsePlan contains:
  - The exact disposition code owns (LLM must use it verbatim)
  - Pre-rendered time strings keyed by (instant, zone) — LLM never writes an offset
  - Required content items — the submit_response validator checks each one is present
  - Clarifying questions built from detected issues, not from LLM memory
  - Assumptions to disclose
  - Compose instructions — compact, tells the LLM exactly what to do

The LLM's only creative job: phrase the message in natural language, using the
pre-rendered strings for every date and time, and covering every required_content item.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .decide import Decision, EngineResult
from .normalize import CaseNorm
from .state import CaseState
from .timeutil import describe_local, is_zone, try_parse_ts

# ---------------------------------------------------------------------------
# Output type
# ---------------------------------------------------------------------------


@dataclass
class RenderedOption:
    """One schedule option with times pre-rendered in every relevant zone."""

    option_ref: str  # "opt-1", "opt-2", ... (matches engine output)
    sessions: list[dict]  # [{session_id, start_iso, end_iso, interviewers, ...}]
    # time_strings: instant_iso → zone → human-readable string
    time_strings: dict[str, dict[str, str]] = field(default_factory=dict)
    status: str = "proposed"  # "proposed" | "conditional" | "booked"
    conditions: list[str] = field(default_factory=list)
    candidate_days: int = 1


@dataclass
class ResponsePlan:
    """Everything the LLM needs to compose a correct, complete response."""

    # --- disposition (code-decided; LLM MUST use this verbatim) ---
    disposition: str
    secondary_dispositions: list[str] = field(default_factory=list)

    # --- schedule options ---
    options: list[RenderedOption] = field(default_factory=list)
    near_fits: list[dict] = field(default_factory=list)
    conditional_outside_hours: list[dict] = field(default_factory=list)

    # --- pre-rendered times: instant_iso → {zone → rendered_string} ---
    # LLM must use these strings; it must NOT compute or invent offsets.
    pre_rendered_times: dict[str, dict[str, str]] = field(default_factory=dict)

    # --- required content (validator checks each item is covered) ---
    required_content: list[str] = field(default_factory=list)

    # --- questions (for ask_clarification / conditional_handoff) ---
    clarifying_questions: list[str] = field(default_factory=list)

    # --- context ---
    assumptions: list[str] = field(default_factory=list)
    diagnosis: list[str] = field(default_factory=list)
    effects_summary: list[str] = field(default_factory=list)
    unmodeled_note: str = ""  # surfaces unrecognized fields to the LLM

    # --- compact LLM instructions ---
    compose_instructions: str = ""

    # --- observability ---
    rule_fired: int = 0
    decision_reason: str = ""


# ---------------------------------------------------------------------------
# Time rendering helpers
# ---------------------------------------------------------------------------


def _render_instant(instant_iso: str, zones: list[str]) -> dict[str, str]:
    """Render one ISO instant in multiple IANA zones → {zone: human string}."""
    dt = try_parse_ts(instant_iso)
    if dt is None:
        return {}
    out = {}
    for z in zones:
        if is_zone(z):
            try:
                out[z] = describe_local(instant_iso, z)
            except Exception:  # noqa: BLE001, S110
                pass  # bad zone string — skip silently
    return out


def _collect_zones(inputs: dict, norm: CaseNorm) -> list[str]:
    """Collect the IANA zones that matter for this case (recruiter, candidate, interviewers)."""
    zones = []

    def _add(z):
        if z and is_zone(str(z)) and z not in zones:
            zones.append(str(z))

    recruiter = inputs.get("recruiter") or {}
    _add(recruiter.get("time_zone"))

    for c in [inputs.get("candidate")] + list(inputs.get("candidates") or []):
        if isinstance(c, dict):
            _add(c.get("time_zone"))

    for iv in inputs.get("interviewers") or []:
        if isinstance(iv, dict):
            _add(iv.get("time_zone"))

    if not zones:
        zones.append("UTC")
    return zones


def _render_options(raw_options: list[dict], zones: list[str]) -> list[RenderedOption]:
    """Pre-render all schedule option times into human strings."""
    rendered = []
    for i, opt in enumerate(raw_options or []):
        ref = opt.get("option_id") or f"opt-{i + 1}"
        sessions = opt.get("sessions") or []
        time_strings: dict[str, dict[str, str]] = {}

        for s in sessions:
            for key in ("start", "end"):
                instant = s.get(key)
                if instant and instant not in time_strings:
                    time_strings[instant] = _render_instant(instant, zones)

        rendered.append(
            RenderedOption(
                option_ref=ref,
                sessions=sessions,
                time_strings=time_strings,
                status=opt.get("status", "proposed"),
                conditions=opt.get("conditions") or [],
                candidate_days=opt.get("candidate_days", 1),
            )
        )
    return rendered


def _all_pre_rendered(
    rendered_options: list[RenderedOption],
) -> dict[str, dict[str, str]]:
    """Merge all time_strings into one lookup table for the ResponsePlan."""
    merged: dict[str, dict[str, str]] = {}
    for opt in rendered_options:
        for instant, zone_map in opt.time_strings.items():
            if instant not in merged:
                merged[instant] = {}
            merged[instant].update(zone_map)
    return merged


# ---------------------------------------------------------------------------
# Required-content builders per disposition
# ---------------------------------------------------------------------------


def _required_for_propose(
    rendered_options: list[RenderedOption],
    assumptions: list[str],
    near_fits: list[dict],
) -> list[str]:
    items = []
    n = len(rendered_options)
    if n == 1:
        items.append(
            "Present 1 schedule option (opt-1) with times in all relevant zones"
        )
    else:
        refs = ", ".join(o.option_ref for o in rendered_options)
        items.append(
            f"Present {n} schedule options ({refs}) with times in all relevant zones"
        )
    items.append("State that the times are not yet reserved")
    items.append(
        "Ask the recruiter which option to book, or ask them to confirm with the candidate"
    )
    for a in assumptions:
        items.append(f"Disclose assumption: {a}")
    if near_fits:
        items.append(
            f"Optionally mention that {len(near_fits)} near-fit(s) exist "
            "but a complete schedule was found"
        )
    return items


def _required_for_conditional(
    near_fits: list[dict],
    conditional_hours: list[dict],
    assumptions: list[str],
) -> list[str]:
    items = []
    all_conditional = near_fits + conditional_hours
    for c in all_conditional[:3]:  # cap at 3
        blocker = c.get("blocking_participant", "someone")
        reason = c.get("reason") or c.get("observed_status") or "unavailable"
        pool_note = c.get("pool_note", "")
        note = f" ({pool_note})" if pool_note else ""
        items.append(
            f"Describe the conditional option: {blocker} is {reason}{note}. "
            "Give the recruiter exact instructions: who to ask, the specific time and zone, "
            "what agreement is needed. Do NOT contact anyone yourself."
        )
    for a in assumptions:
        items.append(f"Disclose assumption: {a}")
    items.append("Give the recruiter a specific owned next step (who to ask, by when)")
    return items


def _required_for_infeasible(diagnosis: list[str], assumptions: list[str]) -> list[str]:
    items = ["Explain clearly why no valid schedule exists"]
    for d in diagnosis[:3]:
        items.append(f"Mention constraint: {d}")
    for a in assumptions:
        items.append(f"Disclose assumption: {a}")
    items.append(
        "Suggest a concrete path forward (e.g. ask for more availability, "
        "relax a constraint, name who can approve an exception)"
    )
    return items


def _required_for_ask_clarification(
    missing_facts: list[str],
    questions: list[str],
) -> list[str]:
    items = []
    if missing_facts:
        items.append(f"Ask for the missing information: {', '.join(missing_facts)}")
    for q in questions:
        items.append(f"Ask: {q}")
    if not items:
        items.append("Ask a focused clarifying question (one question only)")
    return items


def _required_for_request_availability(
    coverage_gaps: list[str],
    assumptions: list[str],
) -> list[str]:
    items = []
    if coverage_gaps:
        items.append(
            f"Request availability for: {', '.join(coverage_gaps[:3])}. "
            "Specify the date range needed and what format to send it in."
        )
    else:
        items.append(
            "Request the missing availability. Specify whose, the date range, and format."
        )
    for a in assumptions:
        items.append(f"Disclose assumption: {a}")
    return items


def _required_for_hold_for_gate(state: CaseState) -> list[str]:
    pending = state.pending_gates()
    items = []
    for g in pending[:2]:
        items.append(
            f"Explain that session {g.session_id} cannot be scheduled until "
            f"round '{g.required_round}' result is recorded by {g.authorized_by}"
        )
    items.append("Do not propose times. Do not search for options.")
    return items


def _required_for_effects(effects_summary: list[str]) -> list[str]:
    items = list(effects_summary)
    if not items:
        items.append(
            "Report the status of each write operation (what succeeded, what failed)"
        )
    return items


# ---------------------------------------------------------------------------
# Compose instructions per disposition
# ---------------------------------------------------------------------------


def _compose_instructions(disposition: str, n_options: int) -> str:
    if disposition == "propose_options":
        return (
            f"Present the {n_options} option(s) from the response plan. "
            "Use ONLY the pre_rendered_times strings for every date/time — do not compute offsets. "
            "Cover every item in required_content. Be concise."
        )
    if disposition == "conditional_handoff":
        return (
            "Explain each conditional option from the plan. "
            "Use ONLY the pre_rendered_times strings. "
            "Give exact recruiter instructions for each condition. Cover required_content."
        )
    if disposition == "ask_clarification":
        return (
            "Ask the specific questions from clarifying_questions. "
            "One focused reply; do not search or propose times."
        )
    if disposition == "report_infeasible":
        return (
            "Explain why no valid schedule exists (use diagnosis). "
            "Give a concrete next step. Cover required_content."
        )
    if disposition == "request_availability":
        return (
            "Request the missing availability. "
            "Be specific: whose availability, what date range, what format."
        )
    if disposition in ("book", "reschedule"):
        return (
            "Report what was booked/rescheduled. "
            "Use ONLY the pre_rendered_times strings. "
            "State that invitations have been sent. Cover required_content."
        )
    if disposition == "cancel":
        return (
            "Confirm the cancellation. List what was cancelled. "
            "State that cancellation notices will be sent to all attendees."
        )
    if disposition in ("recover_partial", "report_incomplete"):
        return "Report the status of each action. Be precise about what succeeded vs. what is unknown."
    if disposition == "refuse":
        return "Decline clearly. Explain why. Offer a legitimate alternative path if one exists."
    if disposition == "decline_unsupported":
        return (
            "Explain the feature is not available in this release. "
            "Offer the recruiter-owned workaround. Do NOT escalate."
        )
    if disposition == "escalate":
        return "Name who must act and what they need to do. Be specific."
    if disposition == "hold_for_gate":
        return "Explain what result is needed and who must record it. Do not propose times."
    if disposition == "acknowledge_and_record":
        return "Acknowledge the information. Record it. Keep the reply brief."
    if disposition == "update_memory":
        return "Confirm the setting change (from → to). Say it applies from now on."
    return "Write a helpful, concise reply covering required_content."


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def build_response_plan(
    decision: Decision,
    engine: EngineResult,
    state: CaseState,
    norm: CaseNorm,
    inputs: dict,
    assumptions: list[str],
    raw_options: list[dict] | None = None,
    near_fits: list[dict] | None = None,
    conditional_outside_hours: list[dict] | None = None,
) -> ResponsePlan:
    """Build the full ResponsePlan the LLM uses to compose its response."""
    zones = _collect_zones(inputs, norm)
    raw_options = raw_options or []
    near_fits = near_fits or []
    conditional_outside_hours = conditional_outside_hours or []

    # Pre-render all option times
    rendered_options = _render_options(raw_options, zones)
    pre_rendered = _all_pre_rendered(rendered_options)

    # Also render near-fit times
    for nf in near_fits:
        for key in ("start", "end"):
            inst = nf.get(key)
            if inst and inst not in pre_rendered:
                pre_rendered[inst] = _render_instant(inst, zones)

    # Build required content
    disp = decision.disposition
    req: list[str] = []
    if disp == "propose_options":
        req = _required_for_propose(rendered_options, assumptions, near_fits)
    elif disp == "conditional_handoff":
        req = _required_for_conditional(
            near_fits, conditional_outside_hours, assumptions
        )
    elif disp == "report_infeasible":
        req = _required_for_infeasible(engine.diagnosis, assumptions)
    elif disp == "ask_clarification":
        req = _required_for_ask_clarification(
            decision.missing_facts, decision.questions
        )
    elif disp == "request_availability":
        req = _required_for_request_availability(engine.coverage_gaps, assumptions)
    elif disp == "hold_for_gate":
        req = _required_for_hold_for_gate(state)
    elif disp in ("recover_partial", "report_incomplete"):
        effects_summary = [
            f"{e.action} {e.session_id}: {e.status}" for e in state.effects_ledger
        ]
        req = _required_for_effects(effects_summary)
    elif disp in ("book", "reschedule", "cancel"):
        effects_summary = [
            f"{e.action} {e.session_id}: {e.status}" for e in state.effects_ledger
        ]
        req = _required_for_effects(effects_summary) or [
            "Report the outcome of the write action"
        ]
    elif disp == "acknowledge_and_record":
        req = ["Acknowledge the information and confirm it has been noted"]
        for g in state.failed_gates():
            req.append(f"Acknowledge that round '{g.required_round}' did not pass")

    # Unmodeled note
    unmodeled_note = ""
    if norm.unmodeled:
        fields = "; ".join(f"{u['path']}" for u in norm.unmodeled[:5])
        unmodeled_note = (
            f"Note: {len(norm.unmodeled)} input field(s) not modeled by the engine "
            f"({fields}). These were visible in the case JSON — use your judgment "
            "about whether they affect the response."
        )

    return ResponsePlan(
        disposition=disp,
        secondary_dispositions=decision.secondary_dispositions,
        options=rendered_options,
        near_fits=near_fits,
        conditional_outside_hours=conditional_outside_hours,
        pre_rendered_times=pre_rendered,
        required_content=req,
        clarifying_questions=decision.questions,
        assumptions=assumptions,
        diagnosis=engine.diagnosis,
        effects_summary=[
            f"{e.action} {e.session_id}: {e.status}" for e in state.effects_ledger
        ],
        unmodeled_note=unmodeled_note,
        compose_instructions=_compose_instructions(disp, len(rendered_options)),
        rule_fired=decision.rule_fired,
        decision_reason=decision.reason,
    )
