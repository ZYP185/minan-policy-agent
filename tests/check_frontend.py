"""前端静态检查：抽出 <script> 交给 node --check 做语法校验。

遍历 web/*.html，不只查 index.html——登录页也在这里面，它一个语法
错误就是评委连门都进不去，比界面白屏还严重。

前端是单文件无构建的，一个语法错误就是一整屏白。这个检查很快，
但能挡住最致命的问题。
"""
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"

node = shutil.which("node")

pages = sorted(WEB.glob("*.html"))
if not pages:
    print(f"✗ {WEB} 下没有找到 HTML")
    sys.exit(1)

# ---------------------------------------------------------- 逐页：语法 / id
page_js: dict[str, str] = {}
page_html: dict[str, str] = {}

for path in pages:
    html = path.read_text(encoding="utf-8")
    page_html[path.name] = html
    js = "\n".join(re.findall(r"(?is)<script[^>]*>(.*?)</script>", html))
    page_js[path.name] = js

    # 1) script 块
    print(f"[{path.name}] 抽出 JS：{len(js)} 字符")

    # 2) 语法
    if not node:
        print("  · 未找到 node，跳过语法校验")
    else:
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                         encoding="utf-8") as f:
            f.write(js)
            tmp = f.name
        try:
            r = subprocess.run([node, "--check", tmp], capture_output=True, text=True)
            if r.returncode == 0:
                print("  · node --check：语法通过 ✓")
            else:
                print("  · node --check：语法错误 ✗")
                print(r.stderr[:2000])
                sys.exit(1)
        finally:
            Path(tmp).unlink(missing_ok=True)

    # 3) JS 用 el("xxx") 取的 id，HTML 里必须有
    ids_in_html = set(re.findall(r'id="([^"]+)"', html))
    ids_used = set(re.findall(r'el\("([^"]+)"\)', js))
    missing = sorted(ids_used - ids_in_html - {"chips"})   # chips 是动态重建的
    if missing:
        print(f"  ✗ JS 引用了不存在的 id：{missing}")
        sys.exit(1)
    if ids_used:
        print(f"  · JS 引用的 {len(ids_used)} 个 id 全部存在 ✓")

if "index.html" not in page_js:
    print("✗ 缺少 web/index.html")
    sys.exit(1)
js = page_js["index.html"]
# 注意：下面所有检查都必须用 index.html 的内容。
# 上面的循环结束后局部变量 html 指向的是最后一个页面（login.html），
# 直接用它会让检查静默地跑错文件。
html = page_html["index.html"]

# ---------------------------------------------------------- 前后端契约
# 以下两项只针对 index.html：它才是对话界面，事件与卡片都送到这里。
#
# 4) 后端事件类型必须都被前端处理
#    eos / tool_start 是有意不处理的：
#      eos        —— 流结束标记，连接关闭本身就是信号
#      tool_start —— 模型刚开始吐 tool_use 块时触发，比 tool_call 早几毫秒，
#                    且此时还没有参数，等 tool_call 建芯片即可
INTENTIONALLY_IGNORED = {"eos", "tool_start"}

sys.path.insert(0, str(ROOT))
agent_src = (ROOT / "core" / "agent.py").read_text(encoding="utf-8")
emitted = set(re.findall(r'"type":\s*"([a-z_]+)"', agent_src)) | {"eos"}
handled = set(re.findall(r'case "([a-z_]+)":', js)) | INTENTIONALLY_IGNORED
unhandled = sorted(emitted - handled)
if unhandled:
    print(f"[4] ✗ 后端发出但前端未处理的事件：{unhandled}")
    sys.exit(1)
print(f"[4] 后端 {len(emitted)} 种事件类型前端均已处理 ✓"
      f"（{len(INTENTIONALLY_IGNORED)} 种有意忽略）")

# 5) 卡片类型必须都有渲染器
tool_src = (ROOT / "core" / "tools.py").read_text(encoding="utf-8")
kinds = set(re.findall(r'add_artifact\(\s*"([a-z_]+)"', tool_src))
renderers = set(re.findall(r"^\s{2}([a-z_]+)\(a\)\s*\{", js, re.M))
missing_r = sorted(kinds - renderers)
if missing_r:
    print(f"[5] ✗ 这些卡片没有渲染器：{missing_r}")
    sys.exit(1)
print(f"[5] {len(kinds)} 种卡片类型均有渲染器 ✓")

# ---------------------------------------------------------- CSS 健全性
# CSS 出错是静默的：浏览器直接丢掉坏规则，不报错。所以必须主动查。
# 6) 花括号必须配平
css = "\n".join(re.findall(r"(?is)<style[^>]*>(.*?)</style>", html))
depth, bad = 0, []
for i, ch in enumerate(css):
    if ch == "{":
        depth += 1
    elif ch == "}":
        depth -= 1
        if depth < 0:
            bad.append(i)
            depth = 0
if depth or bad:
    print(f"[6] ✗ CSS 花括号不配平（多余 {{ {depth} 个，多余 }} {len(bad)} 个）")
    sys.exit(1)
print(f"[6] CSS 花括号配平 ✓（{len(css)} 字符）")

# 7) 每条声明都要有冒号；选择器块不能空着
rules = re.findall(r"([^{}]+)\{([^{}]*)\}", css)
empty = [sel.strip()[:40] for sel, body in rules if not body.strip()]
if empty:
    print(f"[7] ✗ 这些规则块是空的：{empty[:5]}")
    sys.exit(1)
decls = re.findall(r"[;{]\s*([a-zA-Z-]+)\s*;", css)      # 只有属性名没值
if decls:
    print(f"[7] ✗ 这些声明缺少值：{decls[:5]}")
    sys.exit(1)
print(f"[7] {len(rules)} 条 CSS 规则结构完好 ✓")

# 8) 变量必须先定义后使用：用到 var(--x) 就必须有 --x 的定义
defined = set(re.findall(r"(--[a-zA-Z0-9-]+)\s*:", css))
used = set(re.findall(r"var\((--[a-zA-Z0-9-]+)", css))
undef = sorted(used - defined)
if undef:
    print(f"[8] ✗ 用到未定义的 CSS 变量：{undef}")
    sys.exit(1)
print(f"[8] {len(used)} 个 CSS 变量均已定义 ✓")

# 9) 长辈模式的覆盖规则不能引用不存在的类
elder_block = "\n".join(re.findall(r'(?s)html\[data-elder="1"\]([^{]*)\{', css))
elder_classes = set(re.findall(r"\.([a-zA-Z][a-zA-Z0-9_-]*)", elder_block))
html_classes = set(re.findall(r'class="([^"]+)"', html))
html_classes = {c for group in html_classes for c in group.split()}
# JS 里动态添加的类也算存在
js_classes = set(re.findall(r'className\s*=\s*"([^"]+)"', js))
js_classes = {c for group in js_classes for c in group.split()}
ghost = sorted(elder_classes - html_classes - js_classes)
if ghost:
    print(f"[9] ⚠ 长辈模式覆盖了 HTML/JS 里不存在的类：{ghost}")
else:
    print(f"[9] 长辈模式覆盖的 {len(elder_classes)} 个类均真实存在 ✓")

print("\n前端检查通过")
