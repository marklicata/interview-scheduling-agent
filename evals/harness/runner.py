"""Run agents over cases: one fresh sandbox per attempt, then grade."""
from __future__ import annotations

import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from .contract import AgentResponse, parse_response
from .dataset import build_request
from .graders import grade_case
from .sandbox import SandboxEnvironment

STATUS_RANK = {"fail": 0, "error": 1, "ungraded": 2, "pass": 3}


class AgentTimeout(Exception):
    pass


@dataclass
class RunConfig:
    calendar_delivery: str = "tools"
    salt: str = ""
    timeout_s: float = 0.0
    workers: int = 1
    repeats: int = 1
    include_transcripts: bool = True
    progress: bool = True


def _invoke(agent, request, tools, case, timeout_s):
    if getattr(agent, "uses_answer_key", False):
        def fn():
            return agent.run_with_case(request, tools, case)
    else:
        def fn():
            return agent.run(request, tools)
    if not timeout_s:
        return fn()
    box = {}

    def target():
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - re-raised in the caller's thread
            box["error"] = exc

    th = threading.Thread(target=target, daemon=True)
    th.start()
    th.join(timeout_s)
    if th.is_alive():
        raise AgentTimeout(f"agent did not return within {timeout_s} s")
    if "error" in box:
        raise box["error"]
    return box.get("value")


def _case_meta(case) -> dict:
    inputs = case.get("inputs") or {}
    exp = case.get("expected") or {}
    oracle = case.get("oracle") if isinstance(case.get("oracle"), dict) else {}
    return {
        "case_id": str(case.get("id")),
        "title": case.get("title"),
        "category": case.get("category"),
        "severity": case.get("severity"),
        "difficulty": case.get("difficulty"),
        "release_scope": case.get("release_scope"),
        "authority_mode": (inputs.get("authority") or {}).get("mode"),
        "expected_disposition": exp.get("disposition"),
        "external_writes": (exp.get("side_effects") or {}).get("external_writes"),
        "has_oracle": case.get("oracle") is not None,
        "expect_feasible": oracle.get("expect_feasible") if oracle else None,
        "requirement_refs": list(case.get("requirement_refs") or []),
    }


def run_attempt(case, agent, judge, cfg: RunConfig, attempt: int = 0) -> dict:
    out = {"attempt": attempt, "status": None, "error": None, "duration_s": None}
    try:
        request, stripped = build_request(case, cfg.calendar_delivery, cfg.salt)
        sandbox = SandboxEnvironment(case.get("inputs") or {})
    except Exception as exc:  # noqa: BLE001
        out.update(status="error", error={"source": "harness", "stage": "setup", "type": type(exc).__name__,
                                          "message": str(exc), "traceback": traceback.format_exc(limit=5)})
        return out
    out["stripped_fields"] = stripped
    agent_error = None
    raw = None
    t0 = time.perf_counter()
    try:
        raw = _invoke(agent, request, sandbox.tools, case, cfg.timeout_s)
    except AgentTimeout as exc:
        agent_error = {"source": "agent", "stage": "run", "type": "timeout", "message": str(exc)}
    except Exception as exc:  # noqa: BLE001
        agent_error = {"source": "agent", "stage": "run", "type": type(exc).__name__, "message": str(exc),
                       "traceback": traceback.format_exc(limit=8)}
    out["duration_s"] = round(time.perf_counter() - t0, 4)
    try:
        if agent_error:
            response, problems = AgentResponse(disposition=None), [f"agent error: {agent_error['message']}"]
        else:
            response, problems = parse_response(raw)
        grade = grade_case(case, response, problems, sandbox, judge)
    except Exception as exc:  # noqa: BLE001
        out.update(status="error", error={"source": "harness", "stage": "grading", "type": type(exc).__name__,
                                          "message": str(exc), "traceback": traceback.format_exc(limit=8)})
        if cfg.include_transcripts:
            out["transcript"] = sandbox.transcript()
        return out
    status = grade.status
    if agent_error:
        # An agent crash cannot hide effects it already caused: unsafe or unexpected writes still fail the case.
        by_name = {c.name: c for c in grade.checks}
        effects_failed = by_name["side_effects"].outcome == "fail" and sandbox.writes
        status = "fail" if (grade.safety_violations or effects_failed) else "error"
        out["error"] = agent_error
    out["status"] = status
    out["response"] = response.to_dict()
    out["grade"] = grade.to_dict()
    out["usage"] = response.usage or {}
    if cfg.include_transcripts:
        out["transcript"] = sandbox.transcript()
    return out


def run_case(case, agent, judge, cfg: RunConfig) -> dict:
    attempts = [run_attempt(case, agent, judge, cfg, k) for k in range(max(1, cfg.repeats))]
    worst = min(attempts, key=lambda a: STATUS_RANK.get(a["status"], 1))
    rec = _case_meta(case)
    grade = worst.get("grade") or {}
    rec.update({
        "status": worst["status"],
        "failure_kinds": grade.get("failure_kinds", []),
        "safety_violations": grade.get("safety_violations", []),
        "checks": grade.get("checks", []),
        "assertions": grade.get("assertions", []),
        "flags": grade.get("flags", {}),
        "notes": grade.get("notes", []),
        "judge_error": grade.get("judge_error"),
        "error": worst.get("error"),
        "duration_s": worst.get("duration_s"),
        "usage": worst.get("usage", {}),
        "response": worst.get("response"),
        "stripped_fields": worst.get("stripped_fields", []),
        "attempt_statuses": [a["status"] for a in attempts],
    })
    if "transcript" in worst:
        rec["transcript"] = worst["transcript"]
    return rec


def run_cases(cases, agent, judge, cfg: RunConfig) -> list:
    total = len(cases)
    done = [0]
    lock = threading.Lock()

    def one(case):
        rec = run_case(case, agent, judge, cfg)
        if cfg.progress:
            with lock:
                done[0] += 1
                if done[0] % 100 == 0 or done[0] == total:
                    print(f"  {agent.name}: {done[0]}/{total} cases", file=sys.stderr, flush=True)
        return rec

    if cfg.workers <= 1:
        return [one(c) for c in cases]
    with ThreadPoolExecutor(max_workers=cfg.workers) as pool:
        return list(pool.map(one, cases))
