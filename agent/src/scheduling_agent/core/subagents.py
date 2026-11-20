"""Focused secondary LLM calls made from inside tool handlers (not agent loops).

Routes to the right provider based on SCHEDULING_MODEL_PROVIDER:
  "foundry"   — Azure AI Foundry via OpenAI SDK + DefaultAzureCredential (or AZURE_FOUNDRY_API_KEY).
                SCHEDULING_SUBAGENT_MODEL sets the deployment name (default: gpt-6-astra).
  "anthropic" — Anthropic API; SCHEDULING_SUBAGENT_MODEL sets the model (default: claude-haiku-4-5).
  "copilot"   — not supported; always returns None.

If a call fails it returns None and the caller carries on normally.
Product data is sent for inference only (INV-09, LRN-06).
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import date, timedelta

from .timeutil import is_zone, local_to_instant, parse_ts, zone

log = logging.getLogger(__name__)

# Provider-keyed model defaults.  Override with SCHEDULING_SUBAGENT_MODEL.
_SUBAGENT_DEFAULTS = {
    "foundry": "gpt-6-astra",
    "anthropic": "claude-haiku-4-5",
}

# Module-level singletons — one per provider, lazily initialised.
_azure_client = None
_anthropic_client = None


def secondary_call(
    system: str, user: str, schema: dict, max_tokens: int = 1500
) -> dict | None:
    """Make a focused secondary call.  Returns parsed JSON or None if unavailable."""
    if os.environ.get("SCHEDULING_SUBAGENTS") == "off":  # A/B flag for eval comparison
        return None
    provider = os.environ.get("SCHEDULING_MODEL_PROVIDER", "copilot").lower()
    if provider == "foundry":
        return _azure_secondary_call(system, user, schema, max_tokens)
    if provider == "anthropic":
        return _anthropic_secondary_call(system, user, schema, max_tokens)
    # copilot doesn't support headless secondary calls
    return None


def _azure_secondary_call(
    system: str, user: str, schema: dict, max_tokens: int
) -> dict | None:
    """Secondary call via Azure AI Foundry using the OpenAI SDK."""
    global _azure_client
    try:
        from openai import AzureOpenAI

        if _azure_client is None:
            endpoint = os.environ.get("AZURE_FOUNDRY_ENDPOINT")
            api_key = os.environ.get("AZURE_FOUNDRY_API_KEY")
            api_version = os.environ.get(
                "AZURE_FOUNDRY_API_VERSION", "2025-03-01-preview"
            )
            if api_key:
                _azure_client = AzureOpenAI(
                    azure_endpoint=endpoint, api_key=api_key, api_version=api_version
                )
            else:
                from azure.identity import (
                    DefaultAzureCredential,
                    get_bearer_token_provider,
                )

                token = get_bearer_token_provider(
                    DefaultAzureCredential(),
                    "https://cognitiveservices.azure.com/.default",
                )
                _azure_client = AzureOpenAI(
                    azure_endpoint=endpoint,
                    azure_ad_token_provider=token,
                    api_version=api_version,
                )

        model = (
            os.environ.get("SCHEDULING_SUBAGENT_MODEL") or _SUBAGENT_DEFAULTS["foundry"]
        )
        response = _azure_client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "result", "schema": schema, "strict": False},
            },
            # Azure AI Foundry reasoning models use max_completion_tokens (not max_tokens)
            # and do not support temperature.
            max_completion_tokens=max_tokens,
            timeout=30,
        )
        return json.loads(response.choices[0].message.content)
    except Exception as exc:  # noqa: BLE001
        log.warning("azure_secondary_call failed: %s: %s", type(exc).__name__, exc)
        return None


def _anthropic_secondary_call(
    system: str, user: str, schema: dict, max_tokens: int
) -> dict | None:
    """Secondary call via the Anthropic API."""
    global _anthropic_client
    try:
        import anthropic

        _anthropic_client = _anthropic_client or anthropic.Anthropic()
        model = (
            os.environ.get("SCHEDULING_SUBAGENT_MODEL")
            or _SUBAGENT_DEFAULTS["anthropic"]
        )
        r = _anthropic_client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"format": {"type": "json_schema", "schema": schema}},
            extra_body={"temperature": 0},
            timeout=30,
        )
        return json.loads("".join(b.text for b in r.content if b.type == "text"))
    except Exception as exc:  # noqa: BLE001
        log.warning("anthropic_secondary_call failed: %s: %s", type(exc).__name__, exc)
        return None


def _norm(s: str) -> str:
    return re.sub(r"[\s*_`]+", " ", s or "").strip().lower()


# ---------------------------------------------------------------------------
# Sub-agent A: interpret_availability
# ---------------------------------------------------------------------------

INTERPRET_AVAILABILITY_SYSTEM = """\
You audit interview-scheduling conversations for availability statements whose meaning is
genuinely ambiguous. You do not schedule and you do not write prose.

