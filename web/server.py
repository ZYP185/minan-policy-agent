"""Web 服务（标准库 http.server + SSE + 登录认证）。零第三方依赖。"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import config
from core import agent as agent_mod
from core.retriever import Retriever
from core.store import PolicyStore

STORE: PolicyStore | None = None
RETRIEVER: Retriever | None = None
SESSIONS: dict[str, agent_mod.Session] = {}
_LOCK = threading.Lock()

COOKIE_NAME = "ma_session"
MAX_BODY = 1 << 20                    # 请求体上限 1MB


# ---------------------------------------------------------------- 访问认证
# 无状态签名 token：token = "<签发时间戳>.<HMAC 前 32 位>"。
# 不用服务端 session 表，换来的是重启不掉线、也不必写 TTL 清理。
#
# 必须统一成 bytes 再交给 hmac：环境变量读出来是 str，随机生成是 bytes，
# 两条路径类型不一致的话，只有「设了 POLICY_AGENT_SECRET」的那条会崩。
# 那个组合恰好是线上（Render 的 generateValue）——本地不设密钥，
# 永远走不到坏的那条，登录看着一切正常，一部署就全员登录失败。
_SESSION_SECRET = (
    config.SESSION_SECRET.encode("utf-8")
    if config.SESSION_SECRET
    else secrets.token_bytes(32)
)
_LAST_SEEN: dict[str, float] = {}     # sid → 最后活跃时间，用于空闲回收
_RATE_HIT: dict[str, list[float]] = {}  # sid → 最近调用时刻，用于每分钟限流
_RATE_DAY = {"day": "", "count": 0}    # 全局每日额度

# 登录失败要 sleep，而 sleep 是占着线程的。`/login` 又是唯一免认证的
# 端点，不设上限的话猛刷它就能把线程耗光。宁可拒掉超出部分的尝试。
_LOGIN_SLOTS = threading.BoundedSemaphore(8)


def _sign(msg: str) -> str:
    return hmac.new(_SESSION_SECRET, msg.encode("ascii"), hashlib.sha256).hexdigest()[:32]


def new_token() -> str:
    """签发 token：`<时间戳>.<随机数>.<签名>`。

    随机数不能省。签名若只由时间戳决定，同一秒内签发的 token 完全相同，
    于是同一秒登录的两位评委会共用同一个会话、对话互相串台。
    """
    ts = str(int(time.time()))
    nonce = secrets.token_hex(8)
    return f"{ts}.{nonce}.{_sign(f'{ts}:{nonce}')}"


def token_ok(token: str | None) -> bool:
    if not token:
        return False
    parts = token.split(".")
    if len(parts) != 3:
        return False
    ts, nonce, sig = parts
    if not ts.isdigit() or not nonce:
        return False
    if not hmac.compare_digest(_sign(f"{ts}:{nonce}"), sig):   # 两侧都是纯 ASCII
        return False
    return time.time() - int(ts) <= config.SESSION_DAYS * 86400


def credential_ok(user: str, password: str) -> bool:
    """比对账号密码。

    **必须 encode 后再比**：hmac.compare_digest 对含非 ASCII 字符的 str
    会抛 TypeError: comparing strings with non-ASCII characters。
    这是个中文项目，密码里出现汉字是常态，不 encode 就是登录即 500。
    """
    return hmac.compare_digest(
        user.encode("utf-8"), config.AUTH_USER.encode("utf-8")
    ) and hmac.compare_digest(
        password.encode("utf-8"), config.AUTH_PASS.encode("utf-8")
    )


def cookie_value(header: str | None, name: str) -> str | None:
    """手工解析 Cookie 头，不用 http.cookies.SimpleCookie。

    实测：Cookie 头里只要出现任何一个没有 "=" 的片段或空键，
    SimpleCookie.load() 会内部抛 CookieError 并**整体丢弃**，
    我们的 ma_session 被静默吃掉。而这个头里装着同源的其他 cookie，
    根本不受我们控制——表现就是用户莫名其妙被踢回登录页且无从排查。
    """
    if not header:
        return None
    for seg in header.split(";"):
        k, _, v = seg.strip().partition("=")
        if k.strip() == name and v:
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] == '"':
                v = v[1:-1]
            return v or None
    return None


def rate_limited(sid: str) -> str:
    """检查演示额度。返回空串表示放行，否则返回给用户看的原因。

    提交给评委的链接和密码必然会扩散，这是防止 API 余额被打穿的唯一防线。
    """
    now = time.time()
    with _LOCK:
        # 全局每日
        today = time.strftime("%Y-%m-%d")
        if _RATE_DAY["day"] != today:
            _RATE_DAY["day"] = today
            _RATE_DAY["count"] = 0
        if _RATE_DAY["count"] >= config.RATE_PER_DAY:
            return "今日演示额度已用完，请明天再试。"

        # 单会话每分钟
        hits = [t for t in _RATE_HIT.get(sid, []) if now - t < 60]
        if len(hits) >= config.RATE_PER_MINUTE:
            return "提问太快了，请等一分钟再发。"
        hits.append(now)
        _RATE_HIT[sid] = hits
        _RATE_DAY["count"] += 1
    return ""


def boot() -> None:
    global STORE, RETRIEVER
    print("加载政策库…", flush=True)
    STORE = PolicyStore(config.POLICY_DIR, config.STRUCT_DIR).load()
    s = STORE.stats()
    print(f"  {s['政策数']} 份政策 / {s['条款数']} 条条款 / 地域 {s['地域'] or '(未标注)'}")
    if s["政策数"] == 0:
        print("\n  ⚠ 政策库为空。请先运行：")
        print("      python -m ingest.build --urls urls.txt --structure\n")
    print("构建检索索引…", flush=True)
    from core.llm import LLM
    try:
        llm = LLM()
    except Exception as e:
        print(f"  ⚠ LLM 不可用（{e}），检索重排将关闭")
        llm = None
    RETRIEVER = Retriever(STORE, llm=llm)
    print(f"  索引就绪：{len(RETRIEVER._pool)} 条条款\n", flush=True)


def session_key(token: str) -> str:
    """由登录 token 派生会话 key。

    不再信任客户端传来的 sid——它是任意值，循环换就能把 SESSIONS 撑爆。
    也不直接用 token 本体：那样日志里任何一处意外打印都会同时泄露凭证。
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def get_session(sid: str, mode: str = "resident") -> agent_mod.Session:
    with _LOCK:
        _LAST_SEEN[sid] = time.time()
        _evict()
        s = SESSIONS.get(sid)
        if s is None or s.mode != mode:
            s = agent_mod.Session(STORE, RETRIEVER, mode=mode)
            SESSIONS[sid] = s
        return s


