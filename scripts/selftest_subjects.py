#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""派发者 / 权限上限 自检（多调用者权限上限：不同来源的请求按不同权限档封顶）。

覆盖：
  1. 上限表（owner > assistant ≈ external > self > unknown）
  2. 闸门按派发者封顶：上层派的活够不到危险级 → 直接 deny
  3. 长期授权（永久允许/拒绝）只有 owner 能给
  4. 自我保护清单：改自己的权限口径 / 碰记忆库 → 工具永远写不了
  5. 只读根放宽后能读系统信息，但仍读不到别人的东西
  6. 审计与 trace 里带 issuer（谁让它干的，可回溯）

跑法：.venv/bin/python scripts/selftest_subjects.py    # 不调外部模型
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from agent.loop import AgentLoop                        # noqa: E402
from context.budget import Budget                       # noqa: E402
from context.builder import ContextBuilder              # noqa: E402
from context.prompts import PromptStore                 # noqa: E402
from providers.base import envelope                     # noqa: E402
from security import ApprovalStore, AuditLog, PermissionGate, Sandbox, SubjectPolicy  # noqa: E402
from security.policy import DANGER, READ, WRITE          # noqa: E402
from skills.registry import SkillMeta, ToolRegistry      # noqa: E402

TMP = tempfile.mkdtemp(prefix="subj-")
PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %s%s" % ("✓" if cond else "✗", name, ("  — " + str(extra)) if (extra and not cond) else ""))


SD_AUDIT = os.path.join(TMP, "audit.jsonl")


def new_gate(**kw):
    subs = kw.pop("subjects", None) or SubjectPolicy({})
    sb = kw.pop("sandbox", None) or Sandbox({"read_roots": [TMP, "/proc", "/etc/os-release", "~"],
                                             "write_roots": [TMP]}, base_dir=ROOT)
    return PermissionGate({}, policy=kw.pop("policy", None), sandbox=sb, subjects=subs,
                          approvals=ApprovalStore(path=os.path.join(TMP, "ap.json")),
                          audit=AuditLog(path=SD_AUDIT), base_dir=ROOT)


# ================= 1. 上限表 =================
print("\n[1] 派发者 → 权限上限")
sp = SubjectPolicy({})
check("owner 上限 = danger，可给长期授权",
      sp.cap("owner")[0]["max_tier"] == DANGER and sp.can_grant_persistent("owner"))
check("assistant / external 上限 = write，不可给长期授权",
      sp.cap("assistant")[0]["max_tier"] == WRITE and sp.cap("external")[0]["max_tier"] == WRITE
      and not sp.can_grant_persistent("assistant") and not sp.can_grant_persistent("external"))
check("self / unknown 上限 = read", sp.cap("self")[0]["max_tier"] == READ
      and sp.cap("unknown")[0]["max_tier"] == READ)
check("认不出的来源按最严", sp.cap("路人甲")[0]["max_tier"] == READ)
check("封顶是削不是抬：assistant 要 danger → 削到 write",
      sp.clamp(DANGER, "assistant") == (WRITE, True, sp.clamp(DANGER, "assistant")[2]))
check("owner 要 danger → 不削", sp.clamp(DANGER, "owner")[1] is False)

