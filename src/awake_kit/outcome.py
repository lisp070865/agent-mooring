"""Intent-before-call, result classification, and the no-auto-resend rule.

The failure this exists for: a delivery call that times out is not a failure.
It is an *unknown*. A retry on an unknown produces two deliveries, and the second
one is the one nobody planned. So:

* the intent is written (O_EXCL, first-time only) **before** the call, with the
  exact payload hash it authorises;
* the result is classified ``SENT`` / ``REJECTED`` / ``OUTCOME_UNKNOWN`` and an
  unknown is a terminal state for automatic policy, not a retry trigger;
* only an explicit operator flag can authorise a second attempt, and even then
  the recommended path is reconcile-against-evidence, never blind resend.

A second, subtler rule: an accepted/2xx response is not "delivered". ``VERIFIED``
requires independent evidence (a read-back of the same object, hash-compared).
"""

from __future__ import annotations

import hashlib
import json
import time

from .errors import KitError, ResendForbidden

STATUSES = ("INTENT_RECORDED", "SENT", "DELIVERED_VERIFIED", "REJECTED", "OUTCOME_UNKNOWN")


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class Outcomes:
    def __init__(self, boundary):
        self.b = boundary

    def _rel(self, kind, intent_id):
        return f"state/intents/{kind}.{intent_id}.json"

    def record_intent(self, *, kind: str, intent_id: str, payload: bytes, target: str,
                      max_attempts: int = 1) -> dict:
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        doc = {
            "schema": "awake_kit_intent_v1", "kind": kind, "intent_id": intent_id,
            "target": target, "payload_sha256": hashlib.sha256(payload).hexdigest(),
            "payload_bytes": len(payload), "status": "INTENT_RECORDED",
            "attempt": 1, "max_attempts": max_attempts,
            "auto_resend_allowed": False, "recorded_utc": _now(),
        }
        blob = json.dumps(doc, indent=2, ensure_ascii=False)
        res = self.b.open_exclusive(self._rel(kind, intent_id), blob)
        if not res["created"]:
            existing = json.loads(self.b.read_bytes(res["path"]).decode("utf-8"))
            if existing["payload_sha256"] != doc["payload_sha256"]:
                raise KitError("INTENT_EXISTS_WITH_DIFFERENT_PAYLOAD",
                               intent_id=intent_id,
                               existing_sha256=existing["payload_sha256"],
                               wanted_sha256=doc["payload_sha256"])
            return {"created": False, "reused": True, "intent": existing}
        return {"created": True, "reused": False, "intent": doc}

    def classify(self, *, kind: str, intent_id: str, raw_result) -> dict:
        """raw_result: dict with optional provider_evidence / error fields."""
        intent = json.loads(self.b.read_bytes(self._rel(kind, intent_id)).decode("utf-8"))
        if isinstance(raw_result, dict) and raw_result.get("error"):
            err = str(raw_result["error"])
            if raw_result.get("provider_evidence"):
                status = "OUTCOME_UNKNOWN"
                reason = "ERROR_AFTER_POSSIBLE_DELIVERY"
            else:
                status = "REJECTED"
                reason = err
        elif isinstance(raw_result, dict) and raw_result.get("provider_evidence"):
            status = "SENT"
            reason = "PROVIDER_EVIDENCE_PRESENT"
        else:
            status = "OUTCOME_UNKNOWN"
            reason = "NO_PROVIDER_EVIDENCE_IN_RESPONSE"
        intent = {**intent, "status": status, "classified_reason": reason,
                  "raw_result": raw_result, "classified_utc": _now()}
        if status == "OUTCOME_UNKNOWN":
            intent["auto_resend_allowed"] = False
            intent["next_action"] = "RECONCILE_AGAINST_EVIDENCE_ONLY"
        self.b.atomic_write(self._rel(kind, intent_id), json.dumps(intent, indent=2, ensure_ascii=False))
        return intent

    def mark_verified(self, *, kind: str, intent_id: str, evidence_sha256: str) -> dict:
        intent = json.loads(self.b.read_bytes(self._rel(kind, intent_id)).decode("utf-8"))
        if intent["payload_sha256"] != evidence_sha256:
            raise KitError("EVIDENCE_HASH_MISMATCH", intent_id=intent_id,
                           intent_sha256=intent["payload_sha256"],
                           evidence_sha256=evidence_sha256)
        intent = {**intent, "status": "DELIVERED_VERIFIED", "verified_utc": _now(),
                  "verification_basis": "READBACK_HASH_EQUAL_TO_INTENT_PAYLOAD"}
        self.b.atomic_write(self._rel(kind, intent_id), json.dumps(intent, indent=2, ensure_ascii=False))
        return intent

    def guard_resend(self, *, kind: str, intent_id: str, operator_retry_authorised: bool = False) -> None:
        intent = json.loads(self.b.read_bytes(self._rel(kind, intent_id)).decode("utf-8"))
        if intent["status"] in ("SENT", "DELIVERED_VERIFIED"):
            raise ResendForbidden("ALREADY_SENT_OR_VERIFIED", intent_id=intent_id,
                                  status=intent["status"])
        if intent["status"] == "OUTCOME_UNKNOWN" and not operator_retry_authorised:
            raise ResendForbidden("UNKNOWN_OUTCOME_NEEDS_OPERATOR_DECISION",
                                  intent_id=intent_id,
                                  attempt=intent["attempt"],
                                  next_action=intent.get("next_action"))
        if intent["attempt"] >= intent["max_attempts"] and not operator_retry_authorised:
            raise ResendForbidden("ATTEMPT_BUDGET_SPENT", intent_id=intent_id,
                                  attempt=intent["attempt"], max_attempts=intent["max_attempts"])
