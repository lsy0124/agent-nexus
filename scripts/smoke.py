"""Read-only loopback health inspection. Never creates tasks or Worktrees."""
import argparse
import json
from pathlib import Path
from urllib.parse import urlsplit

import httpx

ROOT = Path(__file__).resolve().parents[1]


def inspect(client):
    """GET only; suitable for both a live instance and an injected transport."""
    health = client.get("/api/health")
    health.raise_for_status()
    response = client.get("/api/state")
    response.raise_for_status()
    page = client.get("/")
    page.raise_for_status()
    state = response.json()
    return {
        "health": health.json(), "frontend_status": page.status_code,
        "repository_ready": bool(state.get("repository", {}).get("ready")),
        "task_count": len(state.get("tasks", [])),
        "connected_runtime_count": sum(a.get("status") == "connected" for a in state.get("agents", [])),
        "inspection_only": True, "tasks_created": 0, "model_calls": 0,
        "detail": "HTTP health and state only; no claim of model availability or task execution.",
    }


def local_url(value):
    parsed = urlsplit(value)
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in {"", "/"}):
        raise argparse.ArgumentTypeError("Use an HTTP loopback origin without credentials or a path")
    return value.rstrip("/")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", type=local_url, default="http://127.0.0.1:8765")
    args = parser.parse_args()
    with httpx.Client(base_url=args.url, timeout=10, trust_env=False, follow_redirects=False) as client:
        print(json.dumps(inspect(client), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
