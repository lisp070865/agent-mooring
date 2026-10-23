"""Stop flag. Consulted before any tool or business action, not just at entry."""

from __future__ import annotations

import json
import time

from .errors import StoppedError

REL = "state/STOP.json"


class StopSignal:
    def __init__(self, boundary, rel=REL):
        self.b = boundary
        self.rel = rel

    def set(self, reason: str, *, by: str = "operator") -> dict:
        payload = {"schema": "awake_kit_stop_v1", "stop": True, "reason": reason,
                   "by": by, "set_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        return self.b.atomic_write(self.rel, json.dumps(payload, indent=2, ensure_ascii=False))

    def read(self) -> dict | None:
        if not self.b.exists(self.rel):
            return None
        raw = self.b.read_bytes(self.rel)
        doc = json.loads(raw.decode("utf-8"))
        doc["_bytes"] = len(raw)
        return doc

    def is_set(self) -> bool:
        doc = self.read()
        return bool(doc) and doc.get("stop") is True

    def gate(self, action: str) -> None:
        """Raise if stopped. Call this *immediately before* every side effect."""
        if self.is_set():
            raise StoppedError("STOP_FLAG_SET", blocked_action=action, stop=self.read())
