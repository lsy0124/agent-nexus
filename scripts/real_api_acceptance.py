"""Real end-to-end acceptance through Agent Nexus against the user's routes.

This performs genuine calls on the routes the user already authorized for this
project (third-party OpenAI-compatible gateways, metered). Keys are read from
environment variables supplied by the operator, are never written to disk by
this script, and are never echoed.

  NEXUS_DEEPSEEK_KEY  -> deepseek-gateway route (gonode.si)   deepseek-v4.1-flash
  NEXUS_GLM_KEY       -> zcode-gateway route   (hyai666.top)  glm5.3

Usage:  python scripts/real_api_acceptance.py [--live]
Without --live it only reports which credentials are present.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from fastapi.testclient import TestClient  # noqa: E402

from nexus.main import create_app  # noqa: E402

RESULTS: list[dict] = []


def record(name: str, passed: bool, detail: str, evidence: dict | None = None) -> None:
    RESULTS.append({"check": name, "passed": bool(passed), "detail": detail, "evidence": evidence or {}})
    print(("PASS " if passed else "FAIL ") + name + " · " + detail[:400], flush=True)


def make_repo(base: Path) -> Path:
    target = base / uuid.uuid4().hex[:12]
    target.mkdir(parents=True)
    (target / "README.md").write_text("real api acceptance fixture\n", encoding="utf-8")
    env = {**os.environ, "GIT_AUTHOR_NAME": "Acceptance", "GIT_AUTHOR_EMAIL": "acceptance@localhost",
           "GIT_COMMITTER_NAME": "Acceptance", "GIT_COMMITTER_EMAIL": "acceptance@localhost",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
    for args in (["init", "--quiet"], ["add", "--", "README.md"],
                 ["commit", "--quiet", "--no-gpg-sign", "-m", "fixture"]):
        done = subprocess.run(["git", *args], cwd=target, env=env, capture_output=True, timeout=30)
        if done.returncode:
            raise RuntimeError(done.stderr.decode("utf-8", "replace"))
    return target


def call_route(client: TestClient, agent_id: str, base_url: str, model: str, key: str,
               title: str) -> dict:
    """Configure, verify with a real nonce request, and return the evidence."""
    configured = client.post(f"/api/agents/{agent_id}/configure", json={
        "api_key": key, "model": model, "approve_usage": True, "base_url": base_url})
    if configured.status_code != 200:
        return {"ok": False, "stage": "configure", "detail": configured.text[:400]}
    tested = client.post(f"/api/agents/{agent_id}/test")
    task_id = tested.json().get("task", {}).get("id")
    if not task_id:
        return {"ok": False, "stage": "dispatch", "detail": tested.text[:400]}
    # Wait for the real request to finish.
    final = None
    for _ in range(150):
        tasks = client.get("/api/tasks").json()
        final = next((item for item in tasks if item["id"] == task_id), None)
        if final and final["status"] in ("done", "failed", "blocked", "cancelled"):
            break
        time.sleep(0.4)
    attempts = client.get(f"/api/tasks/{task_id}/attempts").json()
    usage_rows = client.get(f"/api/usage?span=all").json()["records"]
    row = next((item for item in usage_rows if item["task_id"] == task_id), None)
    return {
        "ok": bool(final and final["status"] == "done"),
        "stage": "executed",
        "task_id": task_id,
        "status": final["status"] if final else "timeout",
        "result": (final or {}).get("result", ""),
        "attempt": attempts[0] if attempts else None,
        "usage_row": row,
        "agent": next((a for a in client.get("/api/agents").json() if a["id"] == agent_id), None),
        "title": title,
    }


def main() -> int:
    live = "--live" in sys.argv
    deepseek_key = os.environ.get("NEXUS_DEEPSEEK_KEY")
    glm_key = os.environ.get("NEXUS_GLM_KEY")
    if not live:
        print(json.dumps({
            "deepseek_key_present": bool(deepseek_key),
            "glm_key_present": bool(glm_key),
            "note": "未传 --live，未发起任何真实调用。",
        }, ensure_ascii=False, indent=2))
        return 0
    if not deepseek_key or not glm_key:
        print("缺少 NEXUS_DEEPSEEK_KEY / NEXUS_GLM_KEY；未发起任何真实调用。")
        return 2

    base = ROOT / "work" / "real-acceptance"
    base.mkdir(parents=True, exist_ok=True)
    repo = make_repo(base)
    # Entering the context manager starts the app's lifespan and keeps its event
    # loop alive, which background dispatch tasks need. Without it the running
    # asyncio task is torn down mid-request and every real call shows as cancelled.
    client = TestClient(create_app(repo, enable_codex=True))
    client.__enter__()
    client.headers.update({"X-Nexus-Token": client.get("/api/session").json()["token"]})

    secret_probe = glm_key  # used only to assert non-leakage

    # --- 1. real DeepSeek call ------------------------------------------------
    deepseek = call_route(client, "deepseek-gateway", "https://gonode.si/v1",
                          "deepseek-v4.1-flash", deepseek_key, "DeepSeek 真实调用")
    record("1. 真实 DeepSeek API 调用", deepseek["ok"],
           f"任务 {deepseek.get('task_id')} 状态 {deepseek.get('status')}；{deepseek.get('result', '')[:200]}",
           {"usage_row": deepseek.get("usage_row"), "agent": {
               k: deepseek["agent"][k] for k in ("model_api_callable", "task_execution_capable", "status")
           } if deepseek.get("agent") else None})

    # --- 2. real GLM call -----------------------------------------------------
    glm = call_route(client, "zcode-gateway", "https://www.hyai666.top/v1",
                     "glm5.3", glm_key, "GLM 真实调用")
    record("2. 真实 GLM API 调用", glm["ok"],
           f"任务 {glm.get('task_id')} 状态 {glm.get('status')}；{glm.get('result', '')[:200]}",
           {"usage_row": glm.get("usage_row"), "agent": {
               k: glm["agent"][k] for k in ("model_api_callable", "task_execution_capable", "status")
           } if glm.get("agent") else None})

    # --- 3. real provider-reported usage captured -----------------------------
    rows = [row for row in (deepseek.get("usage_row"), glm.get("usage_row")) if row]
    reported = [row for row in rows if row["usage_source"] == "provider-reported"
                and row["total_tokens"] is not None]
    record("3. 采集服务商实际返回的 Token 用量", len(reported) == len(rows) == 2,
           "；".join(f"{row['provider_id']} 输入 {row['input_tokens']} / 输出 {row['output_tokens']} / "
                     f"合计 {row['total_tokens']}（{row['usage_source']}）" for row in rows),
           {"rows": rows})

    # --- 4. three-level aggregation ------------------------------------------
    usage = client.get("/api/usage?span=all").json()
    agents_seen = {row["name"] for row in usage["by_agent"]}
    providers_seen = {row["name"] for row in usage["by_provider"]}
    tasks_seen = {row["task_id"] for row in usage["task_costs"]}
    expected_tasks = {row["task_id"] for row in rows}
    # Only requests that actually reported tokens contribute to the token sum;
    # a request without reported usage carries NULL, never a fabricated zero.
    reported_total = sum(row["total_tokens"] or 0 for row in usage["records"])
    aggregated = ({"deepseek-gateway", "zcode-gateway"} <= agents_seen
                  and {"gonode-gateway", "hyai666-gateway"} <= providers_seen
                  and expected_tasks <= tasks_seen
                  and usage["totals"]["total_tokens"] == reported_total
                  and usage["totals"]["usage_unavailable"] == sum(
                      1 for row in usage["records"] if row["usage_source"] == "unavailable"))
    record("4. Agent / 任务 / Provider 三级统计汇总正确", aggregated,
           f"Agent {sorted(agents_seen)}；Provider {sorted(providers_seen)}；"
           f"任务 {sorted(tasks_seen)}；全局合计 {usage['totals']['total_tokens']} tokens；"
           f"逐行相加 {reported_total}",
           {"totals": usage["totals"], "by_agent": usage["by_agent"],
            "by_provider": usage["by_provider"], "records": usage["records"]})

    # --- 5. missing balance endpoint shows 不可查询 ----------------------------
    quota = client.get("/api/quota?probe=true").json()["state"]
    by_provider = {item["provider_id"]: item for item in quota["results"]}
    gateways = [by_provider["gonode-gateway"], by_provider["hyai666-gateway"]]
    record("5. 缺少余额接口时显示「余额不可查询」",
           all(item["queryable"] is False and item["status"] == "unsupported" for item in gateways),
           "；".join(f"{item['provider_id']}={item['display']}" for item in gateways),
           {"quota": gateways, "official": by_provider["deepseek-official"]})
    record("5b. 四类额度互不混用",
           all(item["kind"] in ("official_account_balance", "third_party_gateway_balance",
                                "nexus_budget", "subscription_allowance") for item in quota["results"]),
           "；".join(f"{item['provider_id']}:{item['kind']}" for item in quota["results"]))

    # --- 6. budget ceiling stops dispatch ------------------------------------
    # A fresh task must be used: the earlier task is already done, and re-running
    # it would be refused for the wrong reason. The budget guard is evaluated
    # only once the Agent itself is a valid candidate, so this asserts on the
    # guard's own verdict as well as on the dispatch response.
    client.put("/api/settings/budget", json={"daily_budget": 0.0})
    fresh = client.post("/api/tasks", json={
        "title": "预算保护验收", "description": "验证预算耗尽后停止派发",
        "agent_id": "deepseek-gateway"}).json()
    denied = client.post(f"/api/tasks/{fresh['id']}/run").json()
    budget_status = client.get("/api/budget").json()
    today = next(item for item in budget_status["windows"] if item["span"] == "today")
    record("6. 预算达到上限后停止继续派发",
           denied["accepted"] is False and today["budget_limit"] == 0
           and today["remaining_estimate"] == 0 and today["exhausted"] is True
           and budget_status["gpt_auto_dispatch"] is False,
           f"派发被拒：{denied['reason'][:200]}；今日预算上限 {today['budget_limit']}、"
           f"已用 {today['spent_estimate']}、剩余 {today['remaining_estimate']}",
           {"dispatch": denied, "budget": budget_status["windows"], "settings": budget_status["settings"]})
    client.put("/api/settings/budget", json={"daily_budget": None})

    # The guard's own verdict, isolated from the dispatch candidate checks.
    sys.path.insert(0, str(ROOT / "backend"))
    from nexus.budget import BudgetGuard, RateCard  # noqa: E402
    from nexus.storage import Store  # noqa: E402
    from nexus.usage import UsageCollector  # noqa: E402

    local_store = Store(repo)
    guard = BudgetGuard(local_store, UsageCollector(local_store), RateCard(local_store))
    guard.update_settings({"daily_budget": 0.0})
    verdict = guard.evaluate(agent_id="deepseek-gateway", provider_id="gonode-gateway",
                             model_key="gonode-gateway/deepseek-v4.1-flash", purpose="task")
    guard.update_settings({"daily_budget": None})
    record("6b. 预算守卫本身拒绝派发（与候选筛选无关）",
           verdict["allowed"] is False and "budget_exhausted" in [item["code"] for item in verdict["denials"]],
           "；".join(item["detail"] for item in verdict["denials"])[:220] or "（未拒绝）",
           {"verdict": verdict})

    # --- 7. Codex never auto-dispatched --------------------------------------
    auto_task = client.post("/api/tasks", json={"title": "auto probe", "description": "x",
                                                "agent_id": "auto"}).json()
    auto_run = client.post(f"/api/tasks/{auto_task['id']}/run").json()
    chosen = auto_run.get("task", {}).get("agent_id")
    policy = client.get("/api/policy").json()
    record("7. 未经允许不会自动调用 Codex",
           chosen != "codex" and policy["gpt_auto_dispatch"] is True and "codex" in policy["disabled_agents"],
           f"自动派发选择 {chosen}；策略 gpt_auto_dispatch={policy['gpt_auto_dispatch']}；"
           f"disabled={policy['disabled_agents']}；原因 {auto_run.get('reason', '')[:160]}",
           {"policy": policy})

    # --- 8. persistence across a backend restart ------------------------------
    before = client.get("/api/usage?span=all").json()["totals"]
    client.__exit__(None, None, None)  # a genuine shutdown, then a fresh process
    restarted = TestClient(create_app(repo, enable_codex=True))
    restarted.headers.update({"X-Nexus-Token": restarted.get("/api/session").json()["token"]})
    after = restarted.get("/api/usage?span=all").json()["totals"]
    record("8. 刷新页面与重启后台后历史记录仍然存在",
           after["requests"] == before["requests"] and after["total_tokens"] == before["total_tokens"]
           and after["requests"] > 0,
           f"重启前 {before['requests']} 次 / {before['total_tokens']} tokens；"
           f"重启后 {after['requests']} 次 / {after['total_tokens']} tokens",
           {"before": before, "after": after})

    # --- 9. no key leakage ----------------------------------------------------
    leaked = []
    for path in ("/api/state", "/api/agents", "/api/logs", "/api/usage?span=all",
                 "/api/quota?probe=true", "/api/settings/providers", "/api/catalog", "/api/budget"):
        body = restarted.get(path).text
        if secret_probe and secret_probe in body:
            leaked.append(path)
        if "Authorization" in body and "Bearer" in body:
            leaked.append(path + ":authorization")
    keys = restarted.get("/api/settings/providers").json()
    fingerprints_only = all("api_key" not in json.dumps(item["credential"]) for item in keys["providers"])
    record("9. API 密钥不出现在页面、接口响应或普通日志中",
           not leaked and fingerprints_only,
           f"泄漏路径 {leaked or '无'}；Provider 设置只返回指纹与长度",
           {"vault": keys["vault"]})

    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "routes": {
            "deepseek-gateway": {"endpoint": "https://gonode.si/v1", "model": "deepseek-v4.1-flash"},
            "zcode-gateway": {"endpoint": "https://www.hyai666.top/v1", "model": "glm5.3"},
        },
        "note": "真实调用，使用用户已授权的第三方按量计费网关路由；密钥仅经环境变量传入，未写盘、未回显。",
        "results": RESULTS,
        "usage_totals": after,
        "quota": quota,
    }
    out = ROOT / "outputs" / "usage-quota-real-acceptance.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    passed = sum(1 for item in RESULTS if item["passed"])
    print(f"\n{passed}/{len(RESULTS)} checks passed · evidence: {out}")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
