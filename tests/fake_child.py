"""Synthetic child process used by the offline fault-injection test.

It writes a durable checkpoint and a fault marker inside the state root it is
handed, asserts that no result file exists yet, then exits with a reserved
non-zero code. It never kills anything, never opens a socket, never reads the
environment for secrets.

usage: python tests/fake_child.py <state_root> <claim_key> [reserved_exit_code]
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from awake_kit.paths import RootBoundary          # noqa: E402
from awake_kit.runner import TaskRunner           # noqa: E402
from awake_kit.identity import build_identity     # noqa: E402
import uuid                                       # noqa: E402


def main(argv):
    if len(argv) < 3:
        print(json.dumps({"error": "ARGS_REQUIRED state_root claim_key [exit_code]"}))
        return 2
    root, claim_key = os.path.abspath(argv[1]), argv[2]
    code = int(argv[3]) if len(argv) > 3 else 43
    boundary = RootBoundary(root, expected_entries=())
    identity = build_identity(run_id="RUN-SYNTHETIC-0001", task_id="TASK-SYNTHETIC-0001",
                              holder_id=str(uuid.uuid4()), stage="PREPARE")
    runner = TaskRunner(boundary, identity=identity.as_dict())
    reserved = runner.fault_child_entry(claim_key, reserved_exit_code=code)
    print(json.dumps({"child_pid": os.getpid(), "reserved_exit_code": reserved,
                      "checkpoint_stage": runner.ck.read()["stage"],
                      "result_exists": boundary.exists("state/RESULT.txt")}))
    runner.close()
    return reserved


if __name__ == "__main__":
    sys.exit(main(sys.argv))
