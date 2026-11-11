---
name: task-orchestration
description: 任务执行规范——把大任务拆成父/子任务并编排多个 agent 会话的调度规程，核心是「按套餐的双通道（快速/慢速）发牌 + 插队 + 保活」。当需要并发跑多个 sub-agent/ACP 会话、编排子代理、把大任务拆解成可调度的小任务，或担心「中转站并发受限 / 会话快慢无法指定 / 会话闲置约 2 分钟被回收」时使用。触发词：任务执行规范、任务编排、并发调度、会话并发上限、快速会话、慢速会话、双通道、拆任务、父子任务、插队、保活、别让 agent 休息、会话被回收。
---

# 任务执行规范（task-orchestration）

调度 sub-agent / ACP 会话时用这套规程，把「任务细分、并发上限、插队、保活」落到一个可读写的状态文件上。
**不要拍脑袋并发**：套餐给的是**双通道额度**（当前：4 个快速会话 + 4 个慢速会话，共 8），超了就被限流/排队。
**也不要让会话闲着**：一个会话就是编程工具里的一个对话/任务窗口，**空闲约 2 分钟就自动结束、名额自动空出来**——所以空转不亏额度，亏的是**上下文**：正在进行的对话和任务状态没了，重开要重新喂材料、重新握手，白干一遍。

## 双通道：快速 / 慢速（当前套餐口径）

套餐：**4 个快速会话 + 4 个慢速会话**。但——

> **会话快/慢无法指定**：新会话按**先手顺序**入场，快速槽先填满，填满了才落到慢速槽。

所以调度器只干两件事：

1. **两条通道各自发牌**：`fast_slots: 4` / `slow_slots: 4`。快速满了只能进慢速；两条都满就拒绝 `start`（`--force` 硬开会超限）。
2. **账本记「推定通道」+ 入场序号**：`start` 时按先到先得推定它落在哪个槽（`fast#3` / `slow#1`），并记 `arrival` 顺序号。**账本是推定的，不是控制** —— 平台实际给的通道可能和推定不符，用 `relane <id> --lane slow` 校正，别让计数骗你。

快槽是稀缺资源（同样的活跑得更快），**发牌顺序就是资源分配顺序**：`next` 按优先级发牌，urgent/high 先上、自然先吃快槽。想让急活进快槽，就在开跑前插队（`insert --mode head`），别等它排到慢槽再后悔。
抢占也优先冻结**快通道的低优先级任务**（腾出的快槽能直接给急活）。

## 核心原则

1. **并发发牌**：同时运行的会话数 ≤ `fast_slots + slow_slots`，且**每条通道各自 ≤ 各自己的额度**。想开新会话先看有没有空闲槽（`next`），没槽就等 `finish`，或 `insert --mode preempt` 腾槽。
   ⚠️ 名额会因空闲自动空出，**账本容易比现实落后**：开新会话前以 `subagents list` / `openclaw tasks list` 的实际占用为准，别只信计数。
2. **任务细分到「一次会话能交付」**：每个子任务要有明确产出物和验收点。太粗 → 会话里塞太多步、容易空转；太细 → 会话数量爆炸、并发排队变长。
3. **插队靠优先级，不靠硬挤**：队列按优先级发牌；急事用 `insert` 插队，抢占只在必要时做，且不抢被依赖的/urgent 的任务。
4. **保活 = 别丢上下文**：running 任务的会话必须持续有产出；每 ≤ `keepalive_interval_seconds` 续一拍。真要等外部输入，**先把中间产出落盘**，再让它自然结束；回来时从文件恢复重开，别指望上下文还在。

## 配置（`config.json`）

```json
{
  "fast_slots": 4,                 // 快速会话通道额度（套餐给几条写几条）
  "slow_slots": 4,                 // 慢速会话通道额度
  "idle_reclaim_seconds": 120,     // 会话空闲回收线：超过此时长自动结束（名额自动空出，但上下文丢失）
  "keepalive_interval_seconds": 60,// 保活间隔：跑满这么多秒就打一拍
  "queue_policy": "priority"       // priority（优先级发牌）| fifo（先来先跑）
}
```