INPUT: NOW, the recruiter's zone, PARTICIPANTS (id, role, profile zone), SESSIONS (id, minutes),
and CONVERSATION entries (role, sent-at timestamp, text).

extracted_windows: one item per availability or busy window stated in the conversation.
Resolve relative dates from the entry's sent-at time in the SENDER's local zone (use NOW if
sent-at is unknown). Zone priority: zone written in the text ("explicit") > location the text
puts the person in ("location_cue") > profile zone ("profile") > "none".
start_local/end_local are "YYYY-MM-DDTHH:MM" in time_zone (an IANA name, or "" when basis is none).

ambiguities: report one ONLY when two plausible readings give different times or dates AND nothing
else in the input settles it:
- timezone: the text places the person somewhere other than their profile zone (moved, relocated,
  visiting, "in Dublin that week", travelling between cities) and the window gives no zone; or
  there is no zone and no profile zone.
- date: a relative or partial date ("tomorrow", "the 12th", "next Tuesday") whose sender-local
  reading differs from how someone else restated it, or that has already passed, or whose weekday
  and date disagree. Compute "tomorrow" from the sender's own local date at sent-at.
- identity: a name that could match two participants.
- duration: "rest is free" / "otherwise open" with no outer bound. Ordinary words like "morning",
  "afternoon", "evenings", "after 6" are NOT ambiguous.
NOT ambiguities: a zone-less time from someone whose profile zone no text contradicts; the
recruiter's own times; anything a later message already settled.

Rules: copy every "quote" character-for-character from one conversation text (shortest phrase
that shows the problem). entry is the [n] of the conversation entry the quote comes from.
participant_id is the person the window belongs to ("" if unknown).
readings: 2+ concrete readings with dates and zones.
ask: one question naming the person and stating both readings.
When in doubt, report no ambiguity.

Example 1 — candidate cand (profile America/Chicago) sent 2027-03-02T23:10-06:00:
"I'm in Lisbon all next week, free Tue 2-4."
-> {"extracted_windows":[{"entry":1,"participant_id":"cand","quote":"free Tue 2-4",
"start_local":"2027-03-09T14:00","end_local":"2027-03-09T16:00","time_zone":"Europe/Lisbon",
"timezone_basis":"location_cue"}],"ambiguities":[{"type":"timezone","participant_id":"cand",
"quote":"free Tue 2-4","readings":["Tue Mar 9 2-4 p.m. Lisbon (WET)","Tue Mar 9 2-4 p.m. Chicago (CST)"],
"ask":"Is your Tuesday, March 9 2-4 p.m. window in Lisbon time or Chicago time?"}]}

