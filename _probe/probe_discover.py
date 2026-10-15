"""探测：能否用一套通用逻辑，从陌生的城市站点自动发现政策原文链接？

不写站点适配器，只靠：
  1. 标题特征词（办法/细则/通知/意见/方案/规定/通告/决定）
  2. 试抓验证（正文汉字数、是否有条款结构）
"""
import re
import ssl
import sys
import urllib.error
import urllib.request
from urllib.parse import urljoin, urlparse

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

from ingest import parse as P

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
CTX = ssl.create_default_context()

ENTRIES = [
    ("西安市政策文件库", "https://www.xa.gov.cn/gk/zcfg/gfxwj/zfgfxwj/1.html"),
    ("重庆市政策文件库", "https://www.cq.gov.cn/zwgk/zfxxgkml/szfwj/"),
    ("武汉市最新政策", "https://www.wuhan.gov.cn/zwgk/?channelid=26315"),
]

# 政策标题的特征词
TITLE_HINT = re.compile(
    r"(办法|细则|通知|意见|方案|规定|通告|决定|指引|规范|标准|规划|条例|措施|制度|公告)"
)
# 明显不是政策正文的链接文本
TITLE_BAD = re.compile(
    r"(解读|图解|问答|视频|下载|打印|返回|更多|首页|上一篇|下一篇|查看详情|点击|"
    r"征求意见|关于我们|网站地图|English)"
)


def fetch(url, timeout=20):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9", "Accept-Encoding": "identity",
    })
    with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
        raw = r.read()
        return r.headers.get("Content-Type", ""), raw


def discover(entry: str, limit: int = 60) -> list[tuple[str, str]]:
    ct, raw = fetch(entry)
    html = P.decode(raw, ct)
    base_host = urlparse(entry).netloc
    out, seen = [], set()

    for href, inner in re.findall(
            r'<a\b[^>]*href\s*=\s*["\']([^"\']+)["\'][^>]*>(.*?)</a>', html, re.I | re.S):
        t = re.sub(r"(?s)<[^>]+>", "", inner)
        t = re.sub(r"&nbsp;?", " ", t)
        t = re.sub(r"\s+", " ", t).strip()
        if not (8 <= len(t) <= 60):
            continue
        if not TITLE_HINT.search(t) or TITLE_BAD.search(t):
            continue
        u = urljoin(entry, href.strip())
        if urlparse(u).netloc != base_host:
            continue
        if u in seen:
            continue
        seen.add(u)
        out.append((t, u))
        if len(out) >= limit:
            break
    return out


for name, entry in ENTRIES:
    print("=" * 78)
    print(f"{name}\n  {entry}")
    print("=" * 78)
    try:
        cands = discover(entry)
    except Exception as e:
        print(f"  列表页抓取失败：{type(e).__name__}: {str(e)[:80]}")
        continue

    print(f"  发现 {len(cands)} 个候选链接\n")

    # 试抓前 5 个，验证是不是真正的政策正文
    verified = 0
    for t, u in cands[:5]:
        try:
            ct, raw = fetch(u)
            html = P.decode(raw, ct)
            body, how = P.extract_body(html)
            n = len(P.CJK.findall(body))
            # 有没有条款结构
            arts = len(re.findall(r"第[一二三四五六七八九十百]+条", body))
            ok = n > 300
            verified += ok
            flag = "✓正文" if ok else "✗非正文"
            print(f"    [{flag}] {n:>5}字 条款{arts:>3} 器={how:<8} {t[:34]}")
            if not ok:
                print(f"           {u[:88]}")
        except Exception as e:
            print(f"    [✗抓取] {type(e).__name__} {t[:30]}")

    print(f"\n  → 前 5 个候选里 {verified} 个是政策正文")
    sys.stdout.flush()
