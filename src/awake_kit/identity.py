"""Task identity validation, fail closed.

Two rules that are easy to get wrong and are therefore encoded here rather than
left to convention:

1. A *name* is not an identity. A mailbox address, an alias, a message id, a
   thread id, a directory name, a window title, "the most recently active
   session", or an agent display name must never be used as the identity of the
   thing that is executing. Only an explicitly issued identifier matching a
   declared shape may be.
2. Identity comparisons are field by field. Comparing identity as an unordered
   set of values lets two identities swap slots and still "match".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict

from .errors import IdentityRejected

TOKEN_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
UUID4_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)

#: kinds that are documentation-only and must never be bound as an executor id
NOT_IDENTITY_KINDS = (
    "mailbox_address",
    "mailbox_alias",
    "message_id",
    "thread_id",
    "directory_name",
    "window_title",
    "most_recent_activity",
    "agent_display_name",
    "inferred_from_log_line",
)


@dataclass(frozen=True)
class TaskIdentity:
    run_id: str
    task_id: str
    holder_id: str
    stage: str
    self_reported: bool = True
    provenance: str = field(default="UNVERIFIED_SELF_REPORT")

    def as_dict(self):
        return asdict(self)

    def same_as(self, other: "TaskIdentity") -> dict:
        """Field-by-field equality with the offending field named."""
        out = {}
        for f in ("run_id", "task_id", "holder_id", "stage"):
            mine, theirs = getattr(self, f), getattr(other, f)
            out[f] = (mine == theirs)
        out["all_equal"] = all(out[f] for f in ("run_id", "task_id", "holder_id", "stage"))
        out["first_mismatch"] = next((f for f in out if f not in ("all_equal",) and out[f] is False), None)
        return out


def _check_token(value, name, pattern=TOKEN_RE):
    if not isinstance(value, str):
        raise IdentityRejected(f"{name}_not_a_string", field=name, got=type(value).__name__)
    if value != value.strip():
        raise IdentityRejected(f"{name}_has_surrounding_whitespace", field=name)
    if not value.strip():
        raise IdentityRejected(f"{name}_empty", field=name)
    if not pattern.match(value):
        raise IdentityRejected(
            f"{name}_shape_rejected", field=name, pattern=pattern.pattern, length=len(value)
        )
    return value


def build_identity(run_id, task_id, holder_id, stage, self_reported=True,
                   provenance="UNVERIFIED_SELF_REPORT") -> TaskIdentity:
    """Validate and freeze an identity. Rejects whitespace, CJK, and slot swaps."""
    _check_token(run_id, "run_id")
    _check_token(task_id, "task_id")
    _check_token(stage, "stage")
    _check_token(holder_id, "holder_id", UUID4_RE)
    if not isinstance(self_reported, bool):
        raise IdentityRejected("self_reported_must_be_bool", field="self_reported")
    if self_reported and provenance == "AUTHORITATIVE_PROVIDER_LOOKUP":
        raise IdentityRejected(
            "self_reported_cannot_claim_authoritative_lookup",
            field="provenance",
        )
    return TaskIdentity(run_id, task_id, holder_id, stage, self_reported, provenance)


def assert_identity_source(kind: str) -> None:
    """Guard against binding a non-identity as an executor identity."""
    if kind in NOT_IDENTITY_KINDS:
        raise IdentityRejected(
            "kind_is_not_an_identity", kind=kind,
            not_identity_kinds=list(NOT_IDENTITY_KINDS),
            remedy="use an explicitly issued holder id (uuid4) from the executor itself",
        )
