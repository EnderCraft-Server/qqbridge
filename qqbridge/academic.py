"""Academic mode: pull "academic/technical questions" out of small talk and answer in a
different posture.

Why this needs a mode of its own:
the bot's everyday persona is "a group member" -- short, tsundere, quick to snark, silent
by default. That persona starts to fail the moment a math problem shows up: it treats
"given the function f(x)=ln x + a/x - a, discuss its monotonicity" as ordinary group
chatter, fobs it off in 1-10 characters, or simply ignores it.

So this module does two things:
  1. decide whether the message is an academic/technical question (heuristics, **no extra
     model call**, to save tokens);
  2. when it matches, swap the persona out for a while: switch off the "little feelings",
     lift the length cap, and demand rigor and structure.

Detection only triggers conservatively -- better to stay quiet (answer as a group member)
than to false-positive (turn small talk into a paper). The user can override it explicitly:
a message starting with "学术", "认真答", "详细解释" and the like always matches.
"""
from __future__ import annotations

import re

# ---------- Explicit trigger: the user calls it out ----------
FORCE_PREFIXES = (
    "学术", "学术模式", "认真答", "认真回答", "详细解释", "详细说说",
    "仔细讲讲", "正经回答", "别玩梗", "别贫",
)

# ---------- Subject words ----------
SUBJECT_TERMS = (
    # Natural sciences
    "数学", "物理", "化学", "生物", "地理", "天文", "地质", "气象",
    "微积分", "线性代数", "概率论", "数理统计", "统计学", "数论", "拓扑",
    "力学", "电磁学", "热力学", "光学", "量子", "相对论", "流体力学",
    "有机化学", "无机化学", "分析化学", "物理化学", "生物化学",
    "细胞", "遗传", "基因", "生态", "解剖", "免疫", "神经",
    # Engineering / computer science
    "算法", "数据结构", "编译原理", "操作系统", "计算机网络", "数据库",
    "机器学习", "深度学习", "神经网络", "密码学", "信息论", "图形学",
    "软件工程", "体系结构", "分布式", "并发", "编译器", "解释器",
    # Humanities and social sciences
    "哲学", "经济学", "心理学", "社会学", "语言学", "逻辑学", "伦理学",
    "历史学", "考古", "法学", "政治学", "人类学", "美学",
)

# ---------- Academic actions ----------
SOLVE_VERBS = (
    "证明", "推导", "求解", "计算", "化简", "解方程", "求导", "求积分",
    "讨论", "论证", "阐述", "论述", "辨析", "评述", "综述",
    "求极限", "求值", "换元", "因式分解", "通分",
)

# ---------- Technical terms (a hit here is already a strong signal) ----------
TECH_TERMS = (
    "定理", "定律", "引理", "推论", "公理", "悖论",
    "方程", "函数", "公式", "矩阵", "行列式", "向量", "张量",
    "极限", "导数", "偏导", "积分", "微分", "级数", "收敛", "发散",
    "概率", "期望", "方差", "分布", "假设检验", "回归",
    "熵", "焓", "势能", "动能", "电动势", "电阻", "电容",
    "氧化", "还原", "摩尔", "浓度", "溶液", "渗透压", "等渗", "高渗", "低渗",
    "时间复杂度", "空间复杂度", "递归", "迭代", "指针", "模板", "泛型",
    "红黑树", "哈希", "二叉", "链表", "栈", "队列", "图论", "动态规划",
    "进程", "线程", "死锁", "内存管理", "虚拟内存", "缓存",
    "协议", "握手", "路由", "子网", "事务", "索引", "范式",
    "过拟合", "梯度", "反向传播", "损失函数", "激活函数", "注意力",
    "语义", "语法", "音系", "词法",
)

# ---------- Writing code ----------
CODE_REQUEST = re.compile(
    r"(写|实现|给|来|撸|整)(一(段|个|份)|个)?\s*"
    r"(c\+\+|cpp|c#|python|java|javascript|typescript|go|rust|kotlin|swift|sql|shell|bash|汇编|代码|函数|程序|脚本|算法|模板)",
    re.I,
)
CODE_LANGS = ("c++", "cpp", "python", "java", "javascript", "typescript", "golang",
              "rust", "kotlin", "swift", "sql", "bash", "汇编")
CODE_FENCE = re.compile(r"```")

# ---------- Math notation ----------
MATH_NOTATION = re.compile(
    r"(f\s*\(\s*x\s*\)|[A-Za-z]\s*\(\s*[a-z]\s*\))"      # the f(x) shape
    r"|(\^\s*\d|\d\s*\^)"                                  # powers
    r"|([\u222b\u2211\u220f\u221a\u2264\u2265\u2260\u2248\u2192\u21d2])"  # ∫∑∏√≤≥≠≈→⇒
    r"|(\b(ln|log|sin|cos|tan|lim|max|min|exp)\b)"
    r"|(\b[a-z]\s*[=<>]\s*[^=]*(/[a-z0-9(]))"                 # a fraction like a/x
    r"|(²|³|⁴)"
)

