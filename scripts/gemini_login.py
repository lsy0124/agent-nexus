"""Human-operated official Google login; OAuth input never passes through Nexus.

Manual mode avoids Gemini CLI 0.64.0's early browser-consent listener error.
Both official login modes still need a real terminal: piping them is rejected.
"""
import argparse
import asyncio
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from nexus.gemini_cli_adapter import GeminiCLIAdapter


def terminal_is_interactive():
    """Match the official CLI's stdin/stdout TTY requirement, without reading input."""
    return bool(sys.stdin and sys.stdout and sys.stdin.isatty() and sys.stdout.isatty())


def terminal_instructions():
    script = str(ROOT / "scripts" / "gemini-login.ps1").replace("'", "''")
    print("Google login needs a real interactive terminal; no authentication was started.", file=sys.stderr)
    print("Open Windows Terminal / PowerShell, then run:", file=sys.stderr)
    print(f"& '{script}' -NewWindow", file=sys.stderr)
    print("Do not pipe this command or run it through Nexus task logs.", file=sys.stderr)


async def main(mode="manual", check=False):
    if mode not in ("manual", "browser"):
        raise ValueError("Unsupported official login mode")
    if not check and not terminal_is_interactive():
        terminal_instructions()
        return 2
    adapter = GeminiCLIAdapter(ROOT)
    state = await adapter.detect()  # Public --version/--help only; no auth check.
    if check:
        print(json.dumps({"cli_installed": state["installed"], "cli_version": state["cli_version"],
                          "cli_compatible": adapter.compatible, "interactive_terminal": terminal_is_interactive(),
                          "default_login_mode": "manual", "model_request_started": False,
                          "account_login_verified": False}, ensure_ascii=True))
        return 0 if adapter.compatible else 1
    if not adapter.compatible:
        print("Pinned official Gemini CLI 0.64.0 metadata check failed; login was not started.", file=sys.stderr)
        return 1
    adapter.configure_subscription(approved=True)
    spec = adapter.login_spec()
    environment = dict(spec["environment"])
    if mode == "manual":
        environment["NO_BROWSER"] = "true"
    else:
        # Empty/false strings still count as enabled to the official CLI.
        environment.pop("NO_BROWSER", None)
    print("Sign in with your subscription's Google account on Google's official page.")
    if mode == "manual":
        print("Manual mode: open the URL printed by the official CLI in your browser.")
        print("Enter any authorization code ONLY into the official CLI prompt in THIS terminal.")
    else:
        print("Browser mode: if consent cannot be obtained, retry without -Browser for manual mode.")
    print("Do not paste authorization codes, login files or tokens into chat or Agent Nexus.")
    print("After sign-in, exit the CLI without sending a task, then test the connection in Nexus.")
    print("API / Vertex fallback, extra credits and native tools remain disabled; no model prompt is supplied.", flush=True)
    try:
        # Inherit the user's console handles directly. No PIPE, capture_output,
        # logfile, readline, input(), or OAuth parsing exists in this wrapper.
        return subprocess.call(spec["argv"], cwd=spec["cwd"], env=environment)
    except OSError:
        print("Could not start the official CLI in this terminal; no model task was submitted.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("manual", "browser"), default="manual")
    parser.add_argument("--check", action="store_true", help="Public CLI metadata only; never start login or a model")
    args = parser.parse_args()
    try:
        sys.exit(asyncio.run(main(mode=args.mode, check=args.check)))
    except KeyboardInterrupt:
        sys.exit(130)
