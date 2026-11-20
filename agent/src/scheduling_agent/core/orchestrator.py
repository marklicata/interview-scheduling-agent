"""The agent loop: one scheduling case in, one response out. Host-agnostic.

Hosts call `SchedulingAgentCore.handle(inputs, provider)` with the case (same shape as the harness's
AgentRequest.inputs) and a CalendarProvider. The result is a plain dict in the response contract's shape.
"""

from __future__ import annotations

import json
import time

from .. import AGENT_NAME, __version__
from .disposition import compute_hint
from .fallbacks import DEFAULT_POLICY, FallbackPolicy
from .llm import ModelClient, default_model
from .pipeline_toolbox import PipelineToolbox
from .prompts import PIPELINE_SYSTEM_PROMPT, SYSTEM_PROMPT
from .toolbox import Toolbox

MAX_STEPS = 40
NUDGE = "You have been working on this for a while. Finish by calling submit_response."
_USE_PIPELINE = (
    False  # Non-pipeline: LLM picks disposition from full context; Decision-1 used as review_message sidecar only
)


def render_case(inputs: dict) -> str:
    case = dict(inputs)
    conv = []
    for i, t in enumerate(inputs.get("conversation") or []):
        conv.append(
            {"turn_index": i, **t}
            if isinstance(t, dict)
            else {"turn_index": i, "text": str(t)}
        )
    case["conversation"] = conv
    return (
        "Scheduling case (JSON). Reply after the last conversation turn.\n\n"
        + json.dumps(case, indent=1, default=str, ensure_ascii=False)
    )


class SchedulingAgentCore:
    name = AGENT_NAME
    version = __version__

    def __init__(
        self,
        model: ModelClient | None = None,
        policy: FallbackPolicy | None = None,
        max_steps: int = MAX_STEPS,
        raise_on_failure: bool = False,
    ):
        """raise_on_failure: re-raise model/service failures instead of answering "I couldn't finish". Evaluation
        hosts set it so outages (e.g. throttling) are recorded as evaluation errors, not as agent mistakes; product
        hosts leave it off so the recruiter always gets a truthful answer."""
        self.raise_on_failure = raise_on_failure
        self._model = model
        self.policy = policy or DEFAULT_POLICY
        self.max_steps = max_steps

    @property
    def model(self) -> ModelClient:
        if self._model is None:
            self._model = default_model()
        return self._model

    def handle(self, inputs: dict, provider) -> dict:
        started = time.monotonic()
        inputs = dict(inputs)
        # Pipeline mode: enabled for real models (Copilot, Anthropic).
        # ScriptedModel (name == "scripted") uses the legacy tool sequence in tests.
        use_pipeline = _USE_PIPELINE and getattr(self.model, "name", "") != "scripted"
        if use_pipeline:
            box: Toolbox = PipelineToolbox(inputs, provider, self.policy)
            system_prompt = PIPELINE_SYSTEM_PROMPT
        else:
            hint = compute_hint(inputs)
            if hint is not None:
                inputs["_disposition_hint"] = hint
            box = Toolbox(inputs, provider, self.policy)
            system_prompt = SYSTEM_PROMPT
        tools = box.tool_definitions()
        messages = [{"role": "user", "content": render_case(inputs)}]
        usage = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "model_calls": 0,
        }
        trace = []
        nudged = False
        error = None

        def dispatch(name, args):
            text, is_err = box.call(name, args)
            trace.append(
                {"tool": name, "input": args, "error": is_err, "result": text[:2000]}
            )
            return text, is_err

        try:
            if getattr(self.model, "loop_owner", False):
                # The model runtime (e.g. GitHub Copilot) runs the loop and calls our tools through dispatch.
                loop_usage = self.model.run_loop(
                    system_prompt,
                    tools,
                    messages[0]["content"],
                    dispatch,
                    lambda: box.final is not None,
                    NUDGE,
                )
                for k, v in loop_usage.items():
                    usage[k] = usage.get(k, 0) + v if isinstance(v, (int, float)) else v
            for _ in range(
                0 if getattr(self.model, "loop_owner", False) else self.max_steps
            ):
                turn = self.model.complete(system_prompt, tools, messages)
                usage["model_calls"] += 1
                for k, v in (turn.usage or {}).items():
                    usage[k] = usage.get(k, 0) + (v or 0)
                messages.append({"role": "assistant", "content": turn.raw_content})
                calls = [b for b in turn.blocks if b["type"] == "tool_use"]
                if turn.stop_reason == "refusal":
                    error = "the model declined this request"
                    break
                if not calls:
                    if box.final is not None or nudged:
                        break
                    nudged = True
                    messages.append({"role": "user", "content": NUDGE})
                    continue
                results = []
                for c in calls:
                    text, is_err = dispatch(c["name"], c["input"])
                    results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": c["id"],
                            "content": text,
                            **({"is_error": True} if is_err else {}),
                        }
                    )
                messages.append({"role": "user", "content": results})
                if box.final is not None:
                    break
        except Exception as exc:  # model/provider outage: report truthfully, never hide effects already caused
            if self.raise_on_failure:
                raise
            error = f"{type(exc).__name__}: {exc}"

        usage["elapsed_s"] = round(time.monotonic() - started, 2)
        if box.final is not None:
            resp = dict(box.final)
        else:
            resp = self._incomplete(box, error)
        resp["usage"] = usage
        if box.settings_changed:
            resp["recruiter_settings"] = dict(box.saved_settings)
        if box.case_changed:
            resp["case_facts"] = {
                k: box.inputs.get(k)
                for k in ("candidate", "interviewers", "interview_plan", "policies")
                if box.inputs.get(k) is not None
            }
        resp["trace"] = {
            "agent": f"{self.name} {self.version}",
            "model": getattr(self.model, "name", "?"),
            "effects": box.effects,
            "assumptions": box.assumptions,
            "tool_calls": trace,
            **({"error": error} if error else {}),
        }
        return resp

    @staticmethod
    def _incomplete(box: Toolbox, error: str | None) -> dict:
        done = [
            e
            for e in box.effects
            if e["status"] in ("success", "deduplicated", "already_exists")
        ]
        lines = ["I couldn't finish this request" + (f" ({error})." if error else ".")]
        if done:
            lines.append(
                "These actions did complete: "
                + "; ".join(
                    f"{e['action']} {e.get('target')} ({e['status']})" for e in done
                )
                + "."
            )
        else:
            lines.append("Nothing was booked, changed, or sent.")
        lines.append("Please retry, or take this case over manually.")
        return {
            "disposition": "recover_partial" if done else "report_incomplete",
            "message": " ".join(lines),
            "secondary_dispositions": [],
            "options": [],
            "clarifying_questions": [],
            "stated_times": [],
            "drafts": [],
            "memory_updates": [],
        }
