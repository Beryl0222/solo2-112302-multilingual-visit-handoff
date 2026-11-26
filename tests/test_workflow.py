"""持续收件、确认台账、变更交接与角色视图测试。"""

import json
import unittest

from materials import load_corpus
from reception.planning import PlacementStatus, member_placements
from reception.profiles import PreferenceStatus
from reception.workflow import ReceptionProcessor


def _processor() -> ReceptionProcessor:
    processor = ReceptionProcessor()
    processor.ingest_corpus(load_corpus())
    return processor


class IngestionAndVersionTest(unittest.TestCase):
    def test_records_are_processed_in_arrival_order(self):
        processor = ReceptionProcessor()
        records = sorted(load_corpus()["records"],
                         key=lambda r: r["arrival_order"])
        for record in records:
            processor.ingest(record)
        self.assertEqual(
            [r["id"] for r in processor._records],
            [r["id"] for r in records],
        )

    def test_change_record_creates_new_itinerary_version(self):
        processor = _processor()
        versions = processor.versions
        self.assertGreaterEqual(len(versions), 2)
        self.assertEqual([v.version for v in versions],
                         list(range(1, len(versions) + 1)))
        self.assertIn("变更", versions[-1].basis)
        # 两个版本都必须可走完
        self.assertTrue(all(v.is_feasible() for v in versions))

    def test_late_arrival_extends_processing_without_rebuild_loss(self):
        # 先处理除确认记录外的全部资料，再补一条确认记录
        processor = ReceptionProcessor()
        records = load_corpus()["records"]
        for record in records:
            if record["kind"] != "确认":
                processor.ingest(record)
        versions_before = len(processor.versions)
        confirmation = next(r for r in records if r["kind"] == "确认")
        processor.ingest(confirmation)
        self.assertEqual(len(processor.versions), versions_before)
        self.assertTrue(
            any(p.status == PreferenceStatus.CONFIRMED
                for p in processor.confirmation_ledger)
        )


class ConfirmationAndHandoffTest(unittest.TestCase):
    def setUp(self):
        self.processor = _processor()

    def test_p1_confirmation_is_recorded_against_person(self):
        confirmed = [p for p in self.processor.confirmation_ledger
                     if p.member_id == "P1"
                     and p.status == PreferenceStatus.CONFIRMED]
        self.assertTrue(confirmed)

    def test_p3_prerecorded_audio_refusal_is_preserved(self):
        p3 = next(p for p in self.processor.profiles
                  if p.member_id == "P3")
        self.assertTrue(p3.refuses("替代服务"))

    def test_p2_undecided_companion_stays_pending(self):
        p2 = next(p for p in self.processor.profiles
                  if p.member_id == "P2")
        companion = next(q for q in p2.pending if q.topic == "街区陪行")
        self.assertIn("尚未决定", companion.question)

    def test_change_generates_handoff_with_owner(self):
        change_handoffs = [h for h in self.processor.handoffs
                           if h.topic == "口译延误后的服务改派"]
        self.assertTrue(change_handoffs)
        for handoff in change_handoffs:
            self.assertTrue(handoff.follow_up_owner)
            self.assertTrue(handoff.member_ids)
            self.assertEqual(handoff.status, PreferenceStatus.PENDING)

    def test_every_pending_question_is_handed_off_once(self):
        pending_topics = {
            (q.member_id, q.topic)
            for profile in self.processor.profiles
            for q in profile.pending
        }
        handed = {
            (member_id, h.topic)
            for h in self.processor.handoffs
            for member_id in h.member_ids
        }
        self.assertTrue(pending_topics.issubset(handed))

    def test_dispatch_is_blocked_until_all_confirmed(self):
        self.assertFalse(self.processor.dispatch_ready())

    def test_dispatch_opens_only_after_in_person_resolution(self):
        processor = _processor()
        self.assertFalse(processor.dispatch_ready())

        # 模拟接待员当面核实：先关闭成员画像中的待核实问题，
        # 再循环关闭排程核对中暴露的开放问题，直到没有待确认项。
        for _ in range(5):
            for profile in list(processor.profiles):
                for question in list(profile.pending):
                    processor.resolve_pending_question(
                        profile.member_id, question.topic,
                        "本人当面确认按现安排执行",
                    )
            placements = {p.member_id: p for p in processor.placements()}
            if all(not p.open_questions and not p.blocking_reasons
                   for p in placements.values()):
                break
            for member_id, placement in placements.items():
                for question in list(placement.open_questions):
                    processor.resolve_pending_question(
                        member_id, "排程核对", "本人当面确认服务安排",
                        question_text=question,
                    )

        self.assertTrue(processor.dispatch_ready())
        for version in processor.coordinator_view()["itinerary_versions"]:
            self.assertTrue(version["dispatch_ready"])
        # 确认台账只记录本人已表态的决定（确认或本人拒绝），没有系统代答
        self.assertTrue(processor.confirmation_ledger)
        self.assertTrue(all(
            item.status in (PreferenceStatus.CONFIRMED,
                            PreferenceStatus.DECLINED)
            for item in processor.confirmation_ledger
        ))

    def test_split_evaluation_keeps_objecting_member_unplaced(self):
        split = self.processor.evaluate_split()
        self.assertFalse(split.is_feasible())
        placements = {
            p.member_id: p
            for p in member_placements(
                self.processor.profiles, split, self.processor.model
            )
        }
        self.assertEqual(
            placements["P3"].status, PlacementStatus.BLOCKED
        )


