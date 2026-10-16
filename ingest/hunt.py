"""批量搜寻可用的政策栏目入口。

为什么需要它：城市/区县政府网站有 2800+ 个，结构各不相同，
逐个手工找入口太慢。这个脚本按「多栏目尝试 + 政策密度打分」自动筛出
值得入库的入口 URL，产出的清单直接喂给 ingest.expand。

用法：
    python -m ingest.hunt --candidates counties.txt --out entries.txt
    python -m ingest.hunt --candidates counties.txt --min-policies 12
"""
from __future__ import annotations

import argparse
import re
import sys
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlparse

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from ingest import fetch, parse as P

CJK = re.compile(r"[一-鿿]")

# 可能是政策列表页的栏目名
SECTION = re.compile(
    r"(政策文件|政策法规|规范性文件|政策库|政务公开|政府信息公开|"
    r"政策解读|文件|规章|信息公开目录|法定主动公开)"
)
# 政策标题特征
POLICY = re.compile(
    r"(办法|细则|通知|意见|方案|规定|通告|决定|指引|规范|措施|制度|规划|"
    r"标准|条例|规则|公告|办法的|的通知)"
)
# 明显不是民生政策的（人事、会议、活动类），用来给栏目精确打分
NOISE = re.compile(
    r"(任免|免去|职务|聘任|聘用|招聘|录用|体检|公示|表彰|会议|座谈|"
    r"调研|视察|慰问|信访|值班|放假|招标|中标|采购|成交|比选|"
    r"土地|出让|挂牌|拍卖|征收公告|环评|处罚|许可决定)"
)
# 民生政策特征（加分项）
LIVELIHOOD = re.compile(
    r"(最低生活保障|低保|特困|临时救助|社会救助|养老|老年|高龄|敬老|助餐|"
    r"失能|护理|残疾|康复|医保|医疗|大病|生育|育儿|托育|就业|失业|创业|"
    r"社会保险补贴|灵活就业|住房保障|公租房|保障性租赁|公积金|助学|"
    r"教育资助|儿童|孤儿|抚恤|优抚|低保边缘|低收入|补贴|津贴|救助)"
)


@dataclass
class SiteResult:
    name: str
    home: str
    ok: bool = False
    error: str = ""
    section_title: str = ""
    section_url: str = ""
    policy_links: int = 0
    livelihood_links: int = 0
    noise_ratio: float = 1.0
    samples: list[str] = field(default_factory=list)

    @property
    def score(self) -> float:
        """打分：民生政策链接数为主，噪声比例作惩罚。"""
        if not self.policy_links:
            return 0.0
        useful = self.policy_links * (1 - self.noise_ratio)
        return self.livelihood_links * 2 + useful


def _links(html: str, base: str, pattern: re.Pattern, limit: int = 200):
    out, seen = [], set()
    for href, inner in re.findall(
            r'<a\b[^>]*href\s*=\s*["\']([^"\']+)["\'][^>]*>(.*?)</a>', html, re.I | re.S):
        t = re.sub(r"(?s)<[^>]+>", "", inner)
        t = re.sub(r"&nbsp;?", " ", t)
        t = re.sub(r"\s+", " ", t).strip()
        if not (6 <= len(t) <= 60) or not pattern.search(t):
            continue
        h = href.strip()
        if h.startswith(("javascript:", "#", "mailto:")):
            continue
        u = urljoin(base, h)
        if u in seen:
            continue
        seen.add(u)
        out.append((t, u))
        if len(out) >= limit:
            break
    return out


