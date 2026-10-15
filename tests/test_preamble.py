"""英文前缀拦截器测试。不联网。

模型有时会在调用工具前吐一句英文（"I'll search for relevant policies for you."），
居民会原样看到。这个过滤器负责把它删掉，且不能误伤正文。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.agent import _PreambleFilter


def run(chunks: list[str]) -> str:
    """模拟流式投喂，返回过滤后居民实际看到的内容。"""
    f = _PreambleFilter()
    out = []
    for c in chunks:
        got = f.feed(c)
        if got:
            out.append(got)
    tail = f.flush()
    if tail:
        out.append(tail)
    return "".join(out)


CASES = [
    # (说明, 流式分片, 期望输出)
    ("纯中文原样通过",
     ["您母亲每月能领 180 元。"],
     "您母亲每月能领 180 元。"),

    ("英文前缀被删掉",
     ["I'll search for relevant policies for you.", "您母亲每月能领 180 元。"],
     "您母亲每月能领 180 元。"),

    ("英文与中文挤在同一个分片里",
     ["I'll check the policy.您母亲每月能领 180 元。"],
     "您母亲每月能领 180 元。"),

    ("逐字流式也不能漏",
     list("I'll search now.您母亲每月能领 180 元。"),
     "您母亲每月能领 180 元。"),

    ("英文跨多个分片",
     ["I'll look into", " what subsidies are", " available.您母亲能领补贴。"],
     "您母亲能领补贴。"),

    ("正文里的英文缩写不能被误删",
     ["您母亲可以申领上海 12345 热线的服务。"],
     "您母亲可以申领上海 12345 热线的服务。"),

    ("纯英文自言自语整个丢掉",
     ["I'll record this and run the eligibility check.", ""],
     ""),

    ("中文开头的正文不受影响",
     ["好的，我查到了。具体如下："],
     "好的，我查到了。具体如下："),
]


def main() -> int:
    failures = []
    for name, chunks, expect in CASES:
        got = run(chunks)
        ok = got == expect
        if not ok:
            failures.append(name)
        print(f"  [{'✓' if ok else '✗'}] {name}")
        if not ok:
            print(f"        期望：{expect!r}")
            print(f"        实际：{got!r}")

    print()
    if failures:
        print(f"失败 {len(failures)}/{len(CASES)}")
        return 1
    print(f"全部通过（{len(CASES)} 项）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
