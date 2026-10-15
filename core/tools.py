"""Agent 工具集。

每个工具都是「确定性代码 + 结构化返回」，而不是让模型凭空生成。
这是本 Demo 与普通 RAG 问答的关键区别：模型负责理解和推理，
事实性内容（补贴金额、材料清单、办理流程）全部由工具从政策原文里取。
"""
from __future__ import annotations

import json
import re
from datetime import date
from typing import Any, Callable

import config
from core.bm25 import score_terms
from core.retriever import Hit, Retriever
from core.store import Policy, PolicyStore
from core.tokenizer import normalize, tokenize

# ---------------------------------------------------------------- 画像槽位
PROFILE_FIELDS: dict[str, str] = {
    "region": "所在区/街道",
    "hukou": "户籍情况（本市户籍 / 非本市户籍 / 不清楚）",
    "age": "年龄（数字）",
    "employment": "就业状态（在职 / 失业 / 灵活就业 / 退休 / 学生）",
    "income": "家庭人均月收入（元，数字或区间）",
    "family": "家庭情况（如：已婚育有一孩 / 独居 / 与子女同住）",
    "health": "健康状况（健康 / 残疾并注明等级 / 患有大病）",
    "housing": "住房情况（自有房 / 租房 / 借住 / 无房）",
    "insurance": "参保情况（职工社保 / 居民社保 / 未参保）",
}

# 核心字段：决定大多数政策的准入。这几项到手就足以做一次有价值的体检，
# 其余字段属于"问到再说"，绝不能因为它们缺失就卡住不给结论。
CORE_FIELDS = ("hukou", "age", "employment")

# 核心字段到手即视为"可以体检"，其余字段由 assess_eligibility 按政策需要
# 逐项回报"需补充信息"，由居民决定要不要补 —— 而不是开场审问一遍。
READY_THRESHOLD = 2


class ToolError(Exception):
    """工具执行失败，错误信息会回传给模型让它自我修正。"""


class ToolContext:
    """一次会话的上下文：居民画像、已产生的卡件、检索器引用。"""

    def __init__(self, store: PolicyStore, retriever: Retriever, llm=None) -> None:
        self.store = store
        self.retriever = retriever
        self.llm = llm
        self.profile: dict[str, Any] = {}
        self.artifacts: list[dict] = []      # 前端要渲染的卡片
        self.cited: list[dict] = []          # 已引用的条款，供溯源面板使用
        self.last_hits: dict[str, Hit] = {}  # clause_id -> Hit

    def add_artifact(self, kind: str, title: str, payload: dict) -> dict:
        art = {"kind": kind, "title": title, "payload": payload}
        self.artifacts.append(art)
        return art

    def cite(self, hits: list[Hit]) -> None:
        for h in hits:
            if not any(c["clause_id"] == h.clause.id for c in self.cited):
                self.cited.append(h.to_dict())


# ================================================================ 工具实现
def tool_update_profile(ctx: ToolContext, fields: dict) -> dict:
    """记录/更新居民画像槽位。"""
    if not isinstance(fields, dict) or not fields:
        raise ToolError("fields 必须是非空对象，例如 {\"age\": 82, \"hukou\": \"本市户籍\"}")

    unknown = [k for k in fields if k not in PROFILE_FIELDS]
    for k, v in fields.items():
        if k in PROFILE_FIELDS and v not in (None, ""):
            ctx.profile[k] = v

    got_core = [k for k in CORE_FIELDS if ctx.profile.get(k) not in (None, "")]
    missing_core = [k for k in CORE_FIELDS if k not in got_core]
    other_missing = [
        k for k in PROFILE_FIELDS
        if k not in CORE_FIELDS and ctx.profile.get(k) in (None, "")
    ]

    ready = len(got_core) >= READY_THRESHOLD
    note = (
        "画像已更新。"
        + ("核心信息已够，**现在就应该调用 assess_eligibility 做体检**，"
           "不要再追问剩下的一般字段——体检结果会逐项告诉居民还缺什么。"
           if ready else
           f"核心信息还差 {len(missing_core)} 项，再问 1-2 个就够做体检了。")
        + " 只在居民主动提到时才记录一般字段，绝不要为了填满画像而逐项审问。"
    )

    return {
        "ok": True,
        "profile": ctx.profile,
        "ignored_fields": unknown,
        "core_ready": ready,
        # 只暴露还缺的核心字段；一般字段不再作为"待办清单"抛给模型
        "missing_core_fields": missing_core,
        "missing_core_labels": [PROFILE_FIELDS[k] for k in missing_core],
        "unasked_optional_count": len(other_missing),
        "note": note,
    }


