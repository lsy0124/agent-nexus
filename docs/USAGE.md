# Configuration and usage

## Process configuration

Set environment variables before starting the backend. They apply to that process;
Nexus does not auto-load `.env`. The example contains no keys.

| Variable | Meaning |
| --- | --- |
| `NEXUS_AGENTS` | Comma-separated enabled runtime IDs. Unset uses the declared adapters. Use `,` for no real adapter (portable across PowerShell versions). |
| `NEXUS_KEY_VAULT` | `memory` by default; `dpapi` only when Windows persistence is explicitly wanted. |
| `NEXUS_SECRET_DIR` | Optional private directory override for the vault. Keep it out of source exports; do not point tests at a real vault. |
| `NEXUS_BROWSER_PATH` | Existing Chromium/Edge executable used by isolated browser acceptance. |

Runtime IDs: `codex`, `zcode`, `workbuddy`, `gemini`, `dsh`, `mimo`,
`zcode-gateway`, `deepseek-gateway`. Enabling one permits discovery; it is not
proof of authentication or successful execution. Gateway IDs are routes, not
separate installed desktop applications.

Default gateway URLs/model IDs in the catalog are historical configuration
examples from local development, not recommended vendors or guaranteed routes.
No key, subscription or model access is bundled. Select the intended provider,
endpoint and model in the UI; approve its terms and cost only if appropriate.
Do not enable OAuth/CLI paths by copying another application's private state.
Optional CLI installers operate in the project; review them before use.

## First task

1. Start in offline exploration mode and run the local environment check.
2. Confirm the repository has a reviewed Git baseline and the task's selected
   input files are safe to send to the chosen provider.
3. Configure one authorized runtime and verify it with an explicitly approved
   minimal request. Ordinary tests never perform this step.
4. Review the task objective, output directory, deliverables, model/provider and
   cost approval before dispatch. Importing a plan does not approve execution.
5. Inspect the returned artifacts and Git diff. Dependency artifact approval and
   final merge are distinct decisions. Keep them explicit.

Missing token counts remain unknown. Unknown route prices are not zero. The
application's budgets are local limits and estimates, not a provider balance.
A cancelled HTTP request may still be billed by the provider.

## Troubleshooting

- Port 8765 occupied: inspect its owner; do not stop an unrelated process. Use a
  separate checkout/port for tests. The browser runner refuses an occupied 5173.
- No Git baseline: initialize a new, reviewed source checkout locally with your
  own identity. Do not initialize or reset a parent repository.
- No browser: set `NEXUS_BROWSER_PATH`; tests do not download a browser.
- Not connected after restart: configuration restoration is separate from fresh
  provider verification. Do not remove the verification or billing gates.
- Interrupted/unknown result: review the checkpoint and provider-side effects
  before explicitly authorizing a new attempt. Do not blindly retry or switch vendors.
- DPAPI unavailable: inspect the reported fallback; keep the service in memory
  mode if persistence cannot be verified for the current Windows user.

Private logs/state live under `work/`. Inspect locally and redact before sharing.
