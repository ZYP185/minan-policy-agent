"""一键跑全部检查。

    python tests/run_all.py           # 含需要联网/调用模型的检查
    python tests/run_all.py --offline # 只跑不需要网络的检查
"""
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"

OFFLINE = [
    ("核心模块（分词 / BM25 / 金额抽取）", "test_core.py"),
    ("HTML 解析（容器 / 元数据 / 条款切分）", "test_parse.py"),
    ("英文前缀拦截（流式 / 不误伤正文）", "test_preamble.py"),
    ("扩库/巡检/覆盖自知", "test_ingest_extras.py"),
    ("全链路自检（解析→入库→检索→规则引擎）", None),   # 特殊：用 -m
    ("Web 层（认证闸门 / 请求体加固 / 会话收尾）", "test_server.py"),
    ("前端静态检查（JS 语法 / id / 卡片渲染器）", "check_frontend.py"),
]
ONLINE = [
    ("检索质量（真实语料，含 LLM 重排）", "test_retrieval_real.py"),
    ("Agent 端到端（工具链 / 抗幻觉）", "test_agent.py"),
    ("社区工作端（辖区确认 / 工具无失败）", "test_worker.py"),
    ("演示流程（按上台话术走一遍）", "test_demo_flow.py"),
]

offline_only = "--offline" in sys.argv
cases = OFFLINE + ([] if offline_only else ONLINE)

results = []
print("=" * 72)
print(f"  民安 · 检查清单（{len(cases)} 项{offline_only and '，离线' or ''}）")
print("=" * 72)

for label, script in cases:
    print(f"\n▶ {label}")
    print("-" * 72)
    t0 = time.time()

    if script is None:
        cmd = [sys.executable, "-X", "utf8", "-m", "ingest.build", "--selftest"]
    else:
        cmd = [sys.executable, "-X", "utf8", str(TESTS / script)]

    r = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    dt = time.time() - t0

    if r.returncode != 0:
        # 只回显尾部，避免刷屏
        tail = (r.stdout or "")[-1400:] + (r.stderr or "")[-600:]
        print(tail)
    ok = r.returncode == 0
    results.append((label, ok, dt))
    print(f"{'✓ 通过' if ok else '✗ 失败'}   （{dt:.1f}s）")

print("\n" + "=" * 72)
print("  汇总")
print("=" * 72)
for label, ok, dt in results:
    print(f"  {'✓' if ok else '✗'} {label:<44} {dt:>6.1f}s")

passed = sum(1 for _, ok, _ in results if ok)
print(f"\n  {passed}/{len(results)} 项通过")
sys.exit(0 if passed == len(results) else 1)
