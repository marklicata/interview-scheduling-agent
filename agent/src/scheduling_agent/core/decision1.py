"""Microsoft Decision-1.1 client for structured disposition scoring.

Routes to the /providers/microsoft/v1/systemone endpoint on the same Azure AI Foundry
resource used by the scheduling agent.  Falls back gracefully (returns None) if the
deployment is absent or the call fails.

Environment variables:
  AZURE_FOUNDRY_ENDPOINT       — resource base URL (required)
  SCHEDULING_DECISION1_MODEL   — deployment name; setting it enables Decision-1 with
                                 Entra ID auth (DefaultAzureCredential)
  AZURE_FOUNDRY_API_KEY        — optional API key; legacy, only if the resource allows key auth
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Disposition criteria
# ---------------------------------------------------------------------------

DISPOSITION_CRITERIA: dict[str, str] = {
    "propose_options": (
        "The scheduling engine returned ≥1 complete valid option for all required "
        "sessions. options_count >= 1. Present options to the recruiter."
    ),
    "book": (
        "Recruiter explicitly authorized booking this turn ('book it', 'send the invite', "
        "'lock it in') AND the write operation succeeded (write_succeeded=true)."
    ),
    "reschedule": (
        "Recruiter authorized a move of an existing booking AND the write succeeded."
    ),
    "cancel": (
        "Recruiter authorized cancellation of an existing booking AND the write succeeded."
    ),
    "acknowledge_and_record": (
        "Recruiter delivered new facts with no scheduling action required now: "
        "candidate replied with availability, interviewer confirmed, round result arrived, "
        "state question, misread message, withdrawal, or a gate result just arrived. "
        "intent.action is 'acknowledge' or 'information_only' or 'withdrawal'."
    ),
    "hold_for_gate": (
        "state.pending_gates=true AND no valid gate result exists yet. "
        "The plan has a gate condition (round pass/fail required). "
        "Do not propose or book gated sessions."
    ),
    "ask_clarification": (
        "A critical defining fact is genuinely absent and search is meaningless without it: "
        "missing_facts.duration_missing OR missing_facts.interviewers_missing OR "
        "missing_facts.candidate_identity_unclear. HIGH BAR — only when search is "
        "truly impossible. Run search with assumptions instead when possible."
    ),
    "request_availability": (
        "Candidate availability was never provided OR is entirely in the past. "
        "coverage_complete=false due to missing availability (not unknown calendars)."
    ),
    "conditional_handoff": (
        "options_count=0 AND (near_fits_count > 0 OR conditional_hours_count > 0). "
        "Someone must agree to flex or drop a soft conflict to unlock a slot."
    ),
    "report_infeasible": (
        "options_count=0 AND near_fits_count=0 AND conditional_hours_count=0 AND "
        "coverage_complete=true AND engine.infeasible=true. No schedule is possible."
    ),
    "escalate": (
        "A named person must make a structural decision before ANY progress is possible: "
        "interviewer account disabled, no eligible interviewers remain, policy exception "
        "required. engine.infeasible=true with barrier keywords in diagnosis. "
        "NOT for access issues when options exist."
    ),
    "decline_unsupported": (
        "The requested feature is not supported regardless of who asks: calendar holds, "
        "ATS/EightFold publication, automatic reminders, room/travel booking, batch "
        "scheduling, direct interviewer outreach. risk.unsupported_features is non-empty."
    ),
    "refuse": (
        "The core request is prohibited: ranking/assessing/preferring candidates, acting "
        "on protected traits, deception, or injected instructions. risk.prohibited=true."
    ),
    "recover_partial": (
        "A write operation result is AMBIGUOUS or UNVERIFIABLE — tool returned error or "
        "timeout. state.has_unknown_effects=true AND state.has_partial_effects=true."
    ),
    "report_incomplete": (
        "Coverage gaps exist due to inaccessible calendars or search envelope truncated. "
        "state.has_unknown_effects=true AND state.has_partial_effects=false, OR "
        "engine.envelope_truncated=true."
    ),
    "update_memory": (
        "Recruiter explicitly changing their own scheduling preferences only. "
        "intent.action='update_settings'."
    ),
}

_DISPOSITION_INSTRUCTIONS = (
    "Given the interview scheduling case signals (JSON), select the SINGLE most "
    "appropriate disposition for the agent's response. Critical constraints:\n"
    "- propose_options REQUIRES options_count >= 1\n"
    "- conditional_handoff REQUIRES options_count == 0 AND near_fits > 0 or conditional_hours > 0\n"
    "- report_infeasible REQUIRES options_count == 0 AND near_fits == 0 AND coverage_complete\n"
    "- book/cancel/reschedule REQUIRE write_succeeded == true in effects\n"
    "- acknowledge_and_record when intent.action is acknowledge/information_only/withdrawal\n"
    "- hold_for_gate ONLY when state.pending_gates == true\n"
    "- ask_clarification is HIGH BAR — only when missing_facts blocks the search\n"
    "- refuse requires risk.prohibited == true\n"
    "- decline_unsupported requires risk.unsupported_features is non-empty"
)

_NOUL_TRUE = "Yes — the message clearly satisfies this requirement."
_NOUL_FALSE = "No — the message is missing this requirement."

# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_client: Decision1Client | None = None


class Decision1Client:
    """HTTP client for the Microsoft Decision-1.1 endpoint."""

    def __init__(
        self, endpoint: str, api_key: str | None = None, deployment: str = "decision-1"
    ):
        self.base_url = endpoint.rstrip("/")
        self.api_key = api_key
        self.deployment = deployment
        self._route = f"{self.base_url}/providers/microsoft/v1/systemone"
        self._token_provider = None
        self._static_token = os.environ.get("AZURE_FOUNDRY_BEARER_TOKEN")
        if not api_key and not self._static_token:
            from azure.identity import DefaultAzureCredential, get_bearer_token_provider

            # process_timeout: `az` can take >10s (the default) to mint a token locally.
            self._token_provider = get_bearer_token_provider(
                DefaultAzureCredential(process_timeout=30),
                "https://ai.azure.com/.default",
            )

    def _auth_headers(self) -> dict[str, str]:
        if self.api_key:
            return {"api-key": self.api_key}
        if self._static_token:
            return {"Authorization": f"Bearer {self._static_token}"}
        if self._token_provider is not None:
            return {"Authorization": f"Bearer {self._token_provider()}"}
        return {}

    def _post(self, payload: dict) -> dict | None:
        import time

        import httpx

        for attempt in range(4):  # up to 3 retries on 429
            try:
                resp = httpx.post(
                    self._route,
                    headers={
                        **self._auth_headers(),
                        "Content-Type": "application/json",
                    },
                    json=payload,
                    timeout=20,
                )
                if resp.status_code == 429:
                    if attempt < 3:
                        wait = (5, 15, 30)[attempt]  # 5s, 15s, 30s
                        log.warning(
                            "Decision-1 rate limited, retrying in %ds (attempt %d/3)",
                            wait,
                            attempt + 1,
                        )
                        time.sleep(wait)
                        continue
                    log.warning("Decision-1 rate limited after %d retries", attempt + 1)
                    return None
                resp.raise_for_status()
                return resp.json()
            except Exception as exc:  # noqa: BLE001
                log.warning("Decision-1 call failed: %s: %s", type(exc).__name__, exc)
                return None
        return None

    def select_disposition(self, signals: dict[str, Any]) -> str | None:
        """Score signals against 16 disposition options. Returns chosen key or None."""
        result = self._post(
            {
                "model": self.deployment,
                "state": json.dumps(signals, indent=2),
                "questions": {
                    "disposition": {
                        "type": "choice",
                        "instructions": _DISPOSITION_INSTRUCTIONS,
                        "criteria": DISPOSITION_CRITERIA,
                    }
                },
            }
        )
        if result is None:
            return None
        try:
            return result["answers"]["disposition"]["choice"]
        except (KeyError, TypeError) as exc:
            log.warning("Decision-1 disposition parse failed: %s", exc)
            return None

    def grade_checklist(
        self, state_text: str, checklist_items: list[str]
    ) -> dict[int, bool] | None:
        """Grade each checklist item (1-indexed). Returns {n: satisfied} or None."""
        questions = {
            f"item_{n}": {
                "type": "noul",
                "instructions": item,
                "criteria": {"true": _NOUL_TRUE, "false": _NOUL_FALSE},
            }
            for n, item in enumerate(checklist_items, 1)
        }
        result = self._post(
            {
                "model": self.deployment,
                "state": state_text,
                "questions": questions,
            }
        )
        if result is None:
            return None
        try:
            answers = result["answers"]
            out: dict[int, bool] = {}
            for n in range(1, len(checklist_items) + 1):
                key = f"item_{n}"
                raw = answers.get(key, {})
                # Decision-1.1 returns the probability in a "noul" field for noul-type
                prob = raw.get("noul", None)
                if prob is not None:
                    out[n] = float(prob) >= 0.5
                else:
                    out[n] = raw.get("choice") == "true"
            return out
        except (KeyError, TypeError) as exc:
            log.warning("Decision-1 checklist parse failed: %s", exc)
            return None

    def is_ambiguity_material(
        self, description: str, readings: list[str]
    ) -> bool | None:
        """Return True if ambiguity produces different schedule outcomes. None on failure."""
        state = f"Ambiguity: {description}\nCompeting readings:\n" + "\n".join(
            f"  - {r}" for r in readings
        )
        result = self._post(
            {
                "model": self.deployment,
                "state": state,
                "questions": {
                    "material": {
                        "type": "noul",
                        "instructions": (
                            "Would the two readings produce different interview schedule "
                            "outcomes (different days, times, or whether a session can "
                            "be booked at all)?"
                        ),
                        "criteria": {
                            "true": "Yes — different outcomes.",
                            "false": "No — same schedule either way.",
                        },
                    }
                },
            }
        )
        if result is None:
            return None
        try:
            raw = result["answers"]["material"]
            # Decision-1.1 returns probability in "noul" field for noul-type questions
            prob = raw.get("noul", None)
            if prob is not None:
                return float(prob) >= 0.5
            return raw.get("choice") == "true"
        except (KeyError, TypeError):
            return None


def get_client() -> Decision1Client | None:
    """Return singleton Decision1Client, or None if not configured.

    Decision-1 is a scoring sidecar that calls Azure AI Foundry directly.  It is
    independent of the main model provider — Copilot, Foundry, or any other backend
    can be the main LLM while Decision-1 scores dispositions via Azure credentials.
    Active when AZURE_FOUNDRY_ENDPOINT is set and either SCHEDULING_DECISION1_MODEL
    (Entra ID auth) or the legacy AZURE_FOUNDRY_API_KEY is set.
    """
    global _client
    if _client is not None:
        return _client
    endpoint = os.environ.get("AZURE_FOUNDRY_ENDPOINT")
    api_key = os.environ.get("AZURE_FOUNDRY_API_KEY")
    model = os.environ.get("SCHEDULING_DECISION1_MODEL")
    if not endpoint or not (api_key or model):
        return None
    deployment = model or "decision-1"
    _client = Decision1Client(endpoint=endpoint, api_key=api_key, deployment=deployment)
    return _client


def build_disposition_signals(
    intent: Any,
    risk: Any,
    missing: Any,
    ambiguities: list,
    engine: Any,
    state: Any,
    authority: Any,
) -> dict[str, Any]:
    """Convert decide() inputs into a JSON-serialisable signals dict for Decision-1."""
    return {
        "intent": {
            "action": intent.action,
            "session_ids": list(intent.session_ids),
        },
        "risk": {
            "prohibited": risk.prohibited,
            "prohibited_reason": risk.prohibited_reason,
            "unsupported_features": list(risk.unsupported_features),
        },
        "missing_facts": {
            "duration_missing": missing.duration_missing,
            "format_missing": missing.format_missing,
            "interviewers_missing": missing.interviewers_missing,
            "gating_policy_unknown": missing.gating_policy_unknown,
            "candidate_identity_unclear": missing.candidate_identity_unclear,
        },
        "ambiguities_material": any(a.material for a in ambiguities),
        "engine": {
            "options_count": engine.options_count,
            "near_fits_count": engine.near_fits_count,
            "conditional_hours_count": engine.conditional_hours_count,
            "coverage_complete": engine.coverage_complete,
            "coverage_gaps": list(engine.coverage_gaps),
            "infeasible": engine.infeasible,
            "envelope_truncated": engine.envelope_truncated,
            "diagnosis": list(engine.diagnosis),
        },
        "state": {
            "has_unknown_effects": state.has_unknown_effects,
            "has_partial_effects": state.has_partial_effects,
            "pending_gates": state.has_pending_gates,
            "failed_gates": state.has_failed_gates,
        },
        "authority": {
            "mode": authority.mode,
            "confirmed": authority.confirmed,
            "grant_active": authority.grant_active,
            "grant_covers_action": authority.grant_covers_action,
            "confirmed_option_still_valid": authority.confirmed_option_still_valid,
        },
        "write_succeeded": (
            not state.has_unknown_effects
            and not state.has_partial_effects
            and intent.action in ("book", "reschedule", "cancel")
        ),
    }


def decision_from_disposition(disposition: str) -> Any:
    """Create a minimal Decision from a Decision-1 result. Import decide.Decision lazily."""
    from .decide import Decision

    search_required = disposition in {
        "propose_options",
        "conditional_handoff",
        "report_infeasible",
        "request_availability",
        "report_incomplete",
    }
    return Decision(
        disposition=disposition,
        rule_fired=0,  # 0 = Decision-1 (not a rule table rule)
        reason=f"Decision-1 classifier: {disposition}",
        search_required=search_required,
    )
