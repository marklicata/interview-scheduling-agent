"""Engine-only probe for the harness (no model, no network).

    PYTHONPATH=agent/tests python evals/run_harness.py run --agent engine_probe:create_agent --category ...

A scripted "model" searches the plan as written and proposes what the engine returns, so harness
schedule_validity measures the deterministic engine in isolation. Dispositions will mostly be wrong by design.
"""
from scheduling_agent.core import SchedulingAgentCore
from scheduling_agent.core.llm import ScriptedModel
from scheduling_agent.hosts.harness import HarnessAgent


def _script(system, tools, messages):
    last = messages[-1]["content"]
    if isinstance(last, str):
        return [{"type": "tool_use", "id": "t1", "name": "search_schedules", "input": {}}]
    import json
    res = json.loads(last[0]["content"]) if last and last[0].get("type") == "tool_result" else {}
    if "options" in res:
        opts = [{"from_search": o["option_id"]} for o in res["options"]]
        return [{"type": "tool_use", "id": "t2", "name": "submit_response",
                 "input": {"disposition": "propose_options" if opts else "report_infeasible",
                           "message": "probe", "options": opts}}]
    return [{"type": "tool_use", "id": "t3", "name": "submit_response",
             "input": {"disposition": "ask_clarification", "message": f"probe error {res}"}}]


def create_agent():
    return HarnessAgent(SchedulingAgentCore(model=ScriptedModel(_script)))
