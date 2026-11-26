"""国际访学团多语请求与导览接力排程。

子模块：

* ``profiles``  —— 按来源提取成员本人的语言、听读、移动与隐私需求
* ``planning``  —— 场馆服务、口译资源与可走完的行程排程
* ``workflow``  —— 持续收件、确认台账、交接需求与三种角色视图
"""

from materials import load_corpus
from .profiles import (
    Activity,
    Preference,
    Source,
    build_group_profiles,
    extract_profile,
    record_source,
    teacher_claim_binds_adult,
    apply_confirmation,
)
from .planning import (
    Segment,
    ServiceMode,
    Itinerary,
    build_model,
    plan_group_itinerary,
    plan_split_itinerary,
    check_itinerary,
)
from .workflow import ReceptionProcessor, Handoff

__all__ = [
    "load_corpus",
    "Activity",
    "Preference",
    "Source",
    "build_group_profiles",
    "extract_profile",
    "record_source",
    "teacher_claim_binds_adult",
    "apply_confirmation",
    "Segment",
    "ServiceMode",
    "Itinerary",
    "build_model",
    "plan_group_itinerary",
    "plan_split_itinerary",
    "check_itinerary",
    "ReceptionProcessor",
    "Handoff",
]