def _evict() -> None:
    """回收空闲会话并限制总数。调用方必须持有 _LOCK。

    每个 Session 都攥着完整消息历史且不会自己释放，不回收就是只涨不降。
    """
    cutoff = time.time() - config.SESSION_IDLE_MINUTES * 60
    for k in [k for k, t in _LAST_SEEN.items() if t < cutoff]:
        SESSIONS.pop(k, None)
        _LAST_SEEN.pop(k, None)
        _RATE_HIT.pop(k, None)
    while len(SESSIONS) > config.MAX_SESSIONS:
        oldest = min(_LAST_SEEN, key=_LAST_SEEN.get, default=None)
        if oldest is None:
            break
        SESSIONS.pop(oldest, None)
        _LAST_SEEN.pop(oldest, None)
        _RATE_HIT.pop(oldest, None)


class Handler(BaseHTTPRequestHandler):
    server_version = "PolicyAgent/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if "/api/chat" in self.path:
            return                       # 聊天请求刷屏，静音
        sys.stderr.write("  %s\n" % (fmt % args))

    # ------------------------------------------------------------ 认证
    def _token(self) -> str | None:
        return cookie_value(self.headers.get("Cookie"), COOKIE_NAME)

    def _authed(self) -> bool:
        """未启用认证时一律放行（只在绑定回环地址时成立）。"""
        return True if not config.REQUIRE_AUTH else token_ok(self._token())

    def _sid(self) -> str:
        """会话 key 由服务端派生，忽略客户端传来的 sid。"""
        token = self._token()
        return session_key(token) if token else "local"

    def _gate_api(self) -> bool:
        """API 认证闸门。**永远 401 JSON，绝不重定向。**

        302 会被 fetch 自动跟随、并把方法改成 GET，于是前端拿到 200 的
        登录页 HTML、判定 resp.ok 为真，然后把整页 HTML 当 SSE 流去解析——
        找不到 data: 行，用户看到一个完全空白的回合，且没有任何报错。
        """
        if self._authed():
            return True
        self._json({"error": "未登录或登录已过期", "code": "unauthorized"}, 401)
        return False

    def _gate_page(self) -> bool:
        if self._authed():
            return True
        self._redirect("/login")
        return False

    def _cookie_header(self, token: str) -> str:
        parts = [f"{COOKIE_NAME}={token}", "Path=/", "HttpOnly", "SameSite=Lax",
                 f"Max-Age={config.SESSION_DAYS * 86400}"]
        # 只在确实走 HTTPS 时才加 Secure：一旦错判成 https，
        # 浏览器会直接丢弃这个 cookie，表现为登录成功却反复弹回登录页。
        if self._is_https():
            parts.append("Secure")
        return "; ".join(parts)

    def _is_https(self) -> bool:
        """是否经 HTTPS 访问。由反向代理声明。

        伪造成 https 只会让攻击者自己的 cookie 带 Secure 而收不到，
        伤不到别人，所以这里直接采信。
        """
        proto = self.headers.get("X-Forwarded-Proto", "")
        return proto.split(",")[0].strip().lower() == "https"

    def _redirect(self, location: str, clear_cookie: bool = False):
        """重定向。只发相对路径，顺手堵掉 Host 头注入。"""
        self.send_response(302)
        self.send_header("Location", location)      # 相对路径，不用 Host 拼绝对 URL
        if clear_cookie:
            self.send_header(
                "Set-Cookie",
                f"{COOKIE_NAME}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax",
            )
        # 这行不能省：没有 Content-Length 也没有 Connection: close 的 302
        # 要靠关闭连接来定界，浏览器会干等到超时，表现为登录跳转卡住。
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _login(self, body: dict):
        user = str(body.get("user") or "")
        password = str(body.get("password") or "")
        if not credential_ok(user, password):
            # 固定延迟拖慢枚举。**刻意不做按 IP 锁定**：反向代理后面
            # client_address 永远是网关地址，IP 锁会退化成全局锁——
            # 随便谁试几次就能把评委一起锁在门外。
            if not _LOGIN_SLOTS.acquire(blocking=False):
                return self._redirect("/login?e=1")   # 挤不进去就当作失败，不排队占线程
            try:
                time.sleep(0.75)
            finally:
                _LOGIN_SLOTS.release()
            return self._redirect("/login?e=1")
        self.send_response(302)
        self.send_header("Location", "/")
        self.send_header("Set-Cookie", self._cookie_header(new_token()))
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    # ------------------------------------------------------------ 请求体
    def _read_body(self) -> dict | None:
        """读取并解析请求体。返回 None 表示已回过错误，调用方直接 return。

        **必须在任何响应之前读完**：HTTP/1.1 是 keep-alive，没读干净的
        body 会被当成下一个请求行去解析，客户端侧表现为莫名其妙的错误。
        """
        if self.headers.get("Transfer-Encoding"):
            # 没有 Content-Length 就无从知道边界，残留字节会串到下一个请求
            self.close_connection = True
            self._json({"error": "不支持分块传输的请求体"}, 411)
            return None

        raw = self.headers.get("Content-Length")
        try:
            n = int(raw) if raw else 0
        except (TypeError, ValueError):
            self.close_connection = True
            self._json({"error": "Content-Length 非法"}, 400)
            return None
        if n < 0:
            self.close_connection = True
            self._json({"error": "Content-Length 非法"}, 400)
            return None
        if n > MAX_BODY:
            # 不读了直接断开：把 1MB 垃圾读进内存没有任何意义
            self.close_connection = True
            self._json({"error": "请求体过大"}, 413)
            return None
        if not n:
            return {}

        data = self.rfile.read(n)
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        # 登录页是普通表单提交（urlencoded），API 走 JSON。两种都认，
        # 好处是登录不依赖 JS——登录页 JS 出错也照样进得去门。
        if ctype == "application/x-www-form-urlencoded":
            try:
                pairs = parse_qs(data.decode("utf-8"), keep_blank_values=True)
            except UnicodeDecodeError:
                return {}
            return {k: v[0] for k, v in pairs.items() if v}
        try:
            return json.loads(data.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    # ------------------------------------------------------------ 路由
    def do_GET(self):
        u = urlparse(self.path)
        path = u.path
        try:
            # 平台健康检查必须免认证：Render 默认探 /，探到 302 会判不健康
            if path == "/healthz":
                return self._json({"ok": True, "auth_required": config.REQUIRE_AUTH})
            if path == "/favicon.ico":
                return self._send(204, b"", "image/x-icon")
            if path == "/logout":
                return self._redirect("/login", clear_cookie=True)
            if path == "/login":
                if self._authed():
                    return self._redirect("/")
                return self._file(config.WEB_DIR / "login.html", "text/html; charset=utf-8")

            if path.startswith("/api/"):
                if not self._gate_api():
                    return
                if path == "/api/meta":
                    return self._json(self._meta())
                if path == "/api/policies":
                    return self._json(self._policy_list(parse_qs(u.query)))
                if path.startswith("/api/policy/"):
                    return self._json(self._policy_detail(path.rsplit("/", 1)[-1]))
                return self._send(404, b"404", "text/plain")

            if not self._gate_page():
                return
            if path in ("/", "/index.html"):
                return self._file(config.WEB_DIR / "index.html", "text/html; charset=utf-8")
            return self._send(404, b"404", "text/plain")
        except (BrokenPipeError, ConnectionResetError):
            return

    def do_POST(self):
        u = urlparse(self.path)
        # 先读完 body 再路由，保持 keep-alive 连接不错位
        body = self._read_body()
        if body is None:
            return
        try:
            if u.path == "/login":
                return self._login(body)
            if u.path.startswith("/api/"):
                if not self._gate_api():
                    return
                if u.path == "/api/chat":
                    return self._chat(body)
                if u.path == "/api/reset":
                    get_session(self._sid(), body.get("mode") or "resident").reset()
                    return self._json({"ok": True})
            return self._send(404, b"404", "text/plain")
        except (BrokenPipeError, ConnectionResetError):
            return

    # ------------------------------------------------------------ 聊天（SSE）
    def _chat(self, body: dict):
        sid = self._sid()
        mode = body.get("mode") or "resident"
        text = (body.get("message") or "").strip()
        if not text:
            return self._json({"error": "message 不能为空"}, 400)

        limited = rate_limited(sid)
        if limited:
            return self._json({"error": limited}, 429)

        try:
            session = get_session(sid, mode)
        except Exception as e:
            # Session 构造时会 new 一个 LLM，缺 API Key 就抛异常。
            # 接不住的话异常冒泡出 do_POST，而 BaseHTTPRequestHandler 不捕获，
            # 结果是连接被裸关、没有任何 HTTP 响应，前端只看到 Failed to fetch——
            # 平台环境变量少写一个字母就是这个现象，极难排查。
            traceback.print_exc()
            return self._json({"error": f"服务端未就绪：{e}"}, 503)

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        gen = agent_mod.run(session, text)
        try:
            for ev in gen:
                self._sse(ev)
        except (BrokenPipeError, ConnectionResetError):
            return                        # 浏览器提前关掉了，正常
        except Exception as e:
            traceback.print_exc()
            try:
                self._sse({"type": "error", "message": f"{type(e).__name__}: {e}"})
            except (BrokenPipeError, ConnectionResetError):
                return
        finally:
            # 客户端断连时生成器只是被 GC 掉，正在跑的 LLM 请求不会被取消。
            # close() 把 GeneratorExit 送进去，让 run() 的 finally 完成收尾
            # 修复并释放会话锁。已经跑完的生成器再 close() 是空操作。
            try:
                gen.close()
            except Exception:
                pass
        try:
            self._sse({"type": "eos"})
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _sse(self, obj: dict) -> None:
        data = json.dumps(obj, ensure_ascii=False, default=str)
        self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
        self.wfile.flush()

    # ------------------------------------------------------------ 数据接口
    def _meta(self) -> dict:
        s = STORE.stats()
        return {
            "stats": s,
            "policies": [
                {
                    "id": p.id, "title": p.title, "category": p.category,
                    "region": p.region, "issuer": p.issuer, "status": p.status_on(),
                    "structured": bool(p.structured), "clauses": len(p.clauses),
                    "url": p.url,
                }
                for p in STORE.policies.values()
            ],
            "suggestions": _suggestions(),
            "worker_suggestions": [
                "扫描一下我辖区的政策覆盖情况",
                "哪些民生政策需要居民主动申请，容易被漏掉？",
                "临时救助的适用对象和标准是什么？",
            ],
        }

    def _policy_list(self, q: dict) -> dict:
        region = (q.get("region") or [""])[0]
        category = (q.get("category") or [""])[0]
        items = []
        for p in STORE.filter(region=region or None, category=category or None, valid_only=False):
            items.append({
                "id": p.id, "title": p.title, "doc_number": p.doc_number,
                "issuer": p.issuer, "region": p.region, "category": p.category,
                "status": p.status_on(), "publish_date": p.publish_date,
                "effective_date": p.effective_date, "expiry_date": p.expiry_date,
                "url": p.url, "clauses": len(p.clauses),
            })
        return {"count": len(items), "items": items}

    def _policy_detail(self, pid: str) -> dict:
        p = STORE.get(pid)
        if not p:
            return {"error": f"找不到政策 {pid}"}
        return {
            "id": p.id, "title": p.title, "doc_number": p.doc_number,
            "issuer": p.issuer, "region": p.region, "category": p.category,
            "status": p.status_on(), "publish_date": p.publish_date,
            "effective_date": p.effective_date, "expiry_date": p.expiry_date,
            "url": p.url,
            "clauses": [{"id": c.id, "no": c.no, "text": c.text} for c in p.clauses],
            "structured": p.structured,
            "full_text": p.full_text,
        }

    # ------------------------------------------------------------ 工具方法
    def _json(self, obj, code: int = 200):
        data = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        return self._send(code, data, "application/json; charset=utf-8")

    def _send(self, code: int, data: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        # 置 close_connection 只是让 handler 事后断开，并不会生成这个头
        # （只有反方向才会：send_header 收到 close 会顺手置位）。
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)

    def _file(self, path: Path, ctype: str):
        if not path.exists():
            return self._send(404, b"file not found", "text/plain")
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)


