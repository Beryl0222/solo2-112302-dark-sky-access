"""值班调度服务：可用性、安排校验、确认码版本、闭合改派与审计。

服务默认保存在内存中，便于测试和 CLI 一次性读取语料；需要持久化时可序列化
:meth:`DutyService.snapshot` 的结果。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable
import itertools

from .models import (
    Acceptance,
    Booking,
    BookingStatus,
    Closure,
    ConfirmationVersion,
    Direction,
    DutyError,
    Escort,
    Issue,
    LightingNeed,
    ObservationSlot,
    Person,
    Question,
    Rule,
    Segment,
    VersionStatus,
    format_clock,
    overlaps,
    parse_clock,
    to_plain_dict,
)
from .parsing import parse_statement


BLOCKING_SEVERITIES = {"blocker", "approval", "conflict"}


@dataclass
class PlanReport:
    code: str
    version: int | None
    outbound: Any = None
    return_trip: Any = None
    issues: list[Issue] = field(default_factory=list)
    hold_only: bool = False
    option_id: str | None = None
    slot_id: str | None = None

    @property
    def acceptable(self) -> bool:
        """规则和本人硬需求可接受；仍可能缺少正式接受。"""

        return not any(i.severity in {"blocker", "conflict"} for i in self.issues)

    @property
    def confirmable(self) -> bool:
        return not any(i.severity in BLOCKING_SEVERITIES for i in self.issues)

    @property
    def required_acceptors(self) -> list[dict[str, Any]]:
        seen: set[tuple[str, str | None, str | None]] = set()
        result = []
        for issue in self.issues:
            if issue.severity != "approval" or not issue.acceptor_id:
                continue
            key = (issue.acceptor_type or "person", issue.acceptor_id, issue.code)
            if key in seen:
                continue
            seen.add(key)
            result.append({
                "acceptor_id": issue.acceptor_id,
                "acceptor_type": issue.acceptor_type or "person",
                "topic": issue.code,
                "message": issue.message,
                "segment_id": issue.segment_id,
                "rule_id": issue.rule_id,
            })
        return result

    def to_dict(self) -> dict[str, Any]:
        data = to_plain_dict(self)
        data["acceptable"] = self.acceptable
        data["confirmable"] = self.confirmable
        data["required_acceptors"] = self.required_acceptors
        return data


class DutyService:
    def __init__(self, now: int | str | None = None):
        self.now = parse_clock(now)
        self.people: dict[str, Person] = {}
        self.needs: list[LightingNeed] = []
        self.acceptances: list[Acceptance] = []
        self.segments: dict[str, Segment] = {}
        self.rules: dict[str, Rule] = {}
        self.slots: dict[str, ObservationSlot] = {}
        self.escorts: dict[str, Escort] = {}
        self.bookings: dict[str, Booking] = {}
        self.questions: list[Question] = []
        self.manual_queue: set[str] = set()
        self.event_log: list[dict[str, Any]] = []
        self._ids = itertools.count(1)

    # ------------------------------------------------------------------
    # 基础录入
    # ------------------------------------------------------------------
    def _next_id(self, prefix: str) -> str:
        return f"{prefix}{next(self._ids)}"

    def log(self, event: str, **data: Any) -> None:
        self.event_log.append({
            "seq": len(self.event_log) + 1,
            "at": self.now,
            "event": event,
            **to_plain_dict(data),
        })

    def set_now(self, value: int | str) -> None:
        self.now = parse_clock(value)

    def add_person(self, person_id: str, name: str, **kwargs: Any) -> Person:
        person = Person(id=person_id, name=name, **kwargs)
        self.people[person_id] = person
        self.log("person_added", person=person)
        return person

    def add_rule(self, rule_id: str, title: str, kind: str, **kwargs: Any) -> Rule:
        rule = Rule(id=rule_id, title=title, kind=kind, **kwargs)
        self.rules[rule_id] = rule
        self.log("rule_added", rule=rule)
        return rule

    def add_need(self, need: LightingNeed) -> LightingNeed:
        self.needs.append(need)
        return need

    def ingest_statement(
        self,
        text: str,
        *,
        source_id: str | None = None,
        person_id: str | None = None,
        person_name: str | None = None,
    ):
        result = parse_statement(
            text,
            source_id=source_id,
            default_person_id=person_id,
            default_person_name=person_name,
        )
        for need in result.needs:
            if person_id and not need.proxy:
                need.person_id = person_id
            if person_name and not need.person_name:
                need.person_name = person_name
            self.add_need(need)
        self.log("statement_ingested", source_id=source_id, parse_result=result)
        return result

    def add_acceptance(self, acceptor_id: str, *, acceptor_type: str = "person",
                       kind: str = "condition", **kwargs: Any) -> Acceptance:
        acceptance = Acceptance(
            id=kwargs.pop("id", self._next_id("A")),
            acceptor_id=acceptor_id,
            acceptor_type=acceptor_type,
            kind=kind,
            **kwargs,
        )
        self.acceptances.append(acceptance)
        self.log("acceptance_added", acceptance=acceptance)
        return acceptance

    def accept_plan(self, code: str, acceptor_id: str, version: int | None = None,
                    *, source_id: str | None = None) -> Acceptance:
        booking = self._booking(code)
        version = version or booking.current_version or (booking.current().version if booking.current() else None)
        return self.add_acceptance(
            acceptor_id,
            kind="plan",
            code=code,
            version=version,
            source_id=source_id,
            self_confirmed=True,
        )

    def confirm_escort(self, escort_id: str, code: str | None = None,
                       version: int | None = None) -> Escort:
        escort = self.escorts[escort_id]
        escort.assignment_confirmed = True
        if code:
            escort.assigned_code = code
            self.add_acceptance(
                escort_id,
                acceptor_type="escort",
                kind="escort",
                code=code,
                version=version,
                escort_id=escort_id,
                self_confirmed=True,
            )
        self.log("escort_confirmed", escort_id=escort_id, code=code, version=version)
        return escort

    def add_slot(self, slot: ObservationSlot) -> ObservationSlot:
        self.slots[slot.id] = slot
        self.log("slot_added", slot=slot)
        return slot

    def add_segment(self, segment: Segment) -> Segment:
        self.segments[segment.id] = segment
        self.log("segment_added", segment=segment)
        return segment

    def add_escort(self, escort: Escort) -> Escort:
        self.escorts[escort.id] = escort
        self.log("escort_added", escort=escort)
        return escort

    def hold_seat(self, code: str, slot_id: str | None, member_ids: list[str],
                  seats: int = 1, *, planned_return_segment: str | None = None,
                  planned_outbound_segment: str | None = None,
                  source_ids: list[str] | None = None,
                  at: int | str | None = None) -> Booking:
        booking = Booking(
            code=code,
            slot_id=slot_id,
            member_ids=list(member_ids),
            seats=seats,
            status=BookingStatus.HELD,
            hold_only=True,
            planned_return_segment=planned_return_segment,
            planned_outbound_segment=planned_outbound_segment,
            held_at=parse_clock(at) if at is not None else self.now,
            source_ids=source_ids or [],
        )
        booking.versions.append(ConfirmationVersion(
            version=0,
            status=VersionStatus.DRAFT,
            slot_id=slot_id,
            member_ids=list(member_ids),
            seats=seats,
        ))
        self.bookings[code] = booking
        self.log("seat_held", code=code, slot_id=slot_id, seats=seats)
        return booking

    def admit_code(self, code: str, *, slot_id: str | None, member_ids: list[str],
                   seats: int | None = None, redeemed_at: int | str | None = None,
                   outbound: Any = None, return_trip: Any = None,
                   equipment_ids: list[str] | None = None,
                   source_ids: list[str] | None = None,
                   change_reason: str = "已发确认码核销") -> Booking:
        """录入已核销码及其历史确认版本。

        注意：此方法保留历史事实，不代表该版本满足现行确认条件；缺口由审计/校验指出。
        """

        seats = seats if seats is not None else len(member_ids)
        at = parse_clock(redeemed_at) if redeemed_at is not None else self.now
        booking = self.bookings.get(code) or Booking(code=code, slot_id=slot_id)
        booking.slot_id = slot_id
        booking.member_ids = list(member_ids)
        booking.seats = seats
        booking.status = BookingStatus.REDEEMED
        booking.redeemed_at = at
        if source_ids:
            booking.source_ids.extend(s for s in source_ids if s not in booking.source_ids)
        version_number = booking.current_version + 1 if booking.current_version else 1
        version = ConfirmationVersion(
            version=version_number,
            status=VersionStatus.REDEEMED,
            slot_id=slot_id,
            member_ids=list(member_ids),
            seats=seats,
            outbound=outbound,
            return_trip=return_trip,
            equipment_ids=equipment_ids or [],
            escort_ids=[x for x in [return_trip.escort_id if return_trip else None] if x],
            created_at=at,
            issued_at=at,
            redeemed_at=at,
            change_reason=change_reason,
            source_ids=source_ids or [],
        )
        booking.versions.append(version)
        booking.current_version = version_number
        self.bookings[code] = booking
        self.log("code_redeemed", code=code, version=version_number, at=at)
        return booking

    def _booking(self, code: str) -> Booking:
        if code not in self.bookings:
            raise KeyError(f"未知确认码/预约：{code}")
        return self.bookings[code]

    # ------------------------------------------------------------------
    # 区段与时间计算
    # ------------------------------------------------------------------
    def resolve_segment(self, direction: Direction,
                        segment_id: str | None = None,
                        query: str | None = None) -> Segment | None:
        if segment_id and segment_id in self.segments:
            return self.segments[segment_id]
        candidates = list(self.segments.values())
        if segment_id:
            candidates = [s for s in candidates if s.id == segment_id]
        if query:
            matched = [s for s in candidates if s.matches_alias(query)]
            if matched:
                candidates = matched
        compatible = [
            s for s in candidates
            if direction in (Direction.UNKNOWN, Direction.ROUNDTRIP)
            or s.direction in (direction, Direction.ROUNDTRIP)
        ]
        return (compatible or candidates or [None])[0]

    @staticmethod
    def feasible_starts(segment: Segment, window) -> tuple[int, int] | None:
        latest = window.end - segment.walk_minutes
        if latest < window.start:
            return None
        return window.start, latest

    def candidate_start(self, segment: Segment, preferred: int | None) -> int | None:
        ranges = []
        for window in segment.windows:
            interval = self.feasible_starts(segment, window)
            if interval:
                ranges.append(interval)
        if preferred is None:
            return ranges[0][0] if ranges else None
        for low, high in ranges:
            if low <= preferred <= high:
                return preferred
        if ranges:
            low = min(x[0] for x in ranges)
            high = max(x[1] for x in ranges)
            return min(max(preferred, low), high)
        return preferred

    def default_itinerary(self, segment: Segment, preferred_start: int | None,
                          direction: Direction) -> Any:
        from .models import Itinerary, LightUse

        start = self.candidate_start(segment, preferred_start)
        if start is None:
            return None
        end = start + segment.walk_minutes
        itinerary = Itinerary(direction=direction, segment_id=segment.id, start=start, end=end)

        covering_red = [
            w for w in segment.windows
            if w.mode == "steady_red" and w.start <= start and end <= w.end
        ]
        if covering_red:
            window = covering_red[0]
            itinerary.light_uses.append(LightUse(
                start=start,
                end=end,
                mode="steady_red",
                intensity=window.intensity or "low",
            ))
        if segment.requires_escort:
            escort = self._available_escort(segment.id, start, end)
            if escort:
                itinerary.escort_id = escort.id
        itinerary.continuous_light_required = segment.requires_continuous_light or None
        return itinerary

    def _available_escort(self, segment_id: str, start: int, end: int) -> Escort | None:
        for escort in self.escorts.values():
            if escort.segment_id != segment_id:
                continue
            if escort.start is not None and start < escort.start:
                continue
            if escort.end is not None and end > escort.end:
                continue
            if self._escort_busy(escort.id, start, end):
                continue
            return escort
        return None

    def _escort_busy(self, escort_id: str, start: int, end: int,
                     exclude_code: str | None = None) -> bool:
        escort = self.escorts.get(escort_id)
        if escort and escort.assigned_code and escort.assigned_code != exclude_code:
            other = self.bookings.get(escort.assigned_code)
            if other and other.status not in (BookingStatus.CANCELLED,):
                return True
        for code, booking in self.bookings.items():
            if code == exclude_code or booking.status == BookingStatus.CANCELLED:
                continue
            for version in booking.versions:
                if version.superseded_by is not None:
                    continue
                if version.status not in (
                    VersionStatus.PROPOSED, VersionStatus.ISSUED, VersionStatus.REDEEMED
                ):
                    continue
                for itinerary in (version.outbound, version.return_trip):
                    if not itinerary or itinerary.escort_id != escort_id:
                        continue
                    if overlaps(start, end, itinerary.start, itinerary.end):
                        return True
        return False

    # ------------------------------------------------------------------
    # 容量
    # ------------------------------------------------------------------
    def slot_occupancy(self, slot_id: str, exclude_code: str | None = None) -> int:
        total = 0
        for code, booking in self.bookings.items():
            if code == exclude_code or booking.slot_id != slot_id:
                continue
            if booking.status in (
                BookingStatus.HELD, BookingStatus.CONFIRMED,
                BookingStatus.REDEEMED, BookingStatus.COMPLETED,
            ):
                total += booking.seats
        return total

    def window_occupants(self, rule_id: str, exclude_code: str | None = None) -> set[str]:
        occupants: set[str] = set()
        for code, booking in self.bookings.items():
            if code == exclude_code or booking.status == BookingStatus.CANCELLED:
                continue
            for version in booking.versions:
                if version.superseded_by is not None:
                    continue
                if version.status not in (
                    VersionStatus.PROPOSED, VersionStatus.ISSUED, VersionStatus.REDEEMED
                ):
                    continue
                for itinerary in (version.outbound, version.return_trip):
                    if not itinerary:
                        continue
                    segment = self.segments.get(itinerary.segment_id)
                    if not segment:
                        continue
                    for window in segment.windows:
                        if window.rule_id != rule_id:
                            continue
                        if window.start <= itinerary.start and itinerary.end <= window.end:
                            occupants.add(code)
        return occupants

    def tentative_window_claims(self, rule_id: str,
                                exclude_code: str | None = None) -> set[str]:
        """未形成行程但已表达/预留路线意向的组，用于提前提示同窗口冲突。"""

        claims: set[str] = set()
        for code, booking in self.bookings.items():
            if code == exclude_code or booking.status == BookingStatus.CANCELLED:
                continue
            if code in self.window_occupants(rule_id):
                continue
            segment_id = booking.planned_return_segment
            segment = self.segments.get(segment_id or "")
            if not segment:
                continue
            window = next((w for w in segment.windows if w.rule_id == rule_id), None)
            if window:
                claims.add(code)
        return claims

    # ------------------------------------------------------------------
    # 规则与需求校验
    # ------------------------------------------------------------------
    def _continuous_steady_coverage(self, itinerary: Any) -> tuple[bool, int | None]:
        uses = sorted(
            (u for u in itinerary.light_uses if u.mode == "steady_red"),
            key=lambda u: u.start,
        )
        covered = itinerary.start
        for use in uses:
            if use.start > covered:
                return False, covered
            covered = max(covered, use.end)
            if covered >= itinerary.end:
                return True, None
        return covered >= itinerary.end, (None if covered >= itinerary.end else covered)

    def evaluate_itinerary_rules(self, itinerary: Any, code: str) -> list[Issue]:
        issues: list[Issue] = []
        segment = self.segments.get(itinerary.segment_id)
        if not segment:
            return [Issue(
                "unknown_segment", f"系统中没有区段 {itinerary.segment_id}",
                segment_id=itinerary.segment_id, booking_code=code,
            )]

        if segment.direction != Direction.ROUNDTRIP and itinerary.direction != segment.direction:
            issues.append(Issue(
                "direction_mismatch",
                f"{segment.id} 的登记方向是 {segment.direction.value}，不能用作 {itinerary.direction.value}",
                segment_id=segment.id, booking_code=code,
            ))

        closures = segment.active_closures(start=itinerary.start, end=itinerary.end)
        for closure in closures:
            issues.append(Issue(
                "segment_closed",
                f"{segment.name} 在 {format_clock(closure.start)} 起临时闭合：{closure.reason}",
                segment_id=segment.id,
                rule_id="R-TEMP-CLOSURE",
                source_id=closure.source_id,
                booking_code=code,
                start=closure.start,
                end=closure.end,
            ))

        # 灯光窗口与模式
        for use in itinerary.light_uses:
            if use.mode == "off":
                continue
            covering = [
                w for w in segment.windows
                if w.start <= use.start and use.end <= w.end
            ]
            if not covering:
                issues.append(Issue(
                    "outside_lighting_window",
                    f"{segment.name} {format_clock(use.start)}-{format_clock(use.end)} "
                    "不在许可灯光窗口内",
                    segment_id=segment.id,
                    rule_id="R-LIGHT-WINDOW",
                    booking_code=code,
                    start=use.start,
                    end=use.end,
                ))
                continue
            for window in covering:
                if window.mode == "dark":
                    issues.append(Issue(
                        "absolute_light_ban",
                        f"{segment.name} 在 {format_clock(window.start)}-"
                        f"{format_clock(window.end)} 绝对禁补光",
                        segment_id=segment.id, rule_id=window.rule_id,
                        booking_code=code, start=window.start, end=window.end,
                    ))
                elif window.mode == "steady_red":
                    if use.mode != "steady_red" or (
                        window.intensity and use.intensity not in (window.intensity, "dark")
                    ):
                        issues.append(Issue(
                            "lighting_mode_not_permitted",
                            f"{segment.name} 该窗口只允许{window.intensity or '低强度'}常亮红光",
                            segment_id=segment.id, rule_id=window.rule_id,
                            booking_code=code,
                        ))

        red_windows = [
            w for w in segment.windows
            if w.mode == "steady_red"
            and w.start <= itinerary.start and itinerary.end <= w.end
        ]
        must_be_continuous = bool(
            segment.requires_continuous_light
            or itinerary.continuous_light_required
            or any(w.continuous_only for w in red_windows if itinerary.light_uses)
        )
        # 若任何人明确要求连续红光，也打开全程覆盖校验；单纯“拒绝断续”
        # 不会在本来就全程关灯的黑暗路线上强制有灯。
        person_ids = self._booking(code).member_ids if code in self.bookings else []
        for person_id in person_ids:
            for need in self.needs_for(person_id):
                if self._need_applies(need, itinerary, segment):
                    if need.kind in {"continuous_light"} or (
                        need.kind == "steady_red" and need.modality == "requires"
                    ) or need.continuous is True:
                        must_be_continuous = True

        if must_be_continuous and itinerary.light_uses:
            ok, gap_at = self._continuous_steady_coverage(itinerary)
            if not ok:
                issues.append(Issue(
                    "continuity_not_met",
                    f"{segment.name} 的连续步行照明不能由几次瞬时开灯或闪烁拼成"
                    + (f"，约在 {format_clock(gap_at)} 出现断档" if gap_at is not None else ""),
                    segment_id=segment.id, rule_id="R-P4-CONTINUITY",
                    booking_code=code,
                ))

        # 同一限量红光窗口按“窗口”占组，而不是只看时间是否重叠。
        for window in red_windows:
            occupants = self.window_occupants(window.rule_id, exclude_code=code)
            if len(occupants) >= window.max_groups:
                issues.append(Issue(
                    "red_window_conflict",
                    f"{segment.name} 红光窗口 {format_clock(window.start)}-"
                    f"{format_clock(window.end)} 最多 {window.max_groups} 组；"
                    f"已占用：{', '.join(sorted(occupants))}",
                    severity="conflict",
                    segment_id=segment.id,
                    rule_id="R-P4-WINDOW-CAPACITY",
                    source_id="P4",
                    booking_code=code,
                    start=window.start,
                    end=window.end,
                    alternative={"window_rule_id": window.rule_id},
                ))
            else:
                tentative = self.tentative_window_claims(window.rule_id, exclude_code=code)
                if tentative:
                    issues.append(Issue(
                        "tentative_red_window_overlap",
                        f"{segment.name} 同一红光窗口还有意向/暂留组 "
                        f"{', '.join(sorted(tentative))}；两组不能同时确认，需让其中一组改时段/路线",
                        severity="warning",
                        segment_id=segment.id,
                        rule_id="R-P4-WINDOW-CAPACITY",
                        source_id="P4",
                        booking_code=code,
                        start=window.start,
                        end=window.end,
                        alternative={"window_rule_id": window.rule_id,
                                     "tentative_codes": sorted(tentative)},
                    ))

        # 陪行资源
        if segment.requires_escort or itinerary.escort_id:
            if not itinerary.escort_id:
                issues.append(Issue(
                    "missing_escort",
                    f"{segment.name} 每组必须有陪行，出口不接受无陪行转接",
                    severity="approval",
                    segment_id=segment.id, rule_id="R-ESCORT-REQUIRED",
                    booking_code=code,
                    acceptor_type="staff",
                ))
            else:
                escort = self.escorts.get(itinerary.escort_id)
                if not escort:
                    issues.append(Issue(
                        "escort_unknown", f"没有陪行员 {itinerary.escort_id}",
                        segment_id=segment.id, booking_code=code,
                    ))
                else:
                    if escort.segment_id != segment.id and escort.can_reassign_to is not None:
                        if segment.id not in escort.can_reassign_to:
                            issues.append(Issue(
                                "escort_reassignment_blocked",
                                f"陪行员 {escort.name} 不能从 {escort.segment_id} 调往 {segment.id}",
                                segment_id=segment.id,
                                rule_id="R-ESCORT-REASSIGNMENT",
                                source_id=escort.restriction_source_id,
                                booking_code=code,
                                acceptor_id=escort.id, acceptor_type="escort",
                            ))
                    if escort.start is not None and itinerary.start < escort.start:
                        issues.append(Issue(
                            "escort_outside_hours",
                            f"陪行员 {escort.name} {format_clock(escort.start)} 才开始",
                            segment_id=segment.id, booking_code=code,
                            acceptor_id=escort.id, acceptor_type="escort",
                        ))
                    if escort.end is not None and itinerary.end > escort.end:
                        issues.append(Issue(
                            "escort_outside_hours",
                            f"陪行员 {escort.name} 值班至 {format_clock(escort.end)}，无法走完全程",
                            segment_id=segment.id, booking_code=code,
                            acceptor_id=escort.id, acceptor_type="escort",
                        ))
                    if self._escort_busy(escort.id, itinerary.start, itinerary.end, code):
                        issues.append(Issue(
                            "escort_conflict",
                            f"陪行员 {escort.name} 同一时间已负责另一组",
                            severity="conflict",
                            segment_id=segment.id, booking_code=code,
                            acceptor_id=escort.id, acceptor_type="escort",
                        ))
                    escort_ok = any(
                        a.acceptor_type == "escort"
                        and a.acceptor_id == escort.id
                        and a.kind == "escort"
                        and (not a.code or a.code == code)
                        for a in self.acceptances
                    ) or (escort.assignment_confirmed and escort.assigned_code == code)
                    if not escort_ok:
                        issues.append(Issue(
                            "missing_escort_confirmation",
                            f"需陪行员 {escort.name} 本人确认负责 {code or '该组'} 的这一趟陪行",
                            severity="approval",
                            segment_id=segment.id, booking_code=code,
                            acceptor_id=escort.id, acceptor_type="escort",
                        ))
        return issues

    def evaluate_observation_rules(self, slot_id: str,
                                   light_uses: list[Any] | None = None,
                                   code: str | None = None) -> list[Issue]:
        """校验看星/观测台时段的绝对禁光规则。"""

        slot = self.slots.get(slot_id)
        if slot is None:
            raise KeyError(f"未知观测时段：{slot_id}")
        issues: list[Issue] = []
        for rule in self.rules.values():
            if rule.kind != "absolute_dark":
                continue
            if rule.segment_id not in (None, slot.id, slot.station):
                continue
            dark_start = rule.start if rule.start is not None else slot.start
            dark_end = rule.end if rule.end is not None else slot.end
            for use in light_uses or []:
                if use.mode == "off":
                    continue
                if overlaps(use.start, use.end, dark_start, dark_end):
                    issues.append(Issue(
                        "absolute_light_ban",
                        f"{slot.station} 看星时段 {format_clock(dark_start)}-"
                        f"{format_clock(dark_end)} 绝对禁光，不允许 {use.mode}",
                        segment_id=slot.id, rule_id=rule.id, source_id=rule.source_id,
                        booking_code=code, start=use.start, end=use.end,
                    ))
        return issues

    def needs_for(self, person_id: str) -> list[LightingNeed]:
        return [n for n in self.needs if n.person_id == person_id]

    def _need_applies(self, need: LightingNeed, itinerary: Any,
                      segment: Segment | None = None) -> bool:
        if need.status in {"superseded"}:
            return False
        if itinerary.direction in need.except_directions:
            return False
        if need.activity_scope == "observation" and itinerary.direction not in (
            Direction.AT_STATION, Direction.UNKNOWN
        ):
            return False
        if need.direction not in (Direction.UNKNOWN, Direction.ROUNDTRIP, itinerary.direction):
            return False
        if need.segment_id and need.segment_id != itinerary.segment_id:
            return False
        if need.segment_query and segment and not segment.matches_alias(need.segment_query):
            return False
        return True

    def evaluate_person_needs(self, itinerary: Any, code: str,
                              member_ids: list[str]) -> list[Issue]:
        issues: list[Issue] = []
        segment = self.segments.get(itinerary.segment_id)
        for person_id in member_ids:
            person = self.people.get(person_id)
            who = person.name if person else person_id
            for need in self.needs_for(person_id):
                if need.proxy or not need.self_reported:
                    continue
                if not self._need_applies(need, itinerary, segment):
                    continue

                if need.flashing_allowed is False:
                    flashes = [u for u in itinerary.light_uses if u.mode in {"flash", "strobe"}]
                    if flashes:
                        issues.append(Issue(
                            "flashing_forbidden_by_person",
                            f"{who} 需要避免闪烁，本安排含闪烁/频闪光",
                            person_id=person_id, booking_code=code,
                            segment_id=itinerary.segment_id,
                            acceptor_id=person_id, acceptor_type="person",
                        ))

                if need.kind == "continuous_light" or (
                    need.kind == "steady_red" and need.modality == "requires"
                ) or need.continuous is True:
                    ok, gap = self._continuous_steady_coverage(itinerary)
                    if not ok:
                        issues.append(Issue(
                            "person_continuous_red_required",
                            f"{who} 在这一趟要求连续常亮红光，不能用断续开灯替代",
                            person_id=person_id, booking_code=code,
                            segment_id=itinerary.segment_id,
                            acceptor_id=person_id, acceptor_type="person",
                        ))

                if need.intermittent_allowed is False and itinerary.light_uses:
                    ordered = sorted(itinerary.light_uses, key=lambda u: u.start)
                    has_gap = any(
                        left.end < right.start for left, right in zip(ordered, ordered[1:])
                    )
                    has_intermittent_mode = any(
                        u.mode in {"flash", "strobe", "intermittent"} for u in ordered
                    )
                    if has_gap or has_intermittent_mode:
                        issues.append(Issue(
                            "intermittent_light_rejected",
                            f"{who} 不接受断续开灯；要么全程连续低强度红光，要么本趟不亮灯",
                            person_id=person_id, booking_code=code,
                            segment_id=itinerary.segment_id,
                            acceptor_id=person_id, acceptor_type="person",
                        ))

                if need.kind == "no_bright_light" and need.modality == "rejects":
                    bad = [
                        u for u in itinerary.light_uses
                        if u.intensity == "bright" or u.mode in {"white", "bright_white"}
                    ]
                    if bad:
                        issues.append(Issue(
                            "bright_light_rejected",
                            f"{who} 不接受亮光；低强度红光仍可另行确认",
                            person_id=person_id, booking_code=code,
                            segment_id=itinerary.segment_id,
                        ))

                if need.alternative == "on_call_assistant":
                    replaced = itinerary.measures.get(
                        "continuous_light_replaced_by"
                    ) == "on_call_assistant"
                    if replaced:
                        consent = any(
                            a.acceptor_id == person_id and a.kind == "lighting"
                            and a.terms.get("remove_continuous_light") is True
                            for a in self.acceptances
                        )
                        if not consent:
                            issues.append(Issue(
                                "assistant_not_light_consent",
                                f"“随时可叫助手”不是同意取消 {who} 所需的常亮；"
                                "必须由本人明确接受该替代",
                                severity="approval", person_id=person_id,
                                acceptor_id=person_id, acceptor_type="person",
                                booking_code=code, segment_id=itinerary.segment_id,
                            ))
        return issues

    # ------------------------------------------------------------------
    # 接受记录检查
    # ------------------------------------------------------------------
    def _plan_acceptance(self, person_id: str, code: str,
                         version: int | None) -> Acceptance | None:
        for acceptance in self.acceptances:
            if not acceptance.accepted:
                continue
            if acceptance.acceptor_id != person_id or acceptance.acceptor_type != "person":
                continue
            if acceptance.kind != "plan" or not acceptance.code or acceptance.code != code:
                continue
            if acceptance.version is not None and version is not None and acceptance.version != version:
                continue
            return acceptance
        return None

    def _route_acceptance(self, person_id: str, itinerary: Any,
                          code: str, version: int | None) -> bool:
        if self._plan_acceptance(person_id, code, version):
            return True
        for a in self.acceptances:
            if not a.accepted:
                continue
            if a.acceptor_id != person_id or a.acceptor_type != "person":
                continue
            if a.kind not in {"route", "condition"}:
                continue
            if a.code and a.code != code:
                continue
            if a.version is not None and version is not None and a.version != version:
                continue
            if a.direction and a.direction != itinerary.direction:
                continue
            if a.segment_id and a.segment_id != itinerary.segment_id:
                continue
            if a.start is not None and a.start != itinerary.start:
                continue
            if a.end is not None and a.end != itinerary.end:
                continue
            return True

        # 本人第一人称“往返都能有人陪/可以关灯”等只构成对应措施的接受，
        # 但不自动等于接受任一具体路线时刻；这里不把它当作完整路线接受。
        return False

    def _escort_acceptance_by_person(self, person_id: str, escort_id: str | None,
                                     itinerary: Any, code: str,
                                     version: int | None) -> bool:
        if self._plan_acceptance(person_id, code, version):
            return True
        for a in self.acceptances:
            if not a.accepted:
                continue
            if a.acceptor_id != person_id or a.kind != "escort":
                continue
            if a.code and a.code != code:
                continue
            if a.direction and a.direction != itinerary.direction:
                continue
            if a.escort_id and escort_id and a.escort_id != escort_id:
                # “和韩榆同一个陪行员”这类关系型同意由 terms 处理。
                if a.terms.get("same_escort_as"):
                    anchor = a.terms["same_escort_as"]
                    anchor_accepts = any(
                        x.acceptor_id == anchor and x.kind == "escort"
                        and (not x.escort_id or x.escort_id == escort_id)
                        for x in self.acceptances
                    )
                    if not anchor_accepts:
                        continue
                else:
                    continue
            return True
        # 第一人称“能有人陪”可视为接受陪行这种资源形式，但不替代陪行员本人确认。
        return any(
            n.person_id == person_id and n.alternative == "escort_or_companion"
            and n.modality == "accepts"
            and self._need_applies(n, itinerary, self.segments.get(itinerary.segment_id))
            for n in self.needs
        )

    def check_acceptances(self, code: str, version: int | None,
                          member_ids: list[str], slot: ObservationSlot | None,
                          outbound: Any, return_trip: Any,
                          hold_only: bool) -> list[Issue]:
        issues: list[Issue] = []
        booking = self._booking(code)
        version_obj = next((v for v in booking.versions if v.version == version), booking.current())
        version_number = version_obj.version if version_obj else version

        if hold_only:
            issues.append(Issue(
                "seat_hold_not_confirmation",
                f"{code} 只是空位暂留，尚未取得本人完整确认，不能放行入场",
                severity="approval", booking_code=code,
            ))

        for person_id in member_ids:
            person = self.people.get(person_id)
            who = person.name if person else person_id
            plan_ok = bool(self._plan_acceptance(person_id, code, version_number))

            for trip, label in ((outbound, "去程"), (return_trip, "回程")):
                if trip is None:
                    issues.append(Issue(
                        "missing_trip_plan", f"{who} 缺少可核对的{label}安排",
                        severity="approval", person_id=person_id,
                        acceptor_id=person_id, acceptor_type="person",
                        booking_code=code,
                    ))
                    continue
                if not self._route_acceptance(person_id, trip, code, version_number):
                    segment = self.segments.get(trip.segment_id)
                    seg_name = segment.name if segment else trip.segment_id
                    issues.append(Issue(
                        "missing_route_acceptance",
                        f"需 {who} 本人确认{label}{seg_name} "
                        f"{format_clock(trip.start)}-{format_clock(trip.end)} 的路线和光线条件",
                        severity="approval", person_id=person_id,
                        acceptor_id=person_id, acceptor_type="person",
                        booking_code=code, segment_id=trip.segment_id,
                        start=trip.start, end=trip.end,
                    ))
                if trip.escort_id and not self._escort_acceptance_by_person(
                    person_id, trip.escort_id, trip, code, version_number
                ):
                    escort = self.escorts.get(trip.escort_id)
                    ename = escort.name if escort else trip.escort_id
                    issues.append(Issue(
                        "missing_person_escort_acceptance",
                        f"需 {who} 确认{label}由 {ename} 陪行",
                        severity="approval", person_id=person_id,
                        acceptor_id=person_id, acceptor_type="person",
                        booking_code=code, segment_id=trip.segment_id,
                    ))

            if slot:
                for equipment in slot.equipment:
                    if not equipment.applicable or not equipment.acceptance_required:
                        continue
                    equipment_ok = plan_ok or any(
                        a.accepted and a.acceptor_id == person_id and a.kind == "equipment"
                        and (not a.equipment_id or a.equipment_id == equipment.id)
                        for a in self.acceptances
                    )
                    if not equipment_ok:
                        issues.append(Issue(
                            "missing_equipment_acceptance",
                            f"需 {who} 确认设备适用条件：{equipment.label}",
                            severity="approval", person_id=person_id,
                            acceptor_id=person_id, acceptor_type="person",
                            booking_code=code, rule_id=equipment.id,
                        ))

            # 代理/集体表述永远不能冒充本人接受。
            proxy_needs = [n for n in self.needs if n.person_id == person_id and n.proxy]
            if proxy_needs and not plan_ok:
                issues.append(Issue(
                    "proxy_statement_needs_self_confirmation",
                    f"{who} 的报名含代填/集体表述；须向本人核对具体移动，不能按整句放行",
                    severity="approval", person_id=person_id,
                    acceptor_id=person_id, acceptor_type="person",
                    booking_code=code,
                ))
        return issues

    # ------------------------------------------------------------------
    # 安排评估与候选
    # ------------------------------------------------------------------
    def evaluate_arrangement(self, code: str, outbound: Any = None,
                             return_trip: Any = None, *,
                             version: int | None = None,
                             hold_only: bool | None = None,
                             slot_id: str | None = None) -> PlanReport:
        booking = self._booking(code)
        if booking.status == BookingStatus.CANCELLED:
            return PlanReport(code, version, outbound, return_trip, [Issue(
                "booking_cancelled", f"{code} 已取消", booking_code=code
            )], slot_id=slot_id)

        version_obj = None
        if version is not None:
            version_obj = next((v for v in booking.versions if v.version == version), None)
        elif booking.current():
            version_obj = booking.current()
            version = version_obj.version

        outbound = outbound or (version_obj.outbound if version_obj else None)
        return_trip = return_trip or (version_obj.return_trip if version_obj else None)
        member_ids = version_obj.member_ids if version_obj else booking.member_ids
        candidate_slot_id = slot_id or booking.slot_id
        slot = self.slots.get(candidate_slot_id or "")
        if hold_only is None:
            evaluating_proposal = bool(
                version_obj and version_obj.version > 0
                and version_obj.status == VersionStatus.PROPOSED
            )
            hold_only = (
                booking.hold_only or booking.status == BookingStatus.HELD
            ) and not evaluating_proposal

        issues: list[Issue] = []
        # exclude_code 已去掉该码在原时段的席位；评估替代时段时仍需加回本码人数。
        other_occupancy = self.slot_occupancy(candidate_slot_id or "", exclude_code=code)
        occupancy = other_occupancy + booking.seats if slot_id else other_occupancy
        if slot and occupancy > slot.seats:
            issues.append(Issue(
                "seat_capacity",
                f"{slot.station} {format_clock(slot.start)}-{format_clock(slot.end)} "
                f"需 {occupancy} 席，超过 {slot.seats} 席",
                severity="conflict", booking_code=code, rule_id="R-SEAT-CAPACITY",
            ))

        for trip in (outbound, return_trip):
            if trip is not None:
                issues.extend(self.evaluate_itinerary_rules(trip, code))
                issues.extend(self.evaluate_person_needs(trip, code, member_ids))

        issues.extend(self.check_acceptances(
            code, version, member_ids, slot, outbound, return_trip, hold_only
        ))
        return PlanReport(code, version, outbound, return_trip, issues, slot_id=candidate_slot_id)

    def evaluate_confirmation(self, code: str, version: int | None = None) -> PlanReport:
        return self.evaluate_arrangement(code, version=version)

    def suggest_options(self, code: str, *, at: int | str | None = None) -> list[PlanReport]:
        booking = self._booking(code)
        slot = self.slots.get(booking.slot_id or "")
        current = booking.current()
        preferred_out = slot.start - 5 if slot else None
        preferred_back = slot.end if slot else (parse_clock(at) if at is not None else self.now)

        outbound = current.outbound if current and current.outbound else None
        if outbound is None:
            out_segment = self.resolve_segment(
                Direction.OUTBOUND, booking.planned_outbound_segment
            )
            if out_segment:
                outbound = self.default_itinerary(out_segment, preferred_out, Direction.OUTBOUND)

        segment_ids: list[str | None] = []
        if booking.planned_return_segment:
            segment_ids.append(booking.planned_return_segment)
        segment_ids.extend(s.id for s in self.segments.values() if s.direction == Direction.RETURN)
        # 去重并保持优先顺序
        seen = set()
        ordered_ids = []
        for sid in segment_ids:
            if sid and sid not in seen:
                seen.add(sid)
                ordered_ids.append(sid)

        reports: list[PlanReport] = []
        for index, segment_id in enumerate(ordered_ids):
            segment = self.segments[segment_id]
            return_trip = self.default_itinerary(segment, preferred_back, Direction.RETURN)
            if return_trip is None:
                continue
            report = self.evaluate_arrangement(
                code, outbound=outbound, return_trip=return_trip, version=None
            )
            report.option_id = f"{code}-OPT{index + 1}-{segment_id}"
            reports.append(report)

        reports.sort(key=lambda r: (
            not r.confirmable,
            sum(1 for i in r.issues if i.severity == "blocker"),
            sum(1 for i in r.issues if i.severity == "conflict"),
            sum(1 for i in r.issues if i.severity == "approval"),
            len(r.issues),
        ))
        return reports

    def best_option(self, code: str) -> PlanReport | None:
        options = self.suggest_options(code)
        return options[0] if options else None

    def reschedule_options(self, code: str) -> list[PlanReport]:
        """为“已确认但未进场”的组搜索其他观测时段和匹配路线。"""

        booking = self._booking(code)
        reports: list[PlanReport] = []
        for slot in sorted(self.slots.values(), key=lambda s: s.start):
            outbound_segment = self.resolve_segment(
                Direction.OUTBOUND, booking.planned_outbound_segment
            ) or next(iter([s for s in self.segments.values()
                            if s.direction == Direction.OUTBOUND]), None)
            outbound = None
            if outbound_segment:
                outbound = self.default_itinerary(
                    outbound_segment, slot.start - outbound_segment.walk_minutes,
                    Direction.OUTBOUND,
                )

            return_ids: list[str] = []
            if booking.planned_return_segment:
                return_ids.append(booking.planned_return_segment)
            return_ids.extend(s.id for s in self.segments.values()
                              if s.direction == Direction.RETURN)
            seen = set()
            for segment_id in return_ids:
                if not segment_id or segment_id in seen:
                    continue
                seen.add(segment_id)
                segment = self.segments[segment_id]
                return_trip = self.default_itinerary(segment, slot.end, Direction.RETURN)
                if return_trip is None:
                    continue
                report = self.evaluate_arrangement(
                    code, outbound=outbound, return_trip=return_trip,
                    slot_id=slot.id,
                )
                report.option_id = f"{code}-RESCHED-{slot.id}-{segment_id}"
                reports.append(report)

        reports.sort(key=lambda r: (
            not r.confirmable,
            sum(1 for i in r.issues if i.severity == "blocker"),
            sum(1 for i in r.issues if i.severity == "conflict"),
            sum(1 for i in r.issues if i.severity == "approval"),
        ))
        return reports

    def reconcile_statements(self, person_id: str | None = None,
                             code: str | None = None) -> list[Question]:
        """检测同一移动上的前后表述冲突，生成指向具体方向/区段的追问。"""

        people = [person_id] if person_id else list(self.people)
        created: list[Question] = []
        for pid in people:
            needs = [n for n in self.needs_for(pid) if n.status != "superseded"]
            groups: dict[tuple[str, str], list[LightingNeed]] = {}
            for need in needs:
                key = (
                    need.direction.value if isinstance(need.direction, Direction) else str(need.direction),
                    need.segment_id or need.segment_query or "",
                )
                groups.setdefault(key, []).append(need)

            for (direction_value, segment_key), group in groups.items():
                asks_light = any(
                    n.kind in {"continuous_light"} or (
                        n.kind == "steady_red" and n.modality == "requires"
                    ) or n.requires_light is True
                    for n in group
                )
                rejects_or_off = any(
                    n.kind in {"no_bright_light"} or (
                        n.kind == "light_off_commitment" and n.modality == "accepts"
                    ) or n.light_allowed is False
                    for n in group
                )
                proxy_vs_self = any(n.proxy for n in group) and any(not n.proxy for n in group)
                if not ((asks_light and rejects_or_off) or proxy_vs_self):
                    continue
                direction = Direction(direction_value) if direction_value in Direction._value2member_map_ else Direction.UNKNOWN
                segment_id = next((n.segment_id for n in group if n.segment_id), None)
                start = next((n.start for n in group if n.start is not None), None)
                end = next((n.end for n in group if n.end is not None), None)
                qid = self._next_id("Q-CONFLICT")
                question = Question(
                    id=qid,
                    message=(
                        f"{self.people[pid].name if pid in self.people else pid} "
                        "在同一移动上的光线表述不一致；保留可明确采用的条款，"
                        "并只向本人确认这一次方向/区段/时段。"
                    ),
                    person_id=pid,
                    person_name=self.people[pid].name if pid in self.people else pid,
                    booking_code=code,
                    direction=direction,
                    segment_id=segment_id,
                    start=start,
                    end=end,
                    topic="lighting_statement_conflict",
                    source_ids=[n.source_id for n in group if n.source_id],
                )
                self.add_question(question)
                created.append(question)
        return created

    # ------------------------------------------------------------------
    # 确认码改派
    # ------------------------------------------------------------------
    def create_reassignment(self, code: str, *, segment_id: str | None = None,
                            start: int | str | None = None,
                            escort_id: str | None = None,
                            reason: str = "场地变更改派",
                            outbound: Any = None) -> ConfirmationVersion:
        booking = self._booking(code)
        old = booking.current()
        if old is None:
            raise DutyError(f"{code} 没有可改派的已发确认")

        if segment_id is None and old.return_trip:
            segment_id = old.return_trip.segment_id
        segment = self.segments.get(segment_id or "")
        if segment is None:
            raise DutyError("改派必须指定存在的回程区段")

        if start is not None:
            start_minutes = parse_clock(start)
            end = start_minutes + segment.walk_minutes
            from .models import Itinerary
            return_trip = Itinerary(
                direction=Direction.RETURN,
                segment_id=segment.id,
                start=start_minutes,
                end=end,
            )
            # 若落在红光窗口，生成连续红光；调用方可随后覆盖 light_uses。
            covering = [w for w in segment.windows if w.mode == "steady_red"
                        and w.start <= start_minutes and end <= w.end]
            if covering:
                w = covering[0]
                from .models import LightUse
                return_trip.light_uses.append(LightUse(
                    start=start_minutes, end=end, mode="steady_red",
                    intensity=w.intensity or "low",
                ))
        else:
            preferred = old.return_trip.start if old.return_trip else (
                self.slots[booking.slot_id].end if booking.slot_id in self.slots else self.now
            )
            return_trip = self.default_itinerary(segment, preferred, Direction.RETURN)

        if escort_id:
            return_trip.escort_id = escort_id
        elif segment.requires_escort and not return_trip.escort_id:
            escort = self._available_escort(segment.id, return_trip.start, return_trip.end)
            if escort:
                return_trip.escort_id = escort.id

        if outbound is None:
            outbound = old.outbound
        if outbound is None and booking.slot_id in self.slots:
            slot = self.slots[booking.slot_id]
            out_segment = self.resolve_segment(
                Direction.OUTBOUND, booking.planned_outbound_segment
            )
            if out_segment:
                outbound = self.default_itinerary(
                    out_segment, slot.start - out_segment.walk_minutes,
                    Direction.OUTBOUND,
                )

        new_version_number = max(v.version for v in booking.versions) + 1
        new_version = ConfirmationVersion(
            version=new_version_number,
            status=VersionStatus.PROPOSED,
            slot_id=old.slot_id,
            member_ids=list(old.member_ids),
            seats=old.seats,
            outbound=outbound or old.outbound,
            return_trip=return_trip,
            equipment_ids=list(old.equipment_ids),
            escort_ids=[return_trip.escort_id] if return_trip.escort_id else [],
            created_at=self.now,
            change_reason=reason,
            parent_version=old.version,
            source_ids=[],
        )
        booking.versions.append(new_version)
        self.manual_queue.add(code)
        self.log("reassignment_proposed", code=code, version=new_version_number,
                 segment_id=segment_id, reason=reason)
        return new_version

    def propose_confirmation(self, code: str, **kwargs: Any) -> ConfirmationVersion:
        """从暂留席生成一版待确认安排；参数同 :meth:`create_reassignment`。"""

        return self.create_reassignment(code, **kwargs)

    def issue_proposal(self, code: str, version: int) -> PlanReport:
        booking = self._booking(code)
        proposal = next((v for v in booking.versions if v.version == version), None)
        if proposal is None or proposal.status != VersionStatus.PROPOSED:
            raise DutyError(f"{code} v{version} 不是待签发改派")
        report = self.evaluate_confirmation(code, version)
        if not report.confirmable:
            raise DutyError({
                "error": "proposal_not_confirmable",
                "issues": [i.to_dict() for i in report.issues],
            })

        old = booking.current()
        if old and old.version != version:
            old.superseded_by = version
            if old.status != VersionStatus.REDEEMED:
                old.status = VersionStatus.SUPERSEDED
        proposal.status = VersionStatus.ISSUED if booking.status != BookingStatus.REDEEMED else VersionStatus.ISSUED
        proposal.issued_at = self.now
        booking.current_version = version
        booking.hold_only = False
        if booking.status == BookingStatus.HELD:
            booking.status = BookingStatus.CONFIRMED
        if proposal.return_trip and proposal.return_trip.escort_id:
            escort = self.escorts.get(proposal.return_trip.escort_id)
            if escort:
                escort.assigned_code = code
        self.manual_queue.discard(code)
        self.log("proposal_issued", code=code, version=version)
        return report

    # ------------------------------------------------------------------
    # 闭合/中断处理
    # ------------------------------------------------------------------
    def report_closure(self, segment_id: str, start: int | str | None = None,
                       end: int | str | None = None, reason: str = "临时闭合",
                       source_id: str | None = None) -> Closure:
        segment = self.segments.get(segment_id)
        if segment is None:
            raise KeyError(f"未知区段：{segment_id}")
        closure = Closure(
            segment_id=segment_id,
            start=parse_clock(start) if start is not None else self.now,
            end=parse_clock(end) if end is not None else None,
            reason=reason,
            source_id=source_id,
        )
        segment.closures.append(closure)
        self.log("closure_reported", closure=closure)
        return closure

    def _booking_uses_segment(self, booking: Booking, segment_id: str) -> bool:
        current = booking.current()
        if current:
            for trip in (current.outbound, current.return_trip):
                if trip and trip.segment_id == segment_id:
                    if trip.start is None or not segment_active_overlap(
                        self.segments[segment_id].closures, trip.start, trip.end
                    ):
                        return True
                    return True
        return booking.planned_return_segment == segment_id or (
            booking.planned_outbound_segment == segment_id
        )

    def handle_disruptions(self, at: int | str | None = None) -> dict[str, Any]:
        if at is not None:
            self.now = parse_clock(at)
        recovery: list[dict[str, Any]] = []
        reschedule: list[dict[str, Any]] = []
        held: list[dict[str, Any]] = []

        for code, booking in sorted(self.bookings.items()):
            if booking.status == BookingStatus.CANCELLED:
                continue
            affected_segments = []
            current = booking.current()
            if current:
                for trip in (current.outbound, current.return_trip):
                    if trip and self.segments.get(trip.segment_id):
                        closures = self.segments[trip.segment_id].active_closures(
                            start=trip.start, end=trip.end
                        )
                        if closures:
                            affected_segments.append(trip.segment_id)
            if not affected_segments and booking.planned_return_segment:
                seg = self.segments.get(booking.planned_return_segment)
                if seg and seg.active_closures(at=self.now):
                    affected_segments.append(seg.id)
            if not affected_segments:
                continue

            options = [r.to_dict() for r in self.suggest_options(code)]
            entry = {
                "code": code,
                "status": booking.status.value,
                "member_ids": booking.member_ids,
                "member_names": [self.people.get(p).name if p in self.people else p
                                 for p in booking.member_ids],
                "affected_segments": sorted(set(affected_segments)),
                "options": options,
            }
            if booking.status == BookingStatus.REDEEMED:
                # 已在场内：旧承诺不能兑现，先进入人工待处理；同时给出可带回的方案。
                self.manual_queue.add(code)
                booking.manual_reason = "existing_return_commitment_unfulfillable"
                entry["manual"] = True
                entry["in_field_location"] = self.slots[booking.slot_id].station if booking.slot_id in self.slots else "观测台"
                recovery.append(entry)
            elif booking.status == BookingStatus.CONFIRMED:
                entry["may_shift_time"] = True
                entry["time_options"] = [r.to_dict() for r in self.reschedule_options(code)]
                reschedule.append(entry)
            else:
                entry["hold_only"] = True
                held.append(entry)

        return {
            "at": self.now,
            "recovery": recovery,
            "reschedule_confirmed_not_entered": reschedule,
            "held_options": held,
            "manual_queue": self.manual_queue_view(),
            "questions": [q.to_dict() for q in self.questions],
        }

    def manual_queue_view(self) -> list[dict[str, Any]]:
        result = []
        for code in sorted(self.manual_queue):
            booking = self.bookings[code]
            best = self.best_option(code)
            result.append({
                "code": code,
                "member_ids": booking.member_ids,
                "member_names": [self.people.get(p).name if p in self.people else p
                                 for p in booking.member_ids],
                "location": (self.slots[booking.slot_id].station
                             if booking.slot_id in self.slots else None),
                "reason": booking.manual_reason or "需要人工值守处理",
                "best_option": best.to_dict() if best else None,
            })
        return result

    # ------------------------------------------------------------------
    # 可用性查询
    # ------------------------------------------------------------------
    def availability(self, at: int | str | None = None) -> dict[str, Any]:
        if at is not None:
            query_at = parse_clock(at)
        else:
            query_at = self.now

        slot_reports = []
        for slot in sorted(self.slots.values(), key=lambda s: s.start):
            occupied = self.slot_occupancy(slot.id)
            status = "upcoming"
            if query_at is not None:
                if query_at >= slot.end:
                    status = "finished"
                elif query_at >= slot.start:
                    status = "in_progress"
            slot_reports.append({
                **to_plain_dict(slot),
                "occupied_seats": occupied,
                "available_seats": max(0, slot.seats - occupied),
                "status": status,
            })

        route_reports = []
        for segment in self.segments.values():
            departures = []
            blockers = []
            for window in segment.windows:
                interval = self.feasible_starts(segment, window)
                if not interval:
                    continue
                low, high = interval
                effective_low = max(low, query_at) if query_at is not None else low
                if effective_low > high:
                    blockers.append(Issue(
                        "window_departure_time_passed",
                        f"{segment.name} 本窗口最晚 {format_clock(high)} 出发",
                        severity="info", segment_id=segment.id, rule_id=window.rule_id,
                    ).to_dict())
                    continue
                closure_now = segment.active_closures(at=query_at) if query_at is not None else []
                if closure_now or segment.active_closures(start=effective_low, end=effective_low + segment.walk_minutes):
                    for closure in segment.closures:
                        blockers.append(Issue(
                            "segment_closed",
                            f"{segment.name} 闭合阻碍 {format_clock(effective_low)}-"
                            f"{format_clock(effective_low + segment.walk_minutes)} 的通行",
                            segment_id=segment.id,
                            rule_id="R-TEMP-CLOSURE",
                            source_id=closure.source_id,
                            start=closure.start, end=closure.end,
                        ).to_dict())
                occupants = self.window_occupants(window.rule_id)
                tentative = self.tentative_window_claims(window.rule_id)
                capacity_blocked = len(occupants) >= window.max_groups
                departures.append({
                    "window_rule_id": window.rule_id,
                    "mode": window.mode,
                    "window_start": window.start,
                    "window_end": window.end,
                    "departure_start": effective_low,
                    "departure_end": high,
                    "departure_start_label": format_clock(effective_low),
                    "departure_end_label": format_clock(high),
                    "max_groups": window.max_groups,
                    "occupants": sorted(occupants),
                    "tentative_claims": sorted(tentative),
                    "capacity_blocked": capacity_blocked,
                })
                if capacity_blocked:
                    blockers.append(Issue(
                        "red_window_capacity_full",
                        f"{window.rule_id} 同窗口容量已被 {', '.join(sorted(occupants))} 占满",
                        severity="warning", segment_id=segment.id,
                        rule_id="R-P4-WINDOW-CAPACITY", source_id="P4",
                        alternative={"window_rule_id": window.rule_id},
                    ).to_dict())

            route_reports.append({
                "segment": to_plain_dict(segment),
                "current_closures": [c.to_plain_dict() if hasattr(c, "to_plain_dict") else to_plain_dict(c)
                                     for c in segment.active_closures(at=query_at)],
                "departure_windows": departures,
                "blockers": blockers,
            })

        return {"at": query_at, "at_label": format_clock(query_at),
                "slots": slot_reports, "routes": route_reports,
                "rules": [r.to_plain_dict() if hasattr(r, "to_plain_dict") else to_plain_dict(r)
                           for r in self.rules.values()]}

    # ------------------------------------------------------------------
    # 入口视图、审计与问题
    # ------------------------------------------------------------------
    def entrance_view(self, code: str) -> dict[str, Any]:
        booking = self._booking(code)
        effective = None
        for version in reversed(booking.versions):
            if version.status in (VersionStatus.ISSUED, VersionStatus.REDEEMED):
                effective = version
                break
        if effective is None and booking.current():
            effective = booking.current()

        def trip_view(trip: Any):
            if not trip:
                return None
            segment = self.segments.get(trip.segment_id)
            escort = self.escorts.get(trip.escort_id or "") if trip.escort_id else None
            return {
                "direction": trip.direction.value,
                "segment_id": trip.segment_id,
                "segment_name": segment.name if segment else trip.segment_id,
                "start": trip.start,
                "end": trip.end,
                "time_label": f"{format_clock(trip.start)}-{format_clock(trip.end)}",
                "escort": {
                    "id": escort.id, "name": escort.name,
                    "confirmed": escort.assignment_confirmed if escort else False,
                } if escort else None,
                "release_lighting": [
                    {"mode": u.mode, "intensity": u.intensity,
                     "time": f"{format_clock(u.start)}-{format_clock(u.end)}"}
                    for u in trip.light_uses if u.mode != "off"
                ],
                "continuous_light": self._continuous_steady_coverage(trip)[0],
            }

        if booking.status == BookingStatus.HELD or effective is None or effective.version == 0:
            release = {
                "admit": False,
                "title": "仅暂留空位，未完成入场确认",
                "outbound": None,
                "return_trip": None,
            }
        else:
            outbound = trip_view(effective.outbound)
            return_trip = trip_view(effective.return_trip)
            release = {
                "admit": booking.status in (BookingStatus.CONFIRMED, BookingStatus.REDEEMED),
                "title": "按当前有效版本放行" if booking.status != BookingStatus.REDEEMED else "已入场；回程按当前有效版本接回",
                "outbound": outbound,
                "return_trip": return_trip,
                "escorts": [
                    {"id": eid, "name": self.escorts[eid].name if eid in self.escorts else eid}
                    for eid in effective.escort_ids
                ],
            }

        # 显式投影：不包含 needs/raw_clause/source text/private reason。
        if effective is not None and effective.version > 0:
            effective_report = self.evaluate_confirmation(code, effective.version)
            release["route_clear"] = effective_report.acceptable
            release["ready_for_release"] = effective_report.confirmable
            release["operational_alerts"] = [
                {
                    "code": issue.code,
                    "severity": issue.severity,
                    "message": issue.message,
                    "segment_id": issue.segment_id,
                    "rule_id": issue.rule_id,
                    "source_id": issue.source_id,
                }
                for issue in effective_report.issues
                if issue.severity in {"blocker", "conflict"}
                or issue.code == "missing_escort_confirmation"
            ]
        else:
            release["route_clear"] = False
            release["ready_for_release"] = False
            release["operational_alerts"] = []

        return {
            "code": code,
            "booking_status": booking.status.value,
            "effective_version": effective.version if effective else None,
            "member_count": len(booking.member_ids),
            "member_names": [self.people[p].name if p in self.people else p
                             for p in booking.member_ids],
            "station": self.slots[booking.slot_id].station if booking.slot_id in self.slots else None,
            "release": release,
            "privacy": {
                "narratives_visible": False,
                "raw_lighting_reasons_hidden": True,
                "note": "入口只显示陪行与放行操作，不显示报名者的私人叙述或理由。",
            },
        }

    def audit_code(self, code: str) -> dict[str, Any]:
        booking = self._booking(code)
        history = []
        for version in booking.versions:
            report = None
            if version.status in (VersionStatus.PROPOSED, VersionStatus.ISSUED, VersionStatus.REDEEMED):
                report = self.evaluate_confirmation(code, version.version).to_dict()
            history.append({
                "version": version.version,
                "status": version.status.value,
                "parent_version": version.parent_version,
                "superseded_by": version.superseded_by,
                "change_reason": version.change_reason,
                "source_ids": version.source_ids,
                "created_at": version.created_at,
                "outbound_segment": version.outbound.segment_id if version.outbound else None,
                "return_segment": version.return_trip.segment_id if version.return_trip else None,
                "return_escort": version.return_trip.escort_id if version.return_trip else None,
                "confirmable": report["confirmable"] if report else None,
                "issues": report["issues"] if report else [],
            })

        entrance = self.entrance_view(code)
        current_return = entrance["release"].get("return_trip") if entrance["release"] else None
        stale_warning = None
        if booking.status == BookingStatus.REDEEMED:
            report = self.evaluate_confirmation(code)
            blocked = [i for i in report.issues if i.code in {"segment_closed", "red_window_conflict", "missing_escort_confirmation"}]
            if blocked:
                stale_warning = (
                    "旧确认不能继续作为台上等候/放回依据；必须按当前有效改派或人工值守处理，"
                    "避免按旧安排把整组留在观测台。"
                )
        return {
            "code": code,
            "status": booking.status.value,
            "current_version": booking.current_version,
            "manual_queue": code in self.manual_queue,
            "stale_warning": stale_warning,
            "current_release": entrance["release"],
            "history": history,
            "events": [e for e in self.event_log if e.get("code") == code],
        }

    def verify_release(self, code: str, *, return_segment_id: str | None = None,
                       escort_id: str | None = None,
                       version: int | None = None) -> dict[str, Any]:
        """核对入口/陪行手持的旧安排是否等于当前有效安排。"""

        view = self.entrance_view(code)
        current = view["release"].get("return_trip") or {}
        mismatches = []
        if version is not None and version != view["effective_version"]:
            mismatches.append("version")
        if return_segment_id and current.get("segment_id") != return_segment_id:
            mismatches.append("return_segment")
        if escort_id and (current.get("escort") or {}).get("id") != escort_id:
            mismatches.append("escort")
        route_clear = bool(view["release"].get("route_clear"))
        if mismatches:
            warning = "手持安排已过期，不得按旧路线放人；请核对当前版本或转人工。"
        elif not route_clear:
            warning = "版本虽一致，但当前路线有闭合/资源阻碍或陪行未确认；不得按该安排放人，请转值班调度/人工。"
        else:
            warning = None
        return {
            "code": code,
            "matches_current": not mismatches,
            "safe_to_release": not mismatches and route_clear,
            "mismatches": mismatches,
            "current": current,
            "operational_alerts": view["release"].get("operational_alerts", []),
            "warning": warning,
        }

    def add_question(self, question: Question) -> Question:
        self.questions.append(question)
        return question

    def dispatcher_board(self, at: int | str | None = None) -> dict[str, Any]:
        if at is not None:
            self.now = parse_clock(at)
        disruption = self.handle_disruptions(self.now)
        return {
            "generated_at": self.now,
            "generated_at_label": format_clock(self.now),
            "availability": self.availability(self.now),
            "bookings": {
                code: {
                    "status": b.status.value,
                    "hold_only": b.hold_only,
                    "members": b.member_ids,
                    "current_version": b.current_version,
                    "evaluation": self.evaluate_confirmation(code).to_dict(),
                    "options": [o.to_dict() for o in self.suggest_options(code)],
                    "audit": self.audit_code(code),
                }
                for code, b in sorted(self.bookings.items())
            },
            "disruption": disruption,
            "manual_queue": self.manual_queue_view(),
            "questions": [q.to_dict() for q in self.questions],
        }

    def snapshot(self) -> dict[str, Any]:
        data = {key: value for key, value in self.__dict__.items() if key != "_ids"}
        return to_plain_dict(data)


def segment_active_overlap(closures, start: int, end: int) -> bool:
    return any(
        (c.start is None or c.start < end) and (c.end is None or start < c.end)
        for c in closures
    )
