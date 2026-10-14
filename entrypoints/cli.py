#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CLI 入口：交互 REPL + 单次命令模式。

用法：
  python3 -m entrypoints.cli                     交互
  python3 -m entrypoints.cli -p "算一下 3**12"    单次
  python3 -m entrypoints.cli -P local -p "你好"   指定 provider
  python3 -m entrypoints.cli --resume <trace_id>  接着上次中断的任务继续（会话可续）

M5 新增命令：/trace [trace_id]  /cost  /cancel
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.factory import build_agent, reload_skills   # noqa: E402
from runtime.cancel import CancelToken                 # noqa: E402
from runtime.config import load_config                 # noqa: E402
from runtime.cost import format_cost                   # noqa: E402
from runtime.log import new_trace_id                   # noqa: E402
from runtime.text import sanitize                      # noqa: E402

DIM, BOLD, RESET = "\033[2m", "\033[1m", "\033[0m"

COMMANDS = (
    "命令：/provider <名>  /providers  /model <id>  /tools  /skills  /reload  /mcp [reload]  "
    "/policy  /subject  /sandbox  /approvals [revoke <名>]  /audit [n]  /health  /budget  /session  /sessions  "
    "/memory [关键词]  /summary  /compact  /exp <主题> <正文>  /trace [trace_id]  /cost  /cancel  "
    "/api  /quit"
)


def _renderer(trace_id):
    def on_event(ev):
        t = ev["type"]
        p = ev.get("payload") or {}
        if t == "tool_call":
            print(f"{DIM}  ⚙ 调用 {p.get('name')} {json.dumps(p.get('arguments'), ensure_ascii=False)}{RESET}")
        elif t == "tool_result":
            head = str(p.get("content", ""))[:120].replace("\n", " ")
            print(f"{DIM}    ↳ {'ok' if p.get('ok') else 'fail'}: {head}{RESET}")
        elif t == "thought" and p.get("text"):
            print(f"{DIM}  · 思考 {len(p['text'])} 字{RESET}")
            print(f"{BOLD}{p['text']}{RESET}")
        elif t == "state_change":
            print(f"{DIM}    · 状态 {p.get('from')} → {p.get('to')}（{p.get('reason')}）{RESET}")
        elif t == "error":
            e = p.get("error") or {}
            print(f"  ! {e.get('code')} [{e.get('category')}]: {e.get('message')}")
    return on_event


def _ask(prompt: dict) -> str:
    args = json.dumps(prompt.get("args"), ensure_ascii=False)
    tier = prompt.get("tier")
    print(f"\n  ⚠ 需要确认（级别 {tier}）：{prompt.get('name')} {args}")
    print("    理由：%s" % prompt.get("reason"))
    if prompt.get("second_confirm"):
        try:
            ans = input("    危险操作 · 二次确认，输入 allow 才放行：").strip().lower()
        except EOFError:
            return "deny_once"
        return "allow_once" if ans == "allow" else "deny_once"
    try:
        ans = input("    [y]本次允许 [a]永久允许 [n]拒绝 [N]永久拒绝 > ").strip()
    except EOFError:
        return "deny_once"
    return {"y": "allow_once", "a": "allow_always", "n": "deny_once", "N": "deny_always"}.get(ans, "deny_once")


def _print_health(providers):
    for name, p in providers.items():
        h = p.health()
        d = h.get("data") or {}
        extra = ""
        if "has_key" in d:
            extra = "  key=%s" % ("有" if d["has_key"] else "无")
        if "running" in d:
            extra = "  serve=%s  models=%s" % ("通" if d["running"] else "不通",
                                               ",".join(d.get("models") or []) or "-")
        print(f"  - {name:<8} {d.get('model'):<18} {d.get('base_url')}{extra}")


def _headless_ask(prompt: dict) -> str:
    """单次模式没有真人可问：写入/危险操作一律拒绝，但把话说清楚。"""
    print(f"  （非交互模式：{prompt.get('name')} 需要确认，按拒绝处理）")
    return "deny_once"


def _always_ask(prompt: dict) -> str:
    """单次模式 --yes：所有需确认的操作一律放行（谨慎使用）。"""
    print(f"  （--yes：放行 {prompt.get('name')}）")
    return "allow_once"


