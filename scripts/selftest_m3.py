#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M3 自检：技能包加载/热加载 + 权限三级 + 沙盒 + 审批 + 审计 + MCP 客户端。

跑法：.venv/bin/python scripts/selftest_m3.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from mcpclient import MCPManager                      # noqa: E402
from security import (ApprovalStore, AuditLog, PermissionGate, PermissionPolicy,  # noqa: E402
                      Sandbox)
from security.policy import DANGER, READ, WRITE        # noqa: E402
from skills.loader import SkillLoader, parse_skill_md  # noqa: E402
from skills.registry import SkillMeta, ToolRegistry    # noqa: E402

TMP = tempfile.mkdtemp(prefix="m3test-")
PASS = []
FAIL = []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %s%s" % ("✓" if cond else "✗", name, ("  — " + str(extra)) if (extra and not cond) else ""))


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def new_skill(folder, name, permission="read", entry_script=None, body="说明"):
    d = os.path.join(TMP, folder)
    md = "---\nname: %s\ndescription: %s 的描述\npermission: %s\nentry: scripts/run.py\ntimeout_ms: 5000\ntriggers: [t1, t2]\n---\n\n%s\n" % (
        name, name, permission, body)
    write(os.path.join(d, "SKILL.md"), md)
    if entry_script is not None:
        write(os.path.join(d, "scripts", "run.py"), entry_script)
    return d


OK_SCRIPT = "import json,sys\nreq=json.loads(sys.stdin.readline() or '{}')\nprint(json.dumps({'ok':True,'content':'got '+json.dumps(req.get('args'),ensure_ascii=False)}))\n"

# ================= 1. SKILL.md 解析 =================
print("\n[1] SKILL.md 解析")
meta, body = parse_skill_md("---\nname: x\npermission: write\ntriggers: [a, b, c]\n---\n\n正文说明\n")
check("frontmatter 字段解析", meta.get("name") == "x" and meta.get("permission") == "write")
check("triggers 列表解析", meta.get("triggers") == ["a", "b", "c"], meta.get("triggers"))
check("正文与头部拆开", body.strip() == "正文说明", body)
m2, b2 = parse_skill_md("没有头部\n正文")
check("无 frontmatter 时元数据为空、正文保留", m2 == {} and b2.startswith("没有头部"))

# ================= 2. 目录扫描 =================
print("\n[2] 目录扫描加载")
new_skill("alpha", "alpha_skill", "read", OK_SCRIPT)
new_skill("beta", "beta_skill", "write", OK_SCRIPT)
bad = os.path.join(TMP, "broken")
write(os.path.join(bad, "SKILL.md"), "---\nname: broken\ndescription: 坏技能\nentry: scripts/missing.py\n---\n")
rep = SkillLoader([TMP]).load()
names = sorted(m.name for m in rep["skills"])
check("扫到 2 个技能、坏技能不影响", names == ["alpha_skill", "beta_skill"], names)
check("坏技能被记进 errors", len(rep["errors"]) == 1 and "missing.py" in rep["errors"][0]["reason"], rep["errors"])
am = [m for m in rep["skills"] if m.name == "alpha_skill"][0]
check("元数据（权限/超时/触发词/来源）", am.permission == READ and am.timeout_ms == 5000
      and am.triggers == ["t1", "t2"] and am.source.startswith("file:"))

# ================= 3. 热加载（原子替换） =================
print("\n[3] 热加载")
reg = ToolRegistry()
reg.replace_source("file:", rep["skills"])
check("注册表里有扫描到的技能", set(["alpha_skill", "beta_skill"]).issubset(set(reg.names())))
new_skill("gamma", "gamma_skill", "read", OK_SCRIPT)
rep2 = SkillLoader([TMP]).load()
delta = reg.replace_source("file:", rep2["skills"])
check("新增技能 reload 后立即可用", "gamma_skill" in reg.names() and "gamma_skill" in delta["added"], delta)
shutil.rmtree(os.path.join(TMP, "gamma"))
rep3 = SkillLoader([TMP]).load()
delta3 = reg.replace_source("file:", rep3["skills"])
check("删掉文件夹后 reload 即消失", "gamma_skill" not in reg.names() and "gamma_skill" in delta3["removed"], delta3)
keep = reg.register(SkillMeta(name="manual", description="手工注册", source="builtin"))
reg.replace_source("file:", rep3["skills"])
check("原子替换不动别的来源（builtin 仍在）", "manual" in reg.names())
check("to_schema 出的是合法 JSON Schema", isinstance(reg.to_schema(), list)
      and reg.to_schema()[0]["function"]["name"])

# ================= 4. 权限分级表 =================
print("\n[4] 权限分级表（无漏网）")
pol = PermissionPolicy({})
check("表内命中", pol.classify("read_file")["tier"] == READ and pol.classify("run_command")["tier"] == DANGER)
check("元数据声明兜底", pol.classify("mcp_thing", {"permission": "write"})["tier"] == WRITE)
check("未登记工具按最严（danger）", pol.classify("mystery_tool")["tier"] == DANGER)
check("overrides 可覆盖", PermissionPolicy({"overrides": {"mystery_tool": "read"}}).classify("mystery_tool")["tier"] == READ)
all_metas = [{"name": n, "permission": None} for n in ["read_file", "list_dir", "calc", "now", "http_get"]]
aud = pol.audit_tools(all_metas)
check("内置工具全部有级别、零漏网", aud["clean"] and aud["by_tier"][READ] == 5, aud)

