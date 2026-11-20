Auto Interview Scheduling: Use Cases, Business Rules and Edge Cases 

Global Talent Acquisition | Candidate Assessment | 10 November 2026 

This document describes the use cases, business rules and edge cases for auto scheduling of interviews: what the automation does, what candidates, schedulers and interviewers do when they interact with it, and what should happen when things do not go to plan. It is written independently of any system, so the same use cases, rules and edge cases apply whichever tools carry out the work. 

1. Actors 

Actor 

What they do in scheduling 

Candidate 

Shares availability, receives and confirms the interview schedule, asks for changes or accommodations 

Scheduler (scheduling coordinator) 

Reviews and approves what auto scheduling proposes, and takes over the cases it cannot resolve 

Recruiter 

Agrees the interview plan with the hiring manager that auto scheduling works from 

Hiring manager 

Defines the interview plan (who interviews, in what order, on which dates) and interviews candidates 

Interviewer 

Chooses which of the candidate's available times they can actually make, accepts invites and conducts the interview 

Panel member 

One of several interviewers who meet the candidate together in a single session 

Backup, delegate or proxy 

Steps in when an interviewer cannot attend, or acts on behalf of a hiring team member 

Scheduling automation 

Collects availability, proposes schedules, sends reminders and raises exceptions to the scheduler; never the final decision maker 

2. Background and problem 

Today the scheduler calls or emails each candidate to collect available time. Candidates reply in different ways: a few full days, half days or a few hours here and there. The scheduler then compares that availability with each interviewer's calendar, finds common slots, books them, sends invites to the interviewers and, once they accept, sends the confirmed schedule to the candidate. If a candidate or interviewer asks to reschedule, or someone does not turn up, the whole cycle starts again. 

This creates several problems: 

Candidates have a poor experience because of back-and-forth on availability, and their interview information is scattered across emails. 

It takes nearly six days from the hiring manager asking for interviews to the scheduler starting to set them up. 

Schedulers spend most of their effort on manual communication across email, chat and phone. 

Interviewer calendars are often wrong: a time that shows as free is frequently not truly available, so schedules built from calendars alone lead to declines and rework. 

Delays push out hiring decisions and increase candidate drop-off. 

Scheduling at scale needs a large team; nearly USD 30 million a year is spent on scheduling. 

Candidate feedback says the same thing. Candidates asked for one place to share their preferred interview dates and to see their interview schedule, interviewers and meeting links, instead of searching through email. 

Hiring events are one of the biggest pain points. They follow the same pattern as a loop, but at scale: many candidates must be matched to a shared group of interviewers in a short window, and any late change ripples across many schedules at once. 

3. What gets scheduled 

Each type of interview puts different demands on scheduling. The use cases in section 4 refer back to these types. 

Interview type 

What it is 

What scheduling has to handle 

Single interview 

One interviewer, one session 

Matching one interviewer's calendar with the candidate's availability 

Interview loop 

A series of sessions with different interviewers, in a set order, usually on the same day 

Order of interviews, duration of each, breaks between them, every interviewer free at their own slot, all inside the candidate's window 

Panel interview 

Several interviewers meet the candidate together in one session 

Every panel member free at the same time, plus the candidate 

Finals day or onsite 

A full day of interviews, often in person 

Location, room, travel lead time, a longer block of the candidate's time 

Hiring event 

Many candidates interviewed on the same day or days by a shared pool of interviewers, each candidate going through their own loop 

Matching many candidates to one interviewer pool without clashes, interviewer skills per role, balanced interviewer load, rooms or virtual rooms, capacity and waitlists, same-day changes 

Interviews with special handling 

Interviews with a language requirement (for example French for Quebec candidates), NDA wording (for example US), country-specific rules (for example Japan or Russia), or federal recruiting rules 

Right template and wording, right interviewers, compliance checks 

Out of scope for auto scheduling: screens of any kind (recruiter, hiring manager and online technical screens), executive hiring and pre-apply sourcing conversations. Agreeing the interview plan at intake and organising hiring events are inputs to auto scheduling rather than part of it. 

4. Use cases 

ID 

Use case 

Primary actor 

UC-01 

Request availability from the candidate 

Scheduler or scheduling automation 

UC-02 

Candidate provides availability 

Candidate 

UC-03 

Candidate updates availability 

Candidate 

UC-04 

Candidate has no availability in the window 