def tool_search_policy(ctx: ToolContext, query: str, region: str = "",
                       category: str = "", include_expired: bool = False,
                       top_k: int = 5) -> dict:
    """检索政策条款。"""
    if not query or not query.strip():
        raise ToolError("query 不能为空")

    reg = region or _profile_region(ctx) or config.DEFAULT_REGION or None
    hits = ctx.retriever.search(
        query=query,
        region=reg,
        category=category or None,
        valid_only=not include_expired,
        top_k=max(1, min(int(top_k or 5), 10)),
        rerank=True,
    )

    for h in hits:
        ctx.last_hits[h.clause.id] = h

    if not hits:
        return {
            "found": 0,
            "results": [],
            "hint": "未检索到相关政策条款。可以换用更书面的关键词再试一次"
                    "（例如把『失业了没钱』换成『失业保险金』『就业困难人员』『社会保险补贴』）。"
                    "如果换词后仍然检索不到，必须如实告诉居民没有找到相关政策，"
                    "绝对不要凭常识编造政策内容。",
        }

    ctx.cite(hits)
    return {
        "found": len(hits),
        "search_region": reg or "(未限定)",
        "results": [h.to_dict() for h in hits],
    }


_POLICY_CLAUSE_CAP = 25


def tool_read_clause(ctx: ToolContext, clause_id: str) -> dict:
    """读取某一条款的完整原文（search 返回的是截断预览）。

    同时接受 policy_id —— scan_unapplied 之类只给 policy_id 的工具，
    常常诱使模型把政策 id 当条款 id 传进来。与其报错让它重试（演示时
    会看到一串红色失败标记），不如宽容处理：给整份政策的条款清单。
    """
    clause_id = (clause_id or "").strip()
    h = ctx.last_hits.get(clause_id) or ctx.retriever.fetch(clause_id)

    if not h and clause_id in ctx.store.policies:
        return _whole_policy(ctx, ctx.store.get(clause_id))

    if not h:
        # 再试一次：可能是带 # 后缀的半截 id
        pid = clause_id.split("#")[0]
        if pid in ctx.store.policies:
            return _whole_policy(ctx, ctx.store.get(pid))
        raise ToolError(
            f"找不到 {clause_id}。可传的值有两类："
            "search_policy 返回的 clause_id（形如 p1a2b3c4#3），"
            "或政策 id（形如 p1a2b3c4）——传政策 id 会返回该政策全部条款。"
        )

    ctx.last_hits[h.clause.id] = h
    p = h.policy
    # 同一份政策的相邻条款一起给出，避免断章取义
    idx = next((i for i, c in enumerate(p.clauses) if c.id == h.clause.id), 0)
    neighbors = p.clauses[max(0, idx - 1): idx + 2]
    ctx.cite([h])
    return {
        "clause_id": h.clause.id,
        "clause_no": h.clause.no,
        "text": h.clause.text,
        "policy": _policy_meta(p),
        "context_clauses": [
            {"no": c.no, "text": c.text[:400]} for c in neighbors if c.id != h.clause.id
        ],
    }


