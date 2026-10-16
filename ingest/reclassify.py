"""定向重判政策类别。

为什么需要单独做这件事：完整结构化抽取的提示词里，类别只是十几个字段之一，
模型容易走捷径——**按发文机关判类别**。典型错误：
《教育部 民政部 …关于加快推进养老服务的意见》因为首位机关是"教育部"，
被标成了「教育」，而它讲的是养老服务。

这里用一个小而专的提示词，明确要求按政策内容判断，并且给出反例。
不重跑整个结构化，省时间也避免动到已经正确的字段。

    python -m ingest.reclassify --check     # 只比对，不改动
    python -m ingest.reclassify             # 写回
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import config
from core.llm import LLM, LLMError, text_of

CATEGORIES = ["养老", "救助", "生育", "就业", "医疗", "住房", "残疾人", "教育", "其他"]

SYSTEM = "你是政策分类器。只输出类别名，不解释、不加标点、不加引号。"

PROMPT = """判断这份政策属于哪个民生类别。

**按政策内容判断，不要按发文机关判断。** 会签单位里出现某个部门名，
不代表政策就属于那个领域。反例：
《教育部 民政部 财政部关于加快推进养老服务的意见》讲的是养老服务，
应归为「养老」——"教育部"只是会签单位之一。

标题：{title}

正文节选：
{body}

只输出一个类别名，从这几个里选：
养老 / 救助 / 生育 / 就业 / 医疗 / 住房 / 残疾人 / 教育 / 其他

判断依据（按优先级）：
1. 政策要解决的是哪类人的什么问题
2. 待遇/服务的内容是什么
3. 不要看发文单位名称
"""


def ask(title: str, body: str, llm: LLM) -> str:
    msg = llm.complete(
        [{"role": "user", "content": PROMPT.format(title=title[:200], body=body[:1400])}],
        system=SYSTEM,
        max_tokens=16,
        temperature=0.0,
    )
    raw = re.sub(r"[\s，。、,.\"'「」【】]", "", text_of(msg))
    # 容错：模型可能回了"养老类""属于养老"这种
    for c in CATEGORIES:
        if c in raw:
            return c
    return ""


def main() -> int:
    ap = argparse.ArgumentParser(description="定向重判政策类别")
    ap.add_argument("--check", action="store_true", help="只比对，不写回")
    ap.add_argument("--only-empty", action="store_true",
                    help="只处理未分类的，不动已分类的（保守模式）")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 份（试跑用）")
    args = ap.parse_args()

    config.ensure_dirs()
    llm = LLM()

    files = sorted(config.STRUCT_DIR.glob("*.json"))
    if not files:
        print("没有结构化结果。先运行：python -m ingest.build --urls urls.txt --structure")
        return 1

    print("=" * 84)
    print(f"  类别重判：共 {len(files)} 份{'（仅未分类）' if args.only_empty else ''}")
    print("=" * 84)

    changes: list[tuple[str, str, str, str]] = []   # (pid, title, old, new)
    unchanged = failed = 0

    for i, f in enumerate(files, 1):
        if args.limit and i > args.limit:
            break
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        pid = d.get("policy_id") or f.stem
        title = d.get("title", "")
        old = (d.get("category") or "").strip()
        if args.only_empty and old:
            continue

        # 正文从原文里取（结构化结果里没有全文）
        pfile = config.POLICY_DIR / f.name
        body = ""
        if pfile.exists():
            try:
                body = json.loads(pfile.read_text(encoding="utf-8")).get("full_text", "")
            except (json.JSONDecodeError, OSError):
                pass

        try:
            new = ask(title, body, llm)
        except LLMError as e:
            failed += 1
            print(f"  [{i:>3}] ✗ {title[:44]}  {e}")
            continue

        if not new:
            failed += 1
            print(f"  [{i:>3}] ? {title[:44]}  模型没给出有效类别")
            continue

        if new == old:
            unchanged += 1
            continue

        changes.append((pid, title, old, new))
        print(f"  [{i:>3}] {old or '(空)':<5} → {new:<5}  {title[:52]}", flush=True)
        if not args.check:
            d["category"] = new
            f.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")

    print()
    print("=" * 84)
    print(f"  类别有变 {len(changes)} 份   未变 {unchanged} 份   失败 {failed} 份")
    if args.check and changes:
        print("  （--check 模式，未写回。去掉 --check 执行）")
    print("=" * 84)

    if changes:
        print("\n  变更明细：")
        for pid, title, old, new in changes:
            print(f"    {old or '(空)':<5} → {new:<5} {title[:56]}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
