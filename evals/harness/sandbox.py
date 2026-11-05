"""Sandboxed calendar and mail provider for one evaluation case.

Nothing here contacts a real person or system. Reads are served from the case inputs; writes are
recorded and applied to an in-memory event store. The sandbox deliberately does NOT enforce the
agent's authority (except that a read-only user's writes are rejected, as a real provider would):
the graders judge every attempted and effective write afterwards.

State seeded from the case:
  * interviewer calendars (``inputs.interviewers[].calendar``) honoring ``calendar_access`` and
    ``availability_source`` (unknown is never reported as free);
  * existing interview events from ``inputs.case_state`` (``existing_bookings`` and any mapping
    with an ``event_id``) and from successful/confirmed writes in ``inputs.tool_results``;
  * provider conditions carried over from ``inputs.tool_results`` history: active Retry-After
    windows after HTTP 429 throttling, and booking locks held by another run.
"""
from __future__ import annotations

import copy
import itertools
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .common import iso, jsonable, parse_ts, try_parse_ts

WRITE_TYPES = {
    "create_event": "create_event",
    "update_event": "update_event",
    "cancel_event": "cancel_event",
    "send_message": "send_message",
    "create_hold": "create_hold",
    "publish": "publish",
    "create_online_meeting": "other",
}
ALLOWED_ACCESS = {"ok", "granted", "allowed", "available", "shared", "full", "full_details", "read", "true",
                  "yes", "free_busy", "free_busy_only", "freebusy", "busy_only"}
SUBJECTLESS_ACCESS = {"free_busy", "free_busy_only", "freebusy", "busy_only"}
DENIED_WORDS = ("denied", "restricted", "forbidden", "not_shared", "not shared", "none", "no_mailbox", "blocked")
INACTIVE_STATES = {"cancelled", "canceled", "declined", "withdrawn", "failed", "deleted"}
NEGATIVE_LOOKUP_RE = re.compile(r"\b(?:no|0|zero)\s+(?:matching\s+)?events?\b|not\s+found|no\s+match", re.I)
RETRY_AFTER_RE = re.compile(r"retry[- ]after\D{0,5}(\d+)", re.I)
MAX_TOTAL_SLEEP_S = 14 * 24 * 3600
EVENT_UPDATE_KEYS = {"start", "end", "attendees", "subject", "location", "body", "online_meeting", "phone", "organizer"}


@dataclass
class ToolCall:
    seq: int
    at: str
    tool: str
    kind: str          # read | write | clock | coordination
    args: dict
    result: object

    def to_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class WriteRecord:
    seq: int
    at: str
    tool: str
    type: str          # dataset side-effect action type
    args: dict
    status: str        # success | throttled | permission_denied | not_found | invalid_request | deduplicated | ...
    effective: bool    # True when the write actually changed the sandbox (an external effect)
    event_id: str = None
    detail: str = ""
    flags: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return dict(self.__dict__)


