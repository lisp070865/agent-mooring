"""Exclusive claim + lease + immutable results, in one SQLite guard.

Behaviours worth copying into your own system (and each is tested):

``reserve``
    ``BEGIN IMMEDIATE`` plus an upsert, so two processes cannot both believe they
    own the same unit of work. A row already ``DISPATCHING`` is **never** silently
    taken over - not while its lease is active, and not after it expires either,
    because an expired dispatch means external side effects are unknown and the
    correct answer is a human decision, not a blind second dispatch.

``commit_result``
    the first writer wins and later writers get ``REUSED``; the row and the file
    are both append-only, and an ``RAISE(ABORT)`` trigger makes an accidental
    UPDATE/DELETE impossible rather than merely discouraged.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import sqlite3
import time
import uuid

from .errors import ClaimConflict, ImmutableResultViolation

TERMINAL_STATUSES = frozenset({
    "RECORDED", "SUCCEEDED", "CANCELLED_BY_STOP", "ABANDONED_BY_OPERATOR",
})
#: Statuses meaning "an effect may already exist and the worker could not close it
#: out". They are deliberately fingerprint-blind: changing holder, mode, or
#: fingerprint must never make them ignorable, and leaving one requires evidence
#: (see resolve_blocked / mark's evidence_ref gate).
BLOCKING_STATUSES = frozenset({"BLOCKED_WAIT_USER", "OUTCOME_UNKNOWN", "BLOCKED", "DEADLETTER"})
#: Statuses meaning "a worker was handed this unit of work". A *new* dispatch is
#: refused, but the holder that put it there can still close it out with its own
#: lease token - otherwise a normal prepare->launch->release lifecycle would be
#: indistinguishable from a crash.
IN_FLIGHT_STATUSES = frozenset({"QUEUED", "LAUNCHED"})
RETRYABLE_STATUSES = frozenset({"FAILED_RETRYABLE", "VALIDATION_FAILED"})
LIVE_STATUSES = frozenset({"DISPATCHING"})
RELEASE_DECISIONS = ("RETRY_ALLOWED", "ALREADY_DELIVERED_RECONCILED", "TERMINAL_ABANDON")


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _iso(ts: _dt.datetime) -> str:
    return ts.isoformat(timespec="seconds")


def fingerprint_of(identity: dict, mode: str) -> str:
    blob = json.dumps({"identity": identity, "mode": mode}, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS claims(
  claim_key TEXT PRIMARY KEY,
  run_id TEXT, task_id TEXT, holder_id TEXT, stage TEXT, mode TEXT,
  status TEXT, fingerprint TEXT,
  attempts INTEGER DEFAULT 0 NOT NULL,
  next_retry_at TEXT,
  lease_owner TEXT, lease_until TEXT, lease_token TEXT, lease_generation INTEGER,
  first_seen TEXT, updated_at TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS results(
  claim_key TEXT PRIMARY KEY,
  idempotency_key TEXT UNIQUE,
  payload_sha256 TEXT, result_path TEXT,
  execution_count INTEGER NOT NULL DEFAULT 1,
  committed_at TEXT
);
CREATE TABLE IF NOT EXISTS journal(
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT, event TEXT, claim_key TEXT, detail TEXT
);
CREATE TRIGGER IF NOT EXISTS results_no_update BEFORE UPDATE ON results
  BEGIN SELECT RAISE(ABORT,'IMMUTABLE_RESULT'); END;
CREATE TRIGGER IF NOT EXISTS results_no_delete BEFORE DELETE ON results
  BEGIN SELECT RAISE(ABORT,'IMMUTABLE_RESULT'); END;
"""