def _whole_policy(ctx: ToolContext, p: Policy | None) -> dict:
    """传进来的是政策 id：返回该政策的条款清单。"""
    if not p:
        raise ToolError("政策不存在")
    kept = p.clauses[:_POLICY_CLAUSE_CAP]
    return {
        "requested": "policy",
        "policy": _policy_meta(p),
        "clause_count": len(p.clauses),
        "clauses": [{"clause_id": c.id, "no": c.no, "text": c.text} for c in kept],
        "note": (
            f"这是《{p.title}》的前 {len(kept)} 条（共 {len(p.clauses)} 条）。"
            "如需定位特定内容，用 search_policy 带关键词检索更省事。"
            + ("" if len(kept) == len(p.clauses) else " 条款较多，已截断。")
        ),
    }


def _policy_meta(p: Policy) -> dict:
    return {
        "id": p.id,
        "title": p.title,
        "doc_number": p.doc_number,
        "issuer": p.issuer,
        "publish_date": p.publish_date,
        "effective_date": p.effective_date,
        "expiry_date": p.expiry_date,
        "status": p.status_on(),
        "region": p.region,
        "category": p.category,
        "url": p.url,
    }


def tool_assess_eligibility(ctx: ToolContext, policy_ids: list[str] | None = None,
                            limit: int = 12) -> dict:
    """把居民画像与政策的资格规则逐条比对，给出确定性的符合度判定。

    这是「政策体检」的核心：判定由规则引擎做，不由模型猜。
    """
    profile = ctx.profile
    if not profile:
        raise ToolError("居民画像还是空的，请先用 update_profile 收集基本信息。")

    if policy_ids:
        targets = [ctx.store.get(p) for p in policy_ids]
        targets = [p for p in targets if p]
    else:
        targets = ctx.store.filter(
            category=None, valid_only=True,
        )[: max(1, min(int(limit or 12), 50))]

    results = []
    for p in targets:
        rules = (p.structured or {}).get("eligibility_rules") or []
        checkable = [r for r in rules if isinstance(r, dict)]
        if not checkable:
            results.append({
                "policy_id": p.id,
                "title": p.title,
                "category": p.category,
                "verdict": "需人工确认",
                "reason": "该政策未抽取到结构化资格条件，需阅读原文判断。",
                "matched": [], "unmatched": [], "unknown": [],
                "url": p.url,
            })
            continue

        matched, unmatched, unknown = [], [], []
        for r in checkable:
            r_field = r.get("field", "")
            value = _lookup_profile(profile, r_field)
            desc = r.get("desc") or f"{r_field} {r.get('op')} {r.get('value')}"
            if value is None:
                unknown.append(desc)
                continue
            if _eval_rule(r, value):
                matched.append(desc)
            else:
                unmatched.append(desc)

        if unmatched:
            verdict, reason = "不符合", "存在不满足的硬性条件：" + "；".join(unmatched)
        elif unknown and not matched:
            verdict, reason = "需补充信息", "关键条件信息不足：" + "；".join(unknown)
        elif unknown:
            verdict, reason = "可能符合", "已满足 " + str(len(matched)) + " 项条件，" \
                                              "仍有信息待确认：" + "；".join(unknown)
        else:
            verdict, reason = "符合", "全部 " + str(len(matched)) + " 项条件均满足"

        results.append({
            "policy_id": p.id,
            "title": p.title,
            "category": p.category,
            "verdict": verdict,
            "reason": reason,
            "matched": matched, "unmatched": unmatched, "unknown": unknown,
            "benefit": (p.structured or {}).get("benefit", []),
            "url": p.url,
        })

    order = {"符合": 0, "可能符合": 1, "需补充信息": 2, "需人工确认": 3, "不符合": 4}
    results.sort(key=lambda r: order.get(r["verdict"], 9))
    hit_results = [r for r in results if r["verdict"] in ("符合", "可能符合")]

    ctx.add_artifact("eligibility", "政策匹配结果", {
        "profile": dict(profile),
        "results": results,
        "hit_count": len(hit_results),
    })

    return {
        "profile_used": profile,
        "summary": {
            "符合": sum(1 for r in results if r["verdict"] == "符合"),
            "可能符合": sum(1 for r in results if r["verdict"] == "可能符合"),
            "需补充信息": sum(1 for r in results if r["verdict"] == "需补充信息"),
            "不符合": sum(1 for r in results if r["verdict"] == "不符合"),
        },
        "results": results,
        "note": "verdict 是规则引擎的确定性判定，请据此回答，不要自行改变结论。"
                "对『需补充信息』的政策，要告诉居民还需确认什么。",
    }


