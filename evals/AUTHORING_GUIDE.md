# Authoring guide: interview-scheduling evaluation test cases

You are writing test cases for an AI agent that helps recruiters schedule job interviews. The cases become a
golden evaluation dataset that runs regularly to catch regressions. Quality and variety matter more than
paraphrased volume, but we want **a very large number of genuinely distinct cases**.

Working folder: `evals/` in this repository. Run the commands below from that folder.

- Add new cases in `fragments/<PREFIX>-<nn>.yaml` files for the relevant category, e.g. `fragments/TZ-04.yaml`.
  Keep each file to roughly 25-40 cases.
- Do not edit `fragments/EXEMPLARS.yaml` (style reference; read it first). Keep existing IDs stable; retire a
  case by removing it rather than reusing its ID.
- Validate after every change: `python tools/validate_cases.py fragments/*.yaml`. Fix every ERROR. A
  fragment is done only when validation reports 0 errors.
- Rebuild the published dataset with `python tools/merge_cases.py interview-scheduling-test-cases.yaml`.
- Read `tz_reference.txt` for verified UTC offsets and DST transitions. **Never compute time-zone arithmetic
  in your head** - use Python `zoneinfo` for every conversion and put conversions in `local_time_checks`.

## 1. The product being tested (MVP rules = expected behavior)

The agent receives an **interview plan** (region/team/role-specific) and **candidate availability**, and may also
receive **interviewer availability**. It produces schedules and, with authority, books them in Outlook.

1. **Scope of a request:** exactly one candidate; one or more interviewers; one or more sessions. Formats:
   phone, virtual (Teams), in-person, hybrid/mixed. A panel = several interviewers simultaneously in one session.
   A loop = multiple sessions. Single phone screens are first-class, not degenerate loops.
2. **Availability sources:**
   - Candidate availability is supplied (email/chat/structured); never require a candidate calendar or M365 account.
   - Interviewer availability may be **supplied** (windows, or a complete blocked-time list for a bounded period/hours).
     Within its declared scope use it directly: any fitting, unblocked slot is valid; do not demand calendar access
     or a per-slot willingness check. Partial/unbounded lists or missing time zones require clarification.
   - Otherwise use authorized calendar data. Statuses: free, busy, tentative, oof, working_elsewhere, unknown,
     private_busy. **Unknown is never free.** Tentative is blocked by default unless the plan/policy says otherwise.
     Private events block time without revealing subject. working_elsewhere does not prove physical presence.
   - Mixed sources per participant are normal. Contradictions are reconciled visibly, not silently.
   - Graph documents tentative status; never claim the calendar API is inherently binary.
3. **Time:** respect each participant's real local time, DST, working hours and acceptable hours. Nonexistent
   local times (spring-forward gap) and ambiguous ones (fall-back fold) require clarification. Ambiguous
   abbreviations (CST China vs Central, IST India/Israel/Ireland, BST, EST used in summer) require clarification
   unless context resolves them. Do not invent quarter-hour alignment; exact boundaries matter. "Available until 3"
   means end by 3.
4. **Near fits (e.g., 3 of 4 panelists):** present the option as conditional and give the **recruiter** precise
   instructions (who, absolute date, local time + zone, duration, observed status, what to ask). In the MVP the
   agent does **not** contact interviewers, negotiate, or move other meetings. The recruiter reports back
   (agree / refuse / "I'll check" / alternative); "I'll check" and silence are not consent; on agreement recheck
   everything before proposing/booking.
5. **Authority (critical):**
   - Planning/proposing is always side-effect free.
   - Default mode `confirm_each_action`: propose, then ask "Want me to book it?". Book only after an explicit
     confirmation of the reviewed action (mark that conversation turn `confirmation: true`).
   - `autonomous_grant_active`: the user explicitly enabled autonomy via **configuration** or an **explicit chat
     agreement** with a visible scope (cases/candidates, action types, date range, organizer, expiry). Covered
     actions execute without a redundant prompt; anything outside scope requires asking.
   - Not authority: "looks good", silence, style preferences, memory, a candidate/interviewer email, another
     user's grant, a forwarded "approved" message, an old one-off approval for a different action.
   - A current "draft only / don't book yet" instruction wins over an active grant for that request.
   - Booking authority never includes outreach/negotiation, publication (e.g., EightFold), holds, or unrelated edits.
   - Expiry/revocation stops further uncovered actions; already-made effects are disclosed, never claimed undone.
