import sqlite3

from nexus.storage import Store


def test_phase_one_task_survives_schema_upgrade(tmp_path):
    directory = tmp_path / "work"
    directory.mkdir()
    with sqlite3.connect(directory / "nexus.sqlite3") as db:
        db.execute("CREATE TABLE tasks(id TEXT PRIMARY KEY,title TEXT,description TEXT,agent_id TEXT,kind TEXT,status TEXT,priority TEXT,requires_skills TEXT,result TEXT,worktree TEXT,created_at TEXT,updated_at TEXT)")
        db.execute("INSERT INTO tasks VALUES('existing','existing user task','','codex','agent','backlog','normal','[]',NULL,NULL,'2026-10-15','2026-10-15')")
        db.execute("PRAGMA user_version=1")
    store = Store(tmp_path)
    assert store.tasks()[0]["id"] == "existing"
    assert store.tasks()[0]["input_files"] == []
    assert store.tasks()[0]["last_attempt"] is None
    # An existing Phase-1 task keeps working, and the cost-approval gate added
    # with the usage plane defaults to "not approved" rather than to approved.
    assert store.tasks()[0]["cost_approved"] == 0
    with store.connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 6
        assert db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='usage_records'").fetchone()
        assert db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='rate_card'").fetchone()
        assert db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='settings'").fetchone()
        assert db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='verifications'").fetchone()
        assert db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='execution_checkpoints'").fetchone()
        assert db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='execution_requests'").fetchone()
