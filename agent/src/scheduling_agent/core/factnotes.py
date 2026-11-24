"""FactNote: a typed, verifiable fact that must appear in the agent response.

Each tool that discovers a high-signal fact creates a FactNote.  The submit_response
D1 rubric check then asks fact.claim for every note in self.facts, ensuring required
facts reach the recruiter reply.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# FactNote kinds (extend as needed)
KIND_CAP_REACHED = "cap_reached"
KIND_LOOKUP_FAILED = "lookup_failed"
KIND_NEAR_FIT = "near_fit"
KIND_INFEASIBLE = "infeasible"
KIND_GATE_PENDING = "gate_pending"
KIND_BOOKING_EFFECT = "booking_effect"
KIND_CANCEL_EFFECT = "cancel_effect"
KIND_RESCHEDULE_EFFECT = "reschedule_effect"
KIND_WRITELOCK = "writelock"
KIND_ASSUMPTION = "assumption"


@dataclass(frozen=True)
class FactNote:
    """A concrete, verifiable fact the agent MUST communicate in its response."""

    kind: str
    # The D1 yes/no question; must include the actual value (name, number, date)
    claim: str
    # Short display name of who/what the fact is about
    subject: str = ""
    # Raw evidence values for trace/debugging
    evidence: dict = field(default_factory=dict)
    # Whether D1 should check this fact (False = informational only)
    must_communicate: bool = True
    # de-duplicate: same (kind, subject, day) = same fact
    dedupe_key: str = ""

    def __post_init__(self):
        if not self.dedupe_key:
            object.__setattr__(self, "dedupe_key", f"{self.kind}:{self.subject}")
