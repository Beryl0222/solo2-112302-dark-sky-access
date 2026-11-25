"""调度系统使用的核心数据类型与时间工具。

这里不做自然语言解析，也不做业务裁决；解析器和服务层都依赖这些类型。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
import re
from typing import Any, Iterable


class DutyError(ValueError):
    """输入数据或业务状态不合法。"""


class Direction(str, Enum):
    """人员移动方向。

    ``OBSERVATION`` 不是移动方向，但用于把“看星时段”的光线承诺与路径例外分开。
    """

    OUTBOUND = "outbound"  # 入口 -> 观测台，去程/入场
    RETURN = "return"  # 观测台 -> 入口，回程/返程
    ROUNDTRIP = "roundtrip"
    AT_STATION = "at_station"
    UNKNOWN = "unknown"


class BookingStatus(str, Enum):
    PROSPECT = "prospect"
    HELD = "held"  # 仅暂留空位
    CONFIRMED = "confirmed"  # 本人条件、设备、陪行均齐
    REDEEMED = "redeemed"  # 已核销/已入场
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class VersionStatus(str, Enum):
    DRAFT = "draft"
    PROPOSED = "proposed"
    ISSUED = "issued"
    REDEEMED = "redeemed"
    SUPERSEDED = "superseded"
    CANCELLED = "cancelled"


def parse_clock(value: int | str | None) -> int | None:
    """把 ``"20:31"`` / ``"20：31"`` 转成当天分钟数。"""

    if value is None or value == "":
        return None
    if isinstance(value, int):
        return value
    match = re.search(r"(\d{1,2})\s*[:：]\s*(\d{2})", str(value))
    if not match:
        raise DutyError(f"无法解析时间：{value!r}")
    hour, minute = int(match.group(1)), int(match.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise DutyError(f"时间超出范围：{value!r}")
    return hour * 60 + minute


def format_clock(value: int | None) -> str:
    if value is None:
        return "时间未定"
    return f"{value // 60:02d}:{value % 60:02d}"


def clocks_between(text: str) -> tuple[int | None, int | None]:
    """从一句通告中抽取第一个起止时间区间。"""

    match = re.search(
        r"(\d{1,2}\s*[:：]\s*\d{2})\s*(?:至|到|–|—|-|~|to)\s*"
        r"(\d{1,2}\s*[:：]\s*\d{2})",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        return None, None
    return parse_clock(match.group(1)), parse_clock(match.group(2))


def overlaps(start_a: int | None, end_a: int | None,
             start_b: int | None, end_b: int | None) -> bool:
    """半开/闭区间均可的重叠判断；未知边界按开放边界处理。"""

    left = max(x for x in (start_a, start_b) if x is not None) if (
        start_a is not None or start_b is not None
    ) else None
    right_candidates = [x for x in (end_a, end_b) if x is not None]
    right = min(right_candidates) if right_candidates else None
    if left is None or right is None:
        # 两个都完全无界才算必然重叠；只有一侧无界时用宽松策略。
        return True
    return left < right


@dataclass
class Person:
    id: str
    name: str
    aliases: list[str] = field(default_factory=list)
    group_id: str | None = None
    role: str = "participant"  # participant / proxy / escort / staff
    cannot_self_accept: bool = False
    notes: str = ""

    def matches_name(self, text: str) -> bool:
        names = [self.name, *self.aliases]
        return any(name and name in text for name in names)


@dataclass
class LightingNeed:
    """一条按“人—方向—区段—时间—替代措施”拆开的照明表述。

    整句永远不应直接映射成“要灯/不要灯”。同一句可以产生多条不同方向的框架。
    """

    person_id: str | None = None
    person_name: str | None = None
    source_id: str | None = None
    raw_clause: str = ""
    direction: Direction = Direction.UNKNOWN
    segment_query: str | None = None
    segment_id: str | None = None
    activity_scope: str = "movement"  # movement / observation / general
    kind: str = "statement"
    modality: str = "statement"  # requires / accepts / rejects / offers / uncertain
    color: str | None = None
    intensity: str | None = None
    continuous: bool | None = None
    intermittent_allowed: bool | None = None
    flashing_allowed: bool | None = None
    light_allowed: bool | None = None
    requires_light: bool | None = None
    alternative: str | None = None
    # “随时可叫助手”只是替代资源，不代表同意取消常亮。
    consent_to_remove_continuous_light: bool = False
    except_directions: list[Direction] = field(default_factory=list)
    start: int | None = None
    end: int | None = None
    proxy: bool = False
    self_reported: bool = True
    confidence: str = "high"  # high / medium / low
    status: str = "active"  # active / superseded / clarify / retained
    clarifies_source: str | None = None
    private_to_entrance: bool = False
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return to_plain_dict(self)


@dataclass
class Acceptance:
    """本人或陪行员对某一具体条件/版本的接受记录。"""

    id: str
    acceptor_id: str
    acceptor_type: str = "person"  # person / escort
    kind: str = "condition"  # route / lighting / equipment / escort / plan / time
    code: str | None = None
    version: int | None = None
    direction: Direction | None = None
    segment_id: str | None = None
    start: int | None = None
    end: int | None = None
    equipment_id: str | None = None
    escort_id: str | None = None
    terms: dict[str, Any] = field(default_factory=dict)
    source_id: str | None = None
    note: str = ""
    self_confirmed: bool = True
    accepted: bool = True

    def matches_itinerary(self, itinerary: "Itinerary") -> bool:
        if self.kind not in {"route", "plan", "condition"}:
            return False
        if self.direction and self.direction != itinerary.direction:
            return False
        if self.segment_id and self.segment_id != itinerary.segment_id:
            return False
        if self.start is not None and self.start != itinerary.start:
            return False
        if self.end is not None and self.end != itinerary.end:
            return False
        return True


@dataclass
class LightUse:
    """一次实际灯光使用；连续区间由多个相邻 ``LightUse`` 也可表达。"""

    start: int
    end: int
    mode: str = "steady_red"  # steady_red / flash / white / off
    intensity: str = "low"
    source_id: str | None = None

    @property
    def duration(self) -> int:
        return max(0, self.end - self.start)


@dataclass
class LightPolicyWindow:
    rule_id: str
    start: int
    end: int
    mode: str  # dark / steady_red
    max_groups: int = 1
    intensity: str | None = "low"
    continuous_only: bool = True
    scope: str = "window"  # window / overlapping


@dataclass
class Closure:
    segment_id: str
    start: int | None
    end: int | None = None
    reason: str = ""
    source_id: str | None = None


@dataclass
class Segment:
    id: str
    name: str
    direction: Direction
    walk_minutes: int
    aliases: list[str] = field(default_factory=list)
    origin: str = ""
    destination: str = ""
    windows: list[LightPolicyWindow] = field(default_factory=list)
    closures: list[Closure] = field(default_factory=list)
    requires_escort: bool = False
    requires_continuous_light: bool = False
    exit_no_unescorted_transfer: bool = False

    def matches_alias(self, text: str | None) -> bool:
        if not text:
            return False
        if self.id.lower() in text.lower() or self.name in text:
            return True
        return any(alias and alias in text for alias in self.aliases)

    def active_closures(self, at: int | None = None,
                        start: int | None = None,
                        end: int | None = None) -> list[Closure]:
        result = []
        for closure in self.closures:
            if at is not None:
                if closure.start is not None and at < closure.start:
                    continue
                if closure.end is not None and at >= closure.end:
                    continue
            if start is not None and end is not None:
                c_start = closure.start if closure.start is not None else -10**9
                c_end = closure.end if closure.end is not None else 10**9
                if not (start < c_end and end > c_start):
                    continue
            result.append(closure)
        return result


@dataclass
class Rule:
    id: str
    title: str
    kind: str
    text: str = ""
    segment_id: str | None = None
    start: int | None = None
    end: int | None = None
    params: dict[str, Any] = field(default_factory=dict)
    source_id: str | None = None


@dataclass
class EquipmentCondition:
    id: str
    label: str
    applicable: bool = True
    acceptance_required: bool = True
    note: str = ""


@dataclass
class ObservationSlot:
    id: str
    station: str
    start: int
    end: int
    seats: int
    equipment: list[EquipmentCondition] = field(default_factory=list)


@dataclass
class Escort:
    id: str
    name: str
    segment_id: str
    start: int | None = None
    end: int | None = None
    capacity: int = 1
    self_reported_available: bool = False
    assignment_confirmed: bool = False
    assigned_code: str | None = None
    can_reassign_to: list[str] | None = None  # None 表示未限制
    restriction_source_id: str | None = None
    source_id: str | None = None


@dataclass
class Itinerary:
    direction: Direction
    segment_id: str
    start: int
    end: int
    light_uses: list[LightUse] = field(default_factory=list)
    escort_id: str | None = None
    continuous_light_required: bool | None = None
    measures: dict[str, Any] = field(default_factory=dict)
    note: str = ""

    @property
    def duration(self) -> int:
        return self.end - self.start


@dataclass
class Issue:
    code: str
    message: str
    severity: str = "blocker"  # blocker / approval / conflict / warning / info
    booking_code: str | None = None
    person_id: str | None = None
    acceptor_id: str | None = None
    acceptor_type: str | None = None
    segment_id: str | None = None
    rule_id: str | None = None
    source_id: str | None = None
    start: int | None = None
    end: int | None = None
    alternative: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return to_plain_dict(self)


@dataclass
class ConfirmationVersion:
    version: int
    status: VersionStatus = VersionStatus.DRAFT
    slot_id: str | None = None
    member_ids: list[str] = field(default_factory=list)
    seats: int = 1
    outbound: Itinerary | None = None
    return_trip: Itinerary | None = None
    equipment_ids: list[str] = field(default_factory=list)
    escort_ids: list[str] = field(default_factory=list)
    created_at: int | None = None
    issued_at: int | None = None
    redeemed_at: int | None = None
    change_reason: str = ""
    source_ids: list[str] = field(default_factory=list)
    parent_version: int | None = None
    superseded_by: int | None = None
    plan_signature: str = ""


@dataclass
class Booking:
    code: str
    slot_id: str | None
    member_ids: list[str] = field(default_factory=list)
    seats: int = 1
    status: BookingStatus = BookingStatus.PROSPECT
    hold_only: bool = False
    planned_outbound_segment: str | None = None
    planned_return_segment: str | None = None
    created_at: int | None = None
    held_at: int | None = None
    redeemed_at: int | None = None
    source_ids: list[str] = field(default_factory=list)
    versions: list[ConfirmationVersion] = field(default_factory=list)
    current_version: int = 0
    manual_reason: str | None = None

    def current(self) -> ConfirmationVersion | None:
        if not self.versions or self.current_version <= 0:
            return self.versions[-1] if self.versions else None
        return next((v for v in self.versions if v.version == self.current_version), None)


@dataclass
class Question:
    """经办人需要向某个具体移动/条件确认的问题。"""

    id: str
    message: str
    person_id: str | None = None
    person_name: str | None = None
    booking_code: str | None = None
    direction: Direction | None = None
    segment_id: str | None = None
    start: int | None = None
    end: int | None = None
    topic: str = "acceptance"
    required: bool = True
    options: list[str] = field(default_factory=list)
    source_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return to_plain_dict(self)


def to_plain_dict(obj: Any) -> Any:
    """dataclass/Enum 到可 JSON 化普通对象的转换。"""

    if obj is None:
        return None
    if isinstance(obj, Enum):
        return obj.value
    if hasattr(obj, "__dataclass_fields__"):
        return {k: to_plain_dict(v) for k, v in asdict(obj).items()}
    if isinstance(obj, dict):
        return {k: to_plain_dict(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [to_plain_dict(v) for v in obj]
    return obj


def issue_codes(issues: Iterable[Issue]) -> list[str]:
    return [issue.code for issue in issues]
