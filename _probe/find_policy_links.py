"""从政府网站首页/栏目页挖掘「政策文件」栏目入口。仅标准库。"""
import re
import ssl
import sys
import time
import urllib.request
from urllib.parse import urljoin, urlparse

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")

SEEDS = [
    ("中国政府网", "https://www.gov.cn/"),
    ("民政部", "https://www.mca.gov.cn/"),
    ("人社部", "https://www.mohrss.gov.cn/"),
    ("国家医保局", "https://www.nhsa.gov.cn/"),
    ("北京市政府", "https://www.beijing.gov.cn/"),
    ("上海市政府", "https://www.shanghai.gov.cn/"),
]

# 只要这些关键词命中的栏目链接
WANT = re.compile(r"政策|法规|文件|规章|规范性|信息公开|公报|办事|服务")

ctx = ssl.create_default_context()


def get(url, timeout=20):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        raw = r.read()
        return r.headers.get("Content-Type", ""), raw


def decode(raw, ct):
    m = re.search(rb'charset\s*=\s*["\']?\s*([A-Za-z0-9_\-]+)', raw[:4096], re.I)
    cs = m.group(1).decode("ascii", "replace").lower() if m else "utf-8"
    if "gb" in cs:
        cs = "gb18030"
    return raw.decode(cs, "replace")


LINK = re.compile(r'<a\b[^>]*href\s*=\s*["\']([^"\']+)["\'][^>]*>(.*?)</a>', re.I | re.S)


def links_of(html, base):
    out = []
    for href, inner in LINK.findall(html):
        text = re.sub(r"(?s)<[^>]+>", "", inner)
        text = re.sub(r"&nbsp;?", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        if not text or len(text) > 40:
            continue
        if not WANT.search(text):
            continue
        href = href.strip()
        if href.startswith(("javascript:", "#", "mailto:")):
            continue
        out.append((text, urljoin(base, href)))
    # 去重保序
    seen, uniq = set(), []
    for t, u in out:
        if u in seen:
            continue
        seen.add(u)
        uniq.append((t, u))
    return uniq


for name, home in SEEDS:
    print("=" * 78)
    print(f"### {name}  {home}")
    print("=" * 78)
    try:
        ct, raw = get(home)
        html = decode(raw, ct)
        ls = links_of(html, home)
        own = urlparse(home).netloc
        shown = 0
        for t, u in ls:
            if urlparse(u).netloc != own:
                continue
            print(f"  {t:<24} -> {u}")
            shown += 1
            if shown >= 14:
                break
        if shown == 0:
            print("  (首页未发现政策类栏目链接，可能是 JS 渲染或需从导航 JSON 取)")
    except Exception as e:
        print(f"  [FAIL] {type(e).__name__}: {str(e)[:110]}")
    sys.stdout.flush()
