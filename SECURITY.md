# Security

## Scope

This is a single-user, loopback-only workbench. It is not a multi-user server or
an operating-system sandbox. Keep it bound to `127.0.0.1`; do not expose it with
a public reverse proxy, LAN binding or tunnel. Local processes running as your
user are outside the isolation boundary.

Models return bounded text-file proposals. Nexus checks paths and content before
writing to generated Git Worktrees. A Worktree separates code changes, not OS
privileges. Generic shell execution, model-selected hooks, automatic dependency
installation and automatic approval/merge are not granted by this contract.

## Credentials and private state

- Keys are entered explicitly into the backend; the default vault is memory-only.
  Optional Windows DPAPI persistence is tied to the current Windows account.
  Encryption is not permission to publish a ciphertext or database.
- Never commit `work/`, `.env`, credential exports, task logs, local databases,
  handoff packages or screenshots containing private information.
- `.gitignore` does not remove files already in Git history. Use the release
  exporter and review its audit before considering publication.
- A third-party API route is a separate service with separate terms and billing.
  Configure only endpoints you trust and are authorized to use. Gateway checks
  are defense in depth, not DNS pinning or protection from a compromised proxy.
- Remote cancellation may stop the local request without stopping provider work
  or billing. An unknown outcome must be reviewed before another paid attempt.

## Reporting a vulnerability

No public repository or private reporting address has been configured for this
local candidate. Report privately to the project owner using your existing
trusted channel. After publication, use the repository's private vulnerability
reporting feature if enabled. Do not put keys, real task data or an exploit
against someone else's deployment in a public issue.

Include affected version/commit, a synthetic reproduction, impact and proposed
mitigation. No response-time or supported-version SLA has been established.

If a real key was disclosed, revoke/rotate it with its provider, inspect usage,
then review affected Git history, archives and shared copies. Deleting a file
does not revoke a credential.
