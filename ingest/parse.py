"""HTML → 政策正文 + 元数据。

政府网站的正文容器五花八门（TRS、UCAP、各类自研 CMS），
所以这里用「已知容器优先 + 文本密度兜底」两级策略。
只用标准库。
"""
from __future__ import annotations

import re
from html import unescape

# 常见正文容器（按优先级）。政府网站 CMS 高度雷同，这几个覆盖大多数。
CONTAINERS = [
    (r'<div[^>]*id=["\']UCAP-CONTENT["\'][^>]*>', "UCAP"),        # 中国政府网
    (r'<div[^>]*class=["\'][^"\']*TRS_Editor[^"\']*["\'][^>]*>', "TRS"),
    (r'<div[^>]*class=["\'][^"\']*\bmainText\b[^"\']*["\'][^>]*>', "mainText"),  # 北京市政府
    (r'<div[^>]*id=["\']zoom["\'][^>]*>', "zoom"),
    (r'<div[^>]*class=["\'][^"\']*(?:article|content|detail|zwxl|view)[^"\']*["\'][^>]*>', "generic"),
    (r'<div[^>]*id=["\'][^"\']*(?:content|detail|article)[^"\']*["\'][^>]*>', "generic-id"),
]

# 正文里混进来的模板噪声
NOISE_LINE = re.compile(
    r"^\s*(?:打印|关闭|分享|收藏|返回顶部|扫一扫|上一篇|下一篇|字体|"
    r"来源[:：]|责任编辑[:：]|编辑[:：]|浏览量|【字体|网站地图|联系我们|"
    r"版权所有|主办单位|承办单位|技术支持|ICP备|政府网站标识码|"
    r"公安部备案号|网站标识码|您是第|点击量)"
)
SCRIPT_STYLE = re.compile(r"(?is)<(script|style|noscript|iframe)[^>]*>.*?</\1>")
TABLE_TAG = re.compile(r"(?i)</?(table|tr|td|th|tbody|thead)[^>]*>")
BR_TAG = re.compile(r"(?i)<\s*br\s*/?\s*>")
BLOCK_END = re.compile(r"(?i)</\s*(p|div|li|h[1-6]|tr|section|article)\s*>")
ALL_TAG = re.compile(r"(?s)<[^>]+>")
CJK = re.compile(r"[一-鿿]")


# ---------------------------------------------------------------- 编码
def decode(raw: bytes, content_type: str = "") -> str:
    """按声明编码解码，处理 GB2312 声明但实际是 GBK 的常见情况。"""
    m = re.search(rb'charset\s*=\s*["\']?\s*([A-Za-z0-9_\-]+)', raw[:4096], re.I)
    charset = m.group(1).decode("ascii", "replace").lower() if m else ""
    if not charset and content_type:
        m2 = re.search(r"charset=([A-Za-z0-9_\-]+)", content_type, re.I)
        if m2:
            charset = m2.group(1).lower()
    if not charset:
        charset = "utf-8"
    if charset in ("gb2312", "gbk", "gb-2312"):
        charset = "gb18030"     # 超集，能多解码不少字

    try:
        text = raw.decode(charset, "replace")
    except LookupError:
        text = raw.decode("utf-8", "replace")

    if text.count("�") > len(text) * 0.01 and charset != "utf-8":
        alt = raw.decode("utf-8", "replace")
        if alt.count("�") < text.count("�"):
            return alt
    return text


# ---------------------------------------------------------------- 容器定位
def _find_container(html: str) -> tuple[str, str]:
    for pattern, name in CONTAINERS:
        m = re.search(pattern, html, re.I)
        if not m:
            continue
        inner = _slice_div(html, m.start())
        if inner and len(CJK.findall(inner)) > 300:
            return inner, name
    return "", ""


def _slice_div(html: str, start: int) -> str:
    """从 <div ...> 的起始位置，按标签配对切出完整 div 内容。"""
    open_tag = re.compile(r"<(/?)div\b[^>]*?(/?)>", re.I)
    depth = 0
    for m in open_tag.finditer(html, start):
        tag = m.group(0)
        if m.group(2) == "/" or tag.rstrip().endswith("/>"):
            continue                      # 自闭合
        if m.group(1) == "/":
            depth -= 1
            if depth == 0:
                inner_start = html.find(">", start) + 1
                return html[inner_start:m.start()]
        else:
            depth += 1
    return ""


