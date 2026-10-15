"""探测：能否用接口按地区检索到政策原文？

中国政府网政策文件库有一个搜索接口。如果它能覆盖地方政策，
就有可能在运行时发现并抓取任意城市的政策。
"""
import json
import ssl
import sys
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
CTX = ssl.create_default_context()

# 候选接口
ENDPOINTS = [
    ("gov.cn 政策文件库", "https://sousuo.www.gov.cn/search-gov/data"),
]

QUERIES = [
    "高龄津贴",
    "广州市 高龄津贴",
    "广州市 最低生活保障",
    "成都市 育儿补贴",
    "杭州市 残疾人补贴",
]


def get(url: str, params: dict) -> tuple[int, object]:
    qs = urllib.parse.urlencode(params)
    req = urllib.request.Request(f"{url}?{qs}", headers={
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://sousuo.www.gov.cn/",
    })
    try:
        with urllib.request.urlopen(req, timeout=25, context=CTX) as r:
            body = r.read().decode("utf-8", "replace")
            try:
                return r.status, json.loads(body)
            except json.JSONDecodeError:
                return r.status, body[:400]
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}"


print("=" * 78)
print("探测 gov.cn 政策文件库搜索接口")
print("=" * 78)

for name, url in ENDPOINTS:
    for q in QUERIES:
        params = {
            "t": "zhengcelibrary",
            "q": q,
            "timetype": "timeqb",
            "sort": "score",
            "searchfield": "title",
            "p": 1,
            "n": 10,
        }
        st, data = get(url, params)
        print(f"\n[{st}] {name}  q={q!r}")
        if not isinstance(data, dict):
            print("     非 JSON：", str(data)[:180])
            continue

        # 摸清返回结构
        if not hasattr(probe_search_api := sys.modules[__name__], "_shape"):
            print("     顶层键：", list(data.keys())[:12])
            probe_search_api._shape = True

        search = data.get("searchVO") or data.get("data") or data
        lst = None
        if isinstance(search, dict):
            for k in ("listVO", "list", "result", "results", "items"):
                if isinstance(search.get(k), list):
                    lst = search[k]
                    break
        if lst is None:
            print("     未找到结果数组，顶层键：", list(data.keys())[:14])
            continue

        print(f"     共 {len(lst)} 条")
        for it in lst[:6]:
            if not isinstance(it, dict):
                continue
            title = it.get("title") or it.get("TITLE") or ""
            org = it.get("puborg") or it.get("author") or ""
            date = it.get("pubtimeStr") or it.get("pubtime") or ""
            link = it.get("url") or it.get("link") or ""
            # 标题里带 HTML 高亮标签，去掉
            import re
            title = re.sub(r"<[^>]+>", "", str(title))
            print(f"       · {title[:46]:<48} {str(org)[:14]:<16} {str(date)[:10]}")

sys.stdout.flush()
