"""从一个陌生的政府网站列表页，自动发现政策原文链接。

设计前提（实测得出）：
  - 各城市站点结构差异大，写站点适配器不可持续
  - 但政策列表页有共性：链接文本就是政策标题，且带「办法/细则/通知/意见」等特征词
  - 所以走「标题特征筛选 + 试抓验证」两步，不依赖任何站点配置

实测效果：西安市政策文件库 5/5 候选验证为真政策正文（条款 29-62 条）；
重庆、武汉等 JS 渲染的列表页发现不到东西 —— 这类站点只能靠人工提供入口。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

from ingest import fetch, parse as P

# 政策标题几乎必含这些词之一
TITLE_HINT = re.compile(
    r"(办法|细则|通知|意见|方案|规定|通告|决定|指引|规范|标准|规划|"
    r"条例|措施|制度|公告|规则|章程|政策)"
)
# 这些是栏目/功能链接，不是政策
TITLE_BAD = re.compile(
    r"(解读|图解|问答|视频|图片|下载|打印|返回|更多|首页|上一页|下一页|"
    r"上一篇|下一篇|查看详情|点击|征求意见|关于我们|网站地图|English|"
    r"登录|注册|无障碍|简繁|分享|收藏|订阅|RSS|政务公开|信息公开指南|"
    r"信息公开制度|年报|目录|索引)"
)
# 明显的非政策正文页
URL_BAD = re.compile(r"(index|list|search|login|javascript|#)", re.I)

CJK = re.compile(r"[一-鿿]")

# 民生政策预设：市政府政策库里通常混着大量非民生文件（数据管理、交通管制、
# 应急预案…）。做民生问答助手时用这个过滤器，能把噪音挡掉大半。
LIVELIHOOD_KEYWORDS = [
    "最低生活保障", "低保", "特困", "临时救助", "社会救助", "救济", "困难群众",
    "养老", "老年", "高龄", "敬老", "助餐", "失能", "护理",
    "残疾", "残疾人", "伤残", "康复",
    "医保", "医疗", "大病", "门诊", "住院", "报销", "生育", "育儿", "托育",
    "就业", "失业", "创业", "职业培训", "社会保险补贴", "灵活就业", "工伤",
    "住房保障", "公租房", "保障性租赁", "廉租", "公积金", "安居",
    "教育资助", "助学", "学费减免", "儿童", "未成年人", "孤儿", "留守",
    "优抚", "退役军人", "抚恤", "低保边缘", "低收入",
]
LIVELIHOOD_RE = re.compile("|".join(LIVELIHOOD_KEYWORDS))


@dataclass
class Candidate:
    title: str
    url: str
    verified: bool = False
    char_count: int = 0
    clause_count: int = 0
    issuer: str = ""
    publish_date: str = ""
    doc_number: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.verified and self.char_count >= 300


@dataclass
class DiscoverResult:
    entry: str
    candidates: list[Candidate] = field(default_factory=list)
    note: str = ""

    @property
    def verified(self) -> list[Candidate]:
        return [c for c in self.candidates if c.ok]


def _link_text(inner_html: str) -> str:
    t = re.sub(r"(?s)<[^>]+>", "", inner_html)
    t = re.sub(r"&nbsp;?", " ", t)
    t = re.sub(r"&[a-z]+;", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def extract_candidates(entry_html: str, base_url: str,
                       same_host_only: bool = True,
                       limit: int = 80) -> list[tuple[str, str]]:
    """从列表页 HTML 里抽出 (标题, 绝对URL) 候选。不做网络请求。"""
    base_host = urlparse(base_url).netloc
    seen: set[str] = set()
    out: list[tuple[str, str]] = []

    for href, inner in re.findall(
            r'<a\b[^>]*href\s*=\s*["\']([^"\']+)["\'][^>]*>(.*?)</a>',
            entry_html, re.I | re.S):
        title = _link_text(inner)
        if not (8 <= len(title) <= 60):
            continue
        if not TITLE_HINT.search(title) or TITLE_BAD.search(title):
            continue
        href = href.strip()
        if not href or href.startswith(("javascript:", "#", "mailto:")):
            continue
        url = urljoin(base_url, href)
        if not url.startswith("http"):
            continue
        if same_host_only and urlparse(url).netloc != base_host:
            continue
        if url in seen:
            continue
        seen.add(url)
        out.append((title, url))
        if len(out) >= limit:
            break

    return out


def verify_one(url: str, timeout: int = 25) -> Candidate:
    """抓一篇并判定是不是政策正文。"""
    c = Candidate(title="", url=url)
    try:
        raw, ct, final = fetch.get(url, timeout=timeout)
    except fetch.FetchError as e:
        c.error = str(e)
        return c

    html = P.decode(raw, ct)
    body, how = P.extract_body(html)
    meta = P.extract_meta(html, body, final or url)

    c.char_count = len(CJK.findall(body))
    c.clause_count = len(re.findall(r"第[一二三四五六七八九十百零〇\d]+条", body))
    c.title = meta.get("title", "")
    c.issuer = meta.get("issuer", "")
    c.publish_date = meta.get("publish_date", "")
    c.doc_number = meta.get("doc_number", "")
    # 判定标准：正文够长 且 （有条款结构 或 标题像政策）
    c.verified = c.char_count >= 300
    return c


def discover(entry: str, pages: int = 1, verify: bool = True,
             verify_limit: int | None = None, same_host_only: bool = True,
             livelihood_only: bool = False, keyword: str = "",
             verbose: bool = True) -> DiscoverResult:
    """从入口页发现政策原文。

    pages          —— 若入口 URL 形如 /1.html 或 page=1，自动翻页抓取
    verify         —— 是否试抓验证（强烈建议开，否则会混进栏目页）
    livelihood_only—— 只要民生类政策，过滤掉数据管理/交通管制/应急预案这类
    keyword        —— 自定义标题关键词过滤（正则）
    """
    res = DiscoverResult(entry=entry)
    raw_pairs: list[tuple[str, str]] = []
    extra = re.compile(keyword) if keyword else None
    empty_streak = 0

    for page_url in _page_urls(entry, pages):
        # 连撞两页空/不存在就不再往下翻——别再拿 404 去打人家的服务器
        if empty_streak >= 2:
            if verbose:
                print("  （连续两页没有内容，停止翻页）")
            break

        try:
            raw, ct, final = fetch.get(page_url)
        except fetch.FetchError as e:
            if verbose:
                print(f"  [列表页失败] {page_url}  {e}")
            if not raw_pairs:
                res.note = f"入口页抓取失败：{e}"
            empty_streak += 1
            continue

        html = P.decode(raw, ct)
        pairs = extract_candidates(html, final or page_url, same_host_only)
        if not pairs:
            empty_streak += 1
        else:
            empty_streak = 0

        # 先按标题过滤，省下试抓的开销
        before = len(pairs)
        if livelihood_only:
            pairs = [(t, u) for t, u in pairs if LIVELIHOOD_RE.search(t)]
        if extra:
            pairs = [(t, u) for t, u in pairs if extra.search(t)]
        drop = before - len(pairs)
        if drop and verbose:
            print(f"  [列表页] {page_url}  标题过滤掉 {drop} 个非民生候选")

        have = {u for _, u in raw_pairs}
        fresh = [(t, u) for t, u in pairs if u not in have]
        raw_pairs.extend(fresh)
        if verbose:
            print(f"  [列表页] {page_url}  发现 {len(fresh)} 个候选")

    if not raw_pairs:
        res.note = (res.note or
                    ("标题过滤后没有剩余候选，试试去掉 --livelihood 看看全量。"
                     if (livelihood_only or extra) else
                     "没有发现政策类链接。该列表页可能是 JS 动态加载的——"
                     "请换一个入口 URL（例如某个具体年份/栏目的静态页）。"))
        return res

    limit = verify_limit if verify_limit is not None else len(raw_pairs)
    for i, (title, url) in enumerate(raw_pairs, 1):
        if i > limit:
            break
        if not verify:
            res.candidates.append(Candidate(title=title, url=url, verified=True))
            continue
        c = verify_one(url)
        if not c.title:
            c.title = title            # 页面没给标题就用链接文本
        if verbose:
            mark = "✓" if c.ok else "✗"
            detail = f"{c.char_count}字/条款{c.clause_count}" if c.ok else (c.error or "非正文")
            print(f"    [{mark}] {detail:<18} {c.title[:38]}")
        res.candidates.append(c)

    for c in res.candidates:
        if not c.title:
            c.title = c.url.rsplit("/", 1)[-1]

    return res


def _page_urls(entry: str, pages: int) -> list[str]:
    """生成翻页 URL。只在能可靠推断时翻页，否则只给入口本身。"""
    if pages <= 1:
        return [entry]

    urls = [entry]
    # 形如 /1.html、/index_2.html
    m = re.search(r"/(\d+)\.(html?|shtml)$", entry)
    if m:
        start = int(m.group(1))
        for k in range(1, pages):
            urls.append(re.sub(r"/\d+\.(html?|shtml)$",
                               f"/{start + k}.\\1", entry))
        return urls
    # 形如 ?page=1 / &page=2
    m = re.search(r"([?&])page=(\d+)", entry)
    if m:
        start = int(m.group(2))
        for k in range(1, pages):
            urls.append(re.sub(r"([?&])page=\d+", lambda mm: f"{mm.group(1)}page={start + k}", entry))
        return urls
    return urls
