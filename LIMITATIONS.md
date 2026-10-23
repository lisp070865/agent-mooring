# Limitations — read before trusting anything here

## What was actually demonstrated

The suite in this package runs offline, in a single process plus one deliberately
short-lived child process, against scratch state roots created under `work/`, with
**no network of any kind**. It demonstrates behaviour of the code in this package,
on one machine class, for synthetic identifiers.

The case families are counted separately, **by the executor who assembled this
candidate**: 20 + 10 + 7 = 37 cases, reported by that executor as all passing,
with 5 of 5 mutation controls passing. This package ships no run artifacts, so
**nothing here has been reproduced by anyone else**: a recipient performing a
static review has verified only that the source says what it says. Re-running the
tools is the first independent act.

The closing round that produced this package ran **no** case and **no** mutation control:
it performed static checks only — byte-identity against the previous candidate, a
content/path scan over the shipped directory, a configuration-disabled check, and
manifest plus archive verification by reading the finished archive back. The counts above
therefore remain the earlier executor's self-report, not this round's measurement.

The mutation controls re-introduce known pre-fix defects and require each scenario
to go wrong. They differ in how badly: one control showed a defect that, once the
new gate is removed, is still caught later by a store-level guard — the difference
the gate makes there is a named refusal with zero state movement instead of a
later error that had already consumed a retry attempt. Another control showed a
defect that does reach the business step. Do not read the controls as saying every
hole is the same size.

## What was NOT demonstrated

| Claim | Status | Why |
|-------|--------|-----|
| Real mailbox delivery works | **NOT RUN** | No mail adapter is wired; `DisabledMail` raises by design. The fakes only record hashes. |
| A scheduled/automatic wake works | **NOT RUN in this copy** | No scheduler or client adapter is wired. The source experiment exercised one, under the label below. |
| That wake's trigger source was independently confirmed | **NOT INDEPENDENTLY CONFIRMED** | Status of the source experiment: *executor-side report of success; compliant trigger-source provenance not independently confirmed*. |
| Recovery after power loss | **NOT TESTED** | A child exiting with a reserved code is a *clean* death. Crashes, kills, reboots, and filesystem-level truncation are different failure modes. |
| Long unattended operation | **NOT TESTED** | No multi-hour, multi-day, or repeated-callback run was performed for this copy. |
| Multi-host or multi-user contention | **NOT TESTED** | SQLite + `BEGIN IMMEDIATE` + `O_EXCL` give single-host mutual exclusion. They are not a distributed lock. |
| Byte-for-byte verification of an outbound message body | **NOT AVAILABLE** | Real transports truncate or normalise bodies. Verify payloads by hash; never claim body-level verification from a listing API. |
| "Exactly one run ever happened" | **NOT PROVABLE FROM SINGLE-SLOT FIELDS** | If your scheduler exposes only `lastRunAt`/`lastRunStatus`, a later run overwrites the only evidence. Prefer append-only journals (as `claim.py` does) over platform slots. |
| Whether every right needed to publish is cleared | **OPEN QUESTION FOR THE OWNER — not a missing licence** | This package carries an MIT grant with the attribution the owner chose (`LICENSE`); nothing here is missing a licence. What remains the owner's judgement is the acknowledged design derivation recorded in `PROVENANCE.md` and the unknown earlier sources of the internal chain. Assembling or packaging this candidate is not itself a grant, and publishing stays the owner's decision. A zero in a line-comparison table is not a legal finding, and a search that found nothing is not proof that something is new. |

## Known design limits

* `claim.py` refuses re-dispatch after an expired lease and refuses to escape a
  blocking status by changing holder, mode, or fingerprint. That is deliberately
  annoying: it forces a human decision where external effects are unknown. If you
  need automatic re-dispatch, add an idempotency guarantee on the external side —
  do not relax this gate instead.
* `checkpoint.py` requires `run_id`/`task_id` to be present *and* well-formed on
  both sides; two missing fields are not treated as equal. `claim.py` carries a
  matching guard so a missing field is a named refusal rather than a raw `KeyError`.
* `paths.py` bounds growth (2 MiB per file, 128 MiB per state root) but performs no
  retention or compaction. WAL sidecars (`-wal`, `-shm`) are not cleaned up.
* `identity.py` validates *shape*. It cannot prove that a holder id came from an
  authoritative issuer; `self_reported` and `provenance` exist precisely so a
  self-issued id cannot be laundered into "verified by the provider".
* Checkpoints bind `run_id`/`task_id`, not the holder: a different holder may
  resume, and the lease is what stops two holders at once.
* Nothing here authenticates an inbound sender. If you wire a real transport, you
  must add authentication and decide who owns replay protection.
* Gate refusals do not consume the resume budget and are not journaled by the
  store; the exception you receive is the record. Attempted resumes are journaled.

## Evidence hygiene this package follows

* Numbers in generated reports are computed by the tools, not typed by hand.
* Pass / fail / not-run are separate buckets; a not-run item states its reason.
* A manifest lists every shipped file and deliberately excludes its own digest.
* The sensitive-content scan runs over this directory only; its rules are
  shape-based, so the scanner never needs to embed a real secret to look for one.
* Generated artifacts (`work/`, `results/`) are not shipped, so a reviewer cannot
  mistake this package's own test output for an external attestation.

## Scope of this candidate

Packaging this candidate is a local preparation act. It is **not** approval to
publish, **not** a licence grant, and **not** acceptance for production use.
