# -*- coding: utf-8 -*-
"""Outgoing text normalization self-test.

These rules are wording details the user explicitly asked for. They are not left to
the persona prompt (prompting proved unreliable), so they are enforced in code --
and therefore have to be enforced by tests as well.

Run: python tests/test_textnorm.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qqbridge.textnorm import MAX_PARTS, split_messages, tidy_outgoing

ok = []

# ---------- 1. No trailing full stop ----------
assert tidy_outgoing("好的。") == "好的"
assert tidy_outgoing("行吧。  ") == "行吧"
assert tidy_outgoing("走了。") == "走了"
# A full stop in the middle of a sentence is left alone
assert tidy_outgoing("先这样。回头说") == "先这样。回头说"
ok.append("句尾的「。」会被去掉，句中的保留")

# ---------- 2. "..." -> "。。。" ----------
assert tidy_outgoing("不知道...") == "不知道。。。"
assert tidy_outgoing("啊这.......") == "啊这。。。"
assert tidy_outgoing("emmm…") == "emmm。。。"
assert tidy_outgoing("无语。。。") == "无语。。。", "本来就是三个句号的不该被改"
# A trailing ellipsis is tone, not a sentence-ending period, so it must not be stripped
assert tidy_outgoing("真的假的。。") == "真的假的。。"
assert tidy_outgoing("算了。。") == "算了。。"
ok.append("「...」「…」统一成「。。。」，且不会被当成句尾句号剥掉")

# ---------- 3. Empty and blank input ----------
assert tidy_outgoing("") == ""
assert tidy_outgoing("   ") == ""
# None has to be tolerated too, not just empty strings.
assert tidy_outgoing(None) == ""
ok.append("空白输入返回空字符串")

# ---------- 4. Splitting into several parts (the core of case 19/20) ----------
parts = split_messages(["吃了吗", "没吃就一起"])
assert parts == ["吃了吗", "没吃就一起"], parts
# If the model ignores the instruction and returns one blob with newlines, split it
parts = split_messages("吃了\n你呢\n我还没")
assert parts == ["吃了", "你呢", "我还没"], parts
# A single array element may itself contain newlines
parts = split_messages(["a\nb", "c"])
assert parts == ["a", "b", "c"], parts
# Every part still goes through normalization
parts = split_messages(["好的。", "走...\n"])
assert parts == ["好的", "走。。。"], parts
ok.append("多行 / 数组都能拆成多条，并且每条单独规整")

# ---------- 5. Upper bound ----------
# Six parts is more than MAX_PARTS allows, so the overflow has to merge into the last.
many = [f"第{i}句" for i in range(6)]
parts = split_messages(many)
assert len(parts) == MAX_PARTS, f"最多 {MAX_PARTS} 条，实际 {len(parts)}"
assert parts[-1].startswith("第2句"), f"超出的要并进最后一条：{parts}"
ok.append(f"最多拆 {MAX_PARTS} 条，多出来的并进最后一条（不刷屏）")

# ---------- 6. A normal single part is untouched ----------
assert split_messages(["？"]) == ["？"]
assert split_messages(["？？？"]) == ["？？？"]
assert split_messages(["行（"]) == ["行（"], "句尾的左括号要留着"
ok.append("「？」「？？？」「行（」这类短句原样保留")

print("\n".join("PASS  " + x for x in ok))
print("ALL PASS")
