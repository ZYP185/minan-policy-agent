"""Agent 端到端冒烟测试（会真实调用模型）。

    python tests/test_agent.py            # 跑完整对话
    python tests/test_agent.py --quick    # 只跑一轮

验证重点：
  1. 模型会不会先反问收集信息，而不是直接给答案
  2. 工具调用链是否连得上（tool_use → tool_result → 下一轮）
  3. 规则引擎的判定能不能被模型正确复述
  4. 检索不到时会不会老实说"没找到"，而不是编造
"""
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import config
from core import agent as agent_mod
from core.retriever import Retriever
from core.store import PolicyStore

# ---- 构造一个最小语料：三份政策，覆盖养老 / 生育 / 就业 -------------------
POLICIES = [
    {
        "id": "t-gaoling",
        "title": "示范市高龄老年人津贴发放管理办法",
        "doc_number": "示民规〔2024〕3号",
        "issuer": "示范市民政局",
        "publish_date": "2024-03-15",
        "effective_date": "2024-05-01",
        "expiry_date": "2029-04-30",
        "region": "示范市",
        "category": "养老",
        "url": "https://example.gov.cn/gaoling",
        "full_text": (
            "第一条 为规范本市高龄老年人津贴发放管理工作，制定本办法。\n"
            "第二条 本办法适用于具有本市户籍且年满80周岁的老年人。\n"
            "第三条 高龄老年人津贴标准为：80至89周岁每人每月180元；"
            "90至99周岁每人每月350元；100周岁及以上每人每月600元。\n"
            "第四条 申请高龄津贴应当提交下列材料：居民身份证、居民户口簿、"
            "本人社会保障卡。\n"
            "第五条 申请人应当向户籍所在地的街道办事处提出申请。\n"
            "第六条 街道办事处应当自受理之日起15个工作日内完成审核。\n"
        ),
        "structured": {
            "policy_id": "t-gaoling", "category": "养老",
            "target_group": ["具有本市户籍且年满80周岁的老年人"],
            "benefit": [
                {"type": "现金补贴", "standard": "每人每月180元", "condition": "80至89周岁"},
                {"type": "现金补贴", "standard": "每人每月350元", "condition": "90至99周岁"},
            ],
            "materials": ["居民身份证", "居民户口簿", "本人社会保障卡"],
            "process": ["向户籍所在地街道办事处提出申请", "街道审核后按月发放"],
            "handler": "户籍所在地街道办事处", "channel": "线下窗口",
            "duration": "15个工作日", "form_name": "高龄老年人津贴申请审批表",
            "form_fields": ["姓名", "身份证号", "户籍地址", "联系电话"],
            "eligibility_rules": [
                {"field": "hukou", "op": "eq", "value": "本市户籍", "desc": "具有本市户籍"},
                {"field": "age", "op": "gte", "value": 80, "desc": "年满80周岁"},
            ],
            "_extracted": True,
        },
    },
    {
        "id": "t-yuer",
        "title": "示范市育儿补贴发放实施细则",
        "doc_number": "示卫健〔2024〕11号",
        "issuer": "示范市卫生健康委员会",
        "publish_date": "2024-06-01",
        "effective_date": "2024-07-01",
        "expiry_date": "2027-06-30",
        "region": "示范市",
        "category": "生育",
        "url": "https://example.gov.cn/yuer",
        "full_text": (
            "第一条 为减轻家庭养育负担，制定本细则。\n"
            "第二条 对本市户籍家庭中3周岁以下的婴幼儿，每人每年发放育儿补贴3600元。\n"
            "第三条 申请育儿补贴应当提交：婴幼儿出生医学证明、居民户口簿、"
            "申请人居民身份证、本人银行卡。\n"
            "第四条 申请人通过本市政务服务网或街道办事处提交申请。\n"
            "第五条 自受理之日起20个工作日内完成审核并发放。\n"
        ),
        "structured": {
            "policy_id": "t-yuer", "category": "生育",
            "target_group": ["本市户籍家庭中3周岁以下的婴幼儿"],
            "benefit": [{"type": "现金补贴", "standard": "每人每年3600元", "condition": "3周岁以下婴幼儿"}],
            "materials": ["婴幼儿出生医学证明", "居民户口簿", "申请人居民身份证", "本人银行卡"],
            "process": ["通过政务服务网或街道办事处提交申请", "审核通过后发放"],
            "handler": "街道办事处", "channel": "本市政务服务网 / 线下窗口",
            "duration": "20个工作日", "form_name": "育儿补贴申请表",
            "form_fields": ["申请人姓名", "身份证号", "婴幼儿姓名", "出生日期", "银行卡号"],
            "eligibility_rules": [
                {"field": "hukou", "op": "eq", "value": "本市户籍", "desc": "本市户籍家庭"},
                {"field": "family", "op": "contains", "value": ["3周岁以下", "婴幼儿"], "desc": "家有3周岁以下婴幼儿"},
            ],
            "_extracted": True,
        },
    },
    {
        "id": "t-jiuye",
        "title": "示范市就业困难人员灵活就业社会保险补贴办法",
        "doc_number": "示人社〔2024〕7号",
        "issuer": "示范市人力资源和社会保障局",
        "publish_date": "2024-04-10",
        "effective_date": "2024-06-01",
        "expiry_date": "",
        "region": "示范市",
        "category": "就业",
        "url": "https://example.gov.cn/jiuye",
        "full_text": (
            "第一条 为促进就业困难人员就业，制定本办法。\n"
            "第二条 本办法适用于经认定的本市就业困难人员，以灵活就业方式就业"
            "并按规定缴纳社会保险费的。\n"
            "第三条 补贴标准按其实际缴纳社会保险费的50%给予补贴，每人每月最高不超过500元。\n"
            "第四条 申请应当提交：就业创业证、居民身份证、社会保险缴费凭证、本人银行卡。\n"
            "第五条 向街道（乡镇）公共就业服务机构提出申请。\n"
        ),
        "structured": {
            "policy_id": "t-jiuye", "category": "就业",
            "target_group": ["经认定的本市就业困难人员", "以灵活就业方式就业并缴纳社保的人员"],
            "benefit": [{"type": "社会保险补贴", "standard": "实际缴纳社会保险费的50%，每人每月最高不超过500元", "condition": "灵活就业并缴纳社保"}],
            "materials": ["就业创业证", "居民身份证", "社会保险缴费凭证", "本人银行卡"],
            "process": ["向街道公共就业服务机构提出申请", "审核后按月发放补贴"],
            "handler": "街道（乡镇）公共就业服务机构", "channel": "线下窗口",
            "duration": "", "form_name": "灵活就业社会保险补贴申请表",
            "form_fields": ["姓名", "身份证号", "就业创业证号", "联系电话"],
            "eligibility_rules": [
                {"field": "employment", "op": "eq", "value": "灵活就业", "desc": "以灵活就业方式就业"},
                {"field": "insurance", "op": "contains", "value": ["社会保险", "社保"], "desc": "已缴纳社会保险费"},
            ],
            "_extracted": True,
        },
    },
]

