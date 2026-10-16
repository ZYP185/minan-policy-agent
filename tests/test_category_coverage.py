"""按民生类别测覆盖：每一类问一个典型问题，看能不能答上来。

语料类别分布不均（养老多、就业残疾人教育少）不等于有问题——
真正要看的是"这一类的问题还能不能答"。所以直接问，不问分布。

    python tests/test_category_coverage.py
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from core.llm import LLM
from core.retriever import Retriever
from core.store import PolicyStore

store = PolicyStore(config.POLICY_DIR, config.STRUCT_DIR).load()
if len(store) == 0:
    print("政策库为空")
    raise SystemExit(1)

# 每类一个居民会问的问题 + 期望命中的关键词
CASES = [
    ("养老", "我家老人 85 岁了，能领高龄津贴吗？",
     ["高龄", "老年", "养老", "敬老"]),
    ("救助", "我家收入很低，能申请最低生活保障吗？",
     ["最低生活保障", "低保", "社会救助", "特困"]),
    ("医疗", "住院花了很多钱，医保能报销多少？",
     ["医保", "医疗", "保险", "报销"]),
    ("住房", "我在本市没房子，能申请保障性住房吗？",
     ["住房", "租赁", "公租", "安居"]),
    ("生育", "生孩子有什么补贴可以领？",
     ["生育", "育儿", "托育", "产假"]),
    ("就业", "我失业了，能找到什么就业补助？",
     ["就业", "失业", "创业", "职业培训", "社会保险补贴"]),
    ("残疾人", "家里有残疾人，有什么补贴？",
     ["残疾", "两项补贴", "护理补贴", "康复"]),
    ("教育", "孩子上学有资助政策吗？",
     ["助学", "教育", "资助", "学生", "学费"]),
]

print("=" * 80)
print("  民生类别覆盖实测")
print("=" * 80)
s = store.stats()
print(f"  语料 {s['政策数']} 份 / {s['条款数']} 条")
print(f"  类别分布：{s['分类分布']}\n")

r = Retriever(store, llm=LLM())
rows = []

for cat, q, expects in CASES:
    hits = r.search(q, top_k=5, rerank=True)

    # 命中判定：前 5 名里有没有【类别正确】的政策。
    # 不用关键词子串匹配——那样会误判：例如问教育，返回的养老政策因为
    # 发文机关里含"教育部"三字就被算作命中，会把真实缺口盖住。
    rank = 0
    for i, h in enumerate(hits, 1):
        if h.policy.category == cat:
            rank = i
            break

    local_n = sum(1 for p in store.policies.values() if p.category == cat)

    rows.append((cat, local_n, rank, hits))
    mark = f"#{rank}" if rank else " ✗ "
    print(f"  [{mark:>3}] {cat:<5} 库内 {local_n:>2} 份   {q}")
    if hits:
        top = hits[0].policy
        flag = "" if top.category == cat else f"  ← 类别是「{top.category or '未分类'}」"
        print(f"        top1：{top.title[:50]}{flag}")
    print()

print("=" * 80)
covered = sum(1 for _, _, rank, _ in rows if rank)
print(f"  可答：{covered}/{len(CASES)} 类")
missing = [cat for cat, _, rank, _ in rows if not rank]
if missing:
    print(f"  ✗ 答不上来的类别：{'、'.join(missing)}")
    print("\n  这些类别需要补充语料。补法：")
    print("    python -m ingest.expand --entry <民政/人社/医保局政策栏目URL> --livelihood --yes")
print("=" * 80)

sys.exit(0 if covered >= len(CASES) * 0.75 else 1)
