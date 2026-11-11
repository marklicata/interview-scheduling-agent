# Interview scheduling agent

Product strategy, requirements, the evaluation dataset, and the evaluation harness for an AI agent that helps recruiters create, book, and repair interview schedules.

## Contents

| Path | Purpose |
|---|---|
| `docs/interview-scheduling-requirements.md` | Product strategy and requirements (v0.5). |
| `evals/interview-scheduling-test-cases.yaml` | Merged evaluation dataset: 1,185 test cases with run and grading instructions. |
| `evals/fragments/` | Source test cases by category. Edit these, then rebuild the merged file. |
| `evals/tools/validate_cases.py` | Checks schema, time zones, DST, weekdays, authority/side-effect consistency, and schedule oracles. |
| `evals/tools/case_oracle.py` | Case loader and scheduling oracle shared by the validator and the harness. |
| `evals/tools/merge_cases.py` | Validates the fragments and writes the merged dataset with statistics. |
| `evals/tools/draw_sample.py` | Draws a stratified, held-out sample of test cases. |
| `evals/samples/` | Case-ID lists: the held-out sample and the tuning targets. |
| `evals/AUTHORING_GUIDE.md` | How to write and maintain test cases. |
| `evals/DATASET_REVIEW.md` | Log of test cases changed after review, and why. |
| `evals/tz_reference.txt` | Verified UTC offsets and DST transitions used while authoring. |
| `evals/harness/` | Evaluation harness: agent contract, sandboxed calendar/mail provider, graders, reports. See `evals/harness/README.md`. |
| `evals/run_harness.py` | Harness command line. |
| `evals/rejudge.py` | Re-grades a finished run with a different judge and compares the two judges' verdicts. |
| `evals/tests/` | Harness unit tests. |
| `evals/reports/` | Shared evaluation reports (baseline and self-check). |
| `agent/` | The agent: host-agnostic core, harness entry point, and Microsoft 365 Copilot/Teams host. See `agent/README.md`. |

## Setup

Requires Python 3.9 or later.

```powershell
python -m pip install -r requirements.txt
```

## Common commands

Run from the repository root:

```powershell
python evals/tools/validate_cases.py evals/fragments/*.yaml
python evals/tools/merge_cases.py evals/interview-scheduling-test-cases.yaml
python evals/tools/validate_cases.py evals/interview-scheduling-test-cases.yaml

python evals/run_harness.py run --agent null                         # no-agent baseline (fails the gate)
python -m pip install -e agent                                       # install the agent
python evals/run_harness.py run --agent scheduling_agent.hosts.harness:create_agent  # evaluate it
python evals/run_harness.py self-check                               # verify the harness itself
python -m unittest discover -s evals/tests                           # harness unit tests
```

The merged dataset is AI-authored and machine-checked. Recruiting operations should review expected behavior
before cases become release gates. Evaluation data and results must never be used to train or fine-tune models.
