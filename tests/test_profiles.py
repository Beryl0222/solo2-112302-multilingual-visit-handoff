"""成员资料、来源权限与带范围表达的测试。"""

import unittest

from materials import load_corpus
from reception.profiles import (
    Activity,
    GroupClaim,
    Source,
    build_group_profiles,
    extract_profile,
    record_source,
    teacher_claim_binds_adult,
)


def _record(record_id: str) -> dict:
    return next(r for r in load_corpus()["records"] if r["id"] == record_id)


class SourceAuthorityTest(unittest.TestCase):
    def test_self_submission_is_bound_to_person(self):
        profile = extract_profile(_record("P1"))
        self.assertEqual(profile.member_id, "P1")
        self.assertTrue(profile.is_adult)
        self.assertTrue(all(p.source == Source.SELF for p in profile.preferences))

    def test_oral_supplement_is_self_source_not_minority(self):
        # "本人口头补充"是提交方式，不能据此把成员当成未成年
        profile = extract_profile(_record("P2"))
        self.assertIsNone(profile.is_adult)
        self.assertEqual(
            record_source(_record("P2")), Source.SELF
        )

    def test_teacher_form_is_separate_source(self):
        self.assertEqual(record_source(_record("G2")), Source.TEACHER)

    def test_teacher_cannot_choose_accessibility_for_adult(self):
        profile = extract_profile(_record("P1"))
        claim = GroupClaim(
            text="英语导览即可", source_id="G2",
            source=Source.TEACHER, verified_per_adult=False,
        )
        self.assertFalse(teacher_claim_binds_adult(claim, profile))

        # 逐位成年人确认后才生效
        confirmed = GroupClaim(
            text="英语导览即可", source_id="G2",
            source=Source.TEACHER, verified_per_adult=True,
        )
        self.assertTrue(teacher_claim_binds_adult(confirmed, profile))

    def test_teacher_claim_never_overwrites_self_preferences(self):
        profiles, claims = build_group_profiles(load_corpus()["records"])
        before = {
            p.member_id: [(x.channel, x.value) for x in p.preferences]
            for p in profiles
        }
        teacher_claim = next(c for c in claims if c.source_id == "G2")
        for profile in profiles:
            teacher_claim_binds_adult(teacher_claim, profile)
        after = {
            p.member_id: [(x.channel, x.value) for x in p.preferences]
            for p in profiles
        }
        self.assertEqual(before, after)


class ScopedExpressionTest(unittest.TestCase):
    def test_mandarin_listening_with_english_subtitle(self):
        profile = extract_profile(_record("P1"))
        listening = next(p for p in profile.preferences
                         if p.channel == "听讲语言")
        self.assertEqual(listening.value, "普通话")
        # 本人明示可听普通话覆盖三类语言活动
        self.assertEqual(
            set(listening.scope),
            {Activity.SHOWCASE, Activity.STREET, Activity.EXCHANGE},
        )
        subtitle = next(p for p in profile.preferences
                        if p.channel == "字幕语言")
        self.assertEqual(subtitle.value, "英文")
        # 字幕要求绑定展柜讲解，不外推到步行街
        self.assertEqual(set(subtitle.scope), {Activity.SHOWCASE})

    def test_wheelchair_is_scoped_to_building_not_whole_trip(self):
        profile = extract_profile(_record("P2"))
        mobility = next(p for p in profile.preferences
                        if p.channel == "移动方式")
        self.assertEqual(mobility.value, "楼内轮椅通行")
        self.assertEqual(set(mobility.scope), {Activity.SHOWCASE})
        self.assertFalse(mobility.applies_to(Activity.STREET))
        # 街区陪行由本人决定，未决定前是待核实问题
        topics = {q.topic for q in profile.pending}
        self.assertIn("街区陪行", topics)

    def test_only_first_floor_wheelchair_does_not_become_full_limit(self):
        record = {
            "id": "PX", "kind": "成员",
            "text": "陈某，成年，本人提交，只在一楼使用轮椅，楼上自行步行。",
            "arrival_order": 99, "references": [],
        }
        profile = extract_profile(record)
        mobility = next(p for p in profile.preferences
                        if p.channel == "移动方式")
        self.assertEqual(set(mobility.scope), {Activity.SHOWCASE})
        self.assertIn("一楼", mobility.value)
        # 不得出现"全程轮椅"之类的推断结果
        self.assertFalse(
            any("全程" in p.value for p in profile.preferences)
        )
        self.assertTrue(profile.pending)

    def test_uncertain_detail_becomes_pending_question_not_assumption(self):
        record = {
            "id": "PY", "kind": "成员",
            "text": "王某，成年，本人提交，馆内可能需要轮椅（还不确定）。",
            "arrival_order": 98, "references": [],
        }
        profile = extract_profile(record)
        self.assertFalse(
            any(p.channel == "移动方式" for p in profile.preferences)
        )
        self.assertTrue(
            any(q.topic == "移动方式" for q in profile.pending)
        )


class DistinctNeedsTest(unittest.TestCase):
    def test_directional_chinese_is_not_professional_listening(self):
        profile = extract_profile(_record("P3"))
        directional = next(p for p in profile.preferences
                           if p.channel == "日常问路语言")
        self.assertEqual(directional.value, "中文")
        # 问路能力没有活动范围，不能当作专业讲解能力
        self.assertFalse(directional.applies_to(Activity.SHOWCASE))
        self.assertFalse(
            any(p.channel == "听讲语言" for p in profile.preferences)
        )

    def test_following_interpreter_is_not_consent_to_pooled_question(self):
        record = {
            "id": "PZ", "kind": "成员",
            "text": "李某，成年，本人提交，可以跟随口译，但不同意把问题"
                    "汇总成一个发言。",
            "arrival_order": 97, "references": [],
        }
        profile = extract_profile(record)
        self.assertTrue(
            any(p.channel == "口译跟随" for p in profile.preferences)
        )
        self.assertTrue(profile.refuses("汇总发言", Activity.EXCHANGE))

    def test_private_needs_are_marked_private_to_person(self):
        profile = extract_profile(_record("P3"))
        privacy = next(p for p in profile.preferences if p.channel == "隐私")
        self.assertEqual(privacy.source, Source.SELF)
        self.assertIn("本人", privacy.value)

    def test_refusal_to_split_is_bound_to_person(self):
        profile = extract_profile(_record("P3"))
        self.assertTrue(profile.refuses("拆组"))


class GroupBuildTest(unittest.TestCase):
    def test_unregistered_members_are_placeholders_with_pending_needs(self):
        profiles, _ = build_group_profiles(load_corpus()["records"])
        placeholders = [p for p in profiles if p.is_placeholder]
        # 团组 5 人，登记 3 人，应有 2 个占位成员
        self.assertEqual(len(placeholders), 2)
        for profile in placeholders:
            self.assertFalse(profile.preferences)
            topics = {q.topic for q in profile.pending}
            self.assertEqual(topics, {"讲解语言", "移动方式"})

    def test_all_pending_questions_carry_follow_up_owner(self):
        profiles, _ = build_group_profiles(load_corpus()["records"])
        for profile in profiles:
            for question in profile.pending:
                self.assertTrue(question.follow_up_owner)
                self.assertTrue(question.member_id == profile.member_id)


if __name__ == "__main__":
    unittest.main()
