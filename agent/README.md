# Interview scheduling agent

The agent described in `docs/interview-scheduling-requirements.md`. One host-agnostic core, graded by the evaluation
harness in `evals/`, and installed in Microsoft 365 Copilot and Teams as a custom engine agent.

```
agent/src/scheduling_agent/
  core/                  host-agnostic; never imports hosts or evaluation code (enforced by a test)
    orchestrator.py      the agent loop: case in, response out
    prompts.py           system prompt (requirements condensed into operating rules)
    toolbox.py           tools the model calls, plus the code that enforces safety around them
    scheduler.py         deterministic search, validation, near fits, ranking
    availability.py      per-participant availability with provenance (stated, supplied, calendar)
    fallbacks.py         defaults that keep work moving when a fact is missing; always disclosed
    authority.py         the write gate (confirmation and grant checks)
    timeutil.py          time zones, DST gaps and folds, local-time rendering
    llm.py               Claude on the Anthropic API or Microsoft Foundry; scripted model for tests
    ports.py             the calendar/mail provider interface
  providers/memory.py    in-memory provider (tests, offline tries)
  hosts/harness.py       evaluation-harness entry point
  hosts/m365/            Microsoft 365 Agents SDK host, Microsoft Graph provider, case store, app package
```

## Design

**The model reasons; code guarantees.** Cases arrive with a long tail of free-text facts (policies, a newer
candidate email, "Warsaw folks work 9-5"), so Claude reads the case and decides what to do. What must never be
wrong is deterministic:

| Concern | Enforced by |
|---|---|
| Valid schedules (durations, panels, pools, order, breaks, same day, caps, locks, notice) | `scheduler.py`. The model can present as valid only what `search_schedules` returns or `check_schedule` accepts; `submit_response` re-checks every proposed option. |
| Time math | `timeutil.py`. The model never converts times; `stated_times` are computed. |
| Unknown is never free | `availability.py`. Only a provider status of `ok` turns an empty calendar into free time. |
| Authority (INV-01, CFG-*) | `authority.py`. A confirmation must quote the acting recruiter's own words, after the latest proposal. A grant must be active and cover the action, dates, candidate, and organizer. "Draft only" beats any grant. Read-only users never write. |
| Duplicates and retries (INV-05, ACT-03/04/10) | `toolbox.py`: idempotency keys, an existing-event lookup, booking locks, Retry-After windows from provider history. |
| Rechecks before acting (AVL-05) | `create_event` and `update_event` re-read availability and refuse conflicts the recruiter has not reported as agreed. |
| Targets (INV-07) and recipients (INV-08) | Updates and cancellations only touch this candidate's events; messages only go to people in the case. |
| Truthful claims (INV-04) and privacy (INV-06) | "Booked" options must match real events. Candidate drafts cannot contain calendar subjects that were read. |

**Recruiters control their own scheduling preferences by chat** (`core/settings.py`). They ask the agent, for example
"assume 8 to 4 when someone's hours aren't set", "only show me two options", or "prefer afternoons", and it
updates their settings: default working hours, near-fit stretch window, search days, number of options, minimum notice,
preferred time of day, and which time zone to show first. "What are my settings?" shows each value and its source;
"reset" restores defaults. Settings are personal (stored per recruiter by the host), never grant authority, can't
loosen policy, and only the recruiter's own messages can change them. The values in code are only the defaults.

**Fallbacks keep work moving** (`fallbacks.py`). When a convenience fact is missing, the agent uses a default,
records it as an assumption, and tells the recruiter so they can correct it:

| Missing | Fallback chain |
|---|---|
| Working hours | person's working hours, then acceptable hours, then Outlook working hours (Graph), then **09:00-17:00 local, every day** |
| Time zone | person's zone, then the working-hours zone, then the zone of their stated location, then the plan zone, then the recruiter's zone |
| Search window | request, then 30 days from now |

Working hours are never widened from the calendar. If a schedule works except that one person would meet outside their
working hours (their Outlook settings, or the assumed default), it is returned as a conditional option for the recruiter to
check with that person, within a sane stretch band (`SCHEDULING_STRETCH_START/END`, default 07:00-20:00 their time).

