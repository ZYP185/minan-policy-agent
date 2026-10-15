"""探测：居民问到本地库里没有的地区时，Agent 会怎么答？

这是最危险的失败模式——如果它拿别的城市的政策标准来回答广州居民，
居民照着办就会白跑甚至办错。
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import config
from core import agent as agent_mod
from core.llm import LLM
from core.retriever import Retriever
from core.store import PolicyStore

store = PolicyStore(config.POLICY_DIR, config.STRUCT_DIR).load()
retriever = Retriever(store, llm=LLM())

print(f"库里地域：{store.regions()}\n")
print("广州市 在库里吗：", any("广州" in p.region for p in store.policies.values()))
print()

session = agent_mod.Session(store, retriever, mode="resident")

QUESTIONS = [
    "我是广州市的，我家老人 82 岁，能领高龄津贴吗？能领多少？",
]

for q in QUESTIONS:
    print("=" * 76)
    print(f"👤 {q}")
    print("=" * 76)
    answer = ""
    for ev in agent_mod.run(session, q):
        if ev["type"] == "text":
            answer += ev["text"]
        elif ev["type"] == "tool_call":
            print(f"   🔧 {ev['name']}")
        elif ev["type"] == "tool_result":
            print(f"      → {'✓' if ev['ok'] else '✗'} {ev['summary']}")
        elif ev["type"] == "error":
            print(f"   ⚠ {ev['message']}")
    print(f"\n🤖\n{answer}\n")
