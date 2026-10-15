"""全局配置。所有敏感项从环境变量读取，不写死在代码里。"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent

DATA_DIR = ROOT / "data"
POLICY_DIR = DATA_DIR / "policies"      # 抓取到的政策原文（JSON）
STRUCT_DIR = DATA_DIR / "structured"    # LLM 结构化抽取结果（JSON）
CACHE_DIR = DATA_DIR / "cache"

WEB_DIR = ROOT / "web"

# ---------------------------------------------------------------- 模型接入
# 优先用 DEEPSEEK_API_KEY（便于把 Demo 拷到别的机器），
# 回退到 ANTHROPIC_AUTH_TOKEN（Claude Code 当前会话用的那个 key）。
API_KEY = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN") or ""

# DeepSeek 的 Anthropic 兼容端点。若用户已设 ANTHROPIC_BASE_URL 则沿用。
_BASE = os.environ.get("DEEPSEEK_BASE_URL") or os.environ.get("ANTHROPIC_BASE_URL") or ""
if not _BASE:
    _BASE = "https://api.deepseek.com/anthropic"
if not _BASE.rstrip("/").endswith("/anthropic") and "deepseek" in _BASE:
    _BASE = _BASE.rstrip("/") + "/anthropic"
BASE_URL = _BASE.rstrip("/")

# 主模型：负责 Agent 推理
MODEL = os.environ.get("POLICY_AGENT_MODEL", "deepseek-chat")
# 轻量模型：负责检索重排、结构化抽取等
FAST_MODEL = os.environ.get("POLICY_AGENT_FAST_MODEL", MODEL)

TEMPERATURE = float(os.environ.get("POLICY_AGENT_TEMPERATURE", "0.2"))
MAX_TOKENS = int(os.environ.get("POLICY_AGENT_MAX_TOKENS", "4096"))

# Agent 循环上限，防止工具调用死循环
MAX_AGENT_STEPS = int(os.environ.get("POLICY_AGENT_MAX_STEPS", "12"))

# ---------------------------------------------------------------- 检索参数
TOP_K_RETRIEVE = 12      # BM25 初筛条数
TOP_K_RERANK = 5         # 重排后保留条数

# ---------------------------------------------------------------- 服务
HOST = os.environ.get("POLICY_AGENT_HOST", "127.0.0.1")

# 云平台（Render、Heroku 等）用 $PORT 指定监听端口，忽略它就等于没监听。
PORT = int(os.environ.get("POLICY_AGENT_PORT") or os.environ.get("PORT") or "8848")

# 本 Demo 的试点地域。政策路由按这个做过滤。
DEFAULT_REGION = os.environ.get("POLICY_AGENT_REGION", "")

# ---------------------------------------------------------------- 访问认证
# 评测账号。空 = 未启用认证。
AUTH_USER = os.environ.get("POLICY_AGENT_USER", "")
AUTH_PASS = os.environ.get("POLICY_AGENT_PASS", "")

# token 签名密钥。不设则每次启动随机生成——重启后所有人重新登录，
# 竞赛场景可接受，但部署时应当设成一个固定值。
SESSION_SECRET = os.environ.get("POLICY_AGENT_SECRET", "")
SECRET_IS_RANDOM = not SESSION_SECRET

# 登录有效期（天）。评审期较长，给足余量。
SESSION_DAYS = int(os.environ.get("POLICY_AGENT_SESSION_DAYS", "30"))

# 会话数上限与空闲回收（分钟）。key 由登录 token 派生，正常不会逼近上限；
# 这是防「链接扩散后被脚本刷」的兜底。
MAX_SESSIONS = int(os.environ.get("POLICY_AGENT_MAX_SESSIONS", "32"))
SESSION_IDLE_MINUTES = int(os.environ.get("POLICY_AGENT_SESSION_IDLE_MINUTES", "120"))

# 演示额度：防公开链接被刷穿 API 余额。
RATE_PER_MINUTE = int(os.environ.get("POLICY_AGENT_RATE_PER_MINUTE", "10"))
RATE_PER_DAY = int(os.environ.get("POLICY_AGENT_RATE_PER_DAY", "1000"))


def _is_loopback(host: str) -> bool:
    h = (host or "").strip().lower()
    return h in ("localhost", "::1", "[::1]") or h.startswith("127.")


# 绑回环地址 = 只服务本机，双击 run.py 就能用的体验保持不变，不强制认证。
# 绑 0.0.0.0 等对外地址 = 已经暴露到公网，**必须**认证，否则任何人
# 都能拿你的 API Key 刷余额。
REQUIRE_AUTH = not _is_loopback(HOST)


def ensure_dirs() -> None:
    for d in (DATA_DIR, POLICY_DIR, STRUCT_DIR, CACHE_DIR):
        d.mkdir(parents=True, exist_ok=True)


def auth_enabled() -> bool:
    """账号密码是否已配置齐全。"""
    return bool(AUTH_USER and AUTH_PASS)


def auth_problem() -> str:
    """认证放不上，返回给用户看的原因；没问题返回空串。"""
    if not REQUIRE_AUTH:
        return ""
    if not auth_enabled():
        return (
            f"监听地址是 {HOST}（对外），必须设置评测账号，否则任何人都能调用你的 API。\n"
            "    请设置环境变量 POLICY_AGENT_USER 和 POLICY_AGENT_PASS。\n"
            "    PowerShell:  $env:POLICY_AGENT_USER = \"judge\"; "
            "$env:POLICY_AGENT_PASS = \"你的密码\""
        )
    return ""


def describe() -> str:
    """脱敏后的配置摘要，用于自检输出。"""
    key = API_KEY
    shown = f"{key[:6]}...{key[-4:]}" if len(key) > 12 else ("(未设置)" if not key else "(过短)")
    if not REQUIRE_AUTH:
        auth = "未启用（仅本机可访问）"
    elif auth_enabled():
        auth = f"已启用，账号 {AUTH_USER}"
    else:
        auth = "✗ 未配置，服务无法启动"
    return (
        f"  API 端点 : {BASE_URL}\n"
        f"  API Key  : {shown}\n"
        f"  主模型   : {MODEL}\n"
        f"  轻量模型 : {FAST_MODEL}\n"
        f"  数据目录 : {DATA_DIR}\n"
        f"  监听地址 : {HOST}:{PORT}\n"
        f"  访问认证 : {auth}\n"
    )
