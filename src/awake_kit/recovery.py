"""Bounded recovery, with validation ahead of every side effect.

Two things this module is careful about, because both are easy to get wrong and
both turn a "recovery" into a second execution:

**Gate order.** Validation is *read-only* and happens before anything that could
change state - before the checkpoint write, before the result is reused or
committed, before the lease is taken, and before the attempt counter moves:

    stop -> config identity -> deadline -> checkpoint/claim validation ->
    attempt budget (read) -> live writer -> [only now] reserve + reuse-or-execute

A wrong `run_id`/`task_id`, a mismatched `claim_key`/`idempotency_key` binding, or
a missing required checkpoint therefore produces **zero** business calls and leaves
the existing result and checkpoint byte-unchanged. A legitimate holder change is
not a task change: the holder is compared by the lease, never by the checkpoint.

**Failure kind.** A failure *before* the business step ran has no side effects and
is recorded as `FAILED_RETRYABLE`. A failure at or after the business step started
is recorded as `OUTCOME_UNKNOWN` / `BLOCKED` - persistently, with an evidence
pointer - because the effect may already exist. Blocking statuses cannot be escaped
by switching holder, mode, or fingerprint, and cannot be cleared except through
`ClaimStore.resolve_blocked`, which demands a named decision and a non-empty
evidence reference.
"""

from __future__ import annotations

import json
import time
import uuid

from .checkpoint import Checkpoint
from .claim import BLOCKING_STATUSES, IN_FLIGHT_STATUSES
from .commit import ResultCommitter
from .errors import (BudgetExhausted, ConfigIdentityMismatch, KitError, LiveWriterPresent,
                     StoppedError)
from .stop import StopSignal