There is deliberately no fallback for an interview's duration or format: those define the interview, so the agent
asks (requirements INT-05, CON-10). Defaults are configurable (`SCHEDULING_DEFAULT_WORK_START/END/DAYS`, `SCHEDULING_DEFAULT_SEARCH_DAYS`).
Fallbacks never make unknown availability free and never create authority. A person whose availability is unknown
still gets the work moving: the engine returns a conditional option saying what must be confirmed, and with whom.

## Run it

```powershell
python -m pip install -e agent                     # or: -e "agent[m365,dev]" in a virtual environment
python -m pytest agent/tests                         # unit tests: no network, no model

# Evaluate with the harness (needs Claude credentials; see below)
python evals/run_harness.py run --agent scheduling_agent.hosts.harness:create_agent --limit 20

# Engine only, no model: what the deterministic engine gets right on its own
$env:PYTHONPATH = "agent/tests"; python evals/run_harness.py run --agent engine_probe:create_agent
```

**Model provider.** Set once, at the top of `src/scheduling_agent/core/llm.py`: `MODEL_PROVIDER = "copilot"`
(the default) reaches Claude through the GitHub Copilot SDK as your signed-in GitHub user, with no API key. The
alternatives are `"anthropic"` (`ANTHROPIC_API_KEY`) and `"foundry"` (Claude on Microsoft Foundry, for Azure-hosted
Microsoft 365 deployments). Copilot needs the SDK: `pip install -e "agent[copilot]"` (Python 3.11+).

The Copilot runtime runs the tool loop, but it can only call this agent's tools: built-in shell, file, web, MCP,
skills, and sub-agents are not exposed, every runtime permission request is rejected, and sessions start in an empty
directory with no custom instructions. All effects still pass the write gate.

Optional model settings: `SCHEDULING_AGENT_MODEL` (default `claude-opus-5.5` with Copilot, `claude-opus-5-5`
otherwise) and `SCHEDULING_AGENT_EFFORT` (default `high`). Foundry uses `ANTHROPIC_FOUNDRY_RESOURCE` with either
`ANTHROPIC_FOUNDRY_API_KEY` or Entra ID. On the Anthropic API, server-side refusal fallbacks are enabled
(`SCHEDULING_AGENT_FALLBACKS=off` to disable).

## Microsoft 365 Copilot and Teams

`hosts/m365/` is a custom engine agent built on the Microsoft 365 Agents SDK:

- `app.py` serves `POST /api/messages`. Each conversation is a durable case. The host acknowledges at once, runs
  the core in the background, and posts the reply proactively, because scheduling turns can outlast the channel's
  reply timeout.
- `graph_tools.py` implements the same provider interface as the harness sandbox, on Microsoft Graph: `getSchedule`
  (keeping tentative, OOF, private, and working hours), events with `transactionId` plus extended properties for
  idempotency, Teams meetings, `sendMail`, and directory lookup.
- `case_store.py` keeps cases between turns (JSON files for development; use a tenant-scoped store in production).
- `appPackage/manifest.json` (manifest 1.22, `copilotAgents.customEngineAgents`) makes the bot available in
  Microsoft 365 Copilot and Teams. The icons are placeholders.
- `env.sample` lists the configuration.

Local try-out without a tenant: set `SCHEDULING_PROVIDER=memory`, run `python -m scheduling_agent.hosts.m365.app`,
and chat through the Microsoft 365 Agents Playground. Deployment: register an Azure Bot (single-tenant Entra app),
add an OAuth connection named `graph` with the Graph scopes in `env.sample`, host the app (for example Azure App
Service), fill the manifest placeholders, and upload the app package. Microsoft 365 Agents Toolkit can do this.

**Not verified yet:** the Microsoft 365 host and the Graph provider have unit tests (Graph is mocked) and the app
starts, but they have not run against a real tenant, bot registration, or Copilot. Before enabling writes,
confirm in a test tenant: consent and scopes, shared-mailbox organizer behavior, extended-property filtering,
`getSchedule` visibility for the recruiting population, and the Copilot channel's behavior with proactive replies.

## Data use

Case data, conversations, and evaluation artifacts are used only to serve the request (inference). They are never
used to train or fine-tune models, by us or any provider (INV-09, LRN-06).
