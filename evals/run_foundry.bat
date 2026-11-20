@echo off
REM Launch the eval harness with Azure AI Foundry as the model provider.
REM
REM Required:  az login  (DefaultAzureCredential; key auth is disabled on the account)
REM
REM Usage:
REM   cd C:\Users\malicata\source\interview-scheduling-agent
REM   evals\run_foundry.bat [--out evals\results\iter23-azure-foundry]

SET SCHEDULING_MODEL_PROVIDER=foundry
SET AZURE_FOUNDRY_ENDPOINT=https://fountry-scheduling-agent.cognitiveservices.azure.com/
SET AZURE_FOUNDRY_API_VERSION=2025-03-01-preview
SET SCHEDULING_AGENT_MODEL=gpt-6.1-sol
SET SCHEDULING_SUBAGENT_MODEL=gpt-6-astra

agent\.venv\Scripts\python.exe evals\run_harness.py run ^
  --agent scheduling_agent.hosts.harness:create_agent ^
  --judge harness.judge_copilot:create_judge ^
  --case-file evals\samples\holdout-280.txt ^
  --repeats 3 ^
  --workers 10 ^
  --calendar-delivery tools ^
  --baseline evals\results\iter21-nearfit-check\results.json ^
  --out evals\results\iter23-azure-foundry ^
  --no-transcripts %*
