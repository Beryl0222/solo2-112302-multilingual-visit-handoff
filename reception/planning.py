"""场馆服务、口译资源与行程排程。

排程约束：

* 展柜讲解、步行街、集体交流分别使用场馆提供的不同语言服务、字幕与
  转场时间；
* 一名口译员/讲解员不能同时服务两个分组（资源时间窗冲突校验）；
* 团组预约的入馆窗口不可拆分、不可延长，临时拆组不会复制预约窗口；
* 排程必须预留场馆间移动时间，且每位成员都能参加共同的集合说明；
* 口译员延误时只允许"整体调整"或"本人同意后拆组"；拒绝拆组的成员
  不会因多数人接受分流而被标为已安置。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable

from .profiles import Activity, PersonProfile, PreferenceStatus


class ServiceMode(str, Enum):
    """场馆可提供的语言服务形态。"""

    GUIDE = "讲解员"
    RELAY_INTERPRET = "流动口译转述"
    SELF_READ_SUBTITLE = "自行阅读字幕"
    PRERECORDED = "预录音"
    GROUP_BRIEFING = "集合说明"


# ---------------------------------------------------------------------------
# 时间工具：语料中的 HH:MM 统一换算为当日分钟数
# ---------------------------------------------------------------------------

def to_minutes(hhmm: str) -> int:
    hour, minute = hhmm.split(":")
    return int(hour) * 60 + int(minute)


_WINDOW_RE = re.compile(r"(\d{1,2}:\d{2})\s*[-–—至]\s*(\d{1,2}:\d{2})")
_CLOCK_RE = re.compile(r"\d{1,2}:\d{2}")


def _find_window(text: str) -> tuple[int, int] | None:
    match = _WINDOW_RE.search(text)
    if not match:
        return None
    return to_minutes(match.group(1)), to_minutes(match.group(2))


def _find_clocks(text: str) -> list[int]:
    return [to_minutes(token) for token in _CLOCK_RE.findall(text)]


def _transition_minutes(text: str) -> int:
    match = re.search(r"(\d+)\s*分钟转场", text)
    return int(match.group(1)) if match else 0


# ---------------------------------------------------------------------------
# 领域对象
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Window:
    start: int
    end: int

    def overlaps(self, other: "Window") -> bool:
        return self.start < other.end and other.start < self.end


@dataclass
class Venue:
    """一个场馆活动点及其服务、字幕与转场参数。"""

    record_id: str
    name: str
    activity: Activity
    service_window: Window
    languages: tuple[str, ...]
    subtitle_languages: tuple[str, ...]
    modes: tuple[ServiceMode, ...]
    transition_minutes: int
    wheelchair_route: bool = False


@dataclass
class Resource:
    """口译员、讲解员或流动设备：同一时间不能服务两个分组。

    ``assignments`` 记录本资源已承诺给某预约号的服务窗（合法占用）；
    ``busy`` 是服务其他分组的时间窗，与本团组的排段相遇即冲突。
    """

    name: str
    busy: list[Window] = field(default_factory=list)
    assignments: dict[str, list[Window]] = field(default_factory=dict)
    available_from: int | None = None  # 临时延误后的最早送达时间

    def can_serve(self, window: Window, reservation_id: str = "") -> bool:
        if self.available_from is not None and window.start < self.available_from:
            return False
        for booking_id, windows in self.assignments.items():
            if booking_id == reservation_id:
                continue  # 已承诺给本团组的窗口不算冲突
            if any(window.overlaps(w) for w in windows):
                return False
        return not any(window.overlaps(b) for b in self.busy)


@dataclass
class GroupBooking:
    """团组共用预约：窗口不可拆分或延长。"""

    reservation_id: str
    entry_window: Window
    gate_assembly: int
    bus_departure: int
    gate_walk_minutes: int
    splittable: bool = False


@dataclass
class Segment:
    """行程中的一个活动段。"""

    activity: Activity
    venue_name: str
    window: Window
    mode: ServiceMode
    language: str
    subtitle_language: str | None = None
    group_id: str = "G1"          # 拆组时标识分组
    booking_id: str = "H38"       # 拆组也共用同一预约号
    resources: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        start = f"{self.window.start // 60:02d}:{self.window.start % 60:02d}"
        end = f"{self.window.end // 60:02d}:{self.window.end % 60:02d}"
        return f"{start}-{end} {self.venue_name}{self.activity.value}"


@dataclass
class Itinerary:
    version: int
    segments: list[Segment]
    booking: GroupBooking
    basis: str = ""
    violations: list[str] = field(default_factory=list)
    split: bool = False

    def is_feasible(self) -> bool:
        return not self.violations


# ---------------------------------------------------------------------------
# 语料建模
# ---------------------------------------------------------------------------

def _venue_activity(text: str) -> Activity:
    if "街" in text:
        return Activity.STREET
    if "交流" in text:
        return Activity.EXCHANGE
    return Activity.SHOWCASE


def _venue_name(record_id: str, text: str) -> str:
    match = re.match(r"([一-鿿]+馆|[一-鿿]+街)", text)
    return match.group(1) if match else record_id


@dataclass
class PlanningModel:
    venues: dict[str, Venue]
    resources: dict[str, Resource]
    booking: GroupBooking
    change_notes: list[str]
    records: list[dict]


def build_model(records: list[dict]) -> PlanningModel:
    """从语料记录构建场馆、资源与预约模型，并应用变更记录。"""
    venues: dict[str, Venue] = {}
    resources: dict[str, Resource] = {}
    change_notes: list[str] = []
    booking: GroupBooking | None = None

    for record in records:
        text = record["text"]
        kind = record["kind"]

        if kind == "场馆":
            window = _find_window(text)
            assert window is not None
            activity = _venue_activity(text)
            languages = []
            modes = []
            subtitles: list[str] = []
            if "英语讲解员" in text:
                languages.append("英语")
                modes.append(ServiceMode.GUIDE)
            if "潮汕话讲解员" in text:
                languages.append("潮汕话")
                modes.append(ServiceMode.GUIDE)
            if "流动口译" in text:
                modes.append(ServiceMode.RELAY_INTERPRET)
                languages.append("英语")
            if "字幕可选英文" in text or "英文字幕" in text:
                subtitles.append("英文")
            venues[record["id"]] = Venue(
                record_id=record["id"],
                name=_venue_name(record["id"], text),
                activity=activity,
                service_window=Window(*window),
                languages=tuple(languages),
                subtitle_languages=tuple(subtitles),
                modes=tuple(modes),
                transition_minutes=_transition_minutes(text),
                wheelchair_route="轮椅路线" in text,
            )

        elif kind == "集合":
            clocks = _find_clocks(text)
            reservation = re.search(r"预约号([A-Z0-9]+)", text)
            walk = re.search(r"步行至少(\d+)分钟", text)
            if booking is None:
                booking = GroupBooking(
                    reservation_id=reservation.group(1) if reservation else "H38",
                    entry_window=Window(0, 0),
                    gate_assembly=0, bus_departure=0,
                    gate_walk_minutes=int(walk.group(1)) if walk else 15,
                )
            elif walk:
                booking.gate_walk_minutes = int(walk.group(1))
                if reservation:
                    booking.reservation_id = reservation.group(1)

        elif kind == "团组":
            clocks = _find_clocks(text)
            window = _find_window(text)
            if booking is None:
                booking = GroupBooking("H38", Window(0, 0), 0, 0, 15)
            if window:
                booking.entry_window = Window(*window)
            # 15:40 集合、16:00 巴士
            if len(clocks) >= 3:
                booking.gate_assembly = clocks[-2]
                booking.bus_departure = clocks[-1]

        elif kind == "资源":
            # 英语讲解员 14:00-14:35 已承诺给 G1；14:40 起负责另一组开场。
            guide = resources.setdefault("英语讲解员", Resource("英语讲解员"))
            window = _find_window(text)
            if window:
                guide.assignments.setdefault("H38", []).append(Window(*window))
            clocks = _find_clocks(text)
            later = [c for c in clocks if not window or c >= window[1]]
            if later:
                # 14:40 另有开场交接，记为服务其他分组的占用
                guide.busy.append(Window(later[0], later[0] + 20))

        elif kind == "变更":
            change_notes.append(text)
            delay = re.search(r"最早(\d{1,2}:\d{2})送达", text)
            if delay:
                device = resources.setdefault(
                    "流动口译设备", Resource("流动口译设备")
                )
                device.available_from = to_minutes(delay.group(1))

    assert booking is not None
    return PlanningModel(venues, resources, booking, change_notes, records)


# ---------------------------------------------------------------------------
# 排程
# ---------------------------------------------------------------------------

def _segments_for_version(
    model: PlanningModel, device_delayed: bool
) -> list[Segment]:
    """按语料参数排出展柜 -> 步行街 -> 集合交流的时间线。"""
    museum = next(v for v in model.venues.values()
                  if v.activity == Activity.SHOWCASE)
    street = next(v for v in model.venues.values()
                  if v.activity == Activity.STREET)
    booking = model.booking

    # 展柜讲解：使用场馆登记窗口与英语讲解员
    showcase = Segment(
        Activity.SHOWCASE, museum.name,
        Window(museum.service_window.start, museum.service_window.end),
        ServiceMode.GUIDE, "英语", "英文",
        resources=("英语讲解员",),
    )

    # 步行街开始时间：满足两馆转场要求（取轮椅路线所需的较长时间），
    # 且不早于场馆服务窗口起点。
    travel = max(museum.transition_minutes, street.transition_minutes)
    street_start = max(
        museum.service_window.end + travel, street.service_window.start
    )

    # 东门集合说明与集体交流：街区结束后完成转场，且必须赶上集合时间，
    # 并为集合点到登车/离场预留步行时间（至少 15 分钟）。
    latest_street_end = booking.gate_assembly \
        - booking.gate_walk_minutes - street.transition_minutes
    if street.service_window.end > latest_street_end:
        # 场馆窗口虽开到 15:30，但集合与转场约束要求提前收尾
        street_end = latest_street_end
    else:
        street_end = street.service_window.end

    if device_delayed:
        device = model.resources.get("流动口译设备")
        delivery = device.available_from if device and device.available_from \
            else street_start
        # 设备送达后留 1 分钟交接，再开始英语转述，保证两段不重合
        interpret_start = max(street_start, delivery + 1)
        # 设备送达前：场馆讲解员带领街区步行（不含英语转述讲解）
        street_walk = Segment(
            Activity.STREET, street.name,
            Window(street_start, interpret_start - 1),
            ServiceMode.GUIDE, "潮汕话", None,
        )
        street_talk = Segment(
            Activity.STREET, street.name,
            Window(interpret_start, street_end),
            ServiceMode.RELAY_INTERPRET, "英语", None,
            resources=("流动口译设备",),
        )
        street_segments = [street_walk, street_talk]
    else:
        street_segments = [Segment(
            Activity.STREET, street.name,
            Window(street_start, street_end),
            ServiceMode.RELAY_INTERPRET, "英语", None,
            resources=("流动口译设备",),
        )]

    # 集合说明必须全体可参加：开始时间不早于街区转场完成时刻；
    # 交流段在东门进行，结束于集合时间，之后步行/登车由集合段统一组织。
    exchange_start = street_end + street.transition_minutes
    exchange = Segment(
        Activity.EXCHANGE, "东门",
        Window(exchange_start, booking.gate_assembly),
        ServiceMode.GROUP_BRIEFING, "多语（口译支持个人提问）", None,
    )
    return [showcase, *street_segments, exchange]


def plan_group_itinerary(records: list[dict], version: int = 1) -> Itinerary:
    """排出团组整体行程；存在变更记录时按延误后的资源时间调整。"""
    model = build_model(records)
    delayed = any("流动口译" in note and "最早" in note
                  for note in model.change_notes)
    segments = _segments_for_version(model, delayed)
    itinerary = Itinerary(
        version=version, segments=segments, booking=model.booking,
        basis="；".join(model.change_notes) if delayed else "原始预约",
    )
    itinerary.violations = check_itinerary(itinerary, model)
    return itinerary


def plan_split_itinerary(
    records: list[dict], profiles: list[PersonProfile]
) -> Itinerary:
    """模拟临时拆组方案，用于检验"本人同意"前提。

    拆组不复制预约窗口、不新增预约号；拒绝拆组的成员保持未安置。
    """
    model = build_model(records)
    base = plan_group_itinerary(records, version=2)
    split = Itinerary(
        version=2, segments=list(base.segments), booking=model.booking,
        basis="临时拆组方案（待每位本人同意）", split=True,
    )
    for profile in profiles:
        if profile.refuses("拆组"):
            split.violations.append(
                f"{profile.member_id} 明示不接受临时拆组，不能随多数人分流，"
                "保持未安置状态"
            )
    # 同一资源被两个分组并行使用即冲突
    seen: dict[str, Window] = {}
    for segment in split.segments:
        for resource_name in segment.resources:
            resource = model.resources.get(resource_name)
            if resource and not resource.can_serve(
                segment.window, segment.booking_id
            ):
                split.violations.append(
                    f"{resource_name} 在 {segment.label} 无法同时服务两个分组"
                )
            if resource_name in seen and seen[resource_name].overlaps(segment.window):
                split.violations.append(
                    f"{resource_name} 被两个分组同时占用"
                )
            seen[resource_name] = segment.window
    return split


# ---------------------------------------------------------------------------
# 约束校验
# ---------------------------------------------------------------------------

def check_itinerary(itinerary: Itinerary, model: PlanningModel) -> list[str]:
    """返回行程违反的全部约束；空列表表示可走完。"""
    violations: list[str] = []
    booking = itinerary.booking
    segments = sorted(itinerary.segments, key=lambda s: s.window.start)

    # 1. 入馆窗口不可拆分、不可延长
    first_start = segments[0].window.start
    last_end = max(s.window.end for s in segments
                   if s.activity is not Activity.EXCHANGE)
    if first_start < booking.entry_window.start or \
            last_end > booking.entry_window.end:
        violations.append(
            "活动超出团组入馆窗口 "
            f"{booking.entry_window.start // 60:02d}:"
            f"{booking.entry_window.start % 60:02d}-"
            f"{booking.entry_window.end // 60:02d}:"
            f"{booking.entry_window.end % 60:02d}，窗口不可延长"
        )

    # 2. 场馆间转场时间（同一场馆内相邻活动不重复计转场；轮椅路线取
    #    两馆要求的较长时间，由排段函数 max(museum, street) 保证）
    for earlier, later in zip(segments, segments[1:]):
        if earlier.venue_name == later.venue_name:
            continue
        destination = next((v for v in model.venues.values()
                            if v.name == later.venue_name), None)
        origin = next((v for v in model.venues.values()
                       if v.name == earlier.venue_name), None)
        required = max(
            destination.transition_minutes if destination else 0,
            origin.transition_minutes if origin else 0,
        )
        if later.window.start - earlier.window.end < required:
            violations.append(
                f"{earlier.venue_name} -> {later.venue_name} 仅 "
                f"{later.window.start - earlier.window.end} 分钟，"
                f"少于 {required} 分钟转场要求"
            )

    # 3. 每位成员都能参加共同集合说明：交流段须在集合时间前完成转场，
    #    集合后到巴士发车至少保留规定步行时间
    assembly = next((s for s in segments
                     if s.activity == Activity.EXCHANGE), None)
    if assembly is None:
        violations.append("缺少共同集合说明段")
    else:
        if assembly.window.end > booking.gate_assembly:
            violations.append("集合说明晚于团组集合时间，部分成员无法参加")
        if booking.bus_departure - booking.gate_assembly \
                < booking.gate_walk_minutes:
            violations.append(
                f"集合后到巴士发车不足 {booking.gate_walk_minutes} 分钟步行时间"
            )
        if assembly.window.end > booking.bus_departure:
            violations.append("集合交流晚于巴士发车时间")

    # 4. 同一口译/讲解资源不能同时服务两个分组
    for segment in segments:
        for resource_name in segment.resources:
            resource = model.resources.get(resource_name)
            if resource and not resource.can_serve(
                segment.window, segment.booking_id
            ):
                violations.append(
                    f"{resource_name} 在 {segment.label} 存在时间冲突或尚未送达"
                )

    # 5. 场馆服务窗口与服务形态必须由该场馆实际提供
    for segment in segments:
        venue = next((v for v in model.venues.values()
                      if v.name == segment.venue_name), None)
        if venue is None:
            continue
        # 只有占用该场馆正式服务形态的讲解段才受服务窗口约束；
        # 等待设备期间的入场步行段属于转场，允许早于窗口起点。
        if segment.mode in venue.modes and (
            segment.window.start < venue.service_window.start
            or segment.window.end > venue.service_window.end
        ):
            violations.append(
                f"{segment.venue_name} 活动 {segment.label} 超出场馆服务窗口"
            )
        if segment.mode not in venue.modes and \
                segment.mode != ServiceMode.GROUP_BRIEFING:
            violations.append(
                f"{segment.venue_name} 不提供 {segment.mode.value} 服务"
            )
        if segment.subtitle_language and \
                segment.subtitle_language not in venue.subtitle_languages:
            violations.append(
                f"{segment.venue_name} 无 {segment.subtitle_language} 字幕"
            )

    # 6. 拆组不得复制预约窗口
    if itinerary.split:
        booking_ids = {s.booking_id for s in segments}
        if booking_ids != {booking.reservation_id}:
            violations.append("临时拆组不得复制或新增团组预约窗口")

    return violations


# ---------------------------------------------------------------------------
# 成员安置判定
# ---------------------------------------------------------------------------

class PlacementStatus(str, Enum):
    PLACED = "已安置"
    PENDING_CONFIRM = "待本人确认"
    BLOCKED = "未安置"


@dataclass
class Placement:
    member_id: str
    status: PlacementStatus
    covered: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    blocking_reasons: list[str] = field(default_factory=list)


def _narration_needs(profile: PersonProfile, activity: Activity) -> list[str]:
    """成员在某语言活动段的全部听讲语言需求；空列表表示资料不足。"""
    return [
        p.value for p in profile.preferences
        if p.channel == "听讲语言" and p.applies_to(activity)
    ]


def _need_is_covered(
    need: str, segment: Segment, profile: PersonProfile, activity: Activity
) -> bool:
    """成员的一条听讲需求是否被该活动段的服务覆盖。

    * 英语需求由英语讲解/英语口译转述覆盖；
    * 普通话听讲能力可由英语服务配合本人要求的英文字幕覆盖（如 P1：
      "能听普通话，专业词需要英语字幕"）；
    * 集体交流段保留口译支持的个人提问通道，视为覆盖。
    """
    if segment.mode == ServiceMode.GROUP_BRIEFING:
        return True
    if need in segment.language:
        return True
    if need == "普通话":
        subtitle_wants = {
            p.value for p in profile.preferences
            if p.channel == "字幕语言" and p.applies_to(activity)
        }
        # 本人可听普通话：若该活动同时提供其要求的字幕（如 P1 在展柜
        # 讲解要英文字幕），英语讲解+英文字幕可覆盖；该活动无字幕安排
        # 时不得把英语服务直接等同于普通话可跟，留待当面核实。
        if not subtitle_wants or segment.subtitle_language is None:
            return False
        return segment.subtitle_language in subtitle_wants
    return False


def member_placements(
    profiles: list[PersonProfile],
    itinerary: Itinerary,
    model: PlanningModel,
) -> list[Placement]:
    """逐位成员核对每个活动段的服务覆盖与待确认问题。"""
    placements: list[Placement] = []
    verbal = (Activity.SHOWCASE, Activity.STREET, Activity.EXCHANGE)

    for profile in profiles:
        placement = Placement(profile.member_id, PlacementStatus.PLACED)

        if profile.refuses("拆组") and itinerary.split:
            placement.status = PlacementStatus.BLOCKED
            placement.blocking_reasons.append("本人不接受临时拆组")

        for activity in verbal:
            segments = [s for s in itinerary.segments
                        if s.activity == activity]
            if not segments:
                placement.status = PlacementStatus.BLOCKED
                placement.blocking_reasons.append(f"缺少 {activity.value} 安排")
                continue

            needs = _narration_needs(profile, activity)
            if not needs:
                # 不推断语言能力：登记缺失或仅日常问路能力
                if placement.status != PlacementStatus.BLOCKED:
                    placement.status = PlacementStatus.PENDING_CONFIRM
                placement.open_questions.append(
                    f"{activity.value}的听讲语言待本人当面确认"
                )
            else:
                for need in needs:
                    # 同一活动可能拆成多个分段（如设备送达前的步行段），
                    # 只要任一分段覆盖该需求即视为活动内有服务保障。
                    if any(_need_is_covered(need, segment, profile, activity)
                           for segment in segments):
                        placement.covered.append(f"{activity.value}:{need}")
                    else:
                        if placement.status != PlacementStatus.BLOCKED:
                            placement.status = PlacementStatus.PENDING_CONFIRM
                        placement.open_questions.append(
                            f"{activity.value}需要{need}服务，"
                            f"现安排为{'、'.join(s.language for s in segments)}"
                        )

            # 字幕需求逐段核对：只要该活动有分段提供所要求的字幕即可
            for preference in profile.preferences:
                if preference.channel == "字幕语言" \
                        and preference.applies_to(activity) \
                        and preference.status != PreferenceStatus.DECLINED:
                    if any(s.subtitle_language == preference.value
                           for s in segments):
                        placement.covered.append(
                            f"{activity.value}:{preference.value}字幕"
                        )
                    else:
                        placement.open_questions.append(
                            f"{activity.value}要求{preference.value}字幕，"
                            "现安排："
                            + ("、".join(s.subtitle_language or "无字幕"
                                         for s in segments))
                        )

            # 集体交流：汇总发言需逐人同意，未同意则保留个人提问通道
            if activity == Activity.EXCHANGE \
                    and profile.refuses("汇总发言", Activity.EXCHANGE):
                placement.covered.append("集体交流:保留个人提问通道")
        # 待核实问题随行，不静默丢弃；已当面核实销项的不再重复出现
        for question in profile.pending:
            if question.question not in profile.verified_in_person:
                placement.open_questions.append(question.question)
        placement.open_questions = [
            q for q in placement.open_questions
            if q not in profile.verified_in_person
        ]

        # 收尾状态：阻断优先；无阻断且开放问题全部销项即为已安置
        if placement.status != PlacementStatus.BLOCKED:
            placement.status = (
                PlacementStatus.PENDING_CONFIRM
                if placement.open_questions else PlacementStatus.PLACED
            )
        placements.append(placement)

    return placements


def all_members_confirmed(placements: Iterable[Placement]) -> bool:
    """是否每位成员都已安置且没有待确认问题（发送前置条件）。"""
    placements = list(placements)
    return bool(placements) and all(
        p.status == PlacementStatus.PLACED
        and not p.open_questions
        and not p.blocking_reasons
        for p in placements
    )
