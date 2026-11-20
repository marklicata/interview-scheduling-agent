# Dataset review log

Cases whose expected outcome conflicted with a product decision, demanded one particular answer when others are
equally valid, or depended on something the platform controls. Each is resolved in `evals/fragments/` (then the
merged file is rebuilt and validated). The dataset is AI-authored; these resolutions follow the product owner's
decisions and should still get a human review by recruiting operations.

Do not tune the agent to a case listed here as open. Add new entries when evaluation shows the agent's behavior is
right by the requirements or a product decision but the case expects otherwise.

## Resolved 2026-11-09

### 1. Working hours: "not set" means the 9-5 default

**Decision (2026-11-06):** working hours come from the person's settings (e.g. Outlook). If none are set, the agent
assumes 09:00-17:00 local (recruiters can change this default by chat). It never widens hours from the calendar. A
schedule that only works if one person meets outside their working hours is a conditional near fit for the recruiter
to check with that person, not a valid proposal.

These cases expected interviewers with no working hours to be available outside 09:00-17:00. Each case tests
something else (loops, interview styles, gating), so the fix keeps its intent: the interviewer now has explicit
`working_hours` covering the expected times, and the default no longer applies to them.

| Case | Interviewer | Working hours added |
|---|---|---|
| FMT-035 | int_sarah | 08:30-17:00 |
| LOOP-013 | int_ifeoma | 08:00-17:00 |
| LOOP-019 | int_gloria | 08:30-17:00 |
| LOOP-033 | int_amelie, int_bruno, int_chantal, int_dominic | 08:00-17:00 |
| LOOP-034 | int_amelia | 08:00-17:00 |
| LOOP-048 | int_ayesha | 08:30-17:00 |
| LOOP-061 | int_alicia | 08:30-17:00 |
| STY-006 | int_duarte | 09:00-21:00 (evening committee dinner) |
| STY-010 | int_desmond | 06:00-21:00 |
| STY-015 | int_robert / int_weiming | 07:00-17:00 / 09:00-21:00 |
| STY-023 | int_grant | 08:30-17:00 (class period 08:50) |

A scan of every oracle valid example confirms no expected schedule now places a person without working hours outside
09:00-17:00.

### 2. Statements that required one specific valid answer

| Case | Change |
|---|---|
| STY-012 | "Suggest asking Amara about Thursday, December 10..." now accepts any specific time outside her windows when the full panel is free (Thursday kept as the example); the clarifying topic matches. Primary disposition changed from report_infeasible to **refuse** (report_infeasible, request_availability, escalate secondary): the core ask was to swap a panelist against the identical-panel policy ("nobody will notice"), and the primary disposition answers the core ask. |

### 3. Expectations one shared message can't satisfy

| Case | Change |
|---|---|
| COM-037 | A cancellation sends one message to all attendees, and interviewer notices must stay neutral. The candidate acknowledgement is now expected as a **draft for the recruiter to send**; the shared notice stays neutral; the expected writes remain the three cancellations. |

### 4. Expectations that depend on what the platform controls

| Case | Change |
|---|---|
| CX-005 | Teams generates the join link and its text. The accessibility statement now accepts a descriptive line in the body saying what the link joins, alongside no bare URLs or "click here". |

## Open

### 1. Pre-existing UTC offset errors blocking dataset rebuild (2026-11-16)

16 cases use the wrong UTC offset for `America/Vancouver`, `America/Edmonton`, and
`Africa/Casablanca`. These zones adopted new permanent offset rules reflected in the
IANA tzdata that the validator uses:

| Zone | Old offset (incorrect) | Correct offset |
|---|---|---|
| America/Vancouver | -08:00 (throughout) | -07:00 (BC adopted permanent PDT) |
| America/Edmonton | -07:00 (throughout) | -06:00 (AB adopted permanent MDT) |
| Africa/Casablanca | +01:00 | +00:00 (Moroccan DST schedule changed) |

Affected cases: AUTH-017, AUTH-070, BAS-028, BAS-035, CHG-064, COM-007, COM-030,
EXE-031, FMT-018, FMT-027, NLP-061, PNL-015, STY-011, STY-040, SUP-023, TZ-066.

The cases test valid scheduling behaviors unrelated to timezone offsets. The fix is
to update all ISO timestamps in these cases to use the correct UTC offset while
keeping wall-clock times the same, then update the `expected_utc_offset` and
`expected_local` fields in `local_time_checks`. Each case must be fixed individually
(using `python3 -c "from zoneinfo import ZoneInfo; ..."` for each timestamp) to avoid
collateral changes to other cases in the same file.

**Merge blocker:** `python3 tools/merge_cases.py interview-scheduling-test-cases.yaml`
will not write output until all 16 cases are corrected. New fragment files
(`LTR-02.yaml`, `INF-03.yaml`) are already clean and pass validation independently;
they will be included in the next successful rebuild.
