"""Web 层检查：认证闸门 / 请求体加固 / 会话收尾修复。

全程离线，不调用模型。启一个真实的 ThreadingHTTPServer 打真实 HTTP 请求，
因为这一层最典型的 bug（302 让前端静默空白、Cookie 解析被整体丢弃）
只有走网络才能暴露出来。
"""
import json
import os
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 必须在 import config 之前设：LLM() 没 key 会在 Session 构造时抛异常。
# 这里只为了让它构造得出来，全程不会真的发请求。
os.environ.setdefault("DEEPSEEK_API_KEY", "sk-test-not-used")

import config                                          # noqa: E402
from core.store import PolicyStore                     # noqa: E402
from web import server                                 # noqa: E402

USER, PASS, PASS_CN = "judge", "ma2026", "民安密码"


# ---------------------------------------------------------------- 单元
def test_token():
    tok = server.new_token()
    assert server.token_ok(tok), "刚签发的 token 应当有效"

    ts, nonce, sig = tok.split(".")
    assert not server.token_ok(f"{ts}.{nonce}.{'0' * 32}"), "签名被篡改仍通过"
    assert not server.token_ok(f"{ts}.{nonce}.x"), "长度不符的签名仍通过"
    assert not server.token_ok(f"{ts}.other.{sig}"), "随机数被换掉仍通过"
    assert not server.token_ok("garbage"), "格式不符仍通过"
    assert not server.token_ok(""), "空串仍通过"
    assert not server.token_ok(None), "None 仍通过"

    # 同一秒内签发的 token 必须互不相同——签名只认时间戳的话，
    # 同一秒登录的两位评委会共用同一个会话，对话直接串台。
    assert len({server.new_token() for _ in range(20)}) == 20, \
        "短时间内签发的 token 出现重复"

    # 超出有效期
    old = str(int(server.time.time()) - (config.SESSION_DAYS + 1) * 86400)
    old_nonce = "0123456789abcdef"
    assert not server.token_ok(
        f"{old}.{old_nonce}.{server._sign(f'{old}:{old_nonce}')}"
    ), "过期 token 仍通过"
    print("[OK] token     签发 / 唯一性 / 篡改 / 过期")


def test_cookie_parse():
    """Cookie 头里混入垃圾片段时，仍要能取出自己的那个。

    这是不用 http.cookies.SimpleCookie 的原因——它遇到没有 "=" 的片段
    会抛 CookieError 并整体丢弃，用户就被莫名踢回登录页。
    """
    cases = [
        ("ma_session=abc", "abc"),
        ("ma_session=abc; other=1", "abc"),
        ("other=1; ma_session=abc", "abc"),
        ('ma_session="abc"', "abc"),
        # 下面这几种是 SimpleCookie 会整体丢弃、而我们必须扛住的
        ("ma_session=abc; bad", "abc"),
        ("ma_session=abc; =x", "abc"),
        ("junk; ma_session=abc; more=%zz", "abc"),
        ("ma_session=abc; trailing", "abc"),
    ]
    for header, want in cases:
        got = server.cookie_value(header, "ma_session")
        assert got == want, f"{header!r} → {got!r}，期望 {want!r}"

    for header in ("", None, "other=1", "ma_session=", "ma_session2=abc"):
        assert server.cookie_value(header, "ma_session") is None, \
            f"{header!r} 不应解析出 token"
    print(f"[OK] cookie    {len(cases)} 种含垃圾片段的头均正确解析")


def test_credential_chinese():
    """含中文的密码必须能比对。

    hmac.compare_digest 对非 ASCII 的 str 直接抛 TypeError，而这是个
    中文项目——不 encode 就是"设了中文密码就登录不了且报 500"。
    """
    config.AUTH_USER = "评审"
    config.AUTH_PASS = PASS_CN
    try:
        assert server.credential_ok("评审", PASS_CN), "中文账号密码比对失败"
        assert not server.credential_ok("评审", "wrong"), "错误密码通过"
        assert not server.credential_ok("他人", PASS_CN), "错误账号通过"
    finally:
        config.AUTH_USER, config.AUTH_PASS = USER, PASS
    print("[OK] 凭据     中文账号密码可正常比对（compare_digest 编码坑）")


