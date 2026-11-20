#!/usr/bin/env bash
# Launch the eval harness with Azure AI Foundry as the model provider.
# Sets WSLENV so env vars are visible to the Windows Python executable.
set -e
cd "$(dirname "$0")/.."

export SCHEDULING_MODEL_PROVIDER=foundry
export AZURE_FOUNDRY_ENDPOINT=https://fountry-scheduling-agent.cognitiveservices.azure.com/
export AZURE_FOUNDRY_API_VERSION=2025-03-01-preview
export SCHEDULING_AGENT_MODEL=gpt-6-astra
export SCHEDULING_SUBAGENT_MODEL=gpt-6-astra

# Auth: Entra ID via DefaultAzureCredential (run `az login` first; needs the
# "Cognitive Services OpenAI User" role on the Foundry account). Key auth is disabled.
export SCHEDULING_DECISION1_MODEL=decision-1

# Bridge all variables to the Windows process via WSLENV
export WSLENV="SCHEDULING_MODEL_PROVIDER:AZURE_FOUNDRY_ENDPOINT:AZURE_FOUNDRY_API_VERSION:SCHEDULING_AGENT_MODEL:SCHEDULING_SUBAGENT_MODEL:SCHEDULING_DECISION1_MODEL"

exec agent/.venv/Scripts/python.exe evals/run_harness.py run \
  --agent scheduling_agent.hosts.harness:create_agent \
  --judge harness.judge_copilot:create_judge \
  --case-file evals/samples/broader-50.txt \
  --repeats 3 \
  --workers 2 \
  --calendar-delivery tools \
  --out evals/results/iter28-decision1 \
  --no-transcripts \
  "$@"
