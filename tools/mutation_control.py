"""Mutation controls: prove the new regression cases actually have teeth.

A guard that cannot fail is decoration. Each control here re-introduces the
*pre-fix* behaviour by patching the exact symbol the fix relies on, re-runs the
scenario, and requires the scenario to now go wrong - and requires proof that the
mutation itself took effect (otherwise a dead patch would "pass" too).

Five controls, one per reviewed defect:
  M1   validation ordering  -> wrong identity is accepted, result gets overwritten
  M2   blocking statuses    -> a new mode/holder launders an OUTCOME_UNKNOWN row
  M3   failure classification -> post-business failure becomes FAILED_RETRYABLE and
                                 the business step runs a second time
  M4a  required fields      -> caller and checkpoint both omit run_id/task_id; without
                               the gate the failure surfaces late from the store and
                               has already burned a resume attempt
  M4b  required bindings    -> a checkpoint that names no claim is tolerated and the
                               business step actually runs

usage: python tools/mutation_control.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
import uuid

PKG = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PKG, "src"))

import awake_kit.checkpoint as CK                  # noqa: E402
import awake_kit.claim as CL                       # noqa: E402
import awake_kit.recovery as RC                    # noqa: E402
from awake_kit.config import Config                # noqa: E402
from awake_kit.identity import build_identity      # noqa: E402
from awake_kit.paths import RootBoundary           # noqa: E402
from awake_kit.runner import TaskRunner            # noqa: E402


def root(tag):
    p = os.path.join(PKG, "work", "MUT_" + tag)
    if os.path.isdir(p):
        shutil.rmtree(p)
    return RootBoundary.create_exclusive(p)


def ident():
    return build_identity("RUN-SYNTHETIC-0001", "TASK-SYNTHETIC-0001",
                          str(uuid.uuid4()), "PREPARE").as_dict()


def prepared(tag, claim="K1", idem="RES-K1"):
    b = root(tag)
    cfg = Config.load()
    r = TaskRunner(b, config=cfg, identity=ident())
    r.prepare(claim)
    return b, cfg, r, claim, idem


def policy(b, r, cfg, claim, idem, result="state/RESULT.txt", **kw):
    return RC.RecoveryPolicy(boundary=b, store=r.store, config=cfg,
                             identity={**r.identity, "stage": "RESUME"},
                             claim_key=claim, idempotency_key=idem, result_rel=result,
                             owner=str(uuid.uuid4()), **kw)


def m1():
    """Pre-fix ordering: the checkpoint was consulted last, not first.

    With validation moved to the front, a wrong identity stops before the business
    step and before any artifact exists. Re-introducing the old ordering (validation
    effectively absent until the final COMMIT write) lets the wrong identity run the
    business and write the result - which is what this control must observe.
    """
    def attempt(tag, patched, wrong_identity):
        b, cfg, r, claim, idem = prepared(tag)
        calls = {"n": 0}
        original = CK.Checkpoint.validate
        if patched:
            CK.Checkpoint.validate = lambda self, **kw: {"checkpoint": "PRESENT",
                                                          "mutated": True}
        try:
            try:
                RC.RecoveryPolicy(boundary=b, store=r.store, config=cfg,
                                  identity=wrong_identity, claim_key=claim,
                                  idempotency_key=idem, result_rel="state/RESULT.txt",
                                  owner=str(uuid.uuid4())).resume(
                    lambda: (calls.__setitem__("n", calls["n"] + 1),
                             b"WRONG_IDENTITY_RESULT")[1])
                outcome = "PROCEEDED_TO_COMMIT"
            except Exception as e:
                outcome = f"RAISED:{type(e).__name__}:{getattr(e, 'reason', '')}"
        finally:
            CK.Checkpoint.validate = original
        res = {"identity_used": wrong_identity["run_id"],
               "business_calls": calls["n"],
               "result_written": b.exists("state/RESULT.txt"),
               "result_bytes": (len(b.read_bytes("state/RESULT.txt"))
                                if b.exists("state/RESULT.txt") else 0),
               "outcome": outcome,
               "validate_restored": CK.Checkpoint.validate is original}
        r.close()
        return res

    good_run, good_task = "RUN-SYNTHETIC-0001", "TASK-SYNTHETIC-0001"
    fixed = attempt("M1_fixed", patched=False,
                    wrong_identity={"run_id": "RUN-SYNTHETIC-9999", "task_id": good_task,
                                    "holder_id": str(uuid.uuid4()), "stage": "RESUME"})
    mutated = attempt("M1_mut", patched=True,
                      wrong_identity={"run_id": "RUN-SYNTHETIC-9999", "task_id": good_task,
                                      "holder_id": str(uuid.uuid4()), "stage": "RESUME"})
    return {"with_fix": fixed, "under_mutation": mutated,
            "mutation_took_effect": (mutated["business_calls"] == 1
                                     and mutated["result_written"] is True),
            "control_passed": (fixed["business_calls"] == 0 and fixed["result_written"] is False
                               and fixed["validate_restored"] and mutated["validate_restored"]
                               and mutated["business_calls"] == 1
                               and mutated["result_written"] is True)}


def m2():
    """Pre-fix laundering: blocking statuses were only consulted for same fingerprint."""
    b, cfg, r, claim, idem = prepared("M2")
    try:
        policy(b, r, cfg, claim, idem).resume(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    except Exception:
        pass
    row = r.store.current(claim)
    saved = CL.BLOCKING_STATUSES
    CL.BLOCKING_STATUSES = frozenset()
    mutated = CL.BLOCKING_STATUSES == frozenset()
    try:
        acquired_new_mode = r.store.reserve(claim_key=claim, identity=r.identity,
                                            mode="A_BRAND_NEW_MODE", owner=str(uuid.uuid4()),
                                            max_attempts=9)[0]
    finally:
        CL.BLOCKING_STATUSES = saved
    after = r.store.current(claim)
    r.close()
    return {"status_after_failure": row["status"], "mutation_took_effect": mutated,
            "restore_ok": CL.BLOCKING_STATUSES is saved,
            "acquired_under_mutation": acquired_new_mode,
            "status_now": after["status"],
            "control_passed": row["status"] == "OUTCOME_UNKNOWN" and acquired_new_mode is True}


def m3():
    """Pre-fix classification: every exception became FAILED_RETRYABLE."""
    b, cfg, r, claim, idem = prepared("M3")
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        raise RuntimeError("side effect happened, then failed")

    original_block = CL.ClaimStore.block
    downgraded = []

    def fake_block(self, claim_key, **kw):     # the defect being replaced
        downgraded.append(kw.get("kind"))
        self.conn.execute("UPDATE claims SET status='FAILED_RETRYABLE' WHERE claim_key=?",
                          (claim_key,))
        return {"blocked": False, "simulated": "FAILED_RETRYABLE"}

    CL.ClaimStore.block = fake_block
    mutated = CL.ClaimStore.block is fake_block
    try:
        try:
            policy(b, r, cfg, claim, idem).resume(flaky)
        except Exception:
            pass
        status_after_first = r.store.current(claim)["status"]
        try:
            policy(b, r, cfg, claim, idem).resume(flaky)
        except Exception:
            pass
    finally:
        CL.ClaimStore.block = original_block
    calls_after_second = calls["n"]
    r.close()
    return {"mutation_took_effect": mutated and bool(downgraded),
            "business_calls_under_mutation": calls_after_second,
            "status_after_first_failure": status_after_first,
            "restore_ok": CL.ClaimStore.block is original_block,
            "control_passed": calls_after_second == 2 and status_after_first == "FAILED_RETRYABLE"}


def m4():
    """Pre-fix hole: required fields were never checked, only compared.

    With both the caller and the checkpoint omitting run_id/task_id, a plain
    equality test reports None == None and lets the resume through. Neutering the
    two presence checks must therefore make the scenario PROCEED - that is what
    proves the new checks are load-bearing rather than decorative.
    """
    import json as _json

    def build(tag):
        b = root(tag)
        hand = {"schema": "awake_kit_checkpoint_v1", "stage": "PREPARE",
                "identity": {"holder_id": str(uuid.uuid4()), "stage": "PREPARE"},
                "bindings": {"claim_key": "K1"}, "writer_pid": os.getpid(),
                "facts": {}, "written_utc": "2020-01-01T00:00:00Z", "seq": 1}
        b.atomic_write("state/CHECKPOINT.json", _json.dumps(hand, indent=2))
        return b

    def attempt(tag, patched):
        b = build(tag)
        cfg = Config.load()
        r = TaskRunner(b, config=cfg, identity=ident())
        calls = {"n": 0}
        o_shape, o_recorded = CK._require_identity_shape, CK._check_recorded_identity
        if patched:
            CK._require_identity_shape = lambda identity, *, side: dict(identity or {})
            CK._check_recorded_identity = lambda recorded, *, recorded_stage: dict(recorded or {})
        try:
            try:
                RC.RecoveryPolicy(boundary=b, store=r.store, config=cfg,
                                  identity={"holder_id": str(uuid.uuid4()), "stage": "RESUME"},
                                  claim_key="K1", idempotency_key="RES-K1",
                                  result_rel="state/RESULT.txt",
                                  owner=str(uuid.uuid4())).resume(
                    lambda: (calls.__setitem__("n", calls["n"] + 1), b"UNSIGNED_RESUME_RESULT")[1])
                outcome = "PROCEEDED"
            except Exception as e:
                outcome = f"RAISED:{type(e).__name__}:{getattr(e, 'reason', '')}"
        finally:
            CK._require_identity_shape, CK._check_recorded_identity = o_shape, o_recorded
        res = {"outcome": outcome, "business_calls": calls["n"],
               "result_written": b.exists("state/RESULT.txt"),
               "resume_attempts_burned": (json.loads(b.read_bytes("state/RESUME_COUNTERS.json")
                                                    .decode("utf-8"))
                                          if b.exists("state/RESUME_COUNTERS.json")
                                          else {"resume_attempts": 0})["resume_attempts"],
               "checks_restored": (CK._require_identity_shape is o_shape
                                   and CK._check_recorded_identity is o_recorded)}
        r.close()
        return res

    fixed = attempt("M4_fixed", patched=False)
    mutated = attempt("M4_mut", patched=True)
    front_code = "RAISED:KitError:CALLER_IDENTITY_REQUIRED_FIELD_MISSING_RUN_ID"
    return {"with_fix": fixed, "under_mutation": mutated, "front_gate_code": front_code,
            "meaning": ("with the gate: named refusal, no lease attempt, no budget burned; "
                        "without it: the store trips later on its own guard and the resume "
                        "attempt is already spent"),
            "mutation_took_effect": (not mutated["outcome"].startswith(front_code)
                                     and mutated["resume_attempts_burned"] == 1),
            "control_passed": (fixed["outcome"].startswith(front_code)
                               and fixed["business_calls"] == 0 and not fixed["result_written"]
                               and fixed["resume_attempts_burned"] == 0
                               and not mutated["outcome"].startswith(front_code)
                               and mutated["resume_attempts_burned"] == 1
                               and fixed["checks_restored"] and mutated["checks_restored"])}


def m4b():
    """Pre-fix hole B: an undeclared claim binding was tolerated, so a resume could
    execute work it never identified. This one really does reach the business step."""

    def build(tag, bindings):
        b = root(tag)
        cfg = Config.load()
        r = TaskRunner(b, config=cfg, identity=ident())
        r.prepare("K1")
        doc = json.loads(b.read_bytes("state/CHECKPOINT.json").decode("utf-8"))
        doc["bindings"] = bindings
        doc.pop("_bytes", None)
        doc.pop("_sha256", None)
        b.atomic_write("state/CHECKPOINT.json", json.dumps(doc, indent=2))
        return b, cfg, r

    def run(tag, patched):
        b, cfg, r = build(tag, {})
        original = CK.Checkpoint.validate
        calls = {"n": 0}
        if patched:
            def without_required(self, **kw):
                kw["required_bindings"] = ()        # pre-fix: tolerate an undeclared claim
                return original(self, **kw)
            CK.Checkpoint.validate = without_required
        try:
            try:
                RC.RecoveryPolicy(boundary=b, store=r.store, config=cfg,
                                  identity={**r.identity, "stage": "RESUME"},
                                  claim_key="K1", idempotency_key="RES-K1",
                                  result_rel="state/RESULT.txt",
                                  owner=str(uuid.uuid4())).resume(
                    lambda: (calls.__setitem__("n", calls["n"] + 1), b"UNBOUND_RESUME_RESULT")[1])
                outcome = "PROCEEDED"
            except Exception as e:
                outcome = f"RAISED:{type(e).__name__}:{getattr(e, 'reason', '')}"
        finally:
            CK.Checkpoint.validate = original
        res = {"outcome": outcome, "business_calls": calls["n"],
               "result_written": b.exists("state/RESULT.txt"),
               "validate_restored": CK.Checkpoint.validate is original}
        r.close()
        return res

    fixed = run("M4b_fixed", patched=False)
    mutated = run("M4b_mut", patched=True)
    code = "RAISED:KitError:CHECKPOINT_BINDING_UNDECLARED_CLAIM_KEY"
    return {"with_fix": fixed, "under_mutation": mutated, "front_gate_code": code,
            "mutation_took_effect": (mutated["business_calls"] == 1
                                     and mutated["result_written"] is True),
            "control_passed": (fixed["outcome"] == code and fixed["business_calls"] == 0
                               and not fixed["result_written"]
                               and mutated["business_calls"] == 1 and mutated["result_written"]
                               and fixed["validate_restored"] and mutated["validate_restored"])}


def main():
    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    controls = {"M1_validation_ordering": m1(), "M2_blocking_status_laundering": m2(),
                "M3_failure_classification": m3(), "M4a_required_field_presence": m4(),
                "M4b_required_claim_binding": m4b()}
    doc = {"schema": "awake_kit_mutation_control_v1", "ran_utc": started,
            "meaning": "each control re-introduces the pre-fix behaviour and must now go WRONG; "
                       "a control that cannot go WRONG proves nothing about the fix",
            "controls": controls,
            "all_controls_passed": all(c["control_passed"] for c in controls.values())}
    out = os.path.join(PKG, "results", "MUTATION_CONTROLS.json")
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(doc, indent=2, ensure_ascii=False))
    print(json.dumps(doc, indent=2, ensure_ascii=False))
    return 0 if doc["all_controls_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
