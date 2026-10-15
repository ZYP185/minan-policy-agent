"""把 gov.cn 搜索接口的返回结构打出来。"""
import json
import ssl
import sys
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
CTX = ssl.create_default_context()

params = {
    "t": "zhengcelibrary", "q": "高龄津贴", "timetype": "timeqb",
    "sort": "score", "searchfield": "title", "p": 1, "n": 10,
}
url = "https://sousuo.www.gov.cn/search-gov/data?" + urllib.parse.urlencode(params)
req = urllib.request.Request(url, headers={
    "User-Agent": UA, "Accept": "application/json, text/plain, */*",
    "Referer": "https://sousuo.www.gov.cn/",
})
with urllib.request.urlopen(req, timeout=25, context=CTX) as r:
    data = json.loads(r.read().decode("utf-8", "replace"))


def shape(o, depth=0, key="", max_depth=4):
    pad = "  " * depth
    if depth > max_depth:
        return
    if isinstance(o, dict):
        print(f"{pad}{key or '<root>'}: dict({len(o)} keys)")
        for k, v in o.items():
            shape(v, depth + 1, k, max_depth)
    elif isinstance(o, list):
        print(f"{pad}{key}: list({len(o)})")
        if o:
            shape(o[0], depth + 1, "[0]", max_depth)
    else:
        s = str(o)
        print(f"{pad}{key}: {type(o).__name__} = {s[:70]!r}")


print("=" * 78)
shape(data, max_depth=3)
print("=" * 78)

# 直接找可能的条目列表
def walk(o, path=""):
    if isinstance(o, dict):
        for k, v in o.items():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                print(f"\n列表在 {path}.{k}，共 {len(v)} 条")
                print("  首条键：", list(v[0].keys()))
                for it in v[:5]:
                    t = str(it.get("title", ""))
                    import re
                    t = re.sub(r"<[^>]+>", "", t)
                    print(f"    · {t[:50]:<52} {str(it.get('puborg',''))[:16]} "
                          f"{str(it.get('pubtimeStr') or it.get('pubtime',''))[:10]}")
                    print(f"      url = {it.get('url','')}")
                return True
            if walk(v, f"{path}.{k}"):
                return True
    return False


print("查找结果列表：")
if not walk(data):
    print("  没找到")