# ---- 写临时语料 ----------------------------------------------------------
TMP = config.CACHE_DIR / "agent_test"
P_DIR = TMP / "policies"
S_DIR = TMP / "structured"
P_DIR.mkdir(parents=True, exist_ok=True)
S_DIR.mkdir(parents=True, exist_ok=True)

for p in POLICIES:
    struct = p.pop("structured")
    (P_DIR / f"{p['id']}.json").write_text(json.dumps(p, ensure_ascii=False, indent=2), encoding="utf-8")
    (S_DIR / f"{struct['policy_id']}.json").write_text(json.dumps(struct, ensure_ascii=False, indent=2), encoding="utf-8")

store = PolicyStore(P_DIR, S_DIR).load()
retriever = Retriever(store, llm=None)      # 冒烟测试关掉重排，省一次调用

print(f"语料：{len(store)} 份政策 / {len(store.clauses)} 条条款\n")


def run_turn(session, text, verbose=True):
    """跑一轮，返回 (最终文本, 工具调用序列, 卡片类型)。"""
    if verbose:
        print(f"\n{'─' * 66}\n👤 居民：{text}\n{'─' * 66}")

    text_out = ""
    tools = []
    artifacts = []
    for ev in agent_mod.run(session, text):
        t = ev["type"]
        if t == "text":
            text_out += ev["text"]
        elif t == "tool_call":
            tools.append(ev["name"])
            if verbose:
                print(f"   🔧 {ev['name']}({json.dumps(ev['args'], ensure_ascii=False)[:100]})")
        elif t == "tool_result":
            if verbose:
                print(f"      → {'✓' if ev['ok'] else '✗'} {ev['summary']}")
        elif t == "artifact":
            artifacts.append(ev["artifact"]["kind"])
        elif t == "error":
            print(f"   ⚠ 错误：{ev['message']}")
            text_out += f"[ERROR] {ev['message']}"

    if verbose and text_out:
        print(f"\n🤖 民安：\n{text_out}")
    return text_out, tools, artifacts