# ================= 5. 权限三级判定 =================
print("\n[5] 权限三级（只读放行 / 写入需确认 / 危险二次确认）")
gate = PermissionGate({}, policy=pol, sandbox=Sandbox({"read_roots": [TMP, "~"], "write_roots": [TMP]}, base_dir=ROOT),
                      approvals=ApprovalStore(path=os.path.join(TMP, "approvals.json")),
                      audit=AuditLog(path=os.path.join(TMP, "audit.jsonl")), base_dir=ROOT)
d_read = gate.check({"name": "read_file", "permission": "read"}, {"path": os.path.join(TMP, "x")})
d_write = gate.check({"name": "note_append", "permission": "write"}, {"text": "hi"})
d_danger = gate.check({"name": "run_command", "permission": "danger"}, {"cmd": "ls"})
check("只读 → allow", d_read["decision"] == "allow", d_read)
check("写入 → ask", d_write["decision"] == "ask", d_write)
check("危险 → ask 且二次确认", d_danger["decision"] == "ask" and d_danger["second_confirm"] is True, d_danger)

# ================= 6. 沙盒 =================
print("\n[6] 沙盒约束")
sb = Sandbox({"read_roots": [TMP], "write_roots": [TMP]}, base_dir=ROOT)
check("读根内放行", sb.check_path(os.path.join(TMP, "a.txt"), "read")[0])
check("读根外拒绝", not sb.check_path("/etc/shadow", "read")[0])
check("写根外拒绝", not sb.check_path("/etc/passwd", "write")[0])
check("`..` 绕不出去", not sb.check_path(os.path.join(TMP, "..", "..", "etc", "shadow"), "read")[0])
check("命令白名单放行", sb.check_command(["ls", "-l"])[0])
check("非白名单命令拒绝", not sb.check_command(["rm", "-rf", "/"])[0])
check("危险片段拒绝", not sb.check_command(["echo", "rm -rf /"])[0])
d_sb = gate.check({"name": "read_file", "permission": "read"}, {"path": "/etc/shadow"})
check("越界读被闸拦下（deny）", d_sb["decision"] == "deny" and d_sb["policy"] == "sandbox", d_sb)

# ================= 7. 审批口径 =================
print("\n[7] 审批钩子（本次 / 永久 / 拒绝）")
apath = os.path.join(TMP, "ap2.json")
ap = ApprovalStore(path=apath)
gate2 = PermissionGate({}, policy=pol, sandbox=Sandbox({"read_roots": [TMP], "write_roots": [TMP]}, base_dir=ROOT),
                       approvals=ap, audit=AuditLog(path=os.path.join(TMP, "audit2.jsonl")), base_dir=ROOT)
ap.remember("note_append", "allow_always")
check("永久允许 → allow", gate2.check({"name": "note_append", "permission": "write"}, {"text": "x"})["decision"] == "allow")
ap.remember("run_command", "deny_always")
check("永久拒绝 → deny", gate2.check({"name": "run_command", "permission": "danger"}, {"cmd": "ls"})["decision"] == "deny")
ap3 = ApprovalStore(path=apath)
check("口径落盘、跨进程生效", ap3.lookup("note_append") == "always_allow" and ap3.lookup("run_command") == "always_deny")
check("一次性决定不落盘", ApprovalStore(path=os.path.join(TMP, "ap3.json"), cfg={"allow": []}).lookup("note_append") is None)

# ================= 8. 审计 =================
print("\n[8] 审计（追加、可回溯）")
apath_audit = os.path.join(TMP, "audit3.jsonl")
au = AuditLog(path=apath_audit)
gate3 = PermissionGate({}, policy=pol, sandbox=Sandbox({"read_roots": [TMP], "write_roots": [TMP]}, base_dir=ROOT),
                       approvals=ApprovalStore(path=os.path.join(TMP, "ap4.json")), audit=au, base_dir=ROOT)
gate3.check({"name": "read_file", "permission": "read"}, {"path": "/etc/shadow"})   # 被 deny
rows = au.recent(10)
check("deny 必写审计", any(r.get("action") == "deny" for r in rows), rows)
reg3 = ToolRegistry(gate=gate3, audit=au)
reg3.register(SkillMeta(name="echo_ok", description="", permission="read", handler=lambda a: "ok"))
reg3.run("echo_ok", {}, {"gate": gate3, "session_id": "s1"})
rows = au.recent(10)
calls = [r for r in rows if r.get("action") == "call"]
check("工具调用留下审计（参数/结果/是否获批）", bool(calls) and "result_ok" in calls[0]["detail"], calls[:1])
check("审计只追加（行数递增）", len(au.recent(100)) >= 2)

