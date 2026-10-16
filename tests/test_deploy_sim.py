"""模拟线上环境的验收测试：对外监听 + 强制认证。

对应 DEPLOY.md 的验收清单。部署到 Render 之前先在本地全过一遍，
比在云上试错快得多，也不浪费构建时间。

**这个测试自带服务进程**，直接跑即可：
    python tests/test_deploy_sim.py

它存在的意义不只是"部署前检查一遍"，更是防止一类回归：
只在线上配置下才暴露的 bug。已经抓到过一个——
_SESSION_SECRET 在「环境变量给了字符串」和「随机生成 bytes」两条路径
上类型不一致，hmac 收到 str 直接抛 TypeError。
本地不设密钥就永远走不到坏的那条，登录看着一切正常，一部署全员登录失败。
所以这里**特意把 SECRET 设成字符串**，模拟线上。
"""
import http.cookiejar
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

USER = "judge"
PASS = "Test1234"
# 故意用字符串形式的 SECRET——线上就是字符串，这条路径必须被覆盖
SECRET = "0123456789abcdef0123456789abcdef"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


PORT = str(free_port())
BASE = f"http://127.0.0.1:{PORT}"

results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  [{'✓' if ok else '✗'}] {name}" + (f"   {detail}" if detail else ""))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """不跟随重定向，便于观察 302。"""
    def redirect_request(self, *a, **k):
        return None


def jar_opener(jar: http.cookiejar.CookieJar, follow: bool = True):
    """构造带 cookie 处理的 opener。

    坑：不能为了"不跟随重定向"就 build_opener(NoRedirect) 重新造一个——
    那样会把 CookieProcessor 丢掉，表现为"登录返回了 Set-Cookie 但 jar 是空的"，
    白白让人去查服务端。两个 handler 必须挂到同一个 opener 上。
    """
    handlers = [urllib.request.HTTPCookieProcessor(jar)]
    if not follow:
        handlers.append(NoRedirect)
    return urllib.request.build_opener(*handlers)


def request(path: str, method: str = "GET", data: dict | None = None,
            jar: http.cookiejar.CookieJar | None = None, follow: bool = True):
    """发一个请求。"""
    op = jar_opener(jar if jar is not None else http.cookiejar.CookieJar(), follow)

    body = None
    headers = {"User-Agent": "deploy-sim"}
    if data is not None:
        body = urllib.parse.urlencode(data).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(BASE + path, data=body, headers=headers, method=method)
    try:
        with op.open(req, timeout=20) as r:
            return r.status, dict(r.headers), r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode("utf-8", "replace")


# ---------------------------------------------------------------- 起服务
# 环境变量按线上那套配：对外监听、强制认证、SECRET 是字符串。
env = {
    **os.environ,
    "POLICY_AGENT_HOST": "0.0.0.0",
    "POLICY_AGENT_PORT": PORT,
    "POLICY_AGENT_USER": USER,
    "POLICY_AGENT_PASS": PASS,
    "POLICY_AGENT_SECRET": SECRET,
    "PYTHONIOENCODING": "utf-8",
    "PYTHONUTF8": "1",
}
log = ROOT / "data" / "cache" / "deploy_sim_server.txt"
log.parent.mkdir(parents=True, exist_ok=True)
logf = open(log, "w", encoding="utf-8", errors="replace")

proc = subprocess.Popen(
    [sys.executable, "-X", "utf8", "-m", "web.server"],
    cwd=str(ROOT), env=env, stdout=logf, stderr=subprocess.STDOUT,
)

print("=" * 78)
print(f"  部署前验收模拟  {BASE}")
print("  （已按线上配置启动服务：对外监听 + 强制认证 + 字符串 SECRET）")
print("=" * 78)

# 等就绪
ready = False
for _ in range(40):
    if proc.poll() is not None:
        break
    try:
        st, _, _ = request("/healthz")
        if st == 200:
            ready = True
            break
    except Exception:
        pass
    time.sleep(0.5)

if not ready:
    print("  ✗ 服务未能在 20 秒内就绪，启动日志：")
    logf.flush()
    print(log.read_text(encoding="utf-8", errors="replace")[-2000:])
    proc.terminate()
    sys.exit(1)

# 1) 健康检查（平台探测器用这个，必须无需认证且返回 200）
try:
    st, _, body = request("/healthz")
    ok = st == 200 and "ok" in body.lower()
    record("健康检查 /healthz 免认证返回 200", ok, f"状态 {st}  {body[:60]}")
except Exception as e:
    record("健康检查 /healthz 免认证返回 200", False, str(e)[:60])

# 2) 未登录访问 /api/meta 必须被拒
try:
    st, _, body = request("/api/meta")
    ok = st == 401
    record("未登录访问 /api/meta 返回 401", ok, f"状态 {st}")
except Exception as e:
    record("未登录访问 /api/meta 返回 401", False, str(e)[:60])

# 3) 未登录访问首页 → 应当跳登录页（或直接给登录页）
try:
    st, hdr, body = request("/", follow=False)
    loc = hdr.get("Location", "")
    served_login = "登录" in body or "password" in body.lower() or "username" in body.lower()
    ok = st in (301, 302, 303, 307, 308) or (st == 200 and served_login)
    record("未登录访问首页被导向登录页", ok, f"状态 {st}  Location={loc[:40]}")
except Exception as e:
    record("未登录访问首页被导向登录页", False, str(e)[:60])

# 字段名必须与 web/login.html 的表单一致：name="user" / name="password"。
# 写成 username 的话服务端读到空账号，会被当成密码错误——查半天查不出所以然。
FORM = {"user": USER}

# 4) 错误密码必须被拒
bad_jar = http.cookiejar.CookieJar()
try:
    st, hdr, body = request("/login", "POST",
                            {**FORM, "password": "definitely-wrong"},
                            jar=bad_jar, follow=False)
    loc = hdr.get("Location", "")
    # 被拒的表现：跳回登录页（带 ?e=1），且不下发会话 cookie
    ok = "login" in loc or st == 401
    record("错误密码被拒绝", ok, f"状态 {st}  Location={loc[:36]}")
except Exception as e:
    record("错误密码被拒绝", False, str(e)[:60])

# 5) 正确密码能登录，且必须真的下发会话 cookie
jar2 = http.cookiejar.CookieJar()
try:
    st, hdr, body = request("/login", "POST",
                            {**FORM, "password": PASS},
                            jar=jar2, follow=False)
    loc = hdr.get("Location", "")
    has_cookie = any(c.name for c in jar2)
    ok = st in (301, 302, 303) and has_cookie and "login" not in loc
    record("正确密码登录成功并下发 Cookie", ok,
           f"状态 {st}  Location={loc[:20]}  Cookie数={len(list(jar2))}")
except Exception as e:
    record("正确密码登录成功并下发 Cookie", False, str(e)[:60])

# 6) 登录后能拿到政策库元数据
try:
    st, _, body = request("/api/meta", jar=jar2)
    data = json.loads(body) if st == 200 else {}
    n = data.get("stats", {}).get("政策数", 0)
    ok = st == 200 and n > 0
    record("登录后可读取政策库", ok, f"状态 {st}  政策 {n} 份")
except Exception as e:
    record("登录后可读取政策库", False, str(e)[:60])

# 7) 登出后应重新失效
try:
    request("/logout", jar=jar2, follow=False)
    st2, _, _ = request("/api/meta", jar=jar2)
    ok = st2 == 401
    record("登出后会话失效", ok, f"登出后 /api/meta 状态 {st2}")
except Exception as e:
    record("登出后会话失效", False, str(e)[:60])

print()
print("=" * 78)
passed = sum(1 for _, ok, _ in results if ok)
print(f"  {passed}/{len(results)} 项通过")
if passed < len(results):
    print("\n  未通过项：")
    for name, ok, detail in results:
        if not ok:
            print(f"    ✗ {name}   {detail}")

# 收尾：一定要把子进程收掉，否则测试跑一次就漏一个服务在后台
try:
    proc.terminate()
    proc.wait(timeout=8)
except Exception:
    try:
        proc.kill()
    except Exception:
        pass
finally:
    try:
        logf.close()
    except Exception:
        pass

print("=" * 78)
sys.exit(0 if passed == len(results) else 1)
