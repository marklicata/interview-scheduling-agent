#!/usr/bin/env bash
# Launch the eval harness with CopilotModel (GitHub Copilot SDK, claude-opus-5.5).
# This is the standard eval script. Use run_foundry.sh for Azure AI Foundry model runs.
set -e
cd "$(dirname "$0")/.."

export SCHEDULING_MODEL_PROVIDER=copilot
export SCHEDULING_AGENT_MODEL=claude-opus-5.5
export SCHEDULING_AGENT_EFFORT=high

# Bridge variables to the Windows Python process via WSLENV
# SCHEDULING_AGENT_TIMEOUT_S overrides the 900s default; set to 120 to surface Copilot
# rate-limit failures quickly rather than waiting 15 minutes per stuck worker.
export SCHEDULING_AGENT_TIMEOUT_S="${SCHEDULING_AGENT_TIMEOUT_S:-900}"
export WSLENV="SCHEDULING_MODEL_PROVIDER:SCHEDULING_AGENT_MODEL:SCHEDULING_AGENT_EFFORT:SCHEDULING_AGENT_TIMEOUT_S"

exec agent/.venv/Scripts/python.exe evals/run_harness.py run \
  --agent scheduling_agent.hosts.harness:create_agent \
  --judge harness.judge_copilot:create_judge \
  --case-file evals/samples/holdout-280.txt \
  --repeats 3 \
  --workers 5 \
  --timeout-s 180 \
  --calendar-delivery tools \
  --baseline evals/results/iter21-nearfit-check/results.json \
  --out evals/results/iter25-copilot \
  --no-transcripts \
  "$@"
