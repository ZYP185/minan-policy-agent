"""试出 gov.cn 政策库搜索接口的可用参数组合。"""
import json
import re
import ssl
import sys
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
CTX = ssl.create_default_context()

BASE = "https://sousuo.www.gov.cn/search-gov/data"

COMBOS = [
    ("标题+高龄津贴", {"t": "zhengcelibrary", "q": "高龄津贴", "searchfield": "title", "n": 10}),
    ("全文+高龄津贴", {"t": "zhengcelibrary", "q": "高龄津贴", "searchfield": "content", "n": 10}),
    ("不带field+高龄津贴", {"t": "zhengcelibrary", "q": "高龄津贴", "n": 10}),
    ("全文+广州市高龄津贴", {"t": "zhengcelibrary", "q": "广州市 高龄津贴", "searchfield": "content", "n": 10}),
    ("全文+成都 育儿补贴", {"t": "zhengcelibrary", "q": "成都 育儿补贴", "searchfield": "content", "n": 10}),
    ("全文+养老", {"t": "zhengcelibrary", "q": "养老", "searchfield": "content", "n": 5}),
    ("全文+补贴", {"t": "zhengcelibrary", "q": "补贴", "searchfield": "content", "n": 5}),
]


def probe(label, extra):
    params = {"timetype": "timeqb", "sort": "score", "p": 1, **extra}
    url = f"{BASE}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "application/json, text/plain, */*",
        "Referer": "https://sousuo.www.gov.cn/",
    })
    try:
        with urllib.request.urlopen(req, timeout=25, context=CTX) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        print(f"[{label}] 异常 {type(e).__name__}: {e}")
        return

    code = d.get("code")
    data = d.get("data") or []
    n = len(data) if isinstance(data, list) else 0
    print(f"\n[{label}] code={code} msg={d.get('msg','')!r} 返回 {n} 条")
    if n:
        first = data[0]
        print("  条目键：", list(first.keys())[:18])
    for it in (data[:4] if isinstance(data, list) else []):
        t = re.sub(r"<[^>]+>", "", str(it.get("title", "")))
        print(f"    · {t[:52]:<54}")
        print(f"      org={str(it.get('puborg',''))[:22]}  "
              f"date={str(it.get('pubtimeStr') or it.get('pubtime',''))[:12]}")
        print(f"      url={str(it.get('url',''))[:96]}")


for label, extra in COMBOS:
    probe(label, extra)
    sys.stdout.flush()
