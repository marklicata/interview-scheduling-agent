"""Dataset loading, case selection, request construction, and interpretability lint."""
from __future__ import annotations

import copy
import fnmatch
import hashlib
import json
import os
from dataclasses import dataclass, field

from case_oracle import Oracle, OracleError, load_yaml
from validate_cases import CATEGORIES

from .contract import AgentRequest
from .common import try_parse_ts

DEFAULT_DATASET = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "interview-scheduling-test-cases.yaml")

# Keys anywhere inside inputs that are grading metadata rather than scenario facts.
STRIP_KEYS_ANYWHERE = {"offset_check"}
# Conversation-turn labels that tell the agent which turn the dataset treats as an authorizing
# confirmation. The agent must infer that from the text, so the label is removed.
STRIP_TURN_KEYS = {"confirmation"}


@dataclass
class Dataset:
    path: str
    header: dict
    cases: list
    sha256: str

    @property
    def identity(self) -> dict:
        h = self.header or {}
        try:
            shown = os.path.relpath(self.path)
        except ValueError:  # different drive on Windows
            shown = self.path
        return {
            "id": h.get("id"),
            "version": h.get("version"),
            "status": h.get("status"),
            "path": shown.replace("\\", "/"),
            "sha256": self.sha256,
            "case_count": len(self.cases),
        }


def load_dataset(path: str = DEFAULT_DATASET) -> Dataset:
    with open(path, "rb") as fh:
        digest = hashlib.sha256(fh.read()).hexdigest()
    doc = load_yaml(path)
    if not isinstance(doc, dict) or not isinstance(doc.get("test_cases"), list):
        raise ValueError(f"{path}: expected a mapping with a test_cases list")
    return Dataset(path=path, header=doc.get("dataset") or {}, cases=doc["test_cases"], sha256=digest)


@dataclass
class Selection:
    ids: list = field(default_factory=list)          # exact ids or fnmatch patterns, e.g. TZ-0*
    exclude_ids: list = field(default_factory=list)
    categories: list = field(default_factory=list)   # category names or id prefixes
    severities: list = field(default_factory=list)
    difficulties: list = field(default_factory=list)
    authority_modes: list = field(default_factory=list)
    release_scopes: list = field(default_factory=list)
    tags: list = field(default_factory=list)
    limit: int = 0
    per_category: int = 0   # deterministic sample: N cases per category, evenly spread across the category

    def is_full(self) -> bool:
        return not (self.ids or self.exclude_ids or self.categories or self.severities or self.difficulties
                    or self.authority_modes or self.release_scopes or self.tags or self.limit
                    or self.per_category)

    def describe(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v}


