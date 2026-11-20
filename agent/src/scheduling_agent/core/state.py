"""Compute deterministic case state from inputs + norm.

Gate status, effects ledger, prior proposals, and grant scope — all derived
from structured inputs without any LLM involvement.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .normalize import CaseNorm

# ---------------------------------------------------------------------------
# Gate status
# ---------------------------------------------------------------------------


@dataclass
class GateStatus:
    session_id: str
    required_round: str
    status: str  # "pending" | "cleared" | "failed" | "unknown"
    authorized_by: str = ""


def compute_gate_statuses(norm: CaseNorm) -> list[GateStatus]:
    """Check each gate spec against recorded round_results."""
    passed: set[str] = set()
    decided: set[str] = set()

    for r in norm.round_results:
        if not isinstance(r, dict):
            continue
        # Support multiple field names for the round identifier
        rid = str(
            r.get("round_id")
            or r.get("round")
            or r.get("session_id")
            or r.get("id")
            or ""
        )
        if not rid:
            continue
        decided.add(rid)
        # Support both "outcome" and "result" field names for the pass/fail value
        result_val = str(r.get("outcome") or r.get("result") or "").lower()
        if result_val in ("pass", "passed", "cleared", "true", "yes"):
            passed.add(rid)

    statuses = []
    for gs in norm.gate_specs:
        rid = gs.required_round
        if rid in passed:
            status = "cleared"
        elif rid in decided:
            status = "failed"  # decided but not passed
        else:
            status = "pending"  # no result at all
        statuses.append(
            GateStatus(
                session_id=gs.session_id,
                required_round=rid,
                status=status,
                authorized_by=gs.authorized_by,
            )
        )
    return statuses


# ---------------------------------------------------------------------------
# Effects ledger — what writes were done in prior turns
# ---------------------------------------------------------------------------


@dataclass
class EffectRecord:
    action: str  # "create_event" | "cancel_event" | "update_event"
    session_id: str
    status: str  # "success" | "unknown" | "failed"
    event_id: str = ""
    detail: str = ""


def compute_effects_ledger(
    norm: CaseNorm, tool_results: list[dict] | None = None
) -> list[EffectRecord]:
    """Build the effects ledger from existing_bookings and any tool_results passed in."""
    ledger: list[EffectRecord] = []

    # Existing bookings from case_state
    for booking in norm.existing_bookings:
        if not isinstance(booking, dict):
            continue
        sid = str(booking.get("session_id") or booking.get("id") or "")
        status_raw = str(booking.get("status") or "success").lower()
        status = (
            "success" if status_raw in ("booked", "confirmed", "success") else "unknown"
        )
        ledger.append(
            EffectRecord(
                action="create_event",
                session_id=sid,
                status=status,
                event_id=str(booking.get("event_id") or ""),
            )
        )

    # Tool results from this turn (passed in by orchestrator)
    for tr in tool_results or []:
        if not isinstance(tr, dict):
            continue
        tool = str(tr.get("tool") or "")
        if tool not in ("create_event", "cancel_event", "update_event"):
            continue
        result = tr.get("result") or {}
        status_raw = str(result.get("status") or result.get("outcome") or "").lower()
        if status_raw in ("success", "created", "ok"):
            status = "success"
        elif status_raw in ("unknown", "unverified", "timeout", "error"):
            status = "unknown"
        else:
            status = "failed"
        sid = str(tr.get("session_id") or result.get("session_id") or "")
        ledger.append(
            EffectRecord(
                action=tool,
                session_id=sid,
                status=status,
                event_id=str(result.get("event_id") or ""),
                detail=str(result.get("detail") or result.get("error") or ""),
            )
        )

    return ledger


# ---------------------------------------------------------------------------
# CaseState — the top-level object downstream modules consume
# ---------------------------------------------------------------------------


@dataclass
class CaseState:
    gate_statuses: list[GateStatus] = field(default_factory=list)
    effects_ledger: list[EffectRecord] = field(default_factory=list)
    has_pending_gates: bool = False
    has_failed_gates: bool = False
    has_partial_effects: bool = False  # some writes done, some unknown/failed
    has_unknown_effects: bool = False  # any write outcome unverifiable
    prior_proposals: list[dict] = field(default_factory=list)

    # Convenience look-ups
    def pending_gates(self) -> list[GateStatus]:
        return [g for g in self.gate_statuses if g.status == "pending"]

    def failed_gates(self) -> list[GateStatus]:
        return [g for g in self.gate_statuses if g.status == "failed"]

    def cleared_gate_for(self, session_id: str) -> bool:
        """True when the gate for this session is cleared (or there is no gate)."""
        for g in self.gate_statuses:
            if g.session_id == session_id:
                return g.status == "cleared"
        return True  # no gate spec → no restriction


def compute_state(norm: CaseNorm, tool_results: list[dict] | None = None) -> CaseState:
    """Compute full case state from norm + optional tool results from this turn."""
    gate_statuses = compute_gate_statuses(norm)
    effects_ledger = compute_effects_ledger(norm, tool_results)

    has_pending = any(g.status == "pending" for g in gate_statuses)
    has_failed = any(g.status == "failed" for g in gate_statuses)

    successes = sum(1 for e in effects_ledger if e.status == "success")
    unknowns = sum(1 for e in effects_ledger if e.status == "unknown")
    has_unknown = unknowns > 0
    has_partial = successes > 0 and unknowns > 0

    return CaseState(
        gate_statuses=gate_statuses,
        effects_ledger=effects_ledger,
        has_pending_gates=has_pending,
        has_failed_gates=has_failed,
        has_partial_effects=has_partial,
        has_unknown_effects=has_unknown,
        prior_proposals=norm.prior_proposals,
    )


# ---------------------------------------------------------------------------
# Scope — which plan sessions can be scheduled right now
# ---------------------------------------------------------------------------


@dataclass
class Scope:
    schedulable: list[str] = field(default_factory=list)  # gate cleared or no gate
    held: list[str] = field(default_factory=list)  # gate pending
    failed: list[str] = field(default_factory=list)  # gate failed
    booked: list[str] = field(default_factory=list)  # already booked
    completed: list[str] = field(default_factory=list)  # has round_result


def compute_scope(
    plan: dict,
    norm: CaseNorm,
    state: CaseState,
    requested_ids: list[str] | None = None,
) -> Scope:
    """Determine which plan sessions are schedulable right now."""
    all_sids: list[str] = []
    for s in plan.get("sessions") or []:
        if isinstance(s, dict):
            sid = str(s.get("id") or s.get("session_id") or "")
            if sid:
                all_sids.append(sid)

    booked_sids: set[str] = {
        e.session_id
        for e in state.effects_ledger
        if e.status == "success" and e.action == "create_event" and e.session_id
    }
    completed_sids: set[str] = set()
    for r in norm.round_results:
        if isinstance(r, dict):
            sid = str(
                r.get("round_id")
                or r.get("round")
                or r.get("session_id")
                or r.get("id")
                or ""
            )
            if sid:
                completed_sids.add(sid)

    gate_by_sid = {gs.session_id: gs.status for gs in state.gate_statuses}

    schedulable: list[str] = []
    held: list[str] = []
    failed: list[str] = []
    for sid in all_sids:
        if sid in booked_sids or sid in completed_sids:
            continue
        gate = gate_by_sid.get(sid)
        if gate is None or gate == "cleared":
            schedulable.append(sid)
        elif gate == "pending":
            held.append(sid)
        elif gate == "failed":
            failed.append(sid)
        else:
            schedulable.append(sid)

    if requested_ids:
        req = set(requested_ids)
        schedulable = [s for s in schedulable if s in req]
        held = [s for s in held if s in req]
        failed = [s for s in failed if s in req]

    return Scope(
        schedulable=schedulable,
        held=held,
        failed=failed,
        booked=sorted(booked_sids),
        completed=sorted(completed_sids),
    )
