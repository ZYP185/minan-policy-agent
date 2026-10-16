"""效率实测：为参赛方案书提供真实数字。

竞赛要求写"推动产业效率提升"，但空口说"提升效率"没有说服力。
这个脚本跑一批代表性问询，记录真实耗时、工具调用次数、token 消耗，
产出可以直接写进方案书的数字。

    python tests/bench_efficiency.py

注意：数字是实测的，不要修图。评委追问"你这 40 秒怎么来的"时要答得上来。
"""
import json
import statistics
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import config
from core import agent as agent_mod
from core.llm import LLM
from core.retriever import Retriever
from core.store import PolicyStore

# 覆盖典型场景的问询。分三类，因为它们的耗时特征不同。
CASES = [
    ("模糊诉求", "我家老人 82 岁了，能领什么补贴？"),
    ("补齐信息", "我们是上海的。我母亲 82 岁，本市户籍，退休了。"),
    ("材料核对", "我带了身份证和户口本，够了吗？"),
    ("跨政策", "我最近失业了，家里还有残疾的老人，能申请什么？"),
    ("工单式", "最低生活保障的申请条件和办理流程是什么？"),
    ("边界拒答", "我是广州市的，高龄津贴能领多少？"),
]


def main() -> int:
    store = PolicyStore(config.POLICY_DIR, config.STRUCT_DIR).load()
    if not store.policies:
        print("政策库为空")
        return 1

    print("=" * 84)
    print("  民安 · 效率实测")
    print("=" * 84)
    s = store.stats()
    print(f"  语料：{s['政策数']} 份政策 / {s['条款数']} 条条款")
    print(f"  地区：{'、'.join(s['地域'])}")
    print()

    retriever = Retriever(store, llm=LLM())
    rows = []

    for label, q in CASES:
        # 每问开新会话：模拟居民独立提问，避免上下文复用导致偏快
        sess = agent_mod.Session(store, retriever, mode="resident")
        t0 = time.time()
        first_text_at = None
        tools = 0
        text_len = 0
        usage = {}
        for ev in agent_mod.run(sess, q):
            if ev["type"] == "text":
                if first_text_at is None:
                    first_text_at = time.time() - t0
                text_len += len(ev["text"])
            elif ev["type"] == "tool_call":
                tools += 1
            elif ev["type"] == "done":
                usage = ev.get("usage") or {}
        total = time.time() - t0

        rows.append({
            "场景": label,
            "提问": q[:28],
            "首字(秒)": round(first_text_at or total, 1),
            "完成(秒)": round(total, 1),
            "工具调用": tools,
            "回答字数": text_len,
            "输出token": usage.get("output_tokens", 0),
        })
        r = rows[-1]
        print(f"  [{label}] {q[:30]}")
        print(f"      首字 {r['首字(秒)']:>5.1f}s   完成 {r['完成(秒)']:>5.1f}s   "
              f"工具 {tools} 次   回答 {text_len} 字", flush=True)

    print()
    print("=" * 84)
    print("  汇总（可直接写进方案书）")
    print("=" * 84)

    firsts = [r["首字(秒)"] for r in rows]
    totals = [r["完成(秒)"] for r in rows]
    print(f"  首字响应   中位 {statistics.median(firsts):.1f}s   "
          f"最快 {min(firsts):.1f}s   最慢 {max(firsts):.1f}s")
    print(f"  完整回答   中位 {statistics.median(totals):.1f}s   "
          f"最快 {min(totals):.1f}s   最慢 {max(totals):.1f}s")
    print(f"  工具调用   平均 {statistics.mean([r['工具调用'] for r in rows]):.1f} 次/问")
    print(f"  回答篇幅   平均 {statistics.mean([r['回答字数'] for r in rows]):.0f} 字")

    out = config.CACHE_DIR / "bench_result.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "语料": {"政策数": s["政策数"], "条款数": s["条款数"], "地域": s["地域"]},
        "明细": rows,
        "汇总": {
            "首字中位秒": round(statistics.median(firsts), 1),
            "完成中位秒": round(statistics.median(totals), 1),
            "平均工具调用": round(statistics.mean([r["工具调用"] for r in rows]), 1),
        },
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  明细已存到 {out}")

    print("""
  对照实验怎么做（需要你手工做一次，50 秒的事，但数字很有分量）：

    1. 打开一个政府网站或政务 App，拿秒表
    2. 试着查清「82 岁本市户籍老人能领哪些补贴、分别要带什么材料」
    3. 记录：(a) 总共花了几分钟  (b) 翻了多少个页面  (c) 是否找到了完整答案

    这三个数字就是方案书里「传统方式」那一列。
    不要编，实测多少写多少——被追问时你才答得上来。
""")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
