"""排程约束、资源冲突与成员安置测试。"""

import unittest

from materials import load_corpus
from reception.planning import (
    Activity,
    PlacementStatus,
    ServiceMode,
    Window,
    all_members_confirmed,
    build_model,
    check_itinerary,
    member_placements,
    plan_group_itinerary,
    plan_split_itinerary,
    to_minutes,
)
from reception.profiles import build_group_profiles


def _records():
    return load_corpus()["records"]


class TimelineFeasibilityTest(unittest.TestCase):
    def setUp(self):
        self.records = _records()
        self.model = build_model(self.records)

    def test_entry_window_is_never_extended_or_split(self):
        itinerary = plan_group_itinerary(self.records, version=2)
        booking = itinerary.booking
        verbal = [s for s in itinerary.segments
                  if s.activity is not Activity.EXCHANGE]
        self.assertGreaterEqual(
            min(s.window.start for s in verbal),
            booking.entry_window.start,
        )
        self.assertLessEqual(
            max(s.window.end for s in verbal),
            booking.entry_window.end,
        )
        # 所有分段共用同一预约号，拆组也不复制窗口
        self.assertEqual(
            {s.booking_id for s in itinerary.segments},
            {booking.reservation_id},
        )

    def test_transition_time_between_venues_is_reserved(self):
        itinerary = plan_group_itinerary(self.records, version=2)
        ordered = sorted(itinerary.segments, key=lambda s: s.window.start)
        museum_end = next(s.window.end for s in ordered
                          if s.activity == Activity.SHOWCASE)
        street_start = min(s.window.start for s in ordered
                           if s.activity == Activity.STREET)
        # 两馆转场取轮椅路线所需的较长时间（10 与 12 取 12）
        self.assertGreaterEqual(street_start - museum_end, 12)

    def test_everyone_can_join_common_briefing_before_bus(self):
        itinerary = plan_group_itinerary(self.records, version=2)
        briefing = next(s for s in itinerary.segments
                        if s.activity == Activity.EXCHANGE)
        self.assertEqual(briefing.venue_name, "东门")
        self.assertLessEqual(
            briefing.window.end, itinerary.booking.gate_assembly
        )
        self.assertGreaterEqual(
            itinerary.booking.bus_departure
            - itinerary.booking.gate_assembly,
            itinerary.booking.gate_walk_minutes,
        )

    def test_walk_to_gate_keeps_minimum_15_minutes(self):
        self.assertEqual(self.model.booking.gate_walk_minutes, 15)

    def test_delayed_device_moves_interpreted_segment_after_delivery(self):
        itinerary = plan_group_itinerary(self.records, version=2)
        talk = next(s for s in itinerary.segments
                    if s.mode == ServiceMode.RELAY_INTERPRET)
        self.assertGreaterEqual(talk.window.start, to_minutes("15:05"))
        # 延误期间先安排场馆讲解员带领的街区步行段
        walk = next(s for s in itinerary.segments
                    if s.activity == Activity.STREET
                    and s.mode == ServiceMode.GUIDE)
        self.assertLess(walk.window.end, talk.window.start)

    def test_original_plan_before_change_is_also_feasible(self):
        # 手工构造不含变更记录的资料集，应得到可行的初版
        records = [r for r in self.records if r["kind"] != "变更"]
        itinerary = plan_group_itinerary(records, version=1)
        self.assertEqual([], itinerary.violations)

    def test_violated_transition_is_detected(self):
        itinerary = plan_group_itinerary(self.records, version=2)
        # 人为破坏转场：把步行街段提前
        for segment in itinerary.segments:
            if segment.activity == Activity.STREET:
                segment.window = Window(
                    segment.window.start - 20, segment.window.end - 20
                )
        violations = check_itinerary(itinerary, self.model)
        self.assertTrue(
            any("转场" in v for v in violations), violations
        )


