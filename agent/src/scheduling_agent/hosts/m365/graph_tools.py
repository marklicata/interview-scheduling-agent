"""CalendarProvider backed by Microsoft Graph (Outlook calendars, Teams meetings, mail, directory).

Same interface as the evaluation sandbox (core/ports.py), so the core behaves identically in both.

- Reads: POST /users/{organizer}/calendar/getSchedule keeps Graph's real statuses (free, tentative, busy, oof,
  workingElsewhere, unknown) and Outlook working hours; nothing is collapsed to yes/no (requirements 9.6).
- Writes: events are created in the organizer mailbox with Graph's transactionId plus extended properties
  holding the agent's idempotency key and session id, so retries are found instead of duplicated (INV-05).
- 429 responses surface Retry-After; ambiguous failures (timeouts, 5xx) are reported as such, never as success.

Authentication: a bearer token for Graph (the signed-in recruiter's delegated token from the Microsoft 365
host's SSO, or an app token for a shared organizer mailbox). Required delegated scopes: Calendars.ReadWrite
(plus Calendars.ReadWrite.Shared for a shared organizer mailbox), Mail.Send, User.ReadBasic.All,
MailboxSettings.Read. Verify tenant consent before enabling writes.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

import httpx

from ...core.timeutil import iso, parse_ts, try_parse_ts

GRAPH = "https://graph.microsoft.com/v1.0"
PROP_NS = "String {00020329-0000-0000-C000-000000000046} Name"
PROP_KEY = f"{PROP_NS} x-scheduling-agent-idempotency-key"
PROP_SESSION = f"{PROP_NS} x-scheduling-agent-session-id"
STATUS = {"free": "free", "tentative": "tentative", "busy": "busy", "oof": "oof",
          "workingelsewhere": "working_elsewhere", "unknown": "unknown"}
# Windows time zone names Graph returns in workingHours/mailboxSettings -> IANA (common zones; extend as needed).
WINDOWS_TZ = {
    "Pacific Standard Time": "America/Los_Angeles", "Mountain Standard Time": "America/Denver",
    "US Mountain Standard Time": "America/Phoenix", "Central Standard Time": "America/Chicago",
    "Eastern Standard Time": "America/New_York", "Atlantic Standard Time": "America/Halifax",
    "Alaskan Standard Time": "America/Anchorage", "Hawaiian Standard Time": "Pacific/Honolulu",
    "GMT Standard Time": "Europe/London", "W. Europe Standard Time": "Europe/Berlin",
    "Romance Standard Time": "Europe/Paris", "Central European Standard Time": "Europe/Warsaw",
    "Central Europe Standard Time": "Europe/Budapest", "E. Europe Standard Time": "Europe/Chisinau",
    "FLE Standard Time": "Europe/Kiev", "GTB Standard Time": "Europe/Bucharest", "Israel Standard Time": "Asia/Jerusalem",
    "India Standard Time": "Asia/Kolkata", "China Standard Time": "Asia/Shanghai", "Tokyo Standard Time": "Asia/Tokyo",
    "Korea Standard Time": "Asia/Seoul", "Singapore Standard Time": "Asia/Singapore",
    "AUS Eastern Standard Time": "Australia/Sydney", "New Zealand Standard Time": "Pacific/Auckland",
    "Arabian Standard Time": "Asia/Dubai", "South Africa Standard Time": "Africa/Johannesburg",
    "E. South America Standard Time": "America/Sao_Paulo", "UTC": "UTC",
}
DAYS = {"monday": "Mon", "tuesday": "Tue", "wednesday": "Wed", "thursday": "Thu", "friday": "Fri",
        "saturday": "Sat", "sunday": "Sun"}
MAX_SLEEP_S = 120


def _iana(name):
    if not name:
        return None
    return WINDOWS_TZ.get(name, name if "/" in name or name == "UTC" else None)


def _utc(dt_text: str) -> str:
    """Graph dateTime (no offset, UTC because of the Prefer header) -> ISO with offset."""
    dt = datetime.fromisoformat(dt_text[:19]).replace(tzinfo=timezone.utc)
    return iso(dt)


def _graph_time(value) -> dict:
    return {"dateTime": parse_ts(value).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "UTC"}


class GraphCalendar:
    def __init__(self, token: str, organizer: str | None, people: dict, candidate_ids: set | None = None,
                 client: httpx.Client | None = None):
        """people: participant id -> email for everyone in the case. organizer: mailbox UPN/id (None = me)."""
        self.token = token
        self.organizer = organizer
        self.people = {k: v for k, v in people.items() if v}
        self.by_email = {v.lower(): k for k, v in self.people.items()}
        self.candidate_ids = candidate_ids or set()
        self.http = client or httpx.Client(timeout=30)
        self._locks: dict = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ plumbing
    @property
    def _mailbox(self) -> str:
        return f"/users/{self.organizer}" if self.organizer else "/me"

    def _req(self, method, path, **kw):
        headers = {"Authorization": f"Bearer {self.token}", "Prefer": 'outlook.timezone="UTC"',
                   **kw.pop("headers", {})}
        try:
            return self.http.request(method, GRAPH + path, headers=headers, **kw), None
        except httpx.TimeoutException:
            return None, {"status": "error", "detail": "Microsoft Graph request timed out; outcome unknown"}
        except httpx.HTTPError as exc:
            return None, {"status": "error", "detail": f"Microsoft Graph request failed: {exc}"}

    @staticmethod
    def _failure(resp) -> dict:
        code = resp.status_code
        if code == 429:
            return {"status": "throttled", "retry_after_s": int(resp.headers.get("Retry-After", "30")),
                    "detail": "HTTP 429 Too Many Requests"}
        status = {400: "invalid_request", 401: "permission_denied", 403: "permission_denied",
                  404: "not_found"}.get(code, "error")
        try:
            msg = resp.json().get("error", {}).get("message", "")
        except ValueError:
            msg = resp.text[:200]
        return {"status": status, "detail": f"HTTP {code}: {msg}"}

    def _email(self, who) -> str:
        return self.people.get(who, who)

    def _event(self, ev: dict) -> dict:
        props = {p.get("id"): p.get("value") for p in ev.get("singleValueExtendedProperties") or []}
        attendees = []
        for a in ev.get("attendees") or []:
            mail = ((a.get("emailAddress") or {}).get("address") or "").lower()
            attendees.append(self.by_email.get(mail, mail))
        return {
            "event_id": ev.get("id"), "subject": ev.get("subject"),
            "start": _utc(ev["start"]["dateTime"]) if ev.get("start") else None,
            "end": _utc(ev["end"]["dateTime"]) if ev.get("end") else None,
            "attendees": attendees, "organizer": ((ev.get("organizer") or {}).get("emailAddress") or {}).get("address"),
            "session_id": props.get(PROP_SESSION), "idempotency_key": props.get(PROP_KEY),
            "state": "cancelled" if ev.get("isCancelled") else "active",
            "join_url": (ev.get("onlineMeeting") or {}).get("joinUrl"),
            "responses": {self.by_email.get(((a.get("emailAddress") or {}).get("address") or "").lower(), "?"):
                          (a.get("status") or {}).get("response") for a in ev.get("attendees") or []},
        }

    # ------------------------------------------------------------------ clock / coordination
    def now(self):
        return datetime.now(timezone.utc)

    def sleep(self, seconds):
        secs = min(float(seconds), MAX_SLEEP_S)
        time.sleep(max(0.0, secs))
        return {"status": "ok", "now": iso(self.now())}

    def acquire_booking_lock(self, key):
        # Process-local. Multi-instance deployments must back this with the shared case store.
        with self._lock:
            if self._locks.get(key):
                return {"status": "locked", "detail": "another booking for this session is in progress"}
            self._locks[key] = True
            return {"status": "acquired"}

    def release_booking_lock(self, key):
        with self._lock:
            return {"status": "released" if self._locks.pop(key, None) else "not_held"}

    # ------------------------------------------------------------------ reads
    def get_schedule(self, participant_ids, start, end):
        ids = [participant_ids] if isinstance(participant_ids, str) else list(participant_ids)
        out, lookup = [], {}
        for pid in ids:
            if pid in self.candidate_ids:
                out.append({"participant_id": pid, "status": "no_calendar",
                            "detail": "Candidate calendars are never accessed; use stated availability."})
            elif not self.people.get(pid):
                out.append({"participant_id": pid, "status": "unknown", "detail": "No email address for this person."})
            else:
                lookup[self.people[pid].lower()] = pid
        if not lookup:
            return out
        body = {"schedules": list(lookup), "startTime": _graph_time(start), "endTime": _graph_time(end),
                "availabilityViewInterval": 15}
        resp, err = self._req("POST", f"{self._mailbox}/calendar/getSchedule", json=body)
        if err or resp.status_code >= 300:
            failure = err or self._failure(resp)
            status = "permission_denied" if failure["status"] == "permission_denied" else "unavailable"
            return out + [{"participant_id": pid, "status": status, "detail": failure["detail"]} for pid in lookup.values()]
        for sched in resp.json().get("value") or []:
            pid = lookup.get(str(sched.get("scheduleId", "")).lower(), sched.get("scheduleId"))
            if sched.get("error"):
                msg = sched["error"].get("message") or ""
                status = "permission_denied" if "permission" in msg.lower() or "denied" in msg.lower() else "unavailable"
                out.append({"participant_id": pid, "status": status, "detail": msg})
                continue
            events = []
            for item in sched.get("scheduleItems") or []:
                st = STATUS.get(str(item.get("status", "busy")).lower(), "busy")
                if item.get("isPrivate") and st == "busy":
                    st = "private_busy"
                ev = {"start": _utc(item["start"]["dateTime"]), "end": _utc(item["end"]["dateTime"]), "status": st}
                if item.get("subject") and not item.get("isPrivate"):
                    ev["subject"] = item["subject"]
                events.append(ev)
            entry = {"participant_id": pid, "status": "ok", "events": events}
            wh = sched.get("workingHours") or {}
            tz = _iana((wh.get("timeZone") or {}).get("name"))
            if wh.get("startTime") and tz:
                entry["working_hours"] = {"days": [DAYS.get(d.lower(), d) for d in wh.get("daysOfWeek") or []],
                                          "start": wh["startTime"][:5], "end": wh["endTime"][:5], "time_zone": tz}
            out.append(entry)
        return out

    def find_events(self, session_id=None, idempotency_key=None, attendee=None, start=None, end=None,
                    include_cancelled=False):
        params = {"$expand": f"singleValueExtendedProperties($filter=id eq '{PROP_KEY}' or id eq '{PROP_SESSION}')",
                  "$top": "50"}
        if idempotency_key or session_id:
            prop, val = (PROP_KEY, idempotency_key) if idempotency_key else (PROP_SESSION, session_id)
            params["$filter"] = (f"singleValueExtendedProperties/Any(ep: ep/id eq '{prop}' and "
                                 f"ep/value eq '{str(val).replace(chr(39), chr(39) * 2)}')")
            path = f"{self._mailbox}/events"
        elif start and end:
            params.update(startDateTime=_graph_time(start)["dateTime"], endDateTime=_graph_time(end)["dateTime"])
            path = f"{self._mailbox}/calendarView"
        else:
            return []
        resp, err = self._req("GET", path, params=params)
        if err or resp.status_code >= 300:
            return [{"status": "error", "detail": (err or self._failure(resp))["detail"]}]
        events = [self._event(e) for e in resp.json().get("value") or []]
        if attendee:
            target = self._email(attendee).lower()
            events = [e for e in events if target in {self._email(a).lower() for a in e["attendees"]}]
        if not include_cancelled:
            events = [e for e in events if e["state"] == "active"]
        return events

    def get_event(self, event_id):
        params = {"$expand": f"singleValueExtendedProperties($filter=id eq '{PROP_KEY}' or id eq '{PROP_SESSION}')"}
        resp, err = self._req("GET", f"{self._mailbox}/events/{event_id}", params=params)
        if err:
            return err
        if resp.status_code >= 300:
            return {**self._failure(resp), "event_id": event_id}
        return self._event(resp.json())

    def lookup_people(self, query):
        q = str(query).replace('"', "")
        resp, err = self._req("GET", "/users", params={"$search": f'"displayName:{q}" OR "mail:{q}"',
                                                      "$select": "id,displayName,mail,officeLocation", "$top": "5"},
                              headers={"ConsistencyLevel": "eventual"})
        if err or resp.status_code >= 300:
            return [{"error": (err or self._failure(resp))["detail"]}]
        out = []
        for u in resp.json().get("value") or []:
            tz = None
            r2, _ = self._req("GET", f"/users/{u['id']}/mailboxSettings/timeZone")
            if r2 is not None and r2.status_code < 300:
                tz = _iana(r2.json().get("value"))
            out.append({"id": u.get("mail") or u["id"], "name": u.get("displayName"), "email": u.get("mail"),
                        "time_zone": tz, "location": u.get("officeLocation")})
        return out

    # ------------------------------------------------------------------ writes
    def create_event(self, session_id=None, subject="", start=None, end=None, attendees=None, organizer=None,
                     online_meeting=False, location=None, phone=None, body=None, idempotency_key=None,
                     sensitivity=None, optional_attendees=None):
        if try_parse_ts(start) is None or try_parse_ts(end) is None or not attendees:
            return {"status": "invalid_request", "detail": "start, end, and attendees are required"}
        text = body or ""
        if phone:
            text = f"{text}\n\nPhone: {phone}".strip()
        payload = {
            "subject": subject, "start": _graph_time(start), "end": _graph_time(end),
            "body": {"contentType": "text", "content": text},
            "attendees": [{"emailAddress": {"address": self._email(a)}, "type": "required"} for a in attendees]
            + [{"emailAddress": {"address": self._email(a)}, "type": "optional"} for a in optional_attendees or []],
            "transactionId": idempotency_key,
            "singleValueExtendedProperties": [p for p in (
                {"id": PROP_KEY, "value": idempotency_key} if idempotency_key else None,
                {"id": PROP_SESSION, "value": session_id} if session_id else None) if p],
        }
        if location:
            payload["location"] = {"displayName": location}
        if sensitivity in ("normal", "personal", "private", "confidential"):
            payload["sensitivity"] = sensitivity
        if online_meeting:
            payload.update(isOnlineMeeting=True, onlineMeetingProvider="teamsForBusiness")
        mailbox = f"/users/{organizer}" if organizer and "@" in str(organizer) else self._mailbox
        resp, err = self._req("POST", f"{mailbox}/events", json=payload)
        if err:
            return {**err, "detail": err["detail"] + " - look up the idempotency key before retrying"}
        if resp.status_code >= 300:
            return self._failure(resp)
        ev = self._event(resp.json())
        return {"status": "success", "event_id": ev["event_id"], "invitations_sent_to": list(attendees),
                **({"join_url": ev["join_url"]} if ev.get("join_url") else {})}

    def update_event(self, event_id, changes=None, notify_attendees=True):
        patch = {}
        for k, v in (changes or {}).items():
            if k in ("start", "end"):
                patch[k] = _graph_time(v)
            elif k == "attendees":
                patch[k] = [{"emailAddress": {"address": self._email(a)}, "type": "required"} for a in v]
            elif k == "location":
                patch[k] = {"displayName": v}
            elif k == "body":
                patch[k] = {"contentType": "text", "content": v}
            elif k == "online_meeting":
                patch.update(isOnlineMeeting=bool(v), onlineMeetingProvider="teamsForBusiness")
            elif k in ("subject", "sensitivity"):
                patch[k] = v
        resp, err = self._req("PATCH", f"{self._mailbox}/events/{event_id}", json=patch)
        if err:
            return {**err, "event_id": event_id}
        if resp.status_code >= 300:
            return {**self._failure(resp), "event_id": event_id}
        return {"status": "success", "event_id": event_id}

    def cancel_event(self, event_id, message=None):
        resp, err = self._req("POST", f"{self._mailbox}/events/{event_id}/cancel", json={"comment": message or ""})
        if err:
            return {**err, "event_id": event_id}
        if resp.status_code >= 300:
            return {**self._failure(resp), "event_id": event_id}
        return {"status": "success", "event_id": event_id}

    def send_message(self, to=None, subject="", body="", channel="email"):
        payload = {"message": {"subject": subject, "body": {"contentType": "text", "content": body},
                               "toRecipients": [{"emailAddress": {"address": self._email(t)}} for t in to or []]},
                   "saveToSentItems": True}
        resp, err = self._req("POST", f"{self._mailbox}/sendMail", json=payload)
        if err:
            return err
        if resp.status_code >= 300:
            return self._failure(resp)
        return {"status": "success"}

    def create_online_meeting(self, session_id=None, subject=None, start=None, end=None):
        return {"status": "invalid_request", "detail": "create the event with online_meeting=true instead"}

    def create_hold(self, attendees=None, start=None, end=None, subject=None, expires=None):
        return {"status": "invalid_request", "detail": "holds are not supported in this release"}

    def publish(self, destination=None, record_id=None, payload=None):
        return {"status": "invalid_request", "detail": "publication is not supported in this release"}
