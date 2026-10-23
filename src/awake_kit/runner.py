"""Tiny orchestration showing the gates in order, for examples and tests.

Not a daemon, not a scheduler: a demonstration that prepare -> controlled child
fault -> resume-from-checkpoint -> commit-once can be expressed in ~100 lines of
stdlib with no network and no privileged access.
"""

from __future__ import annotations

import json
import os
import time
import uuid

from .checkpoint import Checkpoint
from .claim import ClaimStore, fingerprint_of
from .errors import KitError
from .identity import build_identity
from .recovery import RecoveryPolicy
from .stop import StopSignal


def _now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def synthetic_identity(stage: str = "PREPARE") -> dict:
    """A clearly-synthetic identity: uuid4 holder, ASCII tokens only."""
    ident = build_identity(run_id="RUN-SYNTHETIC-0001", task_id="TASK-SYNTHETIC-0001",
                           holder_id=str(uuid.uuid4()), stage=stage,
                           self_reported=True, provenance="UNVERIFIED_SELF_REPORT")
    return ident.as_dict()


class TaskRunner:
    def __init__(self, boundary, db_rel="state/guard.sqlite", *, config=None, identity=None):
        self.b = boundary
        self.store = ClaimStore(boundary.resolve(db_rel))
        self.config = config
        self.identity = identity or synthetic_identity()
        self.ck = Checkpoint(boundary)
        self.stop = StopSignal(boundary)

    def close(self):
        self.store.close()

    # ---- phase 1 ----
    def prepare(self, claim_key: str, mode: str = "PREPARE", max_attempts: int = 2) -> dict:
        self.stop.gate("prepare")
        ident = {**self.identity, "stage": "PREPARE"}
        acquired, rec = self.store.reserve(claim_key=claim_key, identity=ident, mode=mode,
                                           owner=self.identity["holder_id"],
                                           max_attempts=max_attempts)
        if not acquired:
            return {"verdict": "PREPARE_NOT_ACQUIRED", "refused_because":
                    rec.get("refused_because"), "existing": rec}
        self.ck.write("PREPARE", identity=ident, bindings={"claim_key": claim_key},
                      required_bindings=("claim_key",),
                      facts={"claim_key": claim_key, "mode": mode})
        self.store.mark(claim_key, "LAUNCHED", "prepared", token=rec["lease_token"])
        self.store.release(claim_key, token=rec["lease_token"], reason="prepare_complete")
        return {"verdict": "PREPARED", "claim_key": claim_key, "lease": rec,
                "checkpoint": self.ck.read()}

    # ---- phase 2: the child that faults on purpose ----
    def fault_child_entry(self, claim_key: str, reserved_exit_code: int = 43) -> int:
        """Runs inside a *child* process: checkpoint durably, then exit non-zero.

        The result file must not exist when this exits; the assertion below makes
        that a hard requirement rather than a hope.
        """
        self.ck.write("FAULT", identity={**self.identity, "stage": "FAULT"},
                      bindings={"claim_key": claim_key},
                      required_bindings=("claim_key",),
                      facts={"reserved_exit_code": reserved_exit_code,
                             "written_by_pid": os.getpid()})
        marker = {"schema": "awake_kit_fault_v1", "planned_exit_code": reserved_exit_code,
                  "written_by_pid": os.getpid(), "write_utc": _now_iso(),
                  "claim_key": claim_key}
        self.b.atomic_write("state/FAULT.json", json.dumps(marker, indent=2))
        if self.b.exists("state/RESULT.txt"):
            raise KitError("RESULT_MUST_NOT_EXIST_BEFORE_RECOVERY", claim_key=claim_key)
        self.b.atomic_write("logs/CHILD_ASSERT_NO_RESULT.txt",
                            "RESULT.txt absent at child exit pid=%s %s\n"
                            % (os.getpid(), _now_iso()))
        return reserved_exit_code

    # ---- phase 3 ----
    def recover(self, claim_key: str, business, *, idempotency_key: str,
                result_rel: str = "state/RESULT.txt", max_resume_attempts: int = 2,
                deadline_utc: str | None = None) -> dict:
        policy = RecoveryPolicy(boundary=self.b, store=self.store, config=self.config,
                                identity={**self.identity, "stage": "RESUME"},
                                claim_key=claim_key, idempotency_key=idempotency_key,
                                result_rel=result_rel, owner=self.identity["holder_id"],
                                max_resume_attempts=max_resume_attempts,
                                deadline_utc=deadline_utc)
        return policy.resume(business)
