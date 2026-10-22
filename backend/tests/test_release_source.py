"""A source release must not accidentally include the operator's state."""
import importlib.util
import json
from pathlib import Path
import pytest

spec = importlib.util.spec_from_file_location("release_source", Path(__file__).resolve().parents[2]/"scripts/release_source.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


def test_export_includes_untracked_implementation_but_excludes_private_state(tmp_path):
    for name in ["README.md", "backend/nexus/planning.py", "work/secrets/provider.dpapi", "outputs/private.json", ".env", "frontend/node_modules/private.js"]:
        p=tmp_path/name; p.parent.mkdir(parents=True,exist_ok=True); p.write_text("LOCAL FIXTURE",encoding="utf-8")
    dest=tmp_path/"work/release-candidates/new"
    release.export(tmp_path,dest)
    manifest=json.loads((dest/"SOURCE_MANIFEST.json").read_text())
    assert set(manifest["files"]) == {"README.md","backend/nexus/planning.py"}
    assert (tmp_path/"outputs/private.json").read_text() == "LOCAL FIXTURE"
    with pytest.raises(ValueError):
        release.export(tmp_path,dest)


def test_audit_does_not_echo_secret_values_and_export_fails_closed(tmp_path):
    secret="sk-"+"A1b2C3d4E5f6G7h8I9j0K1l2"
    (tmp_path/"README.md").write_text(secret)
    result=release.audit(tmp_path)
    assert not result["passed"] and secret not in json.dumps(result)
    with pytest.raises(ValueError):
        release.export(tmp_path,tmp_path/"work/release-candidates/new")


def test_test_marker_does_not_exempt_production_credentials(tmp_path):
    p=tmp_path/"backend/nexus/settings.py";p.parent.mkdir(parents=True)
    p.write_text("sk-THIS-IS-A-TEST-SHAPED-SECRET-STILL-REFUSE")
    assert not release.audit(tmp_path)["passed"]


def test_export_refuses_path_outside_candidate_directory(tmp_path):
    (tmp_path/"README.md").write_text("fixture")
    with pytest.raises(ValueError):
        release.export(tmp_path,tmp_path/"public")
