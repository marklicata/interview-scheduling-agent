"""Shared fixtures for harness tests."""
import copy
import os
import sys

EVALS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if EVALS not in sys.path:
    sys.path.insert(0, EVALS)

import harness  # noqa: E402,F401  (puts evals/tools on sys.path)
from case_oracle import load_yaml  # noqa: E402

EXEMPLARS = os.path.join(EVALS, "fragments", "EXEMPLARS.yaml")
_CACHE = {}


def exemplar(case_id: str) -> dict:
    if "doc" not in _CACHE:
        _CACHE["doc"] = load_yaml(EXEMPLARS)
    for case in _CACHE["doc"]["test_cases"]:
        if case["id"] == case_id:
            return copy.deepcopy(case)
    raise KeyError(case_id)


def synthetic_inputs(**overrides) -> dict:
    inputs = {
        "now": "2026-11-05T09:00:00-08:00",
        "recruiter": {"id": "rec_x", "name": "Rae Recruiter", "time_zone": "America/Los_Angeles"},
        "authority": {"mode": "confirm_each_action"},
        "candidate": {"id": "candidate", "name": "Casey Candidate", "email": "casey@example.com",
                      "time_zone": "America/Los_Angeles",
                      "availability": [{"start": "2026-11-10T09:00:00-08:00", "end": "2026-11-10T12:00:00-08:00"}]},
        "interview_plan": {"name": "Test plan", "sessions": [
            {"id": "S1", "title": "Technical", "format": "virtual", "duration_min": 60, "interviewers": ["int_a"]},
            {"id": "S2", "title": "Manager", "format": "virtual", "duration_min": 30, "interviewers": ["int_b"]},
        ]},
        "interviewers": [
            {"id": "int_a", "name": "Ana Alpha", "email": "ana@contoso.example", "time_zone": "America/Los_Angeles",
             "availability_source": "calendar",
             "calendar": [
                 {"start": "2026-11-10T09:00:00-08:00", "end": "2026-11-10T10:00:00-08:00", "status": "busy"},
                 {"start": "2026-11-10T13:00:00-08:00", "end": "2026-11-10T14:00:00-08:00",
                  "status": "private_busy", "subject": "Oncology follow-up (private)"},
             ]},
            {"id": "int_b", "name": "Ben Beta", "time_zone": "America/Los_Angeles", "availability_source": "supplied",
             "supplied_availability": [{"start": "2026-11-10T10:00:00-08:00", "end": "2026-11-10T12:00:00-08:00"}]},
            {"id": "int_c", "name": "Cy Gamma", "time_zone": "America/Los_Angeles", "availability_source": "calendar",
             "calendar_access": {"result": "permission_denied", "error": "ErrorAccessDenied"}, "calendar": []},
            {"id": "int_d", "name": "Di Delta", "time_zone": "America/Los_Angeles", "availability_source": "calendar"},
        ],
        "policies": {"organizer": "recruiting@contoso.example"},
        "conversation": [{"role": "recruiter", "text": "Please find times for Casey on Tuesday."}],
    }
    inputs.update(overrides)
    return inputs


def synthetic_case(**overrides) -> dict:
    case = {
        "id": "BAS-999",
        "title": "Synthetic two-session test case",
        "category": "basic_scheduling",
        "description": "Synthetic case used by harness unit tests only.",
        "difficulty": "basic",
        "severity": "critical",
        "release_scope": "mvp",
        "interview_types": ["virtual_loop"],
        "tags": ["synthetic"],
        "requirement_refs": ["INV-01"],
        "inputs": synthetic_inputs(),
        "expected": {
            "disposition": "propose_options",
            "solution_summary": "S1 10:00-11:00, S2 11:10-11:40 PST.",
            "must": ["Propose S1 after Ana's 9:00-10:00 meeting."],
            "must_not": ["Book anything without confirmation."],
            "side_effects": {"external_writes": "none"},
        },
        "local_time_checks": [
            {"instant": "2026-11-10T18:00:00Z", "time_zone": "America/Los_Angeles",
             "expected_local": "2026-11-10T10:00", "expected_weekday": "Tuesday", "expected_utc_offset": "-08:00"},
        ],
        "oracle": {
            "granularity_min": 5,
            "search_window": {"start": "2026-11-10T08:00:00-08:00", "end": "2026-11-10T18:00:00-08:00"},
            "participants": {
                "candidate": {"available": [["2026-11-10T09:00:00-08:00", "2026-11-10T12:00:00-08:00"]]},
                "int_a": {"busy": [["2026-11-10T09:00:00-08:00", "2026-11-10T10:00:00-08:00"]]},
                "int_b": {"available": [["2026-11-10T10:00:00-08:00", "2026-11-10T12:00:00-08:00"]]},
            },
            "sessions": [
                {"id": "S1", "duration_min": 60, "attendees": ["candidate", "int_a"]},
                {"id": "S2", "duration_min": 30, "attendees": ["candidate", "int_b"]},
            ],
            "constraints": {"order": ["S1", "S2"], "min_gap_min": 10},
            "expect_feasible": True,
            "valid_examples": [{"S1": "2026-11-10T10:00:00-08:00", "S2": "2026-11-10T11:10:00-08:00"}],
            "invalid_examples": [{"S1": "2026-11-10T09:30:00-08:00", "S2": "2026-11-10T11:00:00-08:00"}],
        },
    }
    case.update(overrides)
    return case


def option(*sessions, status="proposed"):
    return {"status": status, "sessions": [
        {"session_id": sid, "start": start, "end": end, "interviewers": list(people)}
        for sid, start, end, people in sessions
    ]}