def _now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class RecoveryPolicy:
    def __init__(self, *, boundary, store, config, identity: dict, claim_key: str,
                 idempotency_key: str, result_rel: str, owner: str | None = None,
                 max_resume_attempts: int = 2, deadline_utc: str | None = None,
                 lease_seconds: int = 120, require_min_stage: str = "PREPARE"):
        self.b = boundary
        self.store = store
        self.config = config
        self.identity = identity
        self.claim_key = claim_key
        self.idempotency_key = idempotency_key
        self.result_rel = result_rel
        self.owner = owner or str(uuid.uuid4())
        self.max_resume_attempts = int(max_resume_attempts)
        self.deadline_utc = deadline_utc
        self.lease_seconds = int(lease_seconds)
        self.require_min_stage = require_min_stage
        self.stop = StopSignal(boundary)
        self.ck = Checkpoint(boundary)
        self.counters_rel = "state/RESUME_COUNTERS.json"

    # ---- counters ----
    def _counters(self) -> dict:
        if not self.b.exists(self.counters_rel):
            return {"resume_attempts": 0, "resumes_completed": 0, "reuses": 0,
                    "business_executions": 0}
        return json.loads(self.b.read_bytes(self.counters_rel).decode("utf-8"))

    def _save_counters(self, c: dict) -> None:
        self.b.atomic_write(self.counters_rel, json.dumps(c, indent=2, ensure_ascii=False))

    # ---- gates (read-only except for seeding the config marker) ----
    def precheck(self) -> dict:
        report = {"checked_utc": _now_iso(), "gates": {}}
        self.stop.gate("resume")
        report["gates"]["stop"] = "PASS"

        cfg_sha = self.config.sha256
        marker = "state/CONFIG_IDENTITY.json"
        if self.b.exists(marker):
            expected = json.loads(self.b.read_bytes(marker).decode("utf-8"))["config_sha256"]
            if expected != cfg_sha:
                raise ConfigIdentityMismatch("CONFIG_CHANGED_MID_RUN",
                                             config_sha256_started_with=expected,
                                             config_sha256_now=cfg_sha)
            report["gates"]["config_identity"] = "PASS_MATCHES_MARKER"
        else:
            self.b.atomic_write(marker, json.dumps({"config_sha256": cfg_sha,
                                                    "written_utc": _now_iso()}, indent=2))
            report["gates"]["config_identity"] = "PASS_MARKER_SEEDED"

        if self.deadline_utc:
            if _now_iso() > self.deadline_utc:
                raise BudgetExhausted("DEADLINE_PASSED", deadline_utc=self.deadline_utc,
                                      now_utc=_now_iso())
            report["gates"]["deadline"] = "PASS"
        else:
            report["gates"]["deadline"] = "NOT_CONFIGURED"

        # identity / stage / claim correspondence, BEFORE any reuse, commit, lease,
        # or counter movement. The stage being validated is the one this action would
        # write (COMMIT), so "already committed" is confirmed rather than assumed.
        # claim_key is required here: a resume that cannot name the claim it is
        # resuming must be refused, not waved through because the old checkpoint
        # "has no opinion". idempotency_key stays optional - a PREPARE checkpoint
        # legitimately declares it nowhere.
        report["gates"]["checkpoint"] = self.ck.validate(
            identity=self.identity, stage="COMMIT",
            bindings={"claim_key": self.claim_key, "idempotency_key": self.idempotency_key},
            require_min_stage=self.require_min_stage,
            required_bindings=("claim_key",))

        existing = self.store.current(self.claim_key)
        if existing:
            st = existing.get("status")
            if st in BLOCKING_STATUSES:
                raise KitError("CLAIM_IS_BLOCKED_NEEDS_EVIDENCED_RELEASE",
                               claim_key=self.claim_key, status=st,
                               cleared_only_by="ClaimStore.resolve_blocked(evidence_ref=...)",
                               identity_change_clears_it=False)
            if st in IN_FLIGHT_STATUSES and existing.get("lease_owner") != self.owner:
                raise KitError("CLAIM_IS_IN_FLIGHT_WITH_ANOTHER_WORKER",
                               claim_key=self.claim_key, status=st,
                               lease_owner=existing.get("lease_owner"))

        c = self._counters()
        if c["resume_attempts"] >= self.max_resume_attempts:
            raise BudgetExhausted("RESUME_ATTEMPT_BUDGET_SPENT",
                                  attempts=c["resume_attempts"],
                                  max_attempts=self.max_resume_attempts)
        report["gates"]["budget"] = f"PASS {c['resume_attempts']}/{self.max_resume_attempts}"

        if self.store.lease_is_live(self.claim_key):
            raise LiveWriterPresent("ANOTHER_WRITER HOLDS_AN_ACTIVE_LEASE",
                                    claim_key=self.claim_key,
                                    current=self.store.current(self.claim_key),
                                    policy="elapsed time does not grant the lock")
        report["gates"]["live_writer"] = "PASS_NO_ACTIVE_LEASE"
        return report

    # ---- the one public action ----
    def resume(self, business) -> dict:
        """``business`` is a zero-arg callable returning the result payload bytes.

        It runs only when no result exists yet, at most once, and only after every
        gate above has passed.
        """
        report = self.precheck()          # zero state movement on any refusal
        c = self._counters()
        c["resume_attempts"] += 1
        self._save_counters(c)

        acquired, rec = self.store.reserve(
            claim_key=self.claim_key, identity=self.identity, mode="RESUME",
            owner=self.owner, max_attempts=self.max_resume_attempts,
            lease_seconds=self.lease_seconds)

        if not acquired:
            reason = rec.get("refused_because", "UNKNOWN_REFUSAL")
            if reason == "TERMINAL_REUSE":
                existing = self.store.get_result(self.idempotency_key)
                c = self._counters()
                c["reuses"] += 1
                self._save_counters(c)
                self.store._log("RESUME_REUSE", self.claim_key, self.idempotency_key)
                return {"verdict": "REUSED_EXISTING_RESULT", "gate_report": report,
                        "lease": dict(rec), "existing_result": existing,
                        "business_executed": False,
                        "execution_count": (existing or {}).get("execution_count")}
            self.store._log("RESUME_REFUSED", self.claim_key, reason)
            if reason in ("LEASE_ACTIVE", "LIVE_WRITER"):
                raise LiveWriterPresent("CLAIM_REFUSED", claim_key=self.claim_key, detail=rec)
            raise KitError("RESUME_REFUSED", claim_key=self.claim_key, detail=rec)

        token = rec["lease_token"]
        business_started = False
        try:
            if self.b.exists(self.result_rel):
                payload = self.b.read_bytes(self.result_rel)
                committer = ResultCommitter(self.b, self.store)
                res = committer.commit(claim_key=self.claim_key,
                                       idempotency_key=self.idempotency_key,
                                       payload=payload, rel_path=self.result_rel)
                self.store.release(self.claim_key, token=token, reason="reused_existing_result")
                c = self._counters()
                c["reuses"] += 1
                c["resumes_completed"] += 1
                self._save_counters(c)
                return {"verdict": "REUSED_EXISTING_RESULT", "gate_report": report,
                        "commit": res, "business_executed": False}

            self.stop.gate("business")
            if self.ck.completed("COMMIT"):
                raise KitError("CHECKPOINT_SAYS_COMMITTED_BUT_RESULT_MISSING",
                               claim_key=self.claim_key)
            business_started = True
            payload = business()
            business_started = True      # stays set even if committing then fails
            committer = ResultCommitter(self.b, self.store)
            res = committer.commit(claim_key=self.claim_key,
                                   idempotency_key=self.idempotency_key,
                                   payload=payload, rel_path=self.result_rel)
            self.ck.write("COMMIT", identity=self.identity, bindings=self._bindings(),
                          required_bindings=("claim_key",),
                          facts={"payload_sha256": res["payload_sha256"],
                                 "outcome": res["outcome"]})
            self.store.release(self.claim_key, token=token, reason="resumed_and_committed")
            c = self._counters()
            c["resumes_completed"] += 1
            c["business_executions"] += 1
            self._save_counters(c)
            return {"verdict": "RESUMED_AND_COMMITTED", "gate_report": report, "commit": res,
                    "business_executed": True, "execution_count": 1}
        except Exception as exc:
            self._record_failure(token=token, exc=exc, business_started=business_started)
            raise

    def _bindings(self) -> dict:
        return {"claim_key": self.claim_key, "idempotency_key": self.idempotency_key}

    def _record_failure(self, *, token: str, exc: Exception, business_started: bool) -> None:
        """Split "nothing happened" from "we cannot tell what happened"."""
        if not business_started:
            # Nothing before this point touched the outside world: safe to retry.
            try:
                self.store.mark(self.claim_key, "FAILED_RETRYABLE",
                                f"pre_business_error:{type(exc).__name__}", token=token)
            except KitError as inner:
                self.store._log("MARK_FAILED", self.claim_key, type(inner).__name__)
            return
        # The business step started, so its external effect is unknown: this is
        # never FAILED_RETRYABLE, whatever the exception type happens to be.
        kind = "OUTCOME_UNKNOWN"
        evidence_rel = f"state/BLOCKED.{_safe(self.claim_key)}.json"
        doc = {"schema": "awake_kit_blocked_v1", "kind": kind, "claim_key": self.claim_key,
               "idempotency_key": self.idempotency_key,
               "result_rel": self.result_rel,
               "result_exists_after_failure": self.b.exists(self.result_rel),
               "error_type": type(exc).__name__, "error": str(exc)[:300],
               "holder_id": self.identity.get("holder_id"),
               "written_utc": _now_iso(),
               "auto_policy": "business MUST NOT be re-invoked",
               "escape_hatches": {"new_holder": False, "new_mode": False,
                                  "new_fingerprint": False, "elapsed_time": False},
               "release_requires": "ClaimStore.resolve_blocked(decision=..., evidence_ref=...)"}
        try:
            self.b.open_exclusive(evidence_rel, json.dumps(doc, indent=2, ensure_ascii=False))
        except KitError:
            pass
        try:
            self.store.block(self.claim_key, token=token, kind=kind,
                             reason=f"post_business_error:{type(exc).__name__}",
                             evidence={**doc, "evidence_path": evidence_rel})
        except KitError as inner:
            # Token lost (e.g. the row was already re-tagged): still journal it, and
            # force the blocking status through the store so automation stops.
            self.store._log("BLOCK_TOKEN_LOST", self.claim_key,
                            f"{type(inner).__name__}:{evidence_rel}")
            self.store.conn.execute(
                "UPDATE claims SET status=?, detail=?, updated_at=? WHERE claim_key=?",
                (kind, json.dumps({"reason": "block_token_lost",
                                   "evidence_path": evidence_rel}, ensure_ascii=False),
                 _now_iso(), self.claim_key))


def _safe(s: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in s)[:40]
