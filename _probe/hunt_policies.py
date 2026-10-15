# -*- coding: utf-8 -*-
"""
Policy page hunter for the community livelihood-policy agent demo.

Modes:
  probe  <url> [url2 ...]      : fetch each URL, print status/title/hanzi count
  seeds  <file-with-urls>      : fetch list pages, extract candidate links matching keywords
  verify <file-with-urls>      : fetch candidate article URLs, save raw html, full report
  grep   <url> <regex>         : print regex matches from a fetched page

Only stdlib. TLS verification is always ON. Browser UA is always sent.
"""
import sys
import os
import re
import json
import time
import gzip
import zlib
import hashlib
import ssl
import html as htmllib
import urllib.request
import urllib.parse
import urllib.error

PROBE_DIR = os.path.dirname(os.path.abspath(__file__))
RAW_DIR = os.path.join(PROBE_DIR, "raw")
os.makedirs(RAW_DIR, exist_ok=True)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
CTX = ssl.create_default_context()

KEYWORDS = ["低保", "最低生活保障", "特困", "救助", "津贴", "补贴", "养老", "医保",
            "残疾", "生育", "育儿", "就业", "创业", "住房", "保障", "高龄", "抚恤",
            "临时救助", "供养", "门诊", "报销", "护理"]

FEATURE_WORDS = ["现将", "通知如下", "印发", "施行", "申请", "应当", "符合", "条件",
                 "补助", "标准", "对象", "审批", "审核", "公示", "户籍"]


def fetch(url, timeout=30, referer=None):
    req = urllib.request.Request(url)
    req.add_header("User-Agent", UA)
    req.add_header("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8")
    req.add_header("Accept-Language", "zh-CN,zh;q=0.9,en;q=0.8")
    req.add_header("Accept-Encoding", "gzip, deflate")
    if referer:
        req.add_header("Referer", referer)
    with urllib.request.urlopen(req, timeout=timeout, context=CTX) as resp:
        data = resp.read()
        enc = (resp.headers.get("Content-Encoding") or "").lower()
        if "gzip" in enc:
            data = gzip.decompress(data)
        elif "deflate" in enc:
            try:
                data = zlib.decompress(data)
            except zlib.error:
                data = zlib.decompress(data, -zlib.MAX_WBITS)
        return resp.status, resp.headers, resp.geturl(), data


def decode_bytes(data, headers=None):
    ct = ""
    if headers:
        ct = headers.get("Content-Type") or ""
    m = re.search(r"charset=([A-Za-z0-9_-]+)", ct, re.I)
    cands = []
    if m:
        cands.append(m.group(1))
    head = data[:8192].decode("ascii", "ignore")
    m2 = re.search(r'charset\s*=\s*["\']?\s*([A-Za-z0-9_-]+)', head, re.I)
    if m2:
        cands.append(m2.group(1))
    cands += ["utf-8", "gb18030"]
    seen = set()
    for e in cands:
        e = e.lower()
        if e in seen:
            continue
        seen.add(e)
        try:
            return data.decode(e), e
        except Exception:
            continue
    return data.decode("utf-8", "replace"), "utf-8/replace"


def strip_scripts(html):
    return re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)


def html_to_text(html):
    txt = strip_scripts(html)
    txt = re.sub(r"(?is)<br\s*/?>", "\n", txt)
    txt = re.sub(r"(?is)</(p|div|li|tr|h[1-6])>", "\n", txt)
    txt = re.sub(r"(?s)<[^>]+>", " ", txt)
    txt = htmllib.unescape(txt)
    txt = re.sub(r"[ \t ]+", " ", txt)
    return txt


def hanzi_count(text):
    return len(re.findall(r"[一-鿿]", text))


def page_title(html):
    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
    if m:
        t = htmllib.unescape(re.sub(r"\s+", " ", m.group(1))).strip()
        return t
    return ""


