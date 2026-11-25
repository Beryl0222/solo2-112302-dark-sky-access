"""中文/短英文照明表述解析。

设计原则：**拆分条款并保留方向/区段/范围**，不把整句压缩成“要灯/不要灯”。
解析器只产出可审计的语义框架；是否接受安排由 :mod:`darksky.service` 裁决。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any

from .models import Direction, LightingNeed, clocks_between, to_plain_dict


RETURN_WORDS = (
    "回来", "回程", "返程", "回堤", "回入口", "返回", "归途", "出口",
    "return path", "return route", "way back", "back to", "exit",
)
OUTBOUND_WORDS = (
    "去程", "进去", "进亭", "入场", "入口到", "上去", "去东台", "前往",
    "outbound", "way in", "enter", "entry",
)
ROUNDTRIP_WORDS = ("往返", "来回", "both ways", "round trip", "both paths")
OBSERVATION_WORDS = (
    "看星", "观星", "观测时段", "观测期间", "整个观测", "望远镜",
    "observing", "observation", "observation session", "at the eyepiece", "stargazing",
)

SEGMENT_PATTERNS: list[tuple[str, tuple[str, ...]]] = [
    ("P1", ("坡道", "坡上", "斜坡", "slope", "ramp")),
    ("P2", ("水亭", "亭二", "water pavilion", "pavilion")),
    ("P3", ("南堤", "堤边", "过堤", "堤", "south causeway", "causeway", "dike")),
]

TURN_RE = re.compile(
    r"(?:^|[\s。；;！？!?“\"'])"
    r"([一-龥A-Za-z][一-龥A-Za-z·.\-' ]{0,14}?)\s*[：:]"
)
CLAUSE_SPLIT_RE = re.compile(r"[，,。；;！？!?\n]+|但(?:是)?|but\b", re.IGNORECASE)
EXCEPT_EN_RE = re.compile(
    r"^(.*?)\bexcept\s+(?:on\s+)?(?:the\s+)?([A-Za-z ][A-Za-z ]*?)"
    r"(?:[.,;]|$)",
    re.IGNORECASE,
)
EXCEPT_ZH_RE = re.compile(r"除了?([^，,。；;！？!?]+?)(?:以外|之外)?[，,]?")


@dataclass
class ParseResult:
    text: str
    needs: list[LightingNeed] = field(default_factory=list)
    speaker_name: str | None = None
    proxy: bool = False
    private_to_entrance: bool = False

    def to_dict(self) -> dict[str, Any]:
        return to_plain_dict(self)


def _contains_any(text: str, words: tuple[str, ...] | list[str]) -> bool:
    low = text.lower()
    return any(word.lower() in low for word in words)


def detect_direction(clause: str) -> Direction:
    if _contains_any(clause, ROUNDTRIP_WORDS):
        return Direction.ROUNDTRIP
    if _contains_any(clause, RETURN_WORDS):
        return Direction.RETURN
    if _contains_any(clause, OUTBOUND_WORDS):
        return Direction.OUTBOUND
    if _contains_any(clause, OBSERVATION_WORDS):
        return Direction.AT_STATION
    return Direction.UNKNOWN


def detect_segment(clause: str) -> tuple[str | None, str | None]:
    """返回 ``(暂定区段 ID, 原文线索)``。

    这里给出的 P1/P2/P3 只是按语料别名的提示；服务层还可按 direction 和注册表校正。
    “进亭”等方向词不会被孤立地当成返程水亭。
    """

    low = clause.lower()
    for segment_id, aliases in SEGMENT_PATTERNS:
        for alias in aliases:
            if alias.lower() in low:
                return segment_id, alias
    return None, None


def _is_proxy_speaker(speaker: str | None, body: str) -> bool:
    if speaker and _contains_any(speaker, ("家长", "代填", "家人", "工作人员代")):
        return True
    # “我们不要亮光”是集体表述；在没有本人确认时只能作为低置信线索。
    return body.strip().startswith("我们") and "我" not in body.strip()[2:4]


def split_turns(text: str) -> list[tuple[str | None, str, bool]]:
    """把“家长代填：……。子女随后写：……”拆成不同发言主体。"""

    matches = list(TURN_RE.finditer(text))
    if not matches:
        return [(None, text.strip(), False)]

    turns: list[tuple[str | None, str, bool]] = []
    for index, match in enumerate(matches):
        speaker = match.group(1).strip(" “\"'")
        body_start = match.end()
        body_end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[body_start:body_end].strip(" 。；;")
        # 下一个 match 的前导分隔符可能被吃进 body 边界；上面使用 start 已包含它。
        if body:
            turns.append((speaker, body, _is_proxy_speaker(speaker, body)))
    return turns or [(None, text.strip(), False)]


def split_clauses(body: str) -> list[str]:
    return [part.strip(" 　") for part in CLAUSE_SPLIT_RE.split(body) if part and part.strip()]


def _base_need(**kwargs: Any) -> LightingNeed:
    return LightingNeed(**kwargs)


def _negative(clause: str) -> bool:
    return bool(re.search(
        r"不接受|不要|不能|不会|不可|拒绝|请勿|禁止|\bno\b|\bnot\b|without",
        clause,
        flags=re.IGNORECASE,
    ))


def parse_clause(
    clause: str,
    *,
    source_id: str | None,
    speaker_name: str | None,
    proxy: bool,
    private: bool,
    default_direction: Direction = Direction.UNKNOWN,
) -> list[LightingNeed]:
    """解析一个逗号/转折子句。一个子句仍可能产出多条兼容框架。"""

    results: list[LightingNeed] = []
    direction = detect_direction(clause)
    if direction == Direction.UNKNOWN:
        direction = default_direction
    segment_id, segment_query = detect_segment(clause)
    start, end = clocks_between(clause)
    common = dict(
        source_id=source_id,
        person_name=speaker_name,
        raw_clause=clause,
        direction=direction,
        segment_id=segment_id,
        segment_query=segment_query,
        start=start,
        end=end,
        proxy=proxy,
        self_reported=not proxy,
        private_to_entrance=private,
    )

    # “不是不要固定红灯”是对前面宽泛“不要亮光”的澄清，不能再次当成拒灯。
    double_negation_red = bool(re.search(
        r"不是\s*不要\s*(?:固定|恒定)?\s*红灯?|"
        r"not\s+(?:saying\s+)?(?:we\s+)?(?:do not|don't)\s+want\s+fixed\s+red",
        clause,
        flags=re.IGNORECASE,
    ))
    if double_negation_red or re.search(
        r"固定红灯|恒定红光|常亮(?:低强度)?红光?|持续红光|"
        r"全程[^，,。；;]{0,4}红灯|红灯[^，,。；;]{0,4}全程|"
        r"solid red|steady red|constant red", clause, re.IGNORECASE
    ):
        conditional_ok = bool(re.search(r"fine|okay|ok\b|if needed|可以|可接受", clause, re.I))
        explicit_required = bool(re.search(
            r"须|必须|一定|得有|需要|"
            r"\bmust\b|\brequire[sd]?\b|\bneed to\b",
            clause, re.I,
        )) and not conditional_ok
        results.append(_base_need(
            **common,
            kind="steady_red",
            modality="accepts" if conditional_ok or not explicit_required else "requires",
            color="red",
            intensity="low" if re.search(r"低强度|dim|low", clause, re.I) else None,
            continuous=bool(re.search(r"一路|全程|连续|持续|whole way|all the way|continuous|constant", clause, re.I)),
            light_allowed=True,
            tags=["clarifies_broad_no_light"] if double_negation_red else [],
            confidence="high",
        ))

    if re.search(r"一路.*(?:红|灯)|(?:红|灯).*一路|全程.*(?:照|亮|红|灯)|all the way.*(?:red|light)", clause, re.I):
        if not any(n.kind == "steady_red" for n in results):
            results.append(_base_need(
                **common,
                kind="continuous_light",
                modality="requires",
                color="red" if "红" in clause or re.search(r"\bred\b", clause, re.I) else None,
                continuous=True,
                requires_light=True,
                light_allowed=True,
            ))
        else:
            for need in results:
                if need.kind == "steady_red":
                    need.continuous = True
                    need.modality = "requires" if _contains_any(clause, ("须", "必须", "得")) else need.modality

    if re.search(r"避闪|避免闪烁|防闪|不要闪|不接受闪|怕闪|no\s+flash|avoid\s+flash|flashing|strobe|blinking", clause, re.I):
        results.append(_base_need(
            **common,
            kind="avoid_flashing",
            modality="rejects",
            flashing_allowed=False,
            # “走路避闪”首先是步行移动条件，不能扩大成观测台绝对禁光。
            activity_scope="movement",
        ))

    if re.search(r"断续|间歇|反复点亮|几次瞬时|intermittent|repeated\s+light|pulses?", clause, re.I):
        results.append(_base_need(
            **common,
            kind="reject_intermittent_light",
            modality="rejects",
            intermittent_allowed=False,
        ))

    if re.search(r"随时可叫助手|可叫助手|叫助手|助手.{0,8}(?:随时|可叫|待命)|"
                 r"on[- ]call\s+assistant|assistant.*(?:call|available|on call)",
                 clause, re.I):
        results.append(_base_need(
            **common,
            kind="alternative_resource",
            modality="offers",
            alternative="on_call_assistant",
            # 关键业务护栏：叫助手不等于同意把常亮取消。
            consent_to_remove_continuous_light=False,
        ))

    if re.search(r"有人陪|陪行|陪同|陪.{0,6}走|companion|escort|buddy", clause, re.I):
        results.append(_base_need(
            **common,
            kind="alternative_resource",
            modality="accepts" if not _negative(clause) else "requires",
            alternative="escort_or_companion",
        ))

    if re.search(r"关灯|关闭光源|关掉.{0,4}灯|灯.{0,3}关掉|把.{0,5}灯关|可以熄|light\s*off|turn\s+off.{0,12}light|lights?\s+off", clause, re.I):
        accepts = bool(re.search(r"可以|可|能|okay|fine|can|may", clause, re.I)) and not _negative(clause)
        results.append(_base_need(
            **common,
            kind="light_off_commitment",
            modality="accepts" if accepts else ("rejects" if _negative(clause) else "statement"),
            light_allowed=False,
            activity_scope="commitment",
        ))

    # 宽泛“不要亮光/我们不要亮光”。双重否定句已生成澄清框架，不再生成绝对禁光。
    if not double_negation_red and re.search(
        r"不要亮光|不要光|不要亮|不需.{0,4}光|no\s+bright\s+lights?|\bno\s+lights?\b",
        clause,
        re.I,
    ):
        results.append(_base_need(
            **common,
            kind="no_bright_light",
            modality="rejects",
            intensity="bright",
            light_allowed=None,  # 不等于拒绝低强度红光
            confidence="low" if proxy else "medium",
            status="clarify" if proxy else "active",
            tags=(["collective_proxy", "needs_person_scope"] if proxy else []),
        ))

    if not results:
        results.append(_base_need(
            **common,
            kind="unparsed_statement",
            modality="uncertain",
            confidence="low",
        ))
    return results


def _apply_english_exception(result: ParseResult, text: str, private: bool) -> None:
    match = EXCEPT_EN_RE.search(text.strip())
    if not match:
        return
    main_text = match.group(1).strip(" ,")
    exception_text = match.group(2).strip()
    exception_direction = detect_direction(exception_text) or Direction.RETURN

    # 用删掉 except 短语的主句重新解析，避免例外范围被放大到观测时段。
    result.needs = []
    if main_text:
        for need in parse_clause(
            main_text,
            source_id=None,
            speaker_name=result.speaker_name,
            proxy=result.proxy,
            private=private,
        ):
            need.except_directions = [exception_direction]
            need.tags.append("has_return_exception")
            result.needs.append(need)

    result.needs.append(LightingNeed(
        person_name=result.speaker_name,
        raw_clause=exception_text,
        direction=exception_direction,
        kind="scope_exception",
        modality="excludes_from_light_off_commitment",
        except_directions=[exception_direction],
        activity_scope="movement",
        private_to_entrance=private,
        tags=["exception_modifies_light_off_only"],
    ))


def parse_statement(
    text: str,
    *,
    source_id: str | None = None,
    default_person_id: str | None = None,
    default_person_name: str | None = None,
) -> ParseResult:
    """解析一条报名/确认文本。

    返回的 :class:`ParseResult` 可能含多位发言者；人员 ID 可在入库时再解析。
    """

    private = bool(re.search(
        r"原话不用转给入口|不要转给入口|勿转入口|入口不能看|do not (?:show|forward).{0,30}entrance|private",
        text,
        re.I,
    ))
    result = ParseResult(text=text, private_to_entrance=private)

    # 英文 except 结构通常没有中文冒号发言标签。
    if re.search(r"\bexcept\b", text, re.I) and not re.search(r"[一-龥]", text):
        result.speaker_name = default_person_name
        _apply_english_exception(result, text, private)
        for need in result.needs:
            need.source_id = source_id
            if default_person_id:
                need.person_id = default_person_id
        return result

    for speaker, body, proxy in split_turns(text):
        speaker_name = default_person_name
        if speaker:
            # “同行的余果”取末尾 2-4 个中文/英文姓名 token；无法确定时保留原标签。
            name_match = re.search(r"([一-龥]{2,4}|[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)$", speaker.strip())
            speaker_name = name_match.group(1) if name_match else speaker
        result.speaker_name = speaker_name
        result.proxy = proxy

        # 中文“除了回程……都可以关灯”：给相关关灯框架打上例外方向。
        zh_exception = EXCEPT_ZH_RE.search(body)
        exception_direction = None
        if zh_exception:
            exception_direction = detect_direction(zh_exception.group(1))

        for clause in split_clauses(body):
            needs = parse_clause(
                clause,
                source_id=source_id,
                speaker_name=speaker_name,
                proxy=proxy,
                private=private,
            )
            for need in needs:
                if default_person_id and not need.proxy:
                    need.person_id = default_person_id
                if exception_direction:
                    if need.kind == "light_off_commitment":
                        need.except_directions = [exception_direction]
                        need.tags.append("has_exception")
                    elif need.direction == Direction.UNKNOWN:
                        need.direction = exception_direction
                result.needs.append(need)

        if zh_exception:
            result.needs.append(LightingNeed(
                source_id=source_id,
                person_name=speaker_name,
                raw_clause=zh_exception.group(0),
                direction=exception_direction or Direction.UNKNOWN,
                kind="scope_exception",
                modality="excludes_from_light_off_commitment",
                except_directions=[exception_direction] if exception_direction else [],
                proxy=proxy,
                private_to_entrance=private,
                tags=["exception_modifies_light_off_only"],
            ))

    if default_person_id:
        for need in result.needs:
            # 代理发言不能被错误挂到默认本人；语料构建层会给家长/子女分别分配 ID。
            if need.person_id is None and not need.proxy:
                need.person_id = default_person_id
    return result
