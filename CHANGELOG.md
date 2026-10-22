# Changelog

This file records reviewed engineering changes. Earlier local development
reports contain historical experiments, not a public release history.

## Unreleased — 2026-10-22

- Prepare a local source candidate with MIT licensing, bilingual getting-started
  documentation, contribution/security guidance and reproducible validation steps.
- Preserve existing adapters, task planning, bounded parallel workflows, usage,
  budgets, manual handoff, checkpoints and Worktree review.
- Add an explicit source export and audit workflow that excludes operator state
  and historical private evidence.
- Reject mixed public/private DNS answers and recheck gateway destinations
  before sending a key. This is defense in depth, not transport-level DNS pinning.
- Make the health inspection script GET-only; it no longer creates tasks,
  Worktrees or approval decisions in an operator's database.
- Keep browser acceptance output and vault paths inside the isolated run,
  instead of overwriting historical evidence or inheriting a private vault path.
- Pin certifi 2026.10.19 to remove the affected emSign roots (GHSA-6q48-6c7w-9gw2,
  also tracked as PYSEC-2026-4320). Do not disable TLS certificate verification.

No public release, external adoption or live-provider validation is implied.