def _densest_block(html: str) -> str:
    """兜底：找出汉字最密的那个块级元素。"""
    body = re.search(r"(?is)<body[^>]*>(.*)</body>", html)
    html = body.group(1) if body else html
    html = SCRIPT_STYLE.sub(" ", html)

    best, best_score = "", 0
    # 粗粒度：按大块切分后比较汉字密度
    for chunk in re.split(r"(?i)</(?:div|article|section|main)>", html):
        if len(chunk) < 200:
            continue
        plain = _plain(chunk)
        n = len(CJK.findall(plain))
        if n < 300:
            continue
        # 汉字数 / 链接数 —— 正文链接少，导航栏链接多
        links = len(re.findall(r"(?i)<a\b", chunk))
        score = n / (1 + links)
        if score > best_score:
            best, best_score = chunk, score
    return best


# ---------------------------------------------------------------- 转纯文本
def _plain(fragment: str) -> str:
    s = SCRIPT_STYLE.sub(" ", fragment)
    s = TABLE_TAG.sub("\n", s)          # 表格行当独立行，保留结构
    s = BR_TAG.sub("\n", s)
    s = BLOCK_END.sub("\n", s)
    s = ALL_TAG.sub("", s)
    s = unescape(s)
    # 有些站点把 <br/> 转义成 &lt;br/&gt; 写进标题，unescape 之后才变成字面量标签。
    # 剥标签在 unescape 之前，所以这里要再清一遍，否则标题里会露出 <br/>。
    s = re.sub(r"</?[a-zA-Z][a-zA-Z0-9]{0,9}\s*/?>", " ", s)
    s = s.replace("　", " ").replace("\xa0", " ").replace("​", "")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n[ \t]+", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def _clean_lines(text: str) -> str:
    out = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            if out and out[-1] != "":
                out.append("")
            continue
        if NOISE_LINE.match(line):
            continue
        if len(line) <= 2 and not CJK.search(line):
            continue
        out.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def extract_body(html: str) -> tuple[str, str]:
    """返回 (正文, 提取方式)。"""
    inner, how = _find_container(html)
    if not inner:
        inner, how = _densest_block(html), "density"
    if not inner:
        inner, how = re.search(r"(?is)<body[^>]*>(.*)</body>", html).group(1) if re.search(r"(?is)<body", html) else html, "body"
    return _clean_lines(_plain(inner)), how


# ---------------------------------------------------------------- 元数据
RE_TITLE = [
    re.compile(r'<meta[^>]+name=["\'](?:ArticleTitle|articleTitle|title)["\'][^>]+content=["\']([^"\']+)', re.I),
    re.compile(r"<h1[^>]*>(.*?)</h1>", re.I | re.S),
    re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S),
]
RE_DOCNO = re.compile(r"([一-鿿]{1,10}[〔\[\(]\s*(?:19|20)\d{2}\s*[〕\]\)]\s*\d+\s*号)")
RE_DATE = [
    re.compile(r'<meta[^>]+name=["\'](?:PubDate|pubDate|Pubdate|pubdate|author)["\'][^>]+content=["\']([^"\']+)', re.I),
    re.compile(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\'](?:PubDate|pubDate|Pubdate)["\']', re.I),
]
RE_DATE_CN = re.compile(r"(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")
RE_ISSUER = re.compile(r"(?:来源|发布机构|发文机关)\s*[:：]\s*([^\s<>\n]{2,30})")
# 施行/废止日期
RE_EFFECTIVE = re.compile(
    r"自\s*(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*起\s*(?:施行|实施|执行|生效)"
)
RE_EXPIRY = re.compile(
    r"(?:有效期至|有效期截止|至)\s*(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"
    r"|有效期\s*(?:为)?\s*(\d+)\s*年"
)
RE_REGION = re.compile(r"([一-鿿]{2,8}(?:省|市|自治区|自治州))([一-鿿]{2,10}(?:区|县|市|旗))?")

