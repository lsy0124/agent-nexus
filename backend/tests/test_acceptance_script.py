"""Self-test for scripts/real_api_acceptance.py logic using an injected transport.

Proves the acceptance script's assertions and plumbing are correct *without*
reaching any real provider: a mock OpenAI-compatible transport answers the
gateway endpoints, so a failure here would be a bug in the script rather than in
the user's providers.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import uuid
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from nexus import gateway_adapter as gateway  # noqa: E402


def _handler(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content.decode("utf-8"))
    model = body.get("model", "unknown")
    # Two call shapes reach here: the nonce connection test and the vision probe.
    content = body["messages"][-1]["content"]
    if isinstance(content, list):
        answer = "左上红、右上绿、左下蓝、右下白"
    else:
        payload = json.loads(content)
        objective = payload.get("objective", "")
        # The connection test asks for the nonce verbatim as `summary`.
        answer = objective.split("exactly", 1)[-1].split(",")[0].strip() if "exactly" in objective else "done"
    return httpx.Response(200, json={
        "id": "resp_" + uuid.uuid4().hex[:16],
        "model": model,
        "usage": {"prompt_tokens": 296, "completion_tokens": 52, "total_tokens": 348,
                  "prompt_tokens_details": {"cached_tokens": 12}, "credit": 0.01},
        "choices": [{"message": {"content": json.dumps(
            {"summary": answer, "files": [], "approval_requests": []})}, "finish_reason": "stop"}],
    })


@pytest.fixture
def mock_transport(monkeypatch):
    """Route every gateway HTTP call through the fixture transport."""
    transport = httpx.MockTransport(_handler)
    original = gateway.GatewayRouteAdapter.__init__

    def patched(self, *args, **kwargs):
        kwargs.setdefault("http_transport", transport)
        original(self, *args, **kwargs)

    monkeypatch.setattr(gateway.GatewayRouteAdapter, "__init__", patched)
    return transport


def _load_acceptance_module():
    spec = importlib.util.spec_from_file_location(
        "acceptance", ROOT / "scripts" / "real_api_acceptance.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_acceptance_script_passes_against_a_mock_route(mock_transport, tmp_path, monkeypatch):
    """The script's own checks must pass when a provider behaves correctly."""
    module = _load_acceptance_module()
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setenv("NEXUS_DEEPSEEK_KEY", "mock-deepseek-key-1234567890")
    monkeypatch.setenv("NEXUS_GLM_KEY", "mock-glm-key-1234567890")
    monkeypatch.setattr(sys, "argv", ["real_api_acceptance.py", "--live"])
    # The script writes its evidence under ROOT/outputs; keep it in tmp_path.
    (tmp_path / "outputs").mkdir(parents=True, exist_ok=True)

    code = module.main()
    results = {item["check"]: item for item in module.RESULTS}
    assert results, "验收脚本必须产生检查项"
    assert code == 0, f"验收脚本应在模拟服务商下通过；实际失败项：" + str(
        [item for item in module.RESULTS if not item["passed"]])
    for name in ("1. 真实 DeepSeek API 调用", "2. 真实 GLM API 调用",
                 "3. 采集服务商实际返回的 Token 用量",
                 "4. Agent / 任务 / Provider 三级统计汇总正确",
                 "5. 缺少余额接口时显示「余额不可查询」",
                 "6. 预算达到上限后停止继续派发",
                 "6b. 预算守卫本身拒绝派发（与候选筛选无关）",
                 "7. 未经允许不会自动调用 Codex",
                 "8. 刷新页面与重启后台后历史记录仍然存在",
                 "9. API 密钥不出现在页面、接口响应或普通日志中"):
        assert name in results, f"缺少检查项：{name}"
        assert results[name]["passed"], f"检查项未通过：{name} · {results[name]['detail'][:300]}"
    evidence = json.loads((tmp_path / "outputs" / "usage-quota-real-acceptance.json").read_text(encoding="utf-8"))
    assert evidence["results"]
    # No key may appear anywhere in the evidence file.
    text = json.dumps(evidence, ensure_ascii=False)
    assert "mock-deepseek-key" not in text and "mock-glm-key" not in text


def test_acceptance_script_refuses_to_run_without_keys(monkeypatch, tmp_path, capsys):
    module = _load_acceptance_module()
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.delenv("NEXUS_DEEPSEEK_KEY", raising=False)
    monkeypatch.delenv("NEXUS_GLM_KEY", raising=False)
    monkeypatch.setattr(sys, "argv", ["real_api_acceptance.py", "--live"])
    assert module.main() == 2
    assert "未发起任何真实调用" in capsys.readouterr().out


def test_acceptance_script_dry_run_makes_no_calls(monkeypatch, tmp_path, capsys):
    module = _load_acceptance_module()
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.delenv("NEXUS_DEEPSEEK_KEY", raising=False)
    monkeypatch.setattr(sys, "argv", ["real_api_acceptance.py"])
    assert module.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["deepseek_key_present"] is False
    assert "未发起任何真实调用" in payload["note"]