Candidate 

UC-05 

Follow up on missing availability 

Scheduling automation 

UC-06 

Interviewer confirms availability 

Interviewer, scheduling automation 

UC-07 

Schedule a single interview 

Scheduling automation, scheduler 

UC-08 

Schedule an interview loop 

Scheduling automation, scheduler 

UC-09 

Schedule a panel interview 

Scheduling automation, scheduler 

UC-10 

Schedule a finals day or onsite 

Scheduler, scheduling automation 

UC-11 

Let the candidate pick from offered slots 

Candidate 

UC-12 

Review and approve a proposed schedule 

Scheduler 

UC-13 

Invite interviewers 

Scheduling automation 

UC-14 

Confirm the schedule with the candidate 

Scheduling automation 

UC-15 

Candidate views interview details and confirms attendance 

Candidate 

UC-16 

Candidate asks to reschedule 

Candidate 

UC-17 

Interviewer declines or becomes unavailable 

Interviewer 

UC-18 

Handle a no-show 

Scheduling automation 

UC-19 

Stop scheduling when the candidate withdraws or is rejected 

Scheduling automation 

UC-20 

Handle an accommodation request 

Candidate, scheduler 

UC-21 

Escalate what automation cannot resolve 

Scheduling automation 

UC-22 

Auto-schedule a hiring event 

Scheduling automation, scheduler 

UC-23 

Reschedule within a hiring event 

Scheduling automation 

4.1 Collect availability 

UC-01 Request availability from the candidate 

Primary actor: scheduler or scheduling automation. Trigger: the candidate reaches the interview stage. 

The candidate is sent a request to share availability, using a standard template. 

The request carries context: the role, which round, the type and length of interviews and any constraints from the hiring team. 

The request gives the candidate a link to the place where they enter their availability. 

If availability was already provided, a new request can be sent asking the candidate to update it. 

Outcome: the candidate knows what they are being asked for and has a deadline. 

UC-02 Candidate provides availability 

Primary actor: candidate. Trigger: the candidate opens the availability request. 

The candidate sees the context of the request (role, round and what is expected). 

The candidate confirms their time zone. 

The candidate selects the days and times they are free within the allowed window, meeting the minimum asked for. 

The candidate adds a note if needed, for example a preference or a constraint. 

The candidate submits, and the scheduler is notified. 

Outcome: the scheduler has the candidate's availability, time zone and notes in one place. 

UC-03 Candidate updates availability 

Primary actor: candidate. Trigger: the candidate's plans change, or the scheduler asks for an update. 

The candidate opens their availability and adds, changes or removes days. 

The candidate submits the change. 

The previous version is kept, and the scheduler is notified that availability changed. 

If interviews were already booked in removed time, the scheduler is alerted to a conflict. 

Outcome: the scheduler always works from the latest availability, with history available. 

UC-04 Candidate has no availability in the window 

Primary actor: candidate. Trigger: the candidate cannot meet the window or the minimum. 

The candidate states that no suitable time is available, and can add a note explaining why. 

The scheduler is notified with a clear "no availability provided" status. 

The scheduler contacts the candidate to agree a way forward, such as a later window. 

Outcome: the case moves to manual handling without the candidate being stuck. 

UC-05 Follow up on missing availability 

Primary actor: scheduling automation. Trigger: no response to the availability request. 

If the candidate has not responded within the reminder period, a reminder is sent. 

If the candidate still does not respond, the scheduler is alerted to follow up personally. 

Outcome: requests do not sit unanswered without anyone noticing. 

UC-06 Interviewer confirms availability 

Primary actors: interviewer and scheduling automation. Trigger: the candidate has provided availability. 

Automation uses interviewer calendars only as a first filter, to leave out times that are clearly blocked. 

Each interviewer in the plan is asked to choose, from the candidate's available times, the slots they can actually make. 

The interviewer can add a note, for example a preferred time or a constraint. 

If an interviewer does not respond within the agreed time, a reminder is sent; if there is still no response, an approved backup is asked, and then the scheduler is alerted. 

The schedule is built only from slots that both the candidate and the interviewers have confirmed. 

Outcome: interviewer availability is confirmed by the interviewers themselves, so schedules are far less likely to be declined or changed later. 

4.2 Build the schedule 

UC-07 Schedule a single interview 

