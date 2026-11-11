#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dispatch.py — 任务执行规范的调度器（双通道并发闸 + 任务树 + 插队队列 + 保活）

用途：调度 sub-agent / ACP 会话时，把「任务细分、并发上限、插队、保活」落到一个
可读写的状态文件上，避免拍脑袋并发、避免会话闲置被中转站回收。

套餐是「4 个快速会话 + 4 个慢速会话」，但**会话快/慢无法指定**，只能按先手顺序出场：
先到先得，快速槽先填满，再落到慢速槽。所以调度器只做两件事：
  1) 把「快速/慢速」当两条独立通道发牌（各 4 槽），不超发；
  2) 账本里记下推定的通道 + 入场序号；实际通道不符时用 relane 校正。

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
        "fast_slots": 4,          # 快速会话通道槽位
        "slow_slots": 4,          # 慢速会话通道槽位
        "idle_reclaim_seconds": 120,
        "keepalive_interval_seconds": 60,
        "queue_policy": "priority",  # priority | fifo
    }
    raw = {}
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        return defaults
    if ("fast_slots" not in raw and "slow_slots" not in raw
            and "max_concurrency" in raw):
        # 旧版单通道配置：整个并发额度都算快速通道
        defaults["fast_slots"] = int(raw.pop("max_concurrency"))
        defaults["slow_slots"] = 0
    defaults.update({k: v for k, v in raw.items() if k in defaults})
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
    """空闲槽总数（两个通道相加）。"""
    _, _, free = lane_usage(st, cfg)
    return free["fast"] + free["slow"]


def lanes_cfg(cfg):
    """通道槽位配置 → {'fast': n, 'slow': m}"""
    return {
        "fast": max(0, int(cfg.get("fast_slots", 0))),
        "slow": max(0, int(cfg.get("slow_slots", 0))),
    }


def total_capacity(cfg):
    L = lanes_cfg(cfg)
    return L["fast"] + L["slow"]


def task_lane(t):
    """通道：会话没法指定快/慢，账本只记录“推定通道”（先手顺序入场）。
    旧账本里的任务没有 lane 字段，一律按快速通道计。"""
    return t.get("lane") or "fast"


def lane_usage(st, cfg):
    """→ (槽位上限, 占用, 空闲)，按 fast/slow 分开算。"""
    L = lanes_cfg(cfg)
    use = {"fast": 0, "slow": 0}
    for t in running_tasks(st):
        lane = task_lane(t)
        use[lane] = use.get(lane, 0) + 1
    free = {k: max(0, L[k] - use[k]) for k in ("fast", "slow")}
    return L, use, free


def plan_lanes(st, cfg, n):
    """按先手顺序给候选任务预分配通道：快速槽先填满，再进慢速槽。
    会话本身无法指定快/慢，这里只是按“先到先得”推定谁会拿到哪条通道。"""
    L, _, free = lane_usage(st, cfg)
    sim = dict(free)
    picks = []
    taken = {"fast": used_slot_nos(st, "fast"), "slow": used_slot_nos(st, "slow")}
    for t in ready_tasks(st)[:n]:
        if sim["fast"] > 0:
            lane = "fast"
            sim["fast"] -= 1
        elif sim["slow"] > 0:
            lane = "slow"
            sim["slow"] -= 1
        else:
            break
        no = free_slot_no(st, lane, taken[lane])
        taken[lane].add(no)
        picks.append({"id": t["id"], "title": t["title"], "priority": t["priority"],
                      "deps": t["deps"], "lane": lane, "slot": f"{lane}#{no}"})
    return picks, free


def lane_summary(L, use):
    return (f"快速 {use['fast']}/{L['fast']}  慢速 {use['slow']}/{L['slow']}"
            f"  共 {use['fast'] + use['slow']}/{L['fast'] + L['slow']}")


