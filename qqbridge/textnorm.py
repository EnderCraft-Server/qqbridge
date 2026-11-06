"""Text cleanup before sending.

Talking in a group chat is not the same as writing an article. These details
genuinely change how human it reads:
  - A trailing "。" sounds exactly like a customer-service announcement; real
    people rarely type a full stop
  - "..." is a keyboard ellipsis, while "。。。" is what speechlessness/awkwardness
    looks like in a Chinese-language chat
  - A whole paragraph crammed into one message reads like reading a script, no
    matter how short; splitting it into several messages feels like typing

So this is **not delegated to the persona by asking the model nicely** (that is
unreliable anyway); it is enforced in code: the model may write however it likes,
everything goes through one pass before it is sent.
"""
from __future__ import annotations

import re

# Max number of messages to split into. Splitting too finely spams the group;
# 3 is the natural ceiling for "one more thing" / "actually, a correction".
MAX_PARTS = 3


def tidy_outgoing(text: str) -> str:
    """Tidy one message: ellipses and the trailing full stop."""
    s = str(text or "").strip()
    if not s:
        return ""

    # Normalize ellipses to three Chinese full stops; ASCII dots and the unicode
    # ellipsis both count
    s = re.sub(r"\.{3,}", "。。。", s)
    s = re.sub(r"[…]+", "。。。", s)

    # No trailing full stop. But "。。。" / "。。" is a deliberate tone, leave it be
    if s.endswith("。") and not s.endswith("。。"):
        s = s[:-1].rstrip()
    if s.endswith(".") and not s.endswith(".."):
        s = s[:-1].rstrip()
    return s


def split_messages(*candidates) -> list[str]:
    """Turn whatever the model returned into "how many messages to send".

    Two sources are accepted:
      - the model returns an array directly (preferred)
      - the model returns one paragraph with newlines in it (legacy format / it
        ignored the format instruction)
    A multi-line item, or an array element that itself contains newlines, is
    split apart here. At most MAX_PARTS messages overall; anything beyond that is
    merged into the last one so the group does not get spammed.
    """
    # Flatten both input shapes into a plain list of strings first.
    raw: list[str] = []
    for item in candidates:
        if item is None:
            continue
        if isinstance(item, (list, tuple)):
            raw.extend(str(x) for x in item)
        else:
            raw.append(str(item))

    # One line becomes one message; empties are dropped so a blank line never
    # turns into an empty send.
    parts: list[str] = []
    for chunk in raw:
        for line in str(chunk).replace("\r", "").split("\n"):
            line = tidy_outgoing(line)
            if line:
                parts.append(line)

    # Overflow is folded into the last slot: neither dropped silently nor spilled
    # out as extra messages.
    if len(parts) > MAX_PARTS:
        parts = parts[:MAX_PARTS - 1] + [" ".join(parts[MAX_PARTS - 1:])]
    return parts
