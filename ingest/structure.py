"""用 LLM 把政策原文抽成结构化字段。

最关键的是 eligibility_rules：把「年满80周岁的本市户籍老年人」这种自然语言
条件，翻译成 {"field": "age", "op": "gte", "value": 80} 这样的机器可判定规则。
有了它，「政策体检」就能给出确定性结论，而不是让模型每次重新"理解"一遍政策。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# 允许 `python ingest/structure.py` 直接运行
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.llm import LLM, LLMError, text_of

SYSTEM = """你是政务政策结构化抽取引擎。你的唯一任务是把政策原文转换成 JSON，不做任何解释和评价。
铁律：只抽取原文中明确写出的信息。原文没写的，一律留空数组或空字符串，绝对不要推测、不要补充常识。"""

PROMPT = """请从下面的政策原文中抽取结构化信息，输出严格的 JSON。

原文：
---
{text}
---

输出 JSON 格式（不要输出 ```json 代码块标记，直接输出 JSON 对象）：

{{
  "target_group": ["适用对象，逐条列出，用原文表述"],
  "benefit": [
    {{"type": "待遇类型，如 现金补贴/实物救助/费用减免/服务", "standard": "待遇标准，必须逐字引用原文中的金额或标准", "condition": "对应哪一类人可以享受该标准"}}
  ],
  "materials": ["申请所需材料，逐项列出"],
  "process": ["办理流程，按顺序列出，每步一句话"],
  "handler": "受理单位，如 户籍所在地街道办事处",
  "channel": "办理渠道，如 线下窗口/一网通办/随申办",
  "duration": "办理时限，如 15个工作日",
  "form_name": "需要填写的表格名称，没有则空字符串",
  "form_fields": ["申请表中通常需要填写的字段名，如 姓名/身份证号/联系电话/户籍地址"],
  "category": "政策类别，从这几个里选一个：养老、救助、生育、就业、医疗、住房、残疾人、教育、其他",
  "region": "政策的适用范围地域，**必须具体到市或区县**，如 上海市、上海市普陀区、巴中市、青阳县。根据正文的适用范围、发文机关或文号前缀判断。如果正文表明这是全国性政策（如国务院、部委面向全国发文），填 \\"全国\\"。",
  "eligibility_rules": [
    {{
      "field": "条件字段，用英文键：age/hukou/employment/income/health/housing/insurance/family/region",
      "op": "比较运算符，只能是 gte/lte/gt/lt/eq/ne/in/contains 之一",
      "value": "比较值",
      "desc": "用中文复述这条规则，如 年龄不低于80周岁"
    }}
  ]
}}

抽取 eligibility_rules 的要点（这是最重要的字段）：
- 只抽取**硬性准入条件**（不满足就不能申请），不要抽待遇档次划分。
  例如「80-89岁每月180元，90-99岁每月350元」是待遇分档，应放进 benefit，
  而「年满80周岁且具有本市户籍」才是 eligibility_rules。
- 户籍条件 → field="hukou"，value 用原文表述（如"本市户籍"）
- 年龄条件 → field="age"，op="gte"，value 用数字
- 收入条件 → field="income"，op="lte"，value 用数字（单位：元/月）
- 就业状态 → field="employment"，op="eq" 或 "in"
- 残疾/健康 → field="health"
- 如果某条政策的准入条件在原文里没有明确写，eligibility_rules 返回空数组 []。
"""


def _extract_json(raw: str) -> dict:
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("模型输出中没有 JSON 对象")
    return json.loads(raw[start:end + 1])


def structure_policy(policy: dict, llm: LLM, max_chars: int = 12000) -> dict:
    """对单份政策做结构化抽取。失败时返回带 error 的最小结构，不抛异常。"""
    text = policy.get("full_text", "")
    if not text.strip():
        return _empty(policy, "原文为空")

    clipped = text[:max_chars]
    try:
        msg = llm.complete(
            [{"role": "user", "content": PROMPT.format(text=clipped)}],
            system=SYSTEM,
            max_tokens=3000,
            temperature=0.0,
        )
        data = _extract_json(text_of(msg))
    except (LLMError, ValueError, json.JSONDecodeError) as e:
        return _empty(policy, f"{type(e).__name__}: {e}")

    out = _empty(policy, "")
    for key in ("target_group", "benefit", "materials", "process",
                "form_fields", "eligibility_rules"):
        v = data.get(key)
        out[key] = v if isinstance(v, list) else []
    for key in ("handler", "channel", "duration", "form_name", "category", "region"):
        v = data.get(key)
        out[key] = v if isinstance(v, str) else ""

    # 兜底：模型漏给 category 时用标题猜
    if not out["category"]:
        out["category"] = guess_category(policy.get("title", "") + text[:500])

    out["_source_chars"] = len(text)
    out["_extracted"] = True
    return out


def _empty(policy: dict, err: str) -> dict:
    return {
        "policy_id": policy.get("id", ""),
        "title": policy.get("title", ""),
        "target_group": [], "benefit": [], "materials": [], "process": [],
        "handler": "", "channel": "", "duration": "", "form_name": "",
        "form_fields": [], "category": "", "region": "", "eligibility_rules": [],
        "_extracted": False,
        "_error": err,
    }


CATEGORY_HINTS = [
    ("养老", ["高龄", "养老", "老年人", "退休", "敬老", "助餐", "护理"]),
    ("救助", ["最低生活保障", "低保", "特困", "临时救助", "救济", "困难群众", "救助"]),
    ("生育", ["生育", "育儿", "产假", "母婴", "托育", "三孩", "二孩"]),
    ("就业", ["就业", "失业", "创业", "职业培训", "社会保险补贴", "灵活就业", "工伤"]),
    ("医疗", ["医疗", "医保", "大病", "门诊", "住院", "报销", "救助供养"]),
    ("住房", ["住房", "公租房", "保障性租赁", "廉租", "购房", "住房公积金", "安居"]),
    ("残疾人", ["残疾", "残疾人", "伤残", "无障碍"]),
    ("教育", ["教育", "助学", "学费", "学生", "义务教育", "幼儿园"]),
]


def guess_category(text: str) -> str:
    scores: dict[str, int] = {}
    for cat, words in CATEGORY_HINTS:
        s = sum(text.count(w) for w in words)
        if s:
            scores[cat] = s
    return max(scores, key=scores.get) if scores else "其他"


if __name__ == "__main__":
    sample = {
        "id": "demo-1",
        "title": "上海市高龄老年人津贴发放管理办法",
        "full_text": "第二条 本办法适用于具有本市户籍且年满80周岁的老年人。"
                     "第三条 高龄老年人津贴标准为：80至89周岁每人每月180元；"
                     "90至99周岁每人每月350元；100周岁及以上每人每月600元。"
                     "第四条 申请应当提交：居民身份证、居民户口簿、本人社会保障卡。"
                     "第五条 向户籍所在地街道办事处提出申请。"
                     "第六条 自受理之日起15个工作日内完成审核。"
                     "第七条 本办法自2024年5月1日起施行。",
    }
    r = structure_policy(sample, LLM())
    print(json.dumps(r, ensure_ascii=False, indent=2))
