"""Exclusive root boundary, atomic writes, first-time (O_EXCL) creation.

Design intent: every byte this package writes goes through one of the two
writers below, so "did anything escape the declared directory?" has a single
answerable place, and so "was this file written twice?" is answered by the OS
rather than by memory.
"""

from __future__ import annotations

import errno
import os
import stat as statmod
import time

from .errors import BoundaryViolation

PER_FILE_CAP = 2 * 1024 * 1024          # 2 MiB per protocol/log/result file
STATE_ROOT_CAP = 128 * 1024 * 1024      # 128 MiB per state root


def _is_reparse(path) -> bool:
    if os.name == "nt":
        try:
            return bool(os.lstat(path).st_file_attributes & statmod.FILE_ATTRIBUTE_REPARSE_POINT)
        except (AttributeError, OSError):
            return False
    return os.path.islink(path)


class RootBoundary:
    """A directory that was created for one task and is checked on every write."""

    def __init__(self, root: str, expected_entries=()):
        self.root_input = root
        if not os.path.isabs(root):
            raise BoundaryViolation("root_must_be_absolute", root=root)
        real = os.path.realpath(root)
        if os.path.normcase(real) != os.path.normcase(os.path.abspath(root)):
            raise BoundaryViolation("root_resolves_elsewhere", given=os.path.abspath(root), real=real)
        if not os.path.isdir(root):
            raise BoundaryViolation("root_missing", root=root)
        self._walk_reparse(root)
        allowed = {os.path.normcase(e) for e in expected_entries}
        if allowed != set():
            entries = {os.path.normcase(e) for e in os.listdir(root)}
            unexpected = sorted(entries - allowed)
            if unexpected:
                raise BoundaryViolation("root_not_exclusive", root=root,
                                        pre_existing=unexpected, allowed=sorted(allowed))
        self.root = real

    @staticmethod
    def _walk_reparse(path):
        cur = os.path.abspath(path)
        chain = []
        while True:
            chain.append(cur)
            parent = os.path.dirname(cur)
            if parent == cur:
                break
            cur = parent
        for comp in reversed(chain):
            if _is_reparse(comp):
                raise BoundaryViolation("reparse_point_in_path", component=comp,
                                        note="junctions and symlinks break exclusivity")

    @classmethod
    def create_exclusive(cls, root: str, expected_entries=()):
        """Create a state root that must not already exist, then bind to it."""
        if os.path.exists(root):
            raise BoundaryViolation("root_already_exists_use_explicit_binding", root=root)
        os.makedirs(root)
        return cls(root, expected_entries=expected_entries)

    # ---- path checking ----
    def resolve(self, rel: str) -> str:
        if os.path.isabs(rel):
            target = os.path.abspath(rel)
        else:
            target = os.path.abspath(os.path.join(self.root, rel))
        if os.path.normcase(target) == os.path.normcase(self.root):
            return target
        inside = os.path.normcase(target).startswith(os.path.normcase(self.root) + os.sep)
        if not inside:
            raise BoundaryViolation("write_outside_exclusive_root",
                                    target=target, root=self.root)
        # a symlinked component inside the root would let a write escape
        probe = target
        while probe and os.path.normcase(probe) != os.path.normcase(self.root):
            if os.path.exists(probe) and _is_reparse(probe):
                raise BoundaryViolation("reparse_point_inside_root", component=probe)
            probe = os.path.dirname(probe)
        return target

    def state_bytes(self) -> int:
        total = 0
        for dirpath, _dirs, files in os.walk(self.root):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(dirpath, f))
                except OSError:
                    pass
        return total

    def _assert_capacity(self, payload_len: int):
        if payload_len > PER_FILE_CAP:
            raise BoundaryViolation("per_file_cap_exceeded", cap=PER_FILE_CAP, size=payload_len)
        used = self.state_bytes()
        if used + payload_len > STATE_ROOT_CAP:
            raise BoundaryViolation("state_root_cap_exceeded", cap=STATE_ROOT_CAP, used=used,
                                    want=payload_len)

    # ---- writers ----
    def atomic_write(self, rel: str, payload: bytes) -> dict:
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        target = self.resolve(rel)
        self._assert_capacity(len(payload))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        tmp = f"{target}.tmp-{int(time.time()*1000)}"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        try:
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, target)
        try:
            dfd = os.open(os.path.dirname(target), os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            pass  # directory fsync is not portable; file durability already achieved
        return {"path": os.path.relpath(target, self.root).replace("\\", "/"),
                "bytes": os.path.getsize(target), "mode": "atomic_replace"}

    def open_exclusive(self, rel: str, payload: bytes) -> dict:
        """O_EXCL first-time creation. Never overwrites; caller decides reuse."""
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        target = self.resolve(rel)
        self._assert_capacity(len(payload))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        try:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        except FileExistsError as exc:
            if exc.errno not in (errno.EEXIST, 17):
                raise
            return {"created": False,
                    "path": os.path.relpath(target, self.root).replace("\\", "/"),
                    "bytes": os.path.getsize(target)}
        try:
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        return {"created": True,
                "path": os.path.relpath(target, self.root).replace("\\", "/"),
                "bytes": os.path.getsize(target)}

    def read_bytes(self, rel: str) -> bytes:
        target = self.resolve(rel)
        if not os.path.isfile(target):
            raise BoundaryViolation("file_absent", path=rel)
        with open(target, "rb") as f:
            return f.read()

    def exists(self, rel: str) -> bool:
        try:
            return os.path.isfile(self.resolve(rel))
        except BoundaryViolation:
            return False
