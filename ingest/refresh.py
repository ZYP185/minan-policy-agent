"""增量更新与时效巡检。

为什么这件事比"多抓几个城市"更要命：
一条已经废止的政策留在库里，居民照着去办会白跑一趟，甚至错过申报期。
**过期的数据比没有数据更糟**——没有数据时系统会说"我不知道"，
有过期数据时它会自信地给出错误答案。

做的事：
  1. 重抓已入库的每一条政策
  2. 比对正文指纹 → 发现被修订过的
  3. 检查失效日期 → 标记已废止的
  4. 抓取失败的 → 可能是网站改版或页面下线，需要人工确认
  5. 可选：对变更的政策重新做结构化抽取

用法：
    python -m ingest.refresh --check            # 只巡检，不改动任何文件
    python -m ingest.refresh                    # 巡检并更新变更的内容
    python -m ingest.refresh --re-structure     # 变更的政策重新结构化
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import config
from ingest import fetch, parse as P

CJK = re.compile(r"[一-鿿]")


def fingerprint(text: str) -> str:
    """正文指纹。归一化后再哈希，避免空格/换行的抖动造成假变更。"""
    norm = re.sub(r"\s+", "", text or "")
    norm = norm.replace("　", "").replace("​", "")
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]


def _to_date(s: str) -> date | None:
    if not s:
        return None
    m = re.search(r"(\d{4})\D+(\d{1,2})\D+(\d{1,2})", str(s))
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


@dataclass
class Check:
    pid: str
    title: str
    url: str
    status: str            # changed / expired / gone / same / error
    detail: str = ""
    new_text: str = ""
    old_chars: int = 0
    new_chars: int = 0


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def by(self, status: str) -> list[Check]:
        return [c for c in self.checks if c.status == status]


def inspect_one(policy: dict, timeout: int = 25) -> Check:
    pid = policy.get("id", "")
    title = policy.get("title", "")
    url = policy.get("url", "")
    old_text = policy.get("full_text", "")
    c = Check(pid=pid, title=title, url=url, status="same",
              old_chars=len(CJK.findall(old_text)))

    # 1) 失效日期
    exp = _to_date(policy.get("expiry_date", ""))
    if exp and date.today() > exp:
        c.status = "expired"
        c.detail = f"有效期已于 {exp.isoformat()} 届满"

    # 2) 重抓比对
    if not url:
        c.status = "error"
        c.detail = "没有记录原始 URL，无法复核"
        return c

    try:
        raw, ct, final = fetch.get(url, timeout=timeout)
    except fetch.FetchError as e:
        if c.status != "expired":
            c.status = "gone"
            c.detail = f"抓不到了：{e}（网站改版或页面已下线？）"
        else:
            c.detail += f"；且已抓不到（{e}）"
        return c

    html = P.decode(raw, ct)
    body, _ = P.extract_body(html)
    c.new_text = body
    c.new_chars = len(CJK.findall(body))

    if c.new_chars < 100:
        if c.status != "expired":
            c.status = "gone"
            c.detail = f"页面还在但正文几乎为空（{c.new_chars} 字），可能已改版"
        return c

    if fingerprint(old_text) != fingerprint(body):
        if c.status != "expired":
            c.status = "changed"
        delta = c.new_chars - c.old_chars
        c.detail = (f"正文有变化（{c.old_chars} → {c.new_chars} 字，"
                    f"{'+' if delta >= 0 else ''}{delta}）")

    return c


def apply_updates(checks: list[Check], policies: dict[str, Path],
                  re_structure: bool = False) -> int:
    """把变更写回文件。返回更新条数。"""
    n = 0
    for c in checks:
        path = policies.get(c.pid)
        if not path or not path.exists():
            continue
        try:
            policy = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue

        if c.status == "changed" and c.new_text:
            # 重新解析元数据（施行日期、发文字号可能也变了）
            policy["full_text"] = c.new_text
            policy["char_count"] = c.new_chars
            policy["_refreshed_at"] = date.today().isoformat()
            path.write_text(json.dumps(policy, ensure_ascii=False, indent=2),
                            encoding="utf-8")
            n += 1
            # 结构化结果已过期，删掉让它重做
            sfile = config.STRUCT_DIR / f"{c.pid}.json"
            if sfile.exists():
                sfile.unlink()

        elif c.status == "expired":
            policy["_stale"] = True
            policy["_stale_reason"] = c.detail
            path.write_text(json.dumps(policy, ensure_ascii=False, indent=2),
                            encoding="utf-8")
            n += 1

    if re_structure and n:
        from ingest import build
        print("\n  对变更的政策重新做结构化抽取…")
        build.structure_all(force=False)
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description="政策库增量更新与时效巡检")
    ap.add_argument("--check", action="store_true", help="只巡检，不改动文件")
    ap.add_argument("--re-structure", action="store_true",
                    help="变更的政策重新做 LLM 结构化抽取")
    ap.add_argument("--region", default="", help="只巡检指定地区")
    ap.add_argument("--limit", type=int, default=0, help="最多巡检多少条（0=全部）")
    args = ap.parse_args()

    config.ensure_dirs()

    files = sorted(config.POLICY_DIR.glob("*.json"))
    policies: dict[str, Path] = {}
    items: list[dict] = []
    for f in files:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if args.region and args.region not in (d.get("region") or ""):
            continue
        policies[d.get("id", f.stem)] = f
        items.append(d)

    if not items:
        print("政策库为空。先运行：python -m ingest.build --urls urls.txt")
        return 1

    if args.limit:
        items = items[: args.limit]

    print("=" * 82)
    print(f"  政策库巡检：共 {len(items)} 份")
    print("=" * 82)
    print("  （每条都会重新访问原始网站，请耐心等待）\n")

    rep = Report()
    for i, d in enumerate(items, 1):
        c = inspect_one(d)
        rep.checks.append(c)
        mark = {"same": "· ", "changed": "改", "expired": "废",
                "gone": "失", "error": "?"}.get(c.status, "? ")
        print(f"  [{mark}] {i:>2}/{len(items)} {c.title[:44]:<46} {c.detail[:34]}",
              flush=True)

    print()
    print("=" * 82)
    print("  巡检结果")
    print("=" * 82)
    n_same = len(rep.by("same"))
    print(f"  未变更      {n_same:>3} 份")
    for st, label in (("changed", "内容有变更"), ("expired", "已过有效期"),
                      ("gone", "抓不到了"), ("error", "无法复核")):
        lst = rep.by(st)
        if not lst:
            continue
        print(f"  {label:<11} {len(lst):>3} 份")
        for c in lst:
            print(f"        · {c.title[:52]}")
            if c.detail:
                print(f"          {c.detail}")

    todo = rep.by("changed") + rep.by("expired")
    if args.check:
        print(f"\n  （--check 模式，未改动任何文件）")
        if todo:
            print(f"  有 {len(todo)} 份需要处理，去掉 --check 即可写回。")
        return 0

    if todo:
        n = apply_updates(todo, policies, re_structure=args.re_structure)
        print(f"\n  已更新 {n} 份。")
        print("  提示：检索索引在服务启动时构建，重启服务后生效。")
    else:
        print("\n  政策库是最新的。")

    gone = rep.by("gone")
    if gone:
        print(f"\n  ⚠ 有 {len(gone)} 份抓不到了，需要人工确认。")
        print("    可能是网站改版、页面迁移，或政策原文已下线。")
        print("    处理办法：找到新地址后，把对应 JSON 的 url 字段改掉；")
        print("    确认已废止的，直接删除该 JSON 文件。")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
