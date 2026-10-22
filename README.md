# Agent Nexus

[简体中文](README.zh-CN.md) · [Contributing](CONTRIBUTING.md) · [Security](SECURITY.md)

A Windows local workbench for reviewing and coordinating AI-generated file
changes across explicitly configured model runtimes. React + TypeScript,
FastAPI, SQLite, WebSocket and Git Worktrees.

This is a local engineering candidate, version **0.4.0**. It has no public
release or verified external adoption claimed here. Tests use local fixtures;
a passing test does not establish that a model provider or account works.

## What it does

- Validate structured task plans, dependencies and separate output directories;
  run approved collaborations with bounded concurrency.
- Keep model/runtime/provider declarations separate from observed verification.
- Collect reported token usage; keep missing usage missing and cost estimates
  separate from bills and account balances.
- Require approval for metered requests, preserve interrupted attempts and
  checkpoint uncertain outcomes for review instead of blindly replaying them.
- Validate text-file proposals before writing to generated Worktrees, with
  manual handoff and separate review/merge approval.

A model endpoint is not a general coding agent. Generic model-controlled shell,
installation and automatic merge are not enabled. Worktrees are code isolation,
not an OS sandbox. See [architecture and boundaries](docs/ARCHITECTURE.md).

## Requirements and setup

Windows 10/11, Python **3.12**, Node.js **24**, Git and PowerShell. Commands run
from the repository root. Network access is needed to install locked packages;
no model account or paid service is needed to launch the UI or run offline tests.

```powershell
.\scripts\setup.ps1
# Safe exploration: no real model adapters, in-memory vault.
$env:NEXUS_AGENTS = ','
$env:NEXUS_KEY_VAULT = 'memory'
.\scripts\start.ps1
```

Open http://127.0.0.1:8765 and stop the foreground server with Ctrl+C.
`NEXUS_AGENTS=','` selects no adapter IDs; it is intentional (PowerShell 5.1
can remove an environment variable when assigned an empty string).

For background launch use `scripts/launch.ps1` and stop with `scripts/stop.ps1`.
Use the foreground command when process inspection is restricted. Do not change
system execution policy or run as administrator for this project. If execution
policy blocks scripts, use a policy already approved for your environment.

The `.env.example` file is reference documentation, **not automatically loaded**.
To enable an adapter, set `NEXUS_AGENTS` to its ID before startup, then configure
and approve it in the UI. See [configuration and usage](docs/USAGE.md).
Never copy another application's keys or login files into Nexus.

A source archive needs its own Git baseline for Worktrees. If this checkout is
not already a repository, initialize one locally and commit reviewed source
using your own Git identity. No remote or push is required. A normal Git clone
already has this baseline.

## Verification

```powershell
.\scripts\test.ps1
.\.venv\Scripts\python.exe scripts\browser_test.py
.\.venv\Scripts\python.exe scripts\smoke.py --url http://127.0.0.1:8765
.\.venv\Scripts\python.exe scripts\release_source.py audit
```

The first command runs backend and frontend tests and builds production assets.
Browser acceptance uses an isolated repository/database and an existing browser;
set `NEXUS_BROWSER_PATH` if needed. It must not be aimed at your normal instance.
The smoke command only GETs health/state/page and creates no tasks. The audit is
heuristic and does not replace human review. Do not run `*live*` acceptance
scripts without a separate, explicit provider/cost authorization.

## Source release and private state

`work/` contains private runtime state, including databases, handoffs and optional
DPAPI ciphertext. Historical `outputs/` may contain private experiments. Neither
belongs in a public source archive. A `.gitignore` does not remove tracked files.

Use [the local release checklist](docs/RELEASE.md) and the allowlisted exporter.
Do not publish this development checkout or its Git history wholesale. The
exporter copies current reviewed source, including previously untracked modules,
into a new directory, without modifying the original branches or state.

## Screenshots

[Real UI captures](docs/SCREENSHOTS.md) show isolated synthetic test data,
with zero connected model runtimes; they are not live-provider evidence.

## License

Project-owned source is MIT, as authorized by the project owner. Dependencies
and optional runtimes retain their own terms; see [third-party notices](THIRD_PARTY_NOTICES.md).
No vendor affiliation, subscription entitlement or provider availability is implied.