# 原文里出现这些词，说明那句话在讲申请材料
_RE_MAT_SENTENCE = re.compile(
    r"[^。；\n]*(?:应当)?(?:提交|提供|携带|出具|交验|附)[^。；\n]*"
    r"(?:材料|证件|证明|复印件|原件|资料)[^。；\n]*[。；]?"
)


def _sibling_policies(ctx: ToolContext, p: Policy, limit: int = 3) -> list[Policy]:
    """找"讲同一件事、但材料清单更全"的政策。

    政府发文常有这种情况：一份《关于完善XX制度的通知》定框架，
    另一份《XX发放管理办法》写细则。前者没材料清单，后者有。
    居民来办的是细则，所以要把细则找出来。
    """
    if not p.clauses:
        return []
    key_tokens = set(tokenize(p.title)) | {
        t for c in p.clauses[:3] for t in tokenize(c.text)
    }
    if not key_tokens:
        return []

    out: list[Policy] = []
    for other in ctx.store.policies.values():
        if other.id == p.id or other.region != p.region:
            continue
        if not (other.structured or {}).get("materials"):
            continue
        overlap = score_terms(list(key_tokens), tokenize(other.title))
        if overlap >= 0.45:
            out.append(other)
    out.sort(key=lambda o: len(o.structured.get("materials") or []), reverse=True)
    return out[:limit]


def _materials_from_text(policy: Policy) -> list[str]:
    """结构化数据里没有材料清单时，回原文里抠。

    有些政策是框架性通知（例如《关于完善XX制度的通知》），本身不列材料，
    但同一事项往往另有一份《XX发放管理办法》写明了。抠不到就如实返回空。
    """
    found: list[str] = []
    for c in policy.clauses:
        for sent in _RE_MAT_SENTENCE.findall(c.text):
            sent = sent.strip()
            if len(sent) < 8:
                continue
            if sent not in found:
                found.append(sent)
    return found[:4]


def tool_check_materials(ctx: ToolContext, policy_id: str, owned: list[str] | None = None) -> dict:
    """核对居民已有的材料，列出还缺哪些。"""
    p = ctx.store.get(policy_id)
    if not p:
        raise ToolError(f"找不到政策 {policy_id}")

    required = (p.structured or {}).get("materials") or []
    from_text = False
    if not required:
        # 结构化没抽到，退一步去原文里找
        required = _materials_from_text(p)
        from_text = bool(required)

    if not required:
        # 确实是"不需要提交材料"的政策（例如系统自动发放的津贴）。
        # 这不是错误，要如实告诉居民，而不是报错。
        siblings = _sibling_policies(ctx, p)
        note = (f"《{p.title}》的原文中没有规定需要居民提交的申请材料——"
                "这类政策通常由系统按数据比对后主动发放，居民无需申请。"
                "请如实告知居民，不要臆造材料清单。")
        if siblings:
            note += (" 但同一事项还有这些政策写明了材料，如果居民要办的是那项，"
                     "请改用对应的 policy_id 核对：" +
                     "；".join(f"《{s.title}》({s.id})" for s in siblings))
        result = {
            "policy_id": p.id,
            "policy_title": p.title,
            "required": [],
            "already_have": [],
            "missing": [],
            "is_complete": True,
            "related_policies": [{"policy_id": s.id, "title": s.title} for s in siblings],
            "note": note,
        }
        ctx.add_artifact("materials", f"材料核对 · {p.title}", result)
        return result

    owned = owned or []
    owned_norm = [normalize(o) for o in owned if o]
    owned_tokens = [set(tokenize(o)) for o in owned_norm]

    have, missing = [], []
    for req in required:
        req_n = normalize(str(req))
        req_tok = set(tokenize(req_n))
        best = 0.0
        for o_norm, o_tok in zip(owned_norm, owned_tokens):
            if req_n and (req_n in o_norm or o_norm in req_n):
                best = 1.0
                break
            best = max(best, score_terms(req_tok, o_tok))
        (have if best >= 0.34 else missing).append(str(req))

    note = "已按字面比对，如有同义说法（如『户口本』与『户口簿』）可能误判，请向居民确认。"
    if from_text:
        note += " 该政策的材料清单是从原文条款中直接摘出的（未经过结构化抽取），请以原文为准。"

    result = {
        "policy_id": p.id,
        "policy_title": p.title,
        "required": required,
        "already_have": have,
        "missing": missing,
        "is_complete": not missing,
        "from_raw_text": from_text,
        "note": note,
    }
    ctx.add_artifact("materials", f"材料核对 · {p.title}", result)
    return result


