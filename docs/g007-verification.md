# G007 Verification Evidence

- JSON logs redact secret-bearing keys, credential URLs, email, phone, and Discord identifiers.
- Component health, aggregate readiness, scheduler freshness, counters/timers, graceful shutdown,
  expired-lease recovery, denylist reconciliation, and embedding invariants are tested.
- Delete/denylist propagation deactivates chunks/vectors and source replay hides deleted URLs.
- Backup tooling creates restrictive custom-format data archives with 7-day daily and 4-week
  weekly retention; vectors are optional.
- `scripts/verify_g007_restore.sh` passed an actual empty temporary DB migration, data restore,
  active-revision check, and stale-lease reconciliation rehearsal.
