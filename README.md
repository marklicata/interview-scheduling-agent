# Interview scheduling agent

Product strategy, requirements, and the evaluation dataset for an AI agent that helps recruiters create, book,
and repair interview schedules.

## Contents

| Path | Purpose |
|---|---|
| `docs/interview-scheduling-requirements.md` | Product strategy and requirements (v0.5). |
| `evals/interview-scheduling-test-cases.yaml` | Merged evaluation dataset: 1,185 test cases with run and grading instructions. |
| `evals/fragments/` | Source test cases by category. Edit these, then rebuild the merged file. |
| `evals/tools/validate_cases.py` | Checks schema, time zones, DST, weekdays, authority/side-effect consistency, and schedule oracles. |
| `evals/tools/merge_cases.py` | Validates the fragments and writes the merged dataset with statistics. |
| `evals/AUTHORING_GUIDE.md` | How to write and maintain test cases. |
| `evals/tz_reference.txt` | Verified UTC offsets and DST transitions used while authoring. |

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
```

The merged dataset is AI-authored and machine-checked. Recruiting operations should review expected behavior
before cases become release gates.
