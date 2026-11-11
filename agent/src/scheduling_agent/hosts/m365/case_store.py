"""Durable case state for chat hosts (INT-01, UX-05, LRN-01/07).

A case record holds the same `inputs` shape the core receives (candidate, plan, interviewers, conversation,
case_state, authority), so resuming a case a week later replays nothing and re-asks nothing. Personal
preferences are stored per recruiter, separately from cases, and never grant authority.

JsonFileCaseStore is for local development. Production should use a tenant-scoped store with retention and
deletion controls (for example Azure Cosmos DB) implementing the same three methods. Case data is product data:
it is never used to train or fine-tune models (INV-09).
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import threading
from typing import Protocol


class CaseStore(Protocol):
    def load(self, key: str) -> dict | None: ...
    def save(self, key: str, record: dict) -> None: ...
    def delete(self, key: str) -> None: ...


class JsonFileCaseStore:
    def __init__(self, root: str | None = None):
        self.root = pathlib.Path(root or os.environ.get("SCHEDULING_CASE_DIR", ".cases"))
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _path(self, key: str) -> pathlib.Path:
        return self.root / (hashlib.sha256(key.encode()).hexdigest()[:32] + ".json")

    def load(self, key: str) -> dict | None:
        p = self._path(key)
        if not p.exists():
            return None
        return json.loads(p.read_text(encoding="utf-8"))

    def save(self, key: str, record: dict) -> None:
        with self._lock:
            tmp = self._path(key).with_suffix(".tmp")
            tmp.write_text(json.dumps(record, default=str, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self._path(key))

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)
