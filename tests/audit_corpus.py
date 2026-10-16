"""语料质量审计：揪出不是政策原文的条目。

背景：扩容时按标题关键词过滤，但新闻稿/问答稿的标题里同样含
"补贴""救助"这些词，滤不干净。它们进了库会污染检索——
实测「我家老人能领什么钱」返回了育儿补贴的新闻稿。

改用结构性判据：真正的政策原文必有
  (a) 发文字号（X发〔2024〕N号），或
  (b) 条款结构（第一条、第二条…）
两样都没有的，大概率是新闻、讲话、解读、公示。

    python tests/audit_corpus.py            # 只报告
    python tests/audit_corpus.py --purge    # 报告并移出（移到 data/quarantine/）
"""
import json
import re
import shutil
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import config

# 新闻/宣传体标题特征
NEWS_MARK = re.compile(
    r"([！？]|→|——|"
    r"来了|查查|看这里|一文读懂|速看|收藏|转需|"
    r"让.{0,12}得到|更加务实|如何|怎么|什么样|是什么|有哪些|"
    r"解读|答记者问|新闻发布会|评论|观察|"
    r"讲话|致辞|批示|调研|强调|指出|要求|"
    r"公示|公告|征求意见|结果反馈|"
    r"通报|表彰|名单|目录|方案印发|会议)"
)
# 这些标题即使没有条款结构也可能是政策（办法/规定类常常只有几条）
POLICY_LIKE = re.compile(r"(办法|规定|条例|实施细则|实施方案|实施意见|通知|意见|决定|方案)$")
# 公文体套语。这是判别"政策原文 vs 新闻稿"最有力的信号——
# 新闻稿会转述政策，但不会写"请遵照执行"。实测比标题特征可靠得多。
OFFICIALESE = re.compile(
    r"(现将.{0,30}(印发|发布|公布)给你们|请(认真)?(遵照|贯彻)执行|"
    r"请结合实际.{0,10}(贯彻|执行)|现予公布|特此通知|特此公告|"
    r"自\s*\d{4}\s*年.{0,6}起(施行|实施|执行|生效)|"
    r"本(办法|规定|细则|意见|方案|通知)自|"
    r"(第一|二|三|四|五|六|七|八|九|十)条\s|"
    r"附则|本办法所称|本规定所称)"
)
# 纯技术性文件，对民生问答没用
TECHNICAL = re.compile(
    r"(药品目录|诊疗方案|技术规范|编码|数据标准|接口|"
    r"会计准则|统计报表|表单格式|代码表|分类与代码|"
    r"职业资格|考试|大纲|教材|题库)"
)


def analyze(policy: dict) -> tuple[str, str]:
    """返回 (判定, 理由)。判定取值 ok / suspect / bad。

    判别顺序（强信号优先）：
      发文字号 > 条款结构 > 公文套语 > 标题特征
    前三者任意命中即可确认是政策原文；全不命中基本是新闻/讲话/解读。
    """
    title = policy.get("title", "")
    text = policy.get("full_text", "")
    docno = policy.get("doc_number", "")
    clauses = len(re.findall(r"第[一二三四五六七八九十百零〇\d]+条", text))
    chars = policy.get("char_count") or len(re.findall(r"[一-鿿]", text))

    has_docno = bool(re.search(r"[〔\[\(]\s*(?:19|20)\d{2}\s*[〕\]\)]\s*\d+\s*号", docno))
    has_structure = clauses >= 3
    has_officialese = bool(OFFICIALESE.search(text[:6000]))

    # 纯技术性目录，对民生问答没用，优先级最高
    if TECHNICAL.search(title) and clauses == 0:
        return "bad", "技术性目录文件，对民生问答无价值"

    # 强信号命中 → 是政策原文
    if has_docno or has_structure or has_officialese:
        why = "发文字号" if has_docno else ("条款结构" if has_structure else "公文套语")
        return "ok", why

    # 三个强信号全不命中 —— 基本可以断定不是政策原文
    if NEWS_MARK.search(title):
        return "bad", "新闻/讲话/解读体标题，且无文号、无条款、无公文套语"
    if not POLICY_LIKE.search(title.strip()):
        return "bad", "标题不是政策文体，且无文号、无条款、无公文套语"
    # 只剩标题像政策这一条弱证据了
    if chars < 600:
        return "suspect", f"正文仅 {chars} 字，疑似摘要或转载"
    return "suspect", "标题像政策，但无文号、无条款、无公文套语，需人工看一眼"


def main() -> int:
    purge = "--purge" in sys.argv
    pdir = config.POLICY_DIR
    qdir = config.CACHE_DIR.parent / "quarantine"
    qdir.mkdir(parents=True, exist_ok=True)

    buckets = {"ok": [], "suspect": [], "bad": []}
    files: dict[str, Path] = {}

    for f in sorted(pdir.glob("*.json")):
        try:
            p = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        verdict, why = analyze(p)
        buckets[verdict].append((p, why))
        files[p.get("id", f.stem)] = f

    total = sum(len(v) for v in buckets.values())
    print("=" * 84)
    print(f"  语料质量审计：共 {total} 份")
    print("=" * 84)
    print(f"  确认是政策原文      {len(buckets['ok']):>4} 份")
    print(f"  存疑（缺文号/结构）  {len(buckets['suspect']):>4} 份")
    print(f"  确认不是政策        {len(buckets['bad']):>4} 份")

    print("\n" + "=" * 84)
    print("  确认不是政策的（建议移出）")
    print("=" * 84)
    for i, (p, why) in enumerate(buckets["bad"], 1):
        print(f"  {i:>3}. {p.get('title','')[:62]}")
        print(f"       {why}")

    if len(buckets["suspect"]) <= 40:
        print("\n" + "=" * 84)
        print("  存疑的（缺文号与条款结构）")
        print("=" * 84)
        for i, (p, why) in enumerate(buckets["suspect"], 1):
            print(f"  {i:>3}. {p.get('title','')[:58]:<60} {why[:24]}")

    if not purge:
        print(f"\n  加 --purge 可把「确认不是政策」的 {len(buckets['bad'])} 份"
              f"移到 {qdir}")
        return 0

    moved = 0
    for p, _ in buckets["bad"]:
        pid = p.get("id", "")
        f = files.get(pid)
        if f and f.exists():
            shutil.move(str(f), str(qdir / f.name))
            sf = config.STRUCT_DIR / f.name
            if sf.exists():
                shutil.move(str(sf), str(qdir / ("struct_" + sf.name)))
            moved += 1
    print(f"\n  已移出 {moved} 份到 {qdir}")
    print("  确认无误后可直接删除该目录。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