Example 2 — interviewer int_raj (profile Asia/Kolkata): "Thursday 10-12 works for me."
-> one window, timezone_basis "profile", time_zone "Asia/Kolkata", "ambiguities": [].\
"""

INTERPRET_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["extracted_windows", "ambiguities"],
    "properties": {
        "extracted_windows": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "entry",
                    "participant_id",
                    "quote",
                    "start_local",
                    "end_local",
                    "time_zone",
                    "timezone_basis",
                ],
                "properties": {
                    "entry": {"type": "integer"},
                    "participant_id": {"type": "string"},
                    "quote": {"type": "string"},
                    "start_local": {"type": "string"},
                    "end_local": {"type": "string"},
                    "time_zone": {"type": "string"},
                    "timezone_basis": {
                        "type": "string",
                        "enum": ["explicit", "location_cue", "profile", "none"],
                    },
                },
            },
        },
        "ambiguities": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["type", "participant_id", "quote", "readings", "ask"],
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": ["timezone", "date", "identity", "duration"],
                    },
                    "participant_id": {"type": "string"},
                    "quote": {"type": "string"},
                    "readings": {"type": "array", "items": {"type": "string"}},
                    "ask": {"type": "string"},
                },
            },
        },
    },
}


def build_interpret_user(inputs: dict) -> str:
    rec = inputs.get("recruiter") or {}
    people = (
        [dict(inputs["candidate"], _role="candidate")]
        if inputs.get("candidate")
        else []
    ) + [dict(i, _role="interviewer") for i in inputs.get("interviewers") or []]
    lines = [
        f"NOW: {inputs.get('now')}",
        f"RECRUITER: {rec.get('name', '')} | zone {rec.get('time_zone') or 'unknown'}",
        "PARTICIPANTS:",
    ]
    lines += [
        f"- {p.get('id')} | {p.get('name', '')} | {p['_role']} | profile zone {p.get('time_zone') or 'NONE'}"
        for p in people
    ]
    lines.append("SESSIONS:")
    lines += [
        f"- {s.get('id')} | {s.get('duration_min')} min | "
        f"{', '.join(s.get('interviewers') or s.get('pool') or [])}"
        for s in (inputs.get("interview_plan") or {}).get("sessions") or []
    ]
    lines.append("CONVERSATION:")
    lines += [
        f"[{n}] {t.get('role')} | sent {t.get('at') or 'unknown'}\n{t.get('text', '')}"
        for n, t in enumerate(inputs.get("conversation") or [], 1)
    ]
    return "\n".join(lines)


_WD = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
_WD_RE = re.compile(r"\b(mon|tue|wed|thu|fri|sat|sun)[a-z]*\b", re.IGNORECASE)


def _amb(kind: str, w: dict, readings: list[str], ask: str) -> dict:
    return {
        "type": kind,
        "participant_id": w["participant_id"],
        "quote": w["quote"],
        "readings": readings,
        "ask": ask,
        "source": "code",
    }


def _code_checks(w: dict, inputs: dict, profile_tz: str | None) -> list[dict]:
    out: list[dict] = []
    # Location-cue zone that disagrees with the profile
    if (
        w["timezone_basis"] == "location_cue"
        and profile_tz
        and w["time_zone"]
        and w["time_zone"] != profile_tz
    ):
        out.append(
            _amb(
                "timezone",
                w,
                [
                    f"{w['time_zone']} (where the text places them)",
                    f"{profile_tz} (profile)",
                ],
                f"Is '{w['quote']}' in {w['time_zone']} time or {profile_tz} time?",
            )
        )
    elif w["timezone_basis"] == "none" and not profile_tz:
        out.append(
            _amb(
                "timezone",
                w,
                ["unknown zone"],
                f"Which time zone is '{w['quote']}' in?",
            )
        )
    try:
        d_s, t_s = w["start_local"].split("T")
        day = date.fromisoformat(d_s)
    except (ValueError, AttributeError):
        return out
    # Weekday vs date conflict
    wd = _WD_RE.search(w["quote"])
    dom = re.search(
        rf"\b{day.day}(st|nd|rd|th)\b|\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)"
        rf"[a-z]*\.?\s+{day.day}\b",
        w["quote"],
        re.IGNORECASE,
    )
    if wd and dom and _WD[day.weekday()] != wd.group(1).lower()[:3]:
        out.append(
            _amb(
                "date",
                w,
                [f"{wd.group(0)} (weekday)", f"{day:%A %B %d} (date)"],
                f"'{w['quote']}': {day:%B %d} is a {day:%A}. Which day is meant?",
            )
        )
    # DST gap / duplicate
    if is_zone(w["time_zone"]):
        r = local_to_instant(d_s, t_s, w["time_zone"])
        if r["status"] != "ok":
            out.append(
                _amb(
                    "timezone",
                    w,
                    r.get("instants") or ["does not exist"],
                    r.get("detail", f"'{w['quote']}' falls in a DST gap or repeats."),
                )
            )
        elif inputs.get("now") and parse_ts(r["instant"]) < parse_ts(inputs["now"]):
            out.append(
                _amb(
                    "date",
                    w,
                    [f"{d_s} (already past)", "a future date"],
                    f"'{w['quote']}' resolves to {d_s}, which has passed. Which date is meant?",
                )
            )
    # "Tomorrow" crossing midnight between sender zone and recruiter zone
    conv = inputs.get("conversation") or []
    rec_tz = (inputs.get("recruiter") or {}).get("time_zone")
    n = w.get("entry", 0)
    if (
        re.search(r"\b(tomorrow|today|tonight)\b", w["quote"], re.IGNORECASE)
        and 1 <= n <= len(conv)
        and conv[n - 1].get("at")
        and isinstance(rec_tz, str)
        and is_zone(rec_tz)
    ):
        sent = parse_ts(conv[n - 1]["at"])
        s_day = sent.date()
        r_day = sent.astimezone(zone(rec_tz)).date()
        if s_day != r_day:
            off = timedelta(
                days=1 if re.search("tomorrow", w["quote"], re.IGNORECASE) else 0
            )
            a, b = s_day + off, r_day + off
            out.append(
                _amb(
                    "date",
                    w,
                    [
                        f"{a:%A %B %d} (sender's local date)",
                        f"{b:%A %B %d} (recruiter's local date)",
                    ],
                    f"'{w['quote']}' was sent on {s_day:%A %B %d} in the sender's zone. "
                    f"Is it {a:%A %B %d} or {b:%A %B %d}?",
                )
            )
    return out


def interpret_availability(inputs: dict) -> dict | None:
    """Run sub-agent A. Returns structured availability/ambiguity analysis or None."""
    raw = secondary_call(
        INTERPRET_AVAILABILITY_SYSTEM, build_interpret_user(inputs), INTERPRET_SCHEMA
    )
    if raw is None:
        return None
    all_text = _norm(
        " \n ".join(t.get("text", "") for t in inputs.get("conversation") or [])
    )
    profile = {
        p.get("id"): p.get("time_zone")
        for p in [inputs.get("candidate") or {}, *(inputs.get("interviewers") or [])]
    }

    def _quote_ok(q: str) -> bool:
        return bool(_norm(q)) and _norm(q) in all_text

    wins = [w for w in raw["extracted_windows"] if _quote_ok(w["quote"])]
    amb = [
        a
        for a in raw["ambiguities"]
        if _quote_ok(a["quote"]) and len(a["readings"]) >= 2
    ]
    seen = {a["quote"] for a in amb}
    for w in wins:
        for a in _code_checks(w, inputs, profile.get(w["participant_id"])):
            if a["quote"] not in seen:
                amb.append(a)
                seen.add(a["quote"])
    return {
        "extracted_windows": wins,
        "ambiguities": amb,
        "needs_clarification": bool(amb),
        "note": "Windows are advisory; convert with convert_times before use.",
    }


# ---------------------------------------------------------------------------
# Sub-agent B: message reviewer
# ---------------------------------------------------------------------------

MESSAGE_REVIEW_SYSTEM = """\
You check whether a scheduling assistant's reply to a recruiter contains each item on a
checklist. You do not judge style and you do not rewrite.

