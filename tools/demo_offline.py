"""End-to-end demo on synthetic data only: prepare -> controlled fault -> resume.

Everything it writes goes under work/demo/. No network, no mailbox, no scheduler.

usage: python tools/demo_offline.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

PKG = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PKG, "src"))

from awake_kit.config import Config                     # noqa: E402
from awake_kit.identity import build_identity           # noqa: E402
from awake_kit.paths import RootBoundary                # noqa: E402
from awake_kit.runner import TaskRunner                 # noqa: E402


def main():
    doc = json.loads(open(os.path.join(PKG, "data", "synthetic_task.json"),
                          encoding="utf-8").read())
    root = os.path.join(PKG, "work", "demo")
    if os.path.isdir(root):
        import shutil
        shutil.rmtree(root)
    boundary = RootBoundary.create_exclusive(root)
    cfg = Config.load(os.path.join(PKG, "config", "config.example.json"))
    identity = build_identity(doc["identity"]["run_id"], doc["identity"]["task_id"],
                              str(__import__("uuid").uuid4()),  # a fresh synthetic holder
                              "PREPARE").as_dict()
    runner = TaskRunner(boundary, config=cfg, identity=identity)
    claim_key = "K-SYNTHETIC-DEMO"
    log = {"config_sha256": cfg.sha256, "adapter_gates": None}

    from awake_kit.ports import build_ports
    log["adapter_gates"] = build_ports(cfg)["gates"]
    log["prepare"] = runner.prepare(claim_key)

    child = subprocess.run([sys.executable, os.path.join(PKG, "tests", "fake_child.py"),
                            boundary.root, claim_key,
                            str(doc["toy_business"]["reserved_child_exit_code"])],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace")
    log["child"] = {"exit_code": child.returncode, "stdout": child.stdout.strip(),
                    "result_present_before_resume": boundary.exists("state/RESULT.txt")}

    def business():
        return doc["toy_business"]["fixed_text"].encode("utf-8")

    deadline = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                             time.gmtime(time.time() + doc["budgets"]["deadline_seconds"]))
    log["resume"] = runner.recover(claim_key, business,
                                  idempotency_key=doc["idempotency"]["result_key"],
                                  result_rel=doc["toy_business"]["result_rel_path"],
                                  max_resume_attempts=doc["budgets"]["resume_max_attempts"],
                                  deadline_utc=deadline)
    log["second_resume"] = runner.recover(claim_key, business,
                                         idempotency_key=doc["idempotency"]["result_key"],
                                         result_rel=doc["toy_business"]["result_rel_path"],
                                         max_resume_attempts=doc["budgets"]["resume_max_attempts"],
                                         deadline_utc=deadline)
    log["result_on_disk"] = boundary.read_bytes("state/RESULT.txt").decode("utf-8")
    log["guard_table_counts"] = runner.store.counts()
    runner.close()
    print(json.dumps(log, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