def tool_generate_form(ctx: ToolContext, policy_id: str) -> dict:
    """根据居民画像生成预填的申请表草稿。"""
    p = ctx.store.get(policy_id)
    if not p:
        raise ToolError(f"找不到政策 {policy_id}")

    form_spec = (p.structured or {}).get("form_fields") or []
    values: list[dict] = []
    for f in form_spec:
        if isinstance(f, str):
            name, key = f, f
        else:
            name = f.get("name", "")
            key = f.get("key") or name
        v = _lookup_profile(ctx.profile, key) or _lookup_profile(ctx.profile, name)
        values.append({"name": name, "value": v if v is not None else "", "filled": v is not None})

    filled = sum(1 for v in values if v["filled"])
    payload = {
        "policy_id": p.id,
        "policy_title": p.title,
        "doc_number": p.doc_number,
        "issuer": p.issuer,
        "form_name": (p.structured or {}).get("form_name") or f"《{p.title}》申请表",
        "fields": values,
        "filled_count": filled,
        "total_count": len(values),
        "handler": (p.structured or {}).get("handler", ""),
        "duration": (p.structured or {}).get("duration", ""),
    }
    ctx.add_artifact("form", f"申请表草稿 · {p.title}", payload)
    return {**payload, "note": "这是预填草稿，需居民核对后到官方渠道正式提交。"}


def tool_get_process(ctx: ToolContext, policy_id: str) -> dict:
    """取办理流程、办理地点、时限。"""
    p = ctx.store.get(policy_id)
    if not p:
        raise ToolError(f"找不到政策 {policy_id}")
    s = p.structured or {}
    result = {
        "policy_id": p.id,
        "policy_title": p.title,
        "process": s.get("process") or [],
        "channel": s.get("channel", ""),
        "handler": s.get("handler", ""),
        "duration": s.get("duration", ""),
        "materials": s.get("materials") or [],
        "has_structured": bool(s),
    }
    if not s:
        result["note"] = "该政策尚无结构化流程数据，请用 read_clause 读原文后自行梳理，并注明是依据原文归纳。"
    ctx.add_artifact("process", f"办理指引 · {p.title}", result)
    return result


# 民生政策的标准类别。用来判断"库里缺哪一块"。
STANDARD_CATEGORIES = ["养老", "救助", "生育", "就业", "医疗", "住房", "残疾人", "教育"]