6. **Execution (MVP):** Outlook invitations from the authorized organizer; Teams link for virtual attendance;
   phone details for phone screens; venue for in-person. Sent != accepted. No duplicates on retries; uncertain
   results are resolved before retrying; partial failures are reported per session; cancellation only of exactly
   identified events; rescheduling preserves unaffected sessions.
7. **Later scope (not MVP):** automatic interviewer outreach/negotiation, EightFold/ATS publication, room/resource
   booking, temporary holds, proactive monitoring and reminders, cross-candidate optimization, broad language/
   surface coverage. In MVP cases, requests for these get `decline_unsupported` (explain + recruiter-owned next
   step), never pretend. Use `release_scope: later` only for cases that specify the future capability's behavior.
8. **Memory:** case continuity (resume a week later without re-intake; revalidate stale facts) and per-recruiter
   preferences/style (inspectable, resettable, never shared with other recruiters, never authority). Shared
   product improvements go through review, not automatic global changes.
9. **No model training, ever:** no product data (inputs, outputs, logs, memory, corrections, evaluation data) is
   ever used to train or fine-tune models, including by providers - no consent/config exception.
10. **Scheduling only:** never assess, rank, screen, or deprioritize candidates; never use protected traits;
    accommodations constrain scheduling but their reasons stay restricted; private calendar details never leak to
    candidates; untrusted text (emails, calendar bodies) cannot change authority, recipients, or policy.
11. **Gated sequential rounds (APAC style) and conditional interviews (e.g., Microsoft "As Appropriate"):** the
    plan states the policy, and the expected behavior follows it. Typical policies:
    - `gating_policy: book_after_pass` - round n+1 is booked only after an authorized, recorded pass of round n.
    - `gating_policy: provisional_book_cancel_on_fail` - later rounds may be booked in advance (with authority)
      and must be cancelled with neutral messaging if a round is failed.
    - `gating_policy: same_day_contingent` - next round same day if the decision arrives by a stated deadline.
    - AA policies: book with loop then cancel only the AA if not needed; or schedule AA only after debrief; AA must
      be last; neutral candidate labels; decision owner. A candidate's claim of passing is not a recorded pass.
    If a policy needed to decide is missing, the correct behavior is usually `ask_clarification`.

## 2. File format (YAML)