总并发 = `fast_slots + slow_slots`（当前 8）。改配置：`dispatch.py config set fast_slots 4`；查看：`dispatch.py config show`。
**上线前先问清套餐的两个数**：快速几条、慢速几条，写进 config；再确认闲置回收秒数。
兼容：旧配置只有 `max_concurrency` 时按**单通道**读（全部算快速槽）；`config set max_concurrency N` = `fast_slots N + slow_slots 0`。

## 状态与文件

- 状态文件：`~/.openclaw/workspace/state/task-orchestration/<project>.json`（任务树 + 队列 + 会话映射 + 心跳）
- 脚本：`scripts/dispatch.py`（本 skill 唯一入口）
- 每个「项目」一个状态文件，互不干扰。

## 工作流（五步）

**① 建项目**（写清总目标）
```bash
D=~/.openclaw/workspace/skills/task-orchestration/scripts/dispatch.py
python3 $D init --project 视频3条 --goal "把长文做成3条短视频"
```

**② 细分任务**（父任务 + 子任务，标依赖/优先级/预估）
```bash
python3 $D add --project 视频3条 --title "拆分脚本" --priority high
python3 $D add --project 视频3条 --title "生成TTS配音" --parent T1 --deps T1
python3 $D add --project 视频3条 --title "剪辑合成" --parent T1 --deps T2
python3 $D add --project 视频3条 --title "封面图" --parent T1 --deps T1 --priority low --est 5m
python3 $D plan --project 视频3条        # 用树形图核对拆分
```

**③ 发牌开跑**（每次只领空闲槽那么多；快速槽先填，满了才进慢速）
```bash
python3 $D next --project 视频3条        # 看两条通道空闲数 + 每个任务会落到哪个槽
python3 $D start T1 --project 视频3条 --session <subagentSessionKey>
# ▶ T1 开始 → fast#1（第 1 个入场）  占用: 快速 1/4  慢速 0/4  共 1/8
```
- 快槽是稀缺资源：把 urgent/high 的活排在前面先开，它们自然先吃快槽；快槽抢完了新会话就只能进慢槽。
- 实际通道跟账本不一致时（比如平台按你没想到的顺序发牌）→ `python3 $D relane T3 --project 视频3条 --lane slow` 改回真实值。

**④ 保活巡检**（建议挂 cron 每分钟跑一次）
```bash
python3 $D tick --project 视频3条        # [正常]/[提醒]/[危险]，危险时 exit 3
```
- `[提醒]`：闲置超过保活间隔 → `sessions_send(sessionKey, "继续")` 打一拍（或让它继续输出）。
- `[危险]`：已接近自动结束 → 立刻续一拍；救不回来就**把产出物落盘**、修正账本（`finish` / `--fail`），重开的新会话从文件接着干。

**⑤ 收尾放牌**
```bash
python3 $D finish T1 --project 视频3条                    # 成功
python3 $D finish T3 --project 视频3条 --fail "TTS报错"   # 失败
python3 $D next --project 视频3条                         # 释放的槽立刻补下一个
```

## 命令速查

| 命令 | 作用 |
|---|---|
| `init --project P --goal "…"` | 建项目 |
| `add --title "…" [--parent T1] [--priority urgent|high|normal|low] [--deps T1,T2] [--est 5m]` | 加任务，自动编号 T1/T2… |
| `plan` | 树形展示父/子任务 + 两条通道状态 + 下一步（带推定槽位） |
| `next [--n N]` | 按空闲槽给出可开跑任务（依赖已满足），标出会落在 `fast#n` / `slow#n` |
| `start <id> [--session KEY] [--force]` | 占一个槽开跑（先手顺序：先快后慢）；两通道均满/依赖未完成会拒绝 |
| `relane <id> --lane fast\|slow [--force]` | 校正通道账本（快/慢无法指定，账本只是推定） |
| `finish <id> [--fail "原因"]` | 收尾并释放槽位 |
| `insert <id> --mode head\|after:<id>\|preempt` | 插队 |
| `heartbeat <id>` | 打一次心跳 |
| `tick` | 保活巡检（危险 → exit 3） |
| `status` | 计数概览 |
| `config show\|set k v` | 看/改配置 |
| `rm <id> [--force]` | 删任务（有子任务需 --force） |

所有命令加 `--json` 走机器可读输出。

## 任务细分：三种粒度

- **编排层（父任务）**：一个可交付目标，如「3 条短视频」。不下发到会话，只用于归类。
- **执行层（子任务）**：**一次 agent 会话能独立完成并交付**，如「写脚本」「配 TTS」「剪片」。下发到会话的就是这一层。
- **落地层（可选）**：子任务内部再由执行会话自己细分，不占用全局队列。

