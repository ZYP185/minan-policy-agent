"""一键启动：民安 · 社区民生政策服务智能体。

    python run.py

会自动打开浏览器。按 Ctrl+C 停止。
"""
from __future__ import annotations

import os
import sys
import threading
import time
import webbrowser
from pathlib import Path

# Windows 控制台默认 GBK，中文会乱码
if sys.platform == "win32":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import config


def preflight() -> bool:
    """启动前自检，问题说清楚，不让用户面对一个黑盒。"""
    print("=" * 66)
    print("  民安 · 社区民生政策服务智能体")
    print("=" * 66)
    print(config.describe())

    ok = True

    if not config.API_KEY:
        print("  ✗ 未找到 API Key")
        print("    请设置环境变量 DEEPSEEK_API_KEY 后重试。")
        print("    PowerShell:  $env:DEEPSEEK_API_KEY = \"sk-...\"")
        ok = False

    config.ensure_dirs()
    n_policy = len(list(config.POLICY_DIR.glob("*.json")))
    n_struct = len(list(config.STRUCT_DIR.glob("*.json")))
    print(f"  政策文件 : {n_policy} 份（结构化 {n_struct} 份）")
    if n_policy == 0:
        print()
        print("  ⚠ 政策库为空，界面能打开但检索不到任何政策。")
        print("    请先构建语料：")
        print("        python -m ingest.build --urls urls.txt --structure")
        print("    或先跑离线自检确认管线正常：")
        print("        python -m ingest.build --selftest")
        print()
    print()

    if not (config.WEB_DIR / "index.html").exists():
        print(f"  ✗ 找不到前端文件：{config.WEB_DIR / 'index.html'}")
        ok = False

    return ok


def main() -> int:
    if not preflight():
        return 1

    from web import server

    def open_browser() -> None:
        time.sleep(1.2)
        webbrowser.open(f"http://{config.HOST}:{config.PORT}/")

    threading.Thread(target=open_browser, daemon=True).start()
    return server.main()


if __name__ == "__main__":
    raise SystemExit(main())
