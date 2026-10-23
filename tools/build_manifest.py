"""Build results/MANIFEST.sha256.csv (+ .json): bytes and full SHA256 per file.

The manifest never lists its own hash (a file cannot contain its own digest); the
digest is published by the tool output and by the delivery note instead.
Exclusions are named, and the enumeration is closed by an equation:

    on_disk_text_or_code_files == listed + excluded_by_rule

so "0 files missing" cannot be produced by a filter that silently saw nothing.

usage: python tools/build_manifest.py
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time

PKG = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_CSV = os.path.join(PKG, "results", "MANIFEST.sha256.csv")
OUT_JSON = os.path.join(PKG, "results", "MANIFEST.sha256.json")
SKIP_DIRS = {"__pycache__", ".git"}
EXPLICIT_EXCLUDE = {"results/MANIFEST.sha256.csv", "results/MANIFEST.sha256.json"}
SCRATCH_PREFIXES = ("work/",)


def rel(p):
    return os.path.relpath(p, PKG).replace("\\", "/")


def all_files():
    for dirpath, dirnames, files in os.walk(PKG):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in files:
            yield rel(os.path.join(dirpath, name))


def classify(path):
    if path in EXPLICIT_EXCLUDE:
        return "excluded", "manifest_cannot_hash_itself"
    if path.startswith(SCRATCH_PREFIXES):
        return "excluded", "scratch_state_root_rebuilt_by_tests"
    if os.path.basename(path).startswith(".tmp-") or ".tmp-" in os.path.basename(path):
        return "excluded", "atomic_write_temp_residue"
    return "listed", ""


def main():
    universe = sorted(all_files())
    listed, excluded = [], []
    for path in universe:
        kind, why = classify(path)
        (listed if kind == "listed" else excluded).append((path, why))
    rows = []
    for path, _ in listed:
        with open(os.path.join(PKG, path), "rb") as f:
            blob = f.read()
        rows.append({"path": path, "bytes": len(blob), "sha256": hashlib.sha256(blob).hexdigest()})
    rows.sort(key=lambda r: r["path"])
    csv = "path,bytes,sha256\n" + "".join(
        f'"{r["path"]}",{r["bytes"]},{r["sha256"]}\n' for r in rows)
    with open(OUT_CSV, "w", encoding="utf-8", newline="\n") as f:
        f.write(csv)
    closure = {
        "on_disk_files_total": len(universe),
        "listed_total": len(rows),
        "excluded_total": len(excluded),
        "equation_holds": len(universe) == len(rows) + len(excluded),
        "excluded_named": [{"path": p, "reason": why} for p, why in excluded],
        "excluded_without_reason": [p for p, why in excluded if not why],
    }
    recheck = []
    for r in rows:  # independent re-read, same tool, different pass
        with open(os.path.join(PKG, r["path"]), "rb") as f:
            blob = f.read()
        if len(blob) != r["bytes"] or hashlib.sha256(blob).hexdigest() != r["sha256"]:
            recheck.append(r["path"])
    doc = {
        "schema": "awake_kit_manifest_v1",
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "package_root": "THIS_DIRECTORY",
        "hash_function": "sha256",
        "encoding": "raw bytes as stored on disk; files written with LF newlines",
        "files": rows,
        "closure": closure,
        "recheck_mismatches": recheck,
        "self_reference_note": "results/MANIFEST.sha256.csv and .json are intentionally "
                               "not listed; their digests are published externally.",
    }
    with open(OUT_JSON, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(doc, indent=2, ensure_ascii=False))
    print(json.dumps({"files_listed": len(rows), "closure": closure,
                      "recheck_mismatches": recheck,
                      "total_bytes_listed": sum(r["bytes"] for r in rows),
                      "manifest_json_sha256": hashlib.sha256(open(OUT_JSON, "rb").read()).hexdigest(),
                      "manifest_csv_sha256": hashlib.sha256(open(OUT_CSV, "rb").read()).hexdigest()},
                     ensure_ascii=False, indent=2))
    return 0 if closure["equation_holds"] and not recheck and not closure["excluded_without_reason"] else 1


if __name__ == "__main__":
    sys.exit(main())
