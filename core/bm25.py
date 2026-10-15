"""BM25 Okapi 检索（纯 Python，无依赖）。"""
from __future__ import annotations

import math
from collections import Counter
from typing import Sequence


class BM25:
    def __init__(self, corpus: Sequence[Sequence[str]], k1: float = 1.5, b: float = 0.75) -> None:
        self.k1 = k1
        self.b = b
        self.n = len(corpus)
        self.doc_len: list[int] = []
        self.tf: list[Counter] = []
        self.df: Counter = Counter()

        for doc in corpus:
            c = Counter(doc)
            self.tf.append(c)
            self.doc_len.append(len(doc))
            for term in c:
                self.df[term] += 1

        self.avgdl = (sum(self.doc_len) / self.n) if self.n else 0.0
        # idf 用 BM25+ 的平滑写法，避免高频词出现负值
        self.idf: dict[str, float] = {}
        for term, df in self.df.items():
            self.idf[term] = math.log(1 + (self.n - df + 0.5) / (df + 0.5))

    def search(self, query: Sequence[str], top_k: int = 10) -> list[tuple[int, float]]:
        if not self.n or not query:
            return []
        q = [t for t in query if t in self.idf]
        if not q:
            return []

        scores = [0.0] * self.n
        for term in q:
            idf = self.idf[term]
            for i in range(self.n):
                f = self.tf[i].get(term)
                if not f:
                    continue
                dl = self.doc_len[i] or 1
                denom = f + self.k1 * (1 - self.b + self.b * dl / (self.avgdl or 1))
                scores[i] += idf * f * (self.k1 + 1) / denom

        ranked = sorted(
            ((i, s) for i, s in enumerate(scores) if s > 0),
            key=lambda x: x[1],
            reverse=True,
        )
        return ranked[:top_k]


def score_terms(query: Sequence[str], doc_tokens: Sequence[str]) -> float:
    """两个 token 列表的重合度（Jaccard 变体），用于轻量相关性判断。"""
    a, b = set(query), set(doc_tokens)
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / math.sqrt(len(a) * len(b))
