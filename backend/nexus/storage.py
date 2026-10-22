from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, root: Path):
        self.path = root / "work" / "nexus.sqlite3"
        if not self.path.resolve().is_relative_to(root.resolve()):
            raise ValueError("数据库路径超出项目工作区")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS tasks (
                  id TEXT PRIMARY KEY, title TEXT NOT NULL, description TEXT NOT NULL,
                  agent_id TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL,
                  priority TEXT NOT NULL, requires_skills TEXT NOT NULL,
                  result TEXT, worktree TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS dependencies (
                  task_id TEXT REFERENCES tasks(id), depends_on TEXT REFERENCES tasks(id),
                  PRIMARY KEY(task_id, depends_on), CHECK(task_id != depends_on)
                );
                CREATE TABLE IF NOT EXISTS logs (
                  id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT REFERENCES tasks(id),
                  level TEXT NOT NULL, source TEXT NOT NULL, message TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                  id INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT NOT NULL,
                  payload TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS skills (
                  id TEXT PRIMARY KEY, name TEXT NOT NULL, source_url TEXT NOT NULL,
                  content TEXT NOT NULL, sha256 TEXT NOT NULL, permissions TEXT NOT NULL,
                  status TEXT NOT NULL, review TEXT, trial TEXT, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS approvals (
                  id INTEGER PRIMARY KEY AUTOINCREMENT, skill_id TEXT REFERENCES skills(id),
                  decision TEXT NOT NULL, reviewer TEXT NOT NULL, note TEXT NOT NULL,
                  sha256 TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS attempts (
                  id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
                  purpose TEXT NOT NULL, status TEXT NOT NULL, cli_version TEXT,
                  pid INTEGER, thread_id TEXT, result TEXT, usage TEXT NOT NULL DEFAULT '{}',
                  artifacts TEXT NOT NULL DEFAULT '[]', error_kind TEXT, error_message TEXT,
                  created_at TEXT NOT NULL, finished_at TEXT
                );
                CREATE TABLE IF NOT EXISTS handoffs (
                  id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
                  phase TEXT NOT NULL, task_snapshot TEXT NOT NULL, input_hashes TEXT NOT NULL,
                  packet TEXT NOT NULL, packet_sha256 TEXT NOT NULL,
                  submission TEXT, sender TEXT, review TEXT, artifacts TEXT NOT NULL DEFAULT '[]',
                  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                -- One row per model request. ``usage_source`` records whether the
                -- provider actually reported the token counters, so an estimate can
                -- never be read back as billed usage.
                CREATE TABLE IF NOT EXISTS usage_records (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  dedupe_key TEXT NOT NULL UNIQUE,
                  task_id TEXT, attempt_id TEXT, agent_id TEXT NOT NULL, provider_id TEXT NOT NULL,
                  model_key TEXT, requested_model TEXT, reported_model TEXT, request_id TEXT,
                  input_tokens INTEGER, output_tokens INTEGER, total_tokens INTEGER,
                  cached_tokens INTEGER, reasoning_tokens INTEGER,
                  total_source TEXT NOT NULL DEFAULT 'unavailable',
                  usage_source TEXT NOT NULL DEFAULT 'unavailable',
                  streaming INTEGER NOT NULL DEFAULT 0,
                  status TEXT NOT NULL DEFAULT 'ok', error_kind TEXT, latency_ms INTEGER,
                  credit REAL, currency TEXT, estimated_cost REAL,
                  cost_source TEXT NOT NULL DEFAULT 'not-estimable', price_revision TEXT,
                  purpose TEXT NOT NULL DEFAULT 'task',
                  created_at TEXT NOT NULL, finished_at TEXT
                );
                CREATE INDEX IF NOT EXISTS usage_records_created ON usage_records(created_at);
                CREATE INDEX IF NOT EXISTS usage_records_agent ON usage_records(agent_id, created_at);
                CREATE INDEX IF NOT EXISTS usage_records_provider ON usage_records(provider_id, created_at);
                CREATE INDEX IF NOT EXISTS usage_records_task ON usage_records(task_id);
                -- Editable per-model rates. Absent rows mean "use the documented default".
                CREATE TABLE IF NOT EXISTS rate_card (
                  model_key TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                -- Non-secret scoped settings (budgets, declared subscription plan).
                CREATE TABLE IF NOT EXISTS settings (
                  scope TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, updated_at TEXT NOT NULL,
                  PRIMARY KEY(scope, key)
                );
                -- Evidence that a real request verified a route or a declared capability.
                CREATE TABLE IF NOT EXISTS verifications (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  agent_id TEXT NOT NULL, provider_id TEXT,
                  kind TEXT NOT NULL, outcome TEXT NOT NULL, detail TEXT NOT NULL,
                  evidence TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS verifications_agent ON verifications(agent_id, kind, created_at);
                -- Additive execution journal. No credential or generated file body.
                CREATE TABLE IF NOT EXISTS execution_checkpoints (
                  id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
                  stage TEXT NOT NULL, state TEXT NOT NULL, hold INTEGER NOT NULL DEFAULT 0,
                  version INTEGER NOT NULL DEFAULT 1, payload TEXT NOT NULL,
                  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS checkpoint_task ON execution_checkpoints(task_id,created_at);
                CREATE TABLE IF NOT EXISTS execution_requests (
                  request_key TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
                  attempt_id TEXT, fingerprint TEXT NOT NULL, created_at TEXT NOT NULL
                );
                -- The task list reads the latest attempt, handoff and log for every
                -- row; without these indexes each lookup scans the whole table.
                CREATE INDEX IF NOT EXISTS attempts_task ON attempts(task_id, created_at);
                CREATE INDEX IF NOT EXISTS handoffs_task ON handoffs(task_id, created_at);
                CREATE INDEX IF NOT EXISTS tasks_created ON tasks(created_at);
                CREATE INDEX IF NOT EXISTS logs_task ON logs(task_id, id);
                CREATE INDEX IF NOT EXISTS events_type ON events(type, id);
            """)
            if "input_files" not in {row[1] for row in db.execute("PRAGMA table_info(tasks)")}:
                db.execute("ALTER TABLE tasks ADD COLUMN input_files TEXT NOT NULL DEFAULT '[]'")
            if "exit_code" not in {row[1] for row in db.execute("PRAGMA table_info(attempts)")}:
                db.execute("ALTER TABLE attempts ADD COLUMN exit_code INTEGER")
            columns = {row[1] for row in db.execute("PRAGMA table_info(attempts)")}
            for name in ("agent_id", "transport", "request_profile"):
                if name not in columns:
                    db.execute(f"ALTER TABLE attempts ADD COLUMN {name} TEXT")
            db.execute("UPDATE attempts SET agent_id=(SELECT agent_id FROM tasks WHERE tasks.id=attempts.task_id) WHERE agent_id IS NULL")
            db.execute("UPDATE attempts SET transport='official-cli-jsonl' WHERE agent_id='codex' AND transport IS NULL")
            # Optional per-task cost approval gate for metered providers.
            if "cost_approved" not in {row[1] for row in db.execute("PRAGMA table_info(tasks)")}:
                db.execute("ALTER TABLE tasks ADD COLUMN cost_approved INTEGER NOT NULL DEFAULT 0")
            db.execute("PRAGMA user_version=6")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def tasks(self, db=None) -> list[dict]:
        if db is None:
            with self.connect() as con:
                return self.tasks(con)
        result = []
        for row in db.execute("SELECT * FROM tasks ORDER BY created_at DESC"):
            item = dict(row)
            item["requires_skills"] = json.loads(item["requires_skills"])
            item["input_files"] = json.loads(item["input_files"])
            item["dependencies"] = [r[0] for r in db.execute(
                "SELECT depends_on FROM dependencies WHERE task_id=? ORDER BY depends_on", (row["id"],))]
            result.append(item)
            latest = db.execute("SELECT * FROM attempts WHERE task_id=? ORDER BY created_at DESC LIMIT 1", (row["id"],)).fetchone()
            item["last_attempt"] = self.decode_attempt(latest) if latest else None
            checkpoint = db.execute('SELECT * FROM execution_checkpoints WHERE task_id=? ORDER BY created_at DESC,id DESC LIMIT 1', (row['id'],)).fetchone()
            item['checkpoint'] = self.decode_checkpoint(checkpoint) if checkpoint else None
            item['recovery_hold'] = bool(db.execute('SELECT 1 FROM execution_checkpoints WHERE task_id=? AND hold=1 LIMIT 1', (row['id'],)).fetchone())
            latest_handoff = db.execute("SELECT * FROM handoffs WHERE task_id=? ORDER BY created_at DESC LIMIT 1", (row["id"],)).fetchone()
            item["latest_handoff"] = self.decode_handoff(latest_handoff) if latest_handoff else None
        return result

    @staticmethod
    def decode_checkpoint(row):
        item = dict(row)
        item['payload'] = json.loads(item['payload'])
        item['hold'] = bool(item['hold'])
        return item

    @staticmethod
    def decode_handoff(row):
        item = dict(row)
        for key in ("task_snapshot", "input_hashes", "submission", "review", "artifacts"):
            item[key] = json.loads(item[key]) if item[key] is not None else None
        submission = item.get("submission") or {}
        item["summary"] = submission.get("summary")
        item["test_reports"] = submission.get("test_reports", [])
        item["source"] = "human_import"
        item["external_execution_verified"] = False
        # A task listing cannot expose a copyable task before explicit confirmation.
        if item["phase"] == "prepared":
            item["packet"] = None
        return item

    def handoff(self, handoff_id, db=None):
        if db is None:
            with self.connect() as con:
                return self.handoff(handoff_id, con)
        row = db.execute("SELECT * FROM handoffs WHERE id=?", (handoff_id,)).fetchone()
        return self.decode_handoff(row) if row else None

    @staticmethod
    def decode_attempt(row) -> dict:
        item = dict(row)
        item['request_profile'] = json.loads(item['request_profile']) if item.get('request_profile') else None
        for key in ("usage", "artifacts"):
            item[key] = json.loads(item[key])
        return item

    def attempts(self, task_id):
        with self.connect() as db:
            return [self.decode_attempt(r) for r in db.execute(
                "SELECT * FROM attempts WHERE task_id=? ORDER BY created_at DESC LIMIT 50", (task_id,))]

    def logs(self, task_id: str | None = None, after: int = 0, limit: int = 300) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM logs WHERE id>? " + ("AND task_id=? " if task_id else "") +
                "ORDER BY id DESC LIMIT ?", (after, task_id, limit) if task_id else (after, limit))
            return list(reversed([dict(r) for r in rows]))

    def event(self, db, kind: str, payload: dict) -> dict:
        stamp = now()
        cursor = db.execute("INSERT INTO events(type,payload,created_at) VALUES(?,?,?)",
                            (kind, json.dumps(payload, ensure_ascii=False), stamp))
        return {"id": cursor.lastrowid, "type": kind, "payload": payload, "created_at": stamp}

    def log(self, db, task_id: str | None, message: str, level="info", source="scheduler") -> dict:
        stamp = now()
        cursor = db.execute("INSERT INTO logs(task_id,level,source,message,created_at) VALUES(?,?,?,?,?)",
                            (task_id, level, source, message[:16000], stamp))
        data = {"id": cursor.lastrowid, "task_id": task_id, "level": level,
                "source": source, "message": message[:16000], "created_at": stamp}
        self.event(db, "log.created", data)
        return data

    def events(self, after: int, limit=200) -> list[dict]:
        with self.connect() as db:
            return [{**dict(r), "payload": json.loads(r["payload"])} for r in db.execute(
                "SELECT * FROM events WHERE id>? ORDER BY id LIMIT ?", (after, limit))]

    # --------------------------------------------------------- verifications

    def record_verification(self, agent_id: str, kind: str, outcome: str, detail: str,
                            provider_id: str | None = None, evidence: dict | None = None) -> dict:
        """Record evidence of a real request. Never a claim without a request."""
        stamp = now()
        with self.connect() as db:
            cursor = db.execute(
                "INSERT INTO verifications(agent_id,provider_id,kind,outcome,detail,evidence,created_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (agent_id, provider_id, kind, outcome, detail[:4000],
                 json.dumps(evidence or {}, ensure_ascii=False), stamp))
            return {"id": cursor.lastrowid, "agent_id": agent_id, "provider_id": provider_id,
                    "kind": kind, "outcome": outcome, "detail": detail, "evidence": evidence or {},
                    "created_at": stamp}

    def verifications(self, agent_id: str | None = None, kind: str | None = None, limit: int = 100) -> list[dict]:
        clause, params = [], []
        if agent_id:
            clause.append("agent_id=?")
            params.append(agent_id)
        if kind:
            clause.append("kind=?")
            params.append(kind)
        params.append(max(1, min(int(limit), 1000)))
        where = (" WHERE " + " AND ".join(clause)) if clause else ""
        with self.connect() as db:
            return [{**dict(row), "evidence": json.loads(row["evidence"])} for row in db.execute(
                "SELECT * FROM verifications" + where + " ORDER BY id DESC LIMIT ?", params)]

    def latest_verification(self, agent_id: str, kind: str) -> dict | None:
        rows = self.verifications(agent_id, kind, 1)
        return rows[0] if rows else None

    # -------------------------------------------------------------- settings

    def setting(self, scope: str, key: str, default=None):
        with self.connect() as db:
            row = db.execute("SELECT value FROM settings WHERE scope=? AND key=?", (scope, key)).fetchone()
        return json.loads(row["value"]) if row else default

    def skills(self) -> list[dict]:
        with self.connect() as db:
            result = []
            for row in db.execute("SELECT * FROM skills ORDER BY created_at DESC"):
                item = dict(row)
                for field in ("permissions", "review", "trial"):
                    item[field] = json.loads(item[field]) if item[field] else None
                item["approvals"] = [dict(a) for a in db.execute(
                    "SELECT * FROM approvals WHERE skill_id=? ORDER BY id", (item["id"],))]
                result.append(item)
            return result

