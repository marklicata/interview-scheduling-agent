"""Built-in agents.

null               - the "no agent" baseline: makes no decision and takes no action. Every case should fail.

Harness self-check agents (they read the hidden expected answers, so they only test the harness):
answer_key         - returns the expected disposition, proposes the oracle's valid examples, states the
                     reference local times, and performs exactly the expected sandbox writes. Any
                     deterministic failure points at a harness bug or a case the harness cannot interpret.
invalid_key        - like answer_key but proposes the oracle's invalid examples as valid; every case with
                     a complete invalid example must fail schedule_validity.
unauthorized_writer- like answer_key but also creates an event in cases that allow no writes; every such
                     case must fail side_effects.

Custom agents are loaded with ``module:factory`` (the factory returns an object with name, version, run).
"""
from __future__ import annotations

import importlib
import re
from datetime import timedelta

from case_oracle import Oracle, OracleError, from_min, parse_example

from .common import parse_ts, try_parse_ts
from .dataset import plan_sessions
from .graders import plan_to_oracle_map
from .sandbox import SandboxEnvironment

BUILTIN_AGENTS = ("null", "answer_key", "invalid_key", "unauthorized_writer")


class NullAgent:
    """No agent: returns no decision, no options, no message, and touches nothing."""

    name = "null"
    version = "1"

    def run(self, request, tools):
        return {"disposition": None, "message": ""}


def _word_re(token: str):
    return re.compile(rf"(?<![A-Za-z0-9_]){re.escape(token)}(?![A-Za-z0-9_])")


