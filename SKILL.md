---
name: task-orchestration
description: 任务执行规范——把大任务拆成父/子任务并编排多个 agent 会话的调度规程，核心是「按中转站并发上限发牌 + 插队 + 保活」。当需要并发跑多个 sub-agent/ACP 会话、编排子代理、把大任务拆解成可调度的小任务，或担心「中转站并发受限 / 会话闲置约 2 分钟被回收」时使用。触发词：任务执行规范、任务编排、并发调度、会话并发上限、拆任务、父子任务、插队、保活、别让 agent 休息、会话被回收。
---

# 任务执行规范（task-orchestration）

调度 sub-agent / ACP 会话时用这套规程，把「任务细分、并发上限、插队、保活」落到一个可读写的状态文件上。
**不要拍脑袋并发**：中转站对 agent 会话并发有硬上限，超了就被限流/排队。
**也不要让会话闲着**：一个会话就是编程工具里的一个对话/任务窗口，**空闲约 2 分钟就自动结束、名额自动空出来**——所以空转不亏额度，亏的是**上下文**：正在进行的对话和任务状态没了，重开要重新喂材料、重新握手，白干一遍。

## 核心原则

1. **并发发牌**：同时运行的 agent 会话数 ≤ `max_concurrency`。想开新会话先看有没有空闲槽（`next`），没槽就等 `finish`，或 `insert --mode preempt` 腾槽。
   ⚠️ 名额会因空闲自动空出，**账本容易比现实落后**：开新会话前以 `subagents list` / `openclaw tasks list` 的实际占用为准，别只信计数。
2. **任务细分到「一次会话能交付」**：每个子任务要有明确产出物和验收点。太粗 → 会话里塞太多步、容易空转；太细 → 会话数量爆炸、并发排队变长。
3. **插队靠优先级，不靠硬挤**：队列按优先级发牌；急事用 `insert` 插队，抢占只在必要时做，且不抢被依赖的/urgent 的任务。
4. **保活 = 别丢上下文**：running 任务的会话必须持续有产出；每 ≤ `keepalive_interval_seconds` 续一拍。真要等外部输入，**先把中间产出落盘**，再让它自然结束；回来时从文件恢复重开，别指望上下文还在。

## 配置（`config.json`）

```json
{
  "max_concurrency": 2,            // 同时运行的 agent 会话上限（按中转站填写）
  "idle_reclaim_seconds": 120,     // 会话空闲回收线：超过此时长自动结束（名额自动空出，但上下文丢失）
  "keepalive_interval_seconds": 60,// 保活间隔：跑满这么多秒就打一拍
  "queue_policy": "priority"       // priority（优先级发牌）| fifo（先来先跑）
}
```

改配置：`dispatch.py config set max_concurrency 4`；查看：`dispatch.py config show`。
**上线前先问清中转站的两个数**：并发上限、闲置回收秒数，写进 config。

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

**③ 发牌开跑**（每次只领空闲槽那么多）
```bash
python3 $D next --project 视频3条        # 看空闲槽 + 建议分派哪些
python3 $D start T1 --project 视频3条 --session <subagentSessionKey>
```

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
| `plan` | 树形展示父/子任务 + 并发状态 + 下一步 |
| `next [--n N]` | 按空闲槽给出可开跑任务（依赖已满足） |
| `start <id> [--session KEY] [--force]` | 占用一个槽开跑；槽满/依赖未完成会拒绝 |
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
| 抢占腾槽 | `insert T9 --mode preempt` | **槽位已满**且新任务更急——冻结一个低优先级、非 urgent、无运行依赖的任务，腾槽 |
| 提优先级 | `add … --priority urgent` / 重排 | 批量调整紧急度，不动队列顺序 |

抢占后的收尾：`insert --mode preempt` 只冻结排队，**不会自动杀掉会话**。先 `subagents kill <target>` 停掉被冻结会话，再 `dispatch.py start T9`。

## 保活：谁负责什么

**先认清机制**：会话空闲约 2 分钟会自动结束，**名额自动空出来**。所以挂机会丢的不是额度，是会话里的上下文和任务状态。保活的目的只有一个：**别让活干到一半掉链子**。

- **默认机制**：每个 running 会话要持续产出（边想边写、边跑边落盘）。有产出自然就不会闲到被结束。
- **兜底心跳**：超过 `keepalive_interval_seconds` 没动静，就 `sessions_send(session, "继续")` 打一拍。用 `tick` 巡检、cron 定时跑。
- **等外部输入时**：产出落盘 → 让它结束（或主动 `finish`）→ 账本记下断点 → 条件满足后重新 `add`/`insert` 开新会话，从文件恢复。**不要挂在运行的槽上等**：你控制不了它何时被结束，也拿不到收尾机会，等于丢上下文。
- **不要**为了保活让它空转说废话——空转虽不占额度，但会污染上下文、浪费轮次。保活要的是「有真实产出」。
- **账本对账**：因为会话会自动结束，`running` 计数可能虚高。发现账本说在跑、`subagents list` 里却没有 → 立刻 `finish`/`--fail` 修正，别让僵尸任务堵着队列。

## 与 OpenClaw 工具对接

| 动作 | 工具 |
|---|---|
| 开一个执行会话 | `sessions_spawn`（`runtime:"subagent"` 或 `"acp"`）→ 拿到 sessionKey 后 `dispatch.py start <id> --session <key>` |
| 续跑 / 保活 | `sessions_send(sessionKey, "继续…")` |
| 停被抢占的会话 | `subagents kill <target>` |
| 看运行中的后台任务 | `openclaw tasks list` / `openclaw tasks show <id>` |
| 定时保活巡检 | `cron` 每分钟跑 `dispatch.py tick`（危险时用 exit 3 触发告警） |

## 红线

- **不超并发**：没有空闲槽就不 `start`（除非明确知道中转站扛得住，再 `--force`）。
- **不硬拆依赖**：`start` 会挡未完成依赖；别用 `--force` 绕过真依赖。
- **不挂机会话**：要等外部输入 → 先把中间产出落盘再让它结束，重开时从文件恢复，别指望上下文还在。
- **不信虚账**：开新会话前用 `subagents list` / `openclaw tasks list` 核实真实占用。
- **不自动杀会话**：抢占只冻结排队，杀会话要人/`subagents kill` 确认。
