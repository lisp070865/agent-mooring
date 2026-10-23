"""Duplicate-commit prevention: first writer wins, everyone else reuses."""

from __future__ import annotations

import hashlib

from .errors import KitError


class ResultCommitter:
    """Records a business result so it can be produced exactly once.

    Order is deliberate: the file is created with ``O_EXCL`` first (the durable
    artifact), then the database row. If the process dies in between, the reuse
    branch reconstructs the row from the file that is already on disk - it does
    not re-run the business, because re-running is the thing we are preventing.
    """

    def __init__(self, boundary, store):
        self.b = boundary
        self.store = store

    def commit(self, *, claim_key: str, idempotency_key: str, payload,
               rel_path: str) -> dict:
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        if not isinstance(payload, bytes):
            # A business step that returns nothing is not a result. Refusing here is
            # loud and happens after the business started, so recovery records it as
            # OUTCOME_UNKNOWN rather than as a harmless pre-business error.
            raise KitError("BUSINESS_PAYLOAD_NOT_BYTES", claim_key=claim_key,
                           got=type(payload).__name__,
                           remedy="business() must return bytes (or str) as the result payload")
        sha = hashlib.sha256(payload).hexdigest()
        file_res = self.b.open_exclusive(rel_path, payload)
        db_res = self.store.commit_result(claim_key, idempotency_key, payload, rel_path)

        if file_res["created"] and db_res.get("committed"):
            return {"outcome": "COMMITTED", "execution_count": 1, "payload_sha256": sha,
                    "path": file_res["path"], "bytes": file_res["bytes"]}

        if (not file_res["created"]) and (not db_res.get("committed")):
            on_disk = self.b.read_bytes(rel_path)
            if hashlib.sha256(on_disk).hexdigest() != sha:
                raise KitError("RESULT_CONTENT_CONFLICT_ON_REUSE",
                               path=file_res["path"], existing_bytes=len(on_disk),
                               existing_sha256=hashlib.sha256(on_disk).hexdigest(),
                               refused_new_sha256=sha)
            existing = self.store.get_result(idempotency_key) or {}
            return {"outcome": "REUSED", "execution_count": existing.get("execution_count", 1),
                    "payload_sha256": sha, "path": file_res["path"],
                    "bytes": file_res["bytes"], "business_re_executed": False}

        if (not file_res["created"]) and db_res.get("committed"):
            on_disk = self.b.read_bytes(rel_path)
            if hashlib.sha256(on_disk).hexdigest() != sha:
                raise KitError("CRASH_RECONCILE_HASH_MISMATCH", path=file_res["path"])
            return {"outcome": "DB_ROW_RECONSTRUCTED_AFTER_CRASH", "execution_count": 1,
                    "payload_sha256": sha, "path": file_res["path"],
                    "business_re_executed": False}

        raise KitError("UNEXPECTED_COMMIT_STATE", file=file_res, db=db_res)