def tool_check_coverage(ctx: ToolContext, region: str = "") -> dict:
    """【先查这个】检查政策库对某地区的覆盖情况。

    在回答任何涉及具体地区的民生问题之前调用。作用是让系统**确定性地知道
    自己缺什么**，而不是等检索完才隐约意识到"好像没找到本地的"。

    拿别的城市的政策标准回答本地居民，是这类系统最危险的失败模式——
    居民照着办会白跑一趟。这个工具就是为了在那之前拦住。
    """
    region = (region or "").strip()
    store = ctx.store

    local: list[Policy] = []
    national: list[Policy] = []
    for p in store.policies.values():
        if not p.region:
            national.append(p)
        elif not region or region in p.region or p.region in region:
            local.append(p)

    covered_cats = sorted({p.category for p in local if p.category})
    national_cats = sorted({p.category for p in national if p.category})
    missing = [c for c in STANDARD_CATEGORIES if c not in covered_cats]

    # 其他地区（帮居民理解为什么没有本地的）
    others = sorted({p.region for p in store.policies.values() if p.region})

    if not region:
        advice = ("居民没有说明所在地区。请先问一句「您是在哪个城市/区？」，"
                  "再判断本地政策是否覆盖。")
        covered = bool(store.policies)
    elif local:
        covered = True
        advice = (f"库里有 {len(local)} 份 {region} 的地方政策，"
                  f"另有 {len(national)} 份全国性政策同样适用。可以正常检索回答。"
                  + (f" 但以下类别暂无本地政策：{'、'.join(missing)}。"
                     "涉及这些类别时，要说明只有全国性规定、本地细则未收录。"
                     if missing else ""))
    else:
        covered = False
        advice = (
            f"⚠ 政策库里**没有 {region} 的地方政策**。"
            f"现有地方政策覆盖：{'、'.join(others) if others else '（无）'}；"
            f"另有 {len(national)} 份全国性政策。"
            "**绝对不要拿其他城市的待遇标准回答该地区居民**——各城市标准不同，"
            "给了会误导。正确做法：说明库里没有该地区的地方政策，"
            "能讲的只有全国性制度框架，并建议拨打 12345 或去当地居委会咨询。"
        )

    return {
        "region": region or "(未指定)",
        "covered": covered,
        "local_policy_count": len(local),
        "national_policy_count": len(national),
        "local_categories": covered_cats,
        "missing_categories": missing if region else [],
        "regions_in_library": others,
        "sample_local_titles": [p.title[:40] for p in local[:5]],
        "advice": advice,
        "note": "这是覆盖情况查询，不是政策检索。据此决定要不要回答、"
                "以及要不要提醒居民本地政策未收录。",
    }


def tool_scan_unapplied(ctx: ToolContext, region: str = "", category: str = "",
                        limit: int = 20) -> dict:
    """【社区工作端】按辖区筛查"符合条件但可能未申请"的政策覆盖情况。"""
    policies = ctx.store.filter(region=region or None, category=category or None, valid_only=True)
    if not policies:
        return {"found": 0, "items": [], "hint": "该筛选条件下没有生效政策。"}

    items = []
    for p in policies[: max(1, min(int(limit or 20), 100))]:
        s = p.structured or {}
        items.append({
            "policy_id": p.id,
            "title": p.title,
            "category": p.category,
            "region": p.region,
            "target_group": s.get("target_group", []),
            "benefit": s.get("benefit", []),
            "handler": s.get("handler", ""),
            "url": p.url,
        })

    regions_present = sorted({i["region"] or "(全国)" for i in items})
    mixed = len(regions_present) > 1

    ctx.add_artifact("scan", "辖区政策覆盖扫描", {"items": items, "region": region})

    note = "这是政策清单，用于社区工作者比对辖区台账，找出应享未享人员。"
    if not region and mixed:
        note += (
            f" ⚠ 本次未限定地域，结果混合了 {len(regions_present)} 个地区的政策："
            f"{'、'.join(regions_present)}。"
            "**呈现时必须按地域分组，并在每项政策后标明适用地区。**"
            "不同地区的待遇标准不同，绝不能把 A 市的标准写成 B 市居民可享受的。"
            "如果工作者没有说明辖区，应先问清楚再重新扫描。"
        )
    return {
        "found": len(items),
        "region": region or "(未限定，结果跨多个地区)",
        "regions_present": regions_present,
        "mixed_regions": mixed,
        "items": items,
        "note": note,
    }


