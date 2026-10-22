"""Keep each test run inside the workspace without deleting another run's files."""
from pathlib import Path
import os
import uuid


def pytest_configure(config):
    root = Path(__file__).resolve().parents[2]
    if config.option.basetemp is None:
        config.option.basetemp = str(root / "work" / "test-runs" / uuid.uuid4().hex[:12])
    target = Path(config.option.basetemp).resolve()
    if not target.is_relative_to(root):
        raise ValueError("Test temp directory must stay inside the project workspace")
    target.parent.mkdir(parents=True, exist_ok=True)
    # Tests must never read or write the operator's real DPAPI secret store.
    # Pointing the vault at the disposable test run keeps the run isolated.
    isolated = target / "secrets"
    isolated.mkdir(parents=True, exist_ok=True)
    os.environ["NEXUS_SECRET_DIR"] = str(isolated)
