# 暗夜观测值班调度项目

合成资料放在 `data/corpus.json`，记录类型载于 `data/context.json`。每条记录有独立的 `id`、业务类别 `kind` 及原始文字 `text`。`arrival_order` 表示收件先后，文字内注明的业务发生时间可以早于或晚于收件顺序。`references` 只列来源中已明示的记录关联，空值表示未附明示关联。

本仓库在原始资料读取脚本之外，新增了一个仅依赖 Python 3.11 标准库的 `darksky` 包，用于：

- 拆分中文/短英文报名中的照明表述，按**人员、进出方向、区段、适用时段、替代措施**建框架；
- 区分看星绝对禁光、低强度红光限量窗口、连续步行照明、无闪烁等规则；
- 暂留观测台席位，校验本人去程/回程/设备条件接受及陪行资源；
- 查询当前可用时段、阻碍区段、适用规则和待确认人；
- 处理栈道闭合后的已确认未入场组、已入场组、人工值守队列；
- 用确认码给入口志愿者只展示陪行与放行安排，隐藏私人照明叙述；
- 保存确认码版本链，核对旧安排与当前有效改派。

## 运行测试

```bash
python3 -m unittest discover -s tests -v
```

## 命令行

```bash
# 完整中文演练：解析、可用性、闭合、人工队列、入口隐私视图、改派和审计
python3 -m darksky demo

# 调度员看板 JSON
python3 -m darksky board --now 20:31 --json

# 解析单句
python3 -m darksky parse "回堤边得有红灯一路照着，但不接受断续开灯"
python3 -m darksky parse "I will keep my light off except on the return path."

# 入口确认码视图（不含私人理由/原始叙述）
python3 -m darksky code N11 --json
python3 -m darksky code N12

# 确认码版本链和旧安排核对
python3 -m darksky audit N11 --json

# 也可从仓库根目录运行
python3 main.py demo
```

## Python API 速览

```python
from darksky import build_corpus_service, parse_statement

# 只看解析框架
frames = parse_statement(
    "韩榆：去程坡道我可以关灯，回南堤须一路有红灯照着。"
)
for need in frames.needs:
    print(need.person_name, need.direction, need.segment_id,
          need.kind, need.modality, need.continuous)

# 装配 data/corpus.json 的完整演练场景
svc = build_corpus_service(now="20:31")

# 调度员查询
availability = svc.availability("20:31")
disruption = svc.handle_disruptions("20:31")
best_n11 = svc.best_option("N11")
print(best_n11.acceptable, best_n11.confirmable)
print(best_n11.required_acceptors)

# 生成 N11 -> P3/南一 的待签发版本；版本链保留旧 P2/亭二安排
proposal = svc.create_reassignment(
    "N11",
    segment_id="P3",
    start="20:40",
    escort_id="nan1",
    reason="C1 关闭水亭；亭二不能调往南堤",
)

# 必须由本人/陪行员分别确认；不能用家长代填、助手可叫或旧报名句替代
svc.accept_plan("N11", "han", proposal.version)
svc.accept_plan("N11", "yu", proposal.version)
svc.confirm_escort("nan1", "N11", proposal.version)
svc.issue_proposal("N11", proposal.version)

# 入口只拿到操作信息
print(svc.entrance_view("N11"))

# 核对手持旧码/旧派单是否过期
print(svc.verify_release(
    "N11", return_segment_id="P2", escort_id="ting2", version=1
))
print(svc.audit_code("N11"))
```

也可以用 `DutyService.add_person / add_slot / add_segment / add_escort /
hold_seat / ingest_statement / report_closure` 接收新的值班资料；同形语料记录可传给
`darksky.ingest_record`。

## 语料场景的业务裁决

以闭合生效后的 `20:31` 为查询时刻：

1. **T1 东台 20:20–20:40 共四席**：N11 占两席（韩榆、余果，已入场），N12 暂留一席，因此还剩一席。暂留席保留容量，但不构成入场确认。
2. **P1 坡道**：20:10–21:15 绝对禁补光；韩榆本人已确认去程坡道。
3. **P2 水亭**：本来 20:40–20:47 可全程使用低强度常亮红光，但 C1 通知 20:31 起闭合，旧安排 P2/亭二不能继续作为回程放行依据。
4. **P3 南堤**：20:40–20:48 可落在 20:38–20:50 的单组低强度常亮红光窗口内，南一 20:38–20:52 可带一组，是 N11 当前可行的带回路线。
5. N11 已在场内，旧承诺无法按原样兑现，因此先进入人工值守队列并显示韩榆、余果；系统同时给出 P3/南一方案。签发新版前仍需：
   - 韩榆本人确认南堤回程改派及设备条件；
   - 余果本人确认具体路线/时刻与设备条件（其常亮红光、无闪烁和共用陪行的既有表述可复用，但不能扩大成所有条件均已接受）；
   - 南一本人确认负责 N11 这一趟陪行。
6. **N12/B3**：家长的“我们不要亮光”只保留为代填、低置信的“不要亮光”线索；子女本人澄清可明确采用的是“步行避免闪烁、不拒绝固定红光”。经办人应问的是 **N12 南堤回程 20:40–20:48 这一次移动**以及是否参加，而不是重问整句“要不要灯”。在本人确认前，N12 只是一席暂留。
7. N11 若签发 P3 版本，会占用 P3 的同一红光窗口和南一资源；N12 的南堤候选随后会得到 `red_window_conflict`（规则 `R-P4-WINDOW-CAPACITY`），不能把两组排进同一限量窗口。
8. 入口视图只显示当前版本的区段、时刻、陪行和操作灯光，不包含 B1/B2/B3 的原始叙述或私人理由。审计接口保留 v1(P2/亭二) → v2(P3/南一) 的版本链；`verify_release()` 可识别旧派单，避免按旧安排把人留在东台。

## 关键设计约束

- 一句话可以产生多个 `LightingNeed`，不存在整句级别的“要灯/不要灯”标签。
- “随时可叫助手”只记录为 `alternative="on_call_assistant"`，其 `consent_to_remove_continuous_light` 为 `False`；若要用助手替代常亮，必须另有本人明确接受。
- `except on the return path` 只给“关灯承诺”加 `except_directions=[RETURN]`，不会生成覆盖整个观测时段的灯照需求。
- 连续照明由实际 `LightUse` 区间覆盖去程/回程全程来验证；几次 1 分钟开灯、闪烁或时间长度相加但有断档，都会触发 `continuity_not_met`（规则 `R-P4-CONTINUITY`）。
- 同一红光窗口按“窗口容量”占组，而不是只判断两组时间是否重叠。
- 代理/集体报名不会被当作本人接受；陪行资源也必须由陪行员确认其负责的具体班次/组别。

## 目录

```text
darksky/
  models.py     # 枚举、人员、需求、确认版本、区段、问题等数据类型
  parsing.py    # 中文/短英文照明表述拆分
  service.py    # 可用性、规则校验、确认码、闭合改派、人工队列、审计
  corpus.py     # data/corpus.json 装配为演示场景
  cli.py        # python -m darksky 命令行
tests/          # 解析、调度、闭合、隐私和审计测试
data/           # 原始合成语料（保持原样）
materials.py    # 原有语料读取/校验辅助
```
