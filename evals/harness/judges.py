"""Pluggable judges for text assertions (expected.must / must_not and other free-text checks).

A judge is any object with ``name``, ``version`` and ``grade(request: JudgeRequest) -> list[JudgeVerdict]``.
It may see everything about the case (including expected answers) plus the agent's response and the
sandbox transcript. Verdict semantics:

  pass     - a ``must`` assertion holds, or a ``must_not`` behavior did NOT occur
  fail     - a ``must`` assertion does not hold, or a ``must_not`` behavior occurred
  ungraded - the judge cannot decide (never counted as pass)

No model provider is wired in. With ``--judge none`` (the default) every text assertion is reported
as ungraded, so a run without a judge can never produce a release PASS.
Load a custom judge with ``--judge package.module:factory``; the factory returns a judge object.
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass, field

VERDICTS = ("pass", "fail", "ungraded")


@dataclass
class Assertion:
    assertion_id: str      # e.g. "must[0]", "must_not[2]", "side_effect[0]", "local_times"
    kind: str              # must | must_not | side_effect_target | clarifying_topics | local_times
    text: str

    def to_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class JudgeRequest:
    case: dict             # the full case, including expected/oracle/grading_notes
    response: dict         # the agent's parsed response
    transcript: dict       # sandbox calls, writes, messages sent, final events
    assertions: list       # list[Assertion]


@dataclass
class JudgeVerdict:
    assertion_id: str
    verdict: str
    rationale: str = ""
    confidence: float = None
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return dict(self.__dict__)


class NoJudge:
    """Marks every text assertion ungraded. Used when no calibrated judge is configured."""

    name = "none"
    version = "1"

    def grade(self, request: JudgeRequest) -> list:
        return [JudgeVerdict(a.assertion_id, "ungraded", "no judge configured") for a in request.assertions]


def load_judge(spec: str):
    if not spec or spec == "none":
        return NoJudge()
    if ":" not in spec:
        raise ValueError(f"judge spec must be 'none' or 'module:factory', got {spec!r}")
    mod_name, attr = spec.split(":", 1)
    factory = getattr(importlib.import_module(mod_name), attr)
    if isinstance(factory, type) or (callable(factory) and not hasattr(factory, "grade")):
        judge = factory()
    else:
        judge = factory
    for attr_name in ("name", "version", "grade"):
        if not hasattr(judge, attr_name):
            raise ValueError(f"judge {spec!r} has no {attr_name!r}")
    return judge
