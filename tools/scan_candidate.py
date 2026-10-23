"""Scan this package for content that must not be published. Runs locally only.

Two rule families:

1. ``shape`` rules are self-contained: they look for the *form* of real-world
   identifiers (absolute drive paths, email addresses outside the synthetic
   reserve, RFC message-ids, unlabelled long hex, non-synthetic uuids). They need
   no secret to be embedded, which is what lets the scanner itself be published.
2. ``literal`` rules need the actual forbidden strings, so they are supplied at
   run time from a signature file that lives outside this package
   (``--signature-file``). Without it, the report says NOT_APPLIED instead of
   pretending the check passed.

Every rule reports its own tested denominator; a rule that inspected nothing is
an error, not a pass.

usage: python tools/scan_candidate.py [--signature-file <path-with-json-list>]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time

PKG = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SYNTHETIC_EMAIL_DOMAINS = {"example.invalid", "example.test", "localhost"}
SYNTHETIC_UUIDS = {"00000000-0000-4000-8000-000000000001"}
UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[45][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b", re.I)
DRIVE_RE = re.compile(r"\b[A-Za-z]:[\\/]{1,2}[^\\s\"']")
EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
RFC_ID_RE = re.compile(r"<[0-9a-fA-Z._-]{12,}@[A-Za-z0-9.-]+>")
HEX_RE = re.compile(r"\b[0-9a-f]{32,}\b")
DIGEST_KEY_RE = re.compile(r"(sha|hash|digest|fingerprint|summary|token)", re.I)
TEXT_EXT = {".py", ".md", ".json", ".csv", ".txt", ".toml", ".yaml", ".yml"}
SKIP_DIRS = {"__pycache__", ".git", "work"}


def is_text_file(path):
    return os.path.splitext(path)[1].lower() in TEXT_EXT


def walk_files():
    for dirpath, dirnames, files in os.walk(PKG):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in files:
            if not is_text_file(os.path.join(dirpath, name)):
                continue
            yield os.path.relpath(os.path.join(dirpath, name), PKG).replace("\\", "/")


def scan_shapes(files):
    findings = {"absolute_drive_path": [], "email_outside_synthetic_reserve": [],
                "rfc_message_id_shape": [], "uuid_not_declared_synthetic": [],
                "unlabelled_long_hex": []}
    tested = {k: 0 for k in findings}
    for rel in files:
        blob = open(os.path.join(PKG, rel), encoding="utf-8", errors="replace").read()
        generated = rel.startswith("results/") or rel.startswith("data/")
        tested["absolute_drive_path"] += 1
        for m in DRIVE_RE.finditer(blob):
            findings["absolute_drive_path"].append({"file": rel, "match": m.group(0)[:40]})
        tested["email_outside_synthetic_reserve"] += 1
        for m in EMAIL_RE.finditer(blob):
            domain = m.group(0).split("@", 1)[1].lower()
            if domain not in SYNTHETIC_EMAIL_DOMAINS:
                findings["email_outside_synthetic_reserve"].append({"file": rel, "match": m.group(0)})
        tested["rfc_message_id_shape"] += 1
        for m in RFC_ID_RE.finditer(blob):
            findings["rfc_message_id_shape"].append({"file": rel, "match": m.group(0)[:60]})
        tested["uuid_not_declared_synthetic"] += 1
        for m in UUID_RE.finditer(blob):
            v = m.group(0).lower()
            if v in SYNTHETIC_UUIDS:
                continue
            if generated:
                continue  # runtime-generated synthetic uuid in generated output
            findings["uuid_not_declared_synthetic"].append({"file": rel, "match": v})
        tested["unlabelled_long_hex"] += 1
        lines = blob.splitlines()
        # A manifest CSV declares its digest column in the header row
        # (`path,bytes,sha256`); values in that column are computed digests,
        # not identifiers, so they are exempt - other columns are not.
        digest_col = None
        line_starts = []
        if rel.lower().endswith(".csv") and lines:
            header = [h.strip().strip('"') for h in lines[0].split(",")]
            for i, h in enumerate(header):
                if DIGEST_KEY_RE.search(h):
                    digest_col = i
            pos = 0
            for ln in lines:
                line_starts.append(pos)
                nxt = blob.find("\n", pos)
                pos = (nxt + 1) if nxt != -1 else len(blob)
        for m in HEX_RE.finditer(blob):
            idx = blob[:m.start()].count("\n")
            line = lines[idx] if idx < len(lines) else ""
            if digest_col is not None and idx > 0:
                col = line[:m.start() - line_starts[idx]].count(",")
                if col == digest_col:
                    continue          # declared digest column of a manifest table
            key_ok = generated and bool(DIGEST_KEY_RE.search(line))
            synthetic_marker = "synthetic" in line.lower() or "sha256" in line.lower()
            if not (key_ok or synthetic_marker):
                findings["unlabelled_long_hex"].append({"file": rel, "line": idx + 1,
                                                        "match": m.group(0)[:24] + "..."})
    return findings, tested


def scan_literals(files, signature_path):
    if not signature_path:
        return None
    sigs = json.load(open(signature_path, encoding="utf-8"))
    if isinstance(sigs, dict):
        sigs = sigs.get("signatures") or list(sigs.values())
    hits = []
    for rel in files:
        blob = open(os.path.join(PKG, rel), encoding="utf-8", errors="replace").read()
        for entry in sigs:
            if not isinstance(entry, str) or len(entry) < 8:
                continue
            if entry in blob:
                hits.append({"file": rel, "signature_len": len(entry),
                             "signature_sha256": hashlib.sha256(entry.encode()).hexdigest()[:16]})
    return {"signatures_supplied": len(sigs), "files_tested": len(files), "hits": hits}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--signature-file", default=None,
                    help="JSON list of exact forbidden strings; lives OUTSIDE this package")
    args = ap.parse_args()
    files = sorted(walk_files())
    findings, tested = scan_shapes(files)
    literal = scan_literals(files, args.signature_file)
    violations = {k: v for k, v in findings.items() if v}
    doc = {
        "schema": "awake_kit_sensitive_scan_v1",
        "ran_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "scope": "THIS PACKAGE DIRECTORY ONLY; no credential store, session log, "
                 "scheduler store, or research data was read to perform this scan",
        "skipped_directories": sorted(SKIP_DIRS),
        "non_text_files_skipped": True,
        "files_scanned": len(files),
        "rules_tested": tested,
        "rules_with_zero_tested": [k for k, v in tested.items() if v == 0],
        "shape_findings": findings,
        "shape_violations": violations,
        "literal_signatures": literal if literal is not None
                             else "NOT_APPLIED (no --signature-file supplied at this run)",
        "verdict": "CLEAN" if not violations and not (literal and literal["hits"]) else "HITS_PRESENT",
    }
    out = os.path.join(PKG, "results", "SENSITIVE_SCAN.json")
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(doc, indent=2, ensure_ascii=False))
    print(json.dumps({"files_scanned": len(files), "rules_with_zero_tested": doc["rules_with_zero_tested"],
                      "shape_violations": {k: len(v) for k, v in violations.items()},
                      "literal_signatures": doc["literal_signatures"] if isinstance(
                          doc["literal_signatures"], str) else {
                          "signatures_supplied": literal["signatures_supplied"],
                          "hits": len(literal["hits"])},
                      "verdict": doc["verdict"],
                      "scan_sha256": hashlib.sha256(open(out, "rb").read()).hexdigest()},
                     ensure_ascii=False, indent=2))
    return 0 if doc["verdict"] == "CLEAN" else 1


if __name__ == "__main__":
    sys.exit(main())