# ---------- Question shapes ----------
QUESTION = re.compile(r"(什么是|是什么|为什么|为何|如何|怎么(办|做|算|解)|怎样|请(问|解释|说明|分析|简述)|解释一下|说说.*(原理|机制)|区别|关系是|定义)")
ASK_LONG = 40   # past this length, a trailing question mark counts as a weak signal


def _score(text: str) -> tuple[int, list[str]]:
    # Weighted keyword scan: subject words weigh 1, while technical terms / language names /
    # code requests / fenced code / math notation weigh 2. That way a single term hit (>= 2)
    # already clears the bar in detect(), whereas one lone subject word still needs a
    # question shape to go with it.
    low = (text or "").strip().lower()
    hits: list[str] = []
    strong = 0

    for w in SUBJECT_TERMS:
        if w in low:
            strong += 1
            hits.append("学科:" + w)
    for w in TECH_TERMS:
        if w in low:
            strong += 2
            hits.append("术语:" + w)
    for w in SOLVE_VERBS:
        if w in low:
            strong += 1
            hits.append("动作:" + w)
    for w in CODE_LANGS:
        if w in low:
            strong += 2
            hits.append("语言:" + w)
            # Break after the first hit: one message mentioning three languages is still
            # one code signal, not three.
            break
    if CODE_REQUEST.search(low):
        strong += 2
        hits.append("写代码")
    if CODE_FENCE.search(text or ""):
        strong += 2
        hits.append("代码块")
    if MATH_NOTATION.search(text or ""):
        strong += 2
        hits.append("数学记号")
    return strong, hits


def detect(text: str) -> tuple[bool, str]:
    """Return (is_academic, reason).

    Conservative policy: either one strong enough technical signal (term / code / math
    notation), or a combination of a subject word and a question shape. A bare "why" on
    its own never counts.
    """
    raw = (text or "").strip()
    if not raw:
        return False, ""
    low = raw.lower()

    # 1. The user calls it out explicitly
    for p in FORCE_PREFIXES:
        if low.startswith(p):
            return True, "显式要求:" + p

    strong, hits = _score(raw)
    has_q = bool(QUESTION.search(raw))

    # 2. Strong signal (terms/code/math notation worth >= 2, or a subject + action stack)
    if strong >= 2:
        return True, ",".join(hits[:4])

    # 3. A single weak signal plus an explicit question shape
    if strong >= 1 and has_q:
        return True, ",".join(hits[:4]) + ",提问"

    # 4. Long question: only when it ends with a question mark and is long enough
    #    (so that a bare "you there?" does not qualify)
    if has_q and len(raw) >= ASK_LONG and ("？" in raw or "?" in raw):
        return True, "长问句"

    return False, ""


# ---------- Persona override for academic mode ----------
ACADEMIC_CORE = """【学术模式 · 已激活】

提问的人要的是答案，不是群友。从现在起，**上面那套"群友人格"整体让位**：

- **关掉小情绪。** 不傲娇、不嘴硬、不怼人、不玩梗、不反问、不阴阳怪气。
  不许出现"？""6""草""乐""难绷"这类短反应，也不要用"懒得理你"这种姿态。
  对方语气再冲，也按问题本身作答。
- **长度由内容决定。** 不要为了"像群友"把答案憋成一两行。该展开就展开，
  该分点就分点，该给步骤就给步骤，该附例子就附例子。
- **先给结论，再给理由。** 结构：答案/结论 → 推导或解释 → 必要时的例子或边界条件。
- **准确优先于好看。** 拿不准就明说拿不准、说清哪一部分不确定；
  **绝对不要编造公式、定理名、数据或出处**。编一个像模像样的错答案是最坏的结果。
- 单位、量纲、定义域、取值范围这类容易漏的条件，主动写出来。
- 不写客套话，不复述问题，不说"希望有帮助"。
"""

ACADEMIC_JSON_TAIL = """
仍然以 JSON 回复：{"reply": "要发到群里的正文", "reason": "一句话理由"}。
reply 里可以放多行、可以放代码块 —— 把要说的写完整，不要因为"格式"截断答案。
"""

ACADEMIC_PLAIN_TAIL = """
直接输出要发到群里的正文（不要 JSON、不要解释、不要引号），可以多行、可以带代码块。
"""


def build_academic_system(base_prompt: str, *, json_mode: bool = True) -> str:
    """Stitch the group-member persona and the academic override into a system prompt."""
    tail = ACADEMIC_JSON_TAIL if json_mode else ACADEMIC_PLAIN_TAIL
    # rstrip first, otherwise a base prompt ending in whitespace leaves stray spaces before
    # the blank separator line.
    return (base_prompt or "").rstrip() + "\n\n" + ACADEMIC_CORE + tail
