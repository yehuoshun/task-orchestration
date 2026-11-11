# task-orchestration 🪝

任务执行规范 skill —— 调度多个 agent 会话时，把**任务细分、双通道并发上限（快速/慢速）、插队、保活**落到一份可读写的状态文件上。

> 适用场景：并发跑多个 sub-agent / ACP 会话；套餐对会话并发有硬上限，而且**分快速/慢速两条通道**；会话空闲约 2 分钟会自动结束（**名额自动空出，但上下文丢失**）。

## 双通道口径（当前套餐）

**4 个快速会话 + 4 个慢速会话（共 8 并发），但会话快/慢无法指定**：新会话按**先手顺序**入场，快速槽先填满，满了才落到慢速槽。

所以调度器：

- **两条通道各自发牌**，各 4 槽，哪条都不超发，两条都满就拒绝 `start`；
- 账本里记**推定通道 + 入场序号**（`fast#3` / `slow#1`），实际不符时用 `relane` 校正；
- 发牌顺序就是资源分配顺序：`next` 按优先级发牌，urgent/high 先开、自然先吃快槽。

## 它解决什么问题

| 问题 | 做法 |
|---|---|
| 并发被套餐限流 | 双通道发牌：`fast_slots` / `slow_slots` 各自计数，槽满拒绝 `start` |
| 快/慢没法指定，快槽被滥占 | 优先级发牌 + 插队：想让急活拿快槽，就开跑前 `insert --mode head` |
| 任务粒度说不清 | 父/子任务树 + 依赖 + 优先级，`plan` 一眼看全 |
| 急活要加塞 | `insert --mode head / after:<id> / preempt` 三档插队（抢占优先冻结快通道的低优先级任务） |
| 会话闲两分钟就没了 | `tick` 保活巡检（正常/提醒/危险），危险 exit 3 可挂 cron |
| 账本和现实不一致 | 会话自动结束后 `running` 会虚高 → 用 `subagents list` 对账；通道不符用 `relane` |

## 目录结构

```
task-orchestration/
├── SKILL.md                 # 技能说明（agent 读这个）
├── config.json              # 双通道额度 / 回收线 / 保活间隔
├── scripts/dispatch.py      # 调度器（唯一入口）
└── references/patterns.md   # 4 个编排案例
```

## 安装

把整个目录放进 OpenClaw 的 skills 目录即可：

```bash
cp -r task-orchestration ~/.openclaw/workspace/skills/
```

状态文件默认写到 `~/.openclaw/workspace/state/task-orchestration/<project>.json`（脚本内 `STATE_DIR`，可改）。

## 快速上手

```bash
S=~/.openclaw/workspace/skills/task-orchestration/scripts/dispatch.py

python3 $S init  --project 视频3条 --goal "把长文做成3条短视频"
python3 $S add   --project 视频3条 --title "拆分脚本" --priority high
python3 $S add   --project 视频3条 --title "生成TTS配音" --parent T1 --deps T1
python3 $S add   --project 视频3条 --title "剪辑合成" --parent T1 --deps T2
python3 $S plan  --project 视频3条
python3 $S next  --project 视频3条        # 看两条通道空闲 + 推定槽位
python3 $S start T1 --project 视频3条 --session <sessionKey>   # → fast#1
python3 $S tick  --project 视频3条
python3 $S finish T1 --project 视频3条
```

## 命令速查

| 命令 | 作用 |
|---|---|
| `init --project P --goal "…"` | 建项目 |
| `add --title "…" [--parent T1] [--priority urgent\|high\|normal\|low] [--deps T1,T2] [--est 5m]` | 加任务，自动编号 |
| `plan` | 树形展示 + 两条通道占用 + 下一步 |
| `next [--n N]` | 按空闲槽给出可开跑任务（标 `fast#n` / `slow#n`） |
| `start <id> [--session KEY] [--force]` | 占槽开跑（先快后慢，两通道均满拒绝） |
| `relane <id> --lane fast\|slow [--force]` | 校正通道账本 |
| `finish <id> [--fail "原因"]` | 收尾并释放槽位 |
| `insert <id> --mode head\|after:<id>\|preempt` | 插队 |
| `heartbeat <id>` / `tick` | 心跳 / 保活巡检（危险 exit 3） |
| `status` / `config show\|set k v` / `rm <id>` | 状态 / 配置 / 删除 |

所有命令支持 `--json`。

## 配置

```json
{
  "fast_slots": 4,
  "slow_slots": 4,
  "idle_reclaim_seconds": 120,
  "keepalive_interval_seconds": 60,
  "queue_policy": "priority"
}
```

总并发 = `fast_slots + slow_slots`。升级/降级套餐后先改这两个数；旧配置只有 `max_concurrency` 时按单通道（全算快速）读。

## 核心原则

1. **并发按牌发**：没空槽不 `start`；两条通道各不超发。
2. **细分到「一次会话能交付」**：产出物 + 验收点说得清，才算一个子任务。
3. **插队按优先级**：抢占只冻结排队（优先冻快槽里的低优先级任务），杀会话要人确认。
4. **保活为的是上下文**：空闲不占额度，但会话没了就丢状态；要等外部输入先落盘再放手。

## 版本记录

- **v0.2.0**（2026-11-12）套餐升级：**4 快速 + 4 慢速双通道**；会话快/慢无法指定 → 按先手顺序入场（先快后慢）；新增 `relane` 校正通道账本；抢占优先冻结快通道；旧 `max_concurrency` 配置兼容。
- **v0.1.0**（2026-11-11）首版：并发闸 / 任务树 / 优先级队列 / 插队三档 / 保活巡检 / JSON 输出