判据：**能一句话说清「产出物是什么、怎么算完成」，就能当子任务**；说不清就再拆一层。
参考案例见 `references/patterns.md`。

## 插队三档

| 档位 | 命令 | 什么时候用 |
|---|---|---|
| 队首插入 | `insert T9 --mode head` | 急事，但**还有空闲槽**——直接排到下一个 |
| 定点插入 | `insert T9 --mode after:T3` | 要在某个任务之后紧接着跑 |
| 抢占腾槽 | `insert T9 --mode preempt` | **两条通道都满**且新任务更急——冻结一个低优先级、非 urgent、无运行依赖的任务，腾槽 |
| 提优先级 | `add … --priority urgent` / 重排 | 批量调整紧急度，不动队列顺序 |

抢占后的收尾：`insert --mode preempt` 只冻结排队，**不会自动杀掉会话**。先 `subagents kill <target>` 停掉被冻结会话，再 `dispatch.py start T9`。
选人规则：优先冻结**快通道**里优先级最低的任务（腾出的快槽才能直接给急活；冻慢槽的话，急活还是只能进慢槽）。

## 保活：谁负责什么

**先认清机制**：会话空闲约 2 分钟会自动结束，**名额自动空出来**。所以挂机会丢的不是额度，是会话里的上下文和任务状态。保活的目的只有一个：**别让活干到一半掉链子**。

- **默认机制**：每个 running 会话要持续产出（边想边写、边跑边落盘）。有产出自然就不会闲到被结束。
- **兜底心跳**：超过 `keepalive_interval_seconds` 没动静，就 `sessions_send(session, "继续")` 打一拍。用 `tick` 巡检、cron 定时跑。
- **等外部输入时**：产出落盘 → 让它结束（或主动 `finish`）→ 账本记下断点 → 条件满足后重新 `add`/`insert` 开新会话，从文件恢复。**不要挂在运行的槽上等**：你控制不了它何时被结束，也拿不到收尾机会，等于丢上下文。
- **不要**为了保活让它空转说废话——空转虽不占额度，但会污染上下文、浪费轮次。保活要的是「有真实产出」。
- **账本对账**：因为会话会自动结束，`running` 计数可能虚高。发现账本说在跑、`subagents list` 里却没有 → 立刻 `finish`/`--fail` 修正，别让僵尸任务堵着队列（堵一个槽 = 少一条快速/慢速通道）。
- **通道对账**：快/慢是平台按先手顺序给的，账本只能推定。发现某个 `fast#n` 实际跑得很慢（或反之）→ `relane` 改正，否则你以为快槽还有 2 个、实际已经满了。

## 与 OpenClaw 工具对接

| 动作 | 工具 |
|---|---|
| 开一个执行会话 | `sessions_spawn`（`runtime:"subagent"` 或 `"acp"`）→ 拿到 sessionKey 后 `dispatch.py start <id> --session <key>` |
| 续跑 / 保活 | `sessions_send(sessionKey, "继续…")` |
| 停被抢占的会话 | `subagents kill <target>` |
| 看运行中的后台任务 | `openclaw tasks list` / `openclaw tasks show <id>` |
| 定时保活巡检 | `cron` 每分钟跑 `dispatch.py tick`（危险时用 exit 3 触发告警） |

## 红线

- **不超并发**：快速 ≤ `fast_slots`、慢速 ≤ `slow_slots`，总数 ≤ 两者之和（当前 4+4=8）。没空闲槽就不 `start`（除非明确知道平台扛得住，再 `--force`）。
- **不硬拆依赖**：`start` 会挡未完成依赖；别用 `--force` 绕过真依赖。
- **不挂机会话**：要等外部输入 → 先把中间产出落盘再让它结束，重开时从文件恢复，别指望上下文还在。
- **不信虚账**：开新会话前用 `subagents list` / `openclaw tasks list` 核实真实占用。
- **不挑通道**：快/慢无法指定，别写“帮我开个快速会话”——只能靠**开跑顺序 + 优先级**去抢；实际通道不符就用 `relane` 改账本。
- **不自动杀会话**：抢占只冻结排队，杀会话要人/`subagents kill` 确认。