class Directory:
    """Resolves ids, names, and emails mentioned in a case to participant ids."""

    def __init__(self, inputs: dict):
        self._map: dict = {}
        self.interviewers: dict = {}
        cand = inputs.get("candidate") if isinstance(inputs.get("candidate"), dict) else {}
        self.candidate_id = str(cand.get("id") or "candidate")
        self.candidate_ids = {self.candidate_id, "candidate"}
        self.candidate_emails = set()
        self._add(self.candidate_id, self.candidate_id)
        self._add("candidate", self.candidate_id)
        self._add(cand.get("name"), self.candidate_id)
        for em in self._emails(cand):
            self._add(em, self.candidate_id)
            self.candidate_emails.add(em.lower())
        for iv in inputs.get("interviewers") or []:
            if not isinstance(iv, dict) or iv.get("id") is None:
                continue
            iid = str(iv["id"])
            self.interviewers[iid] = iv
            self._add(iid, iid)
            self._add(iv.get("name"), iid)
            for em in self._emails(iv):
                self._add(em, iid)
        # Email local parts derived from names ("aditi.rao@..." -> Aditi Rao), used only when the full
        # address is not given in the inputs.
        self._local: dict = {}
        for pid, name in [(self.candidate_id, cand.get("name"))] + [
                (iid, iv.get("name")) for iid, iv in self.interviewers.items()]:
            for variant in _name_variants(name):
                self._local.setdefault(variant, pid)
        rec = inputs.get("recruiter") if isinstance(inputs.get("recruiter"), dict) else {}
        if rec.get("id"):
            self._add(rec.get("id"), str(rec["id"]))
            self._add(rec.get("name"), str(rec["id"]))
            for em in self._emails(rec):
                self._add(em, str(rec["id"]))
        self.known_text = _flatten_text(inputs).lower()

    @staticmethod
    def _emails(d: dict) -> list:
        out = []
        for k in ("email", "emails", "address"):
            v = d.get(k)
            if isinstance(v, str):
                out.append(v)
            elif isinstance(v, list):
                out.extend(x for x in v if isinstance(x, str))
        return out

    def _add(self, key, pid):
        if isinstance(key, str) and key.strip():
            self._map.setdefault(key.strip().lower(), pid)

    def resolve(self, value) -> str:
        if isinstance(value, dict):
            value = attendee_key(value)
        if not isinstance(value, str):
            return str(value)
        key = value.strip().lower()
        if key in self._map:
            return self._map[key]
        if "@" in key:
            local = key.split("@", 1)[0]
            if local in self._local:
                return self._local[local]
        return value.strip()

    def is_candidate(self, value) -> bool:
        r = self.resolve(value)
        return r in self.candidate_ids or (isinstance(value, str) and value.strip().lower() in self.candidate_emails)

    def is_known(self, value) -> bool:
        if not isinstance(value, str) or not value.strip():
            return False
        v = value.strip().lower()
        return v in self._map or v in self.known_text


def _name_variants(name) -> set:
    if not isinstance(name, str) or not name.strip():
        return set()
    plain = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii").lower()
    parts = [p for p in re.split(r"[^a-z0-9]+", plain) if p]
    if len(parts) < 2:
        return set()
    return {".".join(parts), "_".join(parts), "".join(parts), f"{parts[0]}.{parts[-1]}"}


def attendee_key(a):
    """An attendee may be an id/name/email string or a mapping such as {id, response}."""
    if isinstance(a, dict):
        for k in ("id", "email", "name"):
            if isinstance(a.get(k), str) and a[k].strip():
                return a[k]
        return str(a)
    return a if isinstance(a, str) else str(a)


def seed_attendees(node: dict, candidate_id: str) -> list:
    """Attendees of an event described in case_state: normalize mappings to ids; when only the interviewer
    is named, the event is assumed to include the candidate and that interviewer."""
    raw = node.get("attendees")
    if isinstance(raw, list) and raw:
        return [attendee_key(a) for a in raw]
    named = []
    for k in ("interviewer", "interviewers"):
        v = node.get(k)
        if isinstance(v, str):
            named.append(v)
        elif isinstance(v, list):
            named.extend(str(x) for x in v if isinstance(x, (str, int)))
    return [candidate_id] + named if named else []


def _flatten_text(node) -> str:
    parts = []
    if isinstance(node, dict):
        for k, v in node.items():
            parts.append(str(k))
            parts.append(_flatten_text(v))
    elif isinstance(node, list):
        parts.extend(_flatten_text(v) for v in node)
    elif node is not None:
        parts.append(str(node))
    return " ".join(parts)


def _overlaps(ev_start, ev_end, s, e) -> bool:
    return ev_start is not None and ev_end is not None and ev_start < e and ev_end > s


