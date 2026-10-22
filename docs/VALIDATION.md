# Validation record — 2026-10-22

This is a local validation record, not public CI history or live-provider evidence.

| Check | Result and scope |
| --- | --- |
| Backend full suite | 532 passed, 1 skipped; current Windows user context; 533 collected |
| Frontend unit tests | 15 passed; 3 test files |
| Production build | TypeScript and Vite 8.3.4 passed |
| Clean Python installation | 28 locked packages installed into a new Python 3.12 venv from locally downloaded PyPI wheels; pip check passed |
| Clean frontend installation | npm ci --offline --ignore-scripts into a clean source export; 15 tests and production build passed |
| Isolated browser acceptance | 36 checks passed against the real UI/backend with synthetic fixtures and no model calls |
| Post-certifi HTTP/export regressions | 77 passed, 1 skipped; after certifi 2026.10.19 installation |
| Frontend advisory query | npm official registry reported 0 advisories for the frontend lock |
| Python advisory query | OSV reported 0 matches for the updated 28-package lock |

The full suite ran before updating the local certifi installation. After the
update, the HTTP/gateway/export tests and isolated browser suite were rerun.
A new independent venv installed the updated lock successfully. The archived
local logs distinguish these steps; no full-suite pass under the old dependency
is silently described as a full-suite run under the new dependency.

The initial restricted-context baseline had 515 passes, 3 DPAPI failures and one
DNS skip. Those three tests passed in the current Windows user context, followed
by the full user-context suite above. Synthetic secrets only were used. DPAPI
availability for a different user/machine is not established.

The skipped test concerns an unresolvable hostname: this machine's resolver
answers every name, so the expected DNS-failure branch is not observable here.
Mocked DNS regressions cover mixed public/private answers and destination changes
without contacting any provider. One existing Starlette TestClient deprecation
warning remains; the dependencies and tests currently work.

The certifi upgrade addresses GHSA-6q48-6c7w-9gw2 / PYSEC-2026-4320. The installed
wheel SHA-256 matches PyPI metadata. Comparing the old and new vendor bundles
shows the emSign ECC Root CA - G3 certificate removed; three other emSign roots
remain. No manual trust-store edits or disabled TLS verification were used.
See the [upstream advisory](https://github.com/certifi/python-certifi/security/advisories/GHSA-6q48-6c7w-9gw2).

Advisory results are database snapshots, not proof of absence of vulnerabilities.
Only the frontend and backend lock were queried; optional separately installed
CLI runtime trees were not fully vulnerability-audited and were not installed
for this validation. GitHub Actions has been authored but not executed remotely.

Screenshots in SCREENSHOTS.md show real application rendering of fixture state.
No provider availability, subscription entitlement, external user count or
performance claim follows from this validation.
