"""The contract between the evaluation harness and the agent under test.

An agent is any object with ``name``, ``version`` and ``run(request, tools)``:

    class MyAgent:
        name = "scheduling-agent"
        version = "0.1.0"

        def run(self, request: AgentRequest, tools: SandboxTools) -> AgentResponse | dict:
            ...

``request`` carries only the scenario inputs (see AgentRequest). ``tools`` is the sandboxed
calendar/mail provider (see sandbox.SandboxTools); every external effect must go through it, and
the harness grades what actually happened there, never what the response claims. The response is
an AgentResponse (or an equivalent plain dict); see README.md for field-by-field semantics.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional, Protocol

from . import CONTRACT_VERSION
from .common import parse_ts

from validate_cases import DISPOSITIONS  # noqa: E402  (single source of truth for the enum)

OPTION_STATUSES = ("proposed", "conditional", "booked")
DRAFT_AUDIENCES = ("candidate", "interviewer", "recruiter", "hiring_manager", "coordinator", "other")
CALENDAR_DELIVERY = ("tools", "inline")


@dataclass
class AgentRequest:
    """What the agent sees for one scenario.

    inputs: the case's ``inputs`` block (now, recruiter, authority, candidate, interview_plan,
        interviewers, policies, case_state, conversation, tool_results) with grading-only labels
        removed. With calendar_delivery == "tools", interviewers' ``calendar`` lists are served by
        ``tools.get_schedule`` instead of being inlined; such interviewers carry
        ``calendar_via_tool: true``.
    now: the current time (inputs.now). The sandbox clock starts here.
    request_id: opaque per-run identifier; it does not encode the case id or category.
    """

    request_id: str
    now: str
    inputs: dict
    calendar_delivery: str = "tools"
    contract_version: str = CONTRACT_VERSION

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SessionAssignment:
    """One interview session in a schedule.

    session_id: the interview_plan session id (e.g. "S1", "HM"). start/end: ISO 8601 with offset.
    interviewers: interviewer ids from inputs.interviewers (names/emails are resolved when unique).
    """

    session_id: str
    start: str
    end: str
    interviewers: list = field(default_factory=list)
    format: Optional[str] = None
    location: Optional[str] = None


@dataclass
class ScheduleOption:
    """A schedule shown to the user.

    status:
      proposed    - presented as a valid schedule (graded against the oracle; one invalid option fails)
      conditional - presented as NOT yet valid, pending a stated condition (e.g. a near-fit handoff)
      booked      - claimed to exist as calendar events; must match events in the sandbox
    """

    sessions: list
    status: str = "proposed"
    option_id: Optional[str] = None
    conditions: list = field(default_factory=list)
    note: Optional[str] = None


@dataclass
class StatedTime:
    """A local date/time the response states, so it can be checked mechanically.

    instant: ISO 8601 with offset; time_zone: IANA zone; local: "YYYY-MM-DDTHH:MM" wall-clock time in
    that zone; weekday and utc_offset ("+05:30") are optional but checked when given.
    """

    instant: str
    time_zone: str
    local: str
    weekday: Optional[str] = None
    utc_offset: Optional[str] = None


@dataclass
class DraftMessage:
    """Content prepared for someone but NOT sent (sending requires tools.send_message)."""

    audience: str
    to: list = field(default_factory=list)
    subject: str = ""
    body: str = ""


@dataclass
class AgentResponse:
    """The agent's reply after the last conversation turn.

    disposition: primary behavior, one of the dataset dispositions (None means "no decision").
    message: the user-facing reply to the recruiter/coordinator.
    """

    disposition: Optional[str]
    message: str = ""
    secondary_dispositions: list = field(default_factory=list)
    options: list = field(default_factory=list)
    clarifying_questions: list = field(default_factory=list)
    stated_times: list = field(default_factory=list)
    drafts: list = field(default_factory=list)
    memory_updates: list = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    trace: Any = None

    def to_dict(self) -> dict:
        return asdict(self)


class Agent(Protocol):
    name: str
    version: str

    def run(self, request: AgentRequest, tools: Any) -> Any:  # pragma: no cover - protocol
        ...


# --------------------------------------------------------------------------------------
# Response parsing and schema validation
# --------------------------------------------------------------------------------------


def _as_dict(obj) -> dict:
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    try:
        return asdict(obj)
    except TypeError:
        return {"__unparseable__": repr(obj)}


def _str_list(value, path, problems) -> list:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        problems.append(f"{path} must be a list")
        return []
    out = []
    for i, v in enumerate(value):
        if isinstance(v, str):
            out.append(v)
        else:
            problems.append(f"{path}[{i}] must be a string")
    return out


def _check_ts(value, path, problems):
    try:
        return parse_ts(value)
    except (TypeError, ValueError) as exc:
        problems.append(f"{path}: invalid timestamp {value!r} ({exc})")
        return None


def parse_response(obj) -> tuple:
    """Normalize an agent's return value into (AgentResponse, schema_problems)."""
    problems: list = []
    raw = _as_dict(obj)
    if obj is None:
        problems.append("agent returned None")
    if "__unparseable__" in raw:
        problems.append(f"agent returned an unsupported object: {raw['__unparseable__'][:200]}")
        raw = {}
    disposition = raw.get("disposition")
    if disposition is not None and disposition not in DISPOSITIONS:
        problems.append(f"disposition {disposition!r} is not one of {sorted(DISPOSITIONS)}")
    message = raw.get("message", "")
    if message is None:
        message = ""
    if not isinstance(message, str):
        problems.append("message must be a string")
        message = str(message)
    secondary = _str_list(raw.get("secondary_dispositions"), "secondary_dispositions", problems)
    for d in secondary:
        if d not in DISPOSITIONS:
            problems.append(f"secondary disposition {d!r} is not a known disposition")

    options = []
    raw_options = raw.get("options") or []
    if not isinstance(raw_options, (list, tuple)):
        problems.append("options must be a list")
        raw_options = []
    for i, ro in enumerate(raw_options):
        ro = _as_dict(ro)
        path = f"options[{i}]"
        status = ro.get("status", "proposed")
        if status not in OPTION_STATUSES:
            problems.append(f"{path}.status {status!r} must be one of {list(OPTION_STATUSES)}")
        sessions = []
        raw_sessions = ro.get("sessions") or []
        if not isinstance(raw_sessions, (list, tuple)) or not raw_sessions:
            problems.append(f"{path}.sessions must be a non-empty list")
            raw_sessions = raw_sessions if isinstance(raw_sessions, (list, tuple)) else []
        for j, rs in enumerate(raw_sessions):
            rs = _as_dict(rs)
            spath = f"{path}.sessions[{j}]"
            sid = rs.get("session_id")
            if not isinstance(sid, str) or not sid:
                problems.append(f"{spath}.session_id is required")
                continue
            start = _check_ts(rs.get("start"), f"{spath}.start", problems)
            end = _check_ts(rs.get("end"), f"{spath}.end", problems)
            if start is None or end is None:
                continue
            if end <= start:
                problems.append(f"{spath}: end must be after start")
                continue
            people = rs.get("interviewers") or []
            if not isinstance(people, (list, tuple)) or not all(isinstance(p, str) for p in people):
                problems.append(f"{spath}.interviewers must be a list of strings")
                people = [str(p) for p in people] if isinstance(people, (list, tuple)) else []
            sessions.append(SessionAssignment(
                session_id=sid, start=str(rs.get("start")), end=str(rs.get("end")), interviewers=list(people),
                format=rs.get("format"), location=rs.get("location"),
            ))
        options.append(ScheduleOption(
            sessions=sessions, status=status if status in OPTION_STATUSES else "proposed",
            option_id=ro.get("option_id"), conditions=_str_list(ro.get("conditions"), f"{path}.conditions", problems),
            note=ro.get("note"),
        ))

    stated = []
    raw_stated = raw.get("stated_times") or []
    if not isinstance(raw_stated, (list, tuple)):
        problems.append("stated_times must be a list")
        raw_stated = []
    for i, rt in enumerate(raw_stated):
        rt = _as_dict(rt)
        path = f"stated_times[{i}]"
        if not all(isinstance(rt.get(k), str) for k in ("instant", "time_zone", "local")):
            problems.append(f"{path} needs string instant, time_zone and local")
            continue
        stated.append(StatedTime(instant=rt["instant"], time_zone=rt["time_zone"], local=rt["local"],
                                 weekday=rt.get("weekday"), utc_offset=rt.get("utc_offset")))

    drafts = []
    raw_drafts = raw.get("drafts") or []
    if not isinstance(raw_drafts, (list, tuple)):
        problems.append("drafts must be a list")
        raw_drafts = []
    for i, rd in enumerate(raw_drafts):
        rd = _as_dict(rd)
        audience = rd.get("audience", "other")
        if audience not in DRAFT_AUDIENCES:
            problems.append(f"drafts[{i}].audience {audience!r} must be one of {list(DRAFT_AUDIENCES)}")
        drafts.append(DraftMessage(audience=str(audience), to=_str_list(rd.get("to"), f"drafts[{i}].to", problems),
                                   subject=str(rd.get("subject") or ""), body=str(rd.get("body") or "")))

    usage = raw.get("usage") or {}
    if not isinstance(usage, dict):
        problems.append("usage must be a mapping")
        usage = {}
    memory = raw.get("memory_updates") or []
    if not isinstance(memory, (list, tuple)):
        problems.append("memory_updates must be a list")
        memory = []

    response = AgentResponse(
        disposition=disposition if (disposition is None or disposition in DISPOSITIONS) else None,
        message=message,
        secondary_dispositions=[d for d in secondary if d in DISPOSITIONS],
        options=options,
        clarifying_questions=_str_list(raw.get("clarifying_questions"), "clarifying_questions", problems),
        stated_times=stated,
        drafts=drafts,
        memory_updates=list(memory),
        usage=usage,
        trace=raw.get("trace"),
    )
    if disposition is not None and disposition not in DISPOSITIONS:
        response.disposition = None
    return response, problems