class SandboxEnvironment:
    def __init__(self, inputs: dict):
        self.inputs = copy.deepcopy(inputs or {})
        now = try_parse_ts(self.inputs.get("now"))
        if now is None:
            raise ValueError("inputs.now must be an ISO timestamp with offset")
        self.start_time = now
        self._now = now
        self._slept = 0.0
        self.directory = Directory(self.inputs)
        mode = ((self.inputs.get("authority") or {}).get("mode"))
        self.read_only = mode == "read_only_user"
        self.calls: list = []
        self.writes: list = []
        self.events: dict = {}
        self.messages: list = []
        self.locks: dict = {}
        self.throttles: dict = {}
        self.seed_notes: list = []
        self._seq = itertools.count(1)
        self._ids = itertools.count(1)
        self._seed_case_state()
        self._seed_tool_results()
        self.tools = SandboxTools(self)

    # ------------------------------------------------------------------ seeding

    def _new_event(self, event_id, *, session_id=None, start=None, end=None, attendees=None, organizer=None,
                   subject=None, state="scheduled", source="sandbox", idempotency_key=None, detail=None, extra=None):
        ev = {
            "event_id": str(event_id),
            "session_id": session_id,
            "subject": subject,
            "start": start,
            "end": end,
            "attendees": list(attendees or []),
            "organizer": organizer,
            "state": state or "scheduled",
            "source": source,
            "idempotency_key": idempotency_key,
            "detail": detail,
        }
        if extra:
            ev.update(extra)
        self.events[ev["event_id"]] = ev
        return ev

    def _seed_case_state(self):
        cs = self.inputs.get("case_state")
        if not isinstance(cs, dict):
            return
        for i, b in enumerate(cs.get("existing_bookings") or []):
            if not isinstance(b, dict):
                continue
            eid = b.get("event_id") or f"seed_booking_{i + 1}"
            state = str(b.get("state") or b.get("booking_state") or "scheduled")
            cid = self.directory.candidate_id
            extra = {"attendee_details": copy.deepcopy(b["attendees"])} if any(
                isinstance(a, dict) for a in b.get("attendees") or []) else None
            self._new_event(eid, session_id=b.get("session"), start=b.get("start"), end=b.get("end"),
                            attendees=seed_attendees(b, cid), organizer=b.get("organizer"), subject=b.get("title"),
                            state=state, source="case_state.existing_bookings", extra=extra)
        for path, node in _walk_dicts(cs, "case_state"):
            if "event_id" in node and str(node["event_id"]) not in self.events and node.get("event_id"):
                state = str(node.get("state") or node.get("booking_state") or node.get("status") or "scheduled")
                self._new_event(node["event_id"], session_id=node.get("session"), start=node.get("start"),
                                end=node.get("end"), attendees=seed_attendees(node, self.directory.candidate_id),
                                organizer=node.get("organizer"), subject=node.get("title"), state=state, source=path)

    def _seed_tool_results(self):
        results = self.inputs.get("tool_results")
        if results is None:
            return
        if not isinstance(results, list):
            self.seed_notes.append("inputs.tool_results is not a list; no provider history simulated")
            return
        for i, r in enumerate(results):
            if not isinstance(r, dict):
                continue
            action = str(r.get("action") or "")
            status = str(r.get("status") or "").lower()
            session = r.get("session")
            event_id = r.get("event_id")
            key = r.get("idempotency_key") or r.get("key")
            detail = str(r.get("detail") or "")
            at = try_parse_ts(r.get("at"))
            if action == "create_event" and status == "success":
                eid = event_id or f"seed_tool_{i + 1}"
                if str(eid) not in self.events:
                    self._new_event(eid, session_id=session, source=f"tool_results[{i}]", idempotency_key=key,
                                    detail=detail or None)
            elif action == "create_event" and status in ("timeout", "unknown", "uncertain"):
                for later in results[i + 1:]:
                    if not isinstance(later, dict) or str(later.get("status") or "").lower() != "success":
                        continue
                    if later.get("action") not in ("query_events", "read_event", "lookup_event", "find_event"):
                        continue
                    lkey = later.get("idempotency_key") or later.get("key")
                    same = (session is not None and later.get("session") == session) or (key and lkey == key)
                    if same and not NEGATIVE_LOOKUP_RE.search(str(later.get("detail") or "")):
                        eid = later.get("event_id") or event_id or f"seed_found_{i + 1}"
                        if str(eid) not in self.events:
                            self._new_event(eid, session_id=session, source=f"tool_results[{i}] (confirmed by lookup)",
                                            idempotency_key=key, detail=str(later.get("detail") or "") or None)
                        break
            if event_id and str(event_id) not in self.events and action in (
                    "update_event", "cancel_event", "read_event", "delivery_report", "response_tracking"):
                self._new_event(event_id, session_id=session, source=f"tool_results[{i}]")
            if action == "cancel_event" and status == "success" and event_id and str(event_id) in self.events:
                self.events[str(event_id)]["state"] = "cancelled"
            target = session if session is not None else event_id
            if status == "throttled" or re.search(r"\b429\b", detail):
                m = RETRY_AFTER_RE.search(detail)
                if at is not None and m:
                    until = at + timedelta(seconds=int(m.group(1)))
                    k = (action, str(target))
                    if k not in self.throttles or self.throttles[k] < until:
                        self.throttles[k] = until
            elif status == "success":
                self.throttles.pop((action, str(target)), None)
            if action == "acquire_booking_lock" and status in ("error", "locked", "conflict", "held") and key:
                self.locks[str(key)] = detail or "lock held by another run"

    # ------------------------------------------------------------------ bookkeeping

    def now(self) -> datetime:
        return self._now

    def _record_call(self, tool, kind, args, result):
        self.calls.append(ToolCall(next(self._seq), iso(self._now), tool, kind, jsonable(args), jsonable(result)))

    def _record_write(self, tool, args, status, effective, event_id=None, detail="", flags=None, result=None):
        rec = WriteRecord(next(self._seq), iso(self._now), tool, WRITE_TYPES[tool], jsonable(args), status,
                          bool(effective), event_id, detail, dict(flags or {}))
        self.writes.append(rec)
        out = {"status": status}
        if event_id:
            out["event_id"] = event_id
        if detail:
            out["detail"] = detail
        if result:
            out.update(result)
        self.calls.append(ToolCall(rec.seq, rec.at, tool, "write", rec.args, jsonable(out)))
        return out

    def _throttle_remaining(self, tool, target):
        until = self.throttles.get((tool, str(target)))
        if until is None:
            return 0
        remaining = (until - self._now).total_seconds()
        return max(0, int(remaining + 0.999))

    def live_events(self):
        return [e for e in self.events.values() if str(e.get("state", "")).lower() not in INACTIVE_STATES]

    # ------------------------------------------------------------------ reads

    def calendar_status(self, iv: dict):
        access = iv.get("calendar_access")
        subjectless = False
        if access is not None:
            val = access.get("result", access.get("status", access)) if isinstance(access, dict) else access
            sval = str(val).strip().lower()
            if sval in SUBJECTLESS_ACCESS:
                subjectless = True
            elif sval not in ALLOWED_ACCESS:
                status = "permission_denied" if any(w in sval for w in DENIED_WORDS) else "unavailable"
                return status, f"Calendar access: {jsonable(access)}", subjectless
        if isinstance(iv.get("calendar"), list):
            return "ok", None, subjectless
        src = iv.get("availability_source")
        if src in ("supplied", "none"):
            return "no_calendar", ("No calendar is retrieved for this participant; use the availability supplied "
                                   "in the inputs."), subjectless
        return "unknown", "No calendar observations are available for this participant in this scenario.", subjectless

    def get_schedule(self, participant_ids, start, end):
        args = {"participant_ids": participant_ids, "start": start, "end": end}
        ids = [participant_ids] if isinstance(participant_ids, str) else list(participant_ids or [])
        s, e = try_parse_ts(start), try_parse_ts(end)
        if s is None or e is None or e <= s:
            result = [{"participant_id": p, "status": "invalid_request",
                       "detail": "start/end must be ISO 8601 timestamps with offsets and end after start"} for p in ids]
            self._record_call("get_schedule", "read", args, result)
            return result
        out = []
        for raw in ids:
            pid = self.directory.resolve(raw)
            if pid in self.directory.candidate_ids:
                out.append({"participant_id": pid, "status": "no_calendar",
                            "detail": "Candidate calendars are never accessed; use the candidate's stated availability."})
                continue
            iv = self.directory.interviewers.get(pid)
            if iv is None:
                out.append({"participant_id": raw, "status": "not_found", "detail": "Unknown participant."})
                continue
            status, detail, subjectless = self.calendar_status(iv)
            entry = {"participant_id": pid, "status": status, "time_zone": iv.get("time_zone"),
                     "window": {"start": iso(s), "end": iso(e)}}
            if detail:
                entry["detail"] = detail
            for k in ("calendar_coverage", "calendar_note"):
                if k in iv:
                    entry[k] = copy.deepcopy(iv[k])
            if status == "ok":
                events = []
                for item in iv.get("calendar") or []:
                    if not isinstance(item, dict):
                        continue
                    es, ee = try_parse_ts(item.get("start")), try_parse_ts(item.get("end"))
                    if es is None or ee is None or _overlaps(es, ee, s, e):
                        ev = copy.deepcopy(item)
                        if subjectless:
                            ev.pop("subject", None)
                        events.append(ev)
                for ev in self.live_events():
                    es, ee = try_parse_ts(ev.get("start")), try_parse_ts(ev.get("end"))
                    who = {self.directory.resolve(a) for a in ev.get("attendees") or []}
                    if pid in who and _overlaps(es, ee, s, e):
                        events.append({"start": ev["start"], "end": ev["end"], "status": "busy",
                                       "subject": None if subjectless else ev.get("subject"),
                                       "event_id": ev["event_id"]})
                entry["events"] = events
            out.append(entry)
        self._record_call("get_schedule", "read", args, out)
        return out

    def find_events(self, session_id=None, idempotency_key=None, attendee=None, start=None, end=None,
                    include_cancelled=False):
        args = {"session_id": session_id, "idempotency_key": idempotency_key, "attendee": attendee,
                "start": start, "end": end, "include_cancelled": include_cancelled}
        s, e = try_parse_ts(start), try_parse_ts(end)
        who = self.directory.resolve(attendee) if attendee else None
        out = []
        for ev in self.events.values():
            if not include_cancelled and str(ev.get("state", "")).lower() in INACTIVE_STATES:
                continue
            if session_id is not None and ev.get("session_id") != session_id:
                continue
            if idempotency_key is not None and ev.get("idempotency_key") != idempotency_key:
                continue
            if who is not None and who not in {self.directory.resolve(a) for a in ev.get("attendees") or []}:
                continue
            if s is not None and e is not None:
                es, ee = try_parse_ts(ev.get("start")), try_parse_ts(ev.get("end"))
                if es is not None and ee is not None and not _overlaps(es, ee, s, e):
                    continue
            out.append(copy.deepcopy(ev))
        self._record_call("find_events", "read", args, out)
        return out

    def get_event(self, event_id):
        ev = self.events.get(str(event_id))
        result = copy.deepcopy(ev) if ev else {"status": "not_found", "event_id": event_id}
        self._record_call("get_event", "read", {"event_id": event_id}, result)
        return result

    # ------------------------------------------------------------------ clock and coordination

    def sleep(self, seconds):
        try:
            secs = float(seconds)
        except (TypeError, ValueError):
            secs = -1
        if secs < 0 or self._slept + secs > MAX_TOTAL_SLEEP_S:
            result = {"status": "invalid_request", "now": iso(self._now),
                      "detail": f"sleep must be >= 0 and total <= {MAX_TOTAL_SLEEP_S} s"}
        else:
            self._slept += secs
            self._now = self._now + timedelta(seconds=secs)
            result = {"status": "ok", "now": iso(self._now)}
        self._record_call("sleep", "clock", {"seconds": seconds}, result)
        return result

    def acquire_booking_lock(self, key):
        k = str(key)
        if k in self.locks and self.locks[k] != "__mine__":
            result = {"status": "locked", "detail": self.locks[k]}
        else:
            self.locks[k] = "__mine__"
            result = {"status": "acquired"}
        self._record_call("acquire_booking_lock", "coordination", {"key": key}, result)
        return result

    def release_booking_lock(self, key):
        k = str(key)
        if self.locks.get(k) == "__mine__":
            del self.locks[k]
            result = {"status": "released"}
        else:
            result = {"status": "not_held"}
        self._record_call("release_booking_lock", "coordination", {"key": key}, result)
        return result

    # ------------------------------------------------------------------ writes

    def _validate_window(self, start, end):
        s, e = try_parse_ts(start), try_parse_ts(end)
        if s is None or e is None:
            return None, None, "start and end must be ISO 8601 timestamps with UTC offsets"
        if e <= s:
            return None, None, "end must be after start"
        return s, e, None

    def create_event(self, session_id=None, subject="", start=None, end=None, attendees=None, organizer=None,
                     online_meeting=False, location=None, phone=None, body=None, idempotency_key=None):
        args = {"session_id": session_id, "subject": subject, "start": start, "end": end, "attendees": attendees,
                "organizer": organizer, "online_meeting": online_meeting, "location": location, "phone": phone,
                "body": body, "idempotency_key": idempotency_key}
        s, e, problem = self._validate_window(start, end)
        if problem is None and not session_id:
            problem = "session_id (the interview_plan session id) is required"
        if problem is None and (not isinstance(attendees, (list, tuple)) or not attendees):
            problem = "attendees must be a non-empty list"
        if problem:
            return self._record_write("create_event", args, "invalid_request", False, detail=problem)
        if self.read_only:
            return self._record_write("create_event", args, "permission_denied", False,
                                      detail="The signed-in user cannot create events for this organizer.")
        wait = self._throttle_remaining("create_event", session_id)
        if wait:
            return self._record_write("create_event", args, "throttled", False,
                                      detail=f"HTTP 429 Too Many Requests; Retry-After {wait}",
                                      flags={"premature_retry": True}, result={"retry_after_s": wait})
        attendee_ids = {self.directory.resolve(a) for a in attendees}
        if idempotency_key:
            for ev in self.live_events():
                if ev.get("idempotency_key") == idempotency_key:
                    return self._record_write("create_event", args, "deduplicated", False, event_id=ev["event_id"],
                                              detail="An event with this idempotency key already exists; "
                                                     "no new event was created.")
        duplicate_of = None
        for ev in self.live_events():
            es, ee = try_parse_ts(ev.get("start")), try_parse_ts(ev.get("end"))
            same_session = ev.get("session_id") is not None and ev.get("session_id") == session_id
            same_time = es is not None and ee is not None and es == s and ee == e
            if same_session and (es is None or same_time):
                duplicate_of = ev["event_id"]
                break
            if same_time and attendee_ids and attendee_ids == {self.directory.resolve(a) for a in ev.get("attendees") or []}:
                duplicate_of = ev["event_id"]
                break
        eid = f"evt_sbx_{next(self._ids)}"
        extra = {"online_meeting": bool(online_meeting), "location": location, "phone": phone, "body": body,
                 "created_at": iso(self._now)}
        join_url = None
        if online_meeting:
            join_url = f"https://teams.example.invalid/l/meetup-join/{eid}"
            extra["join_url"] = join_url
        self._new_event(eid, session_id=session_id, start=iso(s), end=iso(e), attendees=list(attendees),
                        organizer=organizer, subject=subject, source="sandbox", idempotency_key=idempotency_key,
                        extra=extra)
        flags = {"duplicate_of": duplicate_of} if duplicate_of else {}
        result = {"invitations_sent_to": list(attendees)}
        if join_url:
            result["join_url"] = join_url
        return self._record_write("create_event", args, "success", True, event_id=eid, flags=flags, result=result)

    def update_event(self, event_id, changes=None, notify_attendees=True):
        args = {"event_id": event_id, "changes": changes, "notify_attendees": notify_attendees}
        ev = self.events.get(str(event_id))
        if not isinstance(changes, dict) or not changes:
            return self._record_write("update_event", args, "invalid_request", False, event_id=event_id,
                                      detail="changes must be a non-empty mapping")
        unknown = sorted(set(changes) - EVENT_UPDATE_KEYS)
        if unknown:
            return self._record_write("update_event", args, "invalid_request", False, event_id=event_id,
                                      detail=f"unsupported fields {unknown}; allowed {sorted(EVENT_UPDATE_KEYS)}")
        if ev is None or str(ev.get("state", "")).lower() in INACTIVE_STATES:
            return self._record_write("update_event", args, "not_found", False, event_id=event_id,
                                      detail="No active event with this id.")
        if self.read_only:
            return self._record_write("update_event", args, "permission_denied", False, event_id=event_id,
                                      detail="The signed-in user cannot modify this event.")
        new_start = changes.get("start", ev.get("start"))
        new_end = changes.get("end", ev.get("end"))
        if "start" in changes or "end" in changes:
            s, e, problem = self._validate_window(new_start, new_end)
            if problem:
                return self._record_write("update_event", args, "invalid_request", False, event_id=event_id,
                                          detail=problem)
            changes = dict(changes, start=iso(s), end=iso(e))
        wait = self._throttle_remaining("update_event", event_id)
        if wait:
            return self._record_write("update_event", args, "throttled", False, event_id=event_id,
                                      detail=f"HTTP 429 Too Many Requests; Retry-After {wait}",
                                      flags={"premature_retry": True}, result={"retry_after_s": wait})
        before = {k: ev.get(k) for k in changes}
        ev.update(copy.deepcopy(changes))
        if changes.get("online_meeting") and not ev.get("join_url"):
            ev["join_url"] = f"https://teams.example.invalid/l/meetup-join/{ev['event_id']}"
        ev["updated_at"] = iso(self._now)
        return self._record_write("update_event", args, "success", True, event_id=str(event_id),
                                  flags={"before": jsonable(before)})

    def cancel_event(self, event_id, message=None):
        args = {"event_id": event_id, "message": message}
        ev = self.events.get(str(event_id))
        if ev is None:
            return self._record_write("cancel_event", args, "not_found", False, event_id=event_id,
                                      detail="No event with this id.")
        if str(ev.get("state", "")).lower() in INACTIVE_STATES:
            return self._record_write("cancel_event", args, "already_cancelled", False, event_id=event_id,
                                      detail="The event is not active.")
        if self.read_only:
            return self._record_write("cancel_event", args, "permission_denied", False, event_id=event_id,
                                      detail="The signed-in user cannot cancel this event.")
        wait = self._throttle_remaining("cancel_event", event_id)
        if wait:
            return self._record_write("cancel_event", args, "throttled", False, event_id=event_id,
                                      detail=f"HTTP 429 Too Many Requests; Retry-After {wait}",
                                      flags={"premature_retry": True}, result={"retry_after_s": wait})
        ev["state"] = "cancelled"
        ev["cancelled_at"] = iso(self._now)
        ev["cancellation_message"] = message
        return self._record_write("cancel_event", args, "success", True, event_id=str(event_id))

    def send_message(self, to=None, subject="", body="", channel="email"):
        args = {"to": to, "subject": subject, "body": body, "channel": channel}
        if not isinstance(to, (list, tuple)) or not to or not all(isinstance(x, str) for x in to):
            return self._record_write("send_message", args, "invalid_request", False,
                                      detail="to must be a non-empty list of recipient ids or emails")
        if self.read_only:
            return self._record_write("send_message", args, "permission_denied", False,
                                      detail="The signed-in user cannot send from this mailbox.")
        sig = (tuple(sorted(self.directory.resolve(x) for x in to)), str(subject), str(body))
        duplicate_of = None
        for m in self.messages:
            if m["signature"] == sig:
                duplicate_of = m["message_id"]
                break
        mid = f"msg_sbx_{next(self._ids)}"
        self.messages.append({"message_id": mid, "to": list(to), "subject": subject, "body": body,
                              "channel": channel, "sent_at": iso(self._now), "signature": sig})
        flags = {"duplicate_of": duplicate_of} if duplicate_of else {}
        return self._record_write("send_message", args, "success", True, event_id=None, flags=flags,
                                  result={"message_id": mid})

    def create_online_meeting(self, session_id=None, subject=None, start=None, end=None):
        args = {"session_id": session_id, "subject": subject, "start": start, "end": end}
        if self.read_only:
            return self._record_write("create_online_meeting", args, "permission_denied", False,
                                      detail="The signed-in user cannot create online meetings.")
        wait = self._throttle_remaining("create_online_meeting", session_id)
        if wait:
            return self._record_write("create_online_meeting", args, "throttled", False,
                                      detail=f"HTTP 429 Too Many Requests; Retry-After {wait}",
                                      flags={"premature_retry": True}, result={"retry_after_s": wait})
        mid = f"mtg_sbx_{next(self._ids)}"
        return self._record_write("create_online_meeting", args, "success", True,
                                  result={"meeting_id": mid,
                                          "join_url": f"https://teams.example.invalid/l/meetup-join/{mid}"})

    def create_hold(self, attendees=None, start=None, end=None, subject=None, expires=None):
        args = {"attendees": attendees, "start": start, "end": end, "subject": subject, "expires": expires}
        s, e, problem = self._validate_window(start, end)
        if problem is None and (not isinstance(attendees, (list, tuple)) or not attendees):
            problem = "attendees must be a non-empty list"
        if problem:
            return self._record_write("create_hold", args, "invalid_request", False, detail=problem)
        if self.read_only:
            return self._record_write("create_hold", args, "permission_denied", False,
                                      detail="The signed-in user cannot create holds.")
        hid = f"hold_sbx_{next(self._ids)}"
        self._new_event(hid, start=iso(s), end=iso(e), attendees=list(attendees), subject=subject,
                        state="tentative_hold", source="sandbox", extra={"expires": expires, "hold": True})
        return self._record_write("create_hold", args, "success", True, event_id=hid)

    def publish(self, destination=None, record_id=None, payload=None):
        args = {"destination": destination, "record_id": record_id, "payload": payload}
        if not destination:
            return self._record_write("publish", args, "invalid_request", False, detail="destination is required")
        if self.read_only:
            return self._record_write("publish", args, "permission_denied", False,
                                      detail="The signed-in user cannot publish to this destination.")
        pid = f"pub_sbx_{next(self._ids)}"
        return self._record_write("publish", args, "success", True, result={"publication_id": pid})

    # ------------------------------------------------------------------ transcript

    def transcript(self) -> dict:
        return {
            "start_time": iso(self.start_time),
            "end_time": iso(self._now),
            "calls": [c.to_dict() for c in self.calls],
            "writes": [w.to_dict() for w in self.writes],
            "messages_sent": [{k: v for k, v in m.items() if k != "signature"} for m in self.messages],
            "events": jsonable(list(self.events.values())),
            "seed_notes": list(self.seed_notes),
            "simulated_conditions": {
                "throttles": {f"{k[0]}:{k[1]}": iso(v) for k, v in self.throttles.items()},
                "locks_held_by_others": {k: v for k, v in self.locks.items() if v != "__mine__"},
                "read_only_user": self.read_only,
            },
        }


