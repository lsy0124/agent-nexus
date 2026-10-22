# Local release checklist

This procedure creates a reviewable candidate. It does not authorize a push,
visibility change, public release or application submission.

1. Create a non-overwriting checkpoint with `scripts/checkpoint.py`. Verify its
   bundle and archive manifest. Keep the checkpoint private, including its SQLite backup.
2. Review project ownership/license notices and every source change. Run
   `scripts/test.ps1` and isolated browser acceptance. Do not count historical
   logs as current test results or mock calls as live-provider proof.
3. Run `.venv/Scripts/python.exe scripts/release_source.py audit`. This audits
   allowlisted source only and prints paths/rules, never matched secret values.
4. Export with `.venv/Scripts/python.exe scripts/release_source.py export`.
   It creates a new directory under `work/release-candidates/` plus a SHA-256
   manifest. It does not copy `.git`, private state, personal outputs or runtime caches.
5. Review all exported content, licenses, configuration examples, dependencies,
   screenshots and scan findings. Confirm that source links and installation
   instructions work in the candidate. Automated scans are heuristic.
6. Validate locked dependency installation, startup and tests in a clean checkout.
   Record OS/runtime versions and any reused dependency cache or environment.
   Distinguish a clean source check from an independent fresh installation.
7. If creating a candidate Git baseline locally, commit only the audited export.
   Original development history stays in the checkpoint. Do not claim that the
   candidate's first commit represents a long public maintenance history.
8. Obtain separate explicit authorization before uploading or publishing anything.

## Rollback

No release preparation step resets the development repository or deletes operator
state. The checkpoint contains `history.bundle`, `tracked.patch`, `source.zip`,
`task-changes.zip`, the worktree inventory and optional SQLite backup. Verify
manifest hashes, restore to a new recovery directory and review before applying
anything to the original workspace. Stop an owned backend before a database
restore, use SQLite's backup API, preserve the current database and never overwrite
it blindly. Do not upload the private checkpoint to GitHub.

A leaked real key must be revoked/rotated before any history repair. A public
history rewrite would require its own reviewed plan and approval; nothing in
this procedure pushes or rewrites remote history.
