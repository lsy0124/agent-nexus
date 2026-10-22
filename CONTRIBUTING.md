# Contributing

Agent Nexus is a Windows local development workbench. Please describe the
problem, expected behavior and smallest reproduction before proposing a large change.

## Local setup

Use Python 3.12, Node.js 24, Git and PowerShell. Run `scripts/setup.ps1` from
the repository root. Dependencies and generated state stay inside the checkout.
No AI account is needed for the ordinary test suite.

Run `scripts/test.ps1` for backend tests, frontend tests and the production build.
For browser integration changes, run `.venv/Scripts/python.exe scripts/browser_test.py`
after building. Set `NEXUS_BROWSER_PATH` to an existing Chromium/Edge executable
if discovery fails. Browser tests create an isolated Git repository and database.
Never target an operator's normal service or database with mutation tests.

## Changes and review

- Add regressions for reproducible behavior or security failures, using injected
  transports and synthetic data. Ordinary tests must not contact model services.
- Preserve cost approval, input/output path checks, recovery holds and separate
  approval for merging. A provider response is untrusted input.
- Distinguish declared capabilities, locally discovered software, successful
  model calls and verified task deliverables in code, UI and documentation.
- Record missing token usage as missing. Prices and costs are estimates, not bills.
- Keep credentials, private tasks, screenshots of personal data and raw provider
  responses out of commits and reports. See SECURITY.md.
- Preserve existing user work. Never use test cleanup to remove an operator's
  tasks, branches, worktrees or database.
- PowerShell scripts containing non-ASCII text require UTF-8 **with BOM** for
  Windows PowerShell 5.1. Keep changes compatible with the existing launchers.

Describe what changed, why, the exact test command/result, and remaining limits.
Do not claim real provider availability based on a mock or a model list.

Contribute only code you have the right to provide under the MIT license.
Retain attribution and the original terms for third-party code; disclose copied
or adapted code and its source. No CLA or external contact address is implied.
