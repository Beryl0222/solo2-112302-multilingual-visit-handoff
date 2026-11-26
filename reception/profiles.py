"""成员资料与个人需求提取。

业务规则（对应接待要求）：

1. 团组表、本人补充、带队教师说明都是可能来源，但只有本人来源的需求才
   绑定到本人；教师不能替成年成员选择无障碍方式（见
   :func:`teacher_claim_binds_adult`）。
2. 日常问路语言不等于专业讲解语言；能跟随口译不等于同意把问题汇总成一
   个发言，二者分别建为独立需求。
3. "我可以听普通话，字幕请用英文""只在一楼使用轮椅"等带范围表达，范围
   只覆盖本人明示的活动/楼层，不外推为全程限制；无法确定的细节进入
   ``pending``，留给接待员当面核实。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from materials import load_corpus


class Activity(str, Enum):
    """场馆提供的三类活动。"""

    SHOWCASE = "展柜讲解"
    STREET = "步行街"
    EXCHANGE = "集体交流"


class PreferenceStatus(str, Enum):
    """需求的确认状态。"""

    SELF_STATED = "本人已提出"
    PENDING = "待本人确认"
    CONFIRMED = "已确认"
    DECLINED = "本人拒绝"


class Source(str, Enum):
    """资料来源类型。"""

    SELF = "本人"
    TEACHER = "带队教师"
    GROUP_FORM = "团组表"
    RECEPTION = "接待员待核实"


# 场馆活动关键词 -> 活动类型。关键词按具体程度排序，匹配到更具体的
# 场馆词时不再回落到泛指词（如"讲解"只在没有场馆词时兜底）。
_ACTIVITY_KEYWORDS = (
    (Activity.SHOWCASE, ("展柜", "展厅", "馆内", "楼内", "一楼")),
    (Activity.STREET, ("街区", "步行街", "陪行")),
    (Activity.EXCHANGE, ("交流", "发言", "提问", "问题")),
)


def _activity_for_clause(clause: str) -> list[Activity]:
    """把一个分句中出现的活动关键词映射到活动范围。

    只有明确的场馆/活动词才建立范围；"讲解""字幕""听"等泛指词不决定
    范围。没有活动关键词时返回空列表，表示范围无法由文字确定，需当面
    核实，而不是默认全程。
    """
    activities: list[Activity] = []
    for activity, keywords in _ACTIVITY_KEYWORDS:
        if any(word in clause for word in keywords):
            activities.append(activity)
    return activities


@dataclass(frozen=True)
class Preference:
    """绑定到本人的一条需求。

    ``scope`` 为该需求明确覆盖的活动；空集合表示文字未限定范围，按规则
    不得推断为全程限制，应进入待核实清单。
    """

    member_id: str
    channel: str  # 听讲语言 / 字幕语言 / 移动方式 / 提问方式 / 隐私 ...
    value: str
    scope: frozenset[Activity] = frozenset()
    source: Source = Source.SELF
    status: PreferenceStatus = PreferenceStatus.SELF_STATED
    note: str = ""

    def applies_to(self, activity: Activity) -> bool:
        """该需求是否适用于指定活动：明示范围匹配；未限定范围不做外推。"""
        if not self.scope:
            return False
        return activity in self.scope


@dataclass
class PendingQuestion:
    """无法从资料确定、需接待员当面与本人核实的问题。"""

    member_id: str
    topic: str
    question: str
    follow_up_owner: str  # 场馆或带队人
    source_id: str


@dataclass
class PersonProfile:
    """单个成员的完整画像：需求、拒绝事项与待核实问题都绑定本人。"""

    member_id: str
    display_name: str
    is_adult: bool | None = None
    preferences: list[Preference] = field(default_factory=list)
    refusals: list[Preference] = field(default_factory=list)
    pending: list[PendingQuestion] = field(default_factory=list)
    verified_in_person: list[str] = field(default_factory=list)
    source_ids: list[str] = field(default_factory=list)
    is_placeholder: bool = False  # 团组人数中未单独登记的成员

    def preferences_for(self, activity: Activity) -> list[Preference]:
        return [p for p in self.preferences if p.applies_to(activity)]

    def refuses(self, channel: str, activity: Activity | None = None) -> bool:
        for refusal in self.refusals:
            if refusal.channel != channel:
                continue
            if activity is None or refusal.applies_to(activity):
                return True
        return False


@dataclass
class GroupClaim:
    """团组表/教师统一填报的内容；未经本人确认不绑定到成年人。"""

    text: str
    source_id: str
    source: Source
    verified_per_adult: bool = False


# ---------------------------------------------------------------------------
# 来源判定
# ---------------------------------------------------------------------------

def record_source(record: dict) -> Source:
    """根据记录文字判断其来源类型。"""
    text = record["text"]
    if record["kind"] == "代填" or "带队教师" in text or "教师填" in text:
        return Source.TEACHER
    if "本人" in text:
        return Source.SELF
    return Source.GROUP_FORM


def teacher_claim_binds_adult(claim: GroupClaim, profile: PersonProfile) -> bool:
    """教师统一填报能否替代成年成员本人的选择。

    规则：教师不能替成年成员选择无障碍方式，也不能替其决定讲解语言与
    字幕；只有逐位成年人确认过的团组填报才对本人生效。
    """
    if profile.is_adult and not claim.verified_per_adult:
        return False
    return claim.verified_per_adult


# ---------------------------------------------------------------------------
# 成员本人需求提取
# ---------------------------------------------------------------------------

_NAME_RE = re.compile(r"^(?P<name>[一-鿿\w]+)[，,]")


def _display_name(text: str, record_id: str) -> str:
    match = _NAME_RE.match(text)
    return match.group("name") if match else record_id


def _add(profile: PersonProfile, preference: Preference) -> None:
    profile.preferences.append(preference)


def _parse_language_clauses(profile: PersonProfile, text: str, source: Source) -> None:
    """提取听讲、字幕、阅读、问路与提问方式相关表达。"""
    member = profile.member_id

    # "能听普通话" —— 听讲能力，且要与"专业讲解"区分
    if "能听普通话" in text or "可以听普通话" in text:
        # 文字未限定活动时，按本人明示的口头听讲能力覆盖三类语言活动，
        # 但专业讲解语言仍单列待核实，不做能力外推。
        scope = _activity_for_clause(text) or [
            Activity.SHOWCASE, Activity.STREET, Activity.EXCHANGE
        ]
        _add(profile, Preference(
            member, "听讲语言", "普通话",
            frozenset(scope), source,
            note="本人表示可听普通话；日常问路能力不等于专业讲解能力",
        ))

    # 专业讲解语言需求（如"专业词需要英语字幕"）：字幕绑定展柜讲解，
    # 讲解语言本身未写明，进入待核实而不是推断。
    if "专业词" in text and "字幕" in text:
        _add(profile, Preference(
            member, "字幕语言", "英文",
            frozenset({Activity.SHOWCASE}),
            source, note="专业术语需要英文字幕（本人明示于展柜讲解）",
        ))
        profile.pending.append(PendingQuestion(
            member, "讲解语言",
            "专业讲解本身使用何种语言（普通话可否全程跟讲）需当面确认",
            "侨批馆接待员", profile.source_ids[0],
        ))

    # 通用字幕要求："字幕请用英文"
    subtitle = re.search(r"字幕[请需]?用?(中文|英文|英语)", text)
    if subtitle and "专业词" not in text:
        language = "英文" if subtitle.group(1) in ("英文", "英语") else "中文"
        _add(profile, Preference(
            member, "字幕语言", language,
            frozenset(_activity_for_clause(text)), source,
        ))

    # 口语/阅读能力
    if "英语口语" in text:
        _add(profile, Preference(
            member, "听讲语言", "英语",
            frozenset({Activity.SHOWCASE, Activity.STREET, Activity.EXCHANGE}),
            source, note="本人登记英语口语可应对",
        ))
    if "阅读" in text:
        _add(profile, Preference(
            member, "阅读语言", "英语",
            frozenset({Activity.SHOWCASE, Activity.STREET}), source,
        ))

    # 会中文问路 —— 只建立日常沟通能力，不推断专业讲解能力
    if "中文问路" in text or "问路没问题" in text:
        _add(profile, Preference(
            member, "日常问路语言", "中文", frozenset(), source,
            note="仅日常问路；不得据此推断能听懂专业讲解",
        ))

    # 口译跟随与汇总发言是两件事：分别识别，互不构成同意关系
    if "口译" in text:
        _add(profile, Preference(
            member, "口译跟随", "可跟随口译", frozenset(), source,
            note="能跟随口译不等于同意问题汇总成一个发言",
        ))
    if ("汇总" in text or "一个发言" in text) and \
            ("不同意" in text or "不接受" in text):
        profile.refusals.append(Preference(
            member, "汇总发言", "不同意代汇总提问",
            frozenset({Activity.EXCHANGE}), source,
            PreferenceStatus.SELF_STATED,
        ))


def _parse_mobility_clauses(profile: PersonProfile, text: str, source: Source) -> None:
    """提取带范围的移动/无障碍表达。"""
    member = profile.member_id

    if "轮椅" in text:
        if "一楼" in text:
            # "只在一楼使用轮椅"：范围仅限一楼展柜讲解，不外推到街区
            profile.preferences.append(Preference(
                member, "移动方式", "一楼使用轮椅",
                frozenset({Activity.SHOWCASE}), source,
                note="本人明示仅一楼；街区安排不得按全程轮椅处理",
            ))
            profile.pending.append(PendingQuestion(
                member, "移动方式",
                "一楼以外楼层及街区的移动方式需当面核实",
                "侨批馆接待员", profile.source_ids[0],
            ))
        elif "楼内" in text:
            # 楼内轮椅；街区由本人决定
            profile.preferences.append(Preference(
                member, "移动方式", "楼内轮椅通行",
                frozenset({Activity.SHOWCASE}), source,
            ))
            profile.pending.append(PendingQuestion(
                member, "街区陪行",
                "步行街是否使用陪行/轮椅，本人尚未决定，需当面确认",
                "开埠街接待员", profile.source_ids[0],
            ))
        else:
            # 提到轮椅但没有范围：不能推断为全程
            profile.pending.append(PendingQuestion(
                member, "移动方式",
                "轮椅使用的楼层与街区范围文字未写明，需当面核实",
                "接待员", profile.source_ids[0],
            ))

    if "陪行" in text and "轮椅" not in text:
        profile.pending.append(PendingQuestion(
            member, "街区陪行", "步行街是否需要陪行需当面确认",
            "开埠街接待员", profile.source_ids[0],
        ))


def _parse_privacy_and_refusals(profile: PersonProfile, text: str, source: Source) -> None:
    member = profile.member_id

    if "个人问题" in text and ("留给她本人" in text or "本人" in text):
        profile.preferences.append(Preference(
            member, "隐私", "个人问题只与本人沟通", frozenset(),
            source, note="个人不愿公开的需求不进入对学校/场馆的原文转发",
        ))

    if "不接受临时拆组" in text or "不接受拆组" in text:
        profile.refusals.append(Preference(
            member, "拆组", "不接受临时拆组", frozenset(),
            source, PreferenceStatus.SELF_STATED,
        ))

    if "不接受改为单独听预录音" in text or "预录音" in text:
        profile.refusals.append(Preference(
            member, "替代服务", "不接受单独听预录音",
            frozenset({Activity.STREET, Activity.SHOWCASE}),
            source, PreferenceStatus.SELF_STATED,
        ))


def _is_adult(text: str) -> bool | None:
    """成年状态只认明确表述；"本人口头补充"等提交方式不改变年龄。"""
    if "未成年" in text:
        return False
    if "成年" in text:
        return True
    return None


def extract_profile(record: dict) -> PersonProfile:
    """从一条本人成员记录提取成员画像。"""
    text = record["text"]
    profile = PersonProfile(
        member_id=record["id"],
        display_name=_display_name(text, record["id"]),
        source_ids=[record["id"]],
    )
    profile.is_adult = _is_adult(text)
    source = record_source(record)

    _parse_language_clauses(profile, text, source)
    _parse_mobility_clauses(profile, text, source)
    _parse_privacy_and_refusals(profile, text, source)
    return profile


# ---------------------------------------------------------------------------
# 团组建档
# ---------------------------------------------------------------------------

_PERSON_RE = re.compile(r"(?P<n>[0-9一二三四五六七八九十]+)人")
_CN_DIGITS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
              "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _cn_number(token: str) -> int:
    if token.isdigit():
        return int(token)
    return _CN_DIGITS.get(token, 0)


def build_group_profiles(records: list[dict]) -> tuple[list[PersonProfile], list[GroupClaim]]:
    """持续处理已收件资料，返回全部成员画像与教师/团组统一填报。

    未单独登记的团组成员以占位画像返回，其语言与移动需求一律列为待
    核实，系统不做任何推断。
    """
    profiles: list[PersonProfile] = []
    claims: list[GroupClaim] = []

    for record in records:
        if record["kind"] == "成员":
            profiles.append(extract_profile(record))
        elif record["kind"] == "代填":
            claims.append(GroupClaim(
                text=record["text"], source_id=record["id"],
                source=record_source(record),
            ))
        elif record["kind"] == "团组":
            claims.append(GroupClaim(
                text=record["text"], source_id=record["id"],
                source=Source.GROUP_FORM,
            ))

    # 按团组表补齐未登记成员（占位，待核实）
    for record in records:
        if record["kind"] != "团组":
            continue
        match = _PERSON_RE.search(record["text"])
        if not match:
            continue
        total = _cn_number(match.group("n"))
        registered = {p.member_id for p in profiles}
        missing = total - len(registered)
        for index in range(1, missing + 1):
            placeholder = PersonProfile(
                member_id=f"U{index}",
                display_name=f"未登记成员{index}",
                is_adult=None,
                is_placeholder=True,
                source_ids=[record["id"]],
            )
            placeholder.pending.append(PendingQuestion(
                placeholder.member_id, "讲解语言",
                "本人未登记，听讲/字幕语言需当面核实，不得按团组表统一语言处理",
                "接待员", record["id"],
            ))
            placeholder.pending.append(PendingQuestion(
                placeholder.member_id, "移动方式",
                "本人未登记，无障碍与移动需求需当面核实",
                "接待员", record["id"],
            ))
            profiles.append(placeholder)

    return profiles, claims


# ---------------------------------------------------------------------------
# 确认记录处理
# ---------------------------------------------------------------------------

def apply_confirmation(
    profiles: list[PersonProfile], record: dict
) -> dict[str, list[Preference]]:
    """处理"确认"类记录，更新本人需求状态并返回按成员分组的决定。

    只有本人作出的同意/拒绝才改变本人需求的状态；系统不代替本人表态。
    """
    text = record["text"]
    by_id = {p.member_id: p for p in profiles}
    decisions: dict[str, list[Preference]] = {}

    def note(member_id: str, channel: str, value: str,
             status: PreferenceStatus, scope: frozenset[Activity] = frozenset()) -> None:
        preference = Preference(
            member_id, channel, value, scope, Source.SELF, status,
            note=f"确认记录 {record['id']}",
        )
        by_id[member_id].preferences.append(preference)
        decisions.setdefault(member_id, []).append(preference)

    if "P1同意" in text:
        note("P1", "设备晚到安排", "同意先看展柜再读字幕",
             PreferenceStatus.CONFIRMED,
             frozenset({Activity.SHOWCASE, Activity.STREET}))
    if "P3不接受改为单独听预录音" in text or ("P3" in text and "预录音" in text):
        refusal = Preference(
            "P3", "替代服务", "不接受单独听预录音",
            frozenset({Activity.STREET, Activity.SHOWCASE}),
            Source.SELF, PreferenceStatus.DECLINED,
            note=f"确认记录 {record['id']}",
        )
        by_id["P3"].refusals.append(refusal)
        decisions.setdefault("P3", []).append(refusal)
    if "P2尚未决定" in text:
        # 保持待确认，不推断、不默认
        for question in by_id["P2"].pending:
            if question.topic == "街区陪行":
                question.question = "本人尚未决定：步行街是否使用陪行，需当面确认"
    return decisions


def load_profiles() -> tuple[list[PersonProfile], list[GroupClaim]]:
    """便捷入口：直接从语料构建全部画像与统一填报。"""
    records = load_corpus()["records"]
    return build_group_profiles(records)
