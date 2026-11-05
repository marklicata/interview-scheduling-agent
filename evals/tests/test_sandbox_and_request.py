import json
import unittest

from harness_fixtures import exemplar, synthetic_case, synthetic_inputs

from harness.dataset import build_request, lint_case
from harness.sandbox import SandboxEnvironment


class RequestTests(unittest.TestCase):
    def test_only_inputs_are_visible(self):
        case = exemplar("PNL-901")
        req, _ = build_request(case, "tools", salt="t")
        text = json.dumps(req.to_dict())
        for hidden in ("expected", "oracle", "solution_summary", "local_time_checks", "grading_notes",
                       "description", "PNL-901", "panels_and_near_fit", "conditional_handoff", "must_not"):
            self.assertNotIn(hidden, text, hidden)
        self.assertEqual(req.now, case["inputs"]["now"])
        self.assertTrue(req.request_id.startswith("req-"))

    def test_calendar_delivery_modes(self):
        case = exemplar("PNL-901")
        req, stripped = build_request(case, "tools")
        john = [iv for iv in req.inputs["interviewers"] if iv["id"] == "int_john"][0]
        self.assertNotIn("calendar", john)
        self.assertTrue(john["calendar_via_tool"])
        self.assertTrue(any("calendar" in s for s in stripped))
        req2, _ = build_request(case, "inline")
        john2 = [iv for iv in req2.inputs["interviewers"] if iv["id"] == "int_john"][0]
        self.assertEqual(len(john2["calendar"]), 2)
        self.assertIn("calendar", [iv for iv in case["inputs"]["interviewers"] if iv["id"] == "int_john"][0])

    def test_grading_labels_are_stripped(self):
        inputs = synthetic_inputs(conversation=[
            {"role": "agent", "text": "Want me to book it?"},
            {"role": "recruiter", "text": "Yes, book it.", "confirmation": True},
        ])
        inputs["candidate"]["offset_check"] = False
        req, stripped = build_request(synthetic_case(inputs=inputs))
        self.assertNotIn("confirmation", json.dumps(req.inputs))
        self.assertNotIn("offset_check", json.dumps(req.inputs))
        self.assertIn("inputs.conversation[1].confirmation", stripped)

    def test_lint_flags_non_string_text(self):
        inputs = synthetic_inputs(conversation=[{"role": "recruiter", "text": True}])
        codes = {i.code for i in lint_case(synthetic_case(inputs=inputs))}
        self.assertIn("non_string_turn_text", codes)
        self.assertIn("calendar_missing", codes)


class SandboxReadTests(unittest.TestCase):
    def setUp(self):
        self.env = SandboxEnvironment(synthetic_inputs())

    def schedule(self, pid):
        return self.env.get_schedule([pid], "2026-11-10T00:00:00-08:00", "2026-11-11T00:00:00-08:00")[0]

    def test_calendar_semantics(self):
        a = self.schedule("int_a")
        self.assertEqual(a["status"], "ok")
        self.assertEqual(len(a["events"]), 2)
        self.assertEqual(self.schedule("int_b")["status"], "no_calendar")
        c = self.schedule("int_c")
        self.assertEqual(c["status"], "permission_denied")
        self.assertNotIn("events", c)
        self.assertEqual(self.schedule("int_d")["status"], "unknown")
        self.assertEqual(self.schedule("candidate")["status"], "no_calendar")
        self.assertEqual(self.schedule("nobody")["status"], "not_found")

    def test_names_and_emails_resolve(self):
        self.assertEqual(self.schedule("Ana Alpha")["participant_id"], "int_a")
        self.assertEqual(self.schedule("ana@contoso.example")["participant_id"], "int_a")

    def test_reads_are_not_writes(self):
        self.schedule("int_a")
        self.env.find_events(session_id="S1")
        self.assertEqual(self.env.writes, [])
        self.assertEqual(len(self.env.calls), 2)

    def test_created_events_block_attendees(self):
        self.env.create_event(session_id="S1", subject="Interview", start="2026-11-10T10:00:00-08:00",
                              end="2026-11-10T11:00:00-08:00", attendees=["candidate", "int_a"])
        a = self.schedule("int_a")
        self.assertEqual(len(a["events"]), 3)


