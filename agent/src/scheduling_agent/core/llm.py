"""Model access. The orchestrator talks to `ModelClient`; implementations choose where Claude is served.

- AnthropicModel: Claude via the Anthropic API (default) or Microsoft Foundry (`provider="foundry"`), which is
  how an Azure-hosted Microsoft 365 deployment reaches Claude.
- CopilotModel: Claude through GitHub Copilot (Copilot SDK driving the Copilot CLI runtime) with the signed-in
  GitHub account. The runtime runs the tool loop; our tools still do all the work.
- ScriptedModel: deterministic replies for tests; no network.

Product data is sent for inference only. It is never used for training or fine-tuning (INV-09, LRN-06).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

# ============================================================================================================
# AGENT MODEL SETTINGS - change these lines to switch.
#
# MODEL_PROVIDER - where the agent gets its model:
#   "copilot"   : GitHub Copilot, signed in as your GitHub user (no API key needed)
#   "anthropic" : Anthropic API (needs ANTHROPIC_API_KEY)
#   "foundry"   : Claude on Microsoft Foundry (needs ANTHROPIC_FOUNDRY_RESOURCE; key or Azure login)
MODEL_PROVIDER = "copilot"
#
# AGENT_MODEL - model id in the provider's spelling. Copilot uses dots ("claude-opus-5.5", "claude-sonnet-5.5");
# the Anthropic API and Foundry use hyphens ("claude-opus-5-5", "claude-sonnet-5-5").
AGENT_MODEL = "claude-opus-5.5"
#
# AGENT_EFFORT - reasoning effort: "low" | "medium" | "high" | "xhigh" | "max".
AGENT_EFFORT = "high"
#
# For one-off experiments (e.g. comparing models in the harness) these can be overridden per run, without editing
# this file, with the environment variables SCHEDULING_AGENT_MODEL and SCHEDULING_AGENT_EFFORT.
# ============================================================================================================

DEFAULT_MODEL = os.environ.get("SCHEDULING_AGENT_MODEL") or AGENT_MODEL
DEFAULT_EFFORT = os.environ.get("SCHEDULING_AGENT_EFFORT") or AGENT_EFFORT


@dataclass
class ModelTurn:
    blocks: list            # normalized: {"type": "text", "text"} | {"type": "tool_use", "id", "name", "input"}
    raw_content: Any        # what to append to the conversation as the assistant turn (keeps thinking blocks)
    stop_reason: str
    usage: dict = field(default_factory=dict)


class ModelClient(Protocol):
    name: str

    def complete(self, system: str, tools: list, messages: list) -> ModelTurn: ...


def _normalize(content) -> list:
    out = []
    for b in content or []:
        t = getattr(b, "type", None) if not isinstance(b, dict) else b.get("type")
        get = (lambda k: b.get(k)) if isinstance(b, dict) else (lambda k: getattr(b, k, None))
        if t == "text":
            out.append({"type": "text", "text": get("text") or ""})
        elif t == "tool_use":
            out.append({"type": "tool_use", "id": get("id"), "name": get("name"), "input": get("input") or {}})
    return out


class ModelConfigError(RuntimeError):
    """Raised at startup when no model credentials are configured, so a run stops before doing any work."""


def _has_anthropic_profile() -> bool:
    home = os.path.expanduser("~")
    return (bool(os.environ.get("ANTHROPIC_PROFILE")) or os.path.isdir(os.path.join(home, ".config", "anthropic"))
            or bool(os.environ.get("ANTHROPIC_FEDERATION_RULE_ID")))


class AnthropicModel:
    """Claude through the Anthropic SDK, on the Anthropic API or Microsoft Foundry."""

    def __init__(self, model: str | None = None, effort: str | None = None, provider: str | None = None,
                 max_tokens: int = 32000, fallbacks: str | None = None):
        import anthropic

        self.model = (model or DEFAULT_MODEL).replace(".", "-")  # the Anthropic API and Foundry spell ids with hyphens
        self.effort = effort or DEFAULT_EFFORT
        self.provider = (provider or MODEL_PROVIDER).lower()
        self.max_tokens = max_tokens
        self.name = f"{self.provider}:{self.model}"
        if self.provider not in ("anthropic", "foundry"):
            raise ModelConfigError("MODEL_PROVIDER in core/llm.py must be 'anthropic', 'foundry', or 'copilot', got "
                                   f"{self.provider!r}")
        if self.provider == "foundry" and not (os.environ.get("ANTHROPIC_FOUNDRY_RESOURCE")
                                               or os.environ.get("ANTHROPIC_FOUNDRY_BASE_URL")):
            raise ModelConfigError("MODEL_PROVIDER = 'foundry' needs ANTHROPIC_FOUNDRY_RESOURCE (the Foundry "
                                   "resource name) set in the same shell that runs the agent.")
        if self.provider == "anthropic" and not (os.environ.get("ANTHROPIC_API_KEY")
                                                 or os.environ.get("ANTHROPIC_AUTH_TOKEN") or _has_anthropic_profile()):
            raise ModelConfigError("No Claude credentials found. In the same shell that runs the agent, set "
                                   "ANTHROPIC_API_KEY, or change MODEL_PROVIDER at the top of core/llm.py.")
        if self.provider == "foundry":
            # Entra ID (managed identity in Azure) when no API key is configured.
            if os.environ.get("ANTHROPIC_FOUNDRY_API_KEY"):
                self.client = anthropic.AnthropicFoundry(api_key=os.environ["ANTHROPIC_FOUNDRY_API_KEY"],
                                                         resource=os.environ.get("ANTHROPIC_FOUNDRY_RESOURCE"))
            else:
                from azure.identity import DefaultAzureCredential, get_bearer_token_provider

                token = get_bearer_token_provider(DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default")
                self.client = anthropic.AnthropicFoundry(azure_ad_token_provider=token,
                                                         resource=os.environ.get("ANTHROPIC_FOUNDRY_RESOURCE"))
            self.fallbacks = None  # server-side fallbacks are not available on Foundry
        else:
            self.client = anthropic.Anthropic()
            self.fallbacks = fallbacks or os.environ.get("SCHEDULING_AGENT_FALLBACKS", "default")
            if self.fallbacks == "off":
                self.fallbacks = None

    def complete(self, system: str, tools: list, messages: list) -> ModelTurn:
        extra_body: dict[str, Any] = {"cache_control": {"type": "ephemeral"}}  # caches the growing tool loop between calls
        betas = []
        if self.fallbacks:
            extra_body["fallbacks"] = self.fallbacks
            betas.append("server-side-fallback-2026-07-01")
        kwargs: dict[str, Any] = dict(
            model=self.model,
            max_tokens=self.max_tokens,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            tools=tools,
            messages=messages,
            thinking={"type": "adaptive"},
            output_config={"effort": self.effort},
            extra_body=extra_body,
        )
        if betas:
            kwargs["betas"] = betas
        with self.client.beta.messages.stream(**kwargs) as stream:
            msg = stream.get_final_message()
        u = getattr(msg, "usage", None)
        usage = {k: getattr(u, k, 0) or 0 for k in ("input_tokens", "output_tokens", "cache_read_input_tokens",
                                                     "cache_creation_input_tokens")} if u else {}
        return ModelTurn(blocks=_normalize(msg.content), raw_content=msg.content, stop_reason=msg.stop_reason or "",
                         usage=usage)


class ScriptedModel:
    """Test double: `script(system, tools, messages) -> list of blocks` decides each turn."""

    name = "scripted"

    def __init__(self, script: Callable[[str, list, list], list]):
        self.script = script

    def complete(self, system: str, tools: list, messages: list) -> ModelTurn:
        blocks = self.script(system, tools, messages)
        stop = "tool_use" if any(b.get("type") == "tool_use" for b in blocks) else "end_turn"
        return ModelTurn(blocks=_normalize(blocks), raw_content=blocks, stop_reason=stop)


class CopilotModel:
    """Claude through GitHub Copilot: the Copilot SDK drives the Copilot CLI runtime as the signed-in GitHub user.

    The Copilot runtime runs the agent loop itself and calls our tools. Safety is unchanged: only this agent's
    tools are exposed (no shell, files, web, MCP, skills, or sub-agents), every runtime permission request is
    rejected, the session starts in an empty directory without custom instructions, and all effects still pass the
    toolbox's write gate.
    """

    loop_owner = True

    def __init__(self, model: str | None = None, effort: str | None = None, timeout_s: float = 900):
        try:
            import copilot  # noqa: F401
        except ImportError as exc:
            raise ModelConfigError("MODEL_PROVIDER = 'copilot' needs the GitHub Copilot SDK: "
                                   'pip install -e "agent[copilot]" (Python 3.11+).') from exc
        m = model or DEFAULT_MODEL
        # Copilot spells Claude ids with dots in the version ("claude-opus-5.5"); accept the hyphen spelling too.
        head, _, tail = m.rpartition("-")
        self.model = f"{head}.{tail}" if m.startswith("claude-") and tail.isdigit() and head[-1:].isdigit() else m
        self.effort = effort or DEFAULT_EFFORT
        self.timeout_s = float(os.environ.get("SCHEDULING_AGENT_TIMEOUT_S", timeout_s))
        self.name = f"copilot:{self.model}"

    def complete(self, system: str, tools: list, messages: list) -> ModelTurn:  # pragma: no cover
        raise NotImplementedError("CopilotModel runs its own loop; use run_loop")

    def run_loop(self, system: str, tools: list, first_message: str, dispatch, is_done, nudge: str) -> dict:
        import asyncio

        return asyncio.run(self._run(system, tools, first_message, dispatch, is_done, nudge))

    async def _run(self, system, tools, first_message, dispatch, is_done, nudge) -> dict:
        import tempfile

        from copilot import CopilotClient, Tool, ToolResult
        from copilot.rpc import PermissionDecisionReject

        usage: dict = {"model_calls": 0, "input_tokens": 0, "output_tokens": 0}
        offered: set = set()
        ours = {t["name"] for t in tools}

        def make_handler(name):
            def handler(invocation):
                offered.update(getattr(invocation, "available_tools", None) or [])
                text, is_err = dispatch(name, invocation.arguments or {})
                return ToolResult(text_result_for_llm=text, result_type="failure" if is_err else "success")
            return handler

        sdk_tools = [Tool(name=t["name"], description=t["description"], parameters=t["input_schema"],
                          handler=make_handler(t["name"]), skip_permission=True, defer="never",
                          is_terminal=(t["name"] == "submit_response"))
                     for t in tools]

        def deny(request, invocation):
            return PermissionDecisionReject(feedback="Only this agent's scheduling tools are available.")

        def on_event(event):
            data = event.data
            if type(data).__name__ == "AssistantUsageData":
                usage["model_calls"] += 1
                for k in ("input_tokens", "output_tokens"):
                    usage[k] += int(getattr(data, k, 0) or 0)

        workdir = tempfile.mkdtemp(prefix="scheduling-agent-")
        async with CopilotClient() as client:
            session = await client.create_session(
                model=self.model, reasoning_effort=self.effort, tools=sdk_tools, available_tools=sorted(ours),
                system_message={"mode": "replace", "content": system}, on_permission_request=deny,
                working_directory=workdir, skip_custom_instructions=True, enable_config_discovery=False,
                enable_skills=False, on_event=on_event,
            )
            try:
                await session.send_and_wait(first_message, timeout=self.timeout_s)
                if not is_done():
                    await session.send_and_wait(nudge, timeout=self.timeout_s)
            finally:
                await session.disconnect()
        extra = sorted(str(x) for x in offered if str(x) not in ours)
        if extra:
            usage["unexpected_tools_offered"] = extra
        return usage


def default_model():
    if MODEL_PROVIDER.lower() == "copilot":
        return CopilotModel()
    return AnthropicModel()
