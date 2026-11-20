#!/usr/bin/env bash
# Launch the eval harness with Copilot as the main model.
# Decision-1 scores via Azure AI Foundry directly (httpx sidecar).
set -e
cd "$(dirname "$0")/.."

export SCHEDULING_MODEL_PROVIDER=copilot
export SCHEDULING_AGENT_MODEL=claude-opus-5.5
export SCHEDULING_AGENT_EFFORT=high

# Decision-1 sidecar credentials — used by httpx regardless of main model provider
export AZURE_FOUNDRY_ENDPOINT=https://fountry-scheduling-agent.services.ai.azure.com/
# Auth: Entra ID via DefaultAzureCredential (run `az login` first; needs the
# "Cognitive Services OpenAI User" role on the Foundry account). Key auth is disabled.
export SCHEDULING_DECISION1_MODEL=decision-1

export WSLENV="SCHEDULING_MODEL_PROVIDER:SCHEDULING_AGENT_MODEL:SCHEDULING_AGENT_EFFORT:AZURE_FOUNDRY_ENDPOINT:SCHEDULING_DECISION1_MODEL"

exec agent/.venv/Scripts/python.exe evals/run_harness.py run \
  --agent scheduling_agent.hosts.harness:create_agent \
  --judge harness.judge_copilot:create_judge \
  --case-file evals/samples/broader-50.txt \
  --repeats 3 \
  --workers 2 \
  --calendar-delivery tools \
  --out evals/results/iter30-copilot-nopipe-d1 \
  --timeout-s 120 \
  --no-transcripts \
  "$@"
