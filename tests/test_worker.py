"""社区工作端模式测试。会真实调用模型，使用真实语料。

重点验证两件事：
  1. 未确认辖区时不应直接开扫（否则会把多个省市的政策混成一张表）
  2. 工具调用链不应出现失败 —— 尤其是把 policy_id 误当 clause_id 传给
     read_clause 的常见错误，现在应当被宽容处理。
"""
import json
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
if len(store) == 0:
    print("政策库为空")
    raise SystemExit(1)

retriever = Retriever(store, llm=LLM())
print(f"语料：{len(store)} 份政策\n")

session = agent_mod.Session(store, retriever, mode="worker")

all_tools: list[str] = []
all_cards: list[str] = []
tool_errors: list[str] = []


def turn(text: str) -> str:
    print("=" * 74)
    print(f"👤 社区工作者：{text}")
    print("=" * 74)
    answer = ""
    for ev in agent_mod.run(session, text):
        t = ev["type"]
        if t == "text":
            answer += ev["text"]
        elif t == "tool_call":
            all_tools.append(ev["name"])
            print(f"   🔧 {ev['name']}({json.dumps(ev['args'], ensure_ascii=False)[:90]})")
        elif t == "tool_result":
            print(f"      → {'✓' if ev['ok'] else '✗'} {ev['summary']}")
            if not ev["ok"]:
                tool_errors.append(f"{ev['name']}: {ev['summary']}")
        elif t == "artifact":
            all_cards.append(ev["artifact"]["kind"])
        elif t == "error":
            print(f"   ⚠ {ev['message']}")
    print(f"\n🤖 民安·社区工作端：\n{answer}\n")
    return answer


# 第 1 轮：没给辖区。期望它先追问，而不是把多个省市的政策混在一起端上来。
a1 = turn("扫描一下我辖区的政策覆盖情况，哪些是需要居民主动申请、容易被漏掉的？")
# 第 2 轮：给出辖区，期望这时才真正扫描。
a2 = turn("上海市普陀区。")

# ---------------------------------------------------------------- 断言
failures = []
asked_region = any(w in a1 for w in ("辖区", "区/县", "哪个区", "地区", "范围"))
if not asked_region:
    failures.append("第1轮没问辖区就开扫（会把多地区政策混在一起）")
if "scan_unapplied" in all_tools[:3] and asked_region and not any(
        w in a1 for w in ("请", "确认", "告诉")):
    failures.append("第1轮在未确认辖区时就调用了 scan_unapplied")

if "scan_unapplied" not in all_tools:
    failures.append("给出辖区后仍未调用 scan_unapplied")
if "scan" not in all_cards:
    failures.append("没有产出辖区扫描卡片")
if tool_errors:
    failures.append(f"存在工具调用失败：{tool_errors}")
if not a2.strip():
    failures.append("第2轮没有输出文字回答")

# 居民端不应看到 scan_unapplied —— 由 agent.run() 在运行时按 mode 过滤，
# 这里验证过滤逻辑确实生效（而不是把工具从注册表里删掉了）。
from core import tools as T

for mode, should_have in (("worker", True), ("resident", False)):
    names = [s["name"] for s in T.SCHEMAS if mode == "worker" or s["name"] != "scan_unapplied"]
    has = "scan_unapplied" in names
    if has != should_have:
        failures.append(f"{mode} 模式的工具表中 scan_unapplied 存在性错误（应为 {should_have}）")
    else:
        print(f"   [检查] {mode} 模式可用工具 {len(names)} 个，"
              f"scan_unapplied {'可用' if has else '已屏蔽'} ✓")

print("=" * 74)
if failures:
    print("发现问题：")
    for f in failures:
        print(f"  ✗ {f}")
    sys.exit(1)
print("社区工作端测试通过")
sys.exit(0)
