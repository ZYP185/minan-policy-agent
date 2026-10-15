"""探测：城市级政策能不能程序化发现？

结论决定整个「自动扩库」方案的可行性：
  - 如果能找到稳定的列表页 → 半自动扩库可行
  - 如果不能 → 只能做「覆盖自知 + 人工录入」
"""
import re
import ssl
import sys
import urllib.error
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
CTX = ssl.create_default_context()
CJK = re.compile(r"[一-鿿]")

# 若干典型城市/区县的政策栏目候选入口
SITES = [
    ("广州市政府-政策文件", "https://www.gz.gov.cn/zwgk/zfxxgkml/"),
    ("广州市政府-首页", "https://www.gz.gov.cn/"),
    ("成都市政府-政策", "https://www.chengdu.gov.cn/chengdu/c129560/list.shtml"),
    ("成都市政府-首页", "https://www.chengdu.gov.cn/"),
    ("杭州市政府-首页", "https://www.hangzhou.gov.cn/"),
    ("深圳市政府-首页", "https://www.sz.gov.cn/"),
    ("武汉市政府-首页", "https://www.wuhan.gov.cn/"),
    ("南京市政府-首页", "https://www.nanjing.gov.cn/"),
    ("西安市政府-首页", "http://www.xa.gov.cn/"),
    ("重庆市政府-首页", "https://www.cq.gov.cn/"),
]


def fetch(url, timeout=20):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Accept-Encoding": "identity",
    })
    with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
        raw = r.read()
        ct = r.headers.get("Content-Type", "")
        return r.status, ct, raw


def decode(raw, ct):
    m = re.search(rb'charset\s*=\s*["\']?\s*([A-Za-z0-9_\-]+)', raw[:4096], re.I)
    cs = m.group(1).decode("ascii", "replace").lower() if m else "utf-8"
    if "gb" in cs:
        cs = "gb18030"
    try:
        return raw.decode(cs, "replace")
    except LookupError:
        return raw.decode("utf-8", "replace")


WANT = re.compile(r"政策|规范性文件|政府信息公开|公报|法规")


def links(html, base):
    out = []
    for href, inner in re.findall(
            r'<a\b[^>]*href\s*=\s*["\']([^"\']+)["\'][^>]*>(.*?)</a>', html, re.I | re.S):
        t = re.sub(r"(?s)<[^>]+>", "", inner)
        t = re.sub(r"\s+", " ", t).strip()
        if t and len(t) <= 30 and WANT.search(t):
            out.append((t, href))
    return out[:8]


print("=" * 78)
for name, url in SITES:
    try:
        st, ct, raw = fetch(url)
        html = decode(raw, ct)
        n = len(CJK.findall(re.sub(r"(?s)<[^>]+>", " ", html)))
        # 页面里有没有政策正文级的内容（而不是导航壳）
        has_body = n > 500
        flag = "静态OK" if has_body else "内容少/疑JS"
        print(f"[{st}] {name:<22} {len(raw):>7}B 汉字{n:>5}  {flag}")
        if has_body:
            for t, h in links(html, url):
                print(f"        · {t:<24} -> {h[:74]}")
    except urllib.error.HTTPError as e:
        print(f"[{e.code}] {name:<22} HTTP {e.code}")
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        print(f"[--] {name:<22} {msg[:70]}")
    sys.stdout.flush()
