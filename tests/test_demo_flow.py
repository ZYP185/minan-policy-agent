"""按《演示脚本.md》的实际话术，在真实语料上走一遍完整演示流程。

这是最贴近现场的验证——如果这个过了，台上就不会翻车。
"""
import re
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
session = agent_mod.Session(store, retriever, mode="resident")
print(f"语料：{len(store)} 份政策 / {len(store.clauses)} 条条款\n")

SCRIPT = [
    # 第一幕故意不说城市：语料跨 10 个地区，正确行为是先问清再答，
    # 而不是抓一份上海的政策把金额报给可能是广州的居民。
    ("第一幕", "我家老人 82 岁了，能领什么补贴？"),
    ("第二幕", "我们是上海的。我母亲 82 岁，本市户籍，跟我一起住，退休了，每月退休金 3000 多。"),
    ("第三幕", "我带了身份证，还有户口本，够了吗？"),
    ("第三幕", "那帮我填个申请表吧。"),
    ("第四幕", "听说咱们市给养狗的家庭每月发 500 元宠物补贴，怎么申请？"),
]

report = []
all_errors = []

for act, text in SCRIPT:
    print("=" * 76)
    print(f"【{act}】👤 居民：{text}")
    print("=" * 76)
    answer, tools, cards = "", [], []
    for ev in agent_mod.run(session, text):
        t = ev["type"]
        if t == "text":
            answer += ev["text"]
        elif t == "tool_call":
            tools.append(ev["name"])
            print(f"   🔧 {ev['name']}")
        elif t == "tool_result":
            print(f"      → {'✓' if ev['ok'] else '✗'} {ev['summary']}")
            if not ev["ok"]:
                all_errors.append(f"{act} {ev['name']}: {ev['summary']}")
        elif t == "artifact":
            cards.append(ev["artifact"]["kind"])
        elif t == "error":
            all_errors.append(f"{act} 流错误: {ev['message']}")
    print(f"\n{answer}\n")
    report.append((act, text, tools, cards, answer))

# ================================================================ 断言
print("=" * 76)
print("  演示流程检查")
print("=" * 76)
failures = []

# 第一幕：居民没说城市。正确行为是**问清地区再答**，绝不能凭空报一个金额——
# 语料跨 10 个地区，上海的 180 元对广州居民是错误信息。
# 所以这里要断言的是"守住"：可以给方向性说法，但不能给具体金额。
r1 = report[0]
f1 = []
if not any(w in r1[4] for w in ("？", "?", "哪个", "请问", "是吗")):
    f1.append("没有追问地区或条件")
if re.search(r"\d+\s*(元|万元)", r1[4]):
    f1.append("在不知道地区的情况下报出了具体金额（可能拿了别处的标准）")
if "assess_eligibility" in r1[2] and "check_coverage" not in r1[2]:
    f1.append("调了体检却没有先确认地区覆盖")
failures.extend(f"第一幕：{x}" for x in f1)
print(f"  第一幕 未定地区时守住      {'✓' if not f1 else '✗'}   "
      f"工具 {r1[2]}{'' if not f1 else '  ← ' + '；'.join(f1)}")

# 断言"到第二幕结束时已检索过 + 做过体检"，而不是"第二轮必须再检索一次"。
# 第一幕可能已经检索并读过原文了，第二轮直接复用上下文是更高效的正确行为。
r2 = report[1]
cumulative = set(r1[2]) | set(r2[2])
f2 = []
if "check_coverage" not in r2[2]:
    f2.append("居民说了城市，却没有先查覆盖")
need = {"search_policy", "assess_eligibility"}
missing = need - cumulative
if missing:
    f2.append(f"累计仍缺少工具调用 {missing}")
if "eligibility" not in r2[3]:
    f2.append("没有产出政策匹配卡片")
if not any(k in r2[4] for k in ("元", "津贴", "补贴")):
    f2.append("回答里没有具体待遇信息")
failures.extend(f"第二幕：{x}" for x in f2)
print(f"  第二幕 查覆盖+体检          {'✓' if not f2 else '✗'}   "
      f"本轮 {r2[2]}  卡片 {r2[3]}{'' if not f2 else '  ← ' + '；'.join(f2)}")