INPUT: DISPOSITION, CHECKLIST (numbered items), FACTS (true details the reply is about), MESSAGE.

For EVERY checklist item return {"n": <item number>, "evidence": "<quote>"}:
- evidence = the shortest span copied character-for-character from MESSAGE that satisfies the item.
  Paraphrase in the message counts as satisfying; you still copy the message's own words.
- If an item has several parts (e.g. "who, when, and what"), the span may be a whole sentence,
  or join two spans with " ... ".
- evidence = "" only if no part of MESSAGE satisfies the item. Read the whole message, including
  lists and later paragraphs, before deciding.
- An item that does not apply to this message (e.g. "if more than one person..." when only one
  person is involved) is satisfied: evidence = "N/A".

Example — CHECKLIST 1. For each conditional option: name the person to ask, its date and time
in the recruiter's zone, and what they must agree to. 2. If more than one person could unlock
the same option, recommend asking one first and stopping at the first yes.
FACTS: option 1 needs Lee Park (tentative hold) for Tue Dec 8 4:00-5:00 p.m. PST.
MESSAGE: "Nothing works for all four. Ask Lee Park whether he can drop his tentative hold for
Tuesday, December 8, 4:00-5:00 p.m. PST. Once he says yes, I'll recheck and book."
-> {"items":[{"n":1,"evidence":"Ask Lee Park whether he can drop his tentative hold for Tuesday, \
December 8, 4:00-5:00 p.m. PST."},{"n":2,"evidence":"N/A"}]}\
"""

REVIEW_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["items"],
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["n", "evidence"],
                "properties": {
                    "n": {"type": "integer"},
                    "evidence": {"type": "string"},
                },
            },
        }
    },
}

# Only the dispositions where a generic checklist reliably finds real gaps (data-validated).
# propose_options, book, reschedule: failures are case-specific — a rubric can't see them.
REVIEW_CHECKLIST: dict[str, list[str]] = {
    "conditional_handoff": [
        (
            "For each conditional option, separately: name the person the recruiter must ask, give "
            "the option's date and time in the recruiter's zone, and say what that person must agree "
            "to or do."
        ),
        (
            "If more than one person could unlock the same option, recommend asking one first and "
            "stopping at the first yes."
        ),
    ],
    "report_infeasible": [
        (
            "State the binding constraint concretely: name the people involved and the specific times "
            "or minutes that conflict."
        ),
        "Offer at least two different concrete next steps the recruiter can take.",
    ],
    "ask_clarification": [
        "Each question names the person or item it is about.",
        (
            "If a question is about an ambiguity, it states the competing readings (both dates, both "
            "zones, or both options) so the recruiter can just pick one."
        ),
    ],
}


def review_message(
    disposition: str,
    message: str,
    facts: list[str],
) -> dict | None:
    """Run sub-agent B. Returns {"missing": [...], "ok": bool} or None.

    Tries Decision-1 rubric grading first (faster, purpose-built for this task).
    Falls back to the LLM secondary call if Decision-1 is unavailable or fails.
    """
    items = REVIEW_CHECKLIST.get(disposition)
    if not items or not message:
        return None

    # Decision-1 path: rubric grading via scored classifier
    from .decision1 import get_client as _get_d1_client

    d1 = _get_d1_client()
    if d1 is not None:
        state_text = "\n".join(
            [
                f"DISPOSITION: {disposition}",
                "FACTS:",
                *(facts or ["- (none)"]),
                "MESSAGE:",
                message,
            ]
        )
        grades = d1.grade_checklist(state_text, items)
        if grades is not None:
            missing = [
                f"Message is missing: {items[n - 1]}"
                for n, satisfied in grades.items()
                if not satisfied
            ]
            return {"missing": missing, "ok": not missing}

    # Fallback: LLM secondary call
    user = "\n".join(
        [
            f"DISPOSITION: {disposition}",
            "CHECKLIST:",
            *[f"{n}. {t}" for n, t in enumerate(items, 1)],
            "FACTS:",
            *(facts or ["- (none)"]),
            "MESSAGE:",
            message,
        ]
    )
    raw = secondary_call(MESSAGE_REVIEW_SYSTEM, user, REVIEW_SCHEMA, max_tokens=800)
    if raw is None:
        return None
    got = {i["n"]: i["evidence"] for i in raw["items"]}
    msg = _norm(message)
    missing: list[str] = []
    for n, text in enumerate(items, 1):
        ev = got.get(n, "")
        if ev == "N/A":
            continue
        parts = [_norm(p) for p in ev.split("...") if _norm(p)]
        if not parts or not all(p in msg for p in parts):
            missing.append(f"Message is missing: {text}")
    return {"missing": missing, "ok": not missing}