# ================================================================ 规则引擎
_OPS = {
    "gte": lambda a, b: _num(a) is not None and _num(a) >= _num(b),
    "lte": lambda a, b: _num(a) is not None and _num(a) <= _num(b),
    "gt": lambda a, b: _num(a) is not None and _num(a) > _num(b),
    "lt": lambda a, b: _num(a) is not None and _num(a) < _num(b),
    "eq": lambda a, b: _norm_str(a) == _norm_str(b),
    "ne": lambda a, b: _norm_str(a) != _norm_str(b),
    "in": lambda a, b: _norm_str(a) in [_norm_str(x) for x in _as_list(b)],
    "contains": lambda a, b: any(_norm_str(x) in _norm_str(a) for x in _as_list(b)),
    "exists": lambda a, b: bool(_norm_str(a)) == bool(b),
}


def _eval_rule(rule: dict, value: Any) -> bool:
    op = str(rule.get("op", "eq")).lower()
    fn = _OPS.get(op)
    if not fn:
        return False
    try:
        return bool(fn(value, rule.get("value")))
    except (TypeError, ValueError):
        return False


def _num(v: Any) -> float | None:
    if isinstance(v, (int, float)):
        return float(v)
    m = re.search(r"-?\d+(?:\.\d+)?", str(v))
    return float(m.group(0)) if m else None


def _norm_str(v: Any) -> str:
    return normalize(str(v)).replace(" ", "")


def _as_list(v: Any) -> list:
    if isinstance(v, list):
        return v
    return [v]


def _lookup_profile(profile: dict, field_name: str) -> Any:
    """从画像里取字段，支持中英文键名与模糊匹配。"""
    if not field_name:
        return None
    pn = _norm_str(field_name)
    for k, v in profile.items():
        if _norm_str(k) == pn:
            return v
    # 别名表：结构化抽取出的字段名可能和画像槽位名不完全一致
    alias = {
        "年龄": "age", "户籍": "hukou", "户籍情况": "hukou", "户籍地": "hukou",
        "就业": "employment", "就业状态": "employment", "工作状态": "employment",
        "收入": "income", "家庭收入": "income", "人均收入": "income", "家庭人均月收入": "income",
        "残疾": "health", "健康": "health", "健康状况": "health",
        "住房": "housing", "住房情况": "housing",
        "参保": "insurance", "社保": "insurance", "参保情况": "insurance",
        "家庭": "family", "家庭情况": "family", "region": "region", "区域": "region",
    }
    for k, v in profile.items():
        if _norm_str(k) == pn:
            return v
    key = alias.get(field_name)
    if key and key in profile:
        return profile[key]
    for alias_name, k in alias.items():
        if alias_name in field_name or field_name in alias_name:
            if k in profile:
                return profile[k]
    return None


def _profile_region(ctx: ToolContext) -> str:
    return str(ctx.profile.get("region") or ctx.profile.get("区域") or "")


# ================================================================ 工具注册表
Handler = Callable[..., dict]

REGISTRY: dict[str, Handler] = {
    "update_profile": tool_update_profile,
    "check_coverage": tool_check_coverage,
    "search_policy": tool_search_policy,
    "read_clause": tool_read_clause,
    "assess_eligibility": tool_assess_eligibility,
    "check_materials": tool_check_materials,
    "generate_form": tool_generate_form,
    "get_process": tool_get_process,
    "scan_unapplied": tool_scan_unapplied,
}

