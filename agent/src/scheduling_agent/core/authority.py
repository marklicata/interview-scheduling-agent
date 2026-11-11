"""The write gate: a deterministic check every external effect must pass (INV-01, INV-07, INV-08, CFG-01..09).

The model proposes a write together with its authority basis. The gate never trusts the model's judgment alone:
- read-only users and expired/revoked grants never authorize writes;
- "confirmation" must point at an actual turn spoken by the acting user (recruiter/coordinator/admin), after
  the latest agent turn, quoting words that literally appear in it; text from candidates, interviewers, other
  people, systems, emails or calendar content never counts;
- "grant" must be an active autonomous grant whose scope covers the action type and dates, and whose organizer
  matches; booking authority never covers holds, publication, or messages unless the scope lists them;
- a current "draft only / don't book / don't send" instruction blocks the write even with an active grant.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .ports import WRITE_METHODS
from .timeutil import try_parse_ts

ACTING_ROLES = {"recruiter", "coordinator", "admin"}
RESTRICTION = re.compile(
    r"\b(draft[- ]only|just (a )?draft|do ?n[o']?t (book|send|schedule|invite|change|cancel|touch)|"
    r"don't (book|send|schedule|invite|change|cancel)|not yet|hold off|wait before|no invites?|"
    r"before (you|we) (book|send)|without booking)\b", re.I)
NEGATION = re.compile(r"^\s*(no|nope|not|don'?t|do not|wait|hold|stop|cancel that)\b", re.I)

# Words that approve an action. After the agent has displayed a proposal, an affirmation or the action's own verb
# approves it ("Yes", "Sim, book it", "네, book it"). Without a displayed proposal, only the action's own verb counts
# ("Book Mei Lin Thursday at 4", "Please cancel Kai's 4pm AA"); "set up", "find", "arrange", or "delete everything"
# are requests to plan or to discuss, not approval of a specific write.
AFFIRM = re.compile(
    r"\b(yes|yep|yeah|yup|sure|ok|okay|go ahead|please do|do (it|both|that|those|them|all)|confirm(ed)?|"
    r"approved?|that works|perfect)\b|"
    r"^\W*(sim|s[ií]|oui|ja|da|tak|はい|네|예|是的|好的?|ok)(\W|$)", re.I)
ACTION_VERBS = {
    "book": re.compile(r"\b(book|send (the |an |out )?(invite|invitation)s?|send it|invite|lock (it )?in|"
                       r"grab (the|that|it))", re.I),
    "reschedule": re.compile(r"\b(move|reschedule|shift|push|change|update|book|send it)\b", re.I),
    "cancel": re.compile(r"\b(cancel|withdraw|call off)", re.I),
    "send_message": re.compile(r"\b(send|email|e-mail|tell|let \w+ know|notify|message|reply|forward)\b", re.I),
    "hold": re.compile(r"\bhold", re.I),
    "publish": re.compile(r"\bpublish", re.I),
}
GO_AHEAD = re.compile(r"\b(go ahead|book|move|reschedule|cancel|send)\b", re.I)
DRAFT_ONLY = re.compile(r"draft[- ]only|just (a )?draft", re.I)

ACTION_SYNONYMS = {
    "book": {"book", "create", "schedule", "booking", "create_event", "invite"},
    "reschedule": {"reschedule", "update", "move", "update_event", "change"},
    "cancel": {"cancel", "cancel_event", "cancellation"},
    "send_message": {"send_message", "send", "message", "email", "outreach"},
    "hold": {"hold", "holds", "create_hold"},
    "publish": {"publish", "publication"},
}


@dataclass
class GateDecision:
    allowed: bool
    reason: str

    def to_dict(self) -> dict:
        return {"allowed": self.allowed, "reason": self.reason}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip().lower()


class WriteGate:
    def __init__(self, inputs: dict, now):
        self.inputs = inputs
        self.authority = inputs.get("authority") or {}
        self.conversation = [t for t in inputs.get("conversation") or [] if isinstance(t, dict)]
        self.now = now
        self.recruiter_id = str((inputs.get("recruiter") or {}).get("id") or "")
        self.recruiter_name = _norm((inputs.get("recruiter") or {}).get("name"))
        self.delegate_roles = self._delegate_roles(inputs)

    @staticmethod
    def _delegate_roles(inputs: dict) -> set:
        """Conversation roles that policy explicitly lets confirm bookings for this case (may_confirm_bookings)."""
        roles = set()
        acting = inputs.get("acting_user") if isinstance(inputs.get("acting_user"), dict) else {}
        pol = inputs.get("policies") if isinstance(inputs.get("policies"), dict) else {}
        for key in ("case_delegates", "case_collaborators", "delegates", "collaborators"):
            groups = pol.get(key)
            entries = [e for grp in (groups.values() if isinstance(groups, dict) else [groups or []])
                       for e in (grp if isinstance(grp, list) else [grp]) if isinstance(e, dict)]
            for e in entries:
                if e.get("may_confirm_bookings") is not True:
                    continue
                if e.get("role"):
                    roles.add(_norm(e["role"]).replace(" ", "_"))
                if acting and str(e.get("user")) == str(acting.get("id")):
                    roles.add("assistant_ea" if "assistant" in _norm(acting.get("role")) else
                              _norm(acting.get("role")).replace(" ", "_"))
        return roles

    # ------------------------------------------------------------------ helpers
    def _last_agent_index(self) -> int:
        idx = -1
        for i, t in enumerate(self.conversation):
            if t.get("role") == "agent":
                idx = i
        return idx

    def current_restriction(self) -> str | None:
        """A draft-only/don't-book instruction from the acting user after the last agent turn."""
        for t in self.conversation[self._last_agent_index() + 1:]:
            text = str(t.get("text") or "")
            if t.get("role") in ACTING_ROLES and RESTRICTION.search(text):
                # "Go ahead with the S2 move. Hold off on S4" approves one thing and restricts another: the model
                # scopes it, and each write still needs its own approval. "Draft only" is always a restriction.
                if GO_AHEAD.search(text) and not DRAFT_ONLY.search(text):
                    continue
                return text
        return None

    def _grant(self) -> dict | None:
        if self.authority.get("mode") != "autonomous_grant_active":
            return None
        g = self.authority.get("grant")
        return g if isinstance(g, dict) else None

    # ------------------------------------------------------------------ decision
    def check(self, method: str, basis: dict | None, start=None, organizer=None) -> GateDecision:
        action = WRITE_METHODS.get(method)
        if action is None:
            return GateDecision(False, f"{method} is not a write this agent performs")
        mode = self.authority.get("mode")
        if mode == "read_only_user":
            return GateDecision(False, "the acting user is read-only; no writes are possible. Hand off to someone "
                                       "with write access.")
        basis = basis or {}
        kind = basis.get("type")
        restriction = self.current_restriction()
        if restriction:
            return GateDecision(False, f"the user's current instruction restricts actions: {restriction!r}")
        if kind == "grant":
            return self._check_grant(action, start, organizer)
        if kind == "confirmation":
            return self._check_confirmation(action, basis)
        return GateDecision(False, "no authority basis given; propose the action and ask for confirmation")

    def _check_grant(self, action, start, organizer) -> GateDecision:
        g = self._grant()
        if g is None:
            return GateDecision(False, f"no active autonomous grant (authority mode {self.authority.get('mode')!r})")
        if str(g.get("status", "active")).lower() not in ("active", ""):
            return GateDecision(False, f"grant status is {g.get('status')!r}")
        exp = try_parse_ts(g.get("expires"))
        if exp is not None and self.now >= exp:
            return GateDecision(False, "the autonomous grant has expired")
        scope = g.get("scope") or {}
        actions = {str(a).lower() for a in scope.get("actions") or []}
        allowed = {a for a in actions for k, syn in ACTION_SYNONYMS.items() if a in syn and k == action}
        if not allowed:
            return GateDecision(False, f"the grant covers {sorted(actions)}, not {action}")
        dr = scope.get("date_range") or {}
        ds, de, st = try_parse_ts(dr.get("start")), try_parse_ts(dr.get("end")), try_parse_ts(start)
        if st is not None and ((ds and st < ds) or (de and st >= de)):
            return GateDecision(False, "the event date is outside the grant's date range")
        named = " ".join(str(scope.get(k) or "") for k in ("case", "candidate", "candidates"))
        cand = _norm((self.inputs.get("candidate") or {}).get("name"))
        # "all candidates" or "all" in the scope means no specific candidate restriction
        _named_norm = _norm(named)
        _covers_all = "all candidates" in _named_norm or _named_norm.startswith("all ")
        if named.strip() and cand and not _covers_all and cand.split()[0] not in _named_norm:
            return GateDecision(False, f"the grant is scoped to {named.strip()!r}, not this candidate")
        org = scope.get("organizer")
        if org and organizer and _norm(org) != _norm(organizer):
            return GateDecision(False, f"the grant requires organizer {org}")
        return GateDecision(True, f"covered by the active {g.get('route', 'configured')} grant "
                                  f"({sorted(actions)})")

    def _check_confirmation(self, action, basis) -> GateDecision:
        try:
            i = int(basis.get("turn_index"))
        except (TypeError, ValueError):
            return GateDecision(False, "confirmation basis needs turn_index")
        if not 0 <= i < len(self.conversation):
            return GateDecision(False, f"turn {i} does not exist")
        turn = self.conversation[i]
        role = turn.get("role")
        text = str(turn.get("text") or "")
        ui_approval = (role == "system" and _norm(text).startswith("ui event") and self.recruiter_name
                       and self.recruiter_name in _norm(text) and re.search(r"approve|book|confirm", text, re.I))
        delegated = role in self.delegate_roles and action in ("book", "reschedule")
        if role not in ACTING_ROLES and not ui_approval and not delegated:
            return GateDecision(False, f"turn {i} is from {role!r}; only the acting recruiter/coordinator (or a "
                                       "delegate that policy allows to confirm bookings) can authorize actions")
        if i < self._last_agent_index():
            return GateDecision(False, f"turn {i} precedes the latest agent turn; a confirmation must answer the "
                                       "current proposal")
        quote = _norm(basis.get("quote"))
        if not quote or quote not in _norm(text):
            return GateDecision(False, f"the quoted confirmation does not appear in turn {i}")
        if NEGATION.search(text):
            return GateDecision(False, f"turn {i} reads as a refusal or hold, not a confirmation")
        verb = ACTION_VERBS.get(action)
        if ui_approval:
            return GateDecision(True, f"approved in the product UI by the acting recruiter (turn {i})")
        if action in ("hold", "publish") or not self._agent_before(i):
            if verb is None or not verb.search(text):
                return GateDecision(False, f"turn {i} does not explicitly ask to {action}. Show the exact action and "
                                           "ask for confirmation first.")
        elif not (AFFIRM.search(text) or (verb is not None and verb.search(text))):
            return GateDecision(False, f"turn {i} does not approve the proposed {action}")
        return GateDecision(True, f"explicitly confirmed by the {role} in turn {i}")

    def _agent_before(self, i: int) -> bool:
        return any(t.get("role") == "agent" for t in self.conversation[:i])