class AnswerKeyAgent:
    """Self-check agent derived from the case's expected outputs and oracle examples."""

    name = "answer_key"
    version = "1"
    uses_answer_key = True
    example_kind = "valid_examples"

    def run(self, request, tools):  # pragma: no cover - the runner calls run_with_case
        raise RuntimeError("answer_key needs the case; run it through the harness runner")

    # ------------------------------------------------------------------ helpers

    def _oracle(self, case):
        spec = case.get("oracle")
        if spec is None:
            return None
        try:
            return Oracle(spec)
        except OracleError:
            return None

    def _example_sessions(self, oracle, example, case=None):
        try:
            starts, people = parse_example(example)
        except (KeyError, TypeError, ValueError):
            return None
        # Oracles may model rooms or time windows as pseudo-participants; only real interviewers attend.
        known = {str(iv.get("id")) for iv in ((case or {}).get("inputs") or {}).get("interviewers") or []
                 if isinstance(iv, dict)}
        out = []
        for osid, t in starts.items():
            sess = oracle.sessions.get(osid)
            if sess is None:
                return None
            ivs = [a for a in sess["att"] if a not in oracle.candidate_ids and (case is None or a in known)]
            ivs += list(people.get(osid, []))
            out.append({"session_id": osid, "start": from_min(t).isoformat(),
                        "end": from_min(t + sess["d"]).isoformat(), "interviewers": ivs})
        return out

    def _options(self, case, oracle):
        exp = case.get("expected") or {}
        wants = {exp.get("disposition")} | set(exp.get("secondary_dispositions") or [])
        if oracle is None or not (wants & {"propose_options", "book", "reschedule"}):
            return []
        options = []
        for i, ex in enumerate((case.get("oracle") or {}).get(self.example_kind) or []):
            sessions = self._example_sessions(oracle, ex, case)
            if sessions:
                options.append({"option_id": f"{self.example_kind}[{i}]", "status": "proposed", "sessions": sessions})
        return options

    def _stated_times(self, case):
        out = []
        for chk in case.get("local_time_checks") or []:
            if isinstance(chk, dict) and "instant" in chk and "expected_local" in chk:
                st = {"instant": str(chk["instant"]), "time_zone": str(chk.get("time_zone")),
                      "local": str(chk["expected_local"])}
                if chk.get("expected_weekday"):
                    st["weekday"] = str(chk["expected_weekday"])
                if chk.get("expected_utc_offset"):
                    st["utc_offset"] = str(chk["expected_utc_offset"])
                out.append(st)
        return out

    def _session_for_action(self, action, plans):
        text = f"{action.get('target', '')} {action.get('notes', '')}"
        for pid in plans:
            if _word_re(pid).search(text):
                return pid
        if len(plans) == 1:
            return next(iter(plans))
        return None

    def _people_in_text(self, text, inputs):
        found = []
        low = text.lower()
        cand = inputs.get("candidate") or {}
        for iv in inputs.get("interviewers") or []:
            if isinstance(iv, dict) and iv.get("id") is not None:
                name = str(iv.get("name") or "")
                if (name and name.lower() in low) or _word_re(str(iv["id"])).search(text):
                    found.append(str(iv["id"]))
        if (cand.get("name") and str(cand["name"]).lower() in low) or "candidate" in low:
            found.append(str(cand.get("id") or "candidate"))
        found += re.findall(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", text)
        return list(dict.fromkeys(found))

    def _wait_for_backoff(self, tools, throttles, tool, target):
        until = throttles.get((tool, str(target)))
        if until is None:
            return
        wait = (until - parse_ts(tools.now())).total_seconds()
        if wait > 0:
            tools.sleep(int(wait) + 1)

    # ------------------------------------------------------------------ writes

    def _perform_writes(self, case, tools, oracle):
        inputs = case.get("inputs") or {}
        exp = case.get("expected") or {}
        se = exp.get("side_effects") or {}
        if se.get("external_writes") != "as_listed":
            return
        plans = plan_sessions(case)
        mapping = plan_to_oracle_map(oracle, plans) if oracle is not None else {}
        example = None
        if oracle is not None:
            for ex in (case.get("oracle") or {}).get("valid_examples") or []:
                example = self._example_sessions(oracle, ex, case)
                if example:
                    break
        by_oracle = {s["session_id"]: s for s in (example or [])}
        throttles = SandboxEnvironment(inputs).throttles
        organizer = (inputs.get("policies") or {}).get("organizer") if isinstance(inputs.get("policies"), dict) else None
        cand_id = str((inputs.get("candidate") or {}).get("id") or "candidate")
        known = {str(iv.get("id")) for iv in inputs.get("interviewers") or [] if isinstance(iv, dict)}
        now = parse_ts(tools.now())
        placeholder = 0
        created = set()
        for action in se.get("actions") or []:
            if not isinstance(action, dict):
                continue
            kind = action.get("type")
            text = f"{action.get('target', '')} {action.get('notes', '')}"
            sid = self._session_for_action(action, plans)
            if kind == "create_event":
                if sid is None or sid in created:
                    sid = next((p for p in plans if p not in created), sid or "S1")
                created.add(sid)
                osid = mapping.get(sid) if sid else None
                ex = by_oracle.get(osid) if osid else None
                plan = plans.get(sid) or {}
                if ex:
                    start, end, ivs = ex["start"], ex["end"], ex["interviewers"]
                elif osid and oracle.base_starts().get(osid):
                    t = oracle.base_starts()[osid][0]
                    sess = oracle.sessions[osid]
                    start, end = from_min(t).isoformat(), from_min(t + sess["d"]).isoformat()
                    ivs = [x for x in sess["att"] if x not in oracle.candidate_ids and x in known]
                    if sess["pool"]:
                        k = int(sess["pool"].get("count", 1))
                        ivs += [p for p in sess["pool"]["from"] if oracle.free[p].covers(t, t + sess["d"])][:k]
                else:
                    placeholder += 1
                    dur = int(plan.get("duration_min") or 60)
                    st = now + timedelta(days=7, hours=placeholder)
                    st = st.replace(second=0, microsecond=0)
                    start, end = st.isoformat(), (st + timedelta(minutes=dur)).isoformat()
                    ivs = [str(x) for x in plan.get("interviewers") or []] or \
                        [p for p in self._people_in_text(str(action.get("target") or ""), inputs) if p != cand_id]
                self._wait_for_backoff(tools, throttles, "create_event", sid)
                tools.create_event(session_id=sid, subject=str(action.get("target") or ""),
                                   start=start, end=end, attendees=[cand_id] + [x for x in ivs if x != cand_id],
                                   organizer=organizer, online_meeting=str(plan.get("format", "")).startswith("virtual"))
            elif kind in ("update_event", "cancel_event"):
                event_id = self._target_event(tools, text, sid)
                if event_id is None:
                    continue
                self._wait_for_backoff(tools, throttles, kind, event_id)
                if kind == "cancel_event":
                    tools.cancel_event(event_id, message="Cancelled.")
                else:
                    osid = mapping.get(sid) if sid else None
                    ex = by_oracle.get(osid) if osid else None
                    changes = {"start": ex["start"], "end": ex["end"]} if ex else {"body": str(action.get("notes") or "Updated.")}
                    if ex and oracle.sessions[osid]["pool"]:
                        # Pool sessions: the expected change may be an interviewer swap.
                        changes["attendees"] = [cand_id] + [x for x in ex["interviewers"] if x != cand_id]
                    tools.update_event(event_id, changes)
            elif kind == "send_message":
                to = self._people_in_text(str(action.get("target") or ""), inputs) or \
                    [str((inputs.get("recruiter") or {}).get("id") or cand_id)]
                tools.send_message(to=to, subject="Interview scheduling update", body=str(action.get("notes") or ""))
            elif kind == "create_hold":
                people = [p for p in self._people_in_text(str(action.get("target") or ""), inputs)
                          if p != cand_id] or [cand_id]
                st = (now + timedelta(days=7)).replace(second=0, microsecond=0)
                tools.create_hold(attendees=people, start=st.isoformat(), end=(st + timedelta(hours=1)).isoformat(),
                                  subject=str(action.get("target") or "Hold"))
            elif kind == "publish":
                tools.publish(destination=str(action.get("target") or "destination"),
                              payload={"notes": action.get("notes")})
            else:
                tools.create_online_meeting(session_id=sid, subject=str(action.get("target") or ""))

    def _target_event(self, tools, text, sid):
        live = tools.find_events()
        for ev in live:
            if ev.get("event_id") and _word_re(str(ev["event_id"])).search(text):
                return ev["event_id"]
        if sid:
            for ev in live:
                if ev.get("session_id") == sid:
                    return ev["event_id"]
        return live[0]["event_id"] if live else None

    # ------------------------------------------------------------------ entry point

    def run_with_case(self, request, tools, case):
        exp = case.get("expected") or {}
        oracle = self._oracle(case)
        self._perform_writes(case, tools, oracle)
        return {
            "disposition": exp.get("disposition"),
            "secondary_dispositions": list(exp.get("secondary_dispositions") or []),
            "message": str(exp.get("solution_summary") or ""),
            "options": self._options(case, oracle),
            "stated_times": self._stated_times(case),
            "clarifying_questions": [str(t) for t in exp.get("clarifying_question_topics") or []],
        }


class InvalidKeyAgent(AnswerKeyAgent):
    """Proposes the oracle's invalid examples as valid schedules (plus the answer key's other outputs)."""

    name = "invalid_key"
    example_kind = "invalid_examples"

    def _options(self, case, oracle):
        if oracle is None:
            return []
        options = []
        for i, ex in enumerate((case.get("oracle") or {}).get("invalid_examples") or []):
            sessions = self._example_sessions(oracle, ex, case)
            if sessions:
                options.append({"option_id": f"invalid_examples[{i}]", "status": "proposed", "sessions": sessions})
        return options


class UnauthorizedWriterAgent(AnswerKeyAgent):
    """Answer key plus one invitation in every case whose expected external writes are none."""

    name = "unauthorized_writer"

    def run_with_case(self, request, tools, case):
        out = super().run_with_case(request, tools, case)
        se = (case.get("expected") or {}).get("side_effects") or {}
        if se.get("external_writes") == "none":
            inputs = case.get("inputs") or {}
            plans = plan_sessions(case)
            sid = next(iter(plans), "S1")
            now = try_parse_ts(tools.now())
            st = (now + timedelta(days=3)).replace(second=0, microsecond=0)
            tools.create_event(session_id=sid, subject="Interview", start=st.isoformat(),
                               end=(st + timedelta(minutes=30)).isoformat(),
                               attendees=[str((inputs.get("candidate") or {}).get("id") or "candidate")])
        return out


def load_agent(spec: str):
    spec = spec or "null"
    if spec == "null":
        return NullAgent()
    if spec == "answer_key":
        return AnswerKeyAgent()
    if spec == "invalid_key":
        return InvalidKeyAgent()
    if spec == "unauthorized_writer":
        return UnauthorizedWriterAgent()
    if ":" not in spec:
        raise ValueError(f"agent spec must be one of {BUILTIN_AGENTS} or 'module:factory', got {spec!r}")
    mod_name, attr = spec.split(":", 1)
    factory = getattr(importlib.import_module(mod_name), attr)
    if isinstance(factory, type) or (callable(factory) and not hasattr(factory, "run")):
        agent = factory()
    else:
        agent = factory
    for attr_name in ("name", "version", "run"):
        if not hasattr(agent, attr_name):
            raise ValueError(f"agent {spec!r} has no {attr_name!r}")
    return agent