Primary actors: scheduling automation and scheduler. Trigger: one interview is required, and the candidate and interviewer have confirmed availability. 

Automation picks a slot that both the candidate and the interviewer have confirmed. 

The slot is booked, with the meeting link or location, once the scheduler approves it (UC-12). 

Invites and confirmation are sent as in UC-13 and UC-14. 

Outcome: the interview is booked and both sides have the details. 

UC-08 Schedule an interview loop 

Primary actors: scheduling automation and scheduler. Trigger: a loop is required, and the candidate and interviewers have confirmed availability. 

Automation takes the interview plan: the list of interviewers, the number of sessions, their order, their lengths and the breaks. 

It looks for a day inside the candidate's availability where each interviewer has confirmed a slot, in the right order, with the breaks in between. 

If one day cannot hold the loop, it proposes a split across days, if the plan allows it. 

If no workable schedule exists, automation first asks the fewest interviewers possible to adjust their slots; if that fails, the scheduler is told why and takes over. 

The proposed loop goes to the scheduler for review (UC-12). 

Outcome: a complete, conflict-free loop ready for review. 

UC-09 Schedule a panel interview 

Primary actors: scheduler and scheduling automation. Trigger: a panel interview is required. 

Automation finds a slot inside the candidate's availability that every panel member has confirmed. 

If no such slot exists, it checks whether an approved backup can replace an unavailable panel member. 

If there is still no slot, the scheduler is alerted with the conflicts. 

The proposed panel slot goes to the scheduler for review. 

Outcome: one session with the whole panel and the candidate booked together. 

UC-10 Schedule a finals day or onsite 

Primary actors: scheduler and scheduling automation. Trigger: the candidate is invited to a finals day or onsite. 

The scheduler confirms the candidate can give a full day, and whether travel is needed. 

The scheduler books the location and room, and allows enough lead time for travel. 

Automation builds the day like a loop (UC-08), including breaks and a lunch break where needed. 

The candidate receives the full-day agenda, location and travel details. 

Outcome: a full-day schedule the candidate can plan around. 

UC-11 Let the candidate pick from offered slots 

Primary actor: candidate. Trigger: the scheduler chooses to offer specific times instead of asking for open availability. 

Automation offers a set of slots that the interviewers have already confirmed. 

The candidate picks the slot that suits them. 

The slot is held for the candidate and confirmed. 

Outcome: faster booking, with no matching step for simple interviews. 

UC-12 Review and approve a proposed schedule 

Primary actor: scheduler. Trigger: automation has proposed a schedule. 

The scheduler sees the proposed schedule in the candidate's time zone, with how it was built (which availability and which interviewer slots were used). 

The scheduler can change the order, swap an interviewer, move a session or adjust breaks. 

The scheduler approves, and invites are sent. 

Outcome: a person always signs off before anything is sent. 

4.3 Communicate 

UC-13 Invite interviewers 

Primary actor: scheduling automation. Trigger: the schedule is approved. 

Each interviewer receives an invite for their session, with the meeting link or location. 

Invites follow compliance rules, for example no candidate name in the invite. 

Acceptances are tracked, and the scheduler is alerted to any decline (UC-17). 

Outcome: all interviewers have accepted their sessions. 

UC-14 Confirm the schedule with the candidate 

Primary actor: scheduling automation. Trigger: interviewers have accepted. 

The confirmed schedule is sent to the candidate by email, using a template. 

The scheduler chooses which details to share with the candidate. 

The same details are available to the candidate in one place, alongside their other application tasks. 

Outcome: the candidate has a single, up-to-date view of their interviews. 

UC-15 Candidate views interview details and confirms attendance 

Primary actor: candidate. Trigger: the candidate receives the confirmed schedule. 

The candidate sees the date, time zone, start and end time of each session, breaks, location or meeting link, and the interview type and description. 

The candidate confirms attendance. 

If anything changes later, the candidate sees the latest details and is told about the change. 

Outcome: confirmed attendance, with fewer missed or late interviews. 

4.4 Handle changes 

UC-16 Candidate asks to reschedule 

Primary actor: candidate. Trigger: the candidate can no longer make a booked session. 

The candidate asks for a change and gives new availability. 

Automation reschedules only the affected sessions where possible; the rest of the loop stays as booked. 

Interviewers receive updated invites, and the candidate receives the updated schedule. 

Outcome: the interview goes ahead with minimum disruption. 

