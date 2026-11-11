#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dispatch.py — 任务执行规范的调度器（并发闸 + 任务树 + 插队队列 + 保活）

用途：调度 sub-agent / ACP 会话时，把「任务细分、并发上限、插队、保活」落到一个
可读写的状态文件上，避免拍脑袋并发、避免会话闲置被中转站回收。

状态文件：~/.openclaw/workspace/state/task-orchestration/<project>.json
配置：    <skill>/config.json

用法速查见 SKILL.md。所有命令支持 --json 输出。
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(SKILL_DIR, "config.json")
STATE_DIR = os.path.expanduser("~/.openclaw/workspace/state/task-orchestration")

PRIO = {"urgent": 0, "high": 1, "normal": 2, "low": 3}
PRIO_NAME = {v: k for k, v in PRIO.items()}
STATUS_ICON = {
    "pending": "○", "running": "▶", "done": "✔",
    "failed": "✘", "suspended": "⏸",
}


def now_ms():
    return int(time.time() * 1000)


def now_h():
    return datetime.now().strftime("%H:%M:%S")


def load_config():
    defaults = {
        "max_concurrency": 2,
        "idle_reclaim_seconds": 120,
        "keepalive_interval_seconds": 60,
        "queue_policy": "priority",  # priority | fifo
    }
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            defaults.update(json.load(f))
    except FileNotFoundError:
        pass
    return defaults


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")


def state_path(project):
    return os.path.join(STATE_DIR, f"{project}.json")