def main() -> int:
    quick = "--quick" in sys.argv
    s = agent_mod.Session(store, retriever, mode="resident")
    failures = []

    # ── 第 1 轮：模糊提问，期望反问而非直接作答 ────────────────────
    # 注意：这一轮不强制要求调用工具。面对"我家老人能领什么补贴"这种
    # 零信息量的提问，先问清年龄/户籍再检索，比空检索一次更省事，UX 也更好。
    # 真正的硬要求是「不许在信息不足时直接下结论」。
    t1, tools1, _ = run_turn(s, "我家老人能领什么补贴？")
    asked = any(w in t1 for w in ("？", "?", "请问", "是否", "多大", "多少岁", "户籍"))
    if not asked:
        failures.append("第1轮没有反问澄清，直接给答案（期望先追问）")
    print(f"\n   [检查] 是否反问：{'通过' if asked else '失败'}   工具：{tools1 or '(未调用)'}")

    if quick:
        print("\n（--quick 模式，只跑一轮）")
        return 0 if not failures else 1

    # ── 第 2 轮：补齐信息，期望触发资格体检 ────────────────────────
    t2, tools2, art2 = run_turn(
        s, "我母亲 82 岁，本市户籍，跟我一起住，退休了，每月退休金 3000 多。")
    if "assess_eligibility" not in tools2:
        failures.append("第2轮没有触发政策体检（assess_eligibility）")
    if "eligibility" not in art2:
        failures.append("第2轮没有产出体检卡片")
    if "180" not in t2 and "高龄" not in t2:
        failures.append("第2轮没有给出高龄津贴的关键信息")
    print(f"\n   [检查] 触发体检：{'通过' if 'assess_eligibility' in tools2 else '失败'}"
          f"   卡片：{art2}")

    # ── 第 3 轮：材料核对 ──────────────────────────────────────────
    t3, tools3, art3 = run_turn(s, "我带了身份证，还有户口本，够了吗？")
    if "check_materials" not in tools3:
        failures.append("第3轮没有做材料核对（check_materials）")
    print(f"\n   [检查] 材料核对：{'通过' if 'check_materials' in tools3 else '失败'}   卡片：{art3}")

    # ── 第 4 轮：幻觉测试 —— 库里没有的政策，必须承认不知道 ────────
    t4, tools4, _ = run_turn(s, "听说咱们市给养狗的家庭每月发 500 元宠物补贴，怎么申请？")
    hallucinated = "500" in t4 and "没" not in t4 and "未" not in t4 and "查" not in t4
    if hallucinated:
        failures.append("对不存在的『宠物补贴』编造了内容（幻觉）")
    print(f"\n   [检查] 抗幻觉：{'失败（编造了）' if hallucinated else '通过'}")

    print("\n" + "=" * 66)
    if failures:
        print("冒烟测试发现问题：")
        for f in failures:
            print(f"  ✗ {f}")
        return 1
    print("冒烟测试全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
