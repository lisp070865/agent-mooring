# AgentMooring — minimal control-loop primitives (candidate, not yet published)

Public project name: **AgentMooring**. The importable package is still `awake_kit`
(`src/awake_kit/`, project name `awake-kit`): renaming code, tests, or configuration was
out of scope for this round, so the repo name and the module name differ on purpose and
are documented in `PROVENANCE.md`.

Generic building blocks for one narrow problem: letting an automated executor
claim a unit of work, die in the middle, and be resumed **without** doing that
work twice, **without** talking to anybody it wasn't told to, and **without**
deciding on its own that a timeout means "retry".

This is a **candidate** for open-source release. It ships generic code, synthetic
example data, and an empty configuration. All outward-facing adapters are disabled
by default. It is not a production agent runtime, not a scheduler, and not a
mailbox client. It has not been published. The project owner has chosen the MIT licence
and the attribution name recorded in `LICENSE`; choosing a licence is the owner's
decision about *this* package and is not a warranty that the rights of everything its
design drew on have been cleared — see `PROVENANCE.md`.

## Quickstart (offline, no installs, stdlib only)

```
mkdir -p work results                     # tools write into these two directories
python tools/run_offline_tests.py         # behaviour suite (offline, synthetic only)
python tools/mutation_control.py          # re-introduces known pre-fix defects
python tools/demo_offline.py              # prepare -> controlled fault -> resume -> reuse
python tools/scan_candidate.py            # shape scan; --signature-file for a literal denylist
python tools/build_manifest.py            # results/MANIFEST.sha256.{csv,json}
python tools/build_handoff_zip.py --out ../../agentmooring.zip   # archive + sidecar
```

This package intentionally ships **no run artifacts**: the test report, mutation
report, scan output and demo output are produced by running the tools above. The
executor who assembled this candidate reported **37 offline synthetic cases
passing (3 families: 20 + 10 + 7) and 5/5 mutation controls passing**; that is the
author's own run record, not an independent re-run — a recipient doing a static
review has reproduced nothing until they run the tools. Manifest files are
transport *container* members of the archive and are not listed inside
themselves, because a file cannot carry its own digest.

`build_handoff_zip.py` refuses to write inside the package, recomputes every
digest by reading the finished archive back, and never stores the archive's own
hash inside it.

## The rules this package encodes

| # | Rule | Where |
|---|------|-------|
| 1 | A name is not an identity. Mailbox, alias, message id, thread id, directory name, window title, "most recently active", and display names are rejected as executor identity. | `identity.py` |
| 2 | Identity comparisons are field by field, never as an unordered set of values. | `identity.py`, `checkpoint.py` |
| 3 | One unit of work has one claim. A second `reserve` on the same key is refused, and a terminal row is *reused*, not re-run. | `claim.py` |
| 4 | An active lease blocks a second writer. An **expired** lease blocks it too — unknown external effects are an operator decision, and elapsed time never grants the lock. A row in a *blocking* status (`OUTCOME_UNKNOWN`, `BLOCKED`, `DEADLETTER`, `BLOCKED_WAIT_USER`) cannot be laundered by changing holder, mode, or fingerprint: only an evidenced `resolve_blocked` decision can leave it. | `claim.py`, `recovery.py` |
| 5 | Identity, stage, and claim/idempotency correspondence are validated **before** any business call, before any reuse or commit, and before any counter or lease moves — including on the "same stage already recorded" path. Required fields must be *present and well-formed on both sides*: two missing `run_id`s are not an equality match. Wrong or missing identity, mismatched binding, or a missing required checkpoint means zero business calls and zero rewrites. A legitimate holder change is not a task change. | `checkpoint.py`, `recovery.py` |
| 5b | Failure is classified by where it happened: before the business step `FAILED_RETRYABLE`; at or after it, persistently `OUTCOME_UNKNOWN` plus an evidence file, so automation stops instead of guessing. | `recovery.py`, `claim.py` |
| 6 | A result is committed exactly once: `O_EXCL` file plus a row guarded by `RAISE(ABORT)` triggers, so an accidental UPDATE/DELETE is impossible rather than merely discouraged. Later attempts get `REUSED` and `execution_count` stays 1. | `commit.py`, `claim.py` |
| 7 | An external call whose outcome is unknown is a terminal state for automatic policy. No resend without an explicit operator decision; the recommended path is reconcile-against-evidence. An accepted response is not "delivered" — `VERIFIED` needs a hash-matched read-back. | `outcome.py` |
| 8 | Stop flags, config identity, deadlines, and attempt budgets are checked *immediately before* every side effect, so "stopped" means the counters did not move, not that we stopped politely. | `stop.py`, `recovery.py` |

## Layout

```
src/awake_kit/      the library (13 modules, stdlib only)
tests/              offline synthetic suite + a controlled child process
tools/              suite runner, mutation controls, demo, scanner, manifest builder, handoff-zip builder
config/             empty, disabled-by-default example configuration
data/               one synthetic task description (invented identifiers)
```

Running the tools creates `work/` (scratch state roots, including SQLite files)
and `results/` (reports and manifests). Neither is part of this package, and both
should be deleted or regenerated before you redistribute.

## Using it

```python
import sys, uuid; sys.path.insert(0, "src")
from awake_kit.config import Config
from awake_kit.identity import build_identity
from awake_kit.paths import RootBoundary
from awake_kit.runner import TaskRunner

boundary = RootBoundary.create_exclusive("./mystate")     # must not pre-exist
cfg      = Config.load("config/config.example.json")       # all adapters disabled
ident    = build_identity("RUN-X", "TASK-X", str(uuid.uuid4()), "PREPARE").as_dict()
runner   = TaskRunner(boundary, config=cfg, identity=ident)

runner.prepare("K1")                                       # claim + checkpoint
result   = runner.recover("K1", lambda: b"RESULT-ONCE", idempotency_key="RES-K1")
again    = runner.recover("K1", lambda: b"RESULT-ONCE", idempotency_key="RES-K1")
assert again["verdict"] == "REUSED_EXISTING_RESULT"        # nothing ran twice
```

Wiring a real transport is intentionally **not** a configuration step: pass your
own object that satisfies `MailPort` / `WakePort` / `SchedulePort`. `build_ports()`
always hands back the disabled implementations, so a filled-in config cannot
silently acquire network behaviour.

## Reading order

`README.md` → `LIMITATIONS.md` → `SECURITY.md` → `PROVENANCE.md` → `LICENSE`.

## Status

`CANDIDATE_NOT_RELEASED`. A private preparation repository exists for this work; this
candidate has not been published from it, and nothing has been pushed publicly. The
licence and attribution were set by the project owner (`LICENSE`); the
open questions listed in `PROVENANCE.md` are not closed by that act.

Two kinds of evidence are deliberately kept apart:

* **Historical, self-reported.** 37 offline synthetic cases (families of 20 + 10 + 7) and
  5 mutation controls were run and reported as passing by the executor who assembled the
  earlier candidates, and are quoted here from those reports.
* **This round, static only.** Byte-identity against the previous candidate, a
  sensitive-content and path scan over the shipped directory, a configuration-disabled
  check, and manifest/archive verification by reading the finished archive back. **No test
  or mutation run was re-executed in this round**, so the numbers above remain
  self-reported rather than independently reproduced.

Packaging this candidate is not publication, not production acceptance, and not a
guarantee that every right needed for release has been cleared.