def load_state(project, required=True):
    p = state_path(project)
    if not os.path.exists(p):
        if required:
            die(f"项目 '{project}' 不存在，先跑: dispatch.py init --project {project} --goal '...'")
        return None
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def save_state(st):
    os.makedirs(STATE_DIR, exist_ok=True)
    st["updated_at"] = now_ms()
    with open(state_path(st["project"]), "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
        f.write("\n")


def die(msg, code=2):
    print(f"错误: {msg}", file=sys.stderr)
    sys.exit(code)


def out(obj, as_json, human=None):
    if as_json:
        print(json.dumps(obj, ensure_ascii=False, indent=2))
    elif human:
        print(human)
    return obj


# ---------- 队列/并发 ----------

def running_tasks(st):
    return [t for t in st["tasks"].values() if t["status"] == "running"]


def ready_tasks(st):
    """依赖已满足、可以开跑的 pending 任务，按 (未插队顺序 / 优先级) 排。"""
    done = {i for i, t in st["tasks"].items() if t["status"] == "done"}
    ready = []
    for tid in st["order"]:
        t = st["tasks"][tid]
        if t["status"] != "pending":
            continue
        if all(d in done for d in t["deps"]):
            ready.append(t)
    if st.get("queue_policy", "priority") == "priority":
        seq = {tid: i for i, tid in enumerate(st["order"])}
        ready.sort(key=lambda t: (t.get("_jump", 0), PRIO.get(t["priority"], 2), seq[t["id"]]))
    return ready


def free_slots(st, cfg):
    return max(0, int(cfg["max_concurrency"]) - len(running_tasks(st)))


# ---------- 命令 ----------

def cmd_init(a):
    st = {
        "project": a.project,
        "goal": a.goal,
        "queue_policy": a.queue_policy,
        "created_at": now_ms(),
        "updated_at": now_ms(),
        "seq": 0,
        "tasks": {},
        "order": [],
    }
    save_state(st)
    out({"ok": True, "project": a.project, "state": state_path(a.project)}, a.json,
        human=f"已建项目 {a.project}: {a.goal}")


def cmd_add(a):
    st = load_state(a.project)
    st["seq"] += 1
    tid = f"T{st['seq']}"
    deps = [d.strip() for d in (a.deps or "").split(",") if d.strip()]
    for d in deps:
        if d not in st["tasks"]:
            die(f"依赖 {d} 不存在")
    if a.parent and a.parent not in st["tasks"]:
        die(f"父任务 {a.parent} 不存在")
    st["tasks"][tid] = {
        "id": tid, "title": a.title, "parent": a.parent,
        "priority": a.priority, "deps": deps, "est": a.est,
        "status": "pending", "session": None,
        "started_at": None, "finished_at": None, "last_heartbeat": None,
        "fail_reason": None, "note": a.note, "_jump": 0,
    }
    st["order"].append(tid)
    save_state(st)
    out({"ok": True, "id": tid, "parent": a.parent, "title": a.title, "priority": a.priority, "deps": deps},
        a.json, human=f"+ {tid} {a.title}" + (f"  (子于 {a.parent})" if a.parent else "") +
        (f"  deps={','.join(deps)}" if deps else ""))


def _tree(st):
    children = {}
    roots = []
    for tid in st["order"]:
        t = st["tasks"][tid]
        if t["parent"]:
            children.setdefault(t["parent"], []).append(tid)
        else:
            roots.append(tid)
    return roots, children


def _print_tree(st, roots, children, depth=0, prefix=""):
    for tid in roots:
        t = st["tasks"][tid]
        icon = STATUS_ICON.get(t["status"], "?")
        extra = []
        if t["deps"]:
            extra.append("deps=" + ",".join(t["deps"]))
        if t["priority"] != "normal":
            extra.append(t["priority"])
        if t["est"]:
            extra.append(f"~{t['est']}")
        suffix = ("  [" + " ".join(extra) + "]") if extra else ""
        print(f"{'  ' * depth}{icon} {tid} {t['title']}{suffix}")
        _print_tree(st, children.get(tid, []), children, depth + 1)


def cmd_plan(a):
    st = load_state(a.project)
    cfg = load_config()
    roots, children = _tree(st)
    if a.json:
        out({"goal": st["goal"], "max_concurrency": cfg["max_concurrency"],
             "running": [t["id"] for t in running_tasks(st)],
             "next": [t["id"] for t in ready_tasks(st)][:free_slots(st, cfg)],
             "tasks": st["tasks"]}, True)
        return
    print(f"目标: {st['goal']}")
    print(f"并发上限: {cfg['max_concurrency']}  运行中: {len(running_tasks(st))}  空闲槽: {free_slots(st, cfg)}")
    print(f"回收线: {cfg['idle_reclaim_seconds']}s  保活间隔: {cfg['keepalive_interval_seconds']}s")
    print("-" * 48)
    _print_tree(st, roots, children)
    print("-" * 48)
    nxt = ready_tasks(st)[:free_slots(st, cfg)]
    print("下一步可跑: " + (", ".join(t["id"] + " " + t["title"] for t in nxt) if nxt else "（无 / 槽位已满）"))


def cmd_next(a):
    st = load_state(a.project)
    cfg = load_config()
    free = free_slots(st, cfg)
    n = min(a.n, free)
    picks = ready_tasks(st)[:n]
    if a.json:
        out({"free_slots": free, "picks": [{"id": t["id"], "title": t["title"],
             "priority": t["priority"], "deps": t["deps"]} for t in picks]}, True)
    else:
        if free <= 0:
            print(f"槽位已满（{len(running_tasks(st))}/{cfg['max_concurrency']}），先等 finish 或 insert --mode preempt")
        elif not picks:
            print("没有可跑任务（依赖未满足或队列空）")
        else:
            print(f"空闲槽 {free}，分派 {len(picks)} 个：")
            for t in picks:
                print(f"  → {t['id']} {t['title']}  ({t['priority']})")
            print("开跑: dispatch.py start <id> --session <sessionKey>")


def cmd_start(a):
    st = load_state(a.project)
    cfg = load_config()
    t = st["tasks"].get(a.id)
    if not t:
        die(f"任务 {a.id} 不存在")
    if t["status"] == "running":
        die(f"{a.id} 已在运行")
    if t["status"] == "done":
        die(f"{a.id} 已完成，不能重跑（要重开先 rm 再 add）")
    if t["status"] == "pending":
        done = {i for i, x in st["tasks"].items() if x["status"] == "done"}
        missing = [d for d in t["deps"] if d not in done]
        if missing and not a.force:
            die(f"{a.id} 依赖未完成: {','.join(missing)}（--force 可强开）")
    if free_slots(st, cfg) <= 0 and not a.force:
        die(f"并发已满（{len(running_tasks(st))}/{cfg['max_concurrency']}）。"
            f"等 finish，或 insert --mode preempt 腾槽，或 --force 硬开（可能超限被中转站限流）")
    t["status"] = "running"
    t["session"] = a.session
    t["started_at"] = now_ms()
    t["last_heartbeat"] = now_ms()
    t.pop("_jump", None)
    save_state(st)
    out({"ok": True, "id": a.id, "session": a.session,
         "running": len(running_tasks(st)), "max": cfg["max_concurrency"]}, a.json,
        human=f"▶ {a.id} 开始  (占用 {len(running_tasks(st))}/{cfg['max_concurrency']})")


def cmd_finish(a):
    st = load_state(a.project)
    t = st["tasks"].get(a.id)
    if not t:
        die(f"任务 {a.id} 不存在")
    t["status"] = "failed" if a.fail else "done"
    t["fail_reason"] = a.fail
    t["finished_at"] = now_ms()
    save_state(st)
    out({"ok": True, "id": a.id, "status": t["status"], "note": a.note}, a.json,
        human=f"{STATUS_ICON.get(t['status'],'?')} {a.id} {t['status']}" + (f": {a.fail}" if a.fail else ""))


def cmd_insert(a):
    """插队：把任务挪到队首 / 指定位置 / 抢占腾槽。"""
    st = load_state(a.project)
    cfg = load_config()
    t = st["tasks"].get(a.id)
    if not t:
        die(f"任务 {a.id} 不存在")
    if a.mode == "head":
        st["order"].remove(a.id)
        st["order"].insert(0, a.id)
        t["_jump"] = -1
        save_state(st)
        out({"ok": True, "id": a.id, "mode": "head", "msg": "已插到队首，下一个就开跑"}, a.json,
            human=f"⇥ {a.id} 已插到队首（依赖未满足时仍会自动等待）")
    elif a.mode.startswith("after:"):
        ref = a.mode.split(":", 1)[1]
        if ref not in st["tasks"]:
            die(f"参考任务 {ref} 不存在")
        st["order"].remove(a.id)
        st["order"].insert(st["order"].index(ref) + 1, a.id)
        t["_jump"] = -1
        save_state(st)
        out({"ok": True, "id": a.id, "mode": a.mode}, a.json, human=f"⇥ {a.id} 已插到 {ref} 之后")
    elif a.mode == "preempt":
        running = running_tasks(st)
        if not running:
            out({"ok": True, "msg": "没有运行中的任务，无需抢占；直接 start 即可"}, a.json,
                human="没有运行中的任务，无需抢占；直接 start 即可")
            return
        if free_slots(st, cfg) > 0:
            out({"ok": True, "msg": f"还有 {free_slots(st, cfg)} 个空闲槽，无需抢占；直接 start {a.id} 即可"}, a.json,
                human=f"还有 {free_slots(st, cfg)} 个空闲槽，无需抢占；直接 start {a.id} 即可")
            return
        # 选可牺牲者：优先级最低、非 urgent、且没有被其它运行任务依赖
        dep_of = {}
        for x in st["tasks"].values():
            for d in x["deps"]:
                dep_of.setdefault(d, []).append(x["id"])
        running_ids = {x["id"] for x in running}
        victims = [x for x in running
                   if x["priority"] != "urgent" and not (set(dep_of.get(x["id"], [])) & running_ids)]
        victims.sort(key=lambda x: (-PRIO.get(x["priority"], 2), x["started_at"] or 0))
        if not victims:
            die("所有运行中任务都不可抢占（urgent 或被依赖）")
        v = victims[0]
        v["status"] = "pending"
        v["started_at"] = None
        v["last_heartbeat"] = None
        st["order"].remove(a.id)
        st["order"].insert(0, a.id)
        t["_jump"] = -1
        save_state(st)
        out({"ok": True, "id": a.id, "mode": "preempt", "victim": v["id"],
             "msg": f"已冻结 {v['id']}（回到 pending 排队），腾出 1 槽给 {a.id}",
             "todo": f"确认 {v['id']} 的 session 已停（subagents kill），再 start {a.id}"}, a.json,
            human=(f"⇥ {a.id} 抢占成功：冻结 {v['id']}（回 pending），腾出 1 槽。\n"
                   f"   先停它的会话: subagents kill  →  再 dispatch.py start {a.id}"))
    else:
        die("mode 只能是 head / after:<id> / preempt")


def cmd_heartbeat(a):
    st = load_state(a.project)
    t = st["tasks"].get(a.id)
    if not t:
        die(f"任务 {a.id} 不存在")
    t["last_heartbeat"] = now_ms()
    save_state(st)
    out({"ok": True, "id": a.id, "at": now_h()}, a.json, human=f"♥ {a.id} 心跳 @ {now_h()}")


def cmd_tick(a):
    """保活巡检：为每个运行中任务报告距上次心跳的时长 + 建议动作。"""
    st = load_state(a.project)
    cfg = load_config()
    kp = int(cfg["keepalive_interval_seconds"])
    rc = int(cfg["idle_reclaim_seconds"])
    rows = []
    for t in running_tasks(st):
        hb = t.get("last_heartbeat") or t.get("started_at") or now_ms()
        idle = int((now_ms() - hb) / 1000)
        if idle >= rc:
            level, action = "危险", f"已接近自动结束（{rc}s）→ 立刻续一拍；救不回来就产出落盘 + finish/--fail 修正账本"
        elif idle >= kp:
            level, action = "提醒", f"超过保活间隔（{kp}s）→ sessions_send 打一拍让它继续"
        else:
            level, action = "正常", "—"
        rows.append({"id": t["id"], "title": t["title"], "session": t["session"],
                     "idle_s": idle, "level": level, "action": action})
    if a.json:
        out({"stale_risk": any(r["level"] == "危险" for r in rows), "rows": rows}, True)
        if any(r["level"] == "危险" for r in rows):
            sys.exit(3)
        return
    if not rows:
        print("没有运行中的任务")
        return
    print(f"保活巡检 @ {now_h()}  （间隔{ kp}s / 回收{rc}s）")
    for r in rows:
        print(f"  [{r['level']}] {r['id']} {r['title']}  闲置 {r['idle_s']}s  {r['action']}")
        if r["session"]:
            print(f"       ping: sessions_send(sessionKey={r['session']}, message='继续')")
    if any(r["level"] == "危险" for r in rows):
        sys.exit(3)


def cmd_status(a):
    st = load_state(a.project)
    cfg = load_config()
    counts = {}
    for t in st["tasks"].values():
        counts[t["status"]] = counts.get(t["status"], 0) + 1
    data = {"project": st["project"], "goal": st["goal"],
            "max_concurrency": cfg["max_concurrency"],
            "running": len(running_tasks(st)), "counts": counts}
    if a.json:
        out(data, True)
        return
    print(f"项目 {st['project']} — {st['goal']}")
    print(f"并发 {len(running_tasks(st))}/{cfg['max_concurrency']}   " +
          "  ".join(f"{k}:{v}" for k, v in sorted(counts.items())))


def cmd_rm(a):
    st = load_state(a.project)
    if a.id not in st["tasks"]:
        die(f"任务 {a.id} 不存在")
    kids = [i for i, t in st["tasks"].items() if t["parent"] == a.id]
    if kids and not a.force:
        die(f"{a.id} 还有子任务 {','.join(kids)}，先删子任务或 --force")
    del st["tasks"][a.id]
    st["order"] = [i for i in st["order"] if i != a.id]
    save_state(st)
    out({"ok": True, "removed": a.id}, a.json, human=f"- {a.id} 已删")


def cmd_config(a):
    cfg = load_config()
    if a.action == "show":
        out(cfg, True)
        return
    if a.action == "set":
        if a.key not in cfg:
            die(f"未知配置项 {a.key}（可用: {', '.join(cfg)}）")
        old = cfg[a.key]
        if a.key != "queue_policy":
            try:
                cfg[a.key] = int(a.value)
            except ValueError:
                die(f"{a.key} 需要整数")
        else:
            cfg[a.key] = a.value
        save_config(cfg)
        out({"ok": True, "key": a.key, "old": old, "new": cfg[a.key]}, a.json,
            human=f"config {a.key}: {old} → {cfg[a.key]}")
        return


def main():
    # --json 可以放在子命令前后任意位置
    if "--json" in sys.argv[1:]:
        sys.argv = [sys.argv[0], "--json"] + [x for x in sys.argv[1:] if x != "--json"]
    ap = argparse.ArgumentParser(description="任务执行规范调度器")
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init"); p.add_argument("--project", default="default")
    p.add_argument("--goal", required=True); p.add_argument("--queue-policy", default="priority", choices=["priority", "fifo"])
    p.set_defaults(fn=cmd_init)

    p = sub.add_parser("add"); p.add_argument("--project", default="default")
    p.add_argument("--title", required=True); p.add_argument("--parent")
    p.add_argument("--priority", default="normal", choices=list(PRIO)); p.add_argument("--deps")
    p.add_argument("--est"); p.add_argument("--note"); p.set_defaults(fn=cmd_add)

    p = sub.add_parser("plan"); p.add_argument("--project", default="default"); p.set_defaults(fn=cmd_plan)

    p = sub.add_parser("next"); p.add_argument("--project", default="default")
    p.add_argument("--n", type=int, default=9); p.set_defaults(fn=cmd_next)

    p = sub.add_parser("start"); p.add_argument("id"); p.add_argument("--project", default="default")
    p.add_argument("--session"); p.add_argument("--force", action="store_true"); p.set_defaults(fn=cmd_start)

    p = sub.add_parser("finish"); p.add_argument("id"); p.add_argument("--project", default="default")
    p.add_argument("--fail"); p.add_argument("--note"); p.set_defaults(fn=cmd_finish)

    p = sub.add_parser("insert"); p.add_argument("id"); p.add_argument("--project", default="default")
    p.add_argument("--mode", default="head", help="head | after:<id> | preempt"); p.set_defaults(fn=cmd_insert)

    p = sub.add_parser("heartbeat"); p.add_argument("id"); p.add_argument("--project", default="default")
    p.set_defaults(fn=cmd_heartbeat)

    p = sub.add_parser("tick"); p.add_argument("--project", default="default"); p.set_defaults(fn=cmd_tick)

    p = sub.add_parser("status"); p.add_argument("--project", default="default"); p.set_defaults(fn=cmd_status)

    p = sub.add_parser("rm"); p.add_argument("id"); p.add_argument("--project", default="default")
    p.add_argument("--force", action="store_true"); p.set_defaults(fn=cmd_rm)

    p = sub.add_parser("config"); p.add_argument("action", choices=["show", "set"])
    p.add_argument("key", nargs="?"); p.add_argument("value", nargs="?"); p.set_defaults(fn=cmd_config)

    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        os._exit(0)
