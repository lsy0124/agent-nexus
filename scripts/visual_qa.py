"""Capture real browser screenshots of the new usage/quota console pages.

Uses an isolated disposable workspace and an existing browser. Never touches the
production SQLite database and never calls a real model: the seeded usage rows
are labelled fixtures, and the screenshots exist to check layout and interaction.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    test_root = ROOT / "work" / "visual-qa" / uuid.uuid4().hex[:12]
    test_root.mkdir(parents=True)
    (test_root / "README.md").write_text("VISUAL QA FIXTURE. No real model execution.\n", encoding="utf-8")
    env_git = {**os.environ, "GIT_AUTHOR_NAME": "Visual QA", "GIT_AUTHOR_EMAIL": "visual@localhost",
               "GIT_COMMITTER_NAME": "Visual QA", "GIT_COMMITTER_EMAIL": "visual@localhost",
               "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
    for args in (["init", "--quiet"], ["add", "--", "README.md"],
                 ["commit", "--quiet", "--no-gpg-sign", "-m", "visual qa fixture"]):
        subprocess.run(["git", *args], cwd=test_root, env=env_git, check=True, capture_output=True, timeout=30)
    dist = ROOT / "frontend" / "dist"
    if not (dist / "index.html").exists():
        raise RuntimeError("先运行 npm --prefix frontend run build")
    shutil.copytree(dist, test_root / "frontend" / "dist")
    (test_root / "qa_entry.py").write_text(
        "from pathlib import Path\nfrom nexus.main import create_app\n"
        "app = create_app(Path(__file__).resolve().parent, enable_codex=False)\n", encoding="utf-8")

    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "backend")
    env["NEXUS_URL"] = "http://127.0.0.1:5174"
    env["NEXUS_WORKSPACE"] = str(test_root)
    with (test_root / "server.log").open("w", encoding="utf-8") as log:
        server = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "qa_entry:app", "--app-dir", str(test_root),
             "--host", "127.0.0.1", "--port", "5174", "--no-access-log"],
            cwd=ROOT, env=env, stdout=log, stderr=log,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            with httpx.Client(timeout=2, trust_env=False) as client:
                for _ in range(80):
                    if server.poll() is not None:
                        raise RuntimeError(f"服务启动失败：{(test_root / 'server.log').read_text(encoding='utf-8', errors='replace')[-1500:]}")
                    try:
                        if client.get(env["NEXUS_URL"] + "/api/health").status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    time.sleep(0.15)
                else:
                    raise RuntimeError("服务启动超时")
                # Seed labelled fixture usage so the page has real rendering to check.
                token = client.get(env["NEXUS_URL"] + "/api/session").json()["token"]
                headers = {"X-Nexus-Token": token}
                task = client.post(env["NEXUS_URL"] + "/api/tasks", headers=headers, json={
                    "title": "多 Agent 文件方案任务", "description": "视觉检查夹具",
                    "agent_id": "deepseek-gateway"}).json()
                _seed(test_root, task["id"])
                client.put(env["NEXUS_URL"] + "/api/settings/budget", headers=headers,
                           json={"daily_budget": 2.0, "monthly_budget": 25.0, "per_task_tokens": 200000})
            result = subprocess.run([shutil.which("node"), "tests/visual-usage.mjs"], cwd=ROOT / "frontend", env=env)
            return result.returncode
        finally:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill(); server.wait()


def _seed(root: Path, task_id: str) -> None:
    """Insert clearly-labelled fixture usage rows for layout verification only."""
    sys.path.insert(0, str(ROOT / "backend"))
    from nexus.storage import Store
    from nexus.usage import Usage, UsageCollector, UsageEvent, STATUS_ERROR, STATUS_OK

    collector = UsageCollector(Store(root))
    rows = [
        ("deepseek-gateway", "gonode-gateway", "gonode-gateway/deepseek-v4.1-flash", 18420, 3120, 2100, STATUS_OK, "t1"),
        ("deepseek-gateway", "gonode-gateway", "gonode-gateway/deepseek-v4.1-flash", 9800, 1450, 0, STATUS_OK, "t2"),
        ("zcode-gateway", "hyai666-gateway", "hyai666-gateway/glm5.3", 22150, 3980, 5400, STATUS_OK, "t3"),
        ("zcode-gateway", "hyai666-gateway", "hyai666-gateway/glm5.3", 6100, 700, 0, STATUS_ERROR, "t4"),
        ("deepseek-gateway", "gonode-gateway", "gonode-gateway/deepseek-v4.1-flash", None, None, None, STATUS_ERROR, "t5"),
    ]
    for index, (agent, provider, model, prompt, completion, cached, status, key) in enumerate(rows):
        if prompt is None:
            usage = Usage()
        else:
            usage = Usage(input_tokens=prompt, output_tokens=completion,
                          total_tokens=prompt + completion, cached_tokens=cached or None,
                          source="provider-reported", total_source="provider")
        collector.record(UsageEvent(
            agent_id=agent, provider_id=provider, model_key=model, task_id=task_id,
            attempt_id=f"visual-{index}", usage=usage, status=status,
            error_kind="rate_limit" if status == STATUS_ERROR else None,
            latency_ms=1200 + index * 340, idempotency_key=key))


if __name__ == "__main__":
    sys.exit(main())