def _print_run_result(r: dict) -> None:
    d = r.get("data") or {}
    print(f"\n{BOLD}[{d.get('status')}]{RESET} {d.get('answer')}")
    u = d.get("usage") or {}
    m = r.get("meta") or {}
    print(f"{DIM}  steps={d.get('steps')} tokens={u.get('prompt_tokens')}+{u.get('completion_tokens')} "
          f"费用={format_cost(d.get('cost'))} budget_left={d.get('budget_left')} "
          f"phase={d.get('phase')} trace={m.get('trace_id')}{RESET}")
    cs = d.get("context_stats") or {}
    if cs:
        fam = cs.get("by_family") or {}
        print(f"{DIM}  上下文：用 {cs.get('used_tokens')} tok "
              f"(系统{fam.get('system', 0)}/记忆{fam.get('memory', 0)}/工具{fam.get('tools', 0)}/历史{fam.get('history', 0)}) "
              f"丢弃 {cs.get('dropped')} 单元 {cs.get('units_kept')}/{cs.get('units_total')} "
              f"提示词 {cs.get('prompt_version')}{RESET}")
    if not r.get("ok") and r.get("error"):
        e = r["error"]
        print(f"  ! {e.get('code')} [{e.get('category')}]: {e.get('message')}")
        if e.get("remediation"):
            print(f"{DIM}    补救：{e['remediation']}{RESET}")


def _make_runner(loop, line_q=None):
    """造一个能中断的 run_one：line_q 给了就走后台线程 + 轮询 /cancel。"""
    state = {"token": None}

    def run_one(text: str) -> dict:
        tid = new_trace_id()
        token = CancelToken()
        state["token"] = token
        unsub = loop.bus.subscribe(_renderer(tid))
        try:
            if line_q is None:
                r = loop.run({"input": text}, {"trace_id": tid, "cancel_token": token})
            else:
                box = {}

                def work():
                    try:
                        box["r"] = loop.run({"input": text},
                                            {"trace_id": tid, "cancel_token": token})
                    except Exception as e:  # noqa: BLE001
                        box["e"] = e

                th = threading.Thread(target=work, daemon=True)
                th.start()
                while th.is_alive():
                    try:
                        s = line_q.get(timeout=0.1)
                    except queue.Empty:
                        continue
                    except KeyboardInterrupt:
                        token.cancel("ctrl_c")
                        print(f"\n{DIM}  收到 Ctrl-C，已请求中断……{RESET}")
                        continue
                    cmd = "" if s is None else str(s).strip()
                    if s is None:
                        token.cancel("eof")
                    elif cmd in ("/cancel", "\x03"):
                        token.cancel("cli_cancel")
                        print(f"\n{DIM}  已请求中断（{token.reason}），等当前这步收尾……{RESET}")
                    else:
                        print(f"\n{DIM}  （任务运行中，只认 /cancel；这条输入已忽略）{RESET}")
                th.join()
                if box.get("e") is not None:
                    raise box["e"]
                r = box.get("r") or {}
        finally:
            unsub()
            state["token"] = None
        _print_run_result(r)
        return r

    return run_one, state


def _cmd_trace(ctx, arg: str) -> None:
    ts = ctx.get("trace_store")
    tid = arg or (ts.latest_trace_id() if ts else None)
    if not ts or not tid:
        print("  还没有 trace（跑一条任务就有了）")
        return
    recs = ts.load(tid)
    if not recs:
        print("  找不到 trace：%s" % tid)
        return
    s = ts.summarize(recs)
    print(f"  trace={tid} status={s['status']} steps={s['steps']} tools={s['tools']} "
          f"elapsed={s['elapsed_ms']}ms 费用={format_cost(s['cost'])}")
    print(f"  输入：{str(s.get('input') or '')[:80]}")
    for d in s["step_details"]:
        print(f"    step {d['step']} 耗时={d['duration_ms']}ms usage={d['usage']} cost={d['cost']}")
    for d in s["tool_details"]:
        print(f"    tool {d['name']} 耗时={d['latency_ms']}ms ok={d['ok']}"
              + (f" err={d['error']}" if d.get("error") else ""))
    print(f"  文件：{ts.path_for(tid)}")


def _cmd_cost(loop) -> None:
    t = loop.cost_calc.table()
    print(f"  单价（{t['currency']} · {t['unit_label']}）：")
    for row in t["providers"]:
        print(f"    {row['provider']:<10} in={row['input']:<6} out={row['output']:<6} {row['note']}")
    if t.get("notes"):
        print(f"  {DIM}{t['notes']}{RESET}")
    print("  本进程累计：")
    if loop.cost_totals:
        for name, c in loop.cost_totals.items():
            print(f"    {name:<10} {format_cost(c)}")
    else:
        print("    （还没跑过任务）")


