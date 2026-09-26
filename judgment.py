"""Judgment layer: terminology rules, conflict detection and the review/delivery state machine.

This module contains no HTTP and no HTML. It reads and writes only through the
:class:`data.Database` facade so data, judgments and pages stay separate.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from data import Database, DomainError, UNFINISHED_STATUSES


# ---- pure terminology judgment -----------------------------------------
def cue_glossary_conflicts(text: str, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return concrete conflicts between one cue and a glossary snapshot.

    Each conflict carries the sentence fragment plus which term caused it, so
    the page can point reviewers at the exact cue instead of a vague count.
    """
    conflicts: list[dict[str, Any]] = []
    for entry in entries:
        for forbidden in entry.get("forbidden_terms", []):
            if forbidden and forbidden in text:
                conflicts.append({
                    "kind": "forbidden",
                    "source_term": entry["source_term"],
                    "term": forbidden,
                    "expected": entry["required_translation"],
                    "reason": f"包含禁用译法“{forbidden}”，应改为“{entry['required_translation']}”",
                })
        # The required translation only applies when the source term itself
        # appears in the localized cue, so unrelated cues are not forced to
        # repeat every glossary word.
        if entry["source_term"] in text and entry["required_translation"] not in text:
            conflicts.append({
                "kind": "required",
                "source_term": entry["source_term"],
                "term": entry["source_term"],
                "expected": entry["required_translation"],
                "reason": f"术语“{entry['source_term']}”必须使用指定译法“{entry['required_translation']}”",
            })
    return conflicts


def version_glossary_conflicts(db: Database, version: dict[str, Any] | Any) -> list[dict[str, Any]]:
    """Compare an undelivered version's cues against the current glossary.

    Delivered/superseded versions are exempt: their snapshot pinned the
    glossary, so later terminology edits can never re-open them. Everything
    else (drafts, review, approved, locked, freshly copied post-delivery
    revisions) is checked against the live glossary, including legacy cues
    that survived a glossary edit.
    """
    version_id = int(version["id"])
    project_id = int(version["project_id"])
    if str(version["status"]) in {"delivered", "superseded"}:
        return []
    current_revision = db.current_glossary_revision(project_id)
    if not current_revision:
        return []
    entries = db.glossary_entries(project_id, current_revision)
    results: list[dict[str, Any]] = []
    for cue in db.list_cues(version_id):
        for conflict in cue_glossary_conflicts(cue["text"], entries):
            results.append({
                "cue_id": cue["id"],
                "cue_index": cue["cue_index"],
                "text": cue["text"],
                **conflict,
            })
    return results


def _conflict_error(action_label: str, conflicts: list[dict[str, Any]]) -> DomainError:
    sentences = "；".join(f"#{c['cue_index']}「{c['text']}」{c['reason']}" for c in conflicts[:10])
    return DomainError(
        f"字幕与当前术语表存在 {len(conflicts)} 处冲突，已挡住{action_label}：{sentences}",
        409,
        {"conflicts": conflicts, "count": len(conflicts)},
    )


