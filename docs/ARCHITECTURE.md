# Architecture and boundaries

`backend/nexus/main.py` composes the local API, adapters, storage, policy,
planning, collaboration and review services. `frontend/src/` implements the
console; it consumes state/events rather than inventing execution results.

| Component | Responsibility |
| --- | --- |
| `adapters.py`, `registry.py`, `catalog.py` | Independent runtime loading, capability declaration and observed verification |
| `planning.py`, `orchestration.py` | Validated plans, dependencies, scopes, bounded concurrency and review gates |
| `usage.py`, `quota.py`, `budget.py`, `policy.py` | Reported usage, separately typed quotas, estimates and dispatch permission |
| `reliability.py`, `storage.py` | Persistent attempts, checkpoints, events and recovery holds |
| `file_changes.py`, `worktrees.py`, `review.py` | Checked text writes, isolated Git branches, manifests and separate merge decisions |
| `manual_handoff.py` | Explicit human-mediated import and review, not automated control of another app |
| `keyvault.py` | Memory default; explicit Windows DPAPI persistence |
| `frontend/src/liveConnection.ts` | UI heartbeat, reconnect/backoff and event resynchronization; never task replay |

The gateway file-plan contract is bounded JSON. Nexus validates destinations,
selected input hashes and output size before writing. Model-supplied instructions
cannot approve payment or expand an output scope. Existing tasks and failed
attempts remain evidence; recovery does not erase a previous failure.

Local tests inject model transports and synthetic tasks. Browser screenshots of
these tests show the real application with fixture data, not a successful model
call. Provider prices, capability declarations and historical gateway examples
must be checked against the provider before real use.

No exactly-once remote execution guarantee is made: a connection can fail after
remote work has started. Startup pauses/reconciles uncertain work instead of
assuming it is safe to repeat. Review the checkpoint before another attempt.
