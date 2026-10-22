# Third-party notices

Agent Nexus project-owned source is licensed under MIT. The project owner
confirmed the right to publish that source and selected MIT on 2026-10-22.
This statement does not relicense dependencies or establish third-party rights.

Dependencies are installed separately, not vendored into the source candidate.
The frontend lockfile, backend requirements lock and optional CLI lockfiles retain
exact versions and integrity metadata. Do not include node_modules, Python
site-packages, client profiles or model credentials in a source release.

## Inventory

`docs/DEPENDENCIES.json` records package/version/license metadata from the checked
in locks and installed Python distribution metadata. Missing metadata means
unconfirmed, not public domain. A license label is not a substitute for reading
the corresponding upstream license before redistributing its code or binaries.

Core Python libraries include FastAPI (MIT), HTTPX (BSD-3-Clause), Uvicorn
(BSD-3-Clause), Starlette (BSD-3-Clause) and Pydantic (MIT). The frontend uses
React/React DOM (MIT), lucide-react (ISC), Vite, TypeScript, Vitest and Playwright
under their respective original licenses. Optional Codex and Gemini CLI packages
are separate runtimes; their licensing and service/account terms remain separate
from this project's MIT grant. Their presence in a lockfile is not endorsement,
a bundled subscription, or evidence of authorization.

Catalog URLs and model/provider declarations are compatibility references.
Vendor names and marks belong to their owners. Third-party gateway presets are
historical examples, not a recommendation or a guarantee of lawful access.
No competitor binaries, proprietary app source, private data or market data are
part of the intended source candidate.

If adapted code is identified during review, preserve the original copyright
and license and add its precise source/version here before publication.
