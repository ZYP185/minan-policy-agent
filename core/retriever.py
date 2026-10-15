"""检索层：BM25 初筛 → 元数据过滤 → LLM 重排。

三级流水线的设计意图：
  - BM25 负责"捞得全"（召回）
  - 元数据过滤负责"政治正确"（只能用本地生效的政策）
  - LLM 重排负责"排得准"（政策口语化表述与居民口语差距很大，
    纯字面匹配排不准，例如居民说"我失业了" vs 政策说"处于法定劳动年龄内
    有劳动能力且处于无业状态的人员"）
"""
from __future__ import annotations

import json
from dataclasses import dataclass

import config
from core.bm25 import BM25, score_terms
from core.store import Clause, Policy, PolicyStore
from core.tokenizer import tokenize


@dataclass
class Hit:
    clause: Clause
    policy: Policy
    bm25: float = 0.0
    rerank: float = 0.0

    @property
    def text(self) -> str:
        return self.clause.text

    def to_dict(self) -> dict:
        return {
            "clause_id": self.clause.id,
            "policy_id": self.policy.id,
            "policy_title": self.policy.title,
            "doc_number": self.policy.doc_number,
            "issuer": self.policy.issuer,
            "region": self.policy.region,
            "category": self.policy.category,
            "status": self.policy.status_on(),
            "effective_date": self.policy.effective_date,
            "expiry_date": self.policy.expiry_date,
            "clause_no": self.clause.no,
            "text": self.clause.text,
            "url": self.policy.url,
            "score": round(self.rerank or self.bm25, 4),
        }


class Retriever:
    def __init__(self, store: PolicyStore, llm=None) -> None:
        self.store = store
        self.llm = llm
        self._index: BM25 | None = None
        self._pool: list[Clause] = []      # 参与索引的全部条款
        self.build()

    # ------------------------------------------------------------- 建索引
    def build(self) -> None:
        self._pool = [c for p in self.store.policies.values() for c in p.clauses]
        corpus = [c.tokens for c in self._pool]
        self._index = BM25(corpus) if corpus else None

    # ------------------------------------------------------------- 检索
    def search(
        self,
        query: str,
        region: str | None = None,
        category: str | None = None,
        valid_only: bool = True,
        top_k: int = config.TOP_K_RETRIEVE,
        rerank: bool = True,
    ) -> list[Hit]:
        if not self._index or not self._pool or not query.strip():
            return []

        allowed = self._allowed_ids(region, category, valid_only)
        qtokens = tokenize(query)

        # 多取一些候选，因为要按元数据过滤掉一部分
        raw = self._index.search(qtokens, top_k=min(len(self._pool), max(top_k * 6, 40)))

        hits: list[Hit] = []
        for idx, score in raw:
            c = self._pool[idx]
            if allowed is not None and c.policy_id not in allowed:
                continue
            p = self.store.get(c.policy_id)
            if not p:
                continue
            hits.append(Hit(clause=c, policy=p, bm25=score))
            if len(hits) >= top_k:
                break

        if rerank and self.llm and len(hits) > 1:
            hits = self._llm_rerank(query, hits)
        return hits

    # --------------------------------------------------------- 元数据过滤
    def _allowed_ids(
        self, region: str | None, category: str | None, valid_only: bool
    ) -> set[str] | None:
        if not region and not category and not valid_only:
            return None
        allowed: set[str] = set()
        for p in self.store.policies.values():
            if region and not _region_match(region, p):
                continue
            if category and category != p.category:
                continue
            if valid_only and not p.is_valid():
                continue
            allowed.add(p.id)
        return allowed

    # --------------------------------------------------------- LLM 重排
    def _llm_rerank(self, query: str, hits: list[Hit]) -> list[Hit]:
        """让模型从候选条款里挑出真正相关的，并给出 0-10 相关度。"""
        listing = "\n".join(
            f"[{i}] 《{h.policy.title}》{h.clause.no}：{h.clause.text[:220]}"
            for i, h in enumerate(hits)
        )
        prompt = (
            f"居民提问：{query}\n\n"
            f"候选政策条款：\n{listing}\n\n"
            "请判断每条候选与居民提问的相关程度（0-10 的整数，10 表示直接回答了提问）。"
            "注意：居民用的是口语，政策用的是书面语，要理解语义而非字面。"
            "例如居民问『失业了能领什么钱』，政策里的『失业保险金』『就业困难人员"
            "灵活就业社会保险补贴』都高度相关。\n"
            "只输出 JSON 数组，不要任何解释，格式："
            '[{"i": 0, "score": 9}, {"i": 1, "score": 2}]'
        )
        try:
            msg = self.llm.complete(
                [{"role": "user", "content": prompt}],
                system="你是政策检索相关性评分器，只输出 JSON。",
                max_tokens=600,
                temperature=0.0,
            )
            from core.llm import text_of
            raw = text_of(msg)
            scores = _parse_scores(raw)
        except Exception:
            return hits  # 重排失败就退回 BM25 排序，不影响主流程

        for i, s in scores.items():
            if 0 <= i < len(hits):
                hits[i].rerank = float(s)

        hits.sort(key=lambda h: (h.rerank, h.bm25), reverse=True)
        return hits

    # --------------------------------------------------------- 直查条款
    def fetch(self, clause_id: str) -> Hit | None:
        p, c = self.store.find_clause(clause_id)
        if p and c:
            return Hit(clause=c, policy=p)
        return None

    def related_clauses(self, policy_id: str, limit: int = 30) -> list[Clause]:
        p = self.store.get(policy_id)
        return p.clauses[:limit] if p else []


def _region_match(region: str, p: Policy) -> bool:
    """地域匹配：允许 '上海市' 匹配 '上海市浦东新区' 这类上下级关系。"""
    if not p.region:
        return True  # 没有地域标注的政策视为通用
    return region in p.region or p.region in region


def _parse_scores(raw: str) -> dict[int, float]:
    raw = raw.strip()
    start, end = raw.find("["), raw.rfind("]")
    if start == -1 or end == -1:
        return {}
    try:
        arr = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return {}
    out: dict[int, float] = {}
    for item in arr:
        if isinstance(item, dict) and "i" in item:
            try:
                out[int(item["i"])] = float(item.get("score", 0))
            except (TypeError, ValueError):
                continue
    return out