def used_slot_nos(st, lane):
    used = set()
    for t in running_tasks(st):
        if task_lane(t) != lane:
            continue
        try:
            used.add(int(str(t.get("slot", "")).split("#", 1)[1]))
        except (IndexError, ValueError):
            pass
    return used


def free_slot_no(st, lane, used=None):
    """取该通道里最小的空闲槽编号（会话结束后槽位可以复用）。"""
    taken = used if used is not None else used_slot_nos(st, lane)
    n = 1
    while n in taken:
        n += 1
    return n


# ---------- 命令 ----------

def cmd_init(a):
    st = {
        "project": a.project,
        "goal": a.goal,
        "queue_policy": a.queue_policy,
        "created_at": now_ms(),
        "updated_at": now_ms(),
        "seq": 0,
        "arrival_seq": 0,   # 入场序号：会话快/慢无法指定，按先手顺序入场
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
    L, use, free = lane_usage(st, cfg)
    picks, _ = plan_lanes(st, cfg, 99)
    if a.json:
        out({"goal": st["goal"],
             "lanes": {"fast": L["fast"], "slow": L["slow"]},
             "usage": use, "free": free, "max_concurrency": total_capacity(cfg),
             "running": [t["id"] for t in running_tasks(st)],
             "next": [p["id"] for p in picks],
             "tasks": st["tasks"]}, True)
        return
    print(f"目标: {st['goal']}")
    print(f"通道: {lane_summary(L, use)}  空闲: 快速 {free['fast']} / 慢速 {free['slow']}")
    print(f"回收线: {cfg['idle_reclaim_seconds']}s  保活间隔: {cfg['keepalive_interval_seconds']}s")
    print("（会话快/慢无法指定，按先手顺序入场：先填快速，再进慢速）")
    print("-" * 48)
    _print_tree(st, roots, children)
    print("-" * 48)
    if picks:
        print("下一步可跑: " + ", ".join(f"{p['id']} {p['title']}({p['slot']})" for p in picks))
    else:
        print("下一步可跑: （无 / 通道已满）")


def cmd_next(a):
    st = load_state(a.project)
    cfg = load_config()
    L, use, free = lane_usage(st, cfg)
    picks, _ = plan_lanes(st, cfg, a.n)
    if a.json:
        out({"free_slots": free["fast"] + free["slow"], "free": free,
             "usage": use, "lanes": L, "picks": picks}, True)
    else:
        if free["fast"] + free["slow"] <= 0:
            print(f"通道已满（{lane_summary(L, use)}），先等 finish 或 insert --mode preempt")
        elif not picks:
            print("没有可跑任务（依赖未满足或队列空）")
        else:
            print(f"空闲: 快速 {free['fast']} / 慢速 {free['slow']}，分派 {len(picks)} 个：")
            for p in picks:
                print(f"  → {p['id']} {p['title']}  ({p['priority']})  → {p['slot']}")
            if picks[-1]["lane"] == "slow":
                print("   注: 快速通道已满，这些会话会按先手顺序落在慢速通道")
            print("开跑: dispatch.py start <id> --session <sessionKey>")



def cmd_start(a):
    st = load_state(a.project)
    cfg = load_config()
    L, use, free = lane_usage(st, cfg)
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
    if free["fast"] + free["slow"] <= 0 and not a.force:
        die(f"通道已满（{lane_summary(L, use)}）。"
            f"等 finish，或 insert --mode preempt 腾槽，或 --force 硬开（可能超限被中转站限流）")
    # 会话无法指定快/慢：按先手顺序入场，先占快速槽，满了再进慢速槽
    lane = "fast" if free["fast"] > 0 else "slow"
    slot_no = free_slot_no(st, lane)
    st["arrival_seq"] = int(st.get("arrival_seq", 0)) + 1
    t["status"] = "running"
    t["session"] = a.session
    t["lane"] = lane
    t["slot"] = f"{lane}#{slot_no}"
    t["arrival"] = st["arrival_seq"]
    t["started_at"] = now_ms()
    t["last_heartbeat"] = now_ms()
    t.pop("_jump", None)
    save_state(st)
    _, use2, free2 = lane_usage(st, cfg)
    notes = []
    if lane == "slow":
        notes.append("快速通道已满，本会话按先手顺序进了慢速通道")
    elif free["fast"] == 1:
        notes.append("刚吃完最后一个快速槽，后面的会话会落到慢速通道")
    if free2["fast"] + free2["slow"] == 0:
        notes.append("通道已满，先别开新会话；等 finish 放牌")
    out({"ok": True, "id": a.id, "session": a.session, "lane": lane, "slot": t["slot"],
         "arrival": t["arrival"], "usage": use2, "free": free2,
         "max_concurrency": total_capacity(cfg), "notes": notes}, a.json,
        human=(f"▶ {a.id} 开始 → {t['slot']}（第 {t['arrival']} 个入场）  "
               f"占用: {lane_summary(L, use2)}" + ("\n   注: " + "；".join(notes) if notes else "")))


def cmd_relane(a):
    """校正通道账本：会话快/慢无法指定，平台实际给的通道可能和推定不一样。"""
    st = load_state(a.project)
    cfg = load_config()
    L, use, free = lane_usage(st, cfg)
    t = st["tasks"].get(a.id)
    if not t:
        die(f"任务 {a.id} 不存在")
    if t["status"] != "running":
        die(f"{a.id} 不在运行中（只有 running 任务有通道），当前: {t['status']}")
    old = task_lane(t)
    if old == a.lane:
        out({"ok": True, "id": a.id, "lane": old, "msg": "通道未变"}, a.json,
            human=f"{a.id} 已经在 {old} 通道，无需改动")
        return
    if free[a.lane] <= 0 and not a.force:
        die(f"{a.lane} 通道已满（{lane_summary(L, use)}），改过去会超限；--force 可硬改")
    t["lane"] = a.lane
    t["slot"] = f"{a.lane}#{free_slot_no(st, a.lane)}"
    save_state(st)
    _, use2, free2 = lane_usage(st, cfg)
    out({"ok": True, "id": a.id, "old": old, "new": a.lane, "slot": t["slot"],
         "usage": use2, "free": free2}, a.json,
        human=f"⇄ {a.id} 通道 {old} → {a.lane}（{t['slot']}）  占用: {lane_summary(L, use2)}")


def cmd_finish(a):
    st = load_state(a.project)
    cfg = load_config()
    t = st["tasks"].get(a.id)
    if not t:
        die(f"任务 {a.id} 不存在")
    was_running = t["status"] == "running"
    lane = task_lane(t) if was_running else None
    t["status"] = "failed" if a.fail else "done"
    t["fail_reason"] = a.fail
    t["finished_at"] = now_ms()
    save_state(st)
    L, use, free = lane_usage(st, cfg)
    freed = f"，释放 {lane} 槽" if lane else ""
    out({"ok": True, "id": a.id, "status": t["status"], "note": a.note,
         "lane": lane, "usage": use, "free": free}, a.json,
        human=f"{STATUS_ICON.get(t['status'],'?')} {a.id} {t['status']}" + (f": {a.fail}" if a.fail else "")
              + freed + f"  （{lane_summary(L, use)}）")


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
            _, _, free = lane_usage(st, cfg)
            msg = (f"还有 {free['fast'] + free['slow']} 个空闲槽"
                   f"（快速 {free['fast']} / 慢速 {free['slow']}），无需抢占；直接 start {a.id} 即可")
            out({"ok": True, "msg": msg}, a.json, human=msg)
            return
        # 选可牺牲者：优先冻结快速通道（腾出的快槽能直接给急活）、非 urgent、没有被其它运行任务依赖
        dep_of = {}
        for x in st["tasks"].values():
            for d in x["deps"]:
                dep_of.setdefault(d, []).append(x["id"])
        running_ids = {x["id"] for x in running}
        victims = [x for x in running
                   if x["priority"] != "urgent" and not (set(dep_of.get(x["id"], [])) & running_ids)]
        victims.sort(key=lambda x: (task_lane(x) != "fast", -PRIO.get(x["priority"], 2), x["started_at"] or 0))
        if not victims:
            die("所有运行中任务都不可抢占（urgent 或被依赖）")
        v = victims[0]
        v_lane = task_lane(v)
        v["status"] = "pending"
        v["started_at"] = None
        v["last_heartbeat"] = None
        v["frozen_from"] = v.get("slot")
        st["order"].remove(a.id)
        st["order"].insert(0, a.id)
        t["_jump"] = -1
        save_state(st)
        out({"ok": True, "id": a.id, "mode": "preempt", "victim": v["id"], "victim_lane": v_lane,
             "msg": f"已冻结 {v['id']}（{v.get('slot')}，回到 pending 排队），腾出 1 个{v_lane}槽给 {a.id}",
             "todo": f"确认 {v['id']} 的 session 已停（subagents kill），再 start {a.id}"}, a.json,
            human=(f"⇥ {a.id} 抢占成功：冻结 {v['id']}（{v.get('slot')} → 回 pending），腾出 1 个{v_lane}槽。\n"
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
                     "lane": task_lane(t), "slot": t.get("slot"),
                     "idle_s": idle, "level": level, "action": action})
    if a.json:
        out({"stale_risk": any(r["level"] == "危险" for r in rows), "rows": rows}, True)
        if any(r["level"] == "危险" for r in rows):
            sys.exit(3)
        return
    if not rows:
        print("没有运行中的任务")
        return
    L, use, _ = lane_usage(st, cfg)
    print(f"保活巡检 @ {now_h()}  （间隔{ kp}s / 回收{rc}s / {lane_summary(L, use)}）")
    for r in rows:
        print(f"  [{r['level']}] {r['id']} {r['title']}  {r.get('slot') or '?'}  闲置 {r['idle_s']}s  {r['action']}")
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
    L, use, free = lane_usage(st, cfg)
    data = {"project": st["project"], "goal": st["goal"],
            "lanes": L, "usage": use, "free": free,
            "max_concurrency": total_capacity(cfg),
            "running": len(running_tasks(st)), "counts": counts}
    if a.json:
        out(data, True)
        return
    print(f"项目 {st['project']} — {st['goal']}")
    print(f"通道 {lane_summary(L, use)}   空闲 快速{free['fast']}/慢速{free['slow']}")
    print("状态  " + "  ".join(f"{k}:{v}" for k, v in sorted(counts.items())))


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
        key = a.key
        if key == "max_concurrency":
            # 兼容旧用法：当成“单通道总额度”，全部算快速槽
            key = "fast_slots"
        if key not in cfg:
            die(f"未知配置项 {a.key}（可用: {', '.join(cfg)}）")
        old = cfg[key]
        if key != "queue_policy":
            try:
                cfg[key] = int(a.value)
            except ValueError:
                die(f"{key} 需要整数")
        else:
            cfg[key] = a.value
        if a.key == "max_concurrency":
            cfg["slow_slots"] = 0
        save_config(cfg)
        L = lanes_cfg(cfg)
        out({"ok": True, "key": a.key, "old": old, "new": cfg[key],
             "capacity": total_capacity(cfg)}, a.json,
            human=f"config {a.key}: {old} → {cfg[key]}（总容量 快速{L['fast']} + 慢速{L['slow']}）")
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

    p = sub.add_parser("relane"); p.add_argument("id"); p.add_argument("--project", default="default")
    p.add_argument("--lane", required=True, choices=["fast", "slow"])
    p.add_argument("--force", action="store_true"); p.set_defaults(fn=cmd_relane)

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
