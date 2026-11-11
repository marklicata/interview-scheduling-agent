"""Interview scheduling agent.

`scheduling_agent.core` is host-agnostic: it never imports a host (the evaluation harness, Microsoft 365)
and never reads evaluation data. Hosts live in `scheduling_agent.hosts` and adapt the core to where it runs.
"""

__version__ = "0.1.0"
AGENT_NAME = "scheduling-agent"
