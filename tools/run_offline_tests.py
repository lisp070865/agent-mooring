"""Execute the offline suite and publish results inside this package.

Writes results/OFFLINE_TEST_RESULTS.json and .md. Pass / fail / not-run are
reported separately and never merged into one green number.

usage: python tools/run_offline_tests.py
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import sys
import time

PKG = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PKG, "src"))
sys.path.insert(0, PKG)

from tests import test_offline as TO  # noqa: E402


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def main():
    declared = len(TO.CASES)
    raw = TO.run_all()
    counts = {k: len(raw[k]) for k in ("PASS", "FAIL", "NOT_RUN")}
    observed = sum(counts.values())
    names = ([c["case"] for c in raw["PASS"]] + [c["case"] for c in raw["FAIL"]]
             + [c["case"] for c in raw["NOT_RUN"]])
    dupes = sorted({n for n in names if names.count(n) > 1})
    unrun = sorted({n for n, _f, _ in TO.CASES} - set(names))
    families = {}
    for bucket in ("PASS", "FAIL", "NOT_RUN"):
        for c in raw[bucket]:
            fam = c.get("family", "core_r39a")
            cell = families.setdefault(fam, {"PASS": 0, "FAIL": 0, "NOT_RUN": 0})
            cell[bucket] += 1
    for fam in families:
        families[fam]["declared_total"] = sum(1 for _n, f, _fn in TO.CASES if f == fam)
    coverage = {
        "cases_declared": declared, "cases_observed": observed, "equals": declared == observed,
        "duplicate_bucket_entries": dupes, "cases_with_no_result": unrun,
        "per_family": families,
    }
    doc = {
        "schema": "awake_kit_offline_results_v1",
        "generated_utc": _now(),
        "python": sys.version,
        "platform": platform.platform(),
        "execution_mode": "OFFLINE_SYNTHETIC_ONLY",
        "network_used": False,
        "mailbox_used": "NONE",
        "scheduler_used": "NONE",
        "client_wake_used": "NONE",
        "software_installed": "NONE",
        "coverage": coverage,
        "counts": counts,
        "results": raw,
        "scope_limits": [
            "These are synthetic, single-process, offline behaviours.",
            "They do NOT demonstrate real mailbox delivery, real scheduled wake, "
            "power-loss recovery, or long unattended operation.",
            "Automatic wake in the source experiment is reported as: executor-side report of success, "
            "compliant trigger-source provenance NOT independently confirmed.",
        ],
    }
    out_json = os.path.join(PKG, "results", "OFFLINE_TEST_RESULTS.json")
    blob = json.dumps(doc, indent=2, ensure_ascii=False, sort_keys=False)
    with open(out_json, "w", encoding="utf-8", newline="\n") as f:
        f.write(blob)
    lines = [f"# Offline synthetic test results", "",
             f"generated_utc: {doc['generated_utc']}", f"python: {doc['python']}",
             f"platform: {doc['platform']}", "",
             f"counts: PASS={counts['PASS']} FAIL={counts['FAIL']} NOT_RUN={counts['NOT_RUN']} "
             f"declared={coverage['cases_declared']} observed={coverage['cases_observed']} "
             f"coverage_equals={coverage['equals']}", "",
             "## Counts per case family (families are reported separately on purpose: "
             "a green old suite is not evidence for a new case)", ""]
    lines.append("| family | declared | PASS | FAIL | NOT_RUN |")
    lines.append("|---|---:|---:|---:|---:|")
    for fam in sorted(families):
        cell = families[fam]
        lines.append(f"| {fam} | {cell['declared_total']} | {cell['PASS']} | {cell['FAIL']} "
                     f"| {cell['NOT_RUN']} |")
    lines += ["", "## PASS"]
    for c in raw["PASS"]:
        lines.append(f"- [{c.get('family','core_r39a')}] {c['case']}")
    lines.append("")
    lines.append("## FAIL")
    for c in raw["FAIL"]:
        lines.append(f"- {c['case']}  \n  error: {c['error']}  \n  tail: {c['traceback_tail']}")
    if not raw["FAIL"]:
        lines.append("- NONE")
    lines.append("")
    lines.append("## NOT_RUN (named reason; absence of a run is not a pass)")
    for c in raw["NOT_RUN"]:
        lines.append(f"- {c['case']}: {c['reason']}")
    if not raw["NOT_RUN"]:
        lines.append("- NONE")
    lines += ["", "## Not covered by any of the above", "- real mailbox delivery",
              "- real scheduled/automatic wake", "- power-loss recovery",
              "- multi-day unattended operation", "- any research or market data path", "",
              "## Scope limits", *[f"- {s}" for s in doc["scope_limits"]]]
    out_md = os.path.join(PKG, "results", "OFFLINE_TEST_RESULTS.md")
    with open(out_md, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    print(json.dumps({"PASS": counts["PASS"], "FAIL": counts["FAIL"], "NOT_RUN": counts["NOT_RUN"],
                      "per_family": families,
                      "coverage": coverage,
                      "results_json_sha256": hashlib.sha256(blob.encode("utf-8")).hexdigest(),
                      "fail_names": [c["case"] for c in raw["FAIL"]],
                      "not_run_names": [c["case"] for c in raw["NOT_RUN"]]},
                     ensure_ascii=False, indent=2))
    return 0 if counts["FAIL"] == 0 and coverage["equals"] else 1


if __name__ == "__main__":
    sys.exit(main())