# ================= 9. 唯一执行路径（带闸/审批） =================
print("\n[9] 唯一执行路径（校验→鉴权→执行→截断→审计）")
apx = ApprovalStore(path=os.path.join(TMP, "ap5.json"))
gatex = PermissionGate({}, policy=pol, sandbox=Sandbox({"read_roots": [TMP], "write_roots": [TMP]}, base_dir=ROOT),
                       approvals=apx, audit=AuditLog(path=os.path.join(TMP, "audit4.jsonl")), base_dir=ROOT)
regx = ToolRegistry(gate=gatex, audit=gatex.audit)
regx.register(SkillMeta(name="w_skill", description="写", permission="write",
                        parameters={"type": "object", "properties": {"t": {"type": "string"}}, "required": ["t"]},
                        handler=lambda a: "wrote:" + a["t"]))
r_nocb = regx.run("w_skill", {"t": "a"}, {"gate": gatex})
check("写入无审批通道 → TOOL_DENIED", not r_nocb["ok"] and r_nocb["meta"]["error"] == "TOOL_DENIED", r_nocb)
r_deny = regx.run("w_skill", {"t": "a"}, {"gate": gatex, "approval": lambda p: False})
check("用户拒绝 → 不执行", not r_deny["ok"] and "拒绝" in r_deny["content"], r_deny)
r_ok = regx.run("w_skill", {"t": "a"}, {"gate": gatex, "approval": lambda p: True})
check("用户允许 → 执行成功", r_ok["ok"] and r_ok["content"] == "wrote:a", r_ok)
check("缺参数 → TOOL_BAD_ARGS", regx.run("w_skill", {}, {"gate": gatex})["meta"]["error"] == "TOOL_BAD_ARGS")
check("不存在的技能 → TOOL_NOT_FOUND", regx.run("nope", {})["meta"]["error"] == "TOOL_NOT_FOUND")
big = regx.register(SkillMeta(name="big", description="", permission="read", handler=lambda a: "x" * 20000))
check("超长结果被截断", len(regx.run("big", {}, {"gate": gatex})["content"]) == 8000)

# ================= 10. 脚本技能：超时 / 失败 =================
print("\n[10] 脚本技能（超时 / 非零退出）")
new_skill("slow", "slow_skill", "read", "import time\ntime.sleep(5)\nprint('{}')\n")
d_slow = os.path.join(TMP, "slow", "SKILL.md")
write(d_slow, "---\nname: slow_skill\ndescription: 慢\npermission: read\nentry: scripts/run.py\ntimeout_ms: 400\n---\n")
new_skill("boom", "boom_skill", "read", "import sys\nsys.stderr.write('炸了')\nsys.exit(2)\n")
rep4 = SkillLoader([TMP]).load()
reg4 = ToolRegistry(); reg4.replace_source("file:", rep4["skills"])
r_to = reg4.run("slow_skill", {})
check("脚本超时 → TOOL_TIMEOUT", not r_to["ok"] and r_to["meta"]["error"] == "TOOL_TIMEOUT", r_to["meta"])
r_boom = reg4.run("boom_skill", {})
check("脚本非零退出 → TOOL_FAILED", not r_boom["ok"] and "退出码 2" in r_boom["content"], r_boom["content"][:80])

# ================= 11. MCP 客户端端到端 =================
print("\n[11] MCP 客户端（接上即有工具）")
regm = ToolRegistry()
mgr = MCPManager({"servers": {"demo": {"command": ["python3", os.path.join(ROOT, "scripts", "test_mcp_server.py")],
                                       "timeout": 20, "default_tier": "write"}}}, registry=regm)
start = mgr.start_all()
check("server 拉起并注册工具", start["tools"] == 3 and not start["errors"], start)
check("工具进可选列表（模型能看到）", all(n in regm.names() for n in
      ["demo__echo", "demo__add", "demo__delete_thing"]), regm.names())
check("注解 → 级别映射（只读/默认写/危险）",
      regm.meta("demo__echo").permission == READ and regm.meta("demo__add").permission == WRITE
      and regm.meta("demo__delete_thing").permission == DANGER)
check("工具 Schema 带参数", regm.meta("demo__add").parameters.get("required") == ["a", "b"])
r_echo = regm.run("demo__echo", {"text": "hi"}, {})
check("调用 MCP 工具拿到结果", r_echo["ok"] and "echo: hi" in r_echo["content"], r_echo)
mgr.stop_all()
check("关闭后工具被摘掉", not [n for n in regm.names() if n.startswith("demo__")])
mgr_bad = MCPManager({"servers": {"ghost": {"command": ["/nonexistent/bin"], "timeout": 5}}}, registry=ToolRegistry())
rep_bad = mgr_bad.start_all()
check("坏 server 只记错、不炸", "ghost" in rep_bad["errors"], rep_bad)
mgr_bad.stop_all()

# ================= 汇总 =================
print("\n" + "=" * 52)
print("M3 自检：%d/%d 通过" % (len(PASS), len(PASS) + len(FAIL)))
if FAIL:
    print("失败项：")
    for f in FAIL:
        print("  - %s" % f)
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if FAIL else 0)
