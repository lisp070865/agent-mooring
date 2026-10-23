"""Checkpoint records that cannot silently move backwards.

A checkpoint is the contract between "the part that died" and "the part that
resumes". It is written atomically, it names the last *completed* stage, and the
stage list is declared once so a resumed run can prove it is continuing rather
than restarting (or skipping).
"""

from __future__ import annotations

import hashlib
import json
import os
import time

from .errors import KitError
from .identity import TOKEN_RE

STAGE_ORDER = ("PREPARE", "FAULT", "WAKE", "RESUME", "COMMIT", "CLOSEOUT")
REQUIRED_IDENTITY_FIELDS = ("run_id", "task_id")


def _validate_stage(stage: str) -> str:
    if not isinstance(stage, str) or stage not in STAGE_ORDER:
        raise KitError("UNKNOWN_STAGE", stage=stage, allowed=list(STAGE_ORDER))
    return stage


def _require_identity_shape(identity, *, side: str) -> dict:
    """run_id/task_id must be present and well-formed - on both sides.

    Comparing two missing fields is not a match. If both the caller and the
    checkpoint omit `run_id`, an equality test would silently pass, which is how a
    checkpoint written by half-migrated code becomes resumable by nobody in
    particular. So presence and shape are checked before any comparison.
    """
    if not isinstance(identity, dict):
        raise KitError(f"{side}_IDENTITY_NOT_A_MAPPING", got=type(identity).__name__)
    out = {}
    for name in REQUIRED_IDENTITY_FIELDS:
        value = identity.get(name)
        if value is None or (isinstance(value, str) and not value.strip()):
            raise KitError(f"{side}_IDENTITY_REQUIRED_FIELD_MISSING_{name.upper()}",
                           field=name, side=side,
                           remedy=f"supply a non-empty {name} matching the token shape")
        if not isinstance(value, str) or not TOKEN_RE.match(value):
            raise KitError(f"{side}_IDENTITY_REQUIRED_FIELD_INVALID_{name.upper()}",
                           field=name, side=side, got=str(value)[:24],
                           allowed_shape=TOKEN_RE.pattern)
        out[name] = value
    return out


def _check_recorded_identity(recorded: dict, *, recorded_stage) -> dict:
    """The checkpoint file itself must carry the fields a resumer has to compare."""
    for name in REQUIRED_IDENTITY_FIELDS:
        value = recorded.get(name)
        if not isinstance(value, str) or not value.strip():
            raise KitError(f"CHECKPOINT_REQUIRED_FIELD_MISSING_{name.upper()}",
                           field=name, recorded_stage=recorded_stage,
                           policy="a checkpoint that does not declare its own "
                                  f"{name} cannot authorize a resume")
    return recorded


