"""Evaluation-harness host.

    python evals/run_harness.py run --agent scheduling_agent.hosts.harness:create_agent

The harness passes AgentRequest (inputs only) and its sandboxed provider; the core uses the sandbox exactly as it
uses Microsoft Graph in production. Nothing here reads expected outputs or oracle data.
"""
from __future__ import annotations

from .. import AGENT_NAME, __version__
from ..core import SchedulingAgentCore


class HarnessAgent:
    name = AGENT_NAME
    version = __version__

    def __init__(self, core: SchedulingAgentCore | None = None):
        # Model/service outages (e.g. throttling) surface as evaluation errors, never as graded agent failures.
        self.core = core or SchedulingAgentCore(raise_on_failure=True)

    def run(self, request, tools) -> dict:
        inputs = request.inputs if hasattr(request, "inputs") else request["inputs"]
        return self.core.handle(inputs, tools)


def create_agent() -> HarnessAgent:
    agent = HarnessAgent()
    agent.core.model  # build the model client now: missing credentials stop the run before any case is graded
    return agent
