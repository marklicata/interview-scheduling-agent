"""In-memory CalendarProvider for tests and offline local runs. Nothing leaves the process."""
from __future__ import annotations

import itertools
from datetime import timedelta

from ..core.timeutil import iso, parse_ts, try_parse_ts


class InMemoryCalendar:
    def __init__(self, now, calendars: dict | None = None, people: dict | None = None):
        self._now = parse_ts(now)
        self.calendars = calendars or {}   # participant id -> [{start, end, status, subject?}]
        self.people = people or {}         # alias (name/email) -> id
        self.events: dict = {}
        self.messages: list = []
        self.writes: list = []
        self.locks: dict = {}
        self._ids = itertools.count(1)

    def _id(self, x):
        return self.people.get(x, x)

    def now(self):
        return self._now

    def sleep(self, seconds):
        self._now += timedelta(seconds=float(seconds))
        return {"status": "ok", "now": iso(self._now)}

    def get_schedule(self, participant_ids, start, end):
        out = []
        s, e = parse_ts(start), parse_ts(end)
        for raw in participant_ids:
            pid = self._id(raw)
            if pid not in self.calendars:
                out.append({"participant_id": pid, "status": "unknown"})
                continue
            evs = [dict(ev) for ev in self.calendars[pid]
                   if parse_ts(ev["start"]) < e and s < parse_ts(ev["end"])]
            for ev in self.events.values():
                if ev["state"] == "active" and pid in {self._id(a) for a in ev["attendees"]}:
                    evs.append({"start": ev["start"], "end": ev["end"], "status": "busy", "event_id": ev["event_id"]})
            out.append({"participant_id": pid, "status": "ok", "events": evs})
        return out

    def find_events(self, session_id=None, idempotency_key=None, attendee=None, start=None, end=None,
                    include_cancelled=False):
        out = []
        for ev in self.events.values():
            if ev["state"] != "active" and not include_cancelled:
                continue
            if session_id is not None and ev.get("session_id") != session_id:
                continue
            if idempotency_key is not None and ev.get("idempotency_key") != idempotency_key:
                continue
            if attendee is not None and self._id(attendee) not in {self._id(a) for a in ev["attendees"]}:
                continue
            out.append(dict(ev))
        return out

    def get_event(self, event_id):
        ev = self.events.get(event_id)
        return dict(ev) if ev else {"status": "not_found", "event_id": event_id}

    def acquire_booking_lock(self, key):
        if key in self.locks:
            return {"status": "locked", "detail": "held"}
        self.locks[key] = True
        return {"status": "acquired"}

    def release_booking_lock(self, key):
        return {"status": "released" if self.locks.pop(key, None) else "not_held"}

    def create_event(self, session_id=None, subject="", start=None, end=None, attendees=None, organizer=None,
                     online_meeting=False, location=None, phone=None, body=None, idempotency_key=None,
                     sensitivity=None, optional_attendees=None):
        self.writes.append(("create_event", session_id))
        for ev in self.events.values():
            if idempotency_key and ev.get("idempotency_key") == idempotency_key and ev["state"] == "active":
                return {"status": "deduplicated", "event_id": ev["event_id"]}
        if try_parse_ts(start) is None or try_parse_ts(end) is None:
            return {"status": "invalid_request"}
        eid = f"evt_{next(self._ids)}"
        self.events[eid] = {"event_id": eid, "session_id": session_id, "subject": subject, "start": start, "end": end,
                            "attendees": list(attendees or []), "organizer": organizer, "state": "active",
                            "online_meeting": online_meeting, "idempotency_key": idempotency_key,
                            "sensitivity": sensitivity or "normal",
                            "optional_attendees": list(optional_attendees or [])}
        return {"status": "success", "event_id": eid, "invitations_sent_to": list(attendees or [])}

    def update_event(self, event_id, changes=None, notify_attendees=True):
        self.writes.append(("update_event", event_id))
        ev = self.events.get(event_id)
        if not ev or ev["state"] != "active":
            return {"status": "not_found", "event_id": event_id}
        ev.update(changes or {})
        return {"status": "success", "event_id": event_id}

    def cancel_event(self, event_id, message=None):
        self.writes.append(("cancel_event", event_id))
        ev = self.events.get(event_id)
        if not ev:
            return {"status": "not_found", "event_id": event_id}
        if ev["state"] != "active":
            return {"status": "already_cancelled", "event_id": event_id}
        ev["state"] = "cancelled"
        return {"status": "success", "event_id": event_id}

    def send_message(self, to=None, subject="", body="", channel="email"):
        self.writes.append(("send_message", tuple(to or [])))
        self.messages.append({"to": to, "subject": subject, "body": body})
        return {"status": "success", "message_id": f"msg_{next(self._ids)}"}

    def create_online_meeting(self, session_id=None, subject=None, start=None, end=None):
        self.writes.append(("create_online_meeting", session_id))
        return {"status": "success", "join_url": "https://teams.invalid/meet"}

    def create_hold(self, attendees=None, start=None, end=None, subject=None, expires=None):
        self.writes.append(("create_hold", None))
        return {"status": "success"}

    def publish(self, destination=None, record_id=None, payload=None):
        self.writes.append(("publish", destination))
        return {"status": "success"}
