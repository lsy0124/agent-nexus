"""Record public environment discovery. This never submits a model prompt."""
import asyncio
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from nexus.adapters import make_adapters
from nexus.registry import AdapterRegistry
from nexus.storage import now


async def main():
    registry = AdapterRegistry(make_adapters(ROOT))
    result = {"mode": "public-environment-discovery-only", "time": now(), "model_requests": 0,
              "agents": await registry.detect_all()}
    (ROOT / "outputs" / "phase3-environment.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), "utf-8")
    print(json.dumps({"agents": [{"id": a["id"], "status": a["status"], "cli_version": a.get("cli_version"), "ready": a.get("ready")} for a in result["agents"]]}, ensure_ascii=True))


asyncio.run(main())