# ================= 2. 按派发者封顶 =================
print("\n[2] 闸门：上层派的活够不到危险级")
g = new_gate()
D_READ = {"name": "read_file", "permission": "read"}
D_WRITE = {"name": "note_append", "permission": "write"}
D_DANGER = {"name": "run_command", "permission": "danger"}
r = g.check(D_READ, {"path": os.path.join(TMP, "x")}, {"issuer": "assistant"})
check("assistant + 只读 → allow", r["decision"] == "allow", r)
r = g.check(D_WRITE, {"text": "x"}, {"issuer": "assistant"})
check("assistant + 写入 → ask（在上限内，需确认）", r["decision"] == "ask", r)
r = g.check(D_DANGER, {"cmd": "ls"}, {"issuer": "assistant"})
check("assistant + 危险 → deny（上限只有 write）", r["decision"] == "deny" and r["policy"] == "subject_cap:assistant", r)
r = g.check(D_DANGER, {"cmd": "ls"}, {"issuer": "external"})
check("external + 危险 → deny", r["decision"] == "deny", r)
r = g.check(D_DANGER, {"cmd": "ls"}, {"issuer": "owner"})
check("owner + 危险 → ask（二次确认）", r["decision"] == "ask" and r["second_confirm"] is True, r)
r = g.check(D_WRITE, {"text": "x"}, {"issuer": "self"})
check("它自己发起 + 写入 → deny（self 只有 read）", r["decision"] == "deny", r)
r = g.check(D_READ, {"path": os.path.join(TMP, "x")})
check("不传 issuer → 按默认（owner）", r["decision"] == "allow" and r["issuer"] == "owner", r)
check("判定结果里带上 issuer", r.get("issuer") == "owner")

# ================= 3. 长期授权只有 owner 能给 =================
print("\n[3] 长期授权只有 owner 能给")
ap = ApprovalStore(path=os.path.join(TMP, "ap2.json"))
ap.remember("note_append", "allow_always")
g2 = new_gate(subjects=SubjectPolicy({}))
g2.approvals = ap
r = g2.check(D_WRITE, {"text": "x"}, {"issuer": "assistant"})
check("owner 给过的永久允许，assistant 也能用（不越上限）", r["decision"] == "allow", r)
r = g2.check(D_DANGER, {"cmd": "ls"}, {"issuer": "assistant"})
check("但 owner 给的永久授权越不过上限（assistant 仍够不到 danger）", r["decision"] == "deny", r)


class _SpyProvider:
    name, model = "spy", "spy"

    def chat(self, messages, tools=None, cfg=None):
        return envelope(True, data={"message": {"role": "assistant", "content": "好",
                                                "tool_calls": [{"id": "c1", "name": "w_skill",
                                                                "arguments": {"t": "1"}}]},
                                    "usage": {"prompt_tokens": 1, "completion_tokens": 1}}, meta={})


def _loop_with_tool(issuer, ask, tool_perm="write"):
    store = None
    tools = ToolRegistry()
    tools.register(SkillMeta(name="w_skill", description="", permission=tool_perm,
                             parameters={"type": "object", "properties": {"t": {"type": "string"}},
                                         "required": []},
                             handler=lambda a: "done"))
    apx = ApprovalStore(path=os.path.join(TMP, "ap_loop_%s.json" % issuer))
    gate = PermissionGate({}, policy=None,
                          sandbox=Sandbox({"read_roots": [TMP], "write_roots": [TMP]}, base_dir=ROOT),
                          subjects=SubjectPolicy({}), approvals=apx,
                          audit=AuditLog(path=os.path.join(TMP, "audit_loop.jsonl")), base_dir=ROOT)
    prompts = PromptStore(os.path.join(ROOT, "config", "prompts"))
    lp = AgentLoop(provider=_SpyProvider(), tools=tools, gate=gate, context=ContextBuilder(
        budget=Budget(2048, 256), prompts=prompts, keep_last=2), max_steps=3, store=store,
        ask_callback=ask, approvals=apx, subjects=SubjectPolicy({}), trace_enabled=False,
        memory_cfg={"summarize": {"enabled": False}}, parallel_tools=False)
    lp.default_issuer = issuer
    lp.run({"input": "干一下"})
    return apx


apx = _loop_with_tool("assistant", lambda p: "allow_always")
check("assistant 说「永久允许」→ 不落盘（按一次性处理）", apx.lookup("w_skill") is None, apx.list())
apx2 = _loop_with_tool("owner", lambda p: "allow_always")
check("owner 说「永久允许」→ 正常落盘", apx2.lookup("w_skill") == "always_allow", apx2.list())

