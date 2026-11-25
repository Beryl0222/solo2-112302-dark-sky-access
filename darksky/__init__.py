"""暗夜观测值班调度包。

模块对外暴露常用服务与解析函数，值班脚本通常从 :class:`darksky.service.DutyService`
开始使用。
"""

from .models import (
    Acceptance,
    Booking,
    BookingStatus,
    Closure,
    ConfirmationVersion,
    Direction,
    DutyError,
    EquipmentCondition,
    Escort,
    Issue,
    Itinerary,
    LightingNeed,
    LightPolicyWindow,
    LightUse,
    ObservationSlot,
    Person,
    Question,
    Rule,
    Segment,
    VersionStatus,
    format_clock,
    parse_clock,
)
from .parsing import ParseResult, parse_statement
from .service import DutyService, PlanReport
from .corpus import build_corpus_service, ingest_record

__all__ = [
    "Acceptance",
    "Booking",
    "BookingStatus",
    "Closure",
    "ConfirmationVersion",
    "Direction",
    "DutyError",
    "DutyService",
    "EquipmentCondition",
    "Escort",
    "Issue",
    "Itinerary",
    "LightingNeed",
    "LightPolicyWindow",
    "LightUse",
    "ObservationSlot",
    "ParseResult",
    "Person",
    "PlanReport",
    "Question",
    "Rule",
    "Segment",
    "VersionStatus",
    "build_corpus_service",
    "format_clock",
    "ingest_record",
    "parse_clock",
    "parse_statement",
]

__version__ = "0.1.0"
