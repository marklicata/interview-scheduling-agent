"""Microsoft 365 host: a custom engine agent for Microsoft 365 Copilot and Teams (Microsoft 365 Agents SDK).

    python -m scheduling_agent.hosts.m365.app        # serves POST /api/messages on port 3978

Each Teams/Copilot conversation is one durable scheduling case (case_store.py). On every message:
1. acknowledge immediately (turns can outlast the channel's reply timeout);
2. load the case, append the recruiter's turn, set `now` and the recruiter's time zone from the client;
3. run the same core the evaluation harness grades, with Microsoft Graph as the calendar provider (or the
   in-memory provider when SCHEDULING_PROVIDER=memory, for local tries without a tenant);
4. post the reply proactively and save the case (facts, proposals, effects, memory).

Configuration (environment; see env.sample): the Agents SDK connection (CONNECTIONS__SERVICE_CONNECTION__...),
an OAuth handler named GRAPH for delegated Graph access, SCHEDULING_ORGANIZER (shared recruiting mailbox, optional),
and the model settings in core/llm.py (set MODEL_PROVIDER = "foundry" there for Claude on Microsoft Foundry).
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone

from aiohttp.web import Application, Request, Response, run_app
from microsoft_agents.activity import Activity, ActivityTypes, load_configuration_from_env
from microsoft_agents.authentication.msal import MsalConnectionManager
from microsoft_agents.hosting.aiohttp import CloudAdapter, jwt_authorization_middleware, start_agent_process
from microsoft_agents.hosting.core import AgentApplication, Authorization, MemoryStorage, TurnContext, TurnState

from ...core import SchedulingAgentCore
from ...core.timeutil import is_zone, iso
from ...providers.memory import InMemoryCalendar
from .case_store import JsonFileCaseStore
from .graph_tools import GraphCalendar

log = logging.getLogger("scheduling_agent.m365")

CONFIG = load_configuration_from_env(os.environ)
STORAGE = MemoryStorage()
CONNECTION_MANAGER = MsalConnectionManager(**CONFIG)
ADAPTER = CloudAdapter(connection_manager=CONNECTION_MANAGER)
_USE_GRAPH = os.environ.get("SCHEDULING_PROVIDER", "graph") == "graph"
AUTHORIZATION = Authorization(STORAGE, CONNECTION_MANAGER, **CONFIG) if _USE_GRAPH else None
_AUTH_KW = {"authorization": AUTHORIZATION} if AUTHORIZATION else {"connection_manager": CONNECTION_MANAGER}
AGENT_APP = AgentApplication[TurnState](storage=STORAGE, adapter=ADAPTER, **_AUTH_KW, **CONFIG)
CORE = SchedulingAgentCore()
CASES = JsonFileCaseStore()
APP_ID = (CONFIG.get("CONNECTIONS", {}).get("SERVICE_CONNECTION", {}).get("SETTINGS", {}) or {}).get("CLIENTID", "")

HELP = ("I schedule interviews. Tell me the candidate, their availability, and the interview plan (or the "
        "interviewers and durations), and I'll find valid times. I only book after you confirm. "
        "Commands: `new case`, `show case`.")


def _case_key(activity: Activity) -> str:
    user = getattr(activity.from_property, "aad_object_id", None) or activity.from_property.id
    return f"{activity.channel_id}:{user}:{activity.conversation.id}"


def _settings_key(activity: Activity) -> str:
    user = getattr(activity.from_property, "aad_object_id", None) or activity.from_property.id
    return f"settings:{activity.channel_id}:{user}"


def _new_case(activity: Activity) -> dict:
    tz = activity.local_timezone if is_zone(activity.local_timezone) else os.environ.get("SCHEDULING_DEFAULT_TZ", "UTC")
    user = activity.from_property
    return {"inputs": {
        "recruiter": {"id": getattr(user, "aad_object_id", None) or user.id, "name": user.name, "time_zone": tz},
        "authority": {"mode": "confirm_each_action"},
        "conversation": [], "case_state": {"memory": {}, "prior_proposals": [], "action_log": []},
    }}


def _provider(inputs: dict, token: str | None):
    people = {}
    cand = inputs.get("candidate") or {}
    if cand.get("id"):
        people[cand["id"]] = cand.get("email")
    for i in inputs.get("interviewers") or []:
        people[i.get("id")] = i.get("email") or (i.get("id") if "@" in str(i.get("id")) else None)
    if _USE_GRAPH and token:
        return GraphCalendar(token, os.environ.get("SCHEDULING_ORGANIZER"), people,
                             {cand.get("id")} if cand.get("id") else set())
    return InMemoryCalendar(inputs["now"])


def _render(resp: dict) -> str:
    return resp.get("message") or "I couldn't produce a reply."


def _apply(record: dict, resp: dict, now: str) -> None:
    inputs = record["inputs"]
    if "recruiter_settings" in resp:
        inputs.setdefault("recruiter", {})["settings"] = resp["recruiter_settings"]
    for k, v in (resp.get("case_facts") or {}).items():
        inputs[k] = v
    inputs["conversation"].append({"role": "agent", "at": now, "text": resp.get("message", "")})
    state = inputs.setdefault("case_state", {})
    if resp.get("options"):
        state.setdefault("prior_proposals", []).append({"at": now, "options": resp["options"]})
    for e in (resp.get("trace") or {}).get("effects") or []:
        state.setdefault("action_log", []).append(e)
    for m in resp.get("memory_updates") or []:
        state.setdefault("memory", {})[str(m.get("key") or len(state["memory"]))] = m


async def _run_case(activity: Activity, record: dict, token: str | None, key: str) -> None:
    inputs = record["inputs"]
    try:
        resp = await asyncio.to_thread(CORE.handle, inputs, _provider(inputs, token))
    except Exception:  # never leave the recruiter without an answer
        log.exception("scheduling turn failed")
        resp = {"message": "Something went wrong and I stopped. Nothing new was booked or sent."}
    now = iso(datetime.now(timezone.utc))
    _apply(record, resp, now)
    CASES.save(key, record)
    if "recruiter_settings" in resp:  # personal settings follow the recruiter into every case
        CASES.save(_settings_key(activity), {"settings": resp["recruiter_settings"]})

    async def send(ctx: TurnContext):
        await ctx.send_activity(_render(resp))

    reference = activity.get_conversation_reference()
    await ADAPTER.continue_conversation(APP_ID, reference.get_continuation_activity(), send)


@AGENT_APP.conversation_update("membersAdded")
async def on_members_added(context: TurnContext, _state: TurnState):
    await context.send_activity(HELP)


@AGENT_APP.activity("message", auth_handlers=["GRAPH"] if _USE_GRAPH else None)
async def on_message(context: TurnContext, _state: TurnState):
    activity = context.activity
    text = (activity.text or "").strip()
    key = _case_key(activity)
    if text.lower() in ("new case", "/new", "reset"):
        CASES.delete(key)
        await context.send_activity("Started a new case. Who are we scheduling?")
        return
    record = CASES.load(key) or _new_case(activity)
    if text.lower() in ("show case", "/case"):
        i = record["inputs"]
        await context.send_activity(
            f"Candidate: {(i.get('candidate') or {}).get('name', 'not set')}. "
            f"Plan: {(i.get('interview_plan') or {}).get('name', 'not set')}. "
            f"Interviewers: {', '.join(x.get('name', x.get('id', '?')) for x in i.get('interviewers') or []) or 'none'}. "
            f"Authority: {i.get('authority', {}).get('mode')}.")
        return
    now = datetime.now(timezone.utc)
    saved = CASES.load(_settings_key(activity)) or {}
    record["inputs"].setdefault("recruiter", {})["settings"] = saved.get("settings", {})
    record["inputs"]["now"] = iso(now)
    record["inputs"]["conversation"].append({"role": "recruiter", "at": iso(now), "text": text})
    token = None
    if _USE_GRAPH:
        token = (await AGENT_APP.auth.get_token(context, "GRAPH")).token
    await context.send_activity(Activity(type=ActivityTypes.typing))
    await context.send_activity("Working on it...")
    asyncio.get_running_loop().create_task(_run_case(activity, record, token, key))


async def entry_point(req: Request) -> Response:
    return await start_agent_process(req, req.app["agent_app"], req.app["adapter"])


def create_app() -> Application:
    app = Application(middlewares=[jwt_authorization_middleware])
    app.router.add_post("/api/messages", entry_point)
    app["agent_configuration"] = CONNECTION_MANAGER.get_default_connection_configuration()
    app["agent_app"] = AGENT_APP
    app["adapter"] = AGENT_APP.adapter
    return app


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_app(create_app(), host=os.environ.get("HOST", "localhost"), port=int(os.environ.get("PORT", "3978")))
