# -*- coding: utf-8 -*-
"""README 防漂移检查：文档里写的东西必须真的存在。

这份 README 之前漂得很厉害 —— 里面还写着早就删掉的 /switch chat、/switch agent，
命令超时、步数上限、工具清单也都和代码对不上。
与其靠人肉记得同步，不如让测试来喊。

运行：python tests/test_readme.py
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
text = README.read_text(encoding="utf-8")
ok = []

# ---------- 1. 不能再出现已经删掉的命令 ----------
REMOVED = ["/switch", "/on", "/off"]
stale = [c for c in REMOVED if "`" + c + "`" in text or " " + c + " " in text]
assert not stale, f"README 里还留着早就删掉的命令：{stale}"
ok.append("没有残留已删除的命令（/switch 之类）")

# ---------- 2. 提到的群内命令必须真在 commands.COMMANDS 里 ----------
sys.path.insert(0, str(ROOT))
from qqbridge import commands

mentioned = set(re.findall(r"`(/[a-z]+)`", text))
assert mentioned, "README 里一个命令都没提？"
unknown = sorted(mentioned - set(commands.COMMANDS))
assert not unknown, f"README 提到了不存在的命令：{unknown}"
missing_cmd = sorted(set(commands.COMMANDS) - mentioned)
assert not missing_cmd, f"代码里有命令但 README 没写：{missing_cmd}"
ok.append("群内命令与 commands.COMMANDS 完全一致：" + " ".join(sorted(mentioned)))

# ---------- 3. 反引号里的大写变量必须是 .env.example 里真有的键 ----------
env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
env_keys = {line.split("=", 1)[0].strip()
            for line in env_example.splitlines()
            if "=" in line and not line.strip().startswith("#")}
# 审计状态、协议名之类的全大写词不是环境变量，别误伤
NON_ENV = {"JSON", "HTTP", "DENIED", "SUCCEEDED", "SKIPPED", "MIT", "README", "EULA"}
referenced = set(re.findall(r"`([A-Z][A-Z0-9_]{3,})`", text)) - NON_ENV
bad_env = sorted(referenced - env_keys)
assert not bad_env, f"README 提到了 .env.example 里没有的变量：{bad_env}"
ok.append(f"引用的 {len(referenced)} 个环境变量都能在 .env.example 里找到")

# ---------- 4. MCP 工具：一个不少，一个不多，数量也别吹 ----------
server_src = (ROOT / "qqbridge" / "server.py").read_text(encoding="utf-8")
tool_names = re.findall(r'@tool\("([a-z_]+)"', server_src)
assert tool_names, "没能从 server.py 里解析出工具名"
not_documented = [t for t in tool_names if "`" + t + "`" not in text]
assert not not_documented, f"这些 MCP 工具没写进 README：{not_documented}"

# 三个分组声明的数量加起来必须等于真实工具数
claimed = [int(n) for n in re.findall(r"\*\*[^*]*（(\d+) 个）\*\*", text)]
assert sum(claimed) == len(tool_names), (
    f"README 声称的 MCP 工具数 {claimed} 加起来是 {sum(claimed)}，"
    f"但 server.py 里实际有 {len(tool_names)} 个")
ok.append(f"MCP 工具 {len(tool_names)} 个全部出现在 README，分组数量 {claimed} 对得上")

# ---------- 5. 目录结构里列的文件必须真的存在 ----------
tree = re.search(r"## 目录结构\s*```text\n(.*?)```", text, re.S)
assert tree, "README 里找不到「目录结构」代码块"
listed = []
in_data = False           # data/ 是运行时生成的，不进仓库，不检查
for line in tree.group(1).splitlines():
    entry = line
    for ch in "│├└─":
        entry = entry.replace(ch, " ")
    entry = entry.strip()
    if not entry:
        continue
    token = entry.split()[0]      # 每行第一段就是文件名或目录名
    if token == "data/":
        in_data = True
        continue
    if in_data or token == "qqbridge/" or token.endswith("/"):
        continue
    if re.fullmatch(r"[\w.\-]+\.[a-z]+", token):
        listed.append(token)
assert listed, "目录结构解析出 0 个文件，检查一下解析逻辑"
missing_files = [n for n in listed if not list(ROOT.rglob(n))]
assert not missing_files, f"目录结构里列了不存在的文件：{missing_files}"
ok.append(f"目录结构里的 {len(listed)} 个文件都真实存在")

# ---------- 6. 「测试」一节列出的脚本必须真的存在 ----------
test_block = re.search(r"## 测试(.*?)## 常见问题", text, re.S)
assert test_block, "README 里找不到「测试」一节"
scripts = re.findall(r"(tests/[\w.]+\.(?:py|mjs))", test_block.group(1))
assert scripts, "「测试」一节里没列出任何脚本"
missing_tests = [s for s in scripts if not (ROOT / s).is_file()]
assert not missing_tests, f"README 列了不存在的测试脚本：{missing_tests}"
on_disk = {p.name for p in (ROOT / "tests").glob("*")}
not_listed = sorted(on_disk - {Path(s).name for s in scripts})
assert not not_listed, f"tests/ 里有脚本但 README 没列：{not_listed}"
ok.append("「测试」一节与 tests/ 目录一致：" + " ".join(Path(s).name for s in scripts))

print("\n".join("PASS  " + x for x in ok))
print("ALL PASS")