UC-17 Interviewer declines or becomes unavailable 

Primary actor: interviewer. Trigger: an interviewer declines or cancels after accepting. 

The scheduler is alerted. 

Automation asks an approved backup to confirm the same slot or asks the interviewer to choose another slot inside the candidate's availability. 

For a panel, the session is moved only if a replacement is not acceptable to the hiring manager. 

The candidate is told only if their schedule changes. 

Outcome: the gap is filled without restarting the whole schedule. 

UC-18 Handle a no-show 

Primary actor: scheduling automation. Trigger: the candidate or an interviewer does not attend. 

The interviewer or the candidate reports the no-show. 

Automation asks the person who did not attend for new availability and alerts the scheduler. 

The session is rescheduled, or the outcome is recorded if the candidate does not respond. 

Outcome: every missed session is followed up and has a recorded result. 

UC-19 Stop scheduling when the candidate withdraws or is rejected 

Primary actor: scheduling automation. Trigger: the candidate withdraws or is not moving forward. 

Open availability requests and reminders are cancelled. 

Booked sessions are cancelled, and interviewers are told. 

The candidate's availability is no longer used. 

Outcome: no further messages go to the candidate, and interviewers get their time back. 

4.5 Exceptions 

UC-20 Handle an accommodation request 

Primary actors: candidate and scheduler. Trigger: the candidate asks for an accommodation. 

The candidate states the request in their availability note or directly to the scheduler. 

The case is taken out of automation and handled personally by the scheduler. 

The scheduler arranges the accommodation and builds the schedule around it. 

Outcome: accommodations are always handled by a person. 

UC-21 Escalate what automation cannot resolve 

Primary actor: scheduling automation. Trigger: limited candidate availability, no common slot, an unmet order rule or a special handling need. 

Automation stops and tells the assigned scheduler what it could not resolve and why. 

The scheduler takes over the case manually. 

Outcome: no candidate is left unscheduled because of an automation gap. 

4.6 Hiring events 

Hiring events bring many candidates through interviews in a short window, using a shared pool of interviewers. Organizing the event itself (setting the date, inviting candidates and collecting interviewer commitments) sits outside auto scheduling. Auto scheduling starts once the event window, the confirmed candidate list and the interviewer pool with their committed time blocks are known. 

  

UC-22 Auto-schedule a hiring event 

Primary actors: scheduling automation and scheduler. Trigger: the event window, candidate list and interviewer pool are confirmed. 

Automation builds a loop for every candidate, using only interviewers mapped to that candidate's role and only the time blocks they committed to the event. 

It checks that no interviewer is double-booked, no candidate meets the same interviewer twice and breaks are kept. 

It spreads sessions evenly across the pool and assigns a room or virtual room to each session. 

The scheduler reviews the full schedule, by candidate and by interviewer, makes changes and approves it. 

Once approved, each candidate and each interviewer receives an agenda with only their own sessions. 

Outcome: a complete, conflict-free event schedule, with an agenda sent to everyone taking part. 

UC-23 Reschedule within a hiring event 

Primary actor: scheduling automation. Trigger: a candidate or interviewer is late, drops out or does not turn up. 

The change is picked up as soon as it is reported. 

Sessions are moved to a backup interviewer, shifted later in the day, or rescheduled after the event. 

Freed slots are offered to waitlisted candidates where possible. 

Only the people affected receive updated agendas. 

After the event, interviewer time that was not used is released, and missed sessions are scheduled as follow-up interviews. 

Outcome: the event keeps running with as few gaps as possible. 

5. Business rules 

5.1 Interview plan 

The interview plan is defined for the role at intake and can be adjusted for an individual candidate. 

The plan covers the time period, time zone, location, method (virtual or in person), interview type, interviewers, number of sessions, their order, their length and the time between sessions. 

Hiring manager date preferences captured at intake are respected when building the schedule. 

5.2 Candidate availability 

Every availability request explains the context: role, round, interview type and any constraints from the hiring team. 

The window is time-bound: availability is collected for up to six weeks from the date interviews are requested, never an open-ended or very wide window. 

The candidate must provide at least two full days. A full day is 9 AM to 5 PM in the candidate's time zone. 

Future options the rules must allow for: half days, hourly blocks, a minimum number of slots, custom start and end times, and picking from offered slots, set per role. 

The candidate's time zone is captured with their availability. It defaults to the time zone of their device, and the candidate can change it. 