class ResourceConflictTest(unittest.TestCase):
    def setUp(self):
        self.records = _records()
        self.model = build_model(self.records)

    def test_one_interpreter_cannot_serve_two_groups_at_once(self):
        guide = self.model.resources["英语讲解员"]
        # 14:40 其负责另一组开场，本团组不能占用
        self.assertFalse(
            guide.can_serve(Window(to_minutes("14:40"),
                                   to_minutes("14:55")), "H39")
        )
        # 已承诺给本团组 H38 的窗口不算冲突
        self.assertTrue(
            guide.can_serve(Window(to_minutes("14:00"),
                                   to_minutes("14:35")), "H38")
        )

    def test_delayed_device_rejects_early_segment(self):
        device = self.model.resources["流动口译设备"]
        self.assertFalse(
            device.can_serve(Window(to_minutes("14:50"),
                                    to_minutes("15:00")), "H38")
        )
        self.assertTrue(
            device.can_serve(Window(to_minutes("15:05"),
                                    to_minutes("15:20")), "H38")
        )

    def test_split_keeps_single_booking_id(self):
        profiles, _ = build_group_profiles(self.records)
        split = plan_split_itinerary(self.records, profiles)
        self.assertTrue(split.split)
        self.assertEqual(
            {s.booking_id for s in split.segments}, {"H38"}
        )


class PlacementTest(unittest.TestCase):
    def setUp(self):
        self.records = _records()
        self.model = build_model(self.records)
        self.profiles, _ = build_group_profiles(self.records)
        self.itinerary = plan_group_itinerary(self.records, version=2)

    def _placement(self, member_id):
        return next(p for p in member_placements(
            self.profiles, self.itinerary, self.model
        ) if p.member_id == member_id)

    def test_p1_showcase_covered_by_english_guide_plus_subtitles(self):
        placement = self._placement("P1")
        self.assertIn("展柜讲解:普通话", placement.covered)
        self.assertIn("展柜讲解:英文字幕", placement.covered)

    def test_p1_street_remains_pending_without_subtitle_guarantee(self):
        placement = self._placement("P1")
        self.assertEqual(placement.status, PlacementStatus.PENDING_CONFIRM)
        self.assertTrue(
            any("步行街" in q and "普通话" in q
                for q in placement.open_questions)
        )

    def test_p2_only_pending_item_is_companion_choice(self):
        placement = self._placement("P2")
        self.assertEqual(placement.status, PlacementStatus.PENDING_CONFIRM)
        self.assertTrue(
            all("陪行" in q for q in placement.open_questions),
            placement.open_questions,
        )
        self.assertIn("步行街:英语", placement.covered)

    def test_p3_language_unknown_is_not_inferred(self):
        placement = self._placement("P3")
        # 中文问路不推断为能听懂专业讲解
        self.assertEqual(placement.status, PlacementStatus.PENDING_CONFIRM)
        self.assertTrue(
            all("听讲语言待本人当面确认" in q
                for q in placement.open_questions
                if "听讲语言" in q)
        )

    def test_refusing_split_member_is_never_marked_placed(self):
        split = plan_split_itinerary(self.records, self.profiles)
        placements = member_placements(self.profiles, split, self.model)
        p3 = next(p for p in placements if p.member_id == "P3")
        self.assertEqual(p3.status, PlacementStatus.BLOCKED)
        self.assertTrue(
            any("拆组" in r for r in p3.blocking_reasons)
        )
        # 多数人接受分流也不能改变拒绝者的未安置状态
        self.assertFalse(all_members_confirmed(placements))

    def test_placeholders_stay_pending_until_verified_in_person(self):
        for member_id in ("U1", "U2"):
            placement = self._placement(member_id)
            self.assertEqual(
                placement.status, PlacementStatus.PENDING_CONFIRM
            )

    def test_dispatch_blocked_while_questions_remain(self):
        placements = member_placements(
            self.profiles, self.itinerary, self.model
        )
        self.assertFalse(all_members_confirmed(placements))


class VenueServiceTest(unittest.TestCase):
    def test_each_activity_uses_its_own_service_mode(self):
        itinerary = plan_group_itinerary(_records(), version=2)
        modes = {s.activity: s.mode for s in itinerary.segments}
        self.assertEqual(modes[Activity.SHOWCASE], ServiceMode.GUIDE)
        self.assertIn(
            ServiceMode.RELAY_INTERPRET,
            [s.mode for s in itinerary.segments
             if s.activity == Activity.STREET],
        )
        self.assertEqual(
            modes[Activity.EXCHANGE], ServiceMode.GROUP_BRIEFING
        )

    def test_subtitle_only_offered_where_venue_supports_it(self):
        model = build_model(_records())
        museum = next(v for v in model.venues.values()
                      if v.activity == Activity.SHOWCASE)
        street = next(v for v in model.venues.values()
                      if v.activity == Activity.STREET)
        self.assertIn("英文", museum.subtitle_languages)
        self.assertEqual((), street.subtitle_languages)


if __name__ == "__main__":
    unittest.main()
