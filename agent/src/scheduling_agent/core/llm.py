"""Model access. The orchestrator talks to `ModelClient`; each implementation routes to a provider.

- AnthropicModel: Claude via the direct Anthropic API (SCHEDULING_MODEL_PROVIDER=anthropic).
- AzureOpenAIModel: OpenAI-format models on Azure AI Foundry (SCHEDULING_MODEL_PROVIDER=foundry).
  Authenticates via managed identity; no API key required when running in Azure.
- CopilotModel: Claude through GitHub Copilot CLI (SCHEDULING_MODEL_PROVIDER=copilot).
  Local development only — requires a signed-in GitHub account.
- ScriptedModel: deterministic replies for tests; no network.

The orchestrator uses Anthropic message format internally. AzureOpenAIModel converts to/from
OpenAI format transparently so the rest of the codebase is provider-agnostic.

Product data is sent for inference only. It is never used for training or fine-tuning (INV-09, LRN-06).
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

# ============================================================================================================
# AGENT MODEL SETTINGS
#
# SCHEDULING_MODEL_PROVIDER - set in the environment to choose how the agent reaches its model:
#   "copilot"   : GitHub Copilot CLI, signed in as your GitHub user (local development; no API key needed)
#   "anthropic" : Direct Anthropic API (prototype deployments; needs ANTHROPIC_API_KEY)
#   "foundry"   : Azure AI Foundry, any deployed OpenAI-format model (production on Azure;
#                 needs AZURE_FOUNDRY_RESOURCE or AZURE_FOUNDRY_ENDPOINT; uses managed identity by default)
# Defaults to "copilot" when unset so local development works without any configuration.
MODEL_PROVIDER = os.environ.get("SCHEDULING_MODEL_PROVIDER", "copilot").lower()
#
# SCHEDULING_AGENT_MODEL - model id or deployment name:
#   copilot   : Claude id with dots, e.g. "claude-opus-5.5"
#   anthropic : Claude id with hyphens, e.g. "claude-opus-5-5"
#   foundry   : Azure deployment name, e.g. "gpt-6.1-sol" or "gpt-6-astra"
AGENT_MODEL_FOUNDRY = "gpt-6.1-sol"  # Azure AI Foundry default deployment
AGENT_MODEL_CLAUDE = "claude-opus-5.5"  # Copilot / Anthropic default
AGENT_MODEL = AGENT_MODEL_FOUNDRY if MODEL_PROVIDER == "foundry" else AGENT_MODEL_CLAUDE
#
# SCHEDULING_AGENT_EFFORT - reasoning effort: "low" | "medium" | "high" | "xhigh" | "max".
AGENT_EFFORT = "high"
#
# AGENT_MODEL and AGENT_EFFORT are the in-code defaults; override per-run with SCHEDULING_AGENT_MODEL
# and SCHEDULING_AGENT_EFFORT without editing this file.
# ============================================================================================================

DEFAULT_MODEL = os.environ.get("SCHEDULING_AGENT_MODEL") or AGENT_MODEL
DEFAULT_EFFORT = os.environ.get("SCHEDULING_AGENT_EFFORT") or AGENT_EFFORT


@dataclass
class ModelTurn:
    blocks: list  # normalized: {"type": "text", "text"} | {"type": "tool_use", "id", "name", "input"}
    raw_content: Any  # what to append to the conversation as the assistant turn
    stop_reason: str
    usage: dict = field(default_factory=dict)


class ModelClient(Protocol):
    name: str

    def complete(self, system: str, tools: list, messages: list) -> ModelTurn: ...


def _normalize(content) -> list:
    out = []
    for b in content or []:
        t = getattr(b, "type", None) if not isinstance(b, dict) else b.get("type")
        get = (
            (lambda k: b.get(k))
            if isinstance(b, dict)
            else (lambda k: getattr(b, k, None))
        )
        if t == "text":
            out.append({"type": "text", "text": get("text") or ""})
        elif t == "tool_use":
            out.append(
                {
                    "type": "tool_use",
                    "id": get("id"),
                    "name": get("name"),
                    "input": get("input") or {},
                }
            )
    return out


class ModelConfigError(RuntimeError):
    """Raised at startup when no model credentials are configured, so a run stops before doing any work."""


def _has_anthropic_profile() -> bool:
    home = os.path.expanduser("~")
    return (
        bool(os.environ.get("ANTHROPIC_PROFILE"))
        or os.path.isdir(os.path.join(home, ".config", "anthropic"))
        or bool(os.environ.get("ANTHROPIC_FEDERATION_RULE_ID"))
    )


# ---------------------------------------------------------------------------
# Anthropic ↔ OpenAI message format helpers (used by AzureOpenAIModel)
# ---------------------------------------------------------------------------


def _to_oai_tools(tools: list) -> list:
    """Anthropic tool format → OpenAI function-calling format."""
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t["input_schema"],
            },
        }
        for t in tools
    ]


def _to_oai_messages(messages: list) -> list:
    """Convert Anthropic-format conversation history to OpenAI messages.

    The orchestrator stores history in Anthropic format (tool_result blocks in user
    messages, thinking blocks in assistant messages). This converts it call-by-call so
    the rest of the codebase stays provider-agnostic.
    """
    result = []
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")

        if isinstance(content, str):
            result.append({"role": role, "content": content})
            continue

        if role == "user":
            tool_msgs: list = []
            text_parts: list = []
            for block in content or []:
                if not isinstance(block, dict):
                    continue
                t = block.get("type")
                if t == "tool_result":
                    # content can be a string or a list of text blocks
                    rc = block.get("content") or ""
                    if isinstance(rc, list):
                        rc = " ".join(
                            b.get("text", "") for b in rc if isinstance(b, dict)
                        )
                    tool_msgs.append(
                        {
                            "role": "tool",
                            "content": rc,
                            "tool_call_id": block.get("tool_use_id", ""),
                        }
                    )
                elif t == "text":
                    text_parts.append(block.get("text", ""))
            result.extend(tool_msgs)
            if text_parts:
                result.append({"role": "user", "content": " ".join(text_parts)})

        elif role == "assistant":
            text_parts = []
            tool_calls = []
            for block in content or []:
                if not isinstance(block, dict):
                    continue
                t = block.get("type")
                if t == "text":
                    text_parts.append(block.get("text", ""))
                elif t == "tool_use":
                    tool_calls.append(
                        {
                            "id": block.get("id", ""),
                            "type": "function",
                            "function": {
                                "name": block.get("name", ""),
                                "arguments": json.dumps(block.get("input") or {}),
                            },
                        }
                    )
                # skip "thinking" blocks — no equivalent in OpenAI
            oai: dict[str, Any] = {
                "role": "assistant",
                "content": " ".join(text_parts) or None,
            }
            if tool_calls:
                oai["tool_calls"] = tool_calls
            result.append(oai)

    return result


def _to_responses_tools(tools: list) -> list:
    """Anthropic tool format → OpenAI Responses API function format."""
    return [
        {
            "type": "function",
            "name": t["name"],
            "description": t.get("description", ""),
            "parameters": t.get("input_schema", {}),
            "strict": False,
        }
        for t in tools
    ]


def _to_responses_input(system: str, messages: list) -> list:
    """Convert system prompt + Anthropic history to Responses API input items.

    The Responses API uses a flat input list where function calls and their outputs
    appear as typed items rather than nested in role messages. This lets reasoning
    models (gpt-6.1-sol, gpt-6-astra) use function tools.
    """
    result: list = [{"role": "system", "content": system}]

    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")

        if isinstance(content, str):
            result.append({"role": role, "content": content})
            continue

        if role == "user":
            tool_results: list = []
            text_parts: list = []
            for block in content or []:
                if not isinstance(block, dict):
                    continue
                t = block.get("type")
                if t == "tool_result":
                    rc = block.get("content") or ""
                    if isinstance(rc, list):
                        rc = " ".join(
                            b.get("text", "") for b in rc if isinstance(b, dict)
                        )
                    tool_results.append(
                        {
                            "type": "function_call_output",
                            "call_id": block.get("tool_use_id", ""),
                            "output": str(rc),
                        }
                    )
                elif t == "text":
                    text_parts.append(block.get("text", ""))
            result.extend(tool_results)
            if text_parts:
                result.append({"role": "user", "content": " ".join(text_parts)})

        elif role == "assistant":
            for block in content or []:
                if not isinstance(block, dict):
                    continue
                t = block.get("type")
                if t == "text":
                    text = block.get("text", "")
                    if text:
                        result.append({"role": "assistant", "content": text})
                elif t == "tool_use":
                    result.append(
                        {
                            "type": "function_call",
                            "call_id": block.get("id", ""),
                            "name": block.get("name", ""),
                            "arguments": json.dumps(block.get("input") or {}),
                        }
                    )

    return result


# ---------------------------------------------------------------------------
# Model implementations
# ---------------------------------------------------------------------------


class AnthropicModel:
    """Claude through the direct Anthropic API (SCHEDULING_MODEL_PROVIDER=anthropic)."""

    def __init__(
        self,
        model: str | None = None,
        effort: str | None = None,
        max_tokens: int = 32000,
        fallbacks: str | None = None,
    ):
        import anthropic

        self.model = (model or DEFAULT_MODEL).replace(
            ".", "-"
        )  # Anthropic API uses hyphens
        self.effort = effort or DEFAULT_EFFORT
        self.max_tokens = max_tokens
        self.name = f"anthropic:{self.model}"

        if not (
            os.environ.get("ANTHROPIC_API_KEY")
            or os.environ.get("ANTHROPIC_AUTH_TOKEN")
            or _has_anthropic_profile()
        ):
            raise ModelConfigError(
                "SCHEDULING_MODEL_PROVIDER=anthropic requires ANTHROPIC_API_KEY to be set."
            )

        self.client = anthropic.Anthropic()
        self.fallbacks = fallbacks or os.environ.get(
            "SCHEDULING_AGENT_FALLBACKS", "default"
        )
        if self.fallbacks == "off":
            self.fallbacks = None

    def complete(self, system: str, tools: list, messages: list) -> ModelTurn:
        extra_body: dict[str, Any] = {"cache_control": {"type": "ephemeral"}}
        betas = []
        if self.fallbacks:
            extra_body["fallbacks"] = self.fallbacks
            betas.append("server-side-fallback-2026-07-01")
        kwargs: dict[str, Any] = dict(
            model=self.model,
            max_tokens=self.max_tokens,
            system=[
                {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
            ],
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
        usage = (
            {
                k: getattr(u, k, 0) or 0
                for k in (
                    "input_tokens",
                    "output_tokens",
                    "cache_read_input_tokens",
                    "cache_creation_input_tokens",
                )
            }
            if u
            else {}
        )
        return ModelTurn(
            blocks=_normalize(msg.content),
            raw_content=msg.content,
            stop_reason=msg.stop_reason or "",
            usage=usage,
        )


class AzureOpenAIModel:
    """OpenAI-format models on Azure AI Foundry (SCHEDULING_MODEL_PROVIDER=foundry).

    Uses DefaultAzureCredential (managed identity in Azure) when AZURE_FOUNDRY_API_KEY is not
    set, so no secret needs to be stored in the Container App.

    The orchestrator stores conversation history in Anthropic format; this class converts
    to OpenAI format on each call and returns ModelTurn with Anthropic-compatible blocks,
    keeping the rest of the codebase provider-agnostic.
    """

    def __init__(self, model: str | None = None, effort: str | None = None):
        try:
            from openai import AzureOpenAI
        except ImportError as exc:
            raise ModelConfigError(
                "SCHEDULING_MODEL_PROVIDER=foundry requires the openai package: "
                'pip install -e "agent[m365]"'
            ) from exc

        resource = os.environ.get("AZURE_FOUNDRY_RESOURCE")
        endpoint = os.environ.get("AZURE_FOUNDRY_ENDPOINT") or (
            # AIServices resources (Cognitive Services) use .cognitiveservices.azure.com
            f"https://{resource}.cognitiveservices.azure.com" if resource else None
        )
        if not endpoint:
            raise ModelConfigError(
                "SCHEDULING_MODEL_PROVIDER=foundry requires AZURE_FOUNDRY_RESOURCE (resource name) "
                "or AZURE_FOUNDRY_ENDPOINT (full URL)."
            )

        self.model = model or DEFAULT_MODEL
        self.effort = effort or DEFAULT_EFFORT
        self.name = f"foundry:{self.model}"
        api_version = os.environ.get("AZURE_FOUNDRY_API_VERSION", "2025-03-01-preview")

        api_key = os.environ.get("AZURE_FOUNDRY_API_KEY")
        if api_key:
            self.client = AzureOpenAI(
                azure_endpoint=endpoint, api_key=api_key, api_version=api_version,
                max_retries=8,  # Retry 429 (token rate limit) with exponential backoff
            )
        else:
            from azure.identity import DefaultAzureCredential, get_bearer_token_provider

            token = get_bearer_token_provider(
                DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default"
            )
            self.client = AzureOpenAI(
                azure_endpoint=endpoint,
                azure_ad_token_provider=token,
                api_version=api_version,
                max_retries=8,  # Retry 429 (token rate limit) with exponential backoff
            )

    def complete(self, system: str, tools: list, messages: list) -> ModelTurn:
        # Azure AI Foundry reasoning models (gpt-6.1-sol, gpt-6-astra) only support
        # function tools through the Responses API, not Chat Completions.
        if tools:
            return self._complete_responses(system, tools, messages)

        # No tools — Chat Completions works fine.
        oai_messages = [{"role": "system", "content": system}]
        oai_messages.extend(_to_oai_messages(messages))
        response = self.client.chat.completions.create(
            model=self.model, messages=oai_messages
        )
        choice = response.choices[0]
        msg = choice.message
        blocks: list = []
        if msg.content:
            blocks.append({"type": "text", "text": msg.content})
        u = response.usage
        usage = {
            "input_tokens": u.prompt_tokens if u else 0,
            "output_tokens": u.completion_tokens if u else 0,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        }
        return ModelTurn(
            blocks=blocks, raw_content=blocks, stop_reason="end_turn", usage=usage
        )

    def _complete_responses(
        self, system: str, tools: list, messages: list
    ) -> ModelTurn:
        """Use the OpenAI Responses API (/v1/responses) for function-tool calls.

        Reasoning models on Azure AI Foundry (gpt-6.1-sol, gpt-6-astra) only support
        function tools via the Responses API, not Chat Completions.
        """
        input_items = _to_responses_input(system, messages)
        res_tools = _to_responses_tools(tools)

        effort_map = {
            "low": "low",
            "medium": "medium",
            "high": "high",
            "xhigh": "high",
            "max": "high",
        }
        effort = effort_map.get(self.effort or "", "medium")

        response = self.client.responses.create(
            model=self.model,
            input=input_items,
            tools=res_tools,
            reasoning={"effort": effort},
        )

        # Parse output items back to Anthropic-compatible blocks.
        blocks: list = []
        for item in response.output or []:
            item_type = (
                getattr(item, "type", None)
                if not isinstance(item, dict)
                else item.get("type")
            )
            if item_type == "message":
                for c in getattr(item, "content", []) or []:
                    ct = (
                        getattr(c, "type", None)
                        if not isinstance(c, dict)
                        else c.get("type")
                    )
                    if ct == "output_text":
                        text = (
                            getattr(c, "text", "")
                            if not isinstance(c, dict)
                            else c.get("text", "")
                        )
                        if text:
                            blocks.append({"type": "text", "text": text})
            elif item_type == "function_call":
                call_id = (
                    getattr(item, "call_id", "")
                    if not isinstance(item, dict)
                    else item.get("call_id", "")
                )
                name = (
                    getattr(item, "name", "")
                    if not isinstance(item, dict)
                    else item.get("name", "")
                )
                args = (
                    getattr(item, "arguments", "{}")
                    if not isinstance(item, dict)
                    else item.get("arguments", "{}")
                )
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": call_id,
                        "name": name,
                        "input": json.loads(args or "{}"),
                    }
                )
            # skip "reasoning" items — no equivalent in Anthropic format

        stop = (
            "tool_use"
            if any(b.get("type") == "tool_use" for b in blocks)
            else "end_turn"
        )
        u = getattr(response, "usage", None)
        usage = {
            "input_tokens": getattr(u, "input_tokens", 0) if u else 0,
            "output_tokens": getattr(u, "output_tokens", 0) if u else 0,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        }
        return ModelTurn(
            blocks=blocks, raw_content=blocks, stop_reason=stop, usage=usage
        )


class ScriptedModel:
    """Test double: `script(system, tools, messages) -> list of blocks` decides each turn."""

    name = "scripted"

    def __init__(self, script: Callable[[str, list, list], list]):
        self.script = script

    def complete(self, system: str, tools: list, messages: list) -> ModelTurn:
        blocks = self.script(system, tools, messages)
        stop = (
            "tool_use"
            if any(b.get("type") == "tool_use" for b in blocks)
            else "end_turn"
        )
        return ModelTurn(
            blocks=_normalize(blocks), raw_content=blocks, stop_reason=stop
        )


class CopilotModel:
    """Claude through GitHub Copilot: the Copilot SDK drives the Copilot CLI runtime as the signed-in GitHub user.

    The Copilot runtime runs the agent loop itself and calls our tools. Safety is unchanged: only this agent's
    tools are exposed (no shell, files, web, MCP, skills, or sub-agents), every runtime permission request is
    rejected, the session starts in an empty directory without custom instructions, and all effects still pass the
    toolbox's write gate.
    """

    loop_owner = True

    def __init__(
        self,
        model: str | None = None,
        effort: str | None = None,
        timeout_s: float = 900,
    ):
        try:
            import copilot  # noqa: F401
        except ImportError as exc:
            raise ModelConfigError(
                "MODEL_PROVIDER = 'copilot' needs the GitHub Copilot SDK: "
                'pip install -e "agent[copilot]" (Python 3.11+).'
            ) from exc
        m = model or DEFAULT_MODEL
        # Copilot spells Claude ids with dots in the version ("claude-opus-5.5"); accept the hyphen spelling too.
        head, _, tail = m.rpartition("-")
        self.model = (
            f"{head}.{tail}"
            if m.startswith("claude-") and tail.isdigit() and head[-1:].isdigit()
            else m
        )
        self.effort = effort or DEFAULT_EFFORT
        self.timeout_s = float(os.environ.get("SCHEDULING_AGENT_TIMEOUT_S", timeout_s))
        self.name = f"copilot:{self.model}"

    def complete(
        self, system: str, tools: list, messages: list
    ) -> ModelTurn:  # pragma: no cover
        raise NotImplementedError("CopilotModel runs its own loop; use run_loop")

    def run_loop(
        self,
        system: str,
        tools: list,
        first_message: str,
        dispatch,
        is_done,
        nudge: str,
    ) -> dict:
        import asyncio

        return asyncio.run(
            self._run(system, tools, first_message, dispatch, is_done, nudge)
        )

    async def _run(
        self, system, tools, first_message, dispatch, is_done, nudge
    ) -> dict:
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
                return ToolResult(
                    text_result_for_llm=text,
                    result_type="failure" if is_err else "success",
                )

            return handler

        sdk_tools = [
            Tool(
                name=t["name"],
                description=t["description"],
                parameters=t["input_schema"],
                handler=make_handler(t["name"]),
                skip_permission=True,
                defer="never",
                is_terminal=(t["name"] == "submit_response"),
            )
            for t in tools
        ]

        def deny(request, invocation):
            return PermissionDecisionReject(
                feedback="Only this agent's scheduling tools are available."
            )

        def on_event(event):
            data = event.data
            if type(data).__name__ == "AssistantUsageData":
                usage["model_calls"] += 1
                for k in ("input_tokens", "output_tokens"):
                    usage[k] += int(getattr(data, k, 0) or 0)

        workdir = tempfile.mkdtemp(prefix="scheduling-agent-")
        async with CopilotClient() as client:
            session = await client.create_session(
                model=self.model,
                reasoning_effort=self.effort,
                tools=sdk_tools,
                available_tools=sorted(ours),
                system_message={"mode": "replace", "content": system},
                on_permission_request=deny,
                working_directory=workdir,
                skip_custom_instructions=True,
                enable_config_discovery=False,
                enable_skills=False,
                on_event=on_event,
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
    p = MODEL_PROVIDER
    if p == "copilot":
        return CopilotModel()
    if p == "foundry":
        return AzureOpenAIModel()
    return AnthropicModel()