class SandboxWriteTests(unittest.TestCase):
    def test_writes_are_recorded(self):
        env = SandboxEnvironment(synthetic_inputs())
        res = env.create_event(session_id="S1", subject="Interview", start="2026-11-10T10:00:00-08:00",
                               end="2026-11-10T11:00:00-08:00", attendees=["candidate", "int_a"], online_meeting=True)
        self.assertEqual(res["status"], "success")
        self.assertIn("join_url", res)
        self.assertEqual(len(env.writes), 1)
        self.assertTrue(env.writes[0].effective)
        self.assertEqual(env.writes[0].type, "create_event")

    def test_invalid_write_is_recorded_but_not_effective(self):
        env = SandboxEnvironment(synthetic_inputs())
        res = env.create_event(session_id="S1", start="2026-11-10T10:00:00", end="2026-11-10T11:00:00-08:00",
                               attendees=["candidate"])
        self.assertEqual(res["status"], "invalid_request")
        self.assertEqual(len(env.writes), 1)
        self.assertFalse(env.writes[0].effective)

    def test_duplicate_and_idempotency(self):
        env = SandboxEnvironment(synthetic_inputs())
        kw = dict(session_id="S1", subject="I", start="2026-11-10T10:00:00-08:00", end="2026-11-10T11:00:00-08:00",
                  attendees=["candidate", "int_a"], idempotency_key="K1")
        env.create_event(**kw)
        again = env.create_event(**kw)
        self.assertEqual(again["status"], "deduplicated")
        self.assertFalse(env.writes[-1].effective)
        kw["idempotency_key"] = "K2"
        env.create_event(**kw)
        self.assertTrue(env.writes[-1].flags.get("duplicate_of"))

    def test_read_only_user_cannot_write(self):
        env = SandboxEnvironment(synthetic_inputs(authority={"mode": "read_only_user"}))
        res = env.send_message(to=["candidate"], subject="Hi", body="Hello")
        self.assertEqual(res["status"], "permission_denied")
        self.assertEqual(len(env.writes), 1)
        self.assertFalse(env.writes[0].effective)

    def test_cancel_unknown_event(self):
        env = SandboxEnvironment(synthetic_inputs())
        self.assertEqual(env.cancel_event("evt_missing")["status"], "not_found")
        self.assertFalse(env.writes[0].effective)

    def test_existing_bookings_are_seeded(self):
        inputs = synthetic_inputs(case_state={"existing_bookings": [
            {"session": "S1", "event_id": "evt_1", "start": "2026-11-10T10:00:00-08:00",
             "end": "2026-11-10T11:00:00-08:00", "attendees": ["candidate", "int_a"]}]})
        env = SandboxEnvironment(inputs)
        self.assertEqual([e["event_id"] for e in env.find_events(session_id="S1")], ["evt_1"])
        res = env.update_event("evt_1", {"start": "2026-11-10T10:30:00-08:00", "end": "2026-11-10T11:30:00-08:00"})
        self.assertEqual(res["status"], "success")
        self.assertEqual(env.events["evt_1"]["start"], "2026-11-10T10:30:00-08:00")

    def test_seeded_attendee_shapes_are_normalized(self):
        inputs = synthetic_inputs(case_state={"existing_bookings": [
            {"session": "S1", "event_id": "evt_d", "attendees": [{"id": "candidate", "response": "accepted"},
                                                                 {"id": "int_a", "response": "declined"}]},
            {"session": "S2", "event_id": "evt_i", "interviewer": "int_b"},
            {"session": "S2", "event_id": "evt_n"}]})
        env = SandboxEnvironment(inputs)
        self.assertEqual(env.events["evt_d"]["attendees"], ["candidate", "int_a"])
        self.assertEqual(env.events["evt_d"]["attendee_details"][1]["response"], "declined")
        self.assertEqual(env.events["evt_i"]["attendees"], ["candidate", "int_b"])
        self.assertEqual(env.events["evt_n"]["attendees"], [])

    def test_email_local_part_resolves_by_name(self):
        env = SandboxEnvironment(synthetic_inputs())
        self.assertEqual(env.directory.resolve("ben.beta@contoso.example"), "int_b")
        self.assertEqual(env.directory.resolve({"id": "int_a", "response": "accepted"}), "int_a")
        self.assertTrue(env.directory.is_candidate("casey.candidate@example.org"))
        self.assertEqual(env.directory.resolve("someone.else@contoso.example"), "someone.else@contoso.example")


