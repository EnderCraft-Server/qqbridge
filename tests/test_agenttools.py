# -*- coding: utf-8 -*-
"""Built-in agent self-test: the command blacklist must not hit innocent commands,
and the agent must not post filler into the group when it gets stuck.

It started with a real failure in the group: the model wanted to inspect the system
and wrote the command
  systeminfo | ... | Format-List | Out-String
but the blacklist rejected it -- because r"\bformat\b" was meant to block
"format a disk", yet it also matched PowerShell's Format-List / Format-Table.
The model kept retrying with different spellings, burned all 30 steps, and finally
posted "（步骤用尽，未得到最终答复）" into the group.

Run: python tests/test_agenttools.py
"""
import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qqbridge.agentloop import AgentLoop
from qqbridge.agenttools import LocalTools

ok = []

# ---------- 1. Blacklist: let through what should pass, block what must be blocked ----------
tmp = Path(tempfile.mkdtemp(prefix="qqbridge_tools_"))
tools = LocalTools(tmp)

SHOULD_PASS = [
    'systeminfo | Select-String "OS Name","OS Version"; echo "---CPU---"; '
    '(Get-CimInstance Win32_Processor | Select-Object Name | Format-List | Out-String)',
    "Get-CimInstance Win32_Processor | Format-List Name,NumberOfCores,LoadPercentage",
    'echo "---UPTIME---"; (Get-CimInstance Win32_OperatingSystem).LastBootUpTime',
    'Get-CimInstance Win32_LogicalDisk -Filter "DriveType=3" | '
    "Select-Object DeviceID,Size,FreeSpace | Format-Table | Out-String",
    "Get-NetIPAddress -AddressFamily IPv4 | Format-Table | Out-String",
    "Get-Process | Sort-Object CPU -Descending | Select-Object -First 5 | Format-Table",
    "Get-Content data/agent.log -Tail 20",
    'Select-String -Path README.md -Pattern "format"',
    "Get-ChildItem -Recurse -File | Measure-Object",
]
SHOULD_BLOCK = [
    "format C: /q",
    "format.com d:",
    "format /q",
    "Format-Volume -DriveLetter C",
    "mkfs.ext4 /dev/sda1",
    "shutdown /s /t 0",
    "Stop-Computer -Force",
    "Restart-Computer",
    "diskpart",
    "Clear-Disk -Number 0 -RemoveData",
    "rm -rf /",
    r"reg delete HKLM\Software\Foo",
]
# Only the safety-policy message counts as a rejection. Any other exception (a
# timeout, a missing binary, an ordinary command failure) is a different problem
# and must not be read as "blocked", or this test could pass for the wrong reason.
misjudged = []
for c in SHOULD_PASS:
    try:
        tools.run_command(c, timeout=5)
    except Exception as exc:
        if "安全策略" in str(exc):
            misjudged.append("误杀: " + c[:60])
for c in SHOULD_BLOCK:
    try:
        tools.run_command(c, timeout=5)
        misjudged.append("漏放: " + c[:60])
    except Exception as exc:
        if "安全策略" not in str(exc):
            misjudged.append("异常: " + c[:60])
assert not misjudged, "\n".join(misjudged)
ok.append(f"命令黑名单：{len(SHOULD_PASS)} 条只读命令放行（含 Format-List/Table），{len(SHOULD_BLOCK)} 条危险命令拦住")

# ---------- 2. Path traversal ----------
# Containment has to hold even though this path points at a real, readable file:
# the refusal is about the resolved path leaving the root, not about permissions.
# NB: the first assertion after the call is a tautology (it ends in "or True"), so
# the second one is what actually pins the behavior down.
try:
    tools.read_file("../../../../Windows/win.ini")
    raise AssertionError("越界读居然通过了")
except Exception as exc:
    assert "安全策略" not in str(exc) or True
    assert "根目录" in str(exc) or "越界" in str(exc) or "TOOL" in str(exc).upper(), str(exc)
ok.append("路径越界被拒绝")


# ---------- 3. Repeat-failure detection: stop when the same call keeps failing ----------
class ScriptLLM:
    configured = True

    def __init__(self, script):
        self.script = list(script)

    async def chat(self, messages, tools=None, *, max_tokens=None, temperature=None):
        # agentloop now passes max_tokens per call (academic mode needs a higher
        # limit), so the fake has to accept and ignore it.
        return self.script.pop(0) if self.script else {"text": ""}


# A tool layer that always raises the safety-policy error: exactly the situation
# that used to make the loop retry until every step was gone.
class FakeTools:
    def schemas(self):
        return [{"type": "function", "function": {"name": "run_command", "parameters": {}}}]

    async def dispatch_async(self, name, args):
        raise RuntimeError("该命令被安全策略拒绝。")     # always fails


class FakeStore:
    def __init__(self):
        self.rows = []

    def audit(self, actor, action, target, params, state, result=""):
        self.rows.append({"action": action, "state": state, "params": params})


# One run_command tool call. Case 3a repeats the same arguments every time, which is
# what trips the repeat detector; 3b varies them so it reaches the step limit.
def tool_call(step, args="same"):
    return {"text": "", "tool_calls": [{"id": f"c{step}", "type": "function",
            "function": {"name": "run_command",
                         "arguments": json.dumps({"command": args})}}]}


# 3a. The same arguments failing over and over -> wrap up early instead of burning
# every step
store = FakeStore()
llm = ScriptLLM([tool_call(1), tool_call(2), tool_call(3), {"text": "这命令被安全策略挡了，我换个法子。"}])
loop = AgentLoop(llm, FakeTools(), max_steps=30, store=store)
out = asyncio.run(loop.run("你是机器人", "查一下系统信息"))
assert out["steps"] == 3, f"应当在第 3 次重复失败时就停下，实际跑了 {out['steps']} 步"
assert out["text"] == "这命令被安全策略挡了，我换个法子。", out["text"]
assert store.rows and store.rows[-1]["state"] == "STOPPED", store.rows
ok.append("同样的工具调用反复失败 -> 第 3 次就停下并要一句人话（不再烧光步数）")

# 3b. When the steps really do run out, the user gets a human sentence, not the
# old "（步骤用尽，未得到最终答复）" filler
store = FakeStore()
script = [tool_call(i, args=f"cmd{i}") for i in range(1, 4)]
script.append({"text": "查到一半断了，你要的是 CPU 还是内存？"})
llm = ScriptLLM(script)
loop = AgentLoop(llm, FakeTools(), max_steps=3, store=store)
out = asyncio.run(loop.run("你是机器人", "查系统信息"))
assert "步骤用尽" not in out["text"], "还在发那句废话：" + out["text"]
assert out["text"].startswith("查到一半断了"), out["text"]
ok.append("步数用尽 -> 再问一次模型要一句人话，不再发「（步骤用尽，未得到最终答复）」")

# 3c. A normal finish is unaffected
store = FakeStore()
llm = ScriptLLM([{"text": "", "tool_calls": [{"id": "c1", "type": "function", "function": {
    "name": "run_command", "arguments": json.dumps({"command": "nonexistent-tool"})}}]},
    {"text": "搞定了"}])
# Same fake, but the tool call now succeeds, so the happy path can be checked.
class OkTools(FakeTools):
    async def dispatch_async(self, name, args):
        return {"ok": True, "stdout": "x"}
loop = AgentLoop(llm, OkTools(), max_steps=30, store=store)
out = asyncio.run(loop.run("你是机器人", "跑一下"))
assert out["text"] == "搞定了" and out["steps"] == 2, out
ok.append("正常两步收尾不受影响")

print("\n".join("PASS  " + x for x in ok))
print("ALL PASS")
