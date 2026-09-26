import tempfile
import unittest
from pathlib import Path

from app import Database, DomainError, QCService, seed_demo


class SubtitleQCFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = QCService(Database(Path(self.tmp.name) / "test.db"))
        seed = seed_demo(self.service)
        self.project, self.version = seed["project"], seed["version"]
        self.service.assign(self.version, "alice", {"user": "bob", "role": "translator"}, "owner")
        self.service.assign(self.version, "alice", {"user": "carol", "role": "reviewer"}, "owner")

    def tearDown(self):
        self.tmp.cleanup()

    def _cue(self, text="seal 海豹在冰面", **kw):
        payload = {"cue_index": 1, "start_ms": 1000, "end_ms": 3000, "text": text, "expected_revision": 0}
        payload.update(kw)
        return self.service.save_cue(self.version, "bob", payload)

    def _finish(self, version_id):
        self.service.submit(version_id, "bob")
        self.service.review(version_id, "carol", {"decision": "approve", "comment": "通过"}, "reviewer")
        self.service.lock(version_id, "alice")
        return self.service.deliver(version_id, "alice")

    def _set_glossary(self, required="竖琴海豹", forbidden=None):
        return self.service.set_glossary(self.project, "alice", {
            "source_term": "seal", "required_translation": required,
            "forbidden_terms": forbidden if forbidden is not None else ["密封"]}, "owner")

    def test_full_review_lock_delivery_and_overwrite_protection(self):
        cue = self._cue()
        self.assertEqual(cue["version_revision"], 1)
        comment = self.service.add_comment(self.version, "carol",
                                           {"cue_id": cue["id"], "time_ms": 1200, "body": "术语正确，请确认冻结时间"}, "reviewer")
        self.assertEqual(comment["time_ms"], 1200)
        self.service.submit(self.version, "bob")
        approved = self.service.review(self.version, "carol", {"decision": "approve", "comment": "通过"}, "reviewer")
        self.assertEqual(approved["status"], "approved")
        self.service.lock(self.version, "alice")
        delivery = self.service.deliver(self.version, "alice")
        self.assertEqual(len(delivery["snapshot_hash"]), 64)
        self.assertEqual(delivery["glossary_revision"], 1)
        with self.assertRaisesRegex(DomainError, "只有草稿"):
            self.service.save_cue(self.version, "bob", {"cue_id": cue["id"], "cue_index": 1, "start_ms": 1000,
                                                        "end_ms": 2500, "text": "海豹", "expected_revision": 1})

    def test_revision_overlap_glossary_and_permissions(self):
        first = self._cue()
        with self.assertRaisesRegex(DomainError, "其他成员修改"):
            self.service.save_cue(self.version, "bob", {"cue_id": first["id"], "cue_index": 1, "start_ms": 1000,
                                                        "end_ms": 2500, "text": "海豹", "expected_revision": 0})
        with self.assertRaisesRegex(DomainError, "重叠"):
            self.service.save_cue(self.version, "bob", {"cue_index": 2, "start_ms": 2500, "end_ms": 4000,
                                                        "text": "另一句", "expected_revision": 1})
        with self.assertRaisesRegex(DomainError, "禁用译法"):
            self.service.save_cue(self.version, "bob", {"cue_index": 2, "start_ms": 3500, "end_ms": 4000,
                                                        "text": "密封装置", "expected_revision": 1})
        with self.assertRaisesRegex(DomainError, "权限"):
            self.service.save_cue(self.version, "carol", {"cue_index": 2, "start_ms": 3500, "end_ms": 4000,
                                                          "text": "海豹", "expected_revision": 1})

    def test_delivery_pins_glossary_snapshot(self):
        self._cue()
        delivery = self._finish(self.version)
        self.assertEqual(delivery["glossary_revision"], 1)
        # 术语表后续改动不影响旧快照。
        self._set_glossary(required="竖琴海豹", forbidden=["密封", "海报"])
        detail = self.service.delivery_detail(delivery["id"])
        self.assertEqual(detail["glossary_revision"], 1)
        self.assertEqual(detail["snapshot_hash"], delivery["snapshot_hash"])
        terms = {t["source_term"]: t for t in detail["manifest"]["glossary"]}
        self.assertEqual(terms["seal"]["required_translation"], "海豹")
        self.assertEqual(detail["manifest"]["glossary_revision"], 1)
        current = self.service.glossary_current(self.project)
        self.assertEqual(current["glossary_revision"], 2)
        self.assertEqual(current["terms"][0]["required_translation"], "竖琴海豹")

    def test_glossary_change_lists_conflicting_sentences_and_blocks_submit(self):
        cue = self._cue()
        result = self._set_glossary(required="竖琴海豹")
        self.assertEqual(result["glossary_revision"], 2)
        # 遗留检查列出具体句子。
        self.assertEqual(len(result["impacts"]), 1)
        impact = result["impacts"][0]
        self.assertEqual(impact["version_id"], self.version)
        self.assertEqual(impact["conflicts"][0]["text"], "seal 海豹在冰面")
        self.assertEqual(impact["conflicts"][0]["kind"], "required")
        self.assertIn("竖琴海豹", impact["conflicts"][0]["message"])
        # 冲突挡住提交，错误里带具体句子。
        with self.assertRaisesRegex(DomainError, "seal 海豹在冰面"):
            self.service.submit(self.version, "bob")
        # 改完才能继续。
        fixed = self.service.save_cue(self.version, "bob", {"cue_id": cue["id"], "cue_index": 1, "start_ms": 1000,
                                                            "end_ms": 3000, "text": "seal 竖琴海豹在冰面",
                                                            "expected_revision": 1})
        self.assertEqual(fixed["conflicts"], [])
        self.assertEqual(self.service.version_conflicts(self.version)["conflicts"], [])
        submitted = self.service.submit(self.version, "bob")
        self.assertEqual(submitted["status"], "review")

    def test_forbidden_term_conflict_found_by_legacy_check(self):
        self._cue()
        result = self._set_glossary(required="竖琴海豹", forbidden=["密封", "海豹"])
        kinds = {c["kind"] for i in result["impacts"] for c in i["conflicts"]}
        self.assertEqual(kinds, {"forbidden", "required"})
        project_view = self.service.project_conflicts(self.project)
        self.assertEqual(len(project_view["impacts"]), 1)

    def test_conflicts_block_approve_and_deliver_but_allow_rollback(self):
        cue = self._cue()
        self.service.submit(self.version, "bob")
        self._set_glossary(required="竖琴海豹")
        # 复核通过被冲突挡住；退回是回流路径，不受限。
        with self.assertRaisesRegex(DomainError, "术语冲突"):
            self.service.review(self.version, "carol", {"decision": "approve"}, "reviewer")
        rejected = self.service.review(self.version, "carol", {"decision": "reject", "comment": "术语要改"}, "reviewer")
        self.assertEqual(rejected["status"], "draft")
        self.service.save_cue(self.version, "bob", {"cue_id": cue["id"], "cue_index": 1, "start_ms": 1000,
                                                    "end_ms": 3000, "text": "seal 竖琴海豹在冰面", "expected_revision": 1})
        self.service.submit(self.version, "bob")
        self.service.review(self.version, "carol", {"decision": "approve"}, "reviewer")
        # 批准后再改术语，交付同样被挡住；已批准版本先退回草稿再改。
        self._set_glossary(required="港海豹")
        with self.assertRaisesRegex(DomainError, "术语冲突"):
            self.service.deliver(self.version, "alice")
        with self.assertRaisesRegex(DomainError, "只有草稿"):
            self.service.save_cue(self.version, "bob", {"cue_id": cue["id"], "cue_index": 1, "start_ms": 1000,
                                                        "end_ms": 3000, "text": "seal 港海豹在冰面", "expected_revision": 2})
        self.service.reopen(self.version, "alice")
        self.service.save_cue(self.version, "bob", {"cue_id": cue["id"], "cue_index": 1, "start_ms": 1000,
                                                    "end_ms": 3000, "text": "seal 港海豹在冰面", "expected_revision": 2})
        self.service.submit(self.version, "bob")
        self.service.review(self.version, "carol", {"decision": "approve"}, "reviewer")
        self.service.lock(self.version, "alice")
        delivery = self.service.deliver(self.version, "alice")
        self.assertEqual(delivery["glossary_revision"], 3)

    def test_post_delivery_revision_flow(self):
        self._cue()
        delivery = self._finish(self.version)
        # 交付后的修正从原版本另起修订，字幕和人员随修订复制。
        revision = self.service.create_revision(self.version, "alice")
        self.assertFalse(revision["existing"])
        self.assertEqual(revision["parent_id"], self.version)
        self.assertEqual(revision["status"], "draft")
        copied = self.service.list_cues(revision["id"])
        self.assertEqual([c["text"] for c in copied], ["seal 海豹在冰面"])
        # 同一来源只留一条未完成记录：重复发起返回已有记录。
        again = self.service.create_revision(self.version, "alice")
        self.assertTrue(again["existing"])
        self.assertEqual(again["id"], revision["id"])
        open_children = [v for v in self.service.list_versions(self.project)
                         if v["parent_id"] == self.version and v["status"] not in {"delivered", "superseded"}]
        self.assertEqual(len(open_children), 1)
        with self.assertRaisesRegex(DomainError, "只有项目负责人"):
            self.service.create_revision(self.version, "bob")
        # 在修订上改完并交付；旧版本被取代但旧快照仍可查。
        self.service.save_cue(revision["id"], "bob", {"cue_id": copied[0]["id"], "cue_index": 1, "start_ms": 1000,
                                                      "end_ms": 3000, "text": "seal 海豹在浮冰边缘", "expected_revision": 0})
        self._finish(revision["id"])
        source = self.service.version_detail(self.version)
        self.assertEqual(source["status"], "superseded")
        self.assertEqual(source["delivery"]["snapshot_hash"], delivery["snapshot_hash"])
        old = self.service.delivery_detail(delivery["id"])
        self.assertEqual(old["manifest"]["cues"][0]["text"], "seal 海豹在冰面")
        # 已取代版本不能再当来源；新的交付版本可以继续另起修订。
        with self.assertRaisesRegex(DomainError, "只有已交付版本"):
            self.service.create_revision(self.version, "alice")
        third = self.service.create_revision(revision["id"], "alice")
        self.assertEqual(third["parent_id"], revision["id"])
        self.assertFalse(third["existing"])

    def test_glossary_history_and_current(self):
        self._set_glossary(required="海豹")
        self._set_glossary(required="竖琴海豹")
        current = self.service.glossary_current(self.project)
        self.assertEqual(current["glossary_revision"], 3)
        self.assertEqual(current["terms"][0]["required_translation"], "竖琴海豹")
        history = self.service.glossary_history(self.project)
        self.assertEqual([h["revision"] for h in history], [3, 2, 1])
        self.assertEqual(history[0]["changed_by"], "alice")
        self.assertEqual(history[-1]["notes"], "动物学语境")

    def test_same_source_keeps_single_open_version(self):
        self.service.create_version(self.project, "alice", {"language": "zh-CN", "parent_id": self.version}, "owner")
        with self.assertRaisesRegex(DomainError, "只留一条"):
            self.service.create_version(self.project, "alice", {"language": "zh-CN", "parent_id": self.version}, "owner")


if __name__ == "__main__":
    unittest.main()