def test_repair_tail():
    """被中断的那一轮必须补上 tool_result，否则会话永久 400。"""
    store = server.STORE
    s = server.agent_mod.Session(store, None)

    # 情形一：末条是带 tool_use 的 assistant（客户端中途断连的样子）
    s.messages = [
        {"role": "user", "content": "我家老人能领什么"},
        {"role": "assistant", "content": [
            {"type": "text", "text": "我查一下"},
            {"type": "tool_use", "id": "t1", "name": "search_policy", "input": {}},
            {"type": "tool_use", "id": "t2", "name": "read_clause", "input": {}},
        ]},
    ]
    server.agent_mod._repair_tail(s)
    assert len(s.messages) == 3, "未补上收尾消息"
    tail = s.messages[-1]
    assert tail["role"] == "user"
    ids = {b["tool_use_id"] for b in tail["content"]}
    assert ids == {"t1", "t2"}, f"补的 tool_result 对不上：{ids}"
    assert all(b.get("is_error") for b in tail["content"]), "收尾结果应标记为错误"
    print("[OK] 收尾     中断轮补齐 2 个 tool_result")

    # 情形二：正常收尾（末条 assistant 无 tool_use）→ 必须是空操作
    s.messages = [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": [{"type": "text", "text": "您好"}]},
    ]
    before = len(s.messages)
    server.agent_mod._repair_tail(s)
    assert len(s.messages) == before, "正常收尾被误改"

    # 情形三：末条是 user（步数耗尽的样子）→ 也是空操作
    s.messages = [
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t9",
                                           "name": "x", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t9",
                                      "content": "ok"}]},
    ]
    before = len(s.messages)
    server.agent_mod._repair_tail(s)
    assert len(s.messages) == before, "步数耗尽后又被误加一条"

    # 情形四：空历史
    s.messages = []
    server.agent_mod._repair_tail(s)
    assert s.messages == []
    print("[OK] 收尾     正常收尾 / 步数耗尽 / 空历史 均为空操作")


def test_run_interrupt():
    """客户端断连（生成器被 close）时的收尾与解锁。

    这是最要命的一条路径：run() 先把带 tool_use 的 assistant 消息写进
    历史，跑完工具才补 tool_result。中间被 close 就永久残缺，该会话之后
    每次请求都 400。同时锁必须释放，否则这个会话再也发不出消息。
    """
    store = server.STORE

    class FakeLLM:
        def stream(self, **kw):
            yield {"type": "text", "text": "我查一下"}
            yield {"type": "tool_start", "name": "search_policy"}
            yield {"type": "_final", "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "我查一下"},
                    {"type": "tool_use", "id": "tu1",
                     "name": "search_policy", "input": {"query": "高龄津贴"}},
                ],
            }}

    s = server.agent_mod.Session(store, None)
    s.llm = FakeLLM()

    gen = server.agent_mod.run(s, "我家老人 82 岁")
    for ev in gen:
        if ev["type"] == "tool_call":
            break                      # 工具刚要执行，客户端就断了
    assert s._busy.locked(), "run() 执行期间未持锁"
    gen.close()                        # ← 模拟客户端断连

    last = s.messages[-1]
    assert last["role"] == "user", f"断连后末条应是 tool_result，实际 {last['role']}"
    assert last["content"][0]["type"] == "tool_result", "断连后历史未补齐"
    assert last["content"][0]["tool_use_id"] == "tu1", "补错了 tool_use_id"
    assert not s._busy.locked(), "断连后会话锁未释放，该会话将永久卡死"
    print("[OK] 中断     断连后历史补齐且锁已释放")

    # 锁被占住时，第二个请求必须立刻被拒，而不是排队干等
    s.llm = FakeLLM()
    gen = server.agent_mod.run(s, "第一句")
    next(gen)                          # 启动，占住锁
    second = list(server.agent_mod.run(s, "第二句"))
    assert second and second[0]["type"] == "error", f"并发请求未被拒绝：{second}"
    assert "处理中" in second[0]["message"], f"拒绝文案不对：{second[0]['message']}"
    gen.close()
    assert not s._busy.locked(), "close 后锁未释放"
    print("[OK] 并发     同会话并发请求被立即拒绝，不排队")