def main_container(html):
    """Try to isolate the article body container; return (text, container_id)."""
    ids = ["UCAP-CONTENT", "zoom", "content", "detail", "article", "artical",
           "TRS_Editor", "mainText", "content_body", "fontzoom", "zoomcon",
           "conTent", "Content", "vsb_content", "text", "zoom_Con", "divContent",
           "xlcontent", "wznr", "zwnr", "article_content"]
    best = None
    for cid in ids:
        for pat in (r'(?is)<div[^>]*(?:id|class)\s*=\s*["\']?%s["\']?[^>]*>(.*)' % re.escape(cid),):
            m = re.search(pat, html)
            if m:
                seg = m.group(1)
                # cut at a reasonable closing point to avoid the rest of page
                seg = seg[:200000]
                hc = hanzi_count(html_to_text(seg))
                if hc > 400:
                    if best is None or hc > best[1]:
                        best = (seg, hc, cid)
    if best:
        return html_to_text(best[0]), best[2]
    return html_to_text(html), "full-page"


def extract_links(html, base):
    links = []
    for m in re.finditer(r'(?is)<a\s+[^>]*href\s*=\s*["\']?([^"\'\s>]+)["\']?[^>]*>(.*?)</a>', html):
        href = htmllib.unescape(m.group(1)).strip()
        text = re.sub(r"(?s)<[^>]+>", "", m.group(2))
        text = htmllib.unescape(re.sub(r"\s+", " ", text)).strip()
        if not href or href.startswith(("javascript:", "#", "mailto:", "tel:")):
            continue
        full = urllib.parse.urljoin(base, href)
        links.append((full, text))
    return links


def save_raw(url, html):
    h = hashlib.md5(url.encode("utf-8")).hexdigest()[:10]
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", url.split("//")[-1])[:80]
    fn = os.path.join(RAW_DIR, "%s__%s.html" % (slug, h))
    with open(fn, "w", encoding="utf-8") as f:
        f.write(html)
    return fn


def date_guess(html, text):
    pats = [
        r"(20\d{2}[-/年]\d{1,2}[-/月]\d{1,2}日?)",
        r"(20\d{2}年\d{1,2}月)",
    ]
    for p in pats:
        m = re.search(p, text[:5000])
        if m:
            return m.group(1)
    return ""


def probe_one(url, verbose=True):
    rec = {"url": url}
    try:
        status, headers, final_url, data = fetch(url)
        html, enc = decode_bytes(data, headers)
        text, cid = main_container(html)
        hc = hanzi_count(text)
        title = page_title(html) or "(no <title>)"
        found = [w for w in FEATURE_WORDS if w in text[:20000]]
        rec.update({"status": status, "enc": enc, "bytes": len(data),
                    "final_url": final_url, "title": title, "hanzi": hc,
                    "container": cid, "feature_words": found,
                    "date": date_guess(html, text), "ok": True})
        if verbose:
            print("[%s] %s" % (status, url))
            print("    title: %s" % title)
            print("    bytes=%d enc=%s hanzi(body)=%d container=%s date=%s" % (
                len(data), enc, hc, cid, rec["date"]))
            print("    feature_words: %s" % ",".join(found[:10]))
            if final_url != url:
                print("    -> %s" % final_url)
        rec["html"] = html
    except urllib.error.HTTPError as e:
        rec.update({"status": e.code, "ok": False, "err": "HTTPError %s" % e.code})
        if verbose:
            print("[ERR %s] %s" % (e.code, url))
    except Exception as e:
        rec.update({"status": None, "ok": False, "err": "%s: %s" % (type(e).__name__, e)})
        if verbose:
            print("[ERR] %s -> %s: %s" % (url, type(e).__name__, e))
    return rec


def mode_probe(urls):
    for u in urls:
        probe_one(u, verbose=True)
        print("-" * 70)
        time.sleep(0.5)


