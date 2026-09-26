"""数据层：SQLite 表结构、连接和审计写入，不含任何业务判定。"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "subtitle_qc.db"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    source_language TEXT NOT NULL,
    duration_ms INTEGER NOT NULL CHECK(duration_ms > 0),
    owner TEXT NOT NULL,
    media_name TEXT NOT NULL,
    media_sha256 TEXT NOT NULL,
    glossary_revision INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    language TEXT NOT NULL,
    version_no INTEGER NOT NULL,
    parent_id INTEGER REFERENCES versions(id),
    status TEXT NOT NULL DEFAULT 'draft',
    revision INTEGER NOT NULL DEFAULT 0,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(project_id,language,version_no)
);
-- 同一来源（parent_id）只允许一条未完成的修订记录。
CREATE UNIQUE INDEX IF NOT EXISTS idx_versions_open_revision
    ON versions(parent_id) WHERE parent_id IS NOT NULL AND status NOT IN ('delivered','superseded');
CREATE TABLE IF NOT EXISTS assignments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
    user TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('translator','timeline','reviewer')),
    assigned_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(version_id,user,role)
);
CREATE TABLE IF NOT EXISTS cues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
    cue_index INTEGER NOT NULL,
    start_ms INTEGER NOT NULL CHECK(start_ms >= 0),
    end_ms INTEGER NOT NULL CHECK(end_ms > start_ms),
    text TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(version_id,cue_index)
);
CREATE TABLE IF NOT EXISTS comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
    cue_id INTEGER REFERENCES cues(id) ON DELETE SET NULL,
    user TEXT NOT NULL,
    time_ms INTEGER NOT NULL CHECK(time_ms >= 0),
    body TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL
);
-- 术语表当前状态；每次修改同时写入 glossary_history 并推进 projects.glossary_revision。
CREATE TABLE IF NOT EXISTS glossaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    source_term TEXT NOT NULL,
    required_translation TEXT NOT NULL,
    forbidden_terms TEXT NOT NULL DEFAULT '[]',
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(project_id,source_term)
);
CREATE TABLE IF NOT EXISTS glossary_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL,
    source_term TEXT NOT NULL,
    required_translation TEXT NOT NULL,
    forbidden_terms TEXT NOT NULL DEFAULT '[]',
    notes TEXT NOT NULL DEFAULT '',
    changed_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_glossary_history_project ON glossary_history(project_id,revision);
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
    reviewer TEXT NOT NULL,
    decision TEXT NOT NULL,
    comment TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id INTEGER NOT NULL UNIQUE REFERENCES versions(id),
    supersedes_version_id INTEGER REFERENCES versions(id),
    glossary_revision INTEGER NOT NULL DEFAULT 0,
    snapshot_hash TEXT NOT NULL,
    manifest TEXT NOT NULL,
    delivered_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id INTEGER,
    details TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

# 兼容旧库的列级迁移。
MIGRATIONS = (
    ("projects", "glossary_revision", "glossary_revision INTEGER NOT NULL DEFAULT 0"),
    ("deliveries", "glossary_revision", "glossary_revision INTEGER NOT NULL DEFAULT 0"),
)


class Database:
    """只负责数据结构、连接和审计写入。"""

    def __init__(self, path: str | os.PathLike[str] = DEFAULT_DB):
        self.path = str(path)
        self._init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            for table, column, ddl in MIGRATIONS:
                columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
                if column not in columns:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")

    def log_audit(self, conn: sqlite3.Connection, actor: str, action: str, entity_type: str,
                  entity_id: int | None, details: dict[str, Any]) -> None:
        conn.execute(
            "INSERT INTO audit_log(actor,action,entity_type,entity_id,details,created_at) VALUES(?,?,?,?,?,?)",
            (actor, action, entity_type, entity_id, json.dumps(details, ensure_ascii=False), utcnow()),
        )