def select_cases(cases: list, sel: Selection) -> list:
    cats = set()
    for c in sel.categories:
        cats.add(CATEGORIES.get(c.upper(), c))
    out = []
    per_cat: dict = {}  # category -> matching cases (per_category sampling)
    for case in cases:
        if not isinstance(case, dict):
            continue
        cid = str(case.get("id", ""))
        if sel.ids and not any(fnmatch.fnmatchcase(cid, p) for p in sel.ids):
            continue
        if sel.exclude_ids and any(fnmatch.fnmatchcase(cid, p) for p in sel.exclude_ids):
            continue
        if cats and case.get("category") not in cats:
            continue
        if sel.severities and case.get("severity") not in sel.severities:
            continue
        if sel.difficulties and case.get("difficulty") not in sel.difficulties:
            continue
        mode = ((case.get("inputs") or {}).get("authority") or {}).get("mode")
        if sel.authority_modes and mode not in sel.authority_modes:
            continue
        if sel.release_scopes and case.get("release_scope") not in sel.release_scopes:
            continue
        if sel.tags and not (set(sel.tags) & set(case.get("tags") or [])):
            continue
        if sel.per_category:
            per_cat.setdefault(case.get("category"), []).append(case)
            continue
        out.append(case)
        if sel.limit and len(out) >= sel.limit:
            break
    if sel.per_category:
        keep = set()
        for group in per_cat.values():
            n = len(group)
            k = min(sel.per_category, n)
            keep.update(id(group[(i * n) // k]) for i in range(k))
        out = [c for c in cases if id(c) in keep]
        if sel.limit:
            out = out[:sel.limit]
    return out


# --------------------------------------------------------------------------------------
# Request construction (what the agent is allowed to see)
# --------------------------------------------------------------------------------------


def _strip_keys(node, stripped: list, path: str):
    if isinstance(node, dict):
        for k in list(node.keys()):
            if k in STRIP_KEYS_ANYWHERE:
                stripped.append(f"{path}.{k}")
                del node[k]
            else:
                _strip_keys(node[k], stripped, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            _strip_keys(v, stripped, f"{path}[{i}]")


def request_id_for(case_id: str, salt: str) -> str:
    return "req-" + hashlib.sha256(f"{salt}:{case_id}".encode("utf-8")).hexdigest()[:16]


def build_request(case: dict, calendar_delivery: str = "tools", salt: str = "") -> tuple:
    """Return (AgentRequest, stripped_paths). Only ``inputs`` is ever copied into the request."""
    inputs = copy.deepcopy(case.get("inputs") or {})
    stripped: list = []
    _strip_keys(inputs, stripped, "inputs")
    for i, turn in enumerate(inputs.get("conversation") or []):
        if isinstance(turn, dict):
            for k in STRIP_TURN_KEYS:
                if k in turn:
                    del turn[k]
                    stripped.append(f"inputs.conversation[{i}].{k}")
    if calendar_delivery == "tools":
        for i, iv in enumerate(inputs.get("interviewers") or []):
            if isinstance(iv, dict) and "calendar" in iv:
                del iv["calendar"]
                iv["calendar_via_tool"] = True
                stripped.append(f"inputs.interviewers[{i}].calendar (served by tools.get_schedule)")
    req = AgentRequest(
        request_id=request_id_for(str(case.get("id")), salt),
        now=str(inputs.get("now")),
        inputs=inputs,
        calendar_delivery=calendar_delivery,
    )
    return req, stripped


# --------------------------------------------------------------------------------------
# Interpretability lint: places where the harness cannot apply a grading rule as written
# --------------------------------------------------------------------------------------


@dataclass
class Issue:
    case_id: str
    code: str
    severity: str        # "blocking" (a rule cannot be graded as written) or "note"
    message: str

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def plan_sessions(case: dict) -> dict:
    plan = (case.get("inputs") or {}).get("interview_plan") or {}
    out = {}
    sessions = plan.get("sessions") if isinstance(plan, dict) else None
    for s in sessions or []:
        if isinstance(s, dict) and s.get("id") is not None:
            out[str(s["id"])] = s
    return out


def lint_case(case: dict, oracle_factory=None) -> list:
    cid = str(case.get("id", "?"))
    issues = []
    inputs = case.get("inputs") or {}
    if try_parse_ts(inputs.get("now")) is None:
        issues.append(Issue(cid, "bad_now", "blocking", "inputs.now is not an ISO timestamp with offset"))
    for i, turn in enumerate(inputs.get("conversation") or []):
        if isinstance(turn, dict) and "text" in turn and not isinstance(turn.get("text"), str):
            issues.append(Issue(cid, "non_string_turn_text", "blocking",
                                f"conversation[{i}].text is {type(turn.get('text')).__name__} {turn.get('text')!r}, "
                                "not text (an unquoted YAML yes/no/true/number); the agent sees a non-string message"))
    tr = inputs.get("tool_results")
    if tr is not None and not isinstance(tr, list):
        issues.append(Issue(cid, "tool_results_shape", "note", "inputs.tool_results is not a list; not simulated"))
    for i, iv in enumerate(inputs.get("interviewers") or []):
        if not isinstance(iv, dict):
            continue
        src = iv.get("availability_source")
        if src in ("calendar", "mixed") and "calendar" not in iv and "calendar_access" not in iv:
            issues.append(Issue(cid, "calendar_missing", "note",
                                f"interviewer {iv.get('id')} has availability_source {src} but no calendar data; "
                                "tools.get_schedule reports status 'unknown' (not free)"))
    oracle = case.get("oracle")
    if oracle is not None:
        try:
            o = (oracle_factory or Oracle)(oracle)
        except OracleError as exc:
            issues.append(Issue(cid, "oracle_unbuildable", "blocking", f"schedule checks impossible: {exc}"))
            o = None
        if o is not None:
            plans = plan_sessions(case)
            for pid, ps in plans.items():
                if pid not in o.sessions:
                    continue
                try:
                    pdur = int(ps.get("duration_min"))
                except (TypeError, ValueError):
                    continue
                if pdur != o.sessions[pid]["d"]:
                    issues.append(Issue(cid, "oracle_duration_differs", "note",
                                        f"plan session {pid} lists {pdur} min but the oracle models {o.sessions[pid]['d']} "
                                        "min (e.g. an accommodation); the oracle's duration is enforced"))
            if plans and not (set(plans) & set(o.sessions)):
                from .graders import plan_to_oracle_map  # local import to avoid a cycle
                mapping = plan_to_oracle_map(o, plans, None)
                if not any(mapping.values()):
                    issues.append(Issue(
                        cid, "oracle_plan_unlinked", "blocking",
                        f"oracle sessions {sorted(o.sessions)} cannot be linked to interview_plan sessions "
                        f"{sorted(plans)} by id, attendees, or duration; agent schedules cannot be graded"))
            elif not plans:
                issues.append(Issue(cid, "no_plan_sessions", "note",
                                    "interview_plan has no session ids; agent schedules are matched to oracle ids only"))
    exp = case.get("expected") or {}
    se = exp.get("side_effects") or {}
    mutations = [a for a in se.get("actions") or [] if isinstance(a, dict)
                 and a.get("type") in ("update_event", "cancel_event")]
    if mutations:
        from .sandbox import SandboxEnvironment  # local import keeps module loading simple
        try:
            live = SandboxEnvironment(inputs).live_events()
        except ValueError:
            live = []
        if not live:
            issues.append(Issue(cid, "mutation_target_missing", "blocking",
                                f"expected {', '.join(a['type'] for a in mutations)} but no existing event is present in "
                                "case_state or tool_results, so no agent can perform it in the sandbox"))
    for i, a in enumerate(se.get("actions") or []):
        if isinstance(a, dict) and not (a.get("target") or a.get("notes")):
            issues.append(Issue(cid, "action_without_target", "note",
                                f"expected.side_effects.actions[{i}] has no target/notes; only its type can be checked"))
    for key in ("must", "must_not"):
        for i, t in enumerate(exp.get(key) or []):
            if not isinstance(t, str):
                issues.append(Issue(cid, "non_string_assertion", "blocking", f"expected.{key}[{i}] is not text"))
    return issues


def dump_request(req: AgentRequest) -> str:
    return json.dumps(req.to_dict(), indent=2, ensure_ascii=False, default=str)