def hunt_one(name: str, home: str, max_sections: int = 5) -> SiteResult:
    r = SiteResult(name=name, home=home)
    try:
        raw, ct, final = fetch.get(home, timeout=20)
    except fetch.FetchError as e:
        r.error = str(e)
        return r

    html = P.decode(raw, ct)
    base = final or home
    n_home = len(CJK.findall(re.sub(r"(?s)<[^>]+>", " ", html)))
    if n_home < 300:
        r.error = f"首页仅 {n_home} 汉字，疑 JS 渲染"
        return r

    sections = _links(html, base, SECTION, limit=12)
    # 同域名，且不是首页自身
    sections = [(t, u) for t, u in sections if urlparse(u).netloc == urlparse(base).netloc]

    if not sections:
        r.error = "首页未找到政策类栏目链接"
        return r

    best: SiteResult | None = None
    for title, url in sections[:max_sections]:
        try:
            raw2, ct2, final2 = fetch.get(url, timeout=20)
        except fetch.FetchError:
            continue
        html2 = P.decode(raw2, ct2)
        pols = _links(html2, final2 or url, POLICY, limit=200)
        # 去掉栏目名本身误入的
        pols = [(t, u) for t, u in pols if not SECTION.fullmatch(t)]
        if len(pols) < 3:
            continue

        noise = sum(1 for t, _ in pols if NOISE.search(t))
        live = sum(1 for t, _ in pols if LIVELIHOOD.search(t))
        cand = SiteResult(
            name=name, home=home, ok=True,
            section_title=title, section_url=final2 or url,
            policy_links=len(pols), livelihood_links=live,
            noise_ratio=noise / len(pols),
            samples=[t for t, _ in pols[:3]],
        )
        if best is None or cand.score > best.score:
            best = cand

    if best is None:
        r.error = f"试了 {len(sections[:max_sections])} 个栏目，政策密度都不够"
        return r
    return best


def main() -> int:
    ap = argparse.ArgumentParser(description="批量搜寻可用的政策栏目入口")
    ap.add_argument("--candidates", type=Path, required=True,
                    help="候选站点清单，每行 `名称<TAB>URL`")
    ap.add_argument("--out", type=Path, help="把筛出的入口写到这个文件")
    ap.add_argument("--min-policies", type=int, default=10,
                    help="政策链接数下限，默认 10")
    ap.add_argument("--min-livelihood", type=int, default=2,
                    help="民生政策链接数下限，默认 2")
    ap.add_argument("--workers", type=int, default=6,
                    help="并发数。对政府网站保持克制，默认 6")
    ap.add_argument("--max-sections", type=int, default=5)
    args = ap.parse_args()

    items: list[tuple[str, str]] = []
    for line in args.candidates.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t") if "\t" in line else line.split()
        if len(parts) >= 2 and parts[-1].startswith("http"):
            items.append((" ".join(parts[:-1]), parts[-1]))
        elif parts and parts[0].startswith("http"):
            items.append((urlparse(parts[0]).netloc, parts[0]))

    if not items:
        print("候选清单为空")
        return 1

    print("=" * 88)
    print(f"  批量搜寻政策入口：{len(items)} 个候选站点，并发 {args.workers}")
    print("=" * 88)

    results: list[SiteResult] = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(hunt_one, n, u, args.max_sections): (n, u) for n, u in items}
        for i, f in enumerate(as_completed(futs), 1):
            name, _ = futs[f]
            try:
                r = f.result()
            except Exception as e:
                r = SiteResult(name=name, home="", error=f"{type(e).__name__}: {e}")
            results.append(r)
            mark = "✓" if r.ok else "·"
            if r.ok:
                print(f"  [{i:>3}/{len(items)}] {mark} {name:<16} "
                      f"政策{r.policy_links:>3} 民生{r.livelihood_links:>3} "
                      f"噪声{r.noise_ratio:>4.0%}  《{r.section_title[:14]}》", flush=True)
            else:
                print(f"  [{i:>3}/{len(items)}] {mark} {name:<16} {r.error[:52]}", flush=True)

    good = [r for r in results
            if r.ok and r.policy_links >= args.min_policies
            and r.livelihood_links >= args.min_livelihood]
    good.sort(key=lambda r: r.score, reverse=True)

    print()
    print("=" * 88)
    print(f"  筛出 {len(good)} 个可用入口（政策≥{args.min_policies} 且 民生≥{args.min_livelihood}）")
    print("=" * 88)
    for r in good:
        print(f"\n  {r.name}   政策{r.policy_links} 民生{r.livelihood_links} "
              f"噪声{r.noise_ratio:.0%}")
        print(f"    {r.section_url}")
        for s in r.samples:
            print(f"      · {s[:62]}")

    if args.out and good:
        lines = [f"# 自动搜寻出的政策入口（{len(good)} 个）",
                 "# 用法：python -m ingest.expand --entry <URL> --livelihood --yes", ""]
        for r in good:
            lines.append(f"# {r.name}  政策{r.policy_links} 民生{r.livelihood_links}")
            lines.append(r.section_url)
            lines.append("")
        args.out.write_text("\n".join(lines), encoding="utf-8")
        print(f"\n  已写入 {args.out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
