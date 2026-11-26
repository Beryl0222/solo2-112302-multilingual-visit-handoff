"""持续收件、确认台账、变更交接与角色视图。

角色可见范围：

* **协调员**：每位成员的服务安排、待确认问题、跟进方（场馆或带队人）；
* **场馆**：只收到本场馆服务所需的语言与移动信息，收不到个人原话、
  隐私与拒绝事项；
* **学校**：可查行程版本与改派确认，看不到个人提出需求的原话。

发送规则：预约/确认发送前可以给出尚待本人确认的个人要求；只有确认后
的安排才能对学校和场馆发送。
"""

from __future__ import annotations

from dataclasses import dataclass

from materials import load_corpus
from .profiles import (
    Activity,
    GroupClaim,
    PendingQuestion,
    PersonProfile,
    Preference,
    PreferenceStatus,
    Source,
    apply_confirmation,
    build_group_profiles,
    teacher_claim_binds_adult,
)
from .planning import (
    Itinerary,
    Placement,
    PlanningModel,
    all_members_confirmed,
    build_model,
    check_itinerary,
    member_placements,
    plan_group_itinerary,
    plan_split_itinerary,
)


@dataclass
class Handoff:
    """变化时交接的一条服务需求。"""

    topic: str
    detail: str
    member_ids: list[str]
    follow_up_owner: str          # 场馆或带队人
    source_record_id: str
    status: PreferenceStatus
    itinerary_version: int


@dataclass
class ItineraryVersion:
    version: int
    basis: str
    segments: list[dict]
    violations: list[str]
    dispatch_ready: bool          # 是否可对学校/场馆发送


