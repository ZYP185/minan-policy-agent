"""扩库 / 巡检 / 覆盖自知 三个新能力的单元测试。不联网。

只测纯逻辑部分：链接发现规则、翻页推断、指纹归一化、覆盖判定。
实际的网络行为由 tests/probe_uncovered.py 和命令行手工验证。
"""
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ingest.discover import (Candidate, _page_urls, extract_candidates,
                             LIVELIHOOD_RE)
from ingest.refresh import fingerprint, _to_date

# 模拟一个典型的地方政府政策列表页
LIST_HTML = """
<html><body>
<div class="nav"><a href="/">首页</a><a href="/gk/">政务公开</a></div>
<ul class="list">
  <li><a href="/gk/1.html">西安市人民政府办公厅关于印发农村住房建设管理实施细则的通知</a></li>
  <li><a href="/gk/2.html">西安市人民政府关于印发被征地农民参加基本养老保险补贴办法的通知</a></li>
  <li><a href="/gk/3.html">西安市人民政府办公厅关于印发公共数据管理办法的通知</a></li>
  <li><a href="/gk/4.html">西安市人民政府办公厅关于印发重污染天气应急预案的通知</a></li>
  <li><a href="/gk/1.html">西安市人民政府办公厅关于印发农村住房建设管理实施细则的通知</a></li>
  <li><a href="/gk/5.html">《农村住房建设管理实施细则》政策解读</a></li>
  <li><a href="/gk/6.html">更多</a></li>
  <li><a href="javascript:void(0)">打印</a></li>
  <li><a href="https://other.gov.cn/x.html">外站关于某某管理办法的通知</a></li>
  <li><a href="/gk/7.html">陕西省人民政府关于某某工作的<em>通知</em></a></li>
</ul>
</body></html>
"""


def test_extract_candidates():
    pairs = extract_candidates(LIST_HTML, "https://www.xa.gov.cn/gk/zcfg/")
    titles = [t for t, _ in pairs]
    urls = [u for _, u in pairs]

    # 正常政策应被选中
    assert any("农村住房建设管理实施细则" in t for t in titles), titles
    assert any("养老保险补贴办法" in t for t in titles), titles
    # 去重：同一条出现两次只保留一次
    assert len(urls) == len(set(urls)), "没有去重"
    # 噪声应被剔除
    assert not any("解读" in t for t in titles), "解读稿混进来了"
    assert not any(t in ("更多", "打印", "首页") for t in titles), "栏目链接混进来了"
    # 外站默认排除
    assert not any("other.gov.cn" in u for u in urls), "外站链接混进来了"
    print(f"[OK] extract_candidates 抽出 {len(pairs)} 条，噪声已剔除")
    for t, u in pairs:
        print(f"      · {t[:44]}")

    # 带 HTML 标签的标题要把标签剥掉
    tagged = [t for t, _ in pairs if "某某工作" in t]
    assert tagged and "<em>" not in tagged[0], f"标签没剥干净：{tagged}"
    print(f"[OK] 标签剥离：{tagged[0]}")


def test_livelihood_filter():
    pairs = extract_candidates(LIST_HTML, "https://www.xa.gov.cn/gk/")
    keep = [t for t, _ in pairs if LIVELIHOOD_RE.search(t)]
    assert any("养老保险补贴" in t for t in keep), keep
    assert not any("公共数据管理" in t for t in keep), "非民生政策漏过筛选"
    print(f"[OK] 民生筛选：{len(pairs)} 条 → {len(keep)} 条")
    for t in keep:
        print(f"      · {t[:46]}")


def test_pagination():
    assert _page_urls("https://x.gov.cn/gk/1.html", 1) == ["https://x.gov.cn/gk/1.html"]
    p = _page_urls("https://x.gov.cn/gk/1.html", 3)
    assert p == ["https://x.gov.cn/gk/1.html",
                 "https://x.gov.cn/gk/2.html",
                 "https://x.gov.cn/gk/3.html"], p
    p2 = _page_urls("https://x.gov.cn/list.shtml?page=1&type=a", 3)
    assert p2[1].endswith("page=2&type=a"), p2
    # 推不出规律时只给入口本身，不去瞎猜
    p3 = _page_urls("https://x.gov.cn/gk/index.html", 5)
    assert p3 == ["https://x.gov.cn/gk/index.html"], p3
    print(f"[OK] 翻页推断：/1.html → {[u.rsplit('/',1)[-1] for u in p]}；"
          f"不可推断时不瞎猜")


def test_fingerprint():
    a = "第一条 为规范管理，制定本办法。"
    b = "第一条　为规范管理，制定本办法。"          # 全角空格差异
    c = "第一条 为规范管理，制定本办法。\n\n\n"
    d = "第一条 为规范管理，制定本细则。"
    assert fingerprint(a) == fingerprint(b), "全角空格差异被误判为变更"
    assert fingerprint(a) == fingerprint(c), "换行差异被误判为变更"
    assert fingerprint(a) != fingerprint(d), "真实内容变更没有被检出"
    print("[OK] 指纹：空格/换行不误报，内容变更能检出")


def test_date_parse():
    assert _to_date("2024-05-01") == date(2024, 5, 1)
    assert _to_date("2024年5月1日") == date(2024, 5, 1)
    assert _to_date("") is None
    assert _to_date("说不清") is None
    print("[OK] 日期解析")


def test_coverage_logic():
    """覆盖判定的核心：分得清"本地有政策"和"只有全国性政策"。"""
    import config
    from core.store import PolicyStore
    from core import tools as T
    from core.retriever import Retriever

    store = PolicyStore(config.POLICY_DIR, config.STRUCT_DIR).load()
    if len(store) == 0:
        print("[跳过] 政策库为空")
        return

    r = Retriever(store, llm=None)
    ctx = T.ToolContext(store, r)

    regions = store.regions()
    if not regions:
        print("[跳过] 库里没有标注地域的政策")
        return

    covered_region = regions[0]
    out1 = T.call("check_coverage", ctx, {"region": covered_region})
    assert out1["covered"] is True, f"{covered_region} 应判为已覆盖"
    assert out1["local_policy_count"] > 0
    print(f"[OK] 已覆盖地区 {covered_region}：本地 {out1['local_policy_count']} 份，"
          f"全国性 {out1['national_policy_count']} 份")

    out2 = T.call("check_coverage", ctx, {"region": "火星市"})
    assert out2["covered"] is False, "不存在的地区应判为未覆盖"
    assert "绝对不要拿其他城市" in out2["advice"], out2["advice"][:80]
    print(f"[OK] 未覆盖地区：covered=False，已给出禁止跨地区作答的指令")

    out3 = T.call("check_coverage", ctx, {"region": ""})
    assert "先问一句" in out3["advice"], out3["advice"][:80]
    print(f"[OK] 未指定地区：提示先问清辖区")


def main() -> int:
    test_extract_candidates()
    test_livelihood_filter()
    test_pagination()
    test_fingerprint()
    test_date_parse()
    test_coverage_logic()
    print("\n扩库/巡检/覆盖 单元测试全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
