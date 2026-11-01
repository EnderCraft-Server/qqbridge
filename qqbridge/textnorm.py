"""发出前的文字规整。

群里说话和写文章不是一回事。这些讲究是真的会影响「像不像人」：
  - 句尾挂个「。」特别像客服播报，真人打字很少收句号
  - 「...」是键盘省略号，「。。。」才是中文语境里的无语/尴尬
  - 一段话塞进一条消息，再短也像在念稿；拆成几条分开发才像在打字

所以这些**不靠 persona 去劝模型**（劝了也不稳定），而是在代码里兜住：
模型怎么写都行，发出去之前统一过一遍。
"""
from __future__ import annotations

import re

# 一条消息最多拆几段。拆太碎会刷屏，3 条是「补一句、改口」的自然上限。
MAX_PARTS = 3


def tidy_outgoing(text: str) -> str:
    """单条消息的规整：省略号、句尾句号。"""
    s = str(text or "").strip()
    if not s:
        return ""

    # 省略号统一成中文的三个句号；英文点号和 unicode 省略号都算
    s = re.sub(r"\.{3,}", "。。。", s)
    s = re.sub(r"[…]+", "。。。", s)

    # 句尾不收句号。但「。。。」「。。」是故意的语气，别动
    if s.endswith("。") and not s.endswith("。。"):
        s = s[:-1].rstrip()
    if s.endswith(".") and not s.endswith(".."):
        s = s[:-1].rstrip()
    return s


def split_messages(*candidates) -> list[str]:
    """把模型给的东西整理成「要分几次发」。

    两种来源都收：
      - 模型直接给数组（首选）
      - 模型给了一整段、里面带换行（老格式 / 它没听格式）
    一条多行、或者数组里某一项自带换行，都会在这里被拆开。
    整体最多 MAX_PARTS 条，超出的并到最后一条里，避免刷屏。
    """
    raw: list[str] = []
    for item in candidates:
        if item is None:
            continue
        if isinstance(item, (list, tuple)):
            raw.extend(str(x) for x in item)
        else:
            raw.append(str(item))

    parts: list[str] = []
    for chunk in raw:
        for line in str(chunk).replace("\r", "").split("\n"):
            line = tidy_outgoing(line)
            if line:
                parts.append(line)

    if len(parts) > MAX_PARTS:
        parts = parts[:MAX_PARTS - 1] + [" ".join(parts[MAX_PARTS - 1:])]
    return parts