class ReceptionProcessor:
    """按收件顺序持续处理团组资料与成员请求。"""

    def __init__(self) -> None:
        self.profiles: list[PersonProfile] = []
        self.claims: list[GroupClaim] = []
        self.model: PlanningModel | None = None
        self.versions: list[Itinerary] = []
        self.handoffs: list[Handoff] = []
        self.confirmation_ledger: list[Preference] = []
        self._records: list[dict] = []
        self._next_version = 1

    # ------------------------------------------------------------------
    # 收件
    # ------------------------------------------------------------------
    def ingest(self, record: dict) -> None:
        """处理一条新到记录，并在变化时生成新版本或交接需求。"""
        self._records.append(record)
        kind = record["kind"]

        if kind == "成员":
            self.profiles, self.claims = build_group_profiles(self._records)
            self._handoff_pending(record["id"])
        elif kind in ("团组", "代填"):
            previous_ids = {p.member_id for p in self.profiles}
            self.profiles, self.claims = build_group_profiles(self._records)
            # 团组表新带出的占位成员，其待核实问题也要立即交接
            for profile in self.profiles:
                if profile.member_id not in previous_ids:
                    for question in profile.pending:
                        self.handoffs.append(
                            self._question_handoff(question)
                        )
            if kind == "代填":
                self._record_teacher_claim(record)
        elif kind in ("场馆", "集合", "资源"):
            self.model = build_model(self._records)
            if self._planning_inputs_ready() and not self.versions:
                self._publish_version("原始预约资料齐备")
        elif kind == "变更":
            self.model = build_model(self._records)
            self._publish_version("变更后重排（口译资源延误）")
            self._handoff_change(record)
        elif kind == "确认":
            decisions = apply_confirmation(self.profiles, record)
            for items in decisions.values():
                self.confirmation_ledger.extend(items)
            self._handoff_confirmation(record)

    def ingest_corpus(self, corpus: dict | None = None) -> None:
        corpus = corpus or load_corpus()
        for record in sorted(corpus["records"], key=lambda r: r["arrival_order"]):
            self.ingest(record)

    # ------------------------------------------------------------------
    def _planning_inputs_ready(self) -> bool:
        if self.model is None or not self.model.venues:
            return False
        activities = {v.activity for v in self.model.venues.values()}
        return Activity.SHOWCASE in activities and Activity.STREET in activities \
            and self.model.booking.entry_window.end > 0

    def _publish_version(self, basis: str) -> Itinerary:
        itinerary = plan_group_itinerary(self._records, self._next_version)
        itinerary.basis = basis
        itinerary.violations = check_itinerary(itinerary, self.model)
        self.versions.append(itinerary)
        self._next_version += 1
        return itinerary

    def _record_teacher_claim(self, record: dict) -> None:
        """教师统一填报：对成年成员不绑定，只留痕并生成核实交接。"""
        claim = next((c for c in self.claims if c.source_id == record["id"]), None)
        if claim is None:
            return
        for profile in self.profiles:
            if not teacher_claim_binds_adult(claim, profile):
                self.handoffs.append(Handoff(
                    topic="讲解语言与字幕",
                    detail="教师统一填报无法替代成年成员本人意见，需逐位核实",
                    member_ids=[profile.member_id],
                    follow_up_owner="带队教师",
                    source_record_id=record["id"],
                    status=PreferenceStatus.PENDING,
                    itinerary_version=self._next_version - 1,
                ))

    def _handoff_pending(self, source_id: str) -> None:
        for profile in self.profiles:
            if source_id not in profile.source_ids:
                continue
            for question in profile.pending:
                self.handoffs.append(self._question_handoff(question))

    def _question_handoff(self, question: PendingQuestion) -> Handoff:
        return Handoff(
            topic=question.topic,
            detail=question.question,
            member_ids=[question.member_id],
            follow_up_owner=question.follow_up_owner,
            source_record_id=question.source_id,
            status=PreferenceStatus.PENDING,
            itinerary_version=max(1, self._next_version - 1),
        )

    def _handoff_change(self, record: dict) -> None:
        affected = [p.member_id for p in self.profiles]
        # 交接说明只保留结构化事实，不转发记录原文
        device = self.model.resources.get("流动口译设备")
        delivery = ""
        if device and device.available_from is not None:
            delivery = (f"流动口译设备最早 "
                        f"{device.available_from // 60:02d}:"
                        f"{device.available_from % 60:02d} 送达；")
        self.handoffs.append(Handoff(
            topic="口译延误后的服务改派",
            detail=delivery + "整体调整行程，拆组须本人逐位同意",
            member_ids=affected,
            follow_up_owner="开埠街接待员",
            source_record_id=record["id"],
            status=PreferenceStatus.PENDING,
            itinerary_version=self._next_version - 1,
        ))

    def _handoff_confirmation(self, record: dict) -> None:
        for profile in self.profiles:
            for preference in profile.preferences:
                if preference.note and record["id"] in preference.note:
                    self.handoffs.append(Handoff(
                        topic=preference.channel,
                        detail=preference.value,
                        member_ids=[profile.member_id],
                        follow_up_owner="协调员",
                        source_record_id=record["id"],
                        status=preference.status,
                        itinerary_version=self._next_version - 1,
                    ))

    # ------------------------------------------------------------------
    # 排程结果与拆组
    # ------------------------------------------------------------------
    def latest_itinerary(self) -> Itinerary:
        if not self.versions:
            raise RuntimeError("资料尚未齐备，无法排程")
        return self.versions[-1]

    def evaluate_split(self) -> Itinerary:
        """评估临时拆组：拒绝拆组者保持未安置，且不复制预约窗口。"""
        return plan_split_itinerary(self._records, self.profiles)

    def placements(self) -> list[Placement]:
        return member_placements(
            self.profiles, self.latest_itinerary(), self.model
        )

    def dispatch_ready(self) -> bool:
        """确认后的安排才能发送：行程可行且每位成员均已确认。"""
        itinerary = self.latest_itinerary()
        return itinerary.is_feasible() and all_members_confirmed(self.placements())

    def resolve_pending_question(
        self, member_id: str, topic: str, decision: str,
        question_text: str | None = None,
    ) -> None:
        """接待员当面核实后，把待确认问题转为本人已确认的需求。

        系统不自行推断：只有录入了本人当面决定后，待核实项才关闭。
        """
        profile = next(p for p in self.profiles
                       if p.member_id == member_id)
        remaining = [q for q in profile.pending if q.topic != topic]
        resolved = [q for q in profile.pending if q.topic == topic]
        profile.pending = remaining
        for question in resolved:
            preference = Preference(
                member_id, topic, decision, frozenset(),
                Source.SELF, PreferenceStatus.CONFIRMED,
                note=f"接待员当面核实（源记录 {question.source_id}）",
            )
            profile.preferences.append(preference)
            self.confirmation_ledger.append(preference)
            self.handoffs.append(Handoff(
                topic=topic, detail=decision, member_ids=[member_id],
                follow_up_owner=question.follow_up_owner,
                source_record_id=question.source_id,
                status=PreferenceStatus.CONFIRMED,
                itinerary_version=self._next_version - 1,
            ))
        # 排程核对产生的开放问题按原文销项
        if question_text is not None:
            profile.verified_in_person.append(question_text)

    # ------------------------------------------------------------------
    # 三种角色视图
    # ------------------------------------------------------------------
    def coordinator_view(self) -> dict:
        """协调员视图：成员安排、待确认问题与跟进方全量可见。"""
        placements = {p.member_id: p for p in self.placements()}
        members = []
        for profile in self.profiles:
            placement = placements[profile.member_id]
            members.append({
                "member_id": profile.member_id,
                "display_name": profile.display_name,
                "is_adult": profile.is_adult,
                "preferences": [
                    {
                        "channel": p.channel,
                        "value": p.value,
                        "scope": sorted(a.value for a in p.scope),
                        "source": p.source.value,
                        "status": p.status.value,
                        "note": p.note,
                    }
                    for p in profile.preferences
                ],
                "refusals": [
                    {"channel": p.channel, "value": p.value,
                     "scope": sorted(a.value for a in p.scope)}
                    for p in profile.refusals
                ],
                "placement": placement.status.value,
                "covered_services": placement.covered,
                "pending_questions": [
                    {"topic": q.topic, "question": q.question,
                     "follow_up_owner": q.follow_up_owner}
                    for q in profile.pending
                ]
                + [{"topic": "排程核对", "question": q,
                    "follow_up_owner": "接待员"}
                   for q in placement.open_questions
                   if q not in {x.question for x in profile.pending}],
            })
        return {
            "role": "协调员",
            "itinerary_versions": self._version_dicts(),
            "handoffs": [vars(h) | {"status": h.status.value}
                         for h in self.handoffs],
            "members": members,
            "dispatch_ready": self.dispatch_ready(),
        }

    def venue_view(self, venue_name: str) -> dict:
        """场馆视图：仅本场馆服务所需的语言与移动信息，无个人原话。"""
        venue = next((v for v in self.model.venues.values()
                      if v.name == venue_name), None)
        if venue is None:
            raise KeyError(f"未知场馆：{venue_name}")

        language_needs: list[dict] = []
        mobility_needs: list[dict] = []
        for profile in self.profiles:
            relevant = [p for p in profile.preferences
                        if p.applies_to(venue.activity)]
            for preference in relevant:
                entry = {
                    "member_id": profile.member_id,
                    "need": f"{preference.channel}:{preference.value}",
                    "status": preference.status.value,
                }
                if preference.channel in ("听讲语言", "字幕语言", "阅读语言"):
                    language_needs.append(entry)
                elif preference.channel == "移动方式":
                    mobility_needs.append(entry)

        segments = [
            {
                "time": s.label,
                "mode": s.mode.value,
                "language": s.language,
                "subtitle": s.subtitle_language,
            }
            for s in self.latest_itinerary().segments
            if s.venue_name == venue.name
        ]
        return {
            "role": "场馆",
            "venue": venue.name,
            "service_window": {
                "start": f"{venue.service_window.start // 60:02d}:"
                         f"{venue.service_window.start % 60:02d}",
                "end": f"{venue.service_window.end // 60:02d}:"
                       f"{venue.service_window.end % 60:02d}",
            },
            "transition_minutes": venue.transition_minutes,
            "segments": segments,
            "language_needs": language_needs,
            "mobility_needs": mobility_needs,
            # 只告知场馆需要其准备的服务事实，不附个人原话/隐私/拒绝
            "dispatch_ready": self.dispatch_ready(),
        }

    def school_view(self) -> dict:
        """学校视图：行程版本与改派确认；不含个人需求原话。"""
        return {
            "role": "学校",
            "reservation_id": self.model.booking.reservation_id,
            "itinerary_versions": self._version_dicts(),
            "reassignment_confirmations": [
                {
                    "member_id": p.member_id,
                    "channel": p.channel,
                    "decision": p.status.value,
                    "source": p.source.value,
                }
                for p in self.confirmation_ledger
            ],
            "dispatch_ready": self.dispatch_ready(),
        }

    # ------------------------------------------------------------------
    def _version_dicts(self) -> list[dict]:
        return [
            ItineraryVersion(
                version=v.version,
                basis=v.basis,
                segments=[
                    {
                        "time": s.label,
                        "activity": s.activity.value,
                        "mode": s.mode.value,
                        "language": s.language,
                        "subtitle": s.subtitle_language,
                        "booking_id": s.booking_id,
                    }
                    for s in v.segments
                ],
                violations=v.violations,
                dispatch_ready=v.is_feasible() and self.dispatch_ready(),
            ).__dict__
            for v in self.versions
        ]
