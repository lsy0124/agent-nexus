"""The operator health check must not mutate a live task database."""
import argparse
import importlib.util
from pathlib import Path
import httpx
import pytest

spec = importlib.util.spec_from_file_location("readonly_smoke", Path(__file__).resolve().parents[2] / "scripts/smoke.py")
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


def test_smoke_uses_only_get_and_accepts_an_already_connected_runtime():
    requests = []
    def handler(request):
        requests.append((request.method, request.url.path))
        if request.url.path == "/api/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/api/state":
            return httpx.Response(200, json={"repository": {"ready": True}, "tasks": [{"id": "private-task"}], "agents": [{"status": "connected"}]})
        return httpx.Response(200, text="<html>local</html>")
    with httpx.Client(base_url="http://127.0.0.1:8765", transport=httpx.MockTransport(handler)) as client:
        result = smoke.inspect(client)
    assert requests == [("GET", "/api/health"), ("GET", "/api/state"), ("GET", "/")]
    assert result["tasks_created"] == result["model_calls"] == 0
    assert result["connected_runtime_count"] == 1
    assert "private-task" not in str(result)


@pytest.mark.parametrize("url", ["https://example.com", "http://user:pass@localhost", "http://localhost/other", "http://localhost?key=private"])
def test_smoke_refuses_nonlocal_or_credential_urls(url):
    with pytest.raises(argparse.ArgumentTypeError):
        smoke.local_url(url)
