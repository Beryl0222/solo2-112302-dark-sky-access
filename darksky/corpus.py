"""把仓库中的合成语料接收为可调度的值班状态。

真实使用时，值班组可以不经过语料 ID，直接调用 :class:`darksky.service.DutyService`
的 ``add_*`` / ``ingest_statement`` 方法；本模块负责把现有 ``data/corpus.json``
按记录类型装配成演示和回归测试共用的场景。
"""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

from .models import (
    Direction,
    EquipmentCondition,
    Itinerary,
    LightPolicyWindow,
    ObservationSlot,
    Question,
    Segment,
    VersionStatus,
    parse_clock,
)
from .parsing import parse_statement
from .service import DutyService


ROOT = Path(__file__).resolve().parent.parent
CORPUS_PATH = ROOT / "data" / "corpus.json"

CHINESE_NUMERALS = {
    "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}


def load_corpus_records(path: str | Path | None = None) -> list[dict[str, Any]]:
    path = Path(path) if path else CORPUS_PATH
    data = json.loads(path.read_text(encoding="utf-8"))
    return sorted(data["records"], key=lambda r: r["arrival_order"])


def _seat_count(text: str) -> int:
    match = re.search(r"(\d+|[一二两三四五六七八九十])\s*席", text)
    if not match:
        return 1
    token = match.group(1)
    return int(token) if token.isdigit() else CHINESE_NUMERALS[token]


def _add_statement(svc: DutyService, record: dict[str, Any]) -> None:
    rid = record["id"]
    text = record["text"]
    result = parse_statement(text, source_id=rid)

    for need in result.needs:
        if rid == "B1":
            need.person_id = "han"
        elif rid == "B2":
            need.person_id = "yu"
        elif rid == "B3":
            # 家长代填句与子女本人澄清必须挂到不同主体。
            if need.proxy or (need.person_name and "家长" in need.person_name):
                need.person_id = "b3_parent"
            else:
                need.person_id = "b3_child"
        svc.add_need(need)


def _add_confirmation_record(svc: DutyService, record: dict[str, Any]) -> None:
    rid = record["id"]
    text = record["text"]

    if "韩榆同意去程坡道" in text:
        svc.add_acceptance(
            "han", kind="route", code="N11", direction=Direction.OUTBOUND,
            segment_id="P1", source_id=rid,
            note="韩榆明确同意去程坡道",
        )
    if "回程原路不符合" in text:
        # 这是一条“不接受/不符合”的记录，不能当成回程同意。
        svc.add_acceptance(
            "han", kind="route", code="N11", direction=Direction.RETURN,
            segment_id="P2", source_id=rid, accepted=False,
            note="回程原路不符合本人条件",
        )
        svc.log("return_route_rejected", person_id="han", code="N11", source_id=rid)
    if "余果确认使用同一个回程陪行员" in text:
        svc.add_acceptance(
            "yu", kind="escort", code="N11", direction=Direction.RETURN,
            terms={"same_escort_as": "han", "shared_return_escort": True},
            source_id=rid, note="余果确认与韩榆共用同一名回程陪行员",
        )


def _add_observation(svc: DutyService, record: dict[str, Any]) -> None:
    text = record["text"]
    start, end = None, None
    time_match = re.search(
        r"(\d{1,2}[:：]\d{2})\s*至\s*(\d{1,2}[:：]\d{2})", text
    )
    if time_match:
        start, end = parse_clock(time_match.group(1)), parse_clock(time_match.group(2))
    station_match = re.search(r"^([一-龥A-Za-z]+?)(?=\d{1,2}[:：])", text)
    station = station_match.group(1) if station_match else "东台"
    slot = ObservationSlot(
        id=record["id"],
        station=station,
        start=start or 20 * 60 + 20,
        end=end or 20 * 60 + 40,
        seats=_seat_count(text),
        equipment=[EquipmentCondition(
            id="eyepiece_adjustable",
            label="目镜可调",
            note="报名者需确认该设备适用条件",
        )] if "目镜可调" in text else [],
    )
    svc.add_slot(slot)


def _add_path(svc: DutyService, record: dict[str, Any]) -> None:
    rid = record["id"]
    text = record["text"]
    direction = Direction.OUTBOUND if "入口到" in text else Direction.RETURN
    duration_match = re.search(r"步行\s*(\d+)\s*分钟", text)
    duration = int(duration_match.group(1)) if duration_match else 5

    if rid == "P1":
        name, aliases, origin, destination = "入口至东台坡道", ["坡道", "坡上", "斜坡"], "入口", "东台"
    elif rid == "P2":
        name, aliases, origin, destination = "东台经水亭至入口", ["水亭", "亭二"], "东台", "入口"
    else:
        name, aliases, origin, destination = "东台经南堤至入口", ["南堤", "堤边", "南堤栈道"], "东台", "入口"

    segment = Segment(
        id=rid, name=name, aliases=aliases, direction=direction,
        walk_minutes=duration, origin=origin, destination=destination,
        requires_escort="每组必有陪行" in text,
        exit_no_unescorted_transfer="出口不接受无陪行转接" in text,
    )

    dark_match = re.search(
        r"(\d{1,2}[:：]\d{2})\s*至\s*(\d{1,2}[:：]\d{2})\s*禁补光", text
    )
    if dark_match:
        segment.windows.append(LightPolicyWindow(
            rule_id=f"R-{rid}-DARK",
            start=parse_clock(dark_match.group(1)),
            end=parse_clock(dark_match.group(2)),
            mode="dark", max_groups=10_000, continuous_only=False,
        ))

    red_match = re.search(
        r"(\d{1,2}[:：]\d{2})\s*至\s*(\d{1,2}[:：]\d{2})\s*容单组常亮低强度红光", text
    )
    if red_match:
        segment.windows.append(LightPolicyWindow(
            rule_id=f"R-{rid}-RED-WINDOW",
            start=parse_clock(red_match.group(1)),
            end=parse_clock(red_match.group(2)),
            mode="steady_red", max_groups=1, intensity="low",
            continuous_only=True,
        ))
    svc.add_segment(segment)


def _add_entry_or_hold(svc: DutyService, record: dict[str, Any]) -> None:
    rid = record["id"]
    text = record["text"]
    code_match = re.search(r"码\s*([A-Za-z]+\d+)", text)
    if not code_match:
        return
    code = code_match.group(1)
    members = []
    if "韩榆" in text:
        members.append("han")
    if "余果" in text:
        members.append("yu")
    if "B3" in text and "b3_child" not in members:
        members.append("b3_child")

    if rid == "I1" or "已在" in text and "核销" in text:
        redeemed_match = re.search(r"已在\s*(\d{1,2}[:：]\d{2})\s*核销", text)
        redeemed_at = redeemed_match.group(1) if redeemed_match else None
        slot = svc.slots["T1"]

        outbound = svc.default_itinerary(svc.segments["P1"], slot.start - 5, Direction.OUTBOUND)
        return_trip = svc.default_itinerary(svc.segments["P2"], slot.end, Direction.RETURN)
        # 历史安排明确写了陪行员亭二。
        if return_trip:
            return_trip.escort_id = "ting2"
        svc.add_escort(__import__("darksky.models", fromlist=["Escort"]).Escort(
            id="ting2", name="亭二", segment_id="P2",
            start=20 * 60 + 40, end=20 * 60 + 50, capacity=1,
            self_reported_available=True, assignment_confirmed=True,
            assigned_code=code, source_id="I1",
        ))
        svc.admit_code(
            code, slot_id="T1", member_ids=members, seats=len(members),
            redeemed_at=redeemed_at, outbound=outbound, return_trip=return_trip,
            equipment_ids=["eyepiece_adjustable"], source_ids=[rid, "F1", "T1", "P1", "P2"],
        )
        # 历史 v1 确实是已核销状态；default_itinerary 未记录来源，无需额外处理。
        return

    if "预留" in record["kind"] or "仅留" in text or "尚未点验" in text:
        svc.hold_seat(
            code, slot_id="T1", member_ids=members or ["b3_child"], seats=1,
            planned_return_segment="P3" if "南堤" in text else None,
            source_ids=[rid, "B3", "T1", "P3"],
        )


def _add_change(svc: DutyService, record: dict[str, Any]) -> None:
    text = record["text"]
    time_match = re.search(r"(\d{1,2}[:：]\d{2})\s*起", text)
    at = parse_clock(time_match.group(1)) if time_match else svc.now
    if "水亭" in text:
        svc.report_closure(
            "P2", start=at, reason="水亭回入口一段临时关闭", source_id=record["id"]
        )
    if "亭二无法调往南堤" in text and "ting2" in svc.escorts:
        svc.escorts["ting2"].can_reassign_to = []
        svc.escorts["ting2"].restriction_source_id = record["id"]
        svc.log("escort_reassignment_blocked", escort_id="ting2", segment_id="P3",
                source_id=record["id"])


def _add_personnel(svc: DutyService, record: dict[str, Any]) -> None:
    text = record["text"]
    if "南一" in text and "南堤" in text:
        time_match = re.search(
            r"(\d{1,2}[:：]\d{2})\s*到\s*(\d{1,2}[:：]\d{2})", text
        )
        start = parse_clock(time_match.group(1)) if time_match else 20 * 60 + 38
        end = parse_clock(time_match.group(2)) if time_match else 20 * 60 + 52
        from .models import Escort
        svc.add_escort(Escort(
            id="nan1", name="南一", segment_id="P3", start=start, end=end,
            capacity=1, self_reported_available=True,
            assignment_confirmed=False, source_id=record["id"],
        ))


def _add_questions(svc: DutyService) -> None:
    p3 = svc.segments.get("P3")
    svc.add_question(Question(
        id="Q-N11-HAN-RETURN-P3",
        booking_code="N11", person_id="han", person_name="韩榆",
        direction=Direction.RETURN, segment_id="P3",
        start=20 * 60 + 40, end=20 * 60 + 48,
        topic="missing_route_acceptance",
        message=(
            "只问韩榆这一次回程移动：水亭旧约已关闭，是否接受改走南堤 "
            "20:40-20:48 连续低强度红光，并由南一陪行？其去程坡道同意仍然有效。"
        ),
        options=["接受南堤回程改派", "不接受，转人工值守"],
        source_ids=["F1", "C1", "L1"],
    ))
    svc.add_question(Question(
        id="Q-N12-B3-CHILD-RETURN-P3",
        booking_code="N12", person_id="b3_child", person_name="B3所报子女",
        direction=Direction.RETURN, segment_id="P3",
        start=20 * 60 + 40, end=20 * 60 + 48,
        topic="child_self_confirmation",
        message=(
            "不要按家长那句“我们不要亮光”整句询问。只需问孩子本人："
            "是否参加，并是否接受这一次南堤回程的固定低强度红光（不闪烁）和陪行？"
        ),
        options=["参加并接受固定红光", "只接受无闪烁/再改时段", "取消暂留席位"],
        source_ids=["B3", "I2", "P3"],
    ))
    svc.add_question(Question(
        id="Q-N11-YU-ROUTE-EQUIPMENT",
        booking_code="N11", person_id="yu", person_name="余果",
        direction=Direction.RETURN, segment_id="P3",
        start=20 * 60 + 40, end=20 * 60 + 48,
        topic="missing_self_confirmation",
        message=(
            "余果已接受必要时常亮红光、拒绝闪烁，并确认共用回程陪行；"
            "但仍需本人确认具体回程时刻/路线与目镜可调设备条件。"
        ),
        source_ids=["B2", "F1", "T1"],
    ))


def ingest_record(svc: DutyService, record: dict[str, Any]) -> None:
    """接收并处理一条与 ``data/corpus.json`` 同形的报名/通告记录。"""

    kind = record["kind"]
    if kind == "报名":
        _add_statement(svc, record)
    elif kind == "本人确认":
        _add_confirmation_record(svc, record)
    elif kind == "观测":
        _add_observation(svc, record)
    elif kind == "路径":
        _add_path(svc, record)
    elif kind == "规则":
        svc.log("rule_record_ingested", source_id=record["id"], text=record["text"])
    elif kind == "入场":
        _add_entry_or_hold(svc, record)
    elif kind == "预留":
        _add_entry_or_hold(svc, record)
    elif kind == "变更":
        _add_change(svc, record)
    elif kind == "人员":
        _add_personnel(svc, record)
    else:
        svc.log("unknown_record_kind", record=record)


def build_corpus_service(records: list[dict[str, Any]] | None = None,
                         *, now: int | str | None = "20:31") -> DutyService:
    """装配语料中的完整演练场景。默认查询时刻为闭合生效后的 20:31。"""

    records = records if records is not None else load_corpus_records()
    svc = DutyService(now=now)

    svc.add_person("han", "韩榆", aliases=["韩榆"], group_id="N11")
    svc.add_person("yu", "余果", aliases=["余果", "同行的余果"], group_id="N11")
    svc.add_person("b3_parent", "B3家长", aliases=["家长代填", "家长"], role="proxy", group_id="B3")
    svc.add_person("b3_child", "B3子女", aliases=["子女", "孩子", "B3本人"], group_id="B3")

    # 暗夜基础规则。
    svc.add_rule("R-OBS-ABSOLUTE-DARK", "看星时段绝对禁光", "absolute_dark",
                 segment_id="T1", start=20 * 60 + 20, end=20 * 60 + 40)
    svc.add_rule("R-P4-CONTINUITY", "重复点亮不可充作连续照明", "continuity", source_id="P4")
    svc.add_rule("R-P4-WINDOW-CAPACITY", "两队占同一红光窗口按资源冲突处理",
                 "window_capacity", source_id="P4",
                 params={"same_window_groups": 1})
    svc.add_rule("R-EXIT-ESCORT", "出口不接受无陪行转接", "escort_required", source_id="L1")

    for record in records:
        ingest_record(svc, record)

    _add_questions(svc)
    return svc