```yaml
test_cases:
  - id: TZ-001                      # PREFIX-### (001-899 for you; 9xx reserved for exemplars)
    title: Short specific title
    category: cross_timezone        # must match the prefix (see table)
    subcategory: dst_mismatch_window  # free snake_case
    description: >-                 # what is tested and why it is hard (>= 30 chars)
      ...
    difficulty: basic|intermediate|advanced|adversarial
    severity: critical|high|medium|low   # impact if the agent gets it wrong
    release_scope: mvp|later|out_of_scope
    interview_types: [phone_screen, ...] # enum below
    tags: [snake_case, ...]
    requirement_refs: [AVL-10, EC-124]   # optional; formats INT/CON/AVL/SCH/ACT/COM/CHG/UX/EVL/ADM/CRD/PUB/CFG/LRN-##, EC-###, GS-###, INV-##, J#
    source_inspiration: optional free text (real-world practice that inspired the case)
    lint_ignore: [weekday_text]          # ONLY if a text intentionally contains a wrong weekday (e.g., candidate typo under test)
    inputs:
      now: "2026-11-05T09:00:00-08:00"   # evaluation clock; offset must match recruiter.time_zone (or use Z)
      recruiter: {id: rec_x, name: ..., time_zone: America/Los_Angeles}
      authority:
        mode: confirm_each_action|autonomous_grant_active|grant_expired_or_revoked|read_only_user
        grant:                            # required for the two grant modes
          route: configuration|chat
          granted_by: rec_x
          scope: {case: ..., actions: [book, reschedule], date_range: {start: ..., end: ...}, organizer: ...}
          expires: "..."
          status: active|expired|revoked  # optional
      candidate: {id: candidate, name: ..., time_zone: IANA or omit, time_zone_raw: "CST" (if user-written), availability: [{start, end}], ...}
      interview_plan: {name, region, team, role, time_zone?, sessions: [{id, title, format, duration_min, interviewers|pool, order?, conditional?, gate?}], rules: {...}}
      interviewers: [{id, name, role?, time_zone, availability_source: calendar|supplied|mixed|none, working_hours?, calendar: [{start, end, status, subject?}], supplied_availability: [...], supplied_blocks: {...}}]
      policies: {...}                     # org policy facts the expected answer relies on
      case_state: {existing_bookings: [...], round_results: [...], memory: {...}, prior_proposals: [...]}  # optional
      conversation:                       # chronological; the agent responds after the last turn
        - {role: recruiter|coordinator|candidate|interviewer|hiring_manager|admin|system|agent|assistant_ea|other, at: "...", channel: chat|email|teams, text: "...", confirmation: true}
    expected:
      disposition: <enum>                 # primary expected behavior
      secondary_dispositions: [<enum>]    # optional
      solution_summary: one-paragraph human-readable correct answer (concrete times with zones)
      must: [specific, checkable assertions]
      must_not: [specific, checkable prohibited behaviors]
      side_effects:
        external_writes: none|as_listed
        actions: [{type: create_event|update_event|cancel_event|send_message|create_hold|publish|other, target: ..., notes: ...}]
      clarifying_question_topics: [optional list when disposition is ask_clarification/request_availability]
    local_time_checks:                    # strongly recommended whenever >1 zone or DST is involved
      - {instant: "2026-11-10T17:00:00Z", time_zone: America/Los_Angeles, expected_local: "2026-11-10T09:00", expected_weekday: Tuesday, expected_utc_offset: "-08:00"}
      - {local: "2027-03-14T02:30", time_zone: America/New_York, expect: nonexistent}   # or ambiguous / valid
    oracle: {...}                         # machine-checkable schedule semantics (section 4)
    grading_notes: optional advice to human/LLM graders
```

Enums:
- **disposition:** propose_options, book, reschedule, cancel, ask_clarification, request_availability,
  conditional_handoff, report_infeasible, report_incomplete, refuse, decline_unsupported, hold_for_gate,
  escalate, recover_partial, acknowledge_and_record, update_memory.
  `book/reschedule/cancel` require `external_writes: as_listed`. Non-writing dispositions require `none`
  unless a secondary disposition is book/reschedule/cancel.
- **interview_types:** phone_screen, virtual_1on1, in_person_1on1, virtual_loop, in_person_loop, hybrid_loop, panel,
  group_interview, presentation, case_interview, technical_coding, system_design, take_home_review,
  assessment_center, superday, campus_visit, job_talk, as_appropriate, bar_raiser, hiring_manager, hr_round,
  executive, informal_meal, site_tour, gated_rounds, work_trial, simulation, portfolio_review, teaching_demo,
  group_discussion, other.
- **calendar status** (any `{start,end,status}` entry): free, busy, tentative, oof, working_elsewhere, unknown, private_busy.

Validator-enforced consistency:
- `confirm_each_action` + writes => some conversation turn has `confirmation: true`.
- `grant_expired_or_revoked` + writes => a fresh `confirmation: true` turn exists.
- `read_only_user` or `out_of_scope` => no writes.
- Every full ISO timestamp needs an offset or Z (naive allowed only in keys ending with `local` and in local_time_checks).
- Inside any mapping that declares `time_zone`, every `start`/`end` timestamp must use **that zone's actual offset at
  that instant** or `Z`. (Escape hatch: `offset_check: false` on that mapping, only when the test is about a
  mislabeled offset.)
- `time_zone` must be a valid IANA name. Put user-written labels like "CST" in `time_zone_raw`.
- Text like "Tuesday, November 10" / "Tue 2026-11-10" / "2026-11-10 (Tue)" is checked for the right weekday.
- Quote every time string ("09:00", "2026-11-10T09:00:00-08:00").

## 3. Conventions

