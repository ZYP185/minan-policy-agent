"""核心模块自检：分词 / BM25 / 金额抽取。不依赖网络。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.bm25 import BM25, score_terms
from core.tokenizer import extract_amounts, normalize, tokenize


def test_tokenize():
    t = tokenize("本市户籍且年满80周岁的老年人，每人每月发放高龄津贴200元。")
    assert "高龄" in t, "bigram 未捕捉到『高龄』"
    assert "津贴" in t, "bigram 未捕捉到『津贴』"
    assert "80" in t, "数字未保留"
    assert "元" in t
    # 停用词不应作为 unigram 出现
    assert "的" not in t
    print("[OK] tokenize")
    print("     ", t[:20])


def test_amounts():
    a = extract_amounts("每人每月发放高龄津贴200元，补贴标准为缴费额的50%，3个工作日内办结。")
    print("[OK] extract_amounts ->", a)
    assert "200元" in a
    assert "50%" in a
    assert any("工作日" in x for x in a)


def test_normalize():
    assert normalize("ＡＢＣ１２３　全角空格") == "ABC123 全角空格"
    print("[OK] normalize")


def test_bm25():
    corpus = [
        tokenize("本市户籍年满80周岁老年人每人每月发放高龄津贴200元"),
        tokenize("最低生活保障标准按照家庭人均收入低于本市低保标准确定"),
        tokenize("灵活就业人员可申请社会保险补贴，补贴标准为缴费额的50%"),
        tokenize("新生儿父母可申领育儿补贴，每孩每年3600元"),
    ]
    bm = BM25(corpus)
    cases = [
        ("高龄老人有什么补贴", 0),
        ("生孩子的补贴", 3),
        ("灵活就业社保补贴", 2),
        ("低保标准", 1),
    ]
    ok = True
    for q, expect in cases:
        hits = bm.search(tokenize(q), top_k=2)
        top = hits[0][0] if hits else None
        mark = "OK " if top == expect else "!! "
        if top != expect:
            ok = False
        print(f"[{mark}] {q!r:22} top={top} 期望={expect} 得分={[round(s, 2) for _, s in hits]}")
    assert ok, "BM25 排序不符合预期"


def test_overlap():
    a = tokenize("高龄津贴发放标准")
    b = tokenize("高龄津贴每人每月200元")
    c = tokenize("灵活就业社保补贴")
    print(f"[OK] overlap  相关={score_terms(a, b):.3f}  无关={score_terms(a, c):.3f}")
    assert score_terms(a, b) > score_terms(a, c)


if __name__ == "__main__":
    test_tokenize()
    test_amounts()
    test_normalize()
    test_bm25()
    test_overlap()
    print("\n全部通过")
