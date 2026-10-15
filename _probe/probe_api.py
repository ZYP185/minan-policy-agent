"""探测 DeepSeek 端点能力：模型列表 / 基础对话 / 工具调用 / 流式。
仅使用标准库，不依赖任何第三方包。
"""
import json
import os
import urllib.request
import urllib.error

KEY = os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get("DEEPSEEK_API_KEY")
if not KEY:
    raise SystemExit("没有找到 ANTHROPIC_AUTH_TOKEN / DEEPSEEK_API_KEY")

OPENAI_BASE = "https://api.deepseek.com/v1"
ANTHROPIC_BASE = "https://api.deepseek.com/anthropic"


def post(url, payload, headers):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        return e.code, body


def get(url, headers):
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


print("=" * 60)
print("[1] 模型列表 (OpenAI 兼容端点)")
print("=" * 60)
st, body = get(f"{OPENAI_BASE}/models", {"Authorization": f"Bearer {KEY}"})
print("status:", st)
if isinstance(body, dict):
    for m in body.get("data", []):
        print("  -", m.get("id"))
else:
    print(body[:500])

print()
print("=" * 60)
print("[2] 基础对话 (Anthropic 兼容端点)")
print("=" * 60)
st, body = post(
    f"{ANTHROPIC_BASE}/v1/messages",
    {
        "model": "deepseek-chat",
        "max_tokens": 100,
        "messages": [{"role": "user", "content": "只回复两个字：可用"}],
    },
    {"x-api-key": KEY, "anthropic-version": "2023-06-01"},
)
print("status:", st)
if isinstance(body, dict):
    for blk in body.get("content", []):
        if blk.get("type") == "text":
            print("  text:", blk["text"])
    print("  usage:", body.get("usage"))
else:
    print(body[:800])

print()
print("=" * 60)
print("[3] 工具调用 (Anthropic 兼容端点) —— Agent 关键能力")
print("=" * 60)
TOOLS = [
    {
        "name": "search_policy",
        "description": "检索政策条款。根据居民的问题，返回匹配的政策原文条款。",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "检索关键词或问题"},
                "region": {"type": "string", "description": "地域，如 阳光区"},
            },
            "required": ["query"],
        },
    }
]
st, body = post(
    f"{ANTHROPIC_BASE}/v1/messages",
    {
        "model": "deepseek-chat",
        "max_tokens": 500,
        "tools": TOOLS,
        "tool_choice": {"type": "any"},
        "messages": [
            {"role": "user", "content": "我家孩子刚出生，能领什么补贴？请先检索政策。"}
        ],
    },
    {"x-api-key": KEY, "anthropic-version": "2023-06-01"},
)
print("status:", st)
if isinstance(body, dict):
    print("  stop_reason:", body.get("stop_reason"))
    for blk in body.get("content", []):
        if blk.get("type") == "tool_use":
            print("  [OK] tool_use:", blk["name"], json.dumps(blk["input"], ensure_ascii=False))
        elif blk.get("type") == "text":
            print("  text:", blk["text"][:200])
else:
    print(body[:800])

print()
print("=" * 60)
print("[4] 流式 (Anthropic 兼容端点)")
print("=" * 60)
req = urllib.request.Request(
    f"{ANTHROPIC_BASE}/v1/messages",
    data=json.dumps(
        {
            "model": "deepseek-chat",
            "max_tokens": 80,
            "stream": True,
            "messages": [{"role": "user", "content": "数到五，用顿号分隔。"}],
        }
    ).encode("utf-8"),
    headers={
        "Content-Type": "application/json",
        "x-api-key": KEY,
        "anthropic-version": "2023-06-01",
    },
    method="POST",
)
try:
    with urllib.request.urlopen(req, timeout=60) as r:
        print("status:", r.status)
        got = 0
        for raw in r:
            line = raw.decode("utf-8", "replace").strip()
            if line.startswith("data:"):
                got += 1
                if got <= 3:
                    print("  chunk:", line[:120])
        print(f"  [OK] 收到 {got} 个 SSE 数据块")
except Exception as e:
    print("  [FAIL]", type(e).__name__, e)
