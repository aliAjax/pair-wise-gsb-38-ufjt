"""判定层：术语维护、遗留检查、复核交付状态机和交付修订。

流程约定：
- 术语每次修改都会推进项目的 glossary_revision 并写入 glossary_history；
- 保存术语后立即对所有未交付版本做遗留检查，返回受影响的具体句子；
- 存在术语冲突的未交付版本会被挡住提交、复核通过和交付，改完才能继续；
- 交付把当时的术语版本和内容固定进快照，后续术语改动不影响旧快照；
- 交付后的修正从原版本另起修订，同一来源只留一条未完成记录。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from .errors import DomainError
from .store import Database, utcnow

# 已完结的版本状态；其余状态都视为"未交付"，要参与遗留检查。
FINAL_STATUSES = ("delivered", "superseded")
_FINAL_FILTER = "','".join(FINAL_STATUSES)


class QCService:
    """所有业务判定集中在这一层，数据读写交给 Database。"""

    def __init__(self, db: Database):
        self.db = db

    # ---------- 项目 ----------

    def create_project(self, actor: str, payload: dict[str, Any], role: str = "owner") -> dict[str, Any]:
        if role not in {"owner", "admin"}:
            raise DomainError("只有项目负责人可以创建项目", 403)
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
        with self.db.connect() as conn:
            try:
                cur = conn.execute(
                    "INSERT INTO projects(name,source_language,duration_ms,owner,media_name,media_sha256,created_at) VALUES(?,?,?,?,?,?,?)",
                    (name, source_language, duration_ms, actor, media_name, media_sha, utcnow()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("项目名称已存在", 409) from exc
            self.db.log_audit(conn, actor, "project.created", "project", cur.lastrowid, {"name": name})
            return dict(conn.execute("SELECT * FROM projects WHERE id=?", (cur.lastrowid,)).fetchone())

    # ---------- 术语维护 + 遗留检查 ----------

    def set_glossary(self, project_id: int, actor: str, payload: dict[str, Any], role: str = "viewer") -> dict[str, Any]:
        source_term = str(payload.get("source_term", "")).strip()
        required = str(payload.get("required_translation", "")).strip()
        forbidden_raw = payload.get("forbidden_terms", [])
        if not source_term or not required or not isinstance(forbidden_raw, list):
            raise DomainError("术语、指定译法和禁用词格式不合法")
        forbidden = [str(t).strip() for t in forbidden_raw if str(t).strip()]
        notes = str(payload.get("notes", ""))
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            project = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
            if not project:
                raise DomainError("项目不存在", 404)
            if actor != project["owner"] and role != "admin":
                raise DomainError("只有项目负责人可以维护术语表", 403)
            conn.execute(
                """INSERT INTO glossaries(project_id,source_term,required_translation,forbidden_terms,notes,created_at)
                   VALUES(?,?,?,?,?,?) ON CONFLICT(project_id,source_term) DO UPDATE SET
                   required_translation=excluded.required_translation,forbidden_terms=excluded.forbidden_terms,notes=excluded.notes""",
                (project_id, source_term, required, json.dumps(forbidden, ensure_ascii=False), notes, utcnow()),
            )
            revision = int(project["glossary_revision"]) + 1
            conn.execute("UPDATE projects SET glossary_revision=? WHERE id=?", (revision, project_id))
            conn.execute(
                "INSERT INTO glossary_history(project_id,revision,source_term,required_translation,forbidden_terms,notes,changed_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (project_id, revision, source_term, required, json.dumps(forbidden, ensure_ascii=False), notes, actor, utcnow()),
            )
            self.db.log_audit(conn, actor, "glossary.saved", "project", project_id,
                              {"source_term": source_term, "glossary_revision": revision})
            # 遗留检查：术语一改动，立刻扫描所有未交付版本。
            impacts = self._project_conflicts(conn, project_id)
        return {
            "project_id": project_id,
            "source_term": source_term,
            "required_translation": required,
            "forbidden_terms": forbidden,
            "glossary_revision": revision,
            "impacts": impacts,
        }

    def glossary_current(self, project_id: int) -> dict[str, Any]:
        with self.db.connect() as conn:
            project = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
            if not project:
                raise DomainError("项目不存在", 404)
            terms = [self._term_row(r) for r in conn.execute(
                "SELECT * FROM glossaries WHERE project_id=? ORDER BY source_term", (project_id,))]
            return {"project_id": project_id, "glossary_revision": int(project["glossary_revision"]), "terms": terms}

    def glossary_history(self, project_id: int) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            if not conn.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone():
                raise DomainError("项目不存在", 404)
            return [
                self._term_row(r) | {"revision": r["revision"], "changed_by": r["changed_by"], "created_at": r["created_at"]}
                for r in conn.execute(
                    "SELECT * FROM glossary_history WHERE project_id=? ORDER BY revision DESC,id DESC", (project_id,))
            ]

    @staticmethod
    def _term_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "source_term": row["source_term"],
            "required_translation": row["required_translation"],
            "forbidden_terms": json.loads(row["forbidden_terms"]),
            "notes": row["notes"],
        }

    # ---------- 术语冲突判定 ----------

    def _terms(self, conn: sqlite3.Connection, project_id: int) -> list[dict[str, Any]]:
        return [self._term_row(r) for r in conn.execute(
            "SELECT * FROM glossaries WHERE project_id=? ORDER BY source_term", (project_id,))]

    @staticmethod
    def _cue_conflicts(terms: list[dict[str, Any]], cue: dict[str, Any]) -> list[dict[str, Any]]:
        """逐句对照当前术语表，返回具体冲突句子。录入校验和遗留检查共用同一套判定。"""
        conflicts: list[dict[str, Any]] = []
        text = cue["text"]
        for term in terms:
            for bad in term["forbidden_terms"]:
                if bad and bad in text:
                    conflicts.append({
                        "cue_id": cue.get("id"),
                        "cue_index": cue["cue_index"],
                        "text": text,
                        "source_term": term["source_term"],
                        "kind": "forbidden",
                        "term": bad,
                        "expected": term["required_translation"],
                        "message": f"第{cue['cue_index']}句「{text}」包含禁用译法 {bad}",
                    })
            # 与录入校验一致：源术语出现在句子里时，必须使用指定译法。
            if term["source_term"] in text and term["required_translation"] not in text:
                conflicts.append({
                    "cue_id": cue.get("id"),
                    "cue_index": cue["cue_index"],
                    "text": text,
                    "source_term": term["source_term"],
                    "kind": "required",
                    "term": term["source_term"],
                    "expected": term["required_translation"],
                    "message": f"第{cue['cue_index']}句「{text}」的术语 {term['source_term']} 需使用指定译法 {term['required_translation']}",
                })
        return conflicts

    def _version_conflicts(self, conn: sqlite3.Connection, version_id: int) -> list[dict[str, Any]]:
        row = conn.execute("SELECT project_id FROM versions WHERE id=?", (version_id,)).fetchone()
        terms = self._terms(conn, int(row["project_id"]))
        conflicts: list[dict[str, Any]] = []
        for cue in conn.execute("SELECT * FROM cues WHERE version_id=? ORDER BY cue_index", (version_id,)):
            conflicts.extend(self._cue_conflicts(terms, dict(cue)))
        return conflicts

    def _project_conflicts(self, conn: sqlite3.Connection, project_id: int) -> list[dict[str, Any]]:
        impacts: list[dict[str, Any]] = []
        rows = conn.execute(
            f"SELECT * FROM versions WHERE project_id=? AND status NOT IN ('{_FINAL_FILTER}') ORDER BY id",
            (project_id,),
        ).fetchall()
        for version in rows:
            conflicts = self._version_conflicts(conn, int(version["id"]))
            if conflicts:
                impacts.append({
                    "version_id": version["id"],
                    "version_no": version["version_no"],
                    "language": version["language"],
                    "status": version["status"],
                    "conflicts": conflicts,
                })
        return impacts

    def _ensure_no_conflicts(self, conn: sqlite3.Connection, version_id: int, action: str) -> None:
        conflicts = self._version_conflicts(conn, version_id)
        if conflicts:
            detail = "；".join(c["message"] for c in conflicts)
            raise DomainError(f"存在 {len(conflicts)} 处术语冲突，{action}前请先修改：{detail}", 409)

    def version_conflicts(self, version_id: int) -> dict[str, Any]:
        with self.db.connect() as conn:
            version = self._version(conn, version_id)
            return {"version_id": version_id, "status": version["status"],
                    "conflicts": self._version_conflicts(conn, version_id)}

    def project_conflicts(self, project_id: int) -> dict[str, Any]:
        with self.db.connect() as conn:
            if not conn.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone():
                raise DomainError("项目不存在", 404)
            return {"project_id": project_id, "impacts": self._project_conflicts(conn, project_id)}

    # ---------- 版本与交付修订 ----------

    def create_version(self, project_id: int, actor: str, payload: dict[str, Any], role: str = "viewer") -> dict[str, Any]:
        language = str(payload.get("language", "")).strip()
        if not language:
            raise DomainError("目标语言不能为空")
        parent_id = payload.get("parent_id")
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            project = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
            if not project:
                raise DomainError("项目不存在", 404)
            if actor != project["owner"] and role != "admin":
                raise DomainError("只有项目负责人可以创建版本", 403)
            if parent_id is not None:
                parent_id = int(parent_id)
                parent = conn.execute("SELECT * FROM versions WHERE id=? AND project_id=?", (parent_id, project_id)).fetchone()
                if not parent or parent["language"] != language:
                    raise DomainError("父版本不存在或目标语言不一致", 409)
                if self._open_revision(conn, parent_id):
                    raise DomainError("同一来源只留一条未完成的修订记录", 409)
            next_no = int(conn.execute(
                "SELECT COALESCE(MAX(version_no),0)+1 value FROM versions WHERE project_id=? AND language=?",
                (project_id, language)).fetchone()["value"])
            try:
                cur = conn.execute(
                    "INSERT INTO versions(project_id,language,version_no,parent_id,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                    (project_id, language, next_no, parent_id, actor, utcnow(), utcnow()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("同一来源只留一条未完成的修订记录", 409) from exc
            self.db.log_audit(conn, actor, "version.created", "version", cur.lastrowid,
                              {"language": language, "version_no": next_no})
            return dict(conn.execute("SELECT * FROM versions WHERE id=?", (cur.lastrowid,)).fetchone())

    @staticmethod
    def _open_revision(conn: sqlite3.Connection, source_id: int) -> sqlite3.Row | None:
        return conn.execute(
            f"SELECT * FROM versions WHERE parent_id=? AND status NOT IN ('{_FINAL_FILTER}') ORDER BY id DESC LIMIT 1",
            (source_id,),
        ).fetchone()

    def create_revision(self, version_id: int, actor: str, role: str = "viewer") -> dict[str, Any]:
        """交付后的修正：从原交付版本另起修订，复制字幕和人员；同一来源只留一条未完成记录。"""
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            source = self._version(conn, version_id)
            if actor != source["owner"] and role != "admin":
                raise DomainError("只有项目负责人可以发起交付修订", 403)
            if source["status"] != "delivered":
                raise DomainError("只有已交付版本可以另起修订", 409)
            existing = self._open_revision(conn, version_id)
            if existing:
                return dict(existing) | {"existing": True}
            next_no = int(conn.execute(
                "SELECT COALESCE(MAX(version_no),0)+1 value FROM versions WHERE project_id=? AND language=?",
                (source["project_id"], source["language"])).fetchone()["value"])
            try:
                cur = conn.execute(
                    "INSERT INTO versions(project_id,language,version_no,parent_id,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                    (source["project_id"], source["language"], next_no, version_id, actor, utcnow(), utcnow()),
                )
            except sqlite3.IntegrityError:
                existing = self._open_revision(conn, version_id)
                return dict(existing) | {"existing": True}
            new_id = int(cur.lastrowid)
            conn.execute(
                """INSERT INTO cues(version_id,cue_index,start_ms,end_ms,text,updated_by,updated_at)
                   SELECT ?,cue_index,start_ms,end_ms,text,?,? FROM cues WHERE version_id=?""",
                (new_id, actor, utcnow(), version_id),
            )
            copied = int(conn.execute("SELECT COUNT(*) c FROM cues WHERE version_id=?", (new_id,)).fetchone()["c"])
            conn.execute(
                """INSERT OR IGNORE INTO assignments(version_id,user,role,assigned_by,created_at)
                   SELECT ?,user,role,?,? FROM assignments WHERE version_id=?""",
                (new_id, actor, utcnow(), version_id),
            )
            self.db.log_audit(conn, actor, "version.revision_created", "version", new_id,
                              {"source_version_id": version_id, "copied_cues": copied})
            return dict(conn.execute("SELECT * FROM versions WHERE id=?", (new_id,)).fetchone()) | {
                "existing": False, "copied_cues": copied}

    def assign(self, version_id: int, actor: str, payload: dict[str, Any], role: str = "viewer") -> dict[str, Any]:
        user = str(payload.get("user", "")).strip()
        assignment_role = str(payload.get("role", "")).strip()
        if not user or assignment_role not in {"translator", "timeline", "reviewer"}:
            raise DomainError("人员或角色不合法")
        with self.db.connect() as conn:
            version = conn.execute(
                "SELECT v.*,p.owner FROM versions v JOIN projects p ON p.id=v.project_id WHERE v.id=?",
                (version_id,)).fetchone()
            if not version:
                raise DomainError("版本不存在", 404)
            if actor != version["owner"] and role != "admin":
                raise DomainError("只有项目负责人可以分配人员", 403)
            conn.execute("INSERT OR IGNORE INTO assignments(version_id,user,role,assigned_by,created_at) VALUES(?,?,?,?,?)",
                         (version_id, user, assignment_role, actor, utcnow()))
            self.db.log_audit(conn, actor, "assignment.saved", "version", version_id, {"user": user, "role": assignment_role})
        return {"version_id": version_id, "user": user, "role": assignment_role}

    # ---------- 字幕与评论 ----------

    def _version(self, conn: sqlite3.Connection, version_id: int) -> sqlite3.Row:
        row = conn.execute(
            "SELECT v.*,p.owner,p.duration_ms FROM versions v JOIN projects p ON p.id=v.project_id WHERE v.id=?",
            (version_id,)).fetchone()
        if not row:
            raise DomainError("字幕版本不存在", 404)
        return row

    def _can_edit(self, conn: sqlite3.Connection, version: sqlite3.Row, actor: str) -> bool:
        if actor == version["owner"]:
            return True
        return bool(conn.execute(
            "SELECT 1 FROM assignments WHERE version_id=? AND user=? AND role IN ('translator','timeline')",
            (version["id"], actor)).fetchone())

    def save_cue(self, version_id: int, actor: str, payload: dict[str, Any], role: str = "viewer") -> dict[str, Any]:
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            version = self._version(conn, version_id)
            if version["status"] != "draft":
                raise DomainError("只有草稿版本可以修改字幕", 409)
            if not self._can_edit(conn, version, actor):
                raise DomainError("没有该版本的翻译或时间轴权限", 403)
            expected = payload.get("expected_revision")
            if expected is not None and int(expected) != int(version["revision"]):
                raise DomainError("版本已被其他成员修改，请刷新后重试", 409)
            try:
                cue_index = int(payload.get("cue_index"))
                start_ms = int(payload.get("start_ms"))
                end_ms = int(payload.get("end_ms"))
            except (TypeError, ValueError) as exc:
                raise DomainError("字幕序号和时间必须是整数") from exc
            text = str(payload.get("text", "")).strip()
            if cue_index < 0 or start_ms < 0 or end_ms <= start_ms or end_ms > int(version["duration_ms"]) or not text:
                raise DomainError("字幕时间、序号或内容不合法")
            conflicts = self._cue_conflicts(self._terms(conn, int(version["project_id"])),
                                            {"id": payload.get("cue_id"), "cue_index": cue_index, "text": text})
            if conflicts:
                raise DomainError("；".join(c["message"] for c in conflicts))
            cue_id = payload.get("cue_id")
            existing = None
            if cue_id is not None:
                existing = conn.execute("SELECT * FROM cues WHERE id=? AND version_id=?", (int(cue_id), version_id)).fetchone()
                if not existing:
                    raise DomainError("字幕条目不存在", 404)
            overlap = conn.execute(
                "SELECT * FROM cues WHERE version_id=? AND id<>? AND start_ms<? AND end_ms>? LIMIT 1",
                (version_id, int(cue_id or -1), end_ms, start_ms),
            ).fetchone()
            if overlap:
                raise DomainError("字幕时间轴发生重叠", 409)
            index_owner = conn.execute("SELECT * FROM cues WHERE version_id=? AND cue_index=? AND id<>?",
                                       (version_id, cue_index, int(cue_id or -1))).fetchone()
            if index_owner:
                raise DomainError("字幕序号已被使用", 409)
            if existing:
                conn.execute("UPDATE cues SET cue_index=?,start_ms=?,end_ms=?,text=?,updated_by=?,updated_at=? WHERE id=?",
                             (cue_index, start_ms, end_ms, text, actor, utcnow(), existing["id"]))
                saved_id = int(existing["id"])
            else:
                cur = conn.execute(
                    "INSERT INTO cues(version_id,cue_index,start_ms,end_ms,text,updated_by,updated_at) VALUES(?,?,?,?,?,?,?)",
                    (version_id, cue_index, start_ms, end_ms, text, actor, utcnow()))
                saved_id = int(cur.lastrowid)
            revision = int(version["revision"]) + 1
            conn.execute("UPDATE versions SET revision=?,updated_at=? WHERE id=?", (revision, utcnow(), version_id))
            self.db.log_audit(conn, actor, "cue.saved", "version", version_id, {"cue_id": saved_id, "revision": revision})
            saved = dict(conn.execute("SELECT * FROM cues WHERE id=?", (saved_id,)).fetchone())
            remaining = self._version_conflicts(conn, version_id)
        return saved | {"version_revision": revision, "conflicts": remaining}

    def add_comment(self, version_id: int, actor: str, payload: dict[str, Any], role: str = "viewer") -> dict[str, Any]:
        body = str(payload.get("body", "")).strip()
        try:
            time_ms = int(payload.get("time_ms"))
        except (TypeError, ValueError) as exc:
            raise DomainError("评论时间必须是毫秒整数") from exc
        with self.db.connect() as conn:
            version = self._version(conn, version_id)
            allowed = actor == version["owner"] or conn.execute(
                "SELECT 1 FROM assignments WHERE version_id=? AND user=?", (version_id, actor)).fetchone()
            if not allowed:
                raise DomainError("只有项目成员可以评论", 403)
            if not body or time_ms < 0 or time_ms > int(version["duration_ms"]):
                raise DomainError("评论内容或时间点不合法")
            cue_id = payload.get("cue_id")
            if cue_id is not None and not conn.execute("SELECT 1 FROM cues WHERE id=? AND version_id=?",
                                                       (int(cue_id), version_id)).fetchone():
                raise DomainError("评论关联的字幕不存在", 404)
            cur = conn.execute("INSERT INTO comments(version_id,cue_id,user,time_ms,body,created_at) VALUES(?,?,?,?,?,?)",
                               (version_id, cue_id, actor, time_ms, body, utcnow()))
            self.db.log_audit(conn, actor, "comment.added", "version", version_id,
                              {"comment_id": cur.lastrowid, "time_ms": time_ms})
        return {"id": int(cur.lastrowid), "version_id": version_id, "cue_id": cue_id,
                "user": actor, "time_ms": time_ms, "body": body, "status": "open"}

    # ---------- 复核交付状态机 ----------

    def submit(self, version_id: int, actor: str, role: str = "viewer") -> dict[str, Any]:
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            version = self._version(conn, version_id)
            if version["status"] != "draft" or not self._can_edit(conn, version, actor):
                raise DomainError("只有草稿版本的翻译或时间轴人员可以提交复核", 409)
            if not conn.execute("SELECT 1 FROM cues WHERE version_id=?", (version_id,)).fetchone():
                raise DomainError("空版本不能提交复核", 409)
            self._ensure_no_conflicts(conn, version_id, "提交复核")
            conn.execute("UPDATE versions SET status='review',updated_at=? WHERE id=?", (utcnow(), version_id))
            self.db.log_audit(conn, actor, "version.submitted", "version", version_id, {})
        return dict(conn.execute("SELECT * FROM versions WHERE id=?", (version_id,)).fetchone())

    def review(self, version_id: int, actor: str, payload: dict[str, Any], role: str = "viewer") -> dict[str, Any]:
        decision = str(payload.get("decision", "")).strip()
        if decision not in {"approve", "reject"}:
            raise DomainError("复核决定必须是 approve 或 reject")
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            version = self._version(conn, version_id)
            if version["status"] != "review":
                raise DomainError("版本当前不在复核阶段", 409)
            assigned = conn.execute("SELECT 1 FROM assignments WHERE version_id=? AND user=? AND role='reviewer'",
                                    (version_id, actor)).fetchone()
            if not assigned and actor != version["owner"]:
                raise DomainError("没有该版本的复核权限", 403)
            if actor == version["created_by"]:
                raise DomainError("创建人不能复核自己的版本", 403)
            # 退回不受冲突限制：它是改完才能继续的回流路径；批准必须先清冲突。
            if decision == "approve":
                self._ensure_no_conflicts(conn, version_id, "复核通过")
            conn.execute("INSERT INTO reviews(version_id,reviewer,decision,comment,created_at) VALUES(?,?,?,?,?)",
                         (version_id, actor, decision, str(payload.get("comment", "")), utcnow()))
            status = "approved" if decision == "approve" else "draft"
            conn.execute("UPDATE versions SET status=?,updated_at=? WHERE id=?", (status, utcnow(), version_id))
            self.db.log_audit(conn, actor, f"version.{decision}", "version", version_id,
                              {"comment": payload.get("comment", "")})
        return dict(conn.execute("SELECT * FROM versions WHERE id=?", (version_id,)).fetchone())

    def reopen(self, version_id: int, actor: str, role: str = "viewer") -> dict[str, Any]:
        """已批准/锁定版本出现术语冲突时退回草稿，改完重新走流程。"""
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            version = self._version(conn, version_id)
            if version["status"] not in {"approved", "locked"}:
                raise DomainError("只有已批准或锁定的版本可以退回修改", 409)
            if actor != version["owner"] and role != "admin":
                raise DomainError("只有项目负责人可以退回版本", 403)
            conn.execute("UPDATE versions SET status='draft',updated_at=? WHERE id=?", (utcnow(), version_id))
            self.db.log_audit(conn, actor, "version.reopened", "version", version_id, {})
        return dict(conn.execute("SELECT * FROM versions WHERE id=?", (version_id,)).fetchone())

    def lock(self, version_id: int, actor: str, role: str = "viewer") -> dict[str, Any]:
        with self.db.connect() as conn:
            version = self._version(conn, version_id)
            if version["status"] != "approved":
                raise DomainError("只有已批准版本可以锁定", 409)
            if actor != version["owner"] and role != "admin":
                raise DomainError("只有项目负责人可以锁定版本", 403)
            conn.execute("UPDATE versions SET status='locked',updated_at=? WHERE id=?", (utcnow(), version_id))
            self.db.log_audit(conn, actor, "version.locked", "version", version_id, {})
        return dict(conn.execute("SELECT * FROM versions WHERE id=?", (version_id,)).fetchone())

    def deliver(self, version_id: int, actor: str, role: str = "viewer") -> dict[str, Any]:
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            version = self._version(conn, version_id)
            if actor != version["owner"] and role != "admin":
                raise DomainError("只有项目负责人可以交付", 403)
            if version["status"] not in {"approved", "locked"}:
                raise DomainError("只有批准或锁定版本可以交付", 409)
            if conn.execute("SELECT 1 FROM deliveries WHERE version_id=?", (version_id,)).fetchone():
                raise DomainError("该版本已经交付，不能用新内容覆盖", 409)
            self._ensure_no_conflicts(conn, version_id, "交付")
            glossary_revision = int(conn.execute("SELECT glossary_revision FROM projects WHERE id=?",
                                                 (version["project_id"],)).fetchone()["glossary_revision"])
            cues = [dict(r) for r in conn.execute(
                "SELECT cue_index,start_ms,end_ms,text FROM cues WHERE version_id=? ORDER BY cue_index", (version_id,))]
            glossary = [dict(r) for r in conn.execute(
                "SELECT source_term,required_translation,forbidden_terms FROM glossaries WHERE project_id=? ORDER BY source_term",
                (version["project_id"],))]
            # 交付即固定当时术语：术语版本号和内容一起进入快照，后续改动不影响旧快照。
            manifest = {"project_id": version["project_id"], "version_id": version_id,
                        "language": version["language"], "version_no": version["version_no"],
                        "glossary_revision": glossary_revision, "cues": cues, "glossary": glossary}
            snapshot_hash = hashlib.sha256(
                json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            previous = conn.execute(
                "SELECT id FROM deliveries WHERE version_id IN (SELECT id FROM versions WHERE project_id=? AND language=? AND id<>?) ORDER BY id DESC LIMIT 1",
                (version["project_id"], version["language"], version_id)).fetchone()
            if previous:
                conn.execute("UPDATE versions SET status='superseded',updated_at=? WHERE id=(SELECT version_id FROM deliveries WHERE id=?)",
                             (utcnow(), previous["id"]))
            cur = conn.execute(
                "INSERT INTO deliveries(version_id,supersedes_version_id,glossary_revision,snapshot_hash,manifest,delivered_by,created_at) VALUES(?,?,?,?,?,?,?)",
                (version_id, previous["id"] if previous else None, glossary_revision, snapshot_hash,
                 json.dumps(manifest, ensure_ascii=False, sort_keys=True), actor, utcnow()),
            )
            conn.execute("UPDATE versions SET status='delivered',updated_at=? WHERE id=?", (utcnow(), version_id))
            self.db.log_audit(conn, actor, "version.delivered", "version", version_id,
                              {"snapshot_hash": snapshot_hash, "glossary_revision": glossary_revision})
        return dict(conn.execute("SELECT * FROM deliveries WHERE id=?", (cur.lastrowid,)).fetchone())

    # ---------- 查询 ----------

    def list_projects(self) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM projects ORDER BY id").fetchall()]

    def list_versions(self, project_id: int | None = None) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            if project_id:
                rows = conn.execute("SELECT * FROM versions WHERE project_id=? ORDER BY id", (project_id,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM versions ORDER BY id").fetchall()
            versions = []
            for row in rows:
                item = dict(row)
                item["conflict_count"] = 0 if row["status"] in FINAL_STATUSES else len(self._version_conflicts(conn, int(row["id"])))
                versions.append(item)
            return versions

    def version_detail(self, version_id: int) -> dict[str, Any]:
        with self.db.connect() as conn:
            version = self._version(conn, version_id)
            data = dict(version)
            data["conflicts"] = self._version_conflicts(conn, version_id)
            delivery = conn.execute("SELECT * FROM deliveries WHERE version_id=?", (version_id,)).fetchone()
            data["delivery"] = dict(delivery) if delivery else None
            data["revisions"] = [dict(r) for r in conn.execute(
                "SELECT * FROM versions WHERE parent_id=? ORDER BY id", (version_id,)).fetchall()]
            return data

    def list_cues(self, version_id: int) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM cues WHERE version_id=? ORDER BY cue_index", (version_id,)).fetchall()]

    def list_comments(self, version_id: int) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM comments WHERE version_id=? ORDER BY id", (version_id,)).fetchall()]

    def list_deliveries(self) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM deliveries ORDER BY id DESC").fetchall()]

    def delivery_detail(self, delivery_id: int) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM deliveries WHERE id=?", (delivery_id,)).fetchone()
            if not row:
                raise DomainError("交付记录不存在", 404)
            data = dict(row)
            data["manifest"] = json.loads(row["manifest"])
            return data

    def audit(self) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM audit_log ORDER BY id DESC").fetchall()]


def seed_demo(service: QCService) -> dict[str, int]:
    projects = service.list_projects()
    if projects:
        return {"project": int(projects[0]["id"])}
    project = service.create_project("alice", {
        "name": "极地纪录片字幕", "source_language": "en", "media_name": "polar.mp4",
        "media_sha256": "b" * 64, "duration_ms": 120000}, "owner")
    service.set_glossary(project["id"], "alice", {
        "source_term": "seal", "required_translation": "海豹",
        "forbidden_terms": ["密封"], "notes": "动物学语境"}, "owner")
    version = service.create_version(project["id"], "alice", {"language": "zh-CN"}, "owner")
    return {"project": int(project["id"]), "version": int(version["id"])}
