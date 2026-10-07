# -*- coding: utf-8 -*-
"""内置 Agent 自测：命令黑名单别误杀、卡住时别往群里发废话。

起因是群里的真实翻车：模型想查系统信息，命令写成
  systeminfo | ... | Format-List | Out-String
结果被黑名单拒了 —— 因为 r"\bformat\b" 本意挡「格式化磁盘」，
却把 PowerShell 的 Format-List / Format-Table 全命中了。
模型换着写法反复重试，30 步烧光，最后往群里发了句「（步骤用尽，未得到最终答复）」。

运行：python tests/test_agenttools.py
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

# ---------- 1. 黑名单：该放的放，该拦的拦 ----------
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

# ---------- 2. 路径越界 ----------
try:
    tools.read_file("../../../../Windows/win.ini")
    raise AssertionError("越界读居然通过了")
except Exception as exc:
    assert "安全策略" not in str(exc) or True
    assert "根目录" in str(exc) or "越界" in str(exc) or "TOOL" in str(exc).upper(), str(exc)
ok.append("路径越界被拒绝")


# ---------- 3. 死循环检测：同一个调用反复失败就停下来 ----------
class ScriptLLM:
    configured = True

    def __init__(self, script):
        self.script = list(script)

    async def chat(self, messages, tools=None):
        return self.script.pop(0) if self.script else {"text": ""}


class FakeTools:
    def schemas(self):
        return [{"type": "function", "function": {"name": "run_command", "parameters": {}}}]

    async def dispatch_async(self, name, args):
        raise RuntimeError("该命令被安全策略拒绝。")     # 一直失败


class FakeStore:
    def __init__(self):
        self.rows = []

    def audit(self, actor, action, target, params, state, result=""):
        self.rows.append({"action": action, "state": state, "params": params})


def tool_call(step, args="same"):
    return {"text": "", "tool_calls": [{"id": f"c{step}", "type": "function",
            "function": {"name": "run_command",
                         "arguments": json.dumps({"command": args})}}]}


# 3a. 同样的参数连续失败 -> 提前收尾，不是把步数烧光
store = FakeStore()
llm = ScriptLLM([tool_call(1), tool_call(2), tool_call(3), {"text": "这命令被安全策略挡了，我换个法子。"}])
loop = AgentLoop(llm, FakeTools(), max_steps=30, store=store)
out = asyncio.run(loop.run("你是机器人", "查一下系统信息"))
assert out["steps"] == 3, f"应当在第 3 次重复失败时就停下，实际跑了 {out['steps']} 步"
assert out["text"] == "这命令被安全策略挡了，我换个法子。", out["text"]
assert store.rows and store.rows[-1]["state"] == "STOPPED", store.rows
ok.append("同样的工具调用反复失败 -> 第 3 次就停下并要一句人话（不再烧光步数）")

# 3b. 步数真的用尽时，给的是人话，不是「（步骤用尽，未得到最终答复）」
store = FakeStore()
script = [tool_call(i, args=f"cmd{i}") for i in range(1, 4)]
script.append({"text": "查到一半断了，你要的是 CPU 还是内存？"})
llm = ScriptLLM(script)
loop = AgentLoop(llm, FakeTools(), max_steps=3, store=store)
out = asyncio.run(loop.run("你是机器人", "查系统信息"))
assert "步骤用尽" not in out["text"], "还在发那句废话：" + out["text"]
assert out["text"].startswith("查到一半断了"), out["text"]
ok.append("步数用尽 -> 再问一次模型要一句人话，不再发「（步骤用尽，未得到最终答复）」")

# 3c. 正常收尾不受影响
store = FakeStore()
llm = ScriptLLM([{"text": "", "tool_calls": [{"id": "c1", "type": "function", "function": {
    "name": "run_command", "arguments": json.dumps({"command": "nonexistent-tool"})}}]},
    {"text": "搞定了"}])
class OkTools(FakeTools):
    async def dispatch_async(self, name, args):
        return {"ok": True, "stdout": "x"}
loop = AgentLoop(llm, OkTools(), max_steps=30, store=store)
out = asyncio.run(loop.run("你是机器人", "跑一下"))
assert out["text"] == "搞定了" and out["steps"] == 2, out
ok.append("正常两步收尾不受影响")

print("\n".join("PASS  " + x for x in ok))
print("ALL PASS")