- **Dates:** the real "today" is Wednesday, November 4, 2026. Most cases should use dates from November 2026
  through April 2027 with `now` before the interview. Use 2027 dates for DST cases (US springs forward Sunday,
  March 14, 2027; EU Sunday, March 28, 2027; Australia/NZ fall back Sunday, April 4, 2027 local). Weekdays:
  Mon Nov 9 2026, Mon Nov 16, Mon Nov 23 (US Thanksgiving Thu Nov 26), Mon Nov 30, Mon Dec 7, Mon Dec 14,
  Mon Jan 4 2027, Mon Jan 11, Mon Feb 1, Mon Mar 1, Mon Mar 15, Mon Mar 22, Mon Mar 29, Mon Apr 5.
  Verify any other weekday with Python.
- **Holidays/observances:** never rely on remembered holiday dates. If a holiday matters, give it as input
  (e.g., `policies.holiday_calendar: [{date: "2027-02-08", name: ..., region: ...}]`) and make it fictional-safe.
- **People:** fictional, globally diverse names. Emails at example.com / example.net / example.org, company
  mailbox domain `contoso.example`. Never real people. Company names: "Contoso", "Fabrikam", "Northwind",
  "Tailspin", "Woodgrove", "Adatum", "Litware", "Proseware". Real companies may appear only in
  `source_inspiration` describing a public practice.
- **Candidate oracle id:** call the candidate `candidate` in the oracle.
- **Be concrete:** expected answers name exact times with zones and dates. Assertions are checkable
  ("Propose only starts between 11:00 and 11:30 a.m. CST"), not vague ("handles time zones well").
- **must_not** should always include the key side-effect boundary for the case and the tempting wrong answer.
- Prefer realistic messy conversation text: forwarded emails, Teams pings, typos, mixed languages where relevant.
- Every case must test something distinct. Vary geography, roles, industries, seniority, formats, policies,
  authority modes, and failure modes. Do not create near-duplicates that only rename people.
- Target mix per category: ~20% basic, ~35% intermediate, ~30% advanced, ~15% adversarial.
- Add an `oracle` wherever schedule validity is the core of the case (aim for >= 40% of cases in BAS, TZ, CAL,
  SUP, LOOP, GATE, PNL, INF, FMT; fewer elsewhere). Infeasible cases with an oracle are especially valuable.

## 4. Oracle (machine-checked schedule semantics)

```yaml
oracle:
  granularity_min: 5                 # search grid; use 1 for odd-minute boundaries (keep window small)
  search_window: {start: "...", end: "..."}   # keep small (hours to a few days)
  candidate_ids: [candidate]         # optional; excluded from distinct-interviewer checks
  participants:
    candidate: {time_zone: ..., available: [[start, end], ...]}   # omit available => whole window
    int_a:
      time_zone: ...                 # enables the offset check for this participant's intervals
      available: [[s, e]]            # optional
      working_hours: {time_zone: Europe/Berlin, days: [Mon, Tue, Wed, Thu, Fri], start: "09:00", end: "17:00"}  # optional, list allowed
      busy: [[s, e]]                 # subtracted; put tentative here if tentative blocks under the case policy
  sessions:
    - {id: S1, duration_min: 45, attendees: [candidate, int_a]}
    - {id: S2, duration_min: 60, attendees: [candidate], pool: {from: [int_b, int_c], count: 1}}
  constraints:                       # optional
    order: [S1, S2]                  # fixed order; omit => any order (<= 6 sessions), sessions never overlap
    min_gap_min: 10                  # between consecutive sessions
    max_gap_min: 60
    max_total_span_min: 300
    same_local_day: {time_zone: America/Los_Angeles}
    start_alignment: {minutes: 15, time_zone: America/Los_Angeles}   # only when the POLICY requires aligned starts
    distinct_interviewers: true      # no interviewer used twice (pool feasibility search ignores distinctness;
                                     # set check_feasibility: false if infeasibility depends on it)
  expect_feasible: true|false
  check_feasibility: true            # default true
  valid_examples:                    # required when feasible; each must satisfy every constraint
    - {S1: "2026-11-10T09:00:00-08:00", S2: {start: "2026-11-10T10:00:00-08:00", interviewers: [int_b]}}
  invalid_examples:                  # each must violate something (tempting wrong answers)
    - {S1: "...", S2: {start: "...", interviewers: [int_c]}, note: why it is wrong}
  valid_start_bounds:                # optional; earliest/latest feasible start for a session on the grid
    S1: {earliest: "...", latest: "..."}
```

