# 编排模式与案例（task-orchestration）

- [案例 A：把长文做成 3 条短视频（父子任务）](#案例-a把长文做成-3-条短视频父子任务)
- [案例 B：双通道 4 快速 + 4 慢速（当前套餐）](#案例-b双通道-4-快速--4-慢速当前套餐)
- [案例 C：单通道小额度（并发只有 1）](#案例-c单通道小额度并发只有-1)
- [案例 D：来了个急活（插队）](#案例-d来了个急活插队)
- [案例 E：子任务必须等人工确认（释放槽位）](#案例-e子任务必须等人工确认释放槽位)
- [定时保活巡检](#定时保活巡检)

## 案例 A：把长文做成 3 条短视频（父子任务）

目标可交付「3 条成片」。子任务粒度对齐「一次会话能交付」：

```
✔ T1 拆分脚本（父；产出 3 段脚本）
  ✔ T2 生成 TTS 配音（依赖 T1）
  ✔ T3 剪辑合成（依赖 T2）
  ○ T4 封面图（依赖 T1，low，可与 T2 并行）
```

```
init --project 视频3条 --goal "把长文做成3条短视频"
add  --project 视频3条 --title "拆分脚本" --priority high
add  --project 视频3条 --title "生成TTS配音" --parent T1 --deps T1
add  --project 视频3条 --title "剪辑合成" --parent T1 --deps T2
add  --project 视频3条 --title "封面图"  --parent T1 --deps T1 --priority low --est 5m
plan --project 视频3条
```

注意 T4（封面图）和 T2 都只依赖 T1，会并行——正好利用并发上限。T3 依赖 T2，串行排在后面。

## 案例 C：单通道小额度（并发只有 1）

`config set max_concurrency 1`（= 快速 1 + 慢速 0，单通道）：任何时刻只能一个会话在跑。此时「拆分要偏大、偏串行」，减少会话切换成本：

```
config set max_concurrency 1
next   # 只发 1 个
start T1 --session <key>
# …T1 跑完
finish T1
next   # 补下一个可跑的
```

判据：并发越低，粒度越粗（一个会话多干几步）；并发越高，粒度越细（好并行）。

## 案例 B：双通道 4 快速 + 4 慢速（当前套餐）

套餐给 8 个并发，但**快/慢无法指定**：按先手顺序入场，先来的占快速槽，满 4 个后新的只能进慢速槽。

策略就是「**把重要的活先送去排快队**」：

```
config show                     # 快速 4 / 慢速 4
next                            # 看两条通道空闲 + 推定的 fast#n / slow#n
start T1 --session <key>        # ▶ fast#1
start T2 --session <key>        # ▶ fast#2
…                               # 快速 4 个占满后，再 start 就是 slow#n
```

- 快槽被低优先级任务占着 → 新来的急活拿不到快槽。所以**先开 urgent/high**，或者急活用 `insert --mode head` 提前排队。
- 快槽里的低优先级任务跑完前，新会话只能进慢速——接受不了就用 `insert --mode preempt`（会优先冻结快槽里的低优先级任务）。
- 平台实际发的通道和你推的不一样 → `relane T3 --lane slow` 改账本，否则后面发牌会算错。

## 案例 D：来了个急活（插队）

槽位已满（两通道 8/8），新任务 `T9` 更急：

```
insert T9 --mode preempt
# ⇥ T9 抢占成功：冻结 T4（回 pending），腾出 1 槽
subagents kill <T4 的会话>
start T9 --session <key>
```

抢占的选人规则：**优先快通道** → 优先级最低 → 非 urgent → 没有被其它运行任务依赖（脚本自动挑，挑不到会报错）。
冻快槽才用得上：腾出的快槽会直接给急活；冻慢槽的话，急活还是只能进慢槽。
只是「想让某任务早一点、但还有空槽」时，用 `insert T9 --mode head`，不动任何运行中的会话。

## 案例 E：子任务必须等人工确认（先落盘，再放手）

会话要等用户拍板才能继续。注意：**挂机不会占住额度**——空闲约 2 分钟它自己就结束了、名额自动空出。真正的风险是**上下文跟着没了**：

```
# 1) 先把中间产出写文件（断点）
#    例: out/封面文案-draft.md
finish T7 --note "待确认：封面文案，断点在 out/封面文案-draft.md"

next        # 空出的名额立刻可以给别的任务

# 2) 确认到了，重开新会话，从文件接着干
insert T7 --mode head
start T7 --session <新会话>
```

原则：**等外部输入 = 先落盘 + 主动收尾**。别指望“挂着等会儿就接着干”——它会被系统结束，你还没机会把结论存下来。

## 定时保活巡检

用 cron 每分钟跑一次 `tick`，危险（超过回收线）时靠 exit 3 触发告警。示例（isolated 巡检 + 播报）：

- 脚本：`python3 ~/.openclaw/workspace/skills/task-orchestration/scripts/dispatch.py tick --project <项目>`
- 退出码：`0` 正常 / `3` 有任务已接近自动结束。
- 接到 `[提醒]` 时：`sessions_send(sessionKey, "继续")`；接到 `[危险]` 时：优先续一拍，救不回来就让产出落盘 + 修正账本。
- **同时用 `subagents list` 对账**：会话自动结束后账本可能还写着 running，见到就 `finish`/`--fail` 清掉。
