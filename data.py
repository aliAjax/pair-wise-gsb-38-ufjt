"""Persistence layer: SQLite schema, migrations and row-level access.

This module owns data only. Business decisions (glossary conflict rules,
state-machine gating, revision lineage rules) live in ``judgment.Service``.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "subtitle_qc.db"

UNFINISHED_STATUSES = ("draft", "review", "approved", "locked")
DELIVERED_STATUSES = ("delivered", "superseded")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DomainError(Exception):
    def __init__(self, message: str, status: int = 400, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.status = status
        self.details = details


class Database:
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
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS projects (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    source_language TEXT NOT NULL,
                    duration_ms INTEGER NOT NULL CHECK(duration_ms > 0),
                    owner TEXT NOT NULL,
                    media_name TEXT NOT NULL,
                    media_sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id INTEGER NOT NULL REFERENCES projects(id),
                    language TEXT NOT NULL,
                    version_no INTEGER NOT NULL,
                    parent_id INTEGER REFERENCES versions(id),
                    root_version_id INTEGER REFERENCES versions(id),
                    baseline_glossary_revision INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'draft',
                    revision INTEGER NOT NULL DEFAULT 0,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(project_id,language,version_no)
                );
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
                CREATE TABLE IF NOT EXISTS glossary_revisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    revision_no INTEGER NOT NULL,
                    created_by TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    UNIQUE(project_id,revision_no)
                );
                CREATE TABLE IF NOT EXISTS glossary_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    revision_no INTEGER NOT NULL,
                    source_term TEXT NOT NULL,
                    required_translation TEXT NOT NULL,
                    forbidden_terms TEXT NOT NULL DEFAULT '[]',
                    notes TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    UNIQUE(project_id,revision_no,source_term)
                );
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
            )
            self._migrate(conn)

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """Upgrade databases created before glossary revisioning existed."""
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "versions" in tables:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(versions)")}
            if "root_version_id" not in cols:
                conn.execute("ALTER TABLE versions ADD COLUMN root_version_id INTEGER")
            if "baseline_glossary_revision" not in cols:
                conn.execute("ALTER TABLE versions ADD COLUMN baseline_glossary_revision INTEGER NOT NULL DEFAULT 0")
        if "deliveries" in tables:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(deliveries)")}
            if "glossary_revision" not in cols:
                conn.execute("ALTER TABLE deliveries ADD COLUMN glossary_revision INTEGER NOT NULL DEFAULT 0")
        if "glossaries" not in tables:
            return
        # Freeze every legacy glossary as immutable revision 1 per project.
        legacy = [dict(r) for r in conn.execute("SELECT * FROM glossaries")]
        for row in legacy:
            exists = conn.execute(
                "SELECT 1 FROM glossary_revisions WHERE project_id=?", (row["project_id"],)
            ).fetchone()
            if exists:
                continue
            conn.execute(
                "INSERT INTO glossary_revisions(project_id,revision_no,created_by,note,created_at) VALUES(?,?,?,?,?)",
                (row["project_id"], 1, "migration", "从旧术语表迁移", utcnow()),
            )
            conn.execute(
                """INSERT INTO glossary_entries(project_id,revision_no,source_term,required_translation,forbidden_terms,notes,created_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (row["project_id"], 1, row["source_term"], row["required_translation"],
                 row["forbidden_terms"], row["notes"], utcnow()),
            )
        conn.execute(
            "UPDATE versions SET root_version_id=id WHERE root_version_id IS NULL"
        )
        conn.execute(
            """UPDATE versions SET baseline_glossary_revision=
               COALESCE((SELECT MAX(revision_no) FROM glossary_revisions gr WHERE gr.project_id=versions.project_id),0)
               WHERE baseline_glossary_revision=0"""
        )

    def _audit(self, conn: sqlite3.Connection, actor: str, action: str, entity_type: str,
               entity_id: int | None, details: dict[str, Any]) -> None:
        conn.execute(
            "INSERT INTO audit_log(actor,action,entity_type,entity_id,details,created_at) VALUES(?,?,?,?,?,?)",
            (actor, action, entity_type, entity_id, json.dumps(details, ensure_ascii=False), utcnow()),
        )

    # ---- projects -------------------------------------------------------
    def create_project(self, actor: str, payload: dict[str, Any]) -> dict[str, Any]:
        name = str(payload.get("name", "")).strip()
        source_language = str(payload.get("source_language", "")).strip()
        media_name = str(payload.get("media_name", "")).strip()
        media_sha = str(payload.get("media_sha256", "")).lower()
        try:
            duration_ms = int(payload.get("duration_ms"))
        except (TypeError, ValueError) as exc:
            raise DomainError("成片时长必须是毫秒整数") from exc
        if not name or not source_language or not media_name or duration_ms <= 0 or len(media_sha) != 64:
            raise DomainError("项目名称、源语言、成片、时长或校验值不完整")
        with self.connect() as conn:
            try:
                cur = conn.execute(
                    "INSERT INTO projects(name,source_language,duration_ms,owner,media_name,media_sha256,created_at) VALUES(?,?,?,?,?,?,?)",
                    (name, source_language, duration_ms, actor, media_name, media_sha, utcnow()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("项目名称已存在", 409) from exc
            self._audit(conn, actor, "project.created", "project", cur.lastrowid, {"name": name})
            return dict(conn.execute("SELECT * FROM projects WHERE id=?", (cur.lastrowid,)).fetchone())

    def get_project(self, project_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()

    def list_projects(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM projects ORDER BY id").fetchall()]

    # ---- glossary -------------------------------------------------------
    def current_glossary_revision(self, project_id: int) -> int:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT MAX(revision_no) rev FROM glossary_revisions WHERE project_id=?", (project_id,)
            ).fetchone()
            return int(row["rev"] or 0)

    def glossary_entries(self, project_id: int, revision_no: int) -> list[dict[str, Any]]:
        if not revision_no:
            return []
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM glossary_entries WHERE project_id=? AND revision_no=? ORDER BY source_term",
                (project_id, revision_no),
            ).fetchall()
            return [dict(r) | {"forbidden_terms": json.loads(r["forbidden_terms"])} for r in rows]

    def save_glossary_term(self, project_id: int, actor: str, source_term: str,
                           required: str, forbidden: list[str], notes: str) -> int:
        """Append an immutable revision: full copy of current terms plus one upsert."""
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current_no = int(conn.execute(
                "SELECT COALESCE(MAX(revision_no),0) rev FROM glossary_revisions WHERE project_id=?",
                (project_id,),
            ).fetchone()["rev"])
            new_no = current_no + 1
            entries: dict[str, sqlite3.Row] = {}
            if current_no:
                for r in conn.execute(
                    "SELECT * FROM glossary_entries WHERE project_id=? AND revision_no=?",
                    (project_id, current_no),
                ):
                    entries[r["source_term"]] = r
            entries[source_term] = (source_term, required, json.dumps(forbidden, ensure_ascii=False), notes)
            conn.execute(
                "INSERT INTO glossary_revisions(project_id,revision_no,created_by,note,created_at) VALUES(?,?,?,?,?)",
                (project_id, new_no, actor, notes, utcnow()),
            )
            for term, payload in entries.items():
                if isinstance(payload, sqlite3.Row):
                    values = (payload["source_term"], payload["required_translation"],
                              payload["forbidden_terms"], payload["notes"])
                else:
                    values = payload
                conn.execute(
                    """INSERT INTO glossary_entries(project_id,revision_no,source_term,required_translation,forbidden_terms,notes,created_at)
                       VALUES(?,?,?,?,?,?,?)""",
                    (project_id, new_no, *values, utcnow()),
                )
            self._audit(conn, actor, "glossary.revision_created", "project", project_id,
                        {"revision_no": new_no, "source_term": source_term})
            return new_no

    # ---- versions -------------------------------------------------------
    def create_version_row(self, project_id: int, language: str, parent_id: int | None,
                           root_id: int | None, baseline_revision: int, actor: str) -> dict[str, Any]:
        with self.connect() as conn:
            next_no = int(conn.execute(
                "SELECT COALESCE(MAX(version_no),0)+1 value FROM versions WHERE project_id=? AND language=?",
                (project_id, language),
            ).fetchone()["value"])
            cur = conn.execute(
                """INSERT INTO versions(project_id,language,version_no,parent_id,root_version_id,
                   baseline_glossary_revision,created_by,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (project_id, language, next_no, parent_id, root_id, baseline_revision, actor, utcnow(), utcnow()),
            )
            version_id = int(cur.lastrowid)
            if parent_id is None:
                conn.execute("UPDATE versions SET root_version_id=id WHERE id=?", (version_id,))
            self._audit(conn, actor, "version.created", "version", version_id,
                        {"language": language, "version_no": next_no, "parent_id": parent_id})
            return dict(conn.execute("SELECT * FROM versions WHERE id=?", (version_id,)).fetchone())

    def get_version(self, version_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT v.*,p.owner,p.duration_ms FROM versions v JOIN projects p ON p.id=v.project_id WHERE v.id=?",
                (version_id,),
            ).fetchone()

    def set_status(self, version_id: int, status: str) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE versions SET status=?,updated_at=? WHERE id=?", (status, utcnow(), version_id))

    def list_versions(self, project_id: int | None = None) -> list[dict[str, Any]]:
        with self.connect() as conn:
            if project_id:
                rows = conn.execute("SELECT * FROM versions WHERE project_id=? ORDER BY id", (project_id,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM versions ORDER BY id").fetchall()
            return [dict(r) for r in rows]

    def unfinished_versions_in_root(self, root_id: int) -> list[dict[str, Any]]:
        placeholders = ",".join("?" for _ in UNFINISHED_STATUSES)
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM versions WHERE root_version_id=? AND status IN ({placeholders}) ORDER BY id",
                (root_id, *UNFINISHED_STATUSES),
            ).fetchall()
            return [dict(r) for r in rows]

    def list_lineage(self, root_id: int) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT v.id,v.project_id,v.language,v.version_no,v.parent_id,v.root_version_id,
                          v.baseline_glossary_revision,v.status,v.revision,v.created_at,
                          d.id AS delivery_id,d.glossary_revision AS pinned_glossary_revision,d.snapshot_hash,
                          d.created_at AS delivered_at
                   FROM versions v LEFT JOIN deliveries d ON d.version_id=v.id
                   WHERE v.root_version_id=? ORDER BY v.version_no""",
                (root_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    # ---- assignments ----------------------------------------------------
    def add_assignment(self, version_id: int, user: str, assignment_role: str, actor: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO assignments(version_id,user,role,assigned_by,created_at) VALUES(?,?,?,?,?)",
                (version_id, user, assignment_role, actor, utcnow()),
            )
            self._audit(conn, actor, "assignment.saved", "version", version_id,
                        {"user": user, "role": assignment_role})

    def assignment_exists(self, version_id: int, user: str, role: str | None = None) -> bool:
        with self.connect() as conn:
            if role:
                row = conn.execute(
                    "SELECT 1 FROM assignments WHERE version_id=? AND user=? AND role=?", (version_id, user, role)
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT 1 FROM assignments WHERE version_id=? AND user=?", (version_id, user)
                ).fetchone()
            return bool(row)

    def editor_exists(self, version_id: int, user: str) -> bool:
        with self.connect() as conn:
            return bool(conn.execute(
                "SELECT 1 FROM assignments WHERE version_id=? AND user=? AND role IN ('translator','timeline')",
                (version_id, user),
            ).fetchone())

    # ---- cues -----------------------------------------------------------
    def list_cues(self, version_id: int) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM cues WHERE version_id=? ORDER BY cue_index", (version_id,)).fetchall()]

    def get_cue(self, version_id: int, cue_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM cues WHERE id=? AND version_id=?", (cue_id, version_id)).fetchone()

    def find_overlap(self, version_id: int, start_ms: int, end_ms: int, cue_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM cues WHERE version_id=? AND id<>? AND start_ms<? AND end_ms>? LIMIT 1",
                (version_id, cue_id, end_ms, start_ms),
            ).fetchone()

    def find_index_owner(self, version_id: int, cue_index: int, cue_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM cues WHERE version_id=? AND cue_index=? AND id<>?",
                (version_id, cue_index, cue_id),
            ).fetchone()

    def has_cues(self, version_id: int) -> bool:
        with self.connect() as conn:
            return bool(conn.execute("SELECT 1 FROM cues WHERE version_id=?", (version_id,)).fetchone())

    def upsert_cue(self, version_id: int, cue_id: int | None, cue_index: int, start_ms: int,
                   end_ms: int, text: str, actor: str) -> tuple[int, int]:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if cue_id is not None:
                conn.execute(
                    "UPDATE cues SET cue_index=?,start_ms=?,end_ms=?,text=?,updated_by=?,updated_at=? WHERE id=?",
                    (cue_index, start_ms, end_ms, text, actor, utcnow(), cue_id),
                )
                saved_id = cue_id
            else:
                cur = conn.execute(
                    "INSERT INTO cues(version_id,cue_index,start_ms,end_ms,text,updated_by,updated_at) VALUES(?,?,?,?,?,?,?)",
                    (version_id, cue_index, start_ms, end_ms, text, actor, utcnow()),
                )
                saved_id = int(cur.lastrowid)
            revision = int(conn.execute("SELECT revision FROM versions WHERE id=?", (version_id,)).fetchone()["revision"]) + 1
            conn.execute("UPDATE versions SET revision=?,updated_at=? WHERE id=?", (revision, utcnow(), version_id))
            self._audit(conn, actor, "cue.saved", "version", version_id, {"cue_id": saved_id, "revision": revision})
            row = conn.execute("SELECT * FROM cues WHERE id=?", (saved_id,)).fetchone()
            return dict(row), revision

    def copy_cues(self, source_version_id: int, target_version_id: int, actor: str) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                """INSERT INTO cues(version_id,cue_index,start_ms,end_ms,text,updated_by,updated_at)
                   SELECT ?,cue_index,start_ms,end_ms,text,?,? FROM cues WHERE version_id=?""",
                (target_version_id, actor, utcnow(), source_version_id),
            )
            self._audit(conn, actor, "version.cues_copied", "version", target_version_id,
                        {"source_version_id": source_version_id, "count": cur.rowcount})
            return cur.rowcount

    def cue_exists(self, version_id: int, cue_id: int) -> bool:
        with self.connect() as conn:
            return bool(conn.execute(
                "SELECT 1 FROM cues WHERE id=? AND version_id=?", (cue_id, version_id)
            ).fetchone())

    # ---- comments / reviews --------------------------------------------
    def add_comment(self, version_id: int, cue_id: int | None, user: str, time_ms: int, body: str) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO comments(version_id,cue_id,user,time_ms,body,created_at) VALUES(?,?,?,?,?,?)",
                (version_id, cue_id, user, time_ms, body, utcnow()),
            )
            self._audit(conn, user, "comment.added", "version", version_id,
                        {"comment_id": cur.lastrowid, "time_ms": time_ms})
            return int(cur.lastrowid)

    def list_comments(self, version_id: int) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM comments WHERE version_id=? ORDER BY id", (version_id,)).fetchall()]

    def add_review(self, version_id: int, reviewer: str, decision: str, comment: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO reviews(version_id,reviewer,decision,comment,created_at) VALUES(?,?,?,?,?)",
                (version_id, reviewer, decision, comment, utcnow()),
            )
            self._audit(conn, reviewer, f"version.{decision}", "version", version_id, {"comment": comment})

    # ---- deliveries -----------------------------------------------------
    def delivery_for_version(self, version_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM deliveries WHERE version_id=?", (version_id,)).fetchone()

    def latest_delivery_in_language(self, project_id: int, language: str,
                                    exclude_version_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                """SELECT d.* FROM deliveries d JOIN versions v ON v.id=d.version_id
                   WHERE v.project_id=? AND v.language=? AND v.id<>? ORDER BY d.id DESC LIMIT 1""",
                (project_id, language, exclude_version_id),
            ).fetchone()

    def insert_delivery(self, version_id: int, supersedes_version_id: int | None, glossary_revision: int,
                        snapshot_hash: str, manifest: str, actor: str) -> dict[str, Any]:
        with self.connect() as conn:
            cur = conn.execute(
                """INSERT INTO deliveries(version_id,supersedes_version_id,glossary_revision,snapshot_hash,manifest,delivered_by,created_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (version_id, supersedes_version_id, glossary_revision, snapshot_hash, manifest, actor, utcnow()),
            )
            self._audit(conn, actor, "version.delivered", "version", version_id,
                        {"snapshot_hash": snapshot_hash, "glossary_revision": glossary_revision})
            return dict(conn.execute("SELECT * FROM deliveries WHERE id=?", (cur.lastrowid,)).fetchone())

    def list_deliveries(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM deliveries ORDER BY id DESC").fetchall()]

    def get_delivery(self, delivery_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM deliveries WHERE id=?", (delivery_id,)).fetchone()

    def audit(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM audit_log ORDER BY id DESC").fetchall()]