The candidate can add a plain-text note alongside their availability. 

The candidate can state that no time is available. 

Every change to availability is versioned, and the scheduler is notified when availability is first provided and each time it changes. 

One reminder is sent 48 hours after the request if the candidate has not responded. 

5.3 Interviewer availability 

Calendar free and busy time is used only as a first filter to leave out clearly blocked times. It is not treated as confirmed interviewer availability, because it is often inaccurate. 

Interviewers confirm their availability by choosing from the candidate's available times, after the candidate has responded. 

Interviewers are asked to respond within an agreed time. If they do not, a reminder is sent, then an approved backup is asked, then the scheduler is alerted. 

A slot an interviewer has confirmed is held for them until the schedule is approved or the slot is released. 

An approved backup, delegate or proxy can take an interviewer's place. 

5.4 Building the schedule 

Every session sits inside the candidate's availability and inside a slot each required interviewer has confirmed. 

The order, length and breaks in the interview plan are respected. 

For a panel, all panel members must have confirmed the same slot. 

Schedules are shown to the scheduler in the candidate's time zone by default. 

Automation proposes; the scheduler reviews, can edit, and approves before anything is sent. 

If no workable schedule exists, the case is escalated to the assigned scheduler. 

5.5 Communication and privacy 

Interviewers are invited first. The candidate receives the confirmed schedule after interviewers accept. 

The candidate sees only the details the scheduler chooses to share: date, time zone, start and end times, breaks, location or meeting link, interview type, title and description. 

The candidate always receives the schedule by email, even when it is also available in one place online, for legal reasons. 

When a schedule changes, the candidate and interviewers always see the latest version. 

Interviewer invites follow compliance rules, including no candidate name in the invite. 

Candidate availability and notes are personal data and are handled under privacy and retention rules. 

5.6 Scope 

Executive hiring is out of scope and is scheduled manually. 

Screens of any kind (recruiter, hiring manager and online technical screens) and pre-apply sourcing conversations are out of scope. Coordination work around interviews, such as agreeing the interview plan at intake or organising a hiring event, is an input to auto scheduling rather than part of it. 

Accommodation requests are always handled by the scheduler, not by automation. 

5.7 Hiring events 

Each event is scheduled within its defined date and time window, up to its maximum number of candidates. 

Only the time blocks interviewers committed to the event are used, and those blocks are held on their calendars. 

Each interviewer is mapped to the roles they can interview for, and is only assigned to candidates for those roles. 

Each candidate's loop at the event follows the interview plan for their role: order, length and breaks. 

An interviewer is never booked into two sessions at the same time, and the agreed maximum of consecutive sessions is respected. 

A candidate does not meet the same interviewer twice, and known conflicts of interest are avoided. 

Interviewer load is balanced across the pool. 

The scheduler reviews and approves the full event schedule before agendas are sent. 

Each candidate and each interviewer receives a personal agenda with only their own sessions. 

Unused interviewer time is released as soon as it is known, and again after the event. 

6. Edge cases 

6.1 Candidate availability 

Edge case 

Expected handling 

Candidate gives fewer than two days, or very limited hours 

Accepted and flagged as limited availability; the scheduler is alerted 

Candidate picks dates outside the window or in the past 

Those dates cannot be selected 

Candidate picks weekends or public holidays 

Allowed or blocked by a defined rule, taking holidays in both the candidate's and interviewers' locations into account 

Daylight-saving change falls inside the window 

Times are converted correctly for the candidate, scheduler and interviewers 

Device time zone is wrong (travel or VPN) 

The candidate sees the detected time zone clearly and confirms it before submitting 

Candidate submits before the reminder is due 

The reminder is not sent 

Candidate changes availability after interviews are booked 

The scheduler is alerted to the conflict and decides whether to reschedule 

Candidate is in process for more than one role 

Availability is tied to the right application and does not overwrite the other 

Scheduler sends more than one availability request 

Only the latest request is active; earlier ones stay in history 

The availability link is missing from the request 

The scheduler is warned before sending, because the candidate cannot respond without it 

The link is opened by someone other than the candidate, or after the request has expired 

Sign-in is required; expired requests show a clear message 

6.2 Interviewers and hiring team 

Edge case 

Expected handling 

No common free time between the candidate and interviewers 

Escalated to the scheduler with the reason 

