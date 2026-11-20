"""Deterministic disposition pre-classifier.

Computes a disposition_hint for the cases the code can resolve without calling the scheduling
engine.  When code can't decide (most scheduling cases), it returns None — the LLM then runs
search_schedules and uses the post-search decision logic in the prompt.

Returned hint dict:
    {
        "disposition": str | None,       # e.g. "escalate", "hold_for_gate", None
        "confidence": "high" | "low",    # high: LLM should follow; low: advisory only
        "reason": str,                   # one-line explanation injected into the user message
        "search_required": bool,         # True: LLM must call search_schedules first
    }
"""

from __future__ import annotations

import re

_UNSUPPORTED = re.compile(
    r"\b(holds?|ATS|EightFold|eightfold|automatic reminders?|batch[- ]schedul|"
    r"room booking|travel booking|outreach to interviewers?)\b",
    re.IGNORECASE,
)
_SETTINGS = re.compile(
    r"\b(change|update|set|reset|default|prefer|show|display)\b.{0,60}"
    r"\b(working hours?|near[- ]fit|search days|number of options|notice|"
    r"time[- ]of[- ]day|time display|timezone order)\b",
    re.IGNORECASE,
)


def compute_hint(inputs: dict) -> dict | None:
    """Return a hint dict or None when the LLM must decide."""

    authority = inputs.get("authority") or {}
    mode = authority.get("mode", "")

    # --- read-only user: nothing can be written ---
    if mode == "read_only_user":
        return {
            "disposition": "escalate",
            "confidence": "high",
            "reason": (
                "The acting user is read-only. Show any valid options you found, "
                "explain they cannot book, and name who can."
            ),
            "search_required": True,  # still find options to show
        }

    # --- gate check: plan has a gate that hasn't been cleared ---
    gate_hint = _check_gate(inputs)
    if gate_hint:
        return gate_hint

    # --- autonomous grant active: remind LLM to execute, not propose ---
    if mode == "autonomous_grant_active":
        return {
            "disposition": None,
            "confidence": "high",
            "reason": (
                "An autonomous grant is active. If the recruiter's message requests an "
                "action covered by the grant's scope (candidate, action type, date range), "
                "execute it immediately — do NOT propose and ask. Run search_schedules first "
                "if scheduling is involved."
            ),
            "search_required": True,
        }

    # Remaining cases need the scheduling engine and/or LLM judgment.
    return None


def _check_gate(inputs: dict) -> dict | None:
    """Return hold_for_gate hint if the plan has an uncleared gate for the next session."""
    plan = inputs.get("interview_plan") or {}
    case_state = inputs.get("case_state") or {}
    round_results = case_state.get("round_results") or []

    # Build lookups: passed rounds AND any round with a result (pass or fail).
    # A gate with ANY result (even a fail) is DECIDED — not pending.
    # Only return hold_for_gate when NO result exists at all for the required round.
    passed: set[str] = set()
    decided: set[str] = set()
    for r in round_results:
        if not isinstance(r, dict):
            continue
        # Support both "round_id"/"id" and the simpler "round" field name
        rid = r.get("round_id") or r.get("round") or r.get("session_id") or r.get("id")
        if not rid:
            continue
        decided.add(str(rid))
        # Support both "outcome" and "result" field names
        result_val = str(r.get("outcome") or r.get("result") or "").lower()
        if result_val in ("pass", "passed", "cleared", "true", "yes"):
            passed.add(str(rid))

    for session in plan.get("sessions") or []:
        if not isinstance(session, dict):
            continue
        # Look for gating policy fields (various schema spellings)
        gating = (
            session.get("gating_policy")
            or session.get("gate")
            or session.get("requires_pass")
            or {}
        )
        if isinstance(gating, str):
            # Skip null-meaning values
            if gating.lower() in ("none", "null", ""):
                continue
            # Strip " pass" / " Pass" suffix that some datasets append
            # e.g. "R1 pass" → required_round = "R1"
            stripped = gating
            for suffix in (" pass", " Pass", " PASS", "_pass"):
                if stripped.endswith(suffix):
                    stripped = stripped[: -len(suffix)].strip()
                    break
            gating = {"required_round": stripped, "type": "book_after_pass"}
        if not isinstance(gating, dict) or not gating:
            continue

        gate_type = str(gating.get("type", "book_after_pass")).lower()
        if gate_type not in ("book_after_pass", "gate", "hold_for_pass"):
            continue

        required_id = (
            gating.get("required_round")
            or gating.get("round_id")
            or gating.get("requires_pass")
            or session.get("id")
        )
        if not required_id:
            continue
        rid_str = str(required_id)

        if rid_str in decided and rid_str not in passed:
            # Gate was decided but NOT passed (failed) — the LLM should acknowledge and record.
            # Return None so the prompt's acknowledge_and_record guidance handles it.
            return None

        if rid_str not in decided:
            # No result at all — gate is genuinely pending.
            role = (
                gating.get("authorized_by")
                or "the authorized recorder named in the plan"
            )
            return {
                "disposition": "hold_for_gate",
                "confidence": "high",
                "reason": (
                    f"The plan requires a passed round result for '{required_id}' before this "
                    f"session can be scheduled. No result is on file yet. Wait for the result "
                    f"(must come from {role}) before proposing times."
                ),
                "search_required": False,
            }
        # rid_str in passed — gate cleared; fall through so the agent can schedule.
    return None