r3 = report[2]
if "check_materials" not in r3[2]:
    failures.append("第三幕：没有做材料核对")
if "materials" not in r3[3]:
    failures.append("第三幕：没有产出材料核对卡片")
print(f"  第三幕 材料核对          {'✓' if 'materials' in r3[3] else '✗'}   卡片 {r3[3]}")

r4 = report[3]
if "form" not in r4[3]:
    failures.append("第三幕：没有产出申请表草稿卡片")
print(f"  第三幕 申请表草稿          {'✓' if 'form' in r4[3] else '✗'}   卡片 {r4[3]}")

r5 = report[4]
hallucinated = "500" in r5[4] and not any(w in r5[4] for w in ("没有", "未", "查不到", "找不到", "不存在"))
if hallucinated:
    failures.append("第四幕：对不存在的宠物补贴编造了内容")
print(f"  第四幕 抗幻觉          {'✗ 编造了' if hallucinated else '✓'}")

# ── 面向普通居民的可读性（这是产品要求，不是锦上添花）──────────────
# 1) 主文里不应出现条款引用 —— 居民不关心条款号，溯源交给右侧面板
cite_count = sum(r[4].count("【依据") for r in report)
if cite_count:
    worst = max(report, key=lambda r: r[4].count("【依据"))
    failures.append(f"主文里出现了 {cite_count} 处【依据：…】引用（应移到右侧溯源面板）")
print(f"  主文引用标注            {'✓ 已清除' if not cite_count else f'✗ 仍有 {cite_count} 处'}")

# 2) 不该用 ### 小标题堆结构
head_count = sum(r[4].count("###") for r in report)
if head_count:
    failures.append(f"回答里出现了 {head_count} 个「###」小标题（应改用自然段落）")
print(f"  小标题堆砌              {'✓ 无' if not head_count else f'✗ 有 {head_count} 个'}")

# 3) 给出待遇结论的那一轮，第一句就得是答案，而不是「根据某某办法」
answer_turn = next((r for r in report if any(
    k in r[4] for k in ("元", "每月", "每年"))), None)
if answer_turn:
    first = next((ln.strip() for ln in answer_turn[4].split("\n") if ln.strip()), "")
    lead_ok = any(k in first[:60] for k in ("元", "能领", "符合", "可以领", "补贴", "津贴"))
    if not lead_ok:
        failures.append(f"结论没有先行，首句是：{first[:50]}")
    print(f"  结论先行                {'✓' if lead_ok else '✗'}   首句：{first[:44]}")

# 4) 篇幅：一次回答不该长到看不下去（按中文正文字数算）
for act, text, tools, cards, ans in report:
    n = len(ans)
    if n > 900:
        failures.append(f"{act}：回答长达 {n} 字，超出可读上限")
print(f"  篇幅                    " + "  ".join(f"{a}={len(x[4])}字" for a, x in
      zip([r[0] for r in report], report)))

# 5) 不许出现英文整句 —— 模型调用工具前爱用英文"自言自语"，
#    居民会原样看到，例如 "I'll record this and run the eligibility check."
EN_SENTENCE = re.compile(r"[A-Za-z][A-Za-z,' ]{24,}")
leaks = []
for act, text, tools, cards, ans in report:
    for m in EN_SENTENCE.finditer(ans):
        leaks.append(f"{act}: {m.group(0).strip()[:60]}")
if leaks:
    failures.append(f"回答里出现 {len(leaks)} 处英文整句")
    for l in leaks[:4]:
        print(f"     ! 英文泄漏 → {l}")
print(f"  英文泄漏                {'✓ 无' if not leaks else f'✗ {len(leaks)} 处'}")

if all_errors:
    failures.append(f"工具调用出现 {len(all_errors)} 次失败")
    for e in all_errors[:5]:
        print(f"     ! {e}")
print(f"  工具调用失败次数          {len(all_errors)}")

print()
if failures:
    print("发现问题：")
    for f in failures:
        print(f"  ✗ {f}")
    sys.exit(1)
print("演示流程全部通过 —— 这套话术可以直接上台")
sys.exit(0)