def _suggestions() -> list[str]:
    """根据库里实际有的政策类别，生成演示用的问题。"""
    cats = set(STORE.categories()) if STORE else set()
    pool = [
        ("养老", "我家老人 82 岁了，能领什么补贴？"),
        ("生育", "我孩子刚出生，能领什么钱？"),
        ("就业", "我最近失业了，有什么补助可以申请？"),
        ("救助", "我家收入不高，能申请低保或者临时救助吗？"),
        ("残疾人", "家里有残疾人，有什么补贴政策？"),
        ("医疗", "大病住院花了很多钱，医保能报销多少？"),
        ("住房", "我在本市没房，能申请住房保障吗？"),
    ]
    hits = [q for c, q in pool if c in cats]
    general = ["我这种情况能享受哪些民生政策？", "外地户口在本地能享受哪些政策？"]
    return (hits + general)[:5]


def main() -> int:
    problem = config.auth_problem()
    if problem:
        print("\n  ✗ 拒绝启动：对外监听却没有设置评测账号\n")
        print(f"    {problem}\n")
        return 1

    config.ensure_dirs()
    t0 = time.time()
    boot()
    print(f"  启动耗时 {time.time() - t0:.1f}s\n", flush=True)

    srv = ThreadingHTTPServer((config.HOST, config.PORT), Handler)
    srv.daemon_threads = True
    srv.request_queue_size = 64     # 默认只有 5，几个人同时打开就会被拒连

    print("=" * 64)
    print("  民安 · 社区民生政策服务智能体")
    print(f"  监听：{config.HOST}:{config.PORT}")
    if config.REQUIRE_AUTH:
        print(f"  登录账号：{config.AUTH_USER}（密码取自 POLICY_AGENT_PASS）")
        print("  健康检查：/healthz（免登录）")
        if config.SECRET_IS_RANDOM:
            print("  ⚠ 未设置 POLICY_AGENT_SECRET，重启后所有登录都会失效")
    else:
        host = "127.0.0.1" if config.HOST in ("0.0.0.0", "::") else config.HOST
        print(f"  打开浏览器访问：http://{host}:{config.PORT}/")
        print("  仅本机可访问，未启用登录")
    print("  按 Ctrl+C 停止")
    print("=" * 64)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
