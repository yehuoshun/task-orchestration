# task-orchestration 🪝

任务执行规范 skill —— 调度多个 agent 会话时，把**任务细分、并发上限、插队、保活**落到一份可读写的状态文件上。

> 适用场景：并发跑多个 sub-agent / ACP 会话；中转站对 agent 会话并发有硬上限；会话空闲约 2 分钟会自动结束（**名额自动空出，但上下文丢失**）。

## 它解决什么问题

| 问题 | 做法 |
|---|---|
| 并发被中转站限流 | `max_concurrency` 发牌：槽满拒绝 `start`，只按空闲槽分派 |
| 任务粒度说不清 | 父/子任务树 + 依赖 + 优先级，`plan` 一眼看全 |
| 急活要加塞 | `insert --mode head / after:<id> / preempt` 三档插队 |
| 会话闲两分钟就没了 | `tick` 保活巡检（正常/提醒/危险），危险 exit 3 可挂 cron |
| 账本和现实不一致 | 会话自动结束后 `running` 会虚高 → 用 `subagents list` 对账，僵尸任务及时 `finish` |

## 目录结构

```
task-orchestration/
├── SKILL.md                 # 技能说明（agent 读这个）
├── config.json              # 并发上限 / 回收线 / 保活间隔
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
python3 $S next  --project 视频3条
python3 $S start T1 --project 视频3条 --session <sessionKey>
python3 $S tick  --project 视频3条
python3 $S finish T1 --project 视频3条
```

## 命令速查

| 命令 | 作用 |
|---|---|
| `init --project P --goal "…"` | 建项目 |
| `add --title "…" [--parent T1] [--priority urgent\|high\|normal\|low] [--deps T1,T2] [--est 5m]` | 加任务，自动编号 |
| `plan` | 树形展示 + 并发状态 + 下一步 |
| `next [--n N]` | 按空闲槽给出可开跑任务 |
| `start <id> [--session KEY] [--force]` | 占用槽开跑 |
| `finish <id> [--fail "原因"]` | 收尾并释放槽位 |
| `insert <id> --mode head\|after:<id>\|preempt` | 插队 |
| `heartbeat <id>` / `tick` | 心跳 / 保活巡检（危险 exit 3） |
| `status` / `config show\|set k v` / `rm <id>` | 状态 / 配置 / 删除 |

所有命令支持 `--json`。

## 配置

```json
{
  "max_concurrency": 2,
  "idle_reclaim_seconds": 120,
  "keepalive_interval_seconds": 60,
  "queue_policy": "priority"
}
```

上线前先问清中转站的两个数：**并发上限**、**闲置回收秒数**。

## 核心原则

1. **并发按牌发**：没空槽不 `start`。
2. **细分到「一次会话能交付」**：产出物 + 验收点说得清，才算一个子任务。
3. **插队按优先级**：抢占只冻结排队，杀会话要人确认。
4. **保活为的是上下文**：空闲不占额度，但会话没了就丢状态；要等外部输入先落盘再放手。

## 版本记录

- **v0.1.0**（2026-11-11）首版：并发闸 / 任务树 / 优先级队列 / 插队三档 / 保活巡检 / JSON 输出