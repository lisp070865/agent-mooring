# Provenance — how this code came to be, and what is not known

## Project naming

The public project is named **AgentMooring**. The importable Python package inside it is
still named `awake_kit` (directory `src/awake_kit/`, `pyproject.toml` name `awake-kit`),
because renaming code, tests, or configuration was explicitly out of scope for this
preparation round. The two names refer to the same thing; a later release may align them.

## What ships, and what is merely installed

These questions are kept apart on purpose.

* **Distribution content.** No third-party dependency distributions or vendored libraries
  are included in this archive. Imports are limited to the Python standard library and
  this package. This inventory does not determine whether implementation text or design is
  derived from other sources, or whether upstream notices apply; the derivation record
  below addresses that separately.
* **Machine environment.** The workstation that assembled this package also has local
  virtual environments containing third-party libraries (HTTP clients, ASGI servers,
  linters, etc.). Those are **not bundled, not referenced by this code, and not part of
  this licence grant**; mentioning them here only prevents a reader from mistaking an
  installed library for a dependency of this project.
* **Derivation** is a separate matter from dependencies and is described below. Reading
  and re-implementing the design of another implementation is not the same as bundling a
  library, and neither question answers the other.

## How the work came to exist (roles, not names)

The internal implementation that this package's design draws on did not appear in one
step. Its formation chain, as reported by the project owner who holds the artifacts:

1. The **project owner** stated the requirement for a multi-agent collaboration setup.
2. An **external architecture assistant** (a conversational AI service used by the owner)
   delivered a first specification and orchestrator design, versioned V1.0 and then V1.1,
   on 28 September. The owner retains the digests of those two delivery packages in a
   private ledger; they are not printed here because a hash of a private artifact is not
   public information about this code.
3. A **local execution agent** deployed V1.0/V1.1 on the machine and patched it in place.
4. An **independent review agent** audited the running system and raised findings.
5. The **local execution agent** and a **second execution agent** extended it: a generic
   executor pool, delivery ledger, receiver queue, audit actuator, and a written
   collaboration protocol that was revised across drafts.

That chain is the honest reason this package is described as *derived by design*: the
state store, delivery ledger, executor identity model and checkpoint mechanism that this
package re-implements are products of steps 2–5 above. The byte-level chain is **not**
established: the working file read during preparation is a later revision, not the content
delivered in V1.0/V1.1, so no claim of a complete file-by-file evolution from those
packages to the current internal code is made here or in this package's README.

## Modules carry no version declaration; protocol citations are a separate claim

Two facts that must not be conflated:

* **Version declaration.** The internal files that were read carry **no** version
  identifier of their own: no `VERSION`, no `__future__.annotations`-adjacent version
  string, no `__version__`. Their identity at review time was therefore recorded as
  content digest plus file timestamp in the private ledger. Any statement that a given
  internal file "is version V1.1" would be an inference from surrounding documents, not
  from the file itself, and is not asserted here.
* **Normative citation.** Some internal components cite a *written collaboration
  protocol* in their module docstrings, naming lettered sections. That is a citation of a
  specification, not a module version. Two separate things must not be merged here: this
  round could **not find that cited protocol revision on the machine it searched**, while
  the project owner has since **recovered the original from cloud storage** and holds it
  outside this workspace. The owner's copy was not obtained or read by this round, so the
  mapping between the cited lettered sections and the protocol document's own numbering
  remains **unconfirmed** — recorded as such, not guessed at, and no section was
  reconstructed to make a citation look right. Unresolved items are listed at the end of
  this file.

## Design elements taken from the internal implementation

| In this package | What was taken | Kind |
|---|---|---|
| `claim.py` | reservation flow (open transaction → read row → decide → upsert), "never auto-take-over a dispatching row", expired-dispatch means unknown effects and needs an operator, attempt/backoff counters, lease owner/until/token/generation, the status word set | structure + names |
| `outcome.py` | rule that an already accepted delivery is never re-sent and that a timeout after a provider id must be reconciled, not retried; append-only history; the `SENT`/`VERIFIED`/`RECORDED`/`FAILED` vocabulary | rule + names |
| `identity.py` | token shape regex source text (verbatim string), task identity stable across executor instances, the list of things that are *not* identities (window title, recency, display name) | text (one literal) + rule |
| `checkpoint.py` | a checkpoint mechanism exists internally: it validates identity before writing, records the last completed action and the next action, numbers records monotonically, and refuses to overwrite an existing checkpoint ("a checkpoint is an identity, not a scratch file") | structure + concept |
| `paths.py`, `config.py`, `ports.py`, `stop.py`, `commit.py`, `recovery.py`, `runner.py` | written for this package; they also reuse the status vocabulary above through `claim.py` | mixed |

Textual overlap measured against the internal files that were read, at two line-length
thresholds, with the boilerplate `from __future__ import annotations` excluded:

* **Above 25 characters: no identical lines.**
* **Above 12 characters: several identical short lines were found**, and they are listed
  rather than smoothed over — a row-cast idiom (`d = dict(row)`), a token-generation line
  (`token = uuid.uuid4().hex`), one SQLite column declaration line, and ordinary
  `import sqlite3` / `import hashlib` / `except Exception:` skeleton lines.
* Five lines were textually identical at drafting time (two expressions, one SQL column
  clause, one generation expression, one comprehension) and were rewritten to equivalent
  forms. Those cases, and the short lines above, are catalogued line-by-line in the
  project's private provenance ledger, which is deliberately **not** shipped.

## What a search result can and cannot show

* A comparison finding no identical lines shows only that the compared files contain no
  identical lines **under the stated length threshold and exclusions**. It does not show
  the absence of derivation, and it does not clear rights.
* Failing to find a mechanism's name or string in the files that were read shows only
  that it was not found **there**. It is not evidence that the mechanism is novel to this
  package. Statements in `SECURITY.md` / `LIMITATIONS.md` about what this package adds are
  written as design choices, not as priority claims.
* Conversely, the absence of a licence file, of version-control metadata, or of
  attribution markers in the internal chain is a statement about what could be searched,
  not proof that anything is original, unencumbered, or freely licensable.

## Known-unknowns, kept open

1. Whether the internal implementation chain (and the written protocol it implements)
   drew on any external source. No evidence either way was obtainable.
2. The licence status of the internal files and of the cited protocol revisions. This is a
   statement about *those* artifacts, not a claim that this package lacks a licence: this
   package ships an MIT grant (see `LICENSE`).
3. The cited protocol section numbering. This round could not find that revision on the
   machine it searched; the owner has separately recovered the original from cloud storage
   and it was not obtained or read here, so **the section correspondence stays unconfirmed**.
   It is deliberately not guessed at and not reconstructed.
4. Files in the internal tree that were **not** read line by line (audit actuator, audit
   receiver, transport adapter, GUI writer, and others), and therefore were not compared.
   "No overlap found" is bounded to what was read.
5. Whether the shipped test sources' internal round identifiers matter to you; they were
   left untouched because code changes were out of scope.

## Authorship

The code and prose in this package were written by the project owner's assistant session on
the owner's machine, under the owner's direction, and are offered under the MIT licence in
`LICENSE` with the copyright holder named there. This document describes derivation and
search results; it is not, and must not be quoted as, a legal opinion or a warranty that
all rights are clear.
