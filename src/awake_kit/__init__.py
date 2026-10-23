"""awake_kit - minimal, offline, generic control-loop primitives.

This package is a *candidate* open-source copy. It contains generic code only,
ships synthetic sample data, and its network-facing adapters are disabled by
default. It is not a production agent runtime and it is not wired to any real
mailbox, scheduler, or client.

Modules
    identity  task/holder identity validation (fail closed)
    claim     exclusive claim + lease with generation, retry budget
    checkpoint  monotonic checkpoint records
    recovery  bounded resume-from-checkpoint with live-writer protection
    commit    duplicate-commit prevention (immutable result, reuse on conflict)
    stop      stop flag consulted before every action
    outcome   external-result classification incl. OUTCOME_UNKNOWN (no auto resend)
    paths     exclusive-root / directory boundary and atomic writes
    config    empty-by-default configuration; no environment credential reads
    ports     public interface abstractions + disabled and fake implementations
    runner    small orchestration showing the gates in order

Nothing here reads credentials, environment secrets, private session logs, or a
private scheduler store. Nothing here opens a network socket.
"""

__all__ = [
    "identity", "claim", "checkpoint", "recovery", "commit", "stop",
    "outcome", "paths", "config", "ports", "runner", "errors",
]
__version__ = "0.1.0-candidate"
PROVENANCE_NOTE = (
    "Semantics distilled from an internal multi-agent control experiment "
    "(exclusive claim/lease, checkpoint-resume, immutable result, unknown-outcome "
    "no-resend). Rewritten from scratch with synthetic identifiers; no internal "
    "identifier, mailbox, path, receipt, or log bytes are present. "
    "See PROVENANCE.md and LICENSE.PENDING.md."
)
