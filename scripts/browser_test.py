"""Run browser acceptance against an isolated, disposable project directory.

Uses only project dependencies and an existing browser. Never touches production SQLite.
"""
from pathlib import Path
import os
import shutil
import socket
import subprocess
import sys
import time
import uuid

import httpx

ROOT = Path(__file__).resolve().parents[1]


def main():
    test_root = ROOT / "work" / "browser-qa" / uuid.uuid4().hex[:12]
    test_root.mkdir(parents=True)
    # Manual handoff acceptance writes through a real, isolated Git Worktree.
    # This repository and explicit fixture commit belong only to this test run.
    (test_root / "README.md").write_text("LOCAL BROWSER QA FIXTURE. No real model or external WorkBuddy execution.\n", encoding="utf-8")
    for args in (["init", "--quiet"], ["add", "--", "README.md"],
                 ["-c", "user.name=Agent Nexus Browser QA Fixture", "-c", "user.email=browser-qa@localhost",
                  "commit", "--quiet", "-m", "Local browser acceptance fixture; no real agent execution"]):
        subprocess.run(["git", *args], cwd=test_root, check=True, capture_output=True, timeout=15)
    dist = ROOT / "frontend" / "dist"
    if not (dist / "index.html").exists():
        raise RuntimeError("Run npm --prefix frontend run build first")
    shutil.copytree(dist, test_root / "frontend" / "dist")
    # A dedicated entrypoint avoids exposing a configurable filesystem root in the app API.
    (test_root / "qa_entry.py").write_text(
        "from pathlib import Path\nfrom nexus.main import create_app\n"
        "app = create_app(Path(__file__).resolve().parent, enable_codex=False)\n", encoding="utf-8")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "backend")
    env["NEXUS_URL"] = "http://127.0.0.1:5173"
    env["NEXUS_KEY_VAULT"] = "memory"
    env["NEXUS_SECRET_DIR"] = str(test_root / "secrets")
    env["NEXUS_TEST_OUTPUT_DIR"] = str(test_root / "evidence")
    # Refuse to reuse another user's server or mutate its database. Binding the
    # address is the authoritative test: an HTTP probe can hang behind a local
    # firewall even when nothing listens, and a hung owner still owns the port.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        try:
            probe.bind(("127.0.0.1", 5173))
        except OSError as exc:
            raise RuntimeError(f"端口 5173 已被占用（{exc}）。请停止该服务，或使用独立实例运行 E2E。") from exc
    with (test_root / "server.log").open("w", encoding="utf-8") as log:
        server = subprocess.Popen([sys.executable, "-m", "uvicorn", "qa_entry:app", "--app-dir", str(test_root),
                                   "--host", "127.0.0.1", "--port", "5173", "--no-access-log"],
                                  cwd=ROOT, env=env, stdout=log, stderr=log,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            with httpx.Client(timeout=1, trust_env=False) as client:
                for _ in range(60):
                    if server.poll() is not None:
                        raise RuntimeError(f"Test server failed; see {test_root / 'server.log'}")
                    try:
                        if client.get(env["NEXUS_URL"] + "/api/health").status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    time.sleep(0.1)
                else:
                    raise RuntimeError("Test server startup timeout")
            result = subprocess.run([shutil.which("node"), "tests/e2e.mjs"], cwd=ROOT / "frontend", env=env)
            return result.returncode
        finally:
            # The venv launcher starts a base-Python child. Terminating only the
            # launcher leaves that child holding the port, so stop the tree.
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(server.pid), "/T", "/F"],
                               capture_output=True, timeout=30)
            else:
                server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()


if __name__ == "__main__":
    sys.exit(main())