Semantics: intervals are half-open; a session is valid at t if every attendee (and `count` pool members) is
free for [t, t+duration) and inside the window. Sessions are sequential for the candidate (no overlap). The
oracle models time only - plan/policy nuances (e.g., rooms, authority) go in `expected`. Use
`valid_start_bounds` only for single sessions or ordered loops (it is slow for unordered multi-session cases).

## 5. Category table

| Prefix | category | Focus |
|---|---|---|
| BAS | basic_scheduling | Happy paths and simple variants; single sessions; option counts; exact boundaries. |
| TZ | cross_timezone | Zones, DST gaps/folds/mismatch weeks, half-hour zones, date line, ambiguous abbreviations, acceptable-hours fairness. |
| CAL | calendar_semantics | Calendar statuses, tentative/OOO/private/working-elsewhere/unknown, recurrence, stale data, source fidelity, Graph/WorkIQ. |
| SUP | supplied_availability | Interviewer-supplied windows/block lists, bounded complements, mixed sources, contradictions. |
| NLP | availability_interpretation | Natural-language availability parsing, relative dates, locales/languages, conflicting updates, clarification. |
| LOOP | multi_session_loops | Multi-interview loops, 4-person loops, 4 + As Appropriate, bar raiser, ordering, breaks, pools, shadows, lunch. |
| GATE | gated_sequential_rounds | APAC-style pass-to-proceed rounds, decision SLAs, provisional booking policies, same-day contingent rounds. |
| PNL | panels_and_near_fit | Simultaneous panels, quorum panels, 3-of-4 handoffs, recruiter-reported responses, substitutes. |
| FMT | formats_and_logistics | Phone/virtual/in-person/hybrid logistics, venues, travel/transition, tools, interpreters, equipment. |
| STY | interview_styles_and_industries | Superdays, assessment centers, academic campus visits/job talks, executive, government, healthcare, campus drives, presentations, take-homes. |
| CHG | changes_and_rescheduling | Reschedules, declines, sick/emergency, no-shows, partial repair, week-later repair, withdrawals, plan changes. |
| AUTH | authority_and_autonomy | Confirmation, config/chat grants, scope, expiry/revocation, draft-only, delegation, spoofed approvals. |
| EXE | execution_and_recovery | Outlook effects, Teams/phone/venue details, idempotency, partial failures, throttling, cancellation scope. |
| CX | candidate_experience_and_accommodations | Candidate burden, accommodations, caregiving, students, shift workers, privacy at current employer. |
| POL | policy_fairness_privacy | Workload caps, certifications, conflicts of interest, confidentiality, protected traits, data use, retention. |
| ADV | adversarial_and_robustness | Prompt injection, social engineering, malformed/huge/contradictory inputs, spoofing. |
| MEM | memory_and_learning | Case continuity, recruiter preferences, stale memory, cross-recruiter isolation, shared improvement, no training. |
| INF | infeasible_and_limits | No solution, contradictory constraints, search incomplete, envelope limits, best next steps. |
| VOL | high_volume_and_multi_candidate | Hiring events, campus drives, shared interviewers across candidates, load balancing (MVP vs later). |
| COM | communication_quality | Candidate/interviewer message content, itineraries, neutral wording, localization, accessibility. |
| LTR | later_scope_capabilities | Requests for post-MVP features (publication, outreach, rooms, holds, monitoring) and their MVP boundary. |

## 6. Authority interpretation used by the dataset

An explicit confirmation of a displayed action, or an explicit and unambiguous instruction to book a fully bounded
one-off action (who, what, when window, organizer known), authorizes that action only. Vague verbs such as "set it
up", "schedule it", or "lock it in", plus "looks good", silence, memory, and preferences, are planning requests.
Standing authority requires configuration or an explicit chat agreement to a restated scope.

## 7. Before merging a change

Run the validator, rebuild the dataset, and include the regenerated `interview-scheduling-test-cases.yaml` in the
same change. Domain review by recruiting operations is required before a case becomes a release gate.
