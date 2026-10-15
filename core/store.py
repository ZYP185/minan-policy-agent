"""政策库：加载政策文档、切分条款、建索引、做时效与地域过滤。

数据模型分两层：
  1. 原文层（data/policies/*.json）  —— 抓取到的政策原文 + 元数据
  2. 结构化层（data/structured/*.json）—— LLM 抽取的适用对象/材料/流程/资格规则

拆成两层的理由：原文永远可溯源（答辩时要能点回原文），
结构化字段则让 Agent 能做确定性判断，而不是全靠模型"猜"。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Iterable

from core.tokenizer import tokenize

# ---------------------------------------------------------------- 条款切分
# 匹配 "第一条" "第十二条" "（三）" "1." 等条款编号开头
_RE_ARTICLE = re.compile(
    r"(?m)^\s*(第[一二三四五六七八九十百零〇\d]+条|"
    r"[一二三四五六七八九十]+、|"
    r"（[一二三四五六七八九十]+）|"
    r"\([一二三四五六七八九十]+\)|"
    r"\d+[\.、])"
)


def split_clauses(text: str) -> list[tuple[str, str]]:
    """把政策全文切成 (条款号, 条款正文)。

    切不出来时退化为按段落切分——宁可切碎，也不要丢内容。
    """
    if not text or not text.strip():
        return []

    marks = list(_RE_ARTICLE.finditer(text))
    if len(marks) < 2:
        paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
        return [(f"第{i + 1}段", p) for i, p in enumerate(paras)]

    out: list[tuple[str, str]] = []
    for i, m in enumerate(marks):
        start = m.start()
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        body = text[start:end].strip()
        if not body:
            continue
        no = m.group(1).strip()
        out.append((no, body))
    return out


# ---------------------------------------------------------------- 数据结构
@dataclass
class Clause:
    id: str
    policy_id: str
    no: str
    text: str
    tokens: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.tokens:
            self.tokens = tokenize(self.text)


@dataclass
class Policy:
    id: str
    title: str
    full_text: str
    url: str = ""
    doc_number: str = ""
    issuer: str = ""
    publish_date: str = ""
    effective_date: str = ""
    expiry_date: str = ""
    region: str = ""
    level: str = ""
    category: str = ""
    clauses: list[Clause] = field(default_factory=list)
    structured: dict = field(default_factory=dict)

    # -------------------------------------------------------- 时效判定
    def status_on(self, today: date | None = None) -> str:
        """返回 '有效' | '未生效' | '已废止' | '未知'。"""
        today = today or date.today()
        eff = _to_date(self.effective_date)
        exp = _to_date(self.expiry_date)
        if exp and today > exp:
            return "已废止"
        if eff and today < eff:
            return "未生效"
        if eff or exp:
            return "有效"
        return "未知"

    def is_valid(self, today: date | None = None) -> bool:
        return self.status_on(today) in ("有效", "未知")

    def clause_by_id(self, cid: str) -> Clause | None:
        return next((c for c in self.clauses if c.id == cid), None)


def _to_date(s: str) -> date | None:
    if not s:
        return None
    s = str(s).strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y年%m月%d日", "%Y-%m", "%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    m = re.search(r"(\d{4})\D+(\d{1,2})\D+(\d{1,2})", s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------- 政策库
class PolicyStore:
    def __init__(self, policy_dir: Path, struct_dir: Path | None = None) -> None:
        self.policy_dir = Path(policy_dir)
        self.struct_dir = Path(struct_dir) if struct_dir else None
        self.policies: dict[str, Policy] = {}
        self.clauses: list[Clause] = []

    # ------------------------------------------------------------ 加载
    def load(self) -> "PolicyStore":
        self.policies.clear()
        self.clauses.clear()

        structs: dict[str, dict] = {}
        if self.struct_dir and self.struct_dir.exists():
            for f in sorted(self.struct_dir.glob("*.json")):
                try:
                    d = json.loads(f.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    continue
                pid = d.get("policy_id") or f.stem
                structs[pid] = d

        if self.policy_dir.exists():
            for f in sorted(self.policy_dir.glob("*.json")):
                try:
                    d = json.loads(f.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    continue
                p = self._build(d, structs)
                if p:
                    self.policies[p.id] = p
                    self.clauses.extend(p.clauses)

        return self

    def _build(self, d: dict, structs: dict[str, dict]) -> Policy | None:
        text = (d.get("full_text") or "").strip()
        if not text:
            return None
        pid = d.get("id") or d.get("policy_id")
        if not pid:
            return None

        raw_clauses = d.get("clauses")
        if isinstance(raw_clauses, list) and raw_clauses:
            pairs = [
                (c.get("no") or f"第{i + 1}条", c.get("text", ""))
                for i, c in enumerate(raw_clauses)
                if c.get("text")
            ]
        else:
            pairs = split_clauses(text)

        clauses = [
            Clause(id=f"{pid}#{i + 1}", policy_id=pid, no=no, text=body)
            for i, (no, body) in enumerate(pairs)
        ]

        # 结构化抽取出的地域比正则从标题猜的准（发文字号常只写"民政局"这种泛称），
        # 有结构化结果就优先用它。
        struct = structs.get(pid, {})
        region = str(struct.get("region") or d.get("region") or "").strip()
        if region in ("全国", "国家", "中华人民共和国"):
            region = ""                     # 全国性政策视为通用，不做地域过滤
        category = str(struct.get("category") or d.get("category") or "").strip()

        return Policy(
            id=pid,
            title=d.get("title") or "(无标题)",
            full_text=text,
            url=d.get("url", ""),
            doc_number=d.get("doc_number", ""),
            issuer=d.get("issuer", ""),
            publish_date=d.get("publish_date", ""),
            effective_date=d.get("effective_date") or struct.get("effective_date", ""),
            expiry_date=d.get("expiry_date", ""),
            region=region,
            level=d.get("level", ""),
            category=category,
            clauses=clauses,
            structured=struct,
        )

    # ------------------------------------------------------------ 查询
    def __len__(self) -> int:
        return len(self.policies)

    def get(self, policy_id: str) -> Policy | None:
        return self.policies.get(policy_id)

    def find_clause(self, cid: str) -> tuple[Policy | None, Clause | None]:
        for p in self.policies.values():
            c = p.clause_by_id(cid)
            if c:
                return p, c
        # 容错：允许只给 "#3" 这类后缀
        if "#" in cid:
            pid, _, idx = cid.rpartition("#")
            p = self.policies.get(pid)
            if p:
                for c in p.clauses:
                    if c.id.endswith("#" + idx):
                        return p, c
        return None, None

    def regions(self) -> list[str]:
        return sorted({p.region for p in self.policies.values() if p.region})

    def categories(self) -> list[str]:
        return sorted({p.category for p in self.policies.values() if p.category})

    def filter(
        self,
        region: str | None = None,
        category: str | None = None,
        valid_only: bool = True,
    ) -> list[Policy]:
        out = []
        for p in self.policies.values():
            if region and region not in p.region and p.region not in region:
                continue
            if category and category != p.category:
                continue
            if valid_only and not p.is_valid():
                continue
            out.append(p)
        return out

    def stats(self) -> dict:
        by_cat: dict[str, int] = {}
        by_status: dict[str, int] = {}
        for p in self.policies.values():
            by_cat[p.category or "(未分类)"] = by_cat.get(p.category or "(未分类)", 0) + 1
            s = p.status_on()
            by_status[s] = by_status.get(s, 0) + 1
        return {
            "政策数": len(self.policies),
            "条款数": len(self.clauses),
            "地域": self.regions(),
            "分类分布": by_cat,
            "时效分布": by_status,
        }