def main() -> int:
    ap = argparse.ArgumentParser(description="myagent CLI")
    ap.add_argument("-p", "--prompt", help="单次模式：只跑这一条")
    ap.add_argument("-P", "--provider", help="provider 名（默认取配置 default_provider）")
    ap.add_argument("-m", "--model", help="临时换模型名")
    ap.add_argument("--max-steps", type=int, default=8)
    ap.add_argument("--no-tools", action="store_true", help="不给工具（纯聊天）")
    ap.add_argument("--yes", action="store_true", help="单次模式下永久允许所有需确认的调用（谨慎）")
    ap.add_argument("-s", "--session", help="会话 id（不给就自动新建一个）")
    ap.add_argument("--user", help="用户 id（会话路由用；默认取 memory.user_id）")
    ap.add_argument("--issuer", help="派发者（owner/assistant/external/self）——决定本次任务的权限上限")
    ap.add_argument("--resume", help="按 trace_id 续跑上次被中断的任务（会话可续）")
    args = ap.parse_args()

    cfg = load_config()
    single = bool(args.prompt or args.resume)
    ask = None if not single else (_always_ask if args.yes else _headless_ask)
    ctx = build_agent(cfg, provider_name=args.provider, max_steps=args.max_steps,
                      ask_callback=ask, session_id=args.session)
    providers, tools, loop, store = ctx["providers"], ctx["tools"], ctx["loop"], ctx["store"]
    api_cfg = cfg.get("api") or {}
    user_id = args.user or (cfg.get("memory") or {}).get("user_id") or "default"
    # M7 会话路由：没显式给 -s / --resume 时，CLI 与 API 按 (渠道, 用户) 落到同一条会话（跨入口连续）
    if not loop.session_id and not args.resume:
        try:
            loop.session_id = store.route_session("cli", user_id,
                                                  scope=api_cfg.get("session_scope", "user"))
        except Exception:  # noqa: BLE001 —— 路由坏了退回新建，不影响聊天
            loop.session_id = store.new_session_id()
    if args.no_tools:
        loop.tools = None
    if args.model:
        loop.provider.model = args.model
    if args.issuer:
        loop.default_issuer = args.issuer.strip().lower()

    # ---- M5：按 trace 续跑 ----
    if args.resume:
        ts = ctx.get("trace_store")
        plan = ts.plan_resume(args.resume) if ts else None
        if not plan:
            print("找不到 trace：%s" % args.resume, file=sys.stderr)
            return 2
        if plan.get("session_id"):
            loop.session_id = plan["session_id"]
        print(f"{DIM}恢复 trace={args.resume} 会话={loop.session_id} 上次状态={plan.get('status')}{RESET}")
        if not args.prompt:
            if not plan.get("resumable"):
                print("  这条 trace 已正常结束；要接着聊直接 ./run.sh -s %s" % loop.session_id)
                return 0
            args.prompt = plan.get("resume_prompt") or "请接着上面未完成的任务继续。"

    mcp_n = len([n for n in tools.names() if str(tools.meta(n).source).startswith("mcp:")])
    file_n = len([n for n in tools.names() if str(tools.meta(n).source).startswith("file:")])
    builtin_n = len(tools.names()) - mcp_n - file_n
    print(f"{DIM}myagent · provider={loop.provider.name}/{loop.provider.model} · "
          f"tools={len(tools.names())}（内置 {builtin_n}/技能包 {file_n}/MCP {mcp_n}） · "
          f"派发者={loop.default_issuer} · 会话={loop.session_id} · 配置={cfg.get('_config_path')}{RESET}")

    # ---- 单次模式：同步跑，不轮询 ----
    if args.prompt:
        run_one, _ = _make_runner(loop, line_q=None)
        return 0 if run_one(args.prompt).get("ok") else 1

    # ---- 交互 REPL：读输入的线程 + 可 /cancel 的后台跑 ----
    print(COMMANDS)
    line_q: "queue.Queue" = queue.Queue()

    def _reader():
        while True:
            try:
                s = input()
            except EOFError:
                line_q.put(None)
                return
            except KeyboardInterrupt:
                line_q.put("\x03")
                continue
            line_q.put(s)

    threading.Thread(target=_reader, daemon=True).start()
    run_one, run_state = _make_runner(loop, line_q=line_q)

    while True:
        print("\n你 > ", end="", flush=True)
        try:
            raw = line_q.get()
        except KeyboardInterrupt:
            print()
            return 0
        if raw is None:
            print()
            return 0
        line = sanitize(str(raw)).strip()
        if line == "\x03":
            print("^C")
            continue
        if not line:
            continue
        if line in ("/quit", "/exit", ":q"):
            return 0
        if line == "/providers":
            _print_health(providers)
            continue
        if line == "/health":
            _print_health(providers)
            print(f"  current: {loop.provider.name}/{loop.provider.model}  budget={loop.context_budget}")
            continue
        if line == "/api":
            a = cfg.get("api") or {}
            toks = a.get("tokens") or []
            env_tok = os.environ.get("MASSISTANTGENT_API_TOKEN") or os.environ.get("MASSISTANTGENT_API_TOKENS")
            print(f"  API: http://{a.get('host', '127.0.0.1')}:{a.get('port', 8765)}  "
                  f"session_scope={a.get('session_scope', 'user')}  "
                  f"tokens={len(toks)}" + ("(+env)" if env_tok else ""))
            print("  端点：POST /v1/chat (SSE)  GET /v1/health /v1/tools /v1/sessions")
            print("  启动：.venv/bin/python -m entrypoints.api")
            continue
        if line == "/cancel":
            tok = run_state.get("token")
            if tok is not None and not tok.cancelled:
                tok.cancel("cli_cancel")
                print("  已请求中断……")
            else:
                print("  当前没有正在跑的任务")
            continue
        if line == "/cost":
            _cmd_cost(loop)
            continue
        if line.startswith("/trace"):
            _cmd_trace(ctx, line[6:].strip())
            continue
        if line == "/session":
            info = store.session_info(loop.session_id) or {}
            print(f"  会话 {loop.session_id}：消息 {store.count_messages(loop.session_id)} 条 · "
                  f"通道 {info.get('channel')} · 库 {store.path} (schema v{store.schema_version})")
            continue
        if line == "/sessions":
            for s in store.list_sessions(limit=15):
                print(f"  {s['id']:<16} {s['message_count']:>4} 条  {s['channel'] or '-'}  {(s['title'] or '')[:30]}")
            continue
        if line.startswith("/exp "):
            parts = line[5:].split(maxsplit=1)
            if len(parts) < 2:
                print("  用法：/exp <主题> <经验正文>")
            else:
                p = ctx["procedural"].append_note(parts[0], parts[1])
                print("  已追加到 %s" % p)
            continue
        if line.startswith("/memory"):
            q = line[7:].strip()
            if q:
                for h in store.search(q, k=10):
                    print("  %.3f [%s] %s" % (h["score"], h["type"], h["content"][:60]))
            else:
                rows = store.memories_active(limit=20)
                print("  长期记忆 %d 条：" % len(rows))
                for m in rows:
                    print("    [%s] %s" % (m["type"], m["content"][:60]))
            continue
        if line == "/summary":
            info = store.session_summary(loop.session_id)
            print("  摘要指针 upto_msg_id=%s" % info["upto_msg_id"])
            print("  摘要：%s" % ((info["summary"] or "(还没压过)")[:300]))
            if ctx["procedural"]:
                for it in ctx["procedural"].list():
                    print("  经验文件：%s — %s" % (it["title"], it["summary"][:50]))
            continue
        if line == "/compact":
            print("  %s" % store.compact(loop.session_id, {"keep_last": loop.summarize_keep_last}))
            continue
        if line == "/budget":
            b = ctx["budget"].to_dict()
            q = b["quota"]
            print(f"  预算 total={b['total']} reserve_output={b['reserve_output']} available={b['available']}")
            print(f"  配额 系统={q['system']} 记忆={q['memory']} 工具={q['tools']} 历史={q['history']}")
            pv = ctx["prompts"].render("system", {})
            print(f"  提示词 {pv['version']}  {pv['path'] or '(内置默认)'}"
                  + (f"  缺变量={pv['missing']}" if pv["missing"] else ""))
            print(f"  组装器 keep_last={ctx['context'].keep_last} 工具结果上限={ctx['context'].max_tool_result_chars} 字符")
            continue
        if line == "/tools":
            for t in tools.list():
                src = t["source"].split(":")[0] if t["source"] else "-"
                print(f"  - {t['name']:<22} [{t['permission']:<6}] {src:<7} {t['description'][:50]}")
            continue
        if line == "/skills":
            for t in tools.list({"source": "file:"}):
                refs = ("  参考:%s" % ",".join(t["references"])) if t.get("references") else ""
                print(f"  - {t['name']:<20} [{t['permission']}] {t['path']}")
                print(f"      {t['description'][:80]}{refs}")
            continue
        if line in ("/reload", "/skills reload"):
            rep = reload_skills(ctx)
            sk = rep.get("skills") or {}
            print(f"  技能包：+{sk.get('added')} -{sk.get('removed')} 共{sk.get('total')}")
            if rep.get("errors"):
                print(f"  坏技能：{rep['errors']}")
            if rep.get("mcp"):
                print(f"  MCP：{rep['mcp']}")
            continue
        if line.startswith("/mcp"):
            arg = line[4:].strip()
            if arg == "reload":
                print(f"  {ctx['mcp'].reload()}")
            for r in ctx["mcp"].status():
                print(f"  - {r['server']:<10} alive={r['alive']} tools={r['tools']} "
                      f"{(r['server_info'] or {}).get('name', '')} {r['command'][:40]}"
                      + (f"  ! {r['error']}" if r["error"] else ""))
            continue
        if line == "/policy":
            pol = ctx["policy"]
            aud = pol.audit_tools(tools.list())
            print(f"  分级表：{len(pol.tiers)} 条；漏网 {len(aud['fallback'])} 个；按级别 {aud['by_tier']}")
            if aud["fallback"]:
                print(f"  ⚠ 未登记（按 danger 兜底）：{aud['fallback']}")
            print(f"  默认动作：{pol.actions}  危险二次确认={pol.second_confirm}")
            continue
        if line.startswith("/subject"):
            arg = line[len("/subject"):].strip().lower()
            if arg and ctx.get("subjects") is not None:
                if arg in ctx["subjects"].subjects:
                    loop.default_issuer = arg
                    print("  本次起派发者 = %s（权限上限 %s）" % (arg, ctx["subjects"].cap(arg)[0].get("max_tier")))
                else:
                    print("  没有这个派发者：%s（可选：%s）" % (arg, ", ".join(ctx["subjects"].subjects)))
            print("  当前派发者：%s" % loop.default_issuer)
            if ctx.get("subjects") is not None:
                print("  派发者 → 权限上限：")
                print(ctx["subjects"].describe())
                print("  只有 owner 能授予「永久允许/永久拒绝」这类长期授权。")
            continue
        if line == "/sandbox":
            sb = ctx["sandbox"].to_dict()
            print(f"  开关={sb['enabled']}")
            print(f"  读根：{sb['read_roots']}")
            print(f"  写根：{sb['write_roots']}")
            print(f"  命令白名单：{sb['cmd_allow']}")
            print(f"  自我保护（工具永远不许写）：{sb.get('protect')}")
            continue
        if line.startswith("/approvals"):
            arg = line[len("/approvals"):].strip()
            ap = ctx["approvals"]
            if arg.startswith("revoke "):
                n = arg[7:].strip()
                print(f"  已撤销：{n}" if ap.revoke(n) else f"  没有这个口径：{n}")
            else:
                d = ap.list()
                print(f"  永久允许：{list(d['allow']) or '-'}")
                print(f"  永久拒绝：{list(d['deny']) or '-'}")
                print(f"  存档：{d['path']}（/approvals revoke <名> 撤销）")
            continue
        if line.startswith("/audit"):
            n = line[6:].strip()
            limit = int(n) if n.isdigit() else 12
            for r in ctx["audit"].recent(limit):
                det = str(r.get("detail") or "")[:90]
                print(f"  {r.get('ts')} [{r.get('actor')}/{r.get('target_type')}] "
                      f"{r.get('action')} {r.get('target_id')} approved={r.get('approved')} {det}")
            continue
        if line.startswith("/provider"):
            parts = line.split()
            if len(parts) < 2:
                print("  用法：/provider <名>")
            elif parts[1] not in providers:
                print("  没有这个 provider：%s" % parts[1])
            else:
                loop.provider = providers[parts[1]]
                print("  已切到 %s/%s" % (loop.provider.name, loop.provider.model))
            continue
        if line.startswith("/model"):
            parts = line.split(maxsplit=1)
            if len(parts) == 2:
                loop.provider.model = parts[1].strip()
            print("  当前模型：%s" % loop.provider.model)
            continue
        run_one(line)


if __name__ == "__main__":
    raise SystemExit(main())
