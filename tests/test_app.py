import tempfile
import unittest
from pathlib import Path

from app import seed_demo
from data import Database, DomainError
from judgment import Service, cue_glossary_conflicts


class SubtitleQCFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "test.db")
        seed = seed_demo(self.db)
        self.project, self.version = seed["project"], seed["version"]
        self.svc = Service(self.db)
        self.svc.assign(self.version, "alice", {"user": "bob", "role": "translator"}, "owner")
        self.svc.assign(self.version, "alice", {"user": "carol", "role": "reviewer"}, "owner")

    def tearDown(self):
        self.tmp.cleanup()

    def _cue(self, text="海豹在冰面", idx=1, start=1000, end=3000, rev=0, actor="bob"):
        return self.svc.save_cue(self.version, actor, {"cue_index": idx, "start_ms": start,
                                                       "end_ms": end, "text": text,
                                                       "expected_revision": rev})

    def test_full_review_lock_delivery_and_overwrite_protection(self):
        cue = self._cue("seal 海豹在冰面")
        self.assertEqual(cue["version_revision"], 1)
        comment = self.svc.add_comment(self.version, "carol", {"cue_id": cue["id"], "time_ms": 1200,
                                                               "body": "术语正确，请确认冻结时间"}, "reviewer")
        self.assertEqual(comment["time_ms"], 1200)
        self.svc.submit(self.version, "bob")
        approved = self.svc.review(self.version, "carol", {"decision": "approve", "comment": "通过"}, "reviewer")
        self.assertEqual(approved["status"], "approved")
        self.svc.lock(self.version, "alice")
        delivery = self.svc.deliver(self.version, "alice")
        self.assertEqual(len(delivery["snapshot_hash"]), 64)
        self.assertEqual(delivery["glossary_revision"], 1)
        with self.assertRaisesRegex(DomainError, "只有草稿"):
            self._cue("海豹", rev=1)

    def test_revision_overlap_glossary_and_permissions(self):
        first = self._cue("海豹")
        with self.assertRaisesRegex(DomainError, "其他成员修改"):
            self.svc.save_cue(self.version, "bob", {"cue_id": first["id"], "cue_index": 1,
                                                   "start_ms": 1000, "end_ms": 2500,
                                                   "text": "海豹", "expected_revision": 0})
        with self.assertRaisesRegex(DomainError, "重叠"):
            self._cue("另一句", idx=2, start=2500, end=4000, rev=1)
        with self.assertRaisesRegex(DomainError, "禁用译法"):
            self._cue("密封装置", idx=2, start=3500, end=4000, rev=1)
        with self.assertRaisesRegex(DomainError, "权限"):
            self._cue("海豹", idx=2, start=3500, end=4000, rev=1, actor="carol")

    # ---- terminology snapshots -----------------------------------------
    def test_glossary_edit_appends_immutable_revision_and_old_snapshot_untouched(self):
        self._cue("海豹在冰面")
        self.svc.submit(self.version, "bob")
        self.svc.review(self.version, "carol", {"decision": "approve", "comment": "通过"}, "reviewer")
        self.svc.deliver(self.version, "alice")
        first_delivery = self.db.get_delivery(1)
        import json
        first_manifest = json.loads(first_delivery["manifest"])
        self.assertEqual(first_manifest["glossary"][0]["required_translation"], "海豹")

        # Glossary changes after delivery: immutable revision 2 is appended.
        result = self.svc.set_glossary(self.project, "alice",
                                       {"source_term": "seal", "required_translation": "海狗",
                                        "forbidden_terms": ["密封", "海豹"], "notes": "修订"}, "owner")
        self.assertEqual(result["revision_no"], 2)
        history = self.svc.glossary(self.project)
        self.assertEqual([r["revision_no"] for r in history["revisions"]], [1, 2])
        self.assertEqual(history["revisions"][0]["terms"][0]["required_translation"], "海豹")

        # Old snapshot stays byte-identical.
        first_again = self.db.get_delivery(1)
        self.assertEqual(first_again["snapshot_hash"], first_delivery["snapshot_hash"])
        self.assertEqual(json.loads(first_again["manifest"])["glossary"][0]["required_translation"], "海豹")

    def test_legacy_conflicts_listed_and_block_submit_review_deliver(self):
        self._cue("海豹在冰面", rev=0)
        # Glossary moves: old cue now conflicts. Undelivered version is affected.
        result = self.svc.set_glossary(self.project, "alice",
                                       {"source_term": "seal", "required_translation": "海狗",
                                        "forbidden_terms": ["密封", "海豹"], "notes": "修订"}, "owner")
        affected = result["affected_versions"]
        self.assertEqual(len(affected), 1)
        self.assertEqual(affected[0]["version_id"], self.version)
        conflict_text = affected[0]["conflicts"][0]["text"]
        self.assertIn("海豹", conflict_text)

        cf = self.svc.conflicts(self.version)
        self.assertTrue(cf["blocked"])
        self.assertGreaterEqual(cf["count"], 1)
        self.assertEqual(cf["conflicts"][0]["cue_index"], 1)

        with self.assertRaisesRegex(DomainError, "挡住提交复核"):
            self.svc.submit(self.version, "bob")

        # Force it into review, then approval must be blocked and bounced back.
        self.db.set_status(self.version, "review")
        with self.assertRaisesRegex(DomainError, "挡住复核通过"):
            self.svc.review(self.version, "carol", {"decision": "approve", "comment": ""}, "reviewer")
        self.assertEqual(self.db.get_version(self.version)["status"], "draft")

        # Fix every conflicting sentence: saving forbidden text is blocked,
        # then the corrected cue clears the gate.
        with self.assertRaisesRegex(DomainError, "禁用译法"):
            self._cue("海豹在另一处", idx=2, start=3500, end=4000, rev=1)
        fixed = self.svc.save_cue(self.version, "bob", {"cue_id": 1, "cue_index": 1,
                                                        "start_ms": 1000, "end_ms": 3000,
                                                        "text": "海狗在冰面",
                                                        "expected_revision": 1})
        self.assertEqual(fixed["remaining_conflicts"], [])
        self.assertFalse(self.svc.conflicts(self.version)["blocked"])
        self.svc.submit(self.version, "bob")
        self.svc.review(self.version, "carol", {"decision": "approve", "comment": "通过"}, "reviewer")
        delivery = self.svc.deliver(self.version, "alice")
        self.assertEqual(delivery["glossary_revision"], 2)

    def test_delivery_pins_glossary_and_later_edit_does_not_revive_conflicts(self):
        self._cue("海豹在冰面")
        self.svc.submit(self.version, "bob")
        self.svc.review(self.version, "carol", {"decision": "approve", "comment": "通过"}, "reviewer")
        self.svc.deliver(self.version, "alice")
        self.svc.set_glossary(self.project, "alice",
                              {"source_term": "seal", "required_translation": "海狗",
                               "forbidden_terms": ["密封", "海豹"], "notes": "修订"}, "owner")
        # Delivered version shows no conflicts: its snapshot is fixed.
        cf = self.svc.conflicts(self.version)
        self.assertFalse(cf["blocked"])
        # Delivered version cannot be re-delivered or edited.
        with self.assertRaises(DomainError):
            self.svc.deliver(self.version, "alice")
        with self.assertRaisesRegex(DomainError, "只有草稿"):
            self.svc.save_cue(self.version, "bob", {"cue_index": 1, "start_ms": 1000,
                                                    "end_ms": 3000, "text": "海狗",
                                                    "expected_revision": 1})

    # ---- post-delivery revisions ---------------------------------------
    def test_revision_branches_from_delivered_source_and_copies_cues(self):
        self._cue("海豹在冰面")
        self.svc.submit(self.version, "bob")
        self.svc.review(self.version, "carol", {"decision": "approve", "comment": "通过"}, "reviewer")
        self.svc.deliver(self.version, "alice")
        self.svc.set_glossary(self.project, "alice",
                              {"source_term": "seal", "required_translation": "海狗",
                               "forbidden_terms": ["密封", "海豹"], "notes": "修订"}, "owner")
        rev = self.svc.create_version(self.project, "alice", {"language": "zh-CN",
                                                              "parent_id": self.version}, "owner")
        self.assertEqual(rev["parent_id"], self.version)
        self.assertEqual(rev["root_version_id"], self.version)
        self.assertEqual(rev["baseline_glossary_revision"], 2)
        copied = self.db.list_cues(rev["id"])
        self.assertEqual(len(copied), 1)
        # Inherited cue conflicts with the current glossary and must be fixed.
        cf = self.svc.conflicts(rev["id"])
        self.assertTrue(cf["blocked"])
        with self.assertRaisesRegex(DomainError, "挡住提交复核"):
            self.svc.submit(rev["id"], "alice")

    def test_only_one_unfinished_record_per_source(self):
        self._cue("海豹在冰面")
        self.svc.submit(self.version, "bob")
        self.svc.review(self.version, "carol", {"decision": "approve", "comment": "通过"}, "reviewer")
        self.svc.deliver(self.version, "alice")
        rev1 = self.svc.create_version(self.project, "alice", {"language": "zh-CN",
                                                               "parent_id": self.version}, "owner")
        with self.assertRaisesRegex(DomainError, "已有未完成版本"):
            self.svc.create_version(self.project, "alice", {"language": "zh-CN",
                                                            "parent_id": self.version}, "owner")
        # Cannot branch from an undelivered version either.
        with self.assertRaisesRegex(DomainError, "只能从已交付版本"):
            self.svc.create_version(self.project, "alice", {"language": "zh-CN",
                                                            "parent_id": rev1["id"]}, "owner")
        # Finish rev1 (deliver) -> the chain may continue from it.
        self.svc.save_cue(rev1["id"], "alice", {"cue_id": self.db.list_cues(rev1["id"])[0]["id"],
                                                "cue_index": 1, "start_ms": 1000, "end_ms": 3000,
                                                "text": "海豹在冰面", "expected_revision": 0}, "owner")
        self.svc.submit(rev1["id"], "alice")
        self.svc.assign(rev1["id"], "alice", {"user": "carol", "role": "reviewer"}, "owner")
        self.svc.review(rev1["id"], "carol", {"decision": "approve", "comment": "通过"}, "reviewer")
        self.svc.deliver(rev1["id"], "alice")
        lineage = self.svc.lineage(self.version)
        self.assertEqual([n["version_no"] for n in lineage["nodes"]], [1, 2])
        self.assertEqual(lineage["unfinished_count"], 0)
        rev2 = self.svc.create_version(self.project, "alice", {"language": "zh-CN",
                                                               "parent_id": rev1["id"]}, "owner")
        self.assertEqual(rev2["root_version_id"], self.version)
        self.assertEqual(lineage["nodes"][0]["delivery_id"], 1)

    def test_pure_conflict_judgment(self):
        entries = [{"source_term": "seal", "required_translation": "海豹", "forbidden_terms": ["密封"]}]
        self.assertEqual(cue_glossary_conflicts("海豹在冰面", entries), [])
        conflicts = cue_glossary_conflicts("密封装置", entries)
        self.assertEqual(conflicts[0]["kind"], "forbidden")
        required = cue_glossary_conflicts("seal 在冰面", entries)
        self.assertEqual(required[0]["kind"], "required")


if __name__ == "__main__":
    unittest.main()
