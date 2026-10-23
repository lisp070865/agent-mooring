# Security notes

## Default posture

Nothing in this package can reach the outside world as shipped.

* `config.example.json` sets `allow_network: false` and every adapter
  `enabled: false` with empty settings.
* `build_ports()` returns `DisabledMail` / `DisabledWake` / `DisabledSchedule`
  regardless of configuration. A real transport must be constructed and injected
  by the integrator, in code a reviewer can see.
* A disabled adapter **raises**. There is no silent no-op path, because a
  no-op that returns success is how "we thought it was delivered" happens.
* `Config` never touches environment variables, never looks for a keyring, never
  enumerates accounts, never reads a browser profile, a private scheduler store,
  or a session log. It records `env_credentials_read=False` and
  `accounts_auto_discovered=False` so an audit can see the claim was made by the
  object itself, not by a comment.
* Case `T20` enforces this structurally: it parses every module with `ast`,
  allowlists which import roots are permitted, denies dotted attribute chains
  (`os.environ`, `socket.*`, `urllib.*`, `http.*`, `ssl.*`, `keyring.*`,
  `winreg.*`, `requests.*`, `subprocess.*`, `ctypes.*`, `shutil.*`), and runs the
  suite under a `socket` tripwire. It also runs the same scanner against a file
  that *does* use those APIs, so a silently-dead scanner cannot pass.

## Rules that exist because of real failure modes

1. **Do not infer identity from names.** A mailbox address, alias, message id,
   thread id, directory name, window title, or "the session that was busiest
   recently" is not proof of who is executing. Wrong identity binding lets one
   executor claim another's lease, which is exactly how double execution starts.
2. **Do not treat an accepted response as delivered.** `accepted` means the
   request entered some queue. Verification requires reading the object back and
   comparing hashes; `outcome.py` models the three states separately
   (`SENT`, `DELIVERED_VERIFIED`, `OUTCOME_UNKNOWN`).
3. **Never auto-resend on timeout.** A timeout after possible delivery is
   `OUTCOME_UNKNOWN`. Resending produces two deliveries and the second one was
   never planned. Reconcile against evidence instead; a second attempt needs an
   explicit operator flag.
4. **Never steal a lock because time passed.** An expired lease means unknown
   external effects, not a free ticket. The only way to clear one is
   `force_open_for_operator`, which requires the lease token.
5. **Do not kill processes to resolve contention.** Nothing here signals other
   processes. Contention is resolved by refusing to run.
6. **Do not use a private log format as a state machine.** Wake confirmation must
   come from the woken side writing its own record. Third-party log parsing
   breaks on the next runtime update and silently produces false positives.
7. **Keep artifacts inside one declared root.** Every write is checked against
   the root; junctions/symlinks inside the path are refused because they defeat
   exclusivity.

## If you integrate a real adapter

* Add authentication of the *sender* before you act on any inbound instruction.
* Bind inbound instructions to the identity you already hold, field by field.
* Persist an intent **before** the outbound call, including the payload hash.
* Keep an append-only journal; single-slot platform fields get overwritten.
* Log hashes and sizes, not payload bodies, unless you have decided the body is
  safe to persist. This package stores payload bytes only when you hand them to
  `commit_result`, which is your artifact decision, not a default.
* Re-run `tools/scan_candidate.py` on whatever you are about to publish, after
  stripping generated `work/` output.

## Reporting

The R43 candidate source was published on GitHub on 2026-10-23. No dedicated private
security-reporting channel is configured or documented for this project yet. Do not
post vulnerability details, credentials, or other sensitive information in public
issues or discussions. Wait for an explicitly identified private reporting channel
before sharing sensitive details. The licence and attribution are recorded in
`LICENSE`, and unresolved provenance questions are listed in `PROVENANCE.md`.