class Service:
    def __init__(self, db: Database):
        self.db = db

    # ---- helpers --------------------------------------------------------
    def _project(self, project_id: int) -> dict[str, Any]:
        project = self.db.get_project(project_id)
        if not project:
            raise DomainError("项目不存在", 404)
        return dict(project)

    def _version(self, version_id: int) -> dict[str, Any]:
        version = self.db.get_version(version_id)
        if not version:
            raise DomainError("字幕版本不存在", 404)
        return dict(version)

    @staticmethod
    def _require_owner(version_or_project: dict[str, Any], actor: str, role: str) -> None:
        if actor != version_or_project["owner"] and role != "admin":
            raise DomainError("只有项目负责人可以执行此操作", 403)

    def can_edit(self, version: dict[str, Any], actor: str) -> bool:
        return actor == version["owner"] or self.db.editor_exists(version["id"], actor)

    def _conflict_gate(self, version: dict[str, Any], action_label: str) -> list[dict[str, Any]]:
        conflicts = version_glossary_conflicts(self.db, version)
        if conflicts:
            raise _conflict_error(action_label, conflicts)
        return conflicts

    # ---- projects & glossary -------------------------------------------
    def create_project(self, actor: str, payload: dict[str, Any], role: str = "viewer") -> dict[str, Any]:
        if role not in {"owner", "admin"}:
            raise DomainError("只有项目负责人可以创建项目", 403)
        return self.db.create_project(actor, payload)

    def set_glossary(self, project_id: int, actor: str, payload: dict[str, Any],
                     role: str = "viewer") -> dict[str, Any]:
        project = self._project(project_id)
        self._require_owner(project, actor, role)
        source_term = str(payload.get("source_term", "")).strip()
        required = str(payload.get("required_translation", "")).strip()
        forbidden = payload.get("forbidden_terms", [])
        if not source_term or not required or not isinstance(forbidden, list) or not all(isinstance(t, str) for t in forbidden):
            raise DomainError("术语、指定译法和禁用词格式不合法")
        new_revision = self.db.save_glossary_term(
            project_id, actor, source_term, required, forbidden, str(payload.get("notes", ""))
        )
        # Legacy check: every not-yet-delivered version is re-checked at once so
        # editors and reviewers immediately see which sentences were affected.
        affected: list[dict[str, Any]] = []
        for version in self.db.list_versions(project_id):
            if version["status"] in {"delivered", "superseded"}:
                continue
            conflicts = version_glossary_conflicts(self.db, version)
            if conflicts:
                affected.append({"version_id": version["id"], "language": version["language"],
                                 "version_no": version["version_no"], "status": version["status"],
                                 "conflicts": conflicts})
        return {"project_id": project_id, "revision_no": new_revision, "source_term": source_term,
                "required_translation": required, "forbidden_terms": forbidden, "affected_versions": affected}

    # ---- versions & assignments ----------------------------------------
    def create_version(self, project_id: int, actor: str, payload: dict[str, Any],
                       role: str = "viewer") -> dict[str, Any]:
        project = self._project(project_id)
        self._require_owner(project, actor, role)
        language = str(payload.get("language", "")).strip()
        if not language:
            raise DomainError("目标语言不能为空")
        parent_id = payload.get("parent_id")
        parent = None
        root_id: int | None = None
        if parent_id is not None:
            parent = self._version(int(parent_id))
            if parent["project_id"] != project_id or parent["language"] != language:
                raise DomainError("父版本不存在或目标语言不一致", 409)
            # Post-delivery corrections branch from a delivered source only, so
            # old content stays queryable and drafts cannot fork into parallel
            # revisions.
            if parent["status"] not in {"delivered", "superseded"}:
                raise DomainError("只能从已交付版本另起修订", 409)
            root_id = int(parent["root_version_id"] or parent["id"])
            # One source keeps at most a single unfinished record.
            open_versions = self.db.unfinished_versions_in_root(root_id)
            if open_versions:
                v = open_versions[0]
                raise DomainError(
                    f"该来源已有未完成版本（版本 #{v['version_no']}，状态 {v['status']}），完成后才能另起修订",
                    409,
                    {"open_version_id": v["id"], "status": v["status"]},
                )
        baseline = self.db.current_glossary_revision(project_id)
        version = self.db.create_version_row(project_id, language, int(parent_id) if parent_id is not None else None,
                                             root_id, baseline, actor)
        if parent_id is not None:
            self.db.copy_cues(int(parent_id), version["id"], actor)
        return version

    def assign(self, version_id: int, actor: str, payload: dict[str, Any], role: str = "viewer") -> dict[str, Any]:
        version = self._version(version_id)
        project = self._project(version["project_id"])
        self._require_owner(project, actor, role)
        user = str(payload.get("user", "")).strip()
        assignment_role = str(payload.get("role", "")).strip()
        if not user or assignment_role not in {"translator", "timeline", "reviewer"}:
            raise DomainError("人员或角色不合法")
        self.db.add_assignment(version_id, user, assignment_role, actor)
        return {"version_id": version_id, "user": user, "role": assignment_role}

    # ---- cues -----------------------------------------------------------
    def save_cue(self, version_id: int, actor: str, payload: dict[str, Any], role: str = "viewer") -> dict[str, Any]:
        version = self._version(version_id)
        if version["status"] != "draft":
            raise DomainError("只有草稿版本可以修改字幕", 409)
        if not self.can_edit(version, actor):
            raise DomainError("没有该版本的翻译或时间轴权限", 403)
        try:
            expected = payload.get("expected_revision")
            if expected is not None and int(expected) != int(version["revision"]):
                raise DomainError("版本已被其他成员修改，请刷新后重试", 409)
            cue_index = int(payload.get("cue_index"))
            start_ms = int(payload.get("start_ms"))
            end_ms = int(payload.get("end_ms"))
        except (TypeError, ValueError) as exc:
            raise DomainError("字幕序号和时间必须是整数") from exc
        text = str(payload.get("text", "")).strip()
        if cue_index < 0 or start_ms < 0 or end_ms <= start_ms or end_ms > int(version["duration_ms"]) or not text:
            raise DomainError("字幕时间、序号或内容不合法")
        # Saving always follows the current glossary: forbidden / wrong
        # translations are stopped immediately while editing.
        current_entries = self.db.glossary_entries(version["project_id"],
                                                   self.db.current_glossary_revision(version["project_id"]))
        save_conflicts = cue_glossary_conflicts(text, current_entries)
        if save_conflicts:
            raise DomainError(save_conflicts[0]["reason"], 409, {"conflicts": save_conflicts})
        cue_id = payload.get("cue_id")
        if cue_id is not None:
            cue_id = int(cue_id)
            if not self.db.get_cue(version_id, cue_id):
                raise DomainError("字幕条目不存在", 404)
        else:
            cue_id = None
        if self.db.find_overlap(version_id, start_ms, end_ms, cue_id or -1):
            raise DomainError("字幕时间轴发生重叠", 409)
        if self.db.find_index_owner(version_id, cue_index, cue_id or -1):
            raise DomainError("字幕序号已被使用", 409)
        saved, revision = self.db.upsert_cue(version_id, cue_id, cue_index, start_ms, end_ms, text, actor)
        # After the edit, the version must carry zero legacy conflicts.
        remaining = version_glossary_conflicts(self.db, {**version, "revision": revision})
        return saved | {"version_revision": revision, "remaining_conflicts": remaining}

    def add_comment(self, version_id: int, actor: str, payload: dict[str, Any], role: str = "viewer") -> dict[str, Any]:
        version = self._version(version_id)
        project = self._project(version["project_id"])
        allowed = actor == project["owner"] or self.db.assignment_exists(version_id, actor)
        if not allowed:
            raise DomainError("只有项目成员可以评论", 403)
        body = str(payload.get("body", "")).strip()
        try:
            time_ms = int(payload.get("time_ms"))
        except (TypeError, ValueError) as exc:
            raise DomainError("评论时间必须是毫秒整数") from exc
        if not body or time_ms < 0 or time_ms > int(version["duration_ms"]):
            raise DomainError("评论内容或时间点不合法")
        cue_id = payload.get("cue_id")
        if cue_id is not None and not self.db.cue_exists(version_id, int(cue_id)):
            raise DomainError("评论关联的字幕不存在", 404)
        comment_id = self.db.add_comment(version_id, int(cue_id) if cue_id is not None else None, actor, time_ms, body)
        return {"id": comment_id, "version_id": version_id, "cue_id": cue_id,
                "user": actor, "time_ms": time_ms, "body": body, "status": "open"}

    # ---- state machine --------------------------------------------------
    def submit(self, version_id: int, actor: str, role: str = "viewer") -> dict[str, Any]:
        version = self._version(version_id)
        if version["status"] != "draft" or not self.can_edit(version, actor):
            raise DomainError("只有草稿版本的翻译或时间轴人员可以提交复核", 409)
        if not self.db.has_cues(version_id):
            raise DomainError("空版本不能提交复核", 409)
        self._conflict_gate(version, "提交复核")
        self.db.set_status(version_id, "review")
        return self._version(version_id)

    def review(self, version_id: int, actor: str, payload: dict[str, Any], role: str = "viewer") -> dict[str, Any]:
        decision = str(payload.get("decision", "")).strip()
        if decision not in {"approve", "reject"}:
            raise DomainError("复核决定必须是 approve 或 reject")
        version = self._version(version_id)
        if version["status"] != "review":
            raise DomainError("版本当前不在复核阶段", 409)
        if not self.db.assignment_exists(version_id, actor, "reviewer") and actor != version["owner"]:
            raise DomainError("没有该版本的复核权限", 403)
        if actor == version["created_by"]:
            raise DomainError("创建人不能复核自己的版本", 403)
        comment = str(payload.get("comment", ""))
        if decision == "approve":
            # A glossary edit while the version was in review blocks approval.
            conflicts = version_glossary_conflicts(self.db, version)
            if conflicts:
                # Return it to draft so editors can fix the concrete sentences;
                # nothing sits in review unblockable.
                self.db.add_review(version_id, actor, "reject",
                                   "术语表已更新，存在字幕冲突，退回修改：" + comment)
                self.db.set_status(version_id, "draft")
                raise _conflict_error("复核通过（版本已退回草稿）", conflicts)
            self.db.add_review(version_id, actor, "approve", comment)
            self.db.set_status(version_id, "approved")
        else:
            self.db.add_review(version_id, actor, "reject", comment)
            self.db.set_status(version_id, "draft")
        return self._version(version_id)

    def lock(self, version_id: int, actor: str, role: str = "viewer") -> dict[str, Any]:
        version = self._version(version_id)
        project = self._project(version["project_id"])
        self._require_owner(project, actor, role)
        if version["status"] != "approved":
            raise DomainError("只有已批准版本可以锁定", 409)
        self._conflict_gate(version, "锁定")
        self.db.set_status(version_id, "locked")
        return self._version(version_id)

    def deliver(self, version_id: int, actor: str, role: str = "viewer") -> dict[str, Any]:
        version = self._version(version_id)
        project = self._project(version["project_id"])
        self._require_owner(project, actor, role)
        if version["status"] not in {"approved", "locked"}:
            raise DomainError("只有批准或锁定版本可以交付", 409)
        if self.db.delivery_for_version(version_id):
            raise DomainError("该版本已经交付，不能用新内容覆盖", 409)
        self._conflict_gate(version, "交付")
        # Pin the glossary at delivery time. Later glossary revisions can never
        # touch this snapshot: cues and glossary are both frozen inside it.
        pinned_revision = self.db.current_glossary_revision(version["project_id"])
        cues = [{k: cue[k] for k in ("cue_index", "start_ms", "end_ms", "text")}
                for cue in self.db.list_cues(version_id)]
        glossary = [{k: e[k] for k in ("source_term", "required_translation", "forbidden_terms")}
                    for e in self.db.glossary_entries(version["project_id"], pinned_revision)]
        manifest = {"project_id": version["project_id"], "version_id": version_id,
                    "language": version["language"], "version_no": version["version_no"],
                    "glossary_revision": pinned_revision, "cues": cues, "glossary": glossary}
        canonical = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        snapshot_hash = hashlib.sha256(canonical.encode()).hexdigest()
        previous = self.db.latest_delivery_in_language(version["project_id"], version["language"], version_id)
        delivery = self.db.insert_delivery(
            version_id, int(previous["version_id"]) if previous else None,
            pinned_revision, snapshot_hash,
            json.dumps(manifest, ensure_ascii=False, sort_keys=True), actor,
        )
        if previous:
            self.db.set_status(previous["version_id"], "superseded")
        self.db.set_status(version_id, "delivered")
        return delivery

    def reopen(self, version_id: int, actor: str, role: str = "viewer") -> dict[str, Any]:
        """Send an approved/locked version back to draft when conflicts appear."""
        version = self._version(version_id)
        project = self._project(version["project_id"])
        self._require_owner(project, actor, role)
        if version["status"] not in {"approved", "locked"}:
            raise DomainError("只有已批准或锁定版本可以退回草稿", 409)
        self.db.set_status(version_id, "draft")
        return self._version(version_id)

    # ---- read models ----------------------------------------------------
    def conflicts(self, version_id: int) -> dict[str, Any]:
        version = self._version(version_id)
        current = self.db.current_glossary_revision(version["project_id"])
        conflicts = version_glossary_conflicts(self.db, version)
        return {
            "version_id": version_id,
            "status": version["status"],
            "baseline_glossary_revision": version["baseline_glossary_revision"],
            "current_glossary_revision": current,
            "blocked": bool(conflicts),
            "count": len(conflicts),
            "conflicts": conflicts,
        }

    def lineage(self, version_id: int) -> dict[str, Any]:
        version = self._version(version_id)
        root_id = int(version["root_version_id"] or version_id)
        nodes = self.db.list_lineage(root_id)
        unfinished = [n for n in nodes if n["status"] in UNFINISHED_STATUSES]
        return {"root_version_id": root_id, "nodes": nodes,
                "unfinished_count": len(unfinished)}

    def glossary(self, project_id: int) -> dict[str, Any]:
        self._project(project_id)
        current = self.db.current_glossary_revision(project_id)
        revisions = []
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM glossary_revisions WHERE project_id=? ORDER BY revision_no", (project_id,)
            ).fetchall()
            for r in rows:
                entries = self.db.glossary_entries(project_id, r["revision_no"])
                revisions.append({
                    "revision_no": r["revision_no"], "created_by": r["created_by"],
                    "note": r["note"], "created_at": r["created_at"],
                    "terms": [{k: e[k] for k in ("source_term", "required_translation", "forbidden_terms", "notes")}
                              for e in entries],
                })
        return {"project_id": project_id, "current_revision_no": current, "revisions": revisions}