SCHEMAS: list[dict] = [
    {
        "name": "update_profile",
        "description": (
            "记录居民的基本信息（画像）。每轮从居民的话里提取到新信息就立刻调用。"
            "注意：这只是记录工具，不要为了填满画像而逐项审问居民——"
            "核心字段（hukou/age/employment）到手就该去做 assess_eligibility 了。"
            "字段名用英文键：" + "、".join(f"{k}({v})" for k, v in PROFILE_FIELDS.items())
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "fields": {
                    "type": "object",
                    "description": "要更新的字段，如 {\"age\": 82, \"hukou\": \"本市户籍\"}",
                    "additionalProperties": True,
                }
            },
            "required": ["fields"],
        },
    },
    {
        "name": "check_coverage",
        "description": (
            "检查政策库对某地区的覆盖情况。**一旦居民提到自己所在的城市/区，先调用它**，"
            "再决定怎么回答。它会告诉你库里有没有该地区的地方政策。"
            "如果返回 covered=false，说明本地政策未收录——此时绝不能拿其他城市的标准作答，"
            "要如实说明并建议拨打 12345 或咨询当地居委会。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "region": {"type": "string", "description": "地区名，如 广州市、上海市普陀区"}
            },
            "required": ["region"],
        },
    },
    {
        "name": "search_policy",
        "description": (
            "检索政策条款。这是获取政策事实的唯一来源。"
            "检索词要用政策书面语而非居民口语，例如把『失业了没钱花』扩展为"
            "『失业保险金 就业困难人员 社会保险补贴 最低生活保障』。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "检索关键词，空格分隔多个同义表达"},
                "region": {"type": "string", "description": "限定地域，如 上海市浦东新区"},
                "category": {"type": "string", "description": "限定政策类别，如 养老、救助、就业"},
                "include_expired": {"type": "boolean", "description": "是否包含已废止政策，默认 false"},
                "top_k": {"type": "integer", "description": "返回条数，默认 5，最多 10"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "read_clause",
        "description": (
            "读取政策条款的完整原文，以及同政策的相邻条款和出处信息。引用政策前必须先读原文。"
            "可传两种 id：search_policy 返回的 clause_id（形如 p1a2b3c4#3）读单条；"
            "政策 id（形如 p1a2b3c4）则返回该政策的条款清单。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "clause_id": {"type": "string", "description": "clause_id 或 policy_id"}
            },
            "required": ["clause_id"],
        },
    },
    {
        "name": "assess_eligibility",
        "description": (
            "【政策体检】把居民画像与政策的资格条件逐条比对，返回确定性的符合/不符合判定。"
            "**信息不全也可以调用**——缺失的条件会返回『需补充信息』，这正是它的用途。"
            "所以信息收集一轮后就应该调用它，不要为了凑齐画像而反复追问居民。"
            "不传 policy_ids 时会体检全部生效政策。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "policy_ids": {"type": "array", "items": {"type": "string"}, "description": "限定要体检的政策 id，留空表示全部"},
                "limit": {"type": "integer", "description": "最多体检多少份政策，默认 12"},
            },
        },
    },
    {
        "name": "check_materials",
        "description": "核对居民已有的材料，列出办理某项政策还缺哪些材料。居民说『我带了XX』时调用。",
        "input_schema": {
            "type": "object",
            "properties": {
                "policy_id": {"type": "string"},
                "owned": {"type": "array", "items": {"type": "string"}, "description": "居民已拥有的材料名称"},
            },
            "required": ["policy_id"],
        },
    },
    {
        "name": "generate_form",
        "description": "根据已收集的居民画像，生成预填好的申请表草稿。",
        "input_schema": {
            "type": "object",
            "properties": {"policy_id": {"type": "string"}},
            "required": ["policy_id"],
        },
    },
    {
        "name": "get_process",
        "description": "获取某项政策的办理流程、办理地点、办理时限。",
        "input_schema": {
            "type": "object",
            "properties": {"policy_id": {"type": "string"}},
            "required": ["policy_id"],
        },
    },
    {
        "name": "scan_unapplied",
        "description": (
            "【社区工作端专用】按辖区筛查政策覆盖情况，帮社区工作者找出应享未享的居民。"
            "居民咨询场景不要调用。"
            "**务必先传入 region**——不限定地域会返回多个省市混合的结果，"
            "各地区待遇标准不同，混在一起会误导使用者。若工作者未说明辖区，先问清楚。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "region": {"type": "string", "description": "辖区，如 上海市浦东新区"},
                "category": {"type": "string"},
                "limit": {"type": "integer"},
            },
        },
    },
]


def call(name: str, ctx: ToolContext, args: dict) -> dict:
    fn = REGISTRY.get(name)
    if not fn:
        raise ToolError(f"未知工具：{name}")
    try:
        return fn(ctx, **(args or {}))
    except ToolError:
        raise
    except TypeError as e:
        raise ToolError(f"参数错误：{e}") from None