class ClaimStore:
    def __init__(self, db_path: str):
        parent = os.path.dirname(os.path.abspath(db_path))
        os.makedirs(parent, exist_ok=True)
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=10000")
        self.conn.executescript(_SCHEMA)

    def close(self):
        self.conn.close()

    def _log(self, event, claim_key, detail=""):
        self.conn.execute("INSERT INTO journal(at,event,claim_key,detail) VALUES(?,?,?,?)",
                          (_iso(_now()), event, claim_key, detail))

    # ---- claim / lease ----
    def reserve(self, *, claim_key: str, identity: dict, mode: str, owner: str,
                max_attempts: int, lease_seconds: int = 120):
        """(acquired, record). Never takes over a DISPATCHING row."""
        fp = fingerprint_of(identity, mode)
        # Defense in depth: this store indexes these fields, so name the refusal
        # instead of surfacing a raw KeyError from a dict lookup.
        for name in ("run_id", "task_id", "holder_id", "stage"):
            value = identity.get(name) if isinstance(identity, dict) else None
            if not isinstance(value, str) or not value.strip():
                raise ClaimConflict(f"IDENTITY_REQUIRED_FIELD_MISSING_{name.upper()}",
                                    claim_key=claim_key, field=name, side="store")
        ts = _now()
        lease_until = _iso(ts + _dt.timedelta(seconds=lease_seconds))
        token = uuid.uuid4().hex
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute("SELECT * FROM claims WHERE claim_key=?", (claim_key,)).fetchone()
            if row:
                d = dict(row)
                status = d.get("status") or ""
                if status in LIVE_STATUSES:
                    if _lease_is_live(d, ts):
                        self.conn.execute("ROLLBACK")
                        return False, {**d, "refused_because": "LEASE_ACTIVE",
                                       "lease_owner": d.get("lease_owner")}
                    # Expired dispatch: side effects are unknown. Fail closed.
                    self.conn.execute("ROLLBACK")
                    return False, {**d, "refused_because": "LEASE_EXPIRED_NEEDS_OPERATOR",
                                   "external_side_effects": "UNKNOWN"}
                if status in BLOCKING_STATUSES:
                    # Deliberately checked BEFORE any fingerprint comparison: a new
                    # holder, a new mode, or a rewritten fingerprint must not launder
                    # a row whose external/business effect is unresolved.
                    self.conn.execute("ROLLBACK")
                    return False, {**d, "refused_because": "BLOCKED_NEEDS_EVIDENCED_RELEASE",
                                   "blocking_status": status,
                                   "cleared_only_by": "resolve_blocked(evidence_ref=...)",
                                   "identity_change_clears_it": False}
                if status in IN_FLIGHT_STATUSES:
                    self.conn.execute("ROLLBACK")
                    return False, {**d, "refused_because": "DISPATCH_IN_FLIGHT_NEEDS_CLOSER",
                                   "in_flight_status": status,
                                   "note": "the holder that launched this may still close it with "
                                           "its lease token; a new dispatch may not assume the "
                                           "worker is gone"}
                same = (d.get("fingerprint") or "") == fp and (d.get("mode") or "") == mode
                if same and status in TERMINAL_STATUSES:
                    self.conn.execute("ROLLBACK")
                    return False, {**d, "refused_because": "TERMINAL_REUSE"}
                prior_attempts = int(d.get("attempts") or 0)
                if same and status in RETRYABLE_STATUSES:
                    nr = d.get("next_retry_at")
                    if prior_attempts >= max_attempts or (nr and nr > _iso(ts)):
                        self.conn.execute("ROLLBACK")
                        return False, {**d, "refused_because": "RETRY_BUDGET_OR_BACKOFF"}
                attempts = prior_attempts + 1 if same else 1
                first_seen = d.get("first_seen") or _iso(ts)
                prev_gen = d.get("lease_generation")
                generation = (int(prev_gen) if prev_gen else 0) + 1
            else:
                attempts, first_seen, generation = 1, _iso(ts), 1

            self.conn.execute(
                """
                INSERT INTO claims(claim_key,run_id,task_id,holder_id,stage,status,first_seen,
                                   updated_at,detail,fingerprint,mode,attempts,next_retry_at,
                                   lease_owner,lease_until,lease_token,lease_generation)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(claim_key) DO UPDATE SET
                  run_id=excluded.run_id, task_id=excluded.task_id, holder_id=excluded.holder_id,
                  stage=excluded.stage, status='DISPATCHING', updated_at=excluded.updated_at,
                  detail=excluded.detail, fingerprint=excluded.fingerprint, mode=excluded.mode,
                  attempts=excluded.attempts, next_retry_at=NULL,
                  lease_owner=excluded.lease_owner, lease_until=excluded.lease_until,
                  lease_token=excluded.lease_token, lease_generation=excluded.lease_generation
                """,
                (claim_key, identity["run_id"], identity["task_id"], identity["holder_id"],
                 identity["stage"], "DISPATCHING", first_seen, _iso(ts), "atomic reservation",
                 fp, mode, attempts, None, owner, lease_until, token, generation),
            )
            self.conn.execute("COMMIT")
            self._log("RESERVE", claim_key, f"gen={generation} attempts={attempts}")
            return True, {"attempts": attempts, "lease_until": lease_until, "lease_owner": owner,
                          "lease_token": token, "lease_generation": generation,
                          "claim_key": claim_key}
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def mark(self, claim_key: str, status: str, detail: str = "", *, token: str,
             next_retry_at: str | None = None, evidence_ref: str | None = None):
        """Terminal/mark transitions are token-guarded; a stale worker cannot retag.

        Leaving a *blocking* status is not a plain tag change: it needs an explicit
        evidence reference, otherwise a crashed worker could quietly downgrade
        OUTCOME_UNKNOWN back to FAILED_RETRYABLE and un-block auto re-execution.
        """
        cur_row = self.conn.execute("SELECT status FROM claims WHERE claim_key=?",
                                    (claim_key,)).fetchone()
        cur_status = dict(cur_row)["status"] if cur_row else None
        if cur_status in BLOCKING_STATUSES and status not in BLOCKING_STATUSES:
            if not (evidence_ref or "").strip():
                raise ClaimConflict("BLOCKING_STATUS_REQUIRES_EVIDENCE_REF",
                                    claim_key=claim_key, current_status=cur_status,
                                    attempted_status=status,
                                    remedy="call resolve_blocked with a non-empty evidence_ref")
        cur = self.conn.execute(
            "UPDATE claims SET status=?, detail=?, updated_at=?, next_retry_at=? "
            "WHERE claim_key=? AND lease_token=?",
            (status, detail, _iso(_now()), next_retry_at, claim_key, token))
        if cur.rowcount != 1:
            raise ClaimConflict("LEASE_TOKEN_MISMATCH", claim_key=claim_key, token=token[:8])
        self._log("MARK", claim_key, status)

    def release(self, claim_key: str, *, token: str, reason: str, status: str = "RECORDED"):
        self.mark(claim_key, status, f"released:{reason}", token=token)

    def current(self, claim_key: str):
        row = self.conn.execute("SELECT * FROM claims WHERE claim_key=?", (claim_key,)).fetchone()
        return dict(row) if row else None

    def lease_is_live(self, claim_key: str) -> bool:
        d = self.current(claim_key)
        return bool(d) and (d.get("status") in LIVE_STATUSES) and _lease_is_live(d, _now())

    def block(self, claim_key: str, *, token: str, kind: str, reason: str,
              evidence: dict | None = None):
        """Record a stop that is NOT a retry invitation.

        ``kind`` OUTCOME_UNKNOWN means the business step may already have had an
        effect; ``BLOCKED`` means it needs a user/decision. Both are blocking
        statuses, so no later holder/mode/fingerprint can dispatch past them.
        """
        if kind not in ("OUTCOME_UNKNOWN", "BLOCKED"):
            raise ClaimConflict("BLOCK_KIND_INVALID", claim_key=claim_key, kind=kind,
                                allowed=["OUTCOME_UNKNOWN", "BLOCKED"])
        detail = json.dumps({"reason": reason, "evidence": evidence or {}}, ensure_ascii=False)
        cur = self.conn.execute(
            "UPDATE claims SET status=?, detail=?, updated_at=? WHERE claim_key=? AND lease_token=?",
            (kind, detail, _iso(_now()), claim_key, token))
        if cur.rowcount != 1:
            row = self.conn.execute("SELECT status FROM claims WHERE claim_key=?",
                                    (claim_key,)).fetchone()
            if row and dict(row)["status"] in (kind,) :
                return {"blocked": False, "reason": "ALREADY_BLOCKED", "status": kind}
            raise ClaimConflict("BLOCK_TOKEN_MISMATCH", claim_key=claim_key, token=token[:8])
        self._log("BLOCK", claim_key, kind)
        return {"blocked": True, "status": kind, "reason": reason}

    def resolve_blocked(self, claim_key: str, *, evidence_ref: str, decision: str,
                        cleared_by: str):
        """The only supported way out of a blocking status: explicit, evidenced, named.

        Deliberately does not accept a lease token (the crashed worker may never
        have recorded one) but does require a non-empty evidence reference and one
        of three named decisions, and it writes a journal row.
        """
        row = self.conn.execute("SELECT * FROM claims WHERE claim_key=?", (claim_key,)).fetchone()
        if not row:
            raise ClaimConflict("CLAIM_NOT_FOUND", claim_key=claim_key)
        d = dict(row)
        if d.get("status") not in BLOCKING_STATUSES:
            raise ClaimConflict("NOT_IN_BLOCKING_STATUS", claim_key=claim_key,
                                status=d.get("status"))
        if not (evidence_ref or "").strip() or len(evidence_ref.strip()) < 8:
            raise ClaimConflict("EVIDENCE_REF_REQUIRED", claim_key=claim_key,
                                rule="non-empty, >=8 chars, must point at a real artifact")
        if decision not in RELEASE_DECISIONS:
            raise ClaimConflict("DECISION_NOT_NAMED", claim_key=claim_key,
                                attempted=decision, allowed=list(RELEASE_DECISIONS))
        target = {"RETRY_ALLOWED": "FAILED_RETRYABLE",
                  "ALREADY_DELIVERED_RECONCILED": "RECORDED",
                  "TERMINAL_ABANDON": "ABANDONED_BY_OPERATOR"}[decision]
        self.conn.execute(
            "UPDATE claims SET status=?, detail=?, updated_at=? WHERE claim_key=?",
            (target, json.dumps({"resolved_from": d.get("status"), "decision": decision,
                                 "evidence_ref": evidence_ref.strip(), "cleared_by": cleared_by},
                                ensure_ascii=False), _iso(_now()), claim_key))
        self._log("RESOLVE_BLOCKED", claim_key, f"{d.get('status')}->{target} via {evidence_ref[:24]}")
        return {"resolved": True, "from": d.get("status"), "to": target, "decision": decision,
                "evidence_ref": evidence_ref.strip(), "cleared_by": cleared_by}

    def force_open_for_operator(self, claim_key: str, *, token: str, reason: str):
        """Only path that clears an expired DISPATCHING row. Requires the lease token."""
        cur = self.conn.execute(
            "UPDATE claims SET status='FAILED_RETRYABLE', detail=?, updated_at=? "
            "WHERE claim_key=? AND lease_token=? AND status='DISPATCHING'",
            (f"operator_recovery:{reason}", _iso(_now()), claim_key, token))
        if cur.rowcount != 1:
            raise ClaimConflict("OPERATOR_RECOVERY_REQUIRES_TOKEN", claim_key=claim_key)
        self._log("OPERATOR_RECOVERY", claim_key, reason)
        return True

    # ---- result ----
    def commit_result(self, claim_key: str, idempotency_key: str, payload: bytes, result_path: str):
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        sha = hashlib.sha256(payload).hexdigest()
        now = _iso(_now())
        try:
            self.conn.execute(
                "INSERT INTO results(claim_key,idempotency_key,payload_sha256,result_path,"
                "execution_count,committed_at) VALUES(?,?,?,?,1,?)",
                (claim_key, idempotency_key, sha, result_path, now))
        except sqlite3.IntegrityError as exc:
            if "IMMUTABLE_RESULT" in str(exc):
                raise ImmutableResultViolation("RESULT_ROW_IS_FROZEN", claim_key=claim_key) from exc
            existing = self.conn.execute(
                "SELECT * FROM results WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            return {"committed": False, "reason": "IDEMPOTENCY_KEY_EXISTS",
                    "existing_sha256": dict(existing)["payload_sha256"] if existing else None,
                    "execution_count": dict(existing)["execution_count"] if existing else None}
        self._log("COMMIT_RESULT", claim_key, sha[:12])
        return {"committed": True, "payload_sha256": sha, "execution_count": 1,
                "committed_at": now, "result_path": result_path}

    def get_result(self, idempotency_key: str):
        row = self.conn.execute("SELECT * FROM results WHERE idempotency_key=?",
                                (idempotency_key,)).fetchone()
        return dict(row) if row else None

    def events(self, limit: int = 100):
        rows = self.conn.execute("SELECT * FROM journal ORDER BY seq DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def counts(self):
        out = {}
        for table in ("claims", "results", "journal"):
            out[table] = self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        return out


def _lease_is_live(row: dict, ts: _dt.datetime) -> bool:
    lu = row.get("lease_until")
    if not lu:
        return False
    try:
        return _dt.datetime.fromisoformat(lu) > ts
    except ValueError:
        return False
