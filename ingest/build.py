"""语料构建 CLI：抓取 → 解析 → 结构化 → 落盘。

用法：
    python -m ingest.build --urls urls.txt                  # 只抓取解析
    python -m ingest.build --urls urls.txt --structure      # 并做 LLM 结构化
    python -m ingest.build --urls urls.txt --force          # 强制重抓
    python -m ingest.build --selftest                       # 不联网，验证管线
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import config
from core.llm import LLM, LLMError
from ingest import fetch, parse, structure


def make_id(url: str) -> str:
    return "p" + hashlib.sha1(url.encode("utf-8")).hexdigest()[:8]


def load_urls(path: Path) -> list[str]:
    urls: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # 支持 "标题<TAB>URL" 或直接 URL
        if "\t" in line:
            line = line.split("\t")[-1].strip()
        if line.startswith("http"):
            urls.append(line)
    return urls


def build_one(url: str, force: bool = False, reparse: bool = False) -> tuple[dict | None, str]:
    """抓取并解析单条。返回 (policy, 状态说明)。

    force   —— 重新下载
    reparse —— 不下载，用本地缓存的 HTML 重新解析（调完解析器时用，
               避免反复请求政府网站）
    """
    pid = make_id(url)
    out = config.POLICY_DIR / f"{pid}.json"
    cache = config.CACHE_DIR / "html" / f"{pid}.html"

    if out.exists() and not force and not reparse:
        try:
            return json.loads(out.read_text(encoding="utf-8")), "已存在，跳过"
        except (json.JSONDecodeError, OSError):
            pass

    final_url = url
    if reparse and cache.exists():
        html = cache.read_text(encoding="utf-8")
        source = "本地缓存"
    else:
        try:
            raw, ctype, final_url = fetch.get(url)
        except fetch.FetchError as e:
            return None, f"抓取失败：{e}"
        html = parse.decode(raw, ctype)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(html, encoding="utf-8")
        source = "已下载"

    policy = parse.parse_policy(html, final_url or url)
    policy["id"] = pid

    if policy["char_count"] < 300:
        return None, f"正文过短（{policy['char_count']} 字），疑似非政策正文页"

    out.write_text(json.dumps(policy, ensure_ascii=False, indent=2), encoding="utf-8")
    return policy, f"OK（{policy['char_count']} 字，{policy['extract_mode']}，{source}）"


def structure_all(force: bool = False) -> None:
    try:
        llm = LLM()
    except LLMError as e:
        print(f"  [跳过结构化] {e}")
        return

    files = sorted(config.POLICY_DIR.glob("*.json"))
    done = skipped = failed = 0
    for i, f in enumerate(files, 1):
        try:
            policy = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        pid = policy.get("id", f.stem)
        out = config.STRUCT_DIR / f"{pid}.json"
        if out.exists() and not force:
            skipped += 1
            continue

        print(f"  [{i}/{len(files)}] 结构化：{policy.get('title', pid)[:40]}…", flush=True)
        result = structure.structure_policy(policy, llm)
        out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        if result.get("_extracted"):
            done += 1
        else:
            failed += 1
            print(f"        失败：{result.get('_error', '')[:90]}")

    print(f"  结构化完成：成功 {done}，跳过 {skipped}，失败 {failed}")


def report() -> None:
    from core.store import PolicyStore
    store = PolicyStore(config.POLICY_DIR, config.STRUCT_DIR).load()
    s = store.stats()
    print("\n" + "=" * 64)
    print("语料库概况")
    print("=" * 64)
    print(f"  政策数：{s['政策数']}    条款数：{s['条款数']}")
    print(f"  时效  ：{s['时效分布']}")
    print(f"  分类  ：{s['分类分布']}")
    print(f"  地域  ：{s['地域']}")
    if s["政策数"]:
        print("\n  清单：")
        for p in list(store.policies.values())[:40]:
            flag = "有" if p.structured else "无"
            print(f"    [{p.status_on():<4}] [{flag}结构] {p.title[:44]:<46} {p.issuer[:16]}")


def selftest() -> None:
    """用内置样本验证「解析 → 结构化 → 入库 → 检索」全链路，不联网。"""
    print("=" * 64)
    print("离线自检：解析 → 结构化 → 入库 → 检索")
    print("=" * 64)
    sys.path.insert(0, str(_ROOT / "tests"))
    from test_parse import FIXTURE

    policy = parse.parse_policy(FIXTURE, "https://example.gov.cn/demo.html")
    policy["id"] = "selftest-1"
    assert policy["char_count"] > 300, f"正文太短：{policy['char_count']}"
    print(f"[1] 解析  OK  正文 {policy['char_count']} 字，文号 {policy['doc_number']}")

    config.ensure_dirs()
    tmp = config.CACHE_DIR / "selftest_policies"
    tmp_struct = config.CACHE_DIR / "selftest_structured"
    tmp.mkdir(parents=True, exist_ok=True)
    tmp_struct.mkdir(parents=True, exist_ok=True)
    (tmp / "selftest-1.json").write_text(
        json.dumps(policy, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    # 结构化结果用固定样本，保证自检不依赖网络和模型
    (tmp_struct / "selftest-1.json").write_text(json.dumps({
        "policy_id": "selftest-1",
        "title": policy["title"],
        "category": "养老",
        "target_group": ["具有本市户籍且年满80周岁的老年人"],
        "benefit": [{"type": "现金补贴", "standard": "每人每月180元", "condition": "80至89周岁"}],
        "materials": ["居民身份证", "居民户口簿", "本人社会保障卡"],
        "process": ["向户籍所在地街道办事处提出申请"],
        "handler": "户籍所在地街道办事处",
        "duration": "15个工作日",
        "form_name": "高龄老年人津贴申请审批表",
        "form_fields": ["姓名", "身份证号", "户籍地址", "联系电话"],
        "eligibility_rules": [
            {"field": "hukou", "op": "eq", "value": "本市户籍", "desc": "具有本市户籍"},
            {"field": "age", "op": "gte", "value": 80, "desc": "年满80周岁"},
        ],
        "_extracted": True,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    from core.store import PolicyStore
    store = PolicyStore(tmp, tmp_struct).load()
    assert len(store) == 1 and len(store.clauses) >= 6
    assert store.get("selftest-1").structured, "结构化数据未加载"
    print(f"[2] 入库  OK  {len(store)} 份政策，{len(store.clauses)} 条条款，"
          f"资格规则 {len(store.get('selftest-1').structured['eligibility_rules'])} 条")

    from core.retriever import Retriever
    r = Retriever(store, llm=None)
    hits = r.search("高龄老人补贴多少钱", top_k=3, rerank=False)
    assert hits, "检索无结果"
    print(f"[3] 检索  OK  top1 = {hits[0].policy.title} / {hits[0].clause.no}")
    print(f"     {hits[0].text[:70]}…")

    from core import tools as T
    ctx = T.ToolContext(store, r)
    T.call("update_profile", ctx, {"fields": {"age": 82, "hukou": "本市户籍"}})
    out = T.call("assess_eligibility", ctx, {})
    v = out["results"][0]["verdict"]
    print(f"[4] 体检  OK  判定 = {v}   （期望：符合）")
    assert v == "符合", f"规则引擎判定错误：{v}"

    T.call("update_profile", ctx, {"fields": {"age": 70}})
    out = T.call("assess_eligibility", ctx, {})
    v = out["results"][0]["verdict"]
    print(f"[5] 反例  OK  70岁判定 = {v}   （期望：不符合）")
    assert v == "不符合", f"70 岁不应符合：{v}"

    print("\n全链路自检通过")


def main() -> int:
    ap = argparse.ArgumentParser(description="政策语料构建")
    ap.add_argument("--urls", type=Path, help="URL 清单文件（每行一个）")
    ap.add_argument("--structure", action="store_true", help="执行 LLM 结构化抽取")
    ap.add_argument("--force", action="store_true", help="强制重抓/重抽")
    ap.add_argument("--reparse", action="store_true",
                    help="不重新下载，用本地缓存的 HTML 重新解析（改了解析器后用）")
    ap.add_argument("--selftest", action="store_true", help="离线全链路自检")
    args = ap.parse_args()

    config.ensure_dirs()

    if args.selftest:
        selftest()
        return 0

    # 有 --urls 就先抓取；只给 --structure 时跳过抓取，直接对已有语料做抽取
    if args.urls:
        if not args.urls.exists():
            print(f"找不到 URL 清单：{args.urls}")
            return 1
        urls = load_urls(args.urls)
        print(f"待抓取 {len(urls)} 条\n")
        ok, fail, skip = 0, 0, 0
        for i, url in enumerate(urls, 1):
            policy, status = build_one(url, force=args.force, reparse=args.reparse)
            if policy is None and "已存在" in status:
                skip += 1
                mark = "-- "
            elif policy is None:
                fail += 1
                mark = "!! "
            else:
                ok += 1
                mark = "OK "
            title = policy.get("title", "")[:36] if policy else ""
            print(f"[{mark}] {i:>2}/{len(urls)} {status:<34} {title}")
            if policy is None and status.startswith("抓取失败"):
                print(f"         {url}")
        print(f"\n抓取完成：成功 {ok}，失败 {fail}，跳过 {skip}")
    elif not args.structure:
        ap.print_help()
        return 1

    if args.structure:
        n = len(list(config.POLICY_DIR.glob("*.json")))
        print(f"\n开始 LLM 结构化抽取（共 {n} 份政策，可中断后重跑，已完成的会跳过）…")
        structure_all(force=args.force)

    report()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
