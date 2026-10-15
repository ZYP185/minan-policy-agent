"""探测政府网站连通性与可抓取性（仅标准库）。"""
import re
import ssl
import sys
import time
import urllib.request
import urllib.error

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

TARGETS = [
    ("中国政府网-政策库", "https://www.gov.cn/zhengce/"),
    ("中国政府网-首页", "https://www.gov.cn/"),
    ("民政部", "https://www.mca.gov.cn/"),
    ("上海市政府-政策文件", "https://www.shanghai.gov.cn/nw12344/index.html"),
    ("北京市政府-政策", "https://www.beijing.gov.cn/zhengce/"),
    ("浙江政务服务网", "https://www.zjzwfw.gov.cn/"),
    ("广东政务服务网", "https://www.gdzwfw.gov.cn/"),
    ("人社部", "https://www.mohrss.gov.cn/"),
    ("国家医保局", "https://www.nhsa.gov.cn/"),
    ("深圳市政府公报", "https://www.sz.gov.cn/zfgb/"),
]


def detect_charset(raw: bytes, header_ct: str) -> str:
    """按 meta 声明 / header / 启发式 判断编码。"""
    m = re.search(rb'charset\s*=\s*["\']?\s*([A-Za-z0-9_\-]+)', raw[:4096], re.I)
    if m:
        return m.group(1).decode("ascii", "replace").lower()
    if header_ct:
        m = re.search(r"charset=([A-Za-z0-9_\-]+)", header_ct, re.I)
        if m:
            return m.group(1).lower()
    return "utf-8"


def fetch(url: str, timeout: int = 20):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        raw = r.read()
        return r.status, r.headers.get("Content-Type", ""), raw


def strip_tags(html: str) -> str:
    html = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    txt = re.sub(r"(?s)<[^>]+>", " ", html)
    txt = re.sub(r"&nbsp;?", " ", txt)
    txt = re.sub(r"\s+", " ", txt)
    return txt.strip()


print("=" * 78)
print("政府网站抓取探测")
print("=" * 78)

results = []
for name, url in TARGETS:
    t0 = time.time()
    try:
        status, ct, raw = fetch(url)
        dt = int((time.time() - t0) * 1000)
        cs = detect_charset(raw, ct)
        try:
            text = raw.decode(cs, "replace")
        except LookupError:
            text = raw.decode("utf-8", "replace")
            cs += "(未知,回退utf-8)"
        if ("gb" in cs or "gbk" in cs or "gb2312" in cs) and "�" in text[:3000]:
            text = raw.decode("gb18030", "replace")
            cs = "gb18030(回退)"
        plain = strip_tags(text)
        cjk = len(re.findall(r"[一-鿿]", plain))
        # 抓几个疑似政策标题
        titles = re.findall(r"[一-鿿]{6,40}", plain)[:3]
        results.append((name, True, status, len(raw), cs, cjk, dt, titles))
        flag = "OK  " if cjk > 20 else "WARN"
        print(f"[{flag}] {name:<20} {status}  {len(raw):>8} B  {cs:<10} 汉字={cjk:<6} {dt}ms")
        for t in titles:
            print(f"         · {t}")
    except Exception as e:
        dt = int((time.time() - t0) * 1000)
        msg = f"{type(e).__name__}: {e}"
        if len(msg) > 100:
            msg = msg[:100]
        results.append((name, False, None, 0, "", 0, dt, []))
        print(f"[FAIL] {name:<20} {dt}ms  {msg}")
    sys.stdout.flush()

ok = sum(1 for r in results if r[1])
print()
print(f"小结：{ok}/{len(TARGETS)} 个站点可访问")