Interviewer declines after the schedule is sent 

An approved backup is used, or only that session is moved 

Interviewer is out of office or has no calendar data 

Treated as unavailable; a backup is considered 

Interviewers are in different time zones from the candidate, leaving little overlap with 9 AM to 5 PM 

Sessions are split across days where the plan allows, otherwise escalated 

Interviewer already has too many interviews that day or week 

Not scheduled beyond the agreed limit; a backup is considered 

Same interviewer appears twice in one loop 

Flagged before the schedule is proposed 

Interviewer does not respond to the availability request 

Reminder sent, then an approved backup is asked, then the scheduler is alerted 

Calendar shows a time as free but the interviewer does not pick it 

The interviewer's choice wins; calendar data is never used on its own to book a session 

Interviewers' confirmed slots do not line up for the loop order 

Automation asks the fewest interviewers possible to adjust, or escalates to the scheduler 

6.3 Loops, panels and finals days 

Edge case 

Expected handling 

Loop does not fit in one day 

Split across days if the plan allows, with the candidate's agreement 

Required order cannot be met (for example the hiring manager must be last) 

Escalated to the scheduler rather than breaking the order silently 

Sessions would run back to back with no break 

The minimum break in the plan is enforced 

One panel member cannot attend 

An approved replacement is used, or the whole panel is moved 

Finals day needs travel 

Not booked until there is enough lead time for travel; location and room are confirmed 

Session is in person but no room is available 

Escalated to the scheduler, or switched to virtual if the plan allows 

6.4 Changes, communication and special handling 

Edge case 

Expected handling 

Candidate does not attend 

The scheduler follows up and reschedules, or records the outcome 

Interviewer does not attend 

The candidate is contacted, and the session is rescheduled at priority 

Candidate withdraws or is rejected mid-scheduling 

Pending requests, reminders and booked sessions are cancelled 

Schedule changes after the candidate has it 

Updated details are sent, and the candidate's view shows the latest version 

Interview needs a language, NDA wording or country-specific rule 

The right template and interviewers are used; otherwise the case is escalated 

Candidate asks for an accommodation 

Taken out of automation and handled by the scheduler 

Executive role 

Not auto-scheduled; handled manually 

6.5 Hiring events 

Edge case 

Expected handling 

More candidates confirm than the event can hold 

Extra candidates go on a waitlist and are offered the next free slot or a later event 

Fewer candidates confirm than planned 

Unused interviewer time blocks are released early so interviewers get their time back 

Interviewer drops out before or on the event day 

Their sessions move to a backup from the pool with the same role skills; affected candidates are told only if their times change 

Candidate does not turn up on the day 

Interviewers are told straight away; the freed slots are offered to waitlisted or rescheduled candidates 

Candidate arrives late 

Remaining sessions are shifted if the day allows; any missed session is rescheduled 

Virtual event with candidates in many time zones 

Candidates are grouped into waves by time zone so each loop falls in their working hours 

Interviewer is not qualified for a candidate's role 

Only interviewers mapped to that role are assigned to the candidate 

Candidate is being considered for more than one role at the event 

One combined agenda with no overlapping sessions 

Same interviewer would meet the same candidate twice, or a conflict of interest exists 

The pairing is blocked when the schedule is built 

Interviewer has too many sessions back to back 

The agreed maximum number of consecutive sessions and minimum break are enforced 

Not enough rooms or virtual rooms for parallel sessions 

Sessions are spread across the day, or the event capacity is reduced 

Event runs over more than one day 

Each candidate receives only the agenda for their day; interviewers receive every day they committed to 

Schedule changes after agendas are sent 

Updated agendas go only to the candidates and interviewers affected 

7. Gaps and open questions 

These points are not settled in the requirements and need a decision before the use cases and rules above can be treated as final. 

Gap 1. Trigger for the availability request: one version starts the request automatically when the candidate moves to the interview stage, another has the scheduler send it. Decide which, or support both. 

Gap 2. Executive cut-off: the requirements say both level 67 and above and level 68 and above. Confirm the level at which roles move to manual scheduling. 

Gap 3. Scheduler notifications: the wording for the "no availability provided" notification was left as a placeholder, and there is no separate notification when a candidate removes availability. 

Gap 4. Automated loop and panel building: defined only for a demo, with success measures and acceptance criteria left open. Constraints beyond the interviewer list and number of interviews (order, breaks, interviewer load, rooms) still need to be agreed. 