def mode_seeds(files):
    urls = []
    for fn in files:
        with open(fn, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    urls.append(line)
    for u in urls:
        print("=" * 78)
        print("SEED: %s" % u)
        rec = probe_one(u, verbose=True)
        html = rec.get("html")
        if not html:
            continue
        base = rec.get("final_url") or u
        links = extract_links(html, base)
        hits = []
        seen = set()
        for full, text in links:
            if full in seen:
                continue
            hit_kw = [k for k in KEYWORDS if k in text]
            if hit_kw and len(text) >= 6:
                seen.add(full)
                hits.append((full, text, hit_kw))
        print("    candidate links: %d / total links %d" % (len(hits), len(links)))
        for full, text, kw in hits[:60]:
            print("    * %s | %s" % (text[:60], full))
        time.sleep(0.5)


def mode_verify(files):
    urls = []
    for fn in files:
        with open(fn, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    urls.append(line)
    report = []
    for u in urls:
        rec = probe_one(u, verbose=True)
        if rec.get("ok"):
            fn = save_raw(u, rec["html"])
            rec["raw_file"] = fn
        rec.pop("html", None)
        report.append(rec)
        print("-" * 70)
        time.sleep(0.4)
    out = os.path.join(PROBE_DIR, "verify_report.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    print("report -> %s" % out)


GOV_SEARCH = ("https://sousuo.www.gov.cn/search-gov/data?t=zhengcelibrary&q={q}"
              "&timetype=timeqb&mintime=&maxtime=&sort=score&sortType=1"
              "&searchfield=title&p={p}&n={n}")


def mode_mine(args):
    """Query gov.cn policy-library search API for each keyword, print title/url/date/org."""
    n = 20
    t = "zhengcelibrary"
    out_lines = []
    for kw in args:
        for p in (1,):
            url = GOV_SEARCH.format(q=urllib.parse.quote(kw), p=p, n=n).replace(
                "t=zhengcelibrary", "t=" + t)
            try:
                status, headers, final_url, data = fetch(url)
                j = json.loads(decode_bytes(data, headers)[0])
            except Exception as e:
                print("QUERY FAIL %s: %s" % (kw, e))
                continue
            sv = j.get("searchVO") or {}
            total = 0
            print("### QUERY: %s (total=%s)" % (kw, sv.get("extendresult", {}).get("totalCount", "?")))
            for cat, catv in (sv.get("catMap") or {}).items():
                for item in (catv.get("listVO") or []):
                    title = item.get("title", "")
                    u = item.get("url", "")
                    pub = item.get("pubtimeStr", "")
                    org = item.get("puborg", "")
                    pcode = item.get("pcode", "")
                    total += 1
                    print("  [%s] %s | %s | %s | %s" % (pub, title[:70], org, pcode, u))
                    out_lines.append("%s\t%s\t%s\t%s\t%s" % (pub, title, org, pcode, u))
            if total == 0:
                print("  (no results under catMap; keys=%s)" % list((sv.get("catMap") or {}).keys()))
    with open(os.path.join(PROBE_DIR, "gov_mine.txt"), "a", encoding="utf-8") as f:
        f.write("\n".join(out_lines) + "\n")


def mode_head(args):
    url = args[0]
    n = int(args[1]) if len(args) > 1 else 3000
    rec = probe_one(url, verbose=False)
    if not rec.get("ok"):
        print(rec)
        return
    status, headers, final_url, data = fetch(url)
    txt, enc = decode_bytes(data, headers)
    print("=== head of %s (enc=%s bytes=%d) ===" % (url, enc, len(data)))
    print(txt[:n])


def mode_grep(args):
    url, pattern = args[0], args[1]
    rec = probe_one(url, verbose=False)
    if not rec.get("ok"):
        print(rec)
        return
    txt = html_to_text(rec["html"])
    for m in re.finditer(pattern, txt):
        s = max(0, m.start() - 40)
        print(txt[s:m.end() + 60].replace("\n", " ")[:200])


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "probe"
    rest = sys.argv[2:]
    if mode == "probe":
        mode_probe(rest)
    elif mode == "seeds":
        mode_seeds(rest)
    elif mode == "verify":
        mode_verify(rest)
    elif mode == "head":
        mode_head(rest)
    elif mode == "mine":
        mode_mine(rest)
    elif mode == "grep":
        mode_grep(rest)
    else:
        print(__doc__)