# 站点名 / 栏目名后缀：政府网站标题尾巴上挂的这些不是政策名的一部分
RE_TITLE_TAIL = re.compile(
    r"[_\-|—·]{1,2}\s*("
    r"中国政府网|国务院公报|国务院文件|国务院部门文件|部门文件|"
    r"政策文件|政策法规|规范性文件|政府信息公开|政务公开|"
    r"网站|门户网站|首页|信息公开专栏|来源[:：]?[一-鿿]{0,15}"
    r"|[一-鿿]{2,12}(?:人民政府|民政局|财政局|人力资源和社会保障局|卫健委|办公厅|办公室)"
    r")\s*$"
)
# 【政策依据】【转发上级文件】这类栏目前缀
RE_TITLE_LEAD = re.compile(r"^\s*[【\[（(][^】\]）)]{2,12}[】\]）)]\s*")
# 书名号内的内容往往才是真正的政策名


def clean_title(raw: str) -> str:
    """把网页标题清洗成政策名。站点后缀可能有两三层，要反复剥。"""
    t = _plain(raw)
    t = re.sub(r"\s+", " ", t).strip()      # 标题里的换行一律收敛成空格
    # 中文标题里两个汉字之间的空格几乎必然是换行残留，收掉
    t = re.sub(r"(?<=[一-鿿]) (?=[一-鿿])", "", t)
    t = RE_TITLE_LEAD.sub("", t)
    for _ in range(6):                      # 反复剥，直到剥不动
        new = RE_TITLE_TAIL.sub("", t).strip()
        if new == t or len(new) < 4:
            break
        t = new
    return re.sub(r"\s+", " ", t).strip(" _-—·")


def extract_meta(html: str, body: str, url: str = "") -> dict:
    meta: dict = {"url": url}

    title = ""
    for rx in RE_TITLE:
        m = rx.search(html)
        if m:
            t = clean_title(m.group(1))
            if len(t) >= 4:
                title = t
                break
    meta["title"] = title

    m = RE_DOCNO.search(body) or RE_DOCNO.search(html[:20000])
    meta["doc_number"] = m.group(1) if m else ""

    pub = ""
    for rx in RE_DATE:
        m = rx.search(html)
        if m and re.search(r"20\d{2}", m.group(1)):
            pub = m.group(1).strip()
            break
    if not pub:
        m = RE_DATE_CN.search(html[:20000])
        if m:
            pub = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    meta["publish_date"] = _norm_date(pub)

    m = RE_EFFECTIVE.search(body)
    meta["effective_date"] = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}" if m else ""

    m = RE_EXPIRY.search(body)
    if m:
        if m.group(1):
            meta["expiry_date"] = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        else:
            meta["valid_years"] = int(m.group(4))
            meta["expiry_date"] = ""
    else:
        meta["expiry_date"] = ""

    m = RE_ISSUER.search(html[:30000])
    meta["issuer"] = _plain(m.group(1)).strip() if m else ""
    if not meta["issuer"]:
        # 从标题里猜发文机关，例如《上海市人民政府关于印发…的通知》
        m2 = re.match(r"([一-鿿]{2,12}(?:局|委|厅|办|政府|公署|部))", title)
        if m2:
            meta["issuer"] = m2.group(1)

    m = RE_REGION.search(meta["issuer"] or title)
    if m:
        meta["region"] = (m.group(1) or "") + (m.group(2) or "")
    else:
        meta["region"] = ""

    return meta


def _norm_date(s: str) -> str:
    if not s:
        return ""
    m = re.search(r"(20\d{2})\D{0,2}(\d{1,2})\D{0,2}(\d{1,2})", s)
    if m:
        try:
            return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        except ValueError:
            return ""
    return ""


def parse_policy(html: str, url: str = "") -> dict:
    body, how = extract_body(html)
    meta = extract_meta(html, body, url)
    meta["full_text"] = body
    meta["extract_mode"] = how
    meta["char_count"] = len(CJK.findall(body))
    return meta