Gap 5. Candidate view of the schedule: showing confirmed interview details in one place and letting candidates confirm attendance were planned but not carried forward, so candidates still rely on email. 

Gap 6. Configurable availability: half days, hourly blocks, a minimum number of slots, picking from offered slots and per-role templates are named as future options but not yet defined. 

Gap 7. Level of the interview plan: it is still open whether plan details are captured once per role or for each candidate. 

Gap 8. Reschedule requests: how candidates and the hiring team ask for changes (self-service or by email to the scheduler) has not been decided. 

Gap 9. Request tracking: there is no reliable way to tell whether a request actually carried the availability link, so communication history cannot show it. 

Gap 10. Hiring events: event hiring was listed as out of scope in the original requirements, so there are no agreed rules yet for event capacity, interviewer time commitments, matching interviewers to candidates by role, or same-day changes. The event use cases, rules and edge cases in this document need to be confirmed with the events team. 

Gap 11. Interviewer availability capture: the response time for interviewers, how many slots each interviewer should confirm, and whether a confirmed slot is held across several candidates have not been agreed. 

Appendix A. Sources 

The use cases, business rules and edge cases in this document were taken from the requirement records and working documents below. Items not found in any source were added from scheduling practice and should be confirmed: mainly the edge cases in sections 6.2 to 6.5, the specialized interview types, and all of the hiring event content (UC-22 and UC-23, section 5.7 and section 6.5), because event hiring was out of scope in the original requirements. Interviewer availability capture (UC-06 and section 5.3) reflects a later decision not to rely on calendar free and busy time alone; the earlier working notes (S9) assumed calendar data would be enough. 

Ref 

Source 

Used in 

S1 

Scenario 10978012: HR GTA | CY23 H2 | Industry | Candidate Assessment | Capture Candidate Availability. Azure DevOps, https://microsoftit.visualstudio.com/OneITVSO/_workitems/edit/10978012 

Section 2; exception handling in UC-21 and section 6 

S2 

Feature 10450504: Auto Scheduling: Requesting candidate to share availability for screening and interview rounds. Azure DevOps, https://microsoftit.visualstudio.com/OneITVSO/_workitems/edit/10450504 

Section 2 (cost and delay); UC-01, UC-02; section 5.2 

S3 

Feature 11235462: [MVP] Capture Candidate Availability, including its discussion comments. Azure DevOps, https://microsoftit.visualstudio.com/OneITVSO/_workitems/edit/11235462 

UC-01 to UC-05; sections 5.2 and 5.6; section 6.1; gaps 1 to 3, 6 and 9 

S4 

Scenario 10978029: HR GTA | CY23 H2 | Envision | Auto Scheduling | Auto-matching. Azure DevOps, https://microsoftit.visualstudio.com/OneITVSO/_workitems/edit/10978029 

UC-08, UC-12, UC-21; section 5.4; gap 4 

S5 

Scenario 11628252: HR GTA | CY24 H1 | Intelligent Interview Scheduling | Post Pilot. Azure DevOps, https://microsoftit.visualstudio.com/OneITVSO/_workitems/edit/11628252 

UC-08, UC-21; gap 4 

S6 

Feature 11015460: Action Center Integration: make interview details available to the candidate. Azure DevOps, https://microsoftit.visualstudio.com/OneITVSO/_workitems/edit/11015460 

UC-14, UC-15; section 5.5; gap 5 

S7 

Feature 8297470: Action Center Integration: availability of interview details for a candidacy (includes candidate feedback). Azure DevOps, https://microsoftit.visualstudio.com/OneITVSO/_workitems/edit/8297470 

Section 2 (candidate feedback); UC-15; section 5.5 

S8 

Feature 11123069: Capture candidate RSVP. Azure DevOps, https://microsoftit.visualstudio.com/OneITVSO/_workitems/edit/11123069 

UC-15; gap 5 

S9 

Brainstorm.xlsx (Jess Soyak, Srishti Khatri), Recruiting Experience - Auto Scheduling team site: https://microsoft.sharepoint.com/teams/RecruitingExperience-AutoScheduling/Shared%20Documents/Auto%20Scheduling/Brainstorm.xlsx 

Section 3 (interview types, scope); UC-16, UC-20; sections 5.1 and 5.5; gaps 7 and 8 

 