"""零依赖中文分词。

不用 jieba 的原因：本 Demo 要求零第三方依赖。对"政策条款检索"这个场景，
字符 bigram 的效果其实和词典分词接近——因为政策术语（"最低生活保障"
"高龄津贴""灵活就业"）本身就是高频共现的固定搭配，bigram 能天然捕捉。
"""
from __future__ import annotations

import re

# 中文停用词：只去掉对检索毫无信息量的功能词，不做激进过滤
STOPWORDS = frozenset("""
的 了 和 与 及 或 在 是 为 对 按 从 到 由 向 于 以 之 其 该 本 此 等 者
有 将 被 并 而 则 就 也 都 又 还 但 若 如 因 所 上 下 中 内 外 前 后
一 二 三 四 五 六 七 八 九 十 个 条 款 项 目 次 种 类 些 每 各 凡
请 可 应 须 需 得 能 会 要 不 无 未 非 己 已 后 时 年 月 日
""".split())

# 单独出现时无意义、但组合起来有意义的字，在 bigram 里保留；作为 unigram 时丢弃
_WEAK_UNIGRAM = frozenset("的了和与及或在是为对按从到由向于之以其该本此等者有个条款项目次种类")

_CJK = r"一-鿿㐀-䶿"
_RE_CJK_RUN = re.compile(f"[{_CJK}]+")
_RE_ASCII_RUN = re.compile(r"[A-Za-z0-9]+")


def tokenize(text: str) -> list[str]:
    """把一段中文/混合文本切成检索用的 token 列表。"""
    if not text:
        return []
    tokens: list[str] = []

    # 1) 中文连续段 → unigram + bigram
    for run in _RE_CJK_RUN.findall(text):
        n = len(run)
        if n == 1:
            if run not in _WEAK_UNIGRAM:
                tokens.append(run)
            continue
        for i in range(n):
            ch = run[i]
            if ch not in _WEAK_UNIGRAM:
                tokens.append(ch)
        for i in range(n - 1):
            bg = run[i:i + 2]
            if bg not in STOPWORDS:
                tokens.append(bg)

    # 2) 英文/数字段 → 原样 + 小写
    for run in _RE_ASCII_RUN.findall(text):
        low = run.lower()
        tokens.append(low)

    return tokens


def normalize(text: str) -> str:
    """归一化：全角转半角、压缩空白。用于比对与展示。"""
    if not text:
        return ""
    out = []
    for ch in text:
        code = ord(ch)
        if code == 0x3000:
            out.append(" ")
        elif 0xFF01 <= code <= 0xFF5E:
            out.append(chr(code - 0xFEE0))
        else:
            out.append(ch)
    return re.sub(r"\s+", " ", "".join(out)).strip()


# ---------------------------------------------------------------- 金额提取
_RE_MONEY = re.compile(
    r"(?:每人|每月|每年|每户|每次|一次性|按|标准为|不低于|不超过|最高)?"
    r"[^。；\n]{0,12}?"
    r"(\d+(?:\.\d+)?)\s*(万元|元|万|千元)"
)
_RE_PERCENT = re.compile(r"(\d+(?:\.\d+)?)\s*%")
_RE_DAYS = re.compile(r"(\d+)\s*(?:个)?(?:工作日|日|天)")


def extract_amounts(text: str) -> list[str]:
    """抽取文本中出现的金额/比例/时限，用于结构化展示。"""
    found: list[str] = []
    for m in _RE_MONEY.finditer(text):
        found.append(f"{m.group(1)}{m.group(2)}")
    for m in _RE_PERCENT.finditer(text):
        found.append(f"{m.group(1)}%")
    for m in _RE_DAYS.finditer(text):
        found.append(f"{m.group(1)}个工作日" if "工作日" in m.group(0) else f"{m.group(1)}天")
    # 去重保序
    seen, uniq = set(), []
    for f in found:
        if f not in seen:
            seen.add(f)
            uniq.append(f)
    return uniq


if __name__ == "__main__":
    demo = "本市户籍且年满80周岁的老年人，每人每月发放高龄津贴200元。"
    print("原文 :", demo)
    print("分词 :", tokenize(demo))
    print("金额 :", extract_amounts(demo))
