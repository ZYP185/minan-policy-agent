"""自动扩库：给一个城市政策栏目入口，自动发现 → 验证 → 抓取 → 结构化 → 入库。

用法：
    # 先看看能发现什么（不写入）
    python -m ingest.expand --entry <URL> --pages 3 --dry-run

    # 全部收下
    python -m ingest.expand --entry <URL> --pages 3 --yes

    # 只挑第 1、3、5 条
    python -m ingest.expand --entry <URL> --pick 1,3,5

    # 指定地域（列表页看不出地域时用）
    python -m ingest.expand --entry <URL> --region 西安市 --yes

人工确认是刻意保留的：自动发现的标题里难免混进解读稿、废止公告、
跨地域转载。收错一条，居民就会被误导一次。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import config
from ingest import build, discover


def show(res: discover.DiscoverResult) -> list[discover.Candidate]:
    """打印候选清单，返回验证通过的。"""
    print()
    print("=" * 86)
    print(f"  发现结果：{res.entry}")
    print("=" * 86)

    if not res.candidates:
        print(f"  {res.note or '没有发现任何候选'}")
        return []

    good = [c for c in res.candidates if c.ok]
    print(f"  共 {len(res.candidates)} 个候选，其中 {len(good)} 个验证为政策正文\n")
    print(f"  {'#':>3}  {'字数':>6} {'条款':>4}  {'标题':<44} {'发文机关'}")
    print("  " + "-" * 84)

    for i, c in enumerate(res.candidates, 1):
        if c.ok:
            print(f"  {i:>3}  {c.char_count:>6} {c.clause_count:>4}  "
                  f"{c.title[:42]:<44} {c.issuer[:16]}")
        else:
            why = c.error or "非政策正文"
            print(f"  {i:>3}  {'--':>6} {'--':>4}  {c.title[:42]:<44} [{why[:18]}]")

    if res.note:
        print(f"\n  说明：{res.note}")
    return good


def pick_interactively(res: discover.DiscoverResult) -> list[discover.Candidate]:
    good = [c for c in res.candidates if c.ok]
    if not good:
        return []
    print()
    print("  输入编号（逗号分隔）、all 全收、none 取消：")
    try:
        ans = input("  > ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return []
    if ans in ("", "none", "n", "q"):
        return []
    if ans in ("all", "a", "y", "yes"):
        return good
    picked = []
    for part in ans.replace("，", ",").split(","):
        part = part.strip()
        if not part.isdigit():
            continue
        i = int(part)
        if 1 <= i <= len(res.candidates) and res.candidates[i - 1].ok:
            picked.append(res.candidates[i - 1])
    return picked


def ingest(cands: list[discover.Candidate], region: str = "",
           do_structure: bool = True) -> tuple[int, int]:
    """把选中的候选入库。返回 (成功, 失败)。"""
    ok = fail = 0
    print()
    print("=" * 86)
    print("  抓取入库")
    print("=" * 86)

    for i, c in enumerate(cands, 1):
        policy, status = build.build_one(c.url, force=True)
        if policy is None:
            fail += 1
            print(f"  [!!] {i}/{len(cands)} {status:<30} {c.title[:36]}")
            continue

        # 地域：列表页常常推断不出，用参数兜底
        if region and not policy.get("region"):
            policy["region"] = region
            out = config.POLICY_DIR / f"{policy['id']}.json"
            out.write_text(json.dumps(policy, ensure_ascii=False, indent=2),
                           encoding="utf-8")

        ok += 1
        print(f"  [OK] {i}/{len(cands)} {policy['char_count']:>5}字  "
              f"{policy['title'][:40]}")

    print(f"\n  入库完成：成功 {ok}，失败 {fail}")

    if do_structure and ok:
        print("\n  开始结构化抽取（LLM）…")
        build.structure_all(force=False)
    return ok, fail


def main() -> int:
    ap = argparse.ArgumentParser(
        description="自动扩库：从政策栏目入口页发现并入库政策",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n"
               "  python -m ingest.expand --entry https://www.xa.gov.cn/gk/zcfg/gfxwj/zfgfxwj/1.html --pages 2 --dry-run\n"
               "  python -m ingest.expand --entry <URL> --region 西安市 --yes\n",
    )
    ap.add_argument("--entry", required=True, help="政策栏目列表页 URL")
    ap.add_argument("--pages", type=int, default=1, help="翻页数（URL 形如 /1.html 时可用）")
    ap.add_argument("--region", default="", help="强制指定地域，如 西安市")
    ap.add_argument("--limit", type=int, default=40, help="最多验证多少个候选")
    ap.add_argument("--yes", "-y", action="store_true", help="全部收下，不交互")
    ap.add_argument("--pick", default="", help="指定编号，如 1,3,5")
    ap.add_argument("--dry-run", action="store_true", help="只看发现结果，不写入")
    ap.add_argument("--no-verify", action="store_true", help="跳过试抓验证（快，但会混进栏目页）")
    ap.add_argument("--no-structure", action="store_true", help="入库后不做 LLM 结构化")
    ap.add_argument("--livelihood", action="store_true",
                    help="只收民生类政策（过滤掉数据管理/交通管制/应急预案等）")
    ap.add_argument("--keyword", default="", help="自定义标题关键词过滤（正则）")
    args = ap.parse_args()

    config.ensure_dirs()

    print(f"入口：{args.entry}")
    print(f"翻页：{args.pages}  地域：{args.region or '(自动识别)'}"
          f"{'  筛选：仅民生' if args.livelihood else ''}"
          f"{'  关键词：' + args.keyword if args.keyword else ''}\n")

    res = discover.discover(
        args.entry,
        pages=args.pages,
        verify=not args.no_verify,
        verify_limit=args.limit,
        livelihood_only=args.livelihood,
        keyword=args.keyword,
    )
    good = show(res)

    if not good:
        print("\n  没有可入库的政策。")
        if not args.no_verify:
            print("  提示：该列表页可能是 JS 动态加载的。试试：")
            print("    · 换一个更具体的栏目页（如某个年份、某个分类）")
            print("    · 用 --no-verify 看看原始链接有没有政策正文")
        return 1

    if args.dry_run:
        print("\n  （--dry-run，未写入）")
        return 0

    if args.pick:
        chosen = []
        for part in args.pick.replace("，", ",").split(","):
            if part.strip().isdigit():
                i = int(part.strip())
                if 1 <= i <= len(res.candidates) and res.candidates[i - 1].ok:
                    chosen.append(res.candidates[i - 1])
    elif args.yes:
        chosen = good
    else:
        chosen = pick_interactively(res)

    if not chosen:
        print("\n  未选择任何政策，退出。")
        return 0

    ingest(chosen, region=args.region, do_structure=not args.no_structure)
    build.report()

    print("\n  提示：检索索引在服务启动时构建。若服务正在运行，需重启才能查到新政策。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
