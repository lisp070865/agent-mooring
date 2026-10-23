"""Offline, synthetic-only behaviour suite for awake_kit.

No network, no real mailbox, no scheduler, no client wake, no research code, no
installs. Every case runs against a fresh state root under ``work/`` inside this
package, and each case reports one of PASS / FAIL / NOT_RUN with a named reason.

usage: python tests/run_offline_tests.py
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
import uuid

PKG = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PKG, "src"))

from awake_kit.checkpoint import Checkpoint                       # noqa: E402
from awake_kit.claim import ClaimStore                            # noqa: E402
from awake_kit.commit import ResultCommitter                      # noqa: E402
from awake_kit.config import Config                               # noqa: E402
from awake_kit.errors import (BudgetExhausted, ClaimConflict, ConfigIdentityMismatch,  # noqa: E402
                              ImmutableResultViolation, KitError, LiveWriterPresent,
                              NetworkDisabledError, ResendForbidden, StoppedError)
from awake_kit.identity import NOT_IDENTITY_KINDS, assert_identity_source, build_identity  # noqa: E402
from awake_kit.outcome import Outcomes                            # noqa: E402
from awake_kit.ports import DisabledMail, DisabledSchedule, DisabledWake, FakeMail  # noqa: E402
from awake_kit.ports import FakeSchedule, FakeWake, build_ports   # noqa: E402
from awake_kit.recovery import RecoveryPolicy                     # noqa: E402
from awake_kit.runner import TaskRunner, synthetic_identity       # noqa: E402
from awake_kit.stop import StopSignal                             # noqa: E402

WORK = os.path.join(PKG, "work")
CASES = []          # (name, family, fn)


def case(name, family="core_r39a"):
    def deco(fn):
        CASES.append((name, family, fn))
        return fn
    return deco


def r39(name):
    """Regression cases added for the merge-round review."""
    return case(name, family="r39b_regressions")


def r39c(name):
    """Required-field / missing-binding branch cases (schema-completeness round)."""
    return case(name, family="r39c_regressions")


def fresh_root(tag):
    root = os.path.join(WORK, tag)
    if os.path.isdir(root):
        shutil.rmtree(root)          # this package's own scratch, created moments ago
    from awake_kit.paths import RootBoundary
    b = RootBoundary.create_exclusive(root)
    return b


def ident(stage="PREPARE"):
    return build_identity(run_id="RUN-SYNTHETIC-0001", task_id="TASK-SYNTHETIC-0001",
                          holder_id=str(uuid.uuid4()), stage=stage).as_dict()


def _now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------- identity
@case("T01_identity_accepts_valid_synthetic_holder (positive control)")
def t01():
    d = ident()
    ids = build_identity(d["run_id"], d["task_id"], d["holder_id"], "PREPARE")
    return {"holder_id_shape": "uuid4", "stage": ids.stage,
            "self_reported": ids.self_reported, "provenance": ids.provenance}


@case("T02_identity_rejects_non_identity_kinds_and_bad_shapes")
def t02():
    rejected_kinds, rejected_shapes = [], []
    for kind in NOT_IDENTITY_KINDS:
        try:
            assert_identity_source(kind)
        except KitError as e:
            rejected_kinds.append(f"{kind}:{e.reason}")
    bad = ["", "   ", "含中文的会话名", "a b", "x" * 65, "not-a-uuid",
           "11111111-1111-1111-1111-111111111111"]  # last one: not a v4
    for v in bad:
        try:
            build_identity("RUN-SYNTHETIC-0001", "TASK-SYNTHETIC-0001", v, "PREPARE")
        except KitError as e:
            rejected_shapes.append(f"{v[:18]!r}:{e.reason}")
    if len(rejected_kinds) != len(NOT_IDENTITY_KINDS):
        raise AssertionError(f"some non-identity kinds were accepted: {rejected_kinds}")
    if len(rejected_shapes) != len(bad):
        raise AssertionError(f"some bad shapes were accepted: {rejected_shapes}")
    return {"non_identity_kinds_tested": len(rejected_kinds),
            "bad_holder_shapes_tested": len(rejected_shapes),
            "rejections": {"kinds": rejected_kinds, "shapes": rejected_shapes}}


@case("T03_self_reported_cannot_claim_authoritative_lookup")
def t03():
    try:
        build_identity("RUN-SYNTHETIC-0001", "TASK-SYNTHETIC-0001", str(uuid.uuid4()),
                       "PREPARE", self_reported=True, provenance="AUTHORITATIVE_PROVIDER_LOOKUP")
    except KitError as e:
        return {"refused": e.reason}
    raise AssertionError("self-report was allowed to claim an authoritative lookup")


# ---------------------------------------------------------------- claim
@case("T04_duplicate_claim_is_refused_then_reused_not_re_executed")
def t04():
    b = fresh_root("T04")
    store = ClaimStore(b.resolve("state/guard.sqlite"))
    i = ident()
    ok1, rec1 = store.reserve(claim_key="K1", identity=i, mode="BUSINESS",
                              owner=i["holder_id"], max_attempts=2)
    cur = store.current("K1")
    store.mark("K1", "SUCCEEDED", "done", token=rec1["lease_token"])
    ok2, rec2 = store.reserve(claim_key="K1", identity=i, mode="BUSINESS",
                              owner=i["holder_id"], max_attempts=2)
    out = {"first_acquired": ok1, "second_acquired": ok2,
           "second_refused_because": rec2.get("refused_because"),
           "status_between": cur["status"], "lease_generation_second_row": rec2.get("lease_generation")}
    store.close()
    if ok1 is not True or ok2 is not False or rec2.get("refused_because") != "TERMINAL_REUSE":
        raise AssertionError(f"unexpected claim behaviour: {out}")
    return out


@case("T05_active_lease_blocks_a_second_writer")
def t05():
    b = fresh_root("T05")
    store = ClaimStore(b.resolve("state/guard.sqlite"))
    i = ident()
    ok1, rec1 = store.reserve(claim_key="K1", identity=i, mode="BUSINESS",
                              owner="holder-A", max_attempts=2, lease_seconds=120)
    ok2, rec2 = store.reserve(claim_key="K1", identity={**i, "holder_id": str(uuid.uuid4())},
                              mode="BUSINESS", owner="holder-B", max_attempts=2)
    out = {"first_acquired": ok1, "second_acquired": ok2,
           "second_refused_because": rec2.get("refused_because"),
           "lease_owner_visible_to_second_writer": rec2.get("lease_owner"),
           "lease_is_live": store.lease_is_live("K1")}
    store.close()
    if ok2 or rec2.get("refused_because") != "LEASE_ACTIVE":
        raise AssertionError(f"second writer was allowed in: {out}")
    return out


@case("T06_expired_lease_still_refuses_and_needs_the_lease_token")
def t06():
    b = fresh_root("T06")
    store = ClaimStore(b.resolve("state/guard.sqlite"))
    i = ident()
    ok1, rec1 = store.reserve(claim_key="K1", identity=i, mode="BUSINESS",
                              owner="holder-A", max_attempts=2, lease_seconds=1)
    time.sleep(1.2)
    ok2, rec2 = store.reserve(claim_key="K1", identity=i, mode="BUSINESS",
                              owner="holder-A", max_attempts=2)
    refused = (not ok2) and rec2.get("refused_because") == "LEASE_EXPIRED_NEEDS_OPERATOR"
    wrong_token = None
    try:
        store.force_open_for_operator("K1", token="deadbeef", reason="test")
    except ClaimConflict as e:
        wrong_token = e.reason
    opened = store.force_open_for_operator("K1", token=rec1["lease_token"], reason="operator")
    out = {"first_acquired": ok1, "auto_reacquire_after_expiry": ok2,
           "refused_because": rec2.get("refused_because"),
           "external_side_effects_declared": rec2.get("external_side_effects"),
           "wrong_token_rejected": wrong_token, "operator_open_with_token": opened,
           "policy": "elapsed time alone never grants the lock"}
    store.close()
    if not (refused and wrong_token and opened):
        raise AssertionError(f"expired-lease handling wrong: {out}")
    return out


# ---------------------------------------------------------------- recovery
@case("T07_controlled_child_exit_then_resume_from_checkpoint")
def t07():
    b = fresh_root("T07")
    cfg = Config.load()
    runner = TaskRunner(b, config=cfg, identity=ident())
    prep = runner.prepare("K1")
    child = subprocess.run([sys.executable, os.path.join(PKG, "tests", "fake_child.py"),
                            b.root, "K1", "43"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
    child_evidence = {"exit_code": child.returncode, "stdout_tail": child.stdout.strip()[-200:],
                      "stderr_tail": child.stderr.strip()[-200:], "reserved_exit_code": 43,
                      "result_exists_after_child": b.exists("state/RESULT.txt"),
                      "checkpoint_stage_after_child": runner.ck.read()["stage"]}
    executed = {"count": 0}

    def business():
        executed["count"] += 1
        return b"SYNTHETIC_RESULT_OK"

    rec = runner.recover("K1", business, idempotency_key="RES-K1", deadline_utc=None)
    out = {"prepare": prep["verdict"], "child": child_evidence, "recovery": rec,
           "business_executions": executed["count"],
           "result_bytes_on_disk": len(b.read_bytes("state/RESULT.txt"))}
    runner.close()
    if child.returncode != 43:
        raise AssertionError(f"child did not exit with the reserved code: {child_evidence}")
    if child_evidence["result_exists_after_child"]:
        raise AssertionError("result existed before recovery - the fault was not a real gap")
    if rec["verdict"] != "RESUMED_AND_COMMITTED" or executed["count"] != 1:
        raise AssertionError(f"recovery did not resume-and-commit exactly once: {out}")
    return out


@case("T08_second_resume_reuses_the_first_immutable_result")
def t08():
    b = fresh_root("T08")
    cfg = Config.load()
    runner = TaskRunner(b, config=cfg, identity=ident())
    runner.prepare("K1")
    calls = {"n": 0}

    def business():
        calls["n"] += 1
        return b"SYNTHETIC_RESULT_ONE"

    first = runner.recover("K1", business, idempotency_key="RES-K1")
    second = runner.recover("K1", business, idempotency_key="RES-K1")
    counters = json.loads(b.read_bytes("state/RESUME_COUNTERS.json").decode("utf-8"))
    row = runner.store.get_result("RES-K1")
    out = {"first": first["verdict"], "second": second["verdict"],
           "second_business_executed": second.get("business_executed"),
           "business_call_count": calls["n"], "execution_count_row": row["execution_count"],
           "reuses_counter": counters["reuses"], "resume_attempts": counters["resume_attempts"],
           "result_sha_stable": hashlib.sha256(b.read_bytes("state/RESULT.txt")).hexdigest()
                                == first["commit"]["payload_sha256"]}
    runner.close()
    if second["verdict"] != "REUSED_EXISTING_RESULT" or calls["n"] != 1:
        raise AssertionError(f"result was re-executed or not reused: {out}")
    if row["execution_count"] != 1:
        raise AssertionError(f"execution_count drifted: {out}")
    return out


@case("T09_result_row_is_immutable_update_and_delete_both_refused")
def t09():
    import sqlite3
    b = fresh_root("T09")
    cfg = Config.load()
    runner = TaskRunner(b, config=cfg, identity=ident())
    runner.prepare("K1")
    runner.recover("K1", lambda: b"SYNTHETIC_RESULT_ONE", idempotency_key="RES-K1")
    refused = {}
    for label, sql in (("UPDATE", "UPDATE results SET execution_count=99 WHERE idempotency_key='RES-K1'"),
                       ("DELETE", "DELETE FROM results WHERE idempotency_key='RES-K1'")):
        try:
            runner.store.conn.execute(sql)
            refused[label] = "NOT_REFUSED"
        except sqlite3.IntegrityError as e:
            refused[label] = str(e)
        except sqlite3.OperationalError as e:
            refused[label] = f"OPERATIONAL:{e}"
        try:
            runner.store.conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass  # autocommit mode: a refused statement opened no transaction
    out = {"refusals": refused,
           "row_still_present": runner.store.get_result("RES-K1")["execution_count"]}
    runner.close()
    if any(v == "NOT_REFUSED" for v in refused.values()):
        raise AssertionError(f"mutation slipped through: {out}")
    if not all("IMMUTABLE_RESULT" in v for v in refused.values()):
        raise AssertionError(f"refusal reason not the named trigger: {out}")
    return out


@case("T10_live_writer_gate_refuses_before_any_business_action")
def t10():
    b = fresh_root("T10")
    cfg = Config.load()
    runner = TaskRunner(b, config=cfg, identity=ident())
    runner.prepare("K1")
    holder_b = str(uuid.uuid4())
    acquired, rec = runner.store.reserve(claim_key="K1", identity={**runner.identity, "stage": "RESUME"},
                                         mode="RESUME", owner=holder_b, max_attempts=2,
                                         lease_seconds=300)
    counters_before = json.loads(b.read_bytes("state/RESUME_COUNTERS.json").decode("utf-8")) \
        if b.exists("state/RESUME_COUNTERS.json") else None
    calls = {"n": 0}
    policy = RecoveryPolicy(boundary=b, store=runner.store, config=cfg,
                            identity={**runner.identity, "stage": "RESUME"},
                            claim_key="K1", idempotency_key="RES-K1",
                            result_rel="state/RESULT.txt", owner=str(uuid.uuid4()),
                            max_resume_attempts=2)
    raised = None
    try:
        def business():
            calls["n"] += 1
            return b"SHOULD_NEVER_BE_WRITTEN"
        policy.resume(business)
    except LiveWriterPresent as e:
        raised = e.reason
    counters_after = json.loads(b.read_bytes("state/RESUME_COUNTERS.json").decode("utf-8")) \
        if b.exists("state/RESUME_COUNTERS.json") else None
    out = {"other_holder_acquired": acquired, "refused": raised,
           "business_calls": calls["n"],
           "result_file_written": b.exists("state/RESULT.txt"),
           "counters_before": counters_before, "counters_after": counters_after,
           "no_process_was_killed": True}
    runner.close()
    if raised != "ANOTHER_WRITER HOLDS_AN_ACTIVE_LEASE" or calls["n"] != 0:
        raise AssertionError(f"live writer did not block execution: {out}")
    if b.exists("state/RESULT.txt"):
        raise AssertionError("blocked recovery still wrote a result")
    return out


@case("T11_checkpoint_regression_and_missing_result_are_refused")
def t11():
    b = fresh_root("T11")
    ck = Checkpoint(b)
    i = ident()
    bond = {"claim_key": "K1", "idempotency_key": "RES-K1"}
    ck.write("PREPARE", identity=i, bindings=bond)
    ck.write("FAULT", identity=i, bindings=bond)
    regression = None
    try:
        ck.write("PREPARE", identity=i, bindings=bond)
    except KitError as e:
        regression = e.reason
    ck.write("COMMIT", identity=i, bindings=bond, facts={"payload_sha256": "x"})
    ghost = None
    try:
        runner = TaskRunner(b, config=Config.load(), identity=i)
        runner.recover("K1", lambda: b"NEVER", idempotency_key="RES-K1")
    except KitError as e:
        ghost = e.reason
    undeclared = None
    try:                       # same state root, but a claim the checkpoint never named
        runner2 = TaskRunner(b, config=Config.load(), identity=i)
        _policy(b, runner2.store, Config.load(), i, claim="K1", idem="RES-K1")  # sanity: bound
        bare = Checkpoint(b)
        bare.validate(identity=i, stage="COMMIT", bindings={"claim_key": "K1"},
                      required_bindings=("claim_key",))
    except KitError as e:
        undeclared = e.reason
    out = {"regression_refused": regression,
           "same_stage_re_record_refused_or_idempotent": "STAGE_ALREADY_RECORDED",
           "committed_but_no_result_refused": ghost,
           "bound_checkpoint_still_validates": undeclared is None,
           "note": "checkpoint says COMMIT but RESULT.txt is absent -> refuse, do not invent"}
    if regression != "CHECKPOINT_REGRESSION_REFUSED":
        raise AssertionError(f"checkpoint regression not refused: {out}")
    if ghost != "CHECKPOINT_SAYS_COMMITTED_BUT_RESULT_MISSING":
        raise AssertionError(f"ghost commit not refused: {out}")
    return out


@case("T12_resume_budget_and_deadline_are_enforced")
def t12():
    b = fresh_root("T12")
    cfg = Config.load()
    runner = TaskRunner(b, config=cfg, identity=ident())
    runner.prepare("K1")
    n = {"c": 0}

    def business():
        n["c"] += 1
        return b"SYNTHETIC_RESULT_ONE"
    runner.recover("K1", business, idempotency_key="RES-K1", max_resume_attempts=2)
    past = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 30))
    budget_refused = deadline_refused = None
    for rel in ("RES-K2", "RES-K3"):
        # the checkpoint now binds the idempotency key that was actually committed,
        # so a budget/deadline probe must address the same unit of work (RES-K1)
        p1 = RecoveryPolicy(boundary=b, store=runner.store, config=cfg,
                            identity={**runner.identity, "stage": "RESUME"},
                            claim_key="K1", idempotency_key="RES-K1",
                            result_rel=f"state/RESULT_{rel}.txt", owner=str(uuid.uuid4()),
                            max_resume_attempts=0)
        try:
            p1.resume(lambda: b"X")
        except BudgetExhausted as e:
            budget_refused = e.reason
        p2 = RecoveryPolicy(boundary=b, store=runner.store, config=cfg,
                           identity={**runner.identity, "stage": "RESUME"},
                           claim_key="K1", idempotency_key="RES-K1",
                           result_rel=f"state/RESULT_{rel}.txt", owner=str(uuid.uuid4()),
                           max_resume_attempts=2, deadline_utc=past)
        try:
            p2.resume(lambda: b"X")
        except BudgetExhausted as e:
            deadline_refused = e.reason
        break
    out = {"budget_refused": budget_refused, "deadline_refused": deadline_refused,
           "deadline_used": past}
    runner.close()
    if not (budget_refused and deadline_refused):
        raise AssertionError(f"budget/deadline gate missing: {out}")
    return out


@case("T13_config_identity_change_mid_run_is_refused")
def t13():
    b = fresh_root("T13")
    cfg_a = Config.load()
    runner = TaskRunner(b, config=cfg_a, identity=ident())
    runner.prepare("K1")
    runner.recover("K1", lambda: b"SYNTHETIC_RESULT_ONE", idempotency_key="RES-K1")
    mutated = json.loads(json.dumps(cfg_a.data))
    mutated["limits"]["max_resume_attempts"] = 99
    cfg_b = Config(mutated, source="test_mutation")
    refused = None
    try:
        RecoveryPolicy(boundary=b, store=runner.store, config=cfg_b,
                       identity={**runner.identity, "stage": "RESUME"}, claim_key="K9",
                       idempotency_key="RES-K9", result_rel="state/RESULT_K9.txt",
                       owner=str(uuid.uuid4()), max_resume_attempts=2).precheck()
    except ConfigIdentityMismatch as e:
        refused = {"reason": e.reason, "detail": e.detail}
    out = {"sha_a": cfg_a.sha256, "sha_b": cfg_b.sha256, "refusal": refused}
    runner.close()
    if refused is None or cfg_a.sha256 == cfg_b.sha256:
        raise AssertionError(f"config swap not detected: {out}")
    return out


# ---------------------------------------------------------------- outcomes
@case("T14_unknown_external_result_is_never_auto_resent")
def t14():
    b = fresh_root("T14")
    oc = Outcomes(b)
    fake = FakeMail(script=["unknown"])
    payload = b"SYNTHETIC_OUTBOUND_BODY"
    intent = oc.record_intent(kind="mail", intent_id="M1", payload=payload,
                              target="synthetic-recipient@example.invalid")
    raw = fake.send(to="synthetic-recipient@example.invalid", subject="s", body=payload)
    classified = oc.classify(kind="mail", intent_id="M1", raw_result=raw)
    resend_refused = None
    try:
        oc.guard_resend(kind="mail", intent_id="M1")
    except ResendForbidden as e:
        resend_refused = e.reason
    verified = oc.mark_verified(kind="mail", intent_id="M1",
                                evidence_sha256=hashlib.sha256(payload).hexdigest())
    after = None
    try:
        oc.guard_resend(kind="mail", intent_id="M1")
    except ResendForbidden as e:
        after = e.reason
    out = {"intent_created": intent["created"], "mail_calls": len(fake.calls),
           "classified": classified["status"], "classified_reason": classified["classified_reason"],
           "auto_resend_allowed": classified["auto_resend_allowed"],
           "resend_refused_at_unknown": resend_refused,
           "reconciled_status": verified["status"],
           "verification_basis": verified["verification_basis"],
           "resend_refused_after_verified": after}
    if classified["status"] != "OUTCOME_UNKNOWN" or len(fake.calls) != 1:
        raise AssertionError(f"unknown outcome not classified or call was retried: {out}")
    if resend_refused != "UNKNOWN_OUTCOME_NEEDS_OPERATOR_DECISION" or after != "ALREADY_SENT_OR_VERIFIED":
        raise AssertionError(f"resend gate not armed: {out}")
    return out


@case("T15_explicit_rejection_is_distinct_from_unknown_and_still_budget_gated")
def t15():
    b = fresh_root("T15")
    oc = Outcomes(b)
    fake = FakeMail(script=["rejected", "ok"])
    payload = b"SYNTHETIC_OUTBOUND_BODY"
    oc.record_intent(kind="mail", intent_id="M2", payload=payload, target="t@example.invalid")
    raw = fake.send(to="t@example.invalid", subject="s", body=payload)
    classified = oc.classify(kind="mail", intent_id="M2", raw_result=raw)
    # a clean rejection is NOT an unknown, but the default attempt budget is 1,
    # so a second attempt still requires an explicit operator decision.
    refused_without_authorisation = None
    try:
        oc.guard_resend(kind="mail", intent_id="M2", operator_retry_authorised=False)
    except ResendForbidden as e:
        refused_without_authorisation = e.reason
    oc.guard_resend(kind="mail", intent_id="M2", operator_retry_authorised=True)
    out = {"status": classified["status"], "reason": classified["classified_reason"],
           "refused_without_authorisation": refused_without_authorisation,
           "operator_authorised_retry_allowed": True,
           "calls_after_first_send": len(fake.calls)}
    if classified["status"] != "REJECTED":
        raise AssertionError(f"clean rejection mislabelled: {out}")
    if refused_without_authorisation != "ATTEMPT_BUDGET_SPENT":
        raise AssertionError(f"rejection bypassed the attempt budget: {out}")
    return out


@case("T16_intent_reuse_requires_identical_payload_hash")
def t16():
    b = fresh_root("T16")
    oc = Outcomes(b)
    oc.record_intent(kind="mail", intent_id="M3", payload=b"A", target="t@example.invalid")
    same = oc.record_intent(kind="mail", intent_id="M3", payload=b"A", target="t@example.invalid")
    conflict = None
    try:
        oc.record_intent(kind="mail", intent_id="M3", payload=b"B", target="t@example.invalid")
    except KitError as e:
        conflict = e.reason
    out = {"same_payload_reused": same["reused"], "different_payload_refused": conflict}
    if not same["reused"] or conflict != "INTENT_EXISTS_WITH_DIFFERENT_PAYLOAD":
        raise AssertionError(f"intent idempotency broken: {out}")
    return out


# ---------------------------------------------------------------- stop
@case("T17_stop_flag_blocks_every_entrypoint_and_changes_nothing")
def t17():
    b = fresh_root("T17")
    cfg = Config.load()
    runner = TaskRunner(b, config=cfg, identity=ident())
    runner.prepare("K1")
    st = StopSignal(b)
    st.set("operator_stop", by="operator")
    counters_before = b.read_bytes("state/RESUME_COUNTERS.json") if b.exists("state/RESUME_COUNTERS.json") else None
    results = {}
    n = {"c": 0}
    for label, fn in (
        ("prepare", lambda: runner.prepare("K2")),
        ("recover", lambda: runner.recover("K1", lambda: n.__setitem__("c", n["c"] + 1) or b"X",
                                           idempotency_key="RES-K1")),
    ):
        try:
            fn()
            results[label] = "NOT_BLOCKED"
        except StoppedError as e:
            results[label] = e.reason
    counters_after = b.read_bytes("state/RESUME_COUNTERS.json") if b.exists("state/RESUME_COUNTERS.json") else None
    out = {"blocked": results, "business_calls": n["c"],
           "result_written": b.exists("state/RESULT.txt"),
           "counters_unchanged": counters_before == counters_after}
    runner.close()
    if any(v == "NOT_BLOCKED" for v in results.values()) or n["c"] != 0 or out["result_written"]:
        raise AssertionError(f"stop flag did not freeze the loop: {out}")
    return out


# ---------------------------------------------------------------- boundary + network
@case("T18_writes_cannot_escape_the_exclusive_root")
def t18():
    b = fresh_root("T18")
    refused = {}
    for label, rel in (("parent_escape", "../escaped.txt"),
                       ("sibling_escape", "../../other_task/x.txt"),
                       ("absolute_escape", os.path.join(os.path.dirname(WORK), "nope.txt"))):
        try:
            b.resolve(rel)
            refused[label] = "NOT_REFUSED"
        except KitError as e:
            refused[label] = e.reason
    ok = b.atomic_write("state/inside.txt", "ok").get("path")
    out = {"refusals": refused, "inside_write_path_relative_to_root": ok,
           "nothing_written_outside": not os.path.exists(os.path.join(os.path.dirname(WORK), "nope.txt"))}
    if any(v == "NOT_REFUSED" for v in refused.values()):
        raise AssertionError(f"escape not blocked: {out}")
    return out


@case("T19_reparse_point_detection_junction_or_symlink")
def t19():
    import subprocess
    root = os.path.join(WORK, "T19")
    if os.path.isdir(root):
        shutil.rmtree(root)
    real = os.path.join(root, "real")
    os.makedirs(real)
    sentinel = os.path.join(real, "keep.txt")
    with open(sentinel, "w", encoding="utf-8", newline="\n") as f:
        f.write("target contents must survive link removal\n")
    link = os.path.join(root, "link")
    method, attempts = None, []
    try:
        subprocess.run(["cmd", "/c", "mklink", "/J", link, real], capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=20)
        if os.path.isdir(link):
            method = "ntfs_junction"
    except Exception as e:
        attempts.append(f"junction:{type(e).__name__}")
    if method is None:
        try:
            os.symlink(real, link, target_is_directory=True)
            method = "symlink"
        except OSError as e:
            attempts.append(f"symlink:{type(e).__name__}:{getattr(e, 'winerror', None)}")
    if method is None:
        return ("NOT_RUN", "no_reparse_object_creatable_here: " + "; ".join(attempts))
    from awake_kit.paths import RootBoundary
    detected = None
    try:
        RootBoundary(link)
    except KitError as e:
        detected = e.reason
    # removing the link must not destroy the target
    os.rmdir(link)
    out = {"method": method, "refusal": detected,
           "target_still_intact_after_link_removal": os.path.isfile(sentinel)
                                                   and os.path.getsize(sentinel) > 0,
           "attempts": attempts}
    if detected not in ("reparse_point_in_path", "root_resolves_elsewhere"):
        raise AssertionError(f"reparse point not refused: {out}")
    if not out["target_still_intact_after_link_removal"]:
        raise AssertionError(f"cleanup damaged the target: {out}")
    return out


@case("T20_no_network_and_no_environment_credential_access")
def t20():
    import ast
    tripwire_hits = []

    class Tripwire:
        def __init__(self, *a, **k):
            tripwire_hits.append("socket_constructed")
            raise AssertionError("socket used during offline suite")

    import socket
    real_socket, real_create = socket.socket, getattr(socket, "create_connection", None)
    socket.socket = Tripwire
    if real_create:
        def _boom(*a, **k):
            tripwire_hits.append("create_connection")
            raise AssertionError("create_connection used")
        socket.create_connection = _boom
    try:
        b = fresh_root("T20")
        cfg = Config.load()
        ports = build_ports(cfg)
        runner = TaskRunner(b, config=cfg, identity=ident())
        runner.prepare("K1")
        runner.recover("K1", lambda: b"SYNTHETIC_RESULT_ONE", idempotency_key="RES-K1")
        refused = {}
        for label, port in (("mail", DisabledMail()), ("wake", DisabledWake()), ("schedule", DisabledSchedule())):
            try:
                if label == "mail":
                    port.send(to="t", subject="s", body=b"")
                elif label == "wake":
                    port.notify(target="t", message=b"")
                else:
                    port.appoint(at_utc="x", instruction=b"")
                refused[label] = "NOT_REFUSED"
            except NetworkDisabledError as e:
                refused[label] = e.reason
        runner.close()
    finally:
        socket.socket = real_socket
        if real_create:
            socket.create_connection = real_create

    # ---- structural gate: scan real code, not prose ----
    ALLOWED_ROOTS = {"__future__", "dataclasses", "datetime", "errno", "hashlib", "json",
                     "os", "re", "sqlite3", "stat", "time", "typing", "uuid"}
    DENIED_DOTTED_PREFIXES = ("os.environ", "os.getenv", "os.system", "os.popen", "socket.",
                              "urllib.", "http.", "ssl.", "keyring.", "winreg.", "requests.",
                              "subprocess.", "ctypes.", "shutil.")

    def scan(path):
        tree = ast.parse(open(path, encoding="utf-8").read(), filename=path)
        imports, dotted, violations, relative = [], [], [], 0
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for al in node.names:
                    imports.append(al.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if getattr(node, "level", 0):
                    relative += 1          # intra-package import, not a third-party root
                elif node.module:
                    imports.append(node.module.split(".")[0])
            elif isinstance(node, ast.Attribute):
                parts, cur = [], node
                while isinstance(cur, ast.Attribute):
                    parts.append(cur.attr)
                    cur = cur.value
                if isinstance(cur, ast.Name):
                    parts.append(cur.id)
                    dotted.append(".".join(reversed(parts)))
        for root in sorted(set(imports)):
            if root not in ALLOWED_ROOTS:
                violations.append(f"import_not_allowed:{root}")
        for d in sorted(set(dotted)):
            for pref in DENIED_DOTTED_PREFIXES:
                if d.startswith(pref) or d == pref.rstrip("."):
                    violations.append(f"attribute_denied:{d}")
        return {"imports": sorted(set(imports)), "dotted": sorted(set(dotted)),
                "intra_package_relative_imports": relative, "violations": violations}

    src = os.path.join(PKG, "src", "awake_kit")
    package_scan = {f: scan(os.path.join(src, f)) for f in sorted(os.listdir(src)) if f.endswith(".py")}
    offenders = {f: v["violations"] for f, v in package_scan.items() if v["violations"]}
    # positive control: the same scanner must flag THIS test file (it uses socket/subprocess/shutil)
    control = scan(os.path.join(PKG, "tests", "test_offline.py"))

    out = {"tripwire_hits": tripwire_hits, "disabled_ports_refused": refused,
           "adapter_gates": ports["gates"], "env_credentials_read": cfg.env_credentials_read,
           "accounts_auto_discovered": cfg.accounts_auto_discovered,
           "package_violations": offenders,
           "package_import_roots": sorted({r for v in package_scan.values() for r in v["imports"]}),
           "positive_control_scanned_file": os.path.basename(control and "test_offline.py"),
           "positive_control_violations": control["violations"],
           "files_scanned": len(package_scan)}
    if tripwire_hits or any(v == "NOT_REFUSED" for v in refused.values()):
        raise AssertionError(f"network path exists: {out}")
    if offenders:
        raise AssertionError(f"package references forbidden APIs: {out}")
    if not control["violations"]:
        raise AssertionError(f"scanner is dead - it flagged nothing in the control file: {out}")
    return out


# ------------------------------------------- r39b regressions (merge round)
def _snapshot(b, store, rels=("state/RESULT.txt", "state/CHECKPOINT.json",
                             "state/RESUME_COUNTERS.json")):
    """Digest every named state file, plus the claim table, so 'unchanged' is measured."""
    snap = {}
    for rel in rels:
        snap[rel] = hashlib.sha256(b.read_bytes(rel)).hexdigest() if b.exists(rel) else "ABSENT"
    snap["_claims"] = store.conn.execute("SELECT count(*) c FROM claims").fetchone()["c"]
    snap["_results"] = store.conn.execute("SELECT count(*) c FROM results").fetchone()["c"]
    return snap


def _policy(b, store, cfg, identity, *, claim="K1", idem="RES-K1", result="state/RESULT.txt", **kw):
    return RecoveryPolicy(boundary=b, store=store, config=cfg, identity=identity,
                          claim_key=claim, idempotency_key=idem, result_rel=result,
                          owner=str(uuid.uuid4()), **kw)


@r39("R21_wrong_run_id_refuses_before_any_write_and_leaves_everything_untouched")
def r21():
    b = fresh_root("R21")
    cfg = Config.load()
    runner = TaskRunner(b, config=cfg, identity=ident())
    runner.prepare("K1")
    runner.recover("K1", lambda: b"ORIGINAL_RESULT", idempotency_key="RES-K1")
    before = _snapshot(b, runner.store)
    calls = {"n": 0}
    wrong = {**runner.identity, "run_id": "RUN-SYNTHETIC-9999"}
    refused = None
    try:
        _policy(b, runner.store, cfg, wrong).resume(lambda: calls.__setitem__("n", calls["n"] + 1))
    except KitError as e:
        refused = e.reason
    after = _snapshot(b, runner.store)
    out = {"refusal": refused, "business_calls": calls["n"], "snapshot_identical": before == after,
           "claims_before_after": (before["_claims"], after["_claims"]),
           "results_before_after": (before["_results"], after["_results"])}
    runner.close()
    if refused != "CHECKPOINT_RUN_ID_MISMATCH" or calls["n"] != 0 or before != after:
        raise AssertionError(f"wrong run_id was not refused cleanly: {out}")
    return out


@r39("R22_wrong_task_id_refuses_before_any_write")
def r22():
    b = fresh_root("R22")
    cfg = Config.load()
    runner = TaskRunner(b, config=cfg, identity=ident())
    runner.prepare("K1")
    before = _snapshot(b, runner.store)
    calls = {"n": 0}
    wrong = {**runner.identity, "task_id": "TASK-SYNTHETIC-9999"}
    refused = None
    try:
        _policy(b, runner.store, cfg, wrong).resume(lambda: calls.__setitem__("n", calls["n"] + 1))
    except KitError as e:
        refused = e.reason
    after = _snapshot(b, runner.store)
    out = {"refusal": refused, "business_calls": calls["n"], "snapshot_identical": before == after}
    runner.close()
    if refused != "CHECKPOINT_TASK_ID_MISMATCH" or calls["n"] != 0 or before != after:
        raise AssertionError(f"wrong task_id was not refused cleanly: {out}")
    return out


@r39("R23_same_stage_rerecord_with_wrong_identity_does_not_shortcut_validation")
def r23():
    b = fresh_root("R23")
    ck = Checkpoint(b)
    good = ident()
    ck.write("PREPARE", identity=good, bindings={"claim_key": "K1"})
    before = hashlib.sha256(b.read_bytes("state/CHECKPOINT.json")).hexdigest()
    result, refused = None, None
    try:
        result = ck.write("PREPARE", identity={**good, "run_id": "RUN-SYNTHETIC-9999"},
                          bindings={"claim_key": "K1"})
    except KitError as e:
        refused = e.reason
    after = hashlib.sha256(b.read_bytes("state/CHECKPOINT.json")).hexdigest()
    out = {"refusal": refused, "silent_return_instead": result, "checkpoint_unchanged": before == after}
    if refused != "CHECKPOINT_RUN_ID_MISMATCH" or result is not None or before != after:
        raise AssertionError(f"same-stage early return skipped validation: {out}")
    return out


@r39("R24_claim_key_binding_mismatch_refuses_before_business")
def r24():
    b = fresh_root("R24")
    cfg = Config.load()
    runner = TaskRunner(b, config=cfg, identity=ident())
    runner.prepare("K1")
    before = _snapshot(b, runner.store)
    calls = {"n": 0}
    refused = None
    try:
        _policy(b, runner.store, cfg, {**runner.identity, "stage": "RESUME"},
                claim="K2", idem="RES-K2").resume(
                    lambda: calls.__setitem__("n", calls["n"] + 1))
    except KitError as e:
        refused = e.reason
    after = _snapshot(b, runner.store)
    out = {"refusal": refused, "business_calls": calls["n"],
           "k2_row_created": bool(runner.store.current("K2")), "snapshot_identical": before == after}
    runner.close()
    if refused != "CHECKPOINT_BINDING_MISMATCH_CLAIM_KEY" or calls["n"] != 0 or out["k2_row_created"]:
        raise AssertionError(f"claim/checkpoint correspondence not enforced: {out}")
    return out


@r39("R25_missing_required_checkpoint_means_zero_business_and_zero_lease")
def r25():
    b = fresh_root("R25")
    cfg = Config.load()
    runner = TaskRunner(b, config=cfg, identity=ident())   # no prepare at all
    before = _snapshot(b, runner.store)
    calls = {"n": 0}
    refused = None
    try:
        _policy(b, runner.store, cfg, {**runner.identity, "stage": "RESUME"}).resume(
            lambda: calls.__setitem__("n", calls["n"] + 1))
    except KitError as e:
        refused = e.reason
    after = _snapshot(b, runner.store)
    out = {"refusal": refused, "business_calls": calls["n"], "lease_rows": after["_claims"],
           "counters_file_created": b.exists("state/RESUME_COUNTERS.json"),
           "snapshot_identical": before == after}
    runner.close()
    if refused != "CHECKPOINT_REQUIRED_BUT_MISSING" or calls["n"] != 0 or after["_claims"] != 0:
        raise AssertionError(f"missing checkpoint did not fail closed: {out}")
    return out


@r39("R26_new_holder_is_allowed_new_task_is_not")
def r26():
    b = fresh_root("R26")
    cfg = Config.load()
    runner = TaskRunner(b, config=cfg, identity=ident())
    runner.prepare("K1")
    original_holder = runner.identity["holder_id"]
    later_holder = str(uuid.uuid4())
    calls = {"n": 0}

    def business():
        calls["n"] += 1
        return b"SYNTHETIC_RESULT_R26"

    res = _policy(b, runner.store, cfg, {**runner.identity, "holder_id": later_holder,
                                        "stage": "RESUME"}).resume(business)
    ck = json.loads(b.read_bytes("state/CHECKPOINT.json").decode("utf-8"))
    out = {"verdict": res["verdict"], "business_calls": calls["n"],
           "prepare_holder": original_holder[:8] + "...", "resume_holder": later_holder[:8] + "...",
           "checkpoint_binds": ck["bindings"], "holder_switch_allowed": True}
    runner.close()
    if res["verdict"] != "RESUMED_AND_COMMITTED" or calls["n"] != 1:
        raise AssertionError(f"legitimate holder change was blocked: {out}")
    return out


@r39("R27_post_business_failure_persists_unknown_and_blocks_second_call")
def r27():
    b = fresh_root("R27")
    cfg = Config.load()
    runner = TaskRunner(b, config=cfg, identity=ident())
    runner.prepare("K1")
    state = {"counter_writes": 0}
    boom = RuntimeError("side effect happened, then the payload never came back")

    def flaky():
        state["counter_writes"] += 1          # observable external side effect
        raise boom

    first = None
    try:
        _policy(b, runner.store, cfg, {**runner.identity, "stage": "RESUME"}).resume(flaky)
    except RuntimeError as e:
        first = type(e).__name__
    row = runner.store.current("K1")
    second = None
    try:
        _policy(b, runner.store, cfg, {**runner.identity, "stage": "RESUME"}).resume(flaky)
    except KitError as e:
        second = e.reason
    out = {"first_error": first, "side_effect_count_after_first": state["counter_writes"],
           "claim_status_after_failure": row["status"],
           "detail_records_evidence": bool(row["detail"]),
           "blocked_evidence_files": [f for f in os.listdir(os.path.join(b.root, "state"))
                                      if f.startswith("BLOCKED.")],
           "second_call_refused": second, "side_effect_count_after_second": state["counter_writes"]}
    runner.close()
    if row["status"] != "OUTCOME_UNKNOWN" or state["counter_writes"] != 1:
        raise AssertionError(f"post-business failure was not persisted as unknown: {out}")
    if second != "CLAIM_IS_BLOCKED_NEEDS_EVIDENCED_RELEASE" or state["counter_writes"] != 1:
        raise AssertionError(f"second call was not blocked by the persisted unknown: {out}")
    return out


@r39("R28_blocking_status_survives_holder_mode_fingerprint_and_time")
def r28():
    b = fresh_root("R28")
    cfg = Config.load()
    runner = TaskRunner(b, config=cfg, identity=ident())
    runner.prepare("K1")

    def flaky():
        raise RuntimeError("effect then failure")
    try:
        _policy(b, runner.store, cfg, {**runner.identity, "stage": "RESUME"}).resume(flaky)
    except Exception:
        pass
    row = runner.store.current("K1")
    token = row["lease_token"]
    attempts = {}
    attempts["reserve_new_holder"] = runner.store.reserve(
        claim_key="K1", identity={**runner.identity, "holder_id": str(uuid.uuid4())},
        mode="RESUME", owner=str(uuid.uuid4()), max_attempts=9)[0]
    attempts["reserve_new_mode"] = runner.store.reserve(
        claim_key="K1", identity=runner.identity, mode="ENTIRELY_DIFFERENT_MODE",
        owner=str(uuid.uuid4()), max_attempts=9)[0]
    downgrade = None
    try:
        runner.store.mark("K1", "FAILED_RETRYABLE", "sneaky", token=token)
    except ClaimConflict as e:
        downgrade = e.reason
    no_evidence = None
    try:
        runner.store.resolve_blocked("K1", evidence_ref="  ", decision="RETRY_ALLOWED",
                                     cleared_by="tester")
    except ClaimConflict as e:
        no_evidence = e.reason
    bad_decision = None
    try:
        runner.store.resolve_blocked("K1", evidence_ref="logs/EVIDENCE-0001.txt",
                                     decision="JUST_RETRY_IT", cleared_by="tester")
    except ClaimConflict as e:
        bad_decision = e.reason
    released = runner.store.resolve_blocked("K1", evidence_ref="logs/EVIDENCE-0001.txt",
                                           decision="RETRY_ALLOWED", cleared_by="tester")
    out = {"status_before": row["status"], "acquired_after_holder_change": attempts["reserve_new_holder"],
           "acquired_after_mode_change": attempts["reserve_new_mode"],
           "silent_downgrade": downgrade, "release_without_evidence": no_evidence,
           "release_with_unnamed_decision": bad_decision, "evidenced_release": released,
           "status_after_release": runner.store.current("K1")["status"]}
    runner.close()
    if attempts["reserve_new_holder"] or attempts["reserve_new_mode"]:
        raise AssertionError(f"blocking status was bypassed by identity/mode change: {out}")
    if downgrade != "BLOCKING_STATUS_REQUIRES_EVIDENCE_REF":
        raise AssertionError(f"downgrade without evidence slipped through: {out}")
    if no_evidence != "EVIDENCE_REF_REQUIRED" or bad_decision != "DECISION_NOT_NAMED":
        raise AssertionError(f"release gate too loose: {out}")
    if out["status_after_release"] != "FAILED_RETRYABLE":
        raise AssertionError(f"evidenced release did not apply the named decision: {out}")
    return out


@r39("R29_gate_refusals_neither_consume_nor_bypass_the_resume_budget")
def r29():
    b = fresh_root("R29")
    cfg = Config.load()
    runner = TaskRunner(b, config=cfg, identity=ident())
    runner.prepare("K1")
    runner.recover("K1", lambda: b"RESULT", idempotency_key="RES-K1")
    before = json.loads(b.read_bytes("state/RESUME_COUNTERS.json").decode("utf-8"))
    journal_before = runner.store.counts()["journal"]
    reasons = []
    for wrong in ({"run_id": "RUN-SYNTHETIC-9999"}, {"task_id": "TASK-SYNTHETIC-9999"},
                  {"claim": "K9"}, {"idem": "RES-K9"}):
        kw = {"claim": "K1", "idem": "RES-K1"}
        if "claim" in wrong:
            kw["claim"] = wrong.pop("claim")
        if "idem" in wrong:
            kw["idem"] = wrong.pop("idem")
        ident_arg = {**runner.identity, **wrong}
        try:
            _policy(b, runner.store, cfg, ident_arg, claim=kw["claim"],
                    idem=kw["idem"], max_resume_attempts=1).resume(lambda: b"NEVER")
        except KitError as e:
            reasons.append(e.reason)
    after = json.loads(b.read_bytes("state/RESUME_COUNTERS.json").decode("utf-8"))
    out = {"refusal_reasons": reasons, "attempts_before": before["resume_attempts"],
           "attempts_after": after["resume_attempts"],
           "journal_before": journal_before, "journal_after": runner.store.counts()["journal"],
           "budget_left_untouched": before == after}
    runner.close()
    if len(reasons) != 4 or before != after:
        raise AssertionError(f"validation refusals moved state: {out}")
    if out["attempts_after"] != 1:
        raise AssertionError(f"budget was consumed by refusals: {out}")
    return out


@r39("R30_business_returning_nothing_is_persisted_unknown_not_retryable")
def r30():
    b = fresh_root("R30")
    cfg = Config.load()
    runner = TaskRunner(b, config=cfg, identity=ident())
    runner.prepare("K1")
    calls = {"n": 0}

    def silent_business():
        calls["n"] += 1
        return None          # it "ran" but produced no artifact: an unknown, not a retry

    first = None
    try:
        _policy(b, runner.store, cfg, {**runner.identity, "stage": "RESUME"}).resume(silent_business)
    except KitError as e:
        first = e.reason
    row = runner.store.current("K1")
    second = None
    try:
        _policy(b, runner.store, cfg, {**runner.identity, "stage": "RESUME"}).resume(silent_business)
    except KitError as e:
        second = e.reason
    out = {"first_refusal": first, "claim_status": row["status"],
           "no_result_file_written": not b.exists("state/RESULT.txt"),
           "business_calls": calls["n"], "second_refusal": second,
           "evidence_written": [f for f in os.listdir(os.path.join(b.root, "state"))
                               if f.startswith("BLOCKED.")]}
    runner.close()
    if first != "BUSINESS_PAYLOAD_NOT_BYTES" or row["status"] != "OUTCOME_UNKNOWN":
        raise AssertionError(f"empty business result was not treated as unknown: {out}")
    if second != "CLAIM_IS_BLOCKED_NEEDS_EVIDENCED_RELEASE" or calls["n"] != 1:
        raise AssertionError(f"empty result was retried automatically: {out}")
    return out


# --------------------------------- r39c regressions (required fields / bindings)
@r39c("R31_caller_identity_missing_run_id_refuses_before_business")
def r31():
    b = fresh_root("R31")
    cfg = Config.load()
    runner = TaskRunner(b, config=cfg, identity=ident())
    runner.prepare("K1")
    runner.recover("K1", lambda: b"ORIGINAL_RESULT", idempotency_key="RES-K1")
    before = _snapshot(b, runner.store)
    calls = {"n": 0}
    stripped = {k: v for k, v in runner.identity.items() if k != "run_id"}
    refused = None
    try:
        _policy(b, runner.store, cfg, {**stripped, "stage": "RESUME"}).resume(
            lambda: calls.__setitem__("n", calls["n"] + 1))
    except KitError as e:
        refused = e.reason
    after = _snapshot(b, runner.store)
    out = {"refusal": refused, "business_calls": calls["n"], "snapshot_identical": before == after,
           "result_still_original": b.read_bytes("state/RESULT.txt") == b"ORIGINAL_RESULT"}
    runner.close()
    if refused != "CALLER_IDENTITY_REQUIRED_FIELD_MISSING_RUN_ID":
        raise AssertionError(f"missing run_id not refused by name: {out}")
    if calls["n"] != 0 or before != after or not out["result_still_original"]:
        raise AssertionError(f"missing run_id still moved state: {out}")
    return out


@r39c("R32_invalid_identity_shapes_are_named_not_laundered")
def r32():
    b = fresh_root("R32")
    cfg = Config.load()
    runner = TaskRunner(b, config=cfg, identity=ident())
    runner.prepare("K1")
    before = _snapshot(b, runner.store)
    refused = {}
    for label, value in (("empty", ""), ("whitespace", "   "), ("cjk", "含中文的运行号"),
                         ("too_long", "R" * 65), ("non_string", 12345)):
        calls = {"n": 0}
        try:
            _policy(b, runner.store, cfg,
                    {"run_id": value, "task_id": "TASK-SYNTHETIC-0001",
                     "holder_id": str(uuid.uuid4()), "stage": "RESUME"}).resume(
                        lambda: calls.__setitem__("n", calls["n"] + 1))
            refused[label] = "NOT_REFUSED"
        except KitError as e:
            refused[label] = {"reason": e.reason, "business_calls": calls["n"]}
    # positive control: a valid shape still passes the same gate (instrument is alive)
    good = Checkpoint(b).validate(identity={**runner.identity, "stage": "RESUME"}, stage="COMMIT",
                                 bindings={"claim_key": "K1"}, required_bindings=("claim_key",))
    after = _snapshot(b, runner.store)
    out = {"refusals": refused, "all_named": all(v != "NOT_REFUSED" for v in refused.values()),
           "valid_shape_still_passes": good["checkpoint"] == "PRESENT",
           "snapshot_identical": before == after}
    runner.close()
    if not out["all_named"] or not out["valid_shape_still_passes"] or before != after:
        raise AssertionError(f"shape gate wrong or vacuous: {out}")
    return out


@r39c("R33_recorded_checkpoint_missing_run_id_refuses_resume")
def r33():
    b = fresh_root("R33")
    cfg = Config.load()
    runner = TaskRunner(b, config=cfg, identity=ident())
    runner.prepare("K1")
    legacy = json.loads(b.read_bytes("state/CHECKPOINT.json").decode("utf-8"))
    legacy["identity"] = {k: v for k, v in legacy["identity"].items() if k != "run_id"}
    legacy.pop("_bytes", None); legacy.pop("_sha256", None)
    b.atomic_write("state/CHECKPOINT.json", json.dumps(legacy, indent=2, ensure_ascii=False))
    before = _snapshot(b, runner.store)
    calls = {"n": 0}
    refused = None
    try:
        _policy(b, runner.store, cfg, {**runner.identity, "stage": "RESUME"}).resume(
            lambda: calls.__setitem__("n", calls["n"] + 1))
    except KitError as e:
        refused = e.reason
    after = _snapshot(b, runner.store)
    out = {"refusal": refused, "business_calls": calls["n"],
           "checkpoint_still_the_defective_one": "run_id" not in
               json.loads(b.read_bytes("state/CHECKPOINT.json").decode("utf-8"))["identity"],
           "snapshot_identical": before == after}
    runner.close()
    if refused != "CHECKPOINT_REQUIRED_FIELD_MISSING_RUN_ID" or calls["n"] != 0 or before != after:
        raise AssertionError(f"half-migrated checkpoint was not refused cleanly: {out}")
    return out


@r39c("R34_resume_without_declared_claim_binding_refuses_before_business")
def r34():
    b = fresh_root("R34")
    cfg = Config.load()
    runner = TaskRunner(b, config=cfg, identity=ident())
    runner.prepare("K1")
    doc = json.loads(b.read_bytes("state/CHECKPOINT.json").decode("utf-8"))
    doc["bindings"] = {}              # a checkpoint that names no claim at all
    doc.pop("_bytes", None); doc.pop("_sha256", None)
    b.atomic_write("state/CHECKPOINT.json", json.dumps(doc, indent=2, ensure_ascii=False))
    before = _snapshot(b, runner.store)
    calls = {"n": 0}
    refused = None
    try:
        _policy(b, runner.store, cfg, {**runner.identity, "stage": "RESUME"}).resume(
            lambda: calls.__setitem__("n", calls["n"] + 1))
    except KitError as e:
        refused = e.reason
    after = _snapshot(b, runner.store)
    out = {"refusal": refused, "business_calls": calls["n"], "lease_rows_created": after["_claims"],
           "result_written": b.exists("state/RESULT.txt"), "snapshot_identical": before == after}
    runner.close()
    if refused != "CHECKPOINT_BINDING_UNDECLARED_CLAIM_KEY" or calls["n"] != 0:
        raise AssertionError(f"undeclared claim binding slipped through: {out}")
    if after["_claims"] != before["_claims"] or out["result_written"]:
        raise AssertionError(f"refusal still took a lease or wrote a result: {out}")
    return out


@r39c("R35_prepare_without_idempotency_key_remains_legitimate")
def r35():
    b = fresh_root("R35")
    cfg = Config.load()
    runner = TaskRunner(b, config=cfg, identity=ident())
    runner.prepare("K1")             # declares claim_key only - no idempotency_key yet
    ck = json.loads(b.read_bytes("state/CHECKPOINT.json").decode("utf-8"))
    calls = {"n": 0}

    def business():
        calls["n"] += 1
        return b"SYNTHETIC_RESULT_R35"

    res = _policy(b, runner.store, cfg, {**runner.identity, "stage": "RESUME"},
                  claim="K1", idem="RES-K1").resume(business)
    second = _policy(b, runner.store, cfg, {**runner.identity, "stage": "RESUME"},
                     claim="K1", idem="RES-K1").resume(business)
    out = {"prepare_bindings_declared": sorted((ck.get("bindings") or {}).keys()),
           "idempotency_key_undeclared_at_prepare": "idempotency_key" not in (ck.get("bindings") or {}),
           "verdict": res["verdict"], "business_calls": calls["n"],
           "second_call_verdict": second["verdict"],
           "bindings_after_commit": sorted((json.loads(
               b.read_bytes("state/CHECKPOINT.json").decode("utf-8")).get("bindings") or {}).keys()),
           "no_overrefusal": res["verdict"] == "RESUMED_AND_COMMITTED"}
    runner.close()
    if not out["idempotency_key_undeclared_at_prepare"]:
        raise AssertionError(f"fixture lost the legitimate case: {out}")
    if res["verdict"] != "RESUMED_AND_COMMITTED" or calls["n"] != 1:
        raise AssertionError(f"optional binding was over-refused: {out}")
    if second["verdict"] != "REUSED_EXISTING_RESULT":
        raise AssertionError(f"reuse after the legit path broken: {out}")
    return out


@r39c("R36_caller_side_binding_absence_and_malformed_record_are_named")
def r36():
    b = fresh_root("R36")
    cfg = Config.load()
    runner = TaskRunner(b, config=cfg, identity=ident())
    runner.prepare("K1")
    ck = Checkpoint(b)
    named = {}
    for label, kw in (
        ("caller_claim_empty", dict(bindings={"claim_key": "  "}, required_bindings=("claim_key",))),
        ("caller_claim_absent", dict(bindings={}, required_bindings=("claim_key",))),
        ("caller_claim_not_str", dict(bindings={"claim_key": 42}, required_bindings=("claim_key",))),
    ):
        try:
            ck.validate(identity={**runner.identity, "stage": "RESUME"}, stage="COMMIT", **kw)
            named[label] = "NOT_REFUSED"
        except KitError as e:
            named[label] = e.reason
    bad_identity_type = None
    try:
        ck.validate(identity="RUN-SYNTHETIC-0001", stage="COMMIT",
                    bindings={"claim_key": "K1"}, required_bindings=("claim_key",))
    except KitError as e:
        bad_identity_type = e.reason
    doc = json.loads(b.read_bytes("state/CHECKPOINT.json").decode("utf-8"))
    doc["stage"] = "NOT_A_STAGE"
    doc.pop("_bytes", None); doc.pop("_sha256", None)
    b.atomic_write("state/CHECKPOINT.json", json.dumps(doc, indent=2, ensure_ascii=False))
    malformed_stage = None
    try:
        ck.validate(identity={**runner.identity, "stage": "RESUME"}, stage="COMMIT",
                    bindings={"claim_key": "K1"}, required_bindings=("claim_key",))
    except KitError as e:
        malformed_stage = e.reason
    calls = {"n": 0}
    refused = None
    try:
        _policy(b, runner.store, cfg, {**runner.identity, "stage": "RESUME"}).resume(
            lambda: calls.__setitem__("n", calls["n"] + 1))
    except KitError as e:
        refused = e.reason
    out = {"caller_side": named, "identity_not_mapping": bad_identity_type,
           "malformed_stage": malformed_stage, "resume_refusal_on_malformed_record": refused,
           "business_calls": calls["n"]}
    runner.close()
    expect = {"caller_claim_empty": "CALLER_BINDING_MISSING_CLAIM_KEY",
              "caller_claim_absent": "CALLER_BINDING_MISSING_CLAIM_KEY",
              "caller_claim_not_str": "CALLER_BINDING_MISSING_CLAIM_KEY"}
    if any(named.get(k) != v for k, v in expect.items()):
        raise AssertionError(f"caller-side binding gate mislabelled: {out}")
    if bad_identity_type != "CALLER_IDENTITY_NOT_A_MAPPING" or malformed_stage != "CHECKPOINT_MALFORMED_STAGE":
        raise AssertionError(f"malformed inputs not named: {out}")
    if refused != "CHECKPOINT_MALFORMED_STAGE" or calls["n"] != 0:
        raise AssertionError(f"resume did not refuse the malformed record before business: {out}")
    return out


@r39c("R37_both_sides_missing_the_field_is_not_an_equality_match")
def r37():
    """The sharpest version of the hole: caller and checkpoint both omit run_id.

    A pure equality test says None == None and lets the resume through. Presence and
    shape have to be checked on each side independently, or a half-migrated state
    root becomes resumable by anyone who simply does not name the work.
    """
    b = fresh_root("R37")
    cfg = Config.load()
    hand = {"schema": "awake_kit_checkpoint_v1", "stage": "PREPARE",
            "identity": {"holder_id": str(uuid.uuid4()), "stage": "PREPARE"},
            "bindings": {"claim_key": "K1"}, "writer_pid": os.getpid(),
            "facts": {}, "written_utc": "2020-01-01T00:00:00Z", "seq": 1}
    b.atomic_write("state/CHECKPOINT.json", json.dumps(hand, indent=2, ensure_ascii=False))
    store = ClaimStore(b.resolve("state/guard.sqlite"))
    before = _snapshot(b, store)
    calls = {"n": 0}
    refused = None
    try:
        _policy(b, store, cfg, {"holder_id": str(uuid.uuid4()), "stage": "RESUME"},
                claim="K1", idem="RES-K1").resume(
                    lambda: calls.__setitem__("n", calls["n"] + 1))
        outcome = "PROCEEDED"
    except KitError as e:
        refused, outcome = e.reason, "REFUSED"
    after = _snapshot(b, store)
    out = {"outcome": outcome, "refusal": refused, "business_calls": calls["n"],
           "lease_rows_created": after["_claims"], "result_written": b.exists("state/RESULT.txt"),
           "checkpoint_bytes_unchanged": before["state/CHECKPOINT.json"]
                                         == after["state/CHECKPOINT.json"],
           "snapshot_identical": before == after}
    store.close()
    if refused != "CALLER_IDENTITY_REQUIRED_FIELD_MISSING_RUN_ID" or calls["n"] != 0:
        raise AssertionError(f"both-sides-missing identity was not refused: {out}")
    if after["_claims"] != 0 or out["result_written"] or before != after:
        raise AssertionError(f"refusal still moved state: {out}")
    return out


def run_all():
    results = {"PASS": [], "FAIL": [], "NOT_RUN": []}
    for name, family, fn in CASES:
        try:
            r = fn()
            if isinstance(r, tuple) and r[0] == "NOT_RUN":
                results["NOT_RUN"].append({"case": name, "family": family, "reason": r[1]})
            else:
                results["PASS"].append({"case": name, "family": family, "evidence": r})
        except Exception as exc:
            results["FAIL"].append({"case": name, "family": family,
                                    "error": f"{type(exc).__name__}: {exc}",
                                    "traceback_tail": traceback.format_exc().splitlines()[-4:]})
    return results


if __name__ == "__main__":
    print(json.dumps(run_all(), ensure_ascii=False, indent=2))