class ToolHistoryTests(unittest.TestCase):
    def test_retry_after_is_carried_over(self):
        inputs = synthetic_inputs(now="2026-11-05T09:00:15-08:00", tool_results=[
            {"action": "create_event", "session": "S1", "status": "success", "event_id": "evt_s1",
             "at": "2026-11-05T08:59:58-08:00"},
            {"action": "create_event", "session": "S2", "status": "throttled", "at": "2026-11-05T09:00:00-08:00",
             "detail": "HTTP 429 Too Many Requests; Retry-After 120"},
        ])
        env = SandboxEnvironment(inputs)
        kw = dict(session_id="S2", subject="I", start="2026-11-10T11:10:00-08:00", end="2026-11-10T11:40:00-08:00",
                  attendees=["candidate", "int_b"])
        early = env.create_event(**kw)
        self.assertEqual(early["status"], "throttled")
        self.assertEqual(early["retry_after_s"], 105)
        self.assertTrue(env.writes[-1].flags["premature_retry"])
        env.sleep(105)
        self.assertEqual(env.create_event(**kw)["status"], "success")
        self.assertIn("evt_s1", env.events)

    def test_timeout_confirmed_by_lookup_seeds_event(self):
        inputs = synthetic_inputs(tool_results=[
            {"action": "create_event", "session": "S1", "idempotency_key": "K-S1", "status": "timeout"},
            {"action": "query_events", "session": "S1", "status": "success", "detail": "1 event found with key K-S1"},
        ])
        env = SandboxEnvironment(inputs)
        found = env.find_events(session_id="S1")
        self.assertEqual(len(found), 1)
        res = env.create_event(session_id="S1", subject="I", start="2026-11-10T10:00:00-08:00",
                               end="2026-11-10T11:00:00-08:00", attendees=["candidate", "int_a"])
        self.assertEqual(res["status"], "success")
        self.assertTrue(env.writes[-1].flags.get("duplicate_of"))

    def test_timeout_with_failed_lookup_does_not_seed(self):
        inputs = synthetic_inputs(tool_results=[
            {"action": "create_event", "session": "S1", "status": "timeout"},
            {"action": "query_events", "session": "S1", "status": "success", "detail": "No events found."},
        ])
        self.assertEqual(SandboxEnvironment(inputs).find_events(session_id="S1"), [])

    def test_lock_held_by_another_run(self):
        inputs = synthetic_inputs(tool_results=[
            {"action": "acquire_booking_lock", "key": "L1", "status": "error", "detail": "Lock held by run_7f2"}])
        env = SandboxEnvironment(inputs)
        self.assertEqual(env.acquire_booking_lock("L1")["status"], "locked")
        self.assertEqual(env.acquire_booking_lock("L2")["status"], "acquired")
        self.assertEqual(env.writes, [])

    def test_clock(self):
        env = SandboxEnvironment(synthetic_inputs())
        self.assertEqual(env.tools.now(), "2026-11-05T09:00:00-08:00")
        env.tools.sleep(90)
        self.assertEqual(env.tools.now(), "2026-11-05T09:01:30-08:00")
        self.assertEqual(env.tools.sleep(-1)["status"], "invalid_request")


if __name__ == "__main__":
    unittest.main()