class RoleViewTest(unittest.TestCase):
    def setUp(self):
        self.processor = _processor()
        self.raw_texts = [r["text"] for r in load_corpus()["records"]]

    def test_coordinator_sees_arrangements_questions_and_owners(self):
        view = self.processor.coordinator_view()
        self.assertEqual(view["role"], "协调员")
        member_ids = {m["member_id"] for m in view["members"]}
        self.assertEqual(member_ids, {"P1", "P2", "P3", "U1", "U2"})
        for member in view["members"]:
            self.assertIn("placement", member)
            for question in member["pending_questions"]:
                self.assertTrue(question["follow_up_owner"])

    def test_venue_view_contains_only_service_facts(self):
        view = self.processor.venue_view("侨批馆")
        serialized = json.dumps(view, ensure_ascii=False)
        # 不得出现成员原始表述与隐私内容
        for raw in self.raw_texts:
            self.assertNotIn(raw, serialized)
        self.assertNotIn("个人问题", serialized)
        self.assertNotIn("拒绝", serialized)
        # 只保留服务所需的语言/移动信息与时间
        self.assertTrue(view["language_needs"])
        self.assertTrue(any(
            need["need"].startswith("移动方式")
            for need in view["mobility_needs"]
        ))

    def test_venue_view_is_scoped_to_that_venue(self):
        museum = self.processor.venue_view("侨批馆")
        street = self.processor.venue_view("开埠街")
        # P2 的轮椅需求只限楼内，街区视图不应出现移动方式需求
        self.assertFalse(
            any(need["member_id"] == "P2"
                for need in street["mobility_needs"])
        )
        self.assertTrue(
            any(need["member_id"] == "P2"
                for need in museum["mobility_needs"])
        )

    def test_school_view_has_versions_but_no_personal_wording(self):
        view = self.processor.school_view()
        serialized = json.dumps(view, ensure_ascii=False)
        for raw in self.raw_texts:
            self.assertNotIn(raw, serialized)
        self.assertNotIn("个人问题", serialized)
        self.assertTrue(view["itinerary_versions"])
        self.assertIn("reservation_id", view)
        # 改派确认只记结构化决定，不记原话
        for item in view["reassignment_confirmations"]:
            self.assertIn("decision", item)
            self.assertLess(len(item["decision"]), 12)

    def test_unknown_venue_raises(self):
        with self.assertRaises(KeyError):
            self.processor.venue_view("不存在的场馆")


if __name__ == "__main__":
    unittest.main()
