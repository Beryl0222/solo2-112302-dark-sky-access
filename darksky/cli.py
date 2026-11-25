"""命令行入口：解析一句话、查看语料演练、入口码和审计链。"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .corpus import build_corpus_service
from .models import format_clock, to_plain_dict
from .parsing import parse_statement
from .service import DutyService


def _print_json(value: Any) -> None:
    print(json.dumps(to_plain_dict(value), ensure_ascii=False, indent=2, sort_keys=False))


def _issue_line(issue: Any) -> str:
    d = issue.to_dict() if hasattr(issue, "to_dict") else issue
    location = []
    if d.get("segment_id"):
        location.append(f"区段={d['segment_id']}")
    if d.get("rule_id"):
        location.append(f"规则={d['rule_id']}")
    if d.get("source_id"):
        location.append(f"通告/来源={d['source_id']}")
    if d.get("acceptor_id"):
        location.append(f"待确认={d['acceptor_type']}:{d['acceptor_id']}")
    suffix = f"（{'，'.join(location)}）" if location else ""
    return f"  - [{d['severity']}/{d['code']}] {d['message']}{suffix}"


def render_demo(*, simulate_confirmations: bool = True) -> str:
    svc = build_corpus_service(now="20:31")
    lines: list[str] = []
    lines.append("暗夜值班演练（查询时刻 20:31）")
    lines.append("=" * 72)

    avail = svc.availability("20:31")
    lines.append("\n1. 观测台空位")
    for slot in avail["slots"]:
        lines.append(
            f"- {slot['id']} {slot['station']} "
            f"{format_clock(slot['start'])}-{format_clock(slot['end'])}："
            f"总 {slot['seats']} 席，已占 {slot['occupied_seats']} 席，"
            f"当前可再排 {slot['available_seats']} 席，状态 {slot['status']}"
        )

    lines.append("\n2. 路线可用时段与阻碍")
    for route in avail["routes"]:
        seg = route["segment"]
        lines.append(f"- {seg['id']} {seg['name']}，步行 {seg['walk_minutes']} 分钟")
        if not route["departure_windows"]:
            lines.append("  无登记灯光窗口")
        for dep in route["departure_windows"]:
            lines.append(
                f"  可出发区间 {dep['departure_start_label']}-{dep['departure_end_label']}，"
                f"模式={dep['mode']}，同窗口上限={dep['max_groups']}，"
                f"已占={dep['occupants'] or '无'}"
            )
        for blocker in route["blockers"]:
            lines.append(_issue_line(blocker))

    lines.append("\n3. 报名表述解析（不会整句判成要灯/不要灯）")
    for need in svc.needs:
        who = need.person_id or need.person_name or "未指明"
        seg = need.segment_id or need.segment_query or "区段未指名"
        lines.append(
            f"- {need.source_id} {who}: {need.direction.value}/{seg}/"
            f"{need.kind}/{need.modality}；原句：{need.raw_clause}"
        )

    lines.append("\n4. 经办人应询问的具体移动")
    for q in svc.questions:
        lines.append(
            f"- [{q.id}] {q.person_name} {q.direction.value if q.direction else ''} "
            f"{q.segment_id or ''} "
            f"{format_clock(q.start) if q.start is not None else ''}-"
            f"{format_clock(q.end) if q.end is not None else ''}"
        )
        lines.append(f"  {q.message}")

    lines.append("\n5. 闭合后的 N11（韩榆、余果，已在场内）")
    report = svc.handle_disruptions("20:31")
    for entry in report["recovery"]:
        lines.append(
            f"- {entry['code']} {('、'.join(entry['member_names']))} 位于"
            f"{entry['in_field_location']}；受影响区段：{','.join(entry['affected_segments'])}"
        )
        best = svc.best_option(entry["code"])
        if best:
            trip = best.return_trip
            lines.append(
                f"  首选带回路线：{trip.segment_id} "
                f"{format_clock(trip.start)}-{format_clock(trip.end)}，"
                f"陪行={trip.escort_id or '未安排'}，"
                f"规则可行={best.acceptable}，可签发={best.confirmable}"
            )
            for issue in best.issues:
                lines.append(_issue_line(issue))

    lines.append("\n人工值守队列：" + (
        "、".join(f"{x['code']}({'/'.join(x['member_names'])})" for x in svc.manual_queue_view())
        or "无"
    ))

    lines.append("\n6. 入口志愿者看到的 N11（初始已核销版本；无私人事由）")
    entrance = svc.entrance_view("N11")
    lines.append(json.dumps(entrance, ensure_ascii=False, indent=2))

    lines.append("\n7. N12 暂留席")
    n12 = svc.entrance_view("N12")
    lines.append(f"- {n12['code']}: {n12['release']['title']}")
    best12 = svc.best_option("N12")
    if best12:
        lines.append(f"  首选候选 {best12.option_id}，可签发={best12.confirmable}")
        for issue in best12.issues:
            lines.append(_issue_line(issue))

    if simulate_confirmations:
        lines.append("\n8. 收到确认码改派所需回复后的演练")
        proposal = svc.create_reassignment(
            "N11", segment_id="P3", start="20:40", escort_id="nan1",
            reason="C1 后改走南堤；亭二不能调往南堤，改由南一陪行",
        )
        lines.append(f"- 已生成待签发 v{proposal.version}: P3/南一")
        svc.accept_plan("N11", "han", proposal.version, source_id="现场收到韩榆本人确认")
        svc.accept_plan("N11", "yu", proposal.version, source_id="现场收到余果本人确认")
        svc.confirm_escort("nan1", "N11", proposal.version)
        final_report = svc.evaluate_confirmation("N11", proposal.version)
        if not final_report.confirmable:
            lines.append("  仍不能签发：")
            for issue in final_report.issues:
                lines.append(_issue_line(issue))
        else:
            svc.issue_proposal("N11", proposal.version)
            lines.append(f"- v{proposal.version} 条件齐全并签发；人工队列移除 N11")

        current = svc.entrance_view("N11")
        lines.append(json.dumps(current, ensure_ascii=False, indent=2))
        stale = svc.verify_release(
            "N11", return_segment_id="P2", escort_id="ting2", version=1
        )
        lines.append("\n旧水亭/亭二安排核对：")
        lines.append(json.dumps(stale, ensure_ascii=False, indent=2))
        lines.append(f"当前人工队列：{sorted(svc.manual_queue) or '无'}")

    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="暗夜观测值班调度工具")
    sub = parser.add_subparsers(dest="command")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--now", default="20:31", help="查询时刻，如 20:31")
    common.add_argument("--json", action="store_true", help="以 JSON 输出")

    sub.add_parser("demo", parents=[common], help="输出语料场景的中文值班演练")
    sub.add_parser("board", parents=[common], help="输出调度员看板 JSON")
    sub.add_parser("questions", parents=[common], help="输出需要经办人追问的具体移动")

    parse_p = sub.add_parser("parse", help="解析一句中文/英文照明表述")
    parse_p.add_argument("text")

    code_p = sub.add_parser("code", parents=[common], help="入口确认码隐私视图")
    code_p.add_argument("code")

    audit_p = sub.add_parser("audit", parents=[common], help="核对确认码版本链")
    audit_p.add_argument("code")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command in (None, "demo"):
        print(render_demo())
        return 0

    svc: DutyService = build_corpus_service(now=getattr(args, "now", "20:31"))

    if args.command == "parse":
        result = parse_statement(args.text)
        _print_json(result)
        return 0
    if args.command == "board":
        _print_json(svc.dispatcher_board(args.now))
        return 0
    if args.command == "questions":
        _print_json(svc.questions)
        return 0
    if args.command == "code":
        view = svc.entrance_view(args.code)
        if args.json:
            _print_json(view)
        else:
            print(json.dumps(view, ensure_ascii=False, indent=2))
        return 0
    if args.command == "audit":
        audit = svc.audit_code(args.code)
        if args.json:
            _print_json(audit)
        else:
            print(json.dumps(audit, ensure_ascii=False, indent=2))
        return 0

    parser.error(f"未知命令：{args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
