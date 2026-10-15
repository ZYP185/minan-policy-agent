"""语料库体检报告。"""
import glob
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from core.store import PolicyStore

store = PolicyStore(config.POLICY_DIR, config.STRUCT_DIR).load()
s = store.stats()

print("=" * 78)
print("  语料库体检")
print("=" * 78)
print(f"  政策 {s['政策数']} 份   条款 {s['条款数']} 条")

# 结构化完成度
n_struct = sum(1 for p in store.policies.values() if p.structured.get("_extracted"))
rules = sum(len(p.structured.get("eligibility_rules") or []) for p in store.policies.values())
mats = sum(len(p.structured.get("materials") or []) for p in store.policies.values())
benefits = sum(len(p.structured.get("benefit") or []) for p in store.policies.values())
print(f"  已结构化 {n_struct}/{len(store)} 份")
print(f"  资格规则 {rules} 条   材料清单项 {mats} 条   待遇条款 {benefits} 条")

print(f"\n  时效分布：{s['时效分布']}")
print(f"  地域分布：{dict(Counter(p.region or '(全国)' for p in store.policies.values()))}")
print(f"  类别分布：{s['分类分布']}")

# 资格规则按字段统计 —— 看抽出了哪些维度的准入条件
fields = Counter()
for p in store.policies.values():
    for r in (p.structured.get("eligibility_rules") or []):
        if isinstance(r, dict) and r.get("field"):
            fields[r["field"]] += 1
print(f"\n  资格规则覆盖的字段：{dict(fields.most_common())}")

# 有没有完全没抽出规则的政策
empty = [p for p in store.policies.values()
         if p.structured.get("_extracted") and not (p.structured.get("eligibility_rules") or [])]
if empty:
    print(f"\n  未抽到资格规则的政策（{len(empty)} 份，可能本身无准入条件）：")
    for p in empty[:8]:
        print(f"    · {p.title[:56]}")

# 缺地域的
no_region = [p for p in store.policies.values() if not p.region]
print(f"\n  无地域标注（视为全国通用）：{len(no_region)} 份")
