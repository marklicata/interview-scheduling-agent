"""Microsoft Graph provider tests against a mocked Graph (no tenant, no network)."""
from __future__ import annotations

import json
import unittest

import httpx

from scheduling_agent.hosts.m365.graph_tools import PROP_KEY, GraphCalendar


def make(handler):
    return GraphCalendar("token", "recruiting@contoso.example",
                         {"candidate": "jordan@example.com", "int_priya": "priya@contoso.example"}, {"candidate"},
                         client=httpx.Client(transport=httpx.MockTransport(handler)))


class GraphProviderTests(unittest.TestCase):
    def test_get_schedule_keeps_statuses_private_and_working_hours(self):
        def handler(req):
            body = json.loads(req.content)
            self.assertEqual(body["schedules"], ["priya@contoso.example"])
            self.assertIn("/users/recruiting@contoso.example/calendar/getSchedule", str(req.url))
            return httpx.Response(200, json={"value": [{
                "scheduleId": "priya@contoso.example",
                "scheduleItems": [
                    {"status": "tentative", "subject": "1:1", "start": {"dateTime": "2026-11-10T16:00:00.0000000"},
                     "end": {"dateTime": "2026-11-10T16:30:00.0000000"}},
                    {"status": "busy", "isPrivate": True, "subject": "Doctor",
                     "start": {"dateTime": "2026-11-10T17:00:00.0000000"},
                     "end": {"dateTime": "2026-11-10T18:00:00.0000000"}}],
                "workingHours": {"daysOfWeek": ["monday", "tuesday"], "startTime": "08:00:00.0000000",
                                 "endTime": "17:00:00.0000000", "timeZone": {"name": "Pacific Standard Time"}}}]})

        out = make(handler).get_schedule(["candidate", "int_priya"], "2026-11-10T00:00:00-08:00",
                                         "2026-11-11T00:00:00-08:00")
        self.assertEqual(out[0]["status"], "no_calendar")
        priya = out[1]
        self.assertEqual([e["status"] for e in priya["events"]], ["tentative", "private_busy"])
        self.assertNotIn("subject", priya["events"][1])
        self.assertEqual(priya["working_hours"], {"days": ["Mon", "Tue"], "start": "08:00", "end": "17:00",
                                                  "time_zone": "America/Los_Angeles"})

    def test_permission_error_is_not_free(self):
        def handler(req):
            return httpx.Response(200, json={"value": [{"scheduleId": "priya@contoso.example",
                                                        "error": {"message": "Access denied"}}]})
        out = make(handler).get_schedule(["int_priya"], "2026-11-10T00:00:00Z", "2026-11-11T00:00:00Z")
        self.assertEqual(out[0]["status"], "permission_denied")

    def test_create_event_sends_idempotency_and_teams(self):
        seen = {}

        def handler(req):
            seen.update(json.loads(req.content))
            return httpx.Response(201, json={"id": "AAMk1", "subject": seen["subject"], "start": seen["start"],
                                             "end": seen["end"], "attendees": seen["attendees"],
                                             "onlineMeeting": {"joinUrl": "https://teams/x"}})

        res = make(handler).create_event(session_id="S1", subject="Interview", start="2026-11-10T10:30:00-06:00",
                                         end="2026-11-10T11:00:00-06:00", attendees=["candidate", "int_priya"],
                                         online_meeting=True, idempotency_key="k1")
        self.assertEqual(res["status"], "success")
        self.assertEqual(seen["transactionId"], "k1")
        self.assertIn({"id": PROP_KEY, "value": "k1"}, seen["singleValueExtendedProperties"])
        self.assertEqual(seen["start"], {"dateTime": "2026-11-10T16:30:00", "timeZone": "UTC"})
        self.assertTrue(seen["isOnlineMeeting"])
        self.assertEqual({a["emailAddress"]["address"] for a in seen["attendees"]},
                         {"jordan@example.com", "priya@contoso.example"})

    def test_throttling_reports_retry_after(self):
        res = make(lambda req: httpx.Response(429, headers={"Retry-After": "12"})).create_event(
            session_id="S1", subject="x", start="2026-11-10T10:30:00-06:00", end="2026-11-10T11:00:00-06:00",
            attendees=["int_priya"])
        self.assertEqual((res["status"], res["retry_after_s"]), ("throttled", 12))

    def test_timeout_is_unknown_not_success(self):
        def handler(req):
            raise httpx.ReadTimeout("slow", request=req)
        res = make(handler).create_event(session_id="S1", subject="x", start="2026-11-10T10:30:00-06:00",
                                         end="2026-11-10T11:00:00-06:00", attendees=["int_priya"])
        self.assertEqual(res["status"], "error")
        self.assertIn("outcome unknown", res["detail"])


if __name__ == "__main__":
    unittest.main()