# ---------------------------------------------------------------- HTTP
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None                     # 不自动跟随，302 原样暴露给断言


_opener = urllib.request.build_opener(_NoRedirect)


def call(method, path, body=None, ctype="application/json", cookie=None):
    """发一个请求，返回 (状态码, 响应头, 响应体)。3xx 不自动跟随。"""
    data = body
    if isinstance(body, dict):
        data = ("&".join(f"{k}={v}" for k, v in body.items())
                if ctype.startswith("application/x-www-form-urlencoded")
                else json.dumps(body).encode("utf-8"))
    if isinstance(data, str):
        data = data.encode("utf-8")

    req = urllib.request.Request(f"{BASE}{path}", data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", ctype)
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        with _opener.open(req, timeout=10) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def test_http():
    # 未登录：页面跳登录页，API 给 401 JSON。两者绝不能混。
    code, hdrs, _ = call("GET", "/")
    assert code == 302, f"未登录访问首页应 302，实际 {code}"
    assert hdrs.get("Location") == "/login", f"跳错地方：{hdrs.get('Location')}"
    assert hdrs.get("Content-Length") == "0", "302 缺 Content-Length，浏览器会干等"
    print("[OK] 闸门     未登录访问 / → 302 /login（带 Content-Length: 0）")

    code, _, body = call("GET", "/api/meta")
    assert code == 401, f"未登录访问 API 应 401，实际 {code}"
    assert b"unauthorized" in body, "401 响应体不是约定的 JSON"
    print("[OK] 闸门     未登录访问 /api/meta → 401 JSON（不是 302）")

    # fetch 会把 302 跟随成 200 的 HTML，前端再拿它当 SSE 流解析，
    # 结果是"空白回合、零报错"。这条断言就是钉死这个回归。
    code, hdrs, _ = call("POST", "/api/chat", {"message": "你好"})
    assert code == 401, f"未登录发消息应 401，实际 {code}"
    assert hdrs.get("Location") is None, \
        "API 竟然重定向了——前端会把登录页 HTML 当 SSE 流，用户看到空白"
    print("[OK] 闸门     POST /api/chat 未登录 → 401，绝不重定向")

    # 健康检查必须免登录
    code, _, body = call("GET", "/healthz")
    assert code == 200 and b'"ok"' in body, f"/healthz 应免认证 200，实际 {code}"
    print("[OK] 健康     /healthz 免登录返回 200")

    # 登录页本身可访问
    code, _, body = call("GET", "/login")
    assert code == 200 and "登录".encode("utf-8") in body, f"/login 打不开：{code}"
    print("[OK] 登录页   /login 可打开")

    # 密码错误
    code, hdrs, _ = call("POST", "/login", {"user": USER, "password": "wrong"},
                         ctype="application/x-www-form-urlencoded")
    assert code == 302 and "e=1" in hdrs.get("Location", ""), "错误密码未被拒绝"
    print("[OK] 登录     错误密码 → 302 /login?e=1")

    # 密码正确（表单编码，验证 _read_body 两种 content-type 都认）
    code, hdrs, _ = call("POST", "/login", {"user": USER, "password": PASS},
                         ctype="application/x-www-form-urlencoded")
    assert code == 302 and hdrs.get("Location") == "/", "登录成功却未跳首页"
    set_cookie = hdrs.get("Set-Cookie", "")
    assert "ma_session=" in set_cookie and "HttpOnly" in set_cookie, \
        f"Cookie 属性不对：{set_cookie}"
    token = server.cookie_value(set_cookie, "ma_session")
    assert server.token_ok(token), "发下来的 token 自己都验不过"
    print("[OK] 登录     表单登录成功，下发 HttpOnly Cookie")

    # 带上 Cookie 就能进
    cookie = f"ma_session={token}"
    code, _, _ = call("GET", "/", cookie=cookie)
    assert code == 200, f"带 Cookie 仍未放行：{code}"
    code, _, body = call("GET", "/api/meta", cookie=cookie)
    assert code == 200 and b"stats" in body, f"带 Cookie 调 API 失败：{code}"
    print("[OK] 会话     带 Cookie 可访问页面与 API")

    # 关键回归：Cookie 头里混着别的垃圾片段仍要认得我们
    messy = f"__cf_bm=xyz; ma_session={token}; junk; =bad"
    code, _, _ = call("GET", "/", cookie=messy)
    assert code == 200, "Cookie 头含垃圾片段时被踢回登录页"
    print("[OK] 会话     含垃圾片段的 Cookie 头仍能通过")

    # 伪造的 token 进不来
    code, _, _ = call("GET", "/api/meta", cookie="ma_session=123.deadbeef")
    assert code == 401, "伪造 token 竟然通过"
    print("[OK] 会话     伪造 token 被拒")

    # 请求体过大
    old_max = server.MAX_BODY
    server.MAX_BODY = 512
    try:
        code, _, _ = call("POST", "/api/chat", {"message": "x" * 4000}, cookie=cookie)
        assert code == 413, f"超大请求体应 413，实际 {code}"
    finally:
        server.MAX_BODY = old_max
    print("[OK] 请求体   超过上限 → 413")

    # 登出
    code, hdrs, _ = call("GET", "/logout", cookie=cookie)
    assert code == 302 and hdrs.get("Location") == "/login", "登出未跳登录页"
    assert "Max-Age=0" in hdrs.get("Set-Cookie", ""), "登出未清除 Cookie"
    print("[OK] 登出     /logout 清 Cookie → /login")


def test_chat_stream():
    """带认证走通一轮 SSE，用假的 run() 替换真实模型调用。"""
    token = server.new_token()
    cookie = f"ma_session={token}"

    real_run = server.agent_mod.run

    def fake_run(session, text):
        assert isinstance(text, str)
        yield {"type": "text", "text": "收到："}
        yield {"type": "text", "text": text}
        yield {"type": "done", "steps": 1}

    server.agent_mod.run = fake_run
    try:
        code, hdrs, body = call("POST", "/api/chat",
                                {"message": "我家老人 82 岁", "mode": "resident"},
                                cookie=cookie)
        assert code == 200, f"聊天应 200，实际 {code}"
        assert "text/event-stream" in hdrs.get("Content-Type", ""), \
            f"Content-Type 不是 SSE：{hdrs.get('Content-Type')}"
        text = body.decode("utf-8")
        assert "收到：" in text and "我家老人 82 岁" in text, f"SSE 内容缺失：{text[:200]}"
        assert '"type": "eos"' in text, "SSE 未以 eos 收尾"
        events = [json.loads(l[5:]) for l in text.splitlines()
                  if l.startswith("data:")]
        assert [e["type"] for e in events][-1] == "eos"
        print(f"[OK] 对话     SSE 正常，{len(events)} 个事件，以 eos 收尾")
    finally:
        server.agent_mod.run = real_run


def test_session_isolation():
    """会话 key 由 token 派生，客户端传的 sid 不再作数。"""
    a, b = server.new_token(), server.new_token()
    assert server.session_key(a) != server.session_key(b), "不同 token 派生出同一 key"
    assert server.session_key(a) == server.session_key(a), "同一 token 派生结果不稳定"
    assert a not in server.session_key(a), "会话 key 里直接包含了凭证本身"
    print("[OK] 会话     key 由 token 派生，且不直接暴露 token")


# ---------------------------------------------------------------- 入口
def main():
    print("=" * 64)
    print("  Web 层检查（离线）")
    print("=" * 64)

    config.REQUIRE_AUTH = True
    config.AUTH_USER, config.AUTH_PASS = USER, PASS
    server.STORE = PolicyStore(config.POLICY_DIR, config.STRUCT_DIR).load()
    # 这一项不需要真实检索索引，留 None 即可（也避免构造 LLM）
    server.RETRIEVER = None

    global BASE
    srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    srv.daemon_threads = True
    BASE = f"http://127.0.0.1:{srv.server_address[1]}"
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    try:
        test_token()
        test_cookie_parse()
        test_credential_chinese()
        test_repair_tail()
        test_run_interrupt()
        test_session_isolation()
        test_http()
        test_chat_stream()
    finally:
        srv.shutdown()
        srv.server_close()

    print("\n全部通过")


BASE = ""

if __name__ == "__main__":
    main()