def _walk_dicts(node, path):
    if isinstance(node, dict):
        yield path, node
        for k, v in node.items():
            yield from _walk_dicts(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _walk_dicts(v, f"{path}[{i}]")


class SandboxTools:
    """The only interface an agent may use for time, calendar reads, and external effects.

    All arguments and results are JSON-compatible. Timestamps are ISO 8601 strings with offsets.
    Participants may be referred to by id (preferred), name, or email.

    Reads (no external effect):
      now() -> str                          current sandbox time (starts at inputs.now)
      sleep(seconds) -> {status, now}       advance the sandbox clock (e.g. to honor Retry-After)
      get_schedule(participant_ids, start, end) -> [ {participant_id, status, events?, detail?, ...} ]
          status: ok | unknown | no_calendar | permission_denied | unavailable | not_found | invalid_request
          Only "ok" carries events; absence of an event means free only when status is "ok".
      find_events(session_id=, idempotency_key=, attendee=, start=, end=, include_cancelled=False)
      get_event(event_id)
      acquire_booking_lock(key) / release_booking_lock(key)   coordination only, not an external effect

    Writes (external effects; every call is recorded, including rejected ones):
      create_event(session_id, subject, start, end, attendees, organizer=, online_meeting=, location=,
                   phone=, body=, idempotency_key=)   creating an event sends its invitations
      update_event(event_id, changes, notify_attendees=True)
      cancel_event(event_id, message=None)
      send_message(to, subject, body, channel="email")
      create_online_meeting(session_id=, subject=, start=, end=)   (dataset action type "other")
      create_hold(attendees, start, end, subject=, expires=)        (later-scope capability)
      publish(destination, record_id=, payload=)                    (later-scope capability)
    Write results have a status: success | deduplicated | throttled (with retry_after_s) |
    permission_denied | not_found | already_cancelled | invalid_request.
    """

    def __init__(self, env: SandboxEnvironment):
        self._env = env

    def now(self) -> str:
        return iso(self._env.now())

    def sleep(self, seconds):
        return self._env.sleep(seconds)

    def get_schedule(self, participant_ids, start, end):
        return self._env.get_schedule(participant_ids, start, end)

    def find_events(self, **filters):
        return self._env.find_events(**filters)

    def get_event(self, event_id):
        return self._env.get_event(event_id)

    def acquire_booking_lock(self, key):
        return self._env.acquire_booking_lock(key)

    def release_booking_lock(self, key):
        return self._env.release_booking_lock(key)

    def create_event(self, **kwargs):
        return self._env.create_event(**kwargs)

    def update_event(self, event_id, changes, notify_attendees=True):
        return self._env.update_event(event_id, changes, notify_attendees)

    def cancel_event(self, event_id, message=None):
        return self._env.cancel_event(event_id, message)

    def send_message(self, to, subject="", body="", channel="email"):
        return self._env.send_message(to, subject, body, channel)

    def create_online_meeting(self, **kwargs):
        return self._env.create_online_meeting(**kwargs)

    def create_hold(self, **kwargs):
        return self._env.create_hold(**kwargs)

    def publish(self, **kwargs):
        return self._env.publish(**kwargs)


def parse_retry_after(detail: str):
    m = RETRY_AFTER_RE.search(detail or "")
    return int(m.group(1)) if m else None


__all__ = ["SandboxEnvironment", "SandboxTools", "Directory", "WriteRecord", "ToolCall", "WRITE_TYPES",
           "parse_retry_after", "parse_ts"]
