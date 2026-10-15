"""用真实语料验证检索质量。不需要语料全部结构化完成。

    python tests/test_retrieval_real.py

考察的是：居民的口语提问，能不能命中正确的政策。
这是整个系统最关键的一环——检索错了，后面全错。
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import config
from core.llm import LLM, LLMError
from core.retriever import Retriever
from core.store import PolicyStore

store = PolicyStore(config.POLICY_DIR, config.STRUCT_DIR).load()
if len(store) == 0:
    print("政策库为空，请先运行：python -m ingest.build --urls urls.txt")
    raise SystemExit(1)

print(f"语料：{len(store)} 份政策 / {len(store.clauses)} 条条款\n")

# 居民会怎么问 → 期望命中的政策关键词
CASES = [
    ("我家老人 82 岁了能领什么钱",        ["老年综合津贴", "高龄津贴", "养老服务"]),
    ("我失业了有什么补助",                ["就业补助", "就业技能提升", "就业"]),
    ("家里有残疾人有什么补贴",            ["残疾人两项补贴", "残疾人"]),
    ("生孩子有补贴吗",                    ["育儿补贴", "育儿"]),
    ("收入低能申请低保吗",                ["最低生活保障", "低保"]),
    ("大病住院花了好多钱能报销吗",        ["医疗保险", "医保", "医疗保障"]),
    ("没房子能申请住房保障吗",            ["保障性租赁住房", "住房"]),
    ("特困人员怎么认定",                  ["特困人员"]),
    ("临时遇到困难能申请救助吗",          ["临时救助"]),
    ("赡养老人有什么支持政策",            ["养老服务", "老年", "互助"]),
]


def first_rank(hits, expects) -> int:
    """期望政策第一次出现在第几名（1 起）。0 表示前 5 名内没出现。

    只按"top5 里有没有关键词"打分太宽松——测不出排序好坏。
    这里看的是名次，才能反映重排的真实增益。
    """
    for i, h in enumerate(hits, 1):
        if any(k in h.policy.title for k in expects):
            return i
    return 0


def check(rerank: bool, verbose: bool = False) -> dict:
    try:
        llm = LLM() if rerank else None
    except LLMError as e:
        print(f"  （{e}，跳过）")
        return {"recall": len(CASES), "hit1": 0, "mrr": 0.0, "total": len(CASES)}

    r = Retriever(store, llm=llm)
    hit1 = recall = 0
    rr_sum = 0.0

    print(f"{'─' * 76}")
    print(f"  重排：{'开' if rerank else '关'}")
    print(f"{'─' * 76}")

    for q, expects in CASES:
        hits = r.search(q, top_k=5, rerank=rerank)
        rank = first_rank(hits, expects)
        if rank:
            recall += 1
            rr_sum += 1.0 / rank
            if rank == 1:
                hit1 += 1
        titles = " / ".join(h.policy.title[:24] for h in hits[:3]) or "(无命中)"
        mark = f"#{rank}" if rank else " ✗ "
        print(f"  [{mark:>3}] {q}")
        print(f"        top3：{titles}")

    mrr = rr_sum / len(CASES)
    print(f"\n  命中率 {recall}/{len(CASES)}   Top1 {hit1}/{len(CASES)}   MRR {mrr:.3f}\n")
    return {"recall": recall, "hit1": hit1, "mrr": mrr, "total": len(CASES)}


print("=" * 76)
print("A. 仅 BM25（无重排）")
print("=" * 76)
a = check(rerank=False)

print("=" * 76)
print("B. BM25 + LLM 重排")
print("=" * 76)
b = check(rerank=True)

print("=" * 76)
print(f"{'指标':<12}{'BM25':>10}{'BM25+重排':>14}")
print("-" * 76)
print(f"{'召回率':<12}{a['recall']:>7}/{a['total']}{b['recall']:>11}/{b['total']}")
print(f"{'Top1 命中':<10}{a['hit1']:>7}/{a['total']}{b['hit1']:>11}/{b['total']}")
print(f"{'MRR':<14}{a['mrr']:>8.3f}{b['mrr']:>12.3f}")
print("=" * 76)
sys.exit(0 if b["recall"] >= len(CASES) * 0.7 else 1)