class Checkpoint:
    def __init__(self, boundary, rel_path="state/CHECKPOINT.json"):
        self.b = boundary
        self.rel = rel_path

    # ---- read-only gate: this must run BEFORE any business call, reuse, commit,
    #      or recovery state transition - including the same-stage early return ----
    def validate(self, *, identity: dict, stage: str, bindings: dict | None = None,
                 require_min_stage: str | None = None,
                 required_bindings: tuple = ()) -> dict:
        _validate_stage(stage)
        supplied = _require_identity_shape(identity, side="CALLER")
        supplied_bindings = dict(bindings or {})
        # a binding the caller declares required must itself be present and typed
        for key in required_bindings:
            value = supplied_bindings.get(key)
            if not isinstance(value, str) or not value.strip():
                raise KitError(f"CALLER_BINDING_MISSING_{key.upper()}", field=key,
                               required_by="recovery",
                               remedy="this stage cannot resume work it cannot name")
        if not self.b.exists(self.rel):
            if require_min_stage:
                raise KitError("CHECKPOINT_REQUIRED_BUT_MISSING",
                               required_min_stage=require_min_stage, expected_path=self.rel,
                               policy="a missing checkpoint is not a licence to run the business")
            return {"checkpoint": "ABSENT", "stage_requested": stage,
                    "identity_validated": supplied,
                    "bindings_validated": supplied_bindings}
        prev = self.read()
        if not isinstance(prev.get("identity"), dict):
            raise KitError("CHECKPOINT_MALFORMED_IDENTITY_ABSENT",
                           keys=sorted(k for k in prev if not k.startswith("_")))
        recorded_stage = prev.get("stage")
        if recorded_stage not in STAGE_ORDER:
            raise KitError("CHECKPOINT_MALFORMED_STAGE", recorded_stage=recorded_stage,
                           allowed=list(STAGE_ORDER))
        recorded = _check_recorded_identity(prev.get("identity"), recorded_stage=recorded_stage)
        if require_min_stage and STAGE_ORDER.index(recorded_stage) < STAGE_ORDER.index(require_min_stage):
            raise KitError("CHECKPOINT_STAGE_TOO_EARLY", recorded=recorded_stage,
                           require_min_stage=require_min_stage)
        reported = {"checkpoint": "PRESENT", "recorded_stage": recorded_stage,
                    "recorded_seq": prev.get("seq"), "stage_requested": stage,
                    "identity_checked": [], "bindings_checked": [],
                    "bindings_not_declared_by_checkpoint": [],
                    "required_bindings_present": list(required_bindings),
                    "holder_may_differ": True}
        # Identity binds the WORK, field by field, and names the offending field.
        for field_name in REQUIRED_IDENTITY_FIELDS:
            want, got = recorded.get(field_name), supplied.get(field_name)
            reported["identity_checked"].append({field_name: got, "matches": want == got})
            if want != got:
                raise KitError(f"CHECKPOINT_{field_name.upper()}_MISMATCH",
                               field=field_name, expected=want, got=got,
                               recorded_stage=recorded_stage)
        recorded_bindings = prev.get("bindings") or {}
        for key, value in supplied_bindings.items():
            if value is None:
                continue
            if not isinstance(value, str) or not TOKEN_RE.match(value):
                raise KitError(f"CALLER_BINDING_INVALID_{key.upper()}", field=key,
                               got=str(value)[:24], allowed_shape=TOKEN_RE.pattern)
            if key not in recorded_bindings:
                # "the other file does not declare this field" is NOT a value conflict
                # and NOT a match. It is tolerated only where declaring it is genuinely
                # optional (idempotency_key is not known until a commit), and refused
                # where the stage cannot act without it (claim_key for a resume).
                if key in required_bindings:
                    raise KitError(f"CHECKPOINT_BINDING_UNDECLARED_{key.upper()}",
                                   field=key, recorded_stage=recorded_stage,
                                   policy="this stage must not resume work whose claim it "
                                          "cannot verify; write the binding, do not guess it")
                reported["bindings_not_declared_by_checkpoint"].append(key)
                continue
            reported["bindings_checked"].append({key: value, "matches": recorded_bindings[key] == value})
            if recorded_bindings[key] != value:
                raise KitError(f"CHECKPOINT_BINDING_MISMATCH_{key.upper()}",
                               field=key, expected=recorded_bindings[key], got=value,
                               recorded_stage=recorded_stage)
        if STAGE_ORDER.index(stage) < STAGE_ORDER.index(recorded_stage):
            raise KitError("CHECKPOINT_REGRESSION_REFUSED",
                           from_stage=recorded_stage, to_stage=stage)
        reported["same_stage"] = recorded_stage == stage
        return reported

    def write(self, stage: str, *, identity: dict, facts: dict | None = None,
              bindings: dict | None = None, required_bindings: tuple = ()) -> dict:
        # shape-check the caller before anything else, so a half-missing identity is
        # never written down in the first place
        report = self.validate(identity=identity, stage=stage, bindings=bindings,
                               required_bindings=required_bindings)
        prev = self.read() if self.b.exists(self.rel) else None
        if report["checkpoint"] == "PRESENT" and prev["stage"] == stage:
            # validated first, then reported: a wrong identity can never hide behind
            # "same stage, nothing to do".
            return {"written": False, "reason": "STAGE_ALREADY_RECORDED", "stage": stage,
                    "written_utc": prev.get("written_utc"), "validation": report}
        payload = {
            "schema": "awake_kit_checkpoint_v1",
            "stage": stage,
            "identity": identity,
            "bindings": {k: v for k, v in (bindings or {}).items() if v is not None},
            "writer_holder_id": identity.get("holder_id"),
            "writer_pid": os.getpid(),
            "facts": facts or {},
            "written_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "seq": (prev.get("seq", 0) + 1) if prev else 1,
        }
        res = self.b.atomic_write(self.rel, json.dumps(payload, indent=2, ensure_ascii=False))
        res["written"] = True
        res["seq"] = payload["seq"]
        return res

    def read(self) -> dict:
        raw = self.b.read_bytes(self.rel)
        doc = json.loads(raw.decode("utf-8"))
        doc["_bytes"] = len(raw)
        doc["_sha256"] = hashlib.sha256(raw).hexdigest()
        return doc

    def completed(self, stage: str) -> bool:
        if not self.b.exists(self.rel):
            return False
        cur = self.read()["stage"]
        return STAGE_ORDER.index(cur) >= STAGE_ORDER.index(stage)