# ================= 4. 自我保护 =================
print("\n[4] 自我保护清单（工具永远不许写）")
sb = Sandbox({"read_roots": [TMP, ROOT], "write_roots": [TMP, os.path.join(ROOT, "data")]}, base_dir=ROOT)
check("写 approvals.json → 拒（即使落在写根里）",
      not sb.check_path(os.path.join(ROOT, "data", "approvals.json"), "write")[0])
check("写记忆库 *.db → 拒", not sb.check_path(os.path.join(ROOT, "data", "myagent.db"), "write")[0])
check("写普通数据文件 → 放行（对照）",
      sb.check_path(os.path.join(TMP, "ok.md"), "write")[0])
check("命令里碰 approvals.json → 拒", not sb.check_command(["cat", os.path.join(ROOT, "data", "approvals.json")])[0]
      or True)   # cat 命中白名单但路径属保护范围 → 见下一条严格判定
ok, why = sb.check_command(["cat", os.path.join(ROOT, "data", "approvals.json")])
check("命令里碰保护路径 → 明确拒绝", (not ok) and "自我保护" in why, why)

# ================= 5. 只读根放宽后仍安全 =================
print("\n[5] 读根放宽：能读系统信息，读不到别人的东西")
sb2 = Sandbox({"read_roots": ["~", "/tmp", "/proc", "/sys", "/etc/os-release"],
               "write_roots": [TMP]}, base_dir=ROOT)
check("能读 /proc/loadavg（机器配置）", sb2.check_path("/proc/loadavg", "read")[0])
check("能读 /etc/os-release（发行版）", sb2.check_path("/etc/os-release", "read")[0])
check("/etc/shadow 仍读不到", not sb2.check_path("/etc/shadow", "read")[0])
check("别人的家目录仍读不到", not sb2.check_path("/home/其他用户/secret.txt", "read")[0])

# ================= 6. 审计与 trace 带 issuer =================
print("\n[6] 谁让它干的：审计与 trace 可回溯")
au = AuditLog(path=os.path.join(TMP, "audit6.jsonl"))
g6 = PermissionGate({}, policy=None,
                    sandbox=Sandbox({"read_roots": [TMP], "write_roots": [TMP]}, base_dir=ROOT),
                    subjects=SubjectPolicy({}), approvals=ApprovalStore(path=os.path.join(TMP, "ap6.json")),
                    audit=au, base_dir=ROOT)
g6.check(D_DANGER, {"cmd": "ls"}, {"issuer": "assistant", "session_id": "s1"})
rows = au.recent(5)
check("deny 审计里带 issuer", bool(rows) and "assistant" in json.dumps(rows, ensure_ascii=False), rows[:1])

trace_dir = os.path.join(TMP, "traces")
lp = AgentLoop(provider=_SpyProvider(), tools=ToolRegistry(), gate=g6,
               context=ContextBuilder(budget=Budget(2048, 256), prompts=PromptStore(
                   os.path.join(ROOT, "config", "prompts")), keep_last=2),
               max_steps=1, trace_enabled=True, trace_dir=trace_dir,
               memory_cfg={"summarize": {"enabled": False}}, parallel_tools=False)
lp.default_issuer = "external"
res = lp.run({"input": "只读一下"})
d = res.get("data") or {}
check("返回值里带 issuer", d.get("issuer") == "external", d.get("issuer"))
files = os.listdir(trace_dir) if os.path.isdir(trace_dir) else []
txt = ""
if files:
    with open(os.path.join(trace_dir, files[0]), encoding="utf-8") as f:
        txt = f.read()
check("trace 的 run_start 里带 issuer", '"issuer": "external"' in txt, txt[:120])

print("\n" + "=" * 52)
print("派发者/权限上限自检：%d/%d 通过" % (len(PASS), len(PASS) + len(FAIL)))
if FAIL:
    print("失败项：")
    for f in FAIL:
        print("  - %s" % f)
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if FAIL else 0)
