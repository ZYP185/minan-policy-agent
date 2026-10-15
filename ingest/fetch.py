"""HTTP 抓取（含礼节性限速与重试）。仅标准库。"""
from __future__ import annotations

import gzip
import ssl
import time
import urllib.error
import urllib.request
import zlib

# 对政府网站保持克制：同一域名两次请求之间至少间隔这么久
DOMAIN_INTERVAL = 1.5
MAX_RETRIES = 3

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")

_ctx = ssl.create_default_context()
_last_hit: dict[str, float] = {}


class FetchError(RuntimeError):
    pass


def _throttle(url: str) -> None:
    from urllib.parse import urlparse
    host = urlparse(url).netloc
    now = time.time()
    wait = DOMAIN_INTERVAL - (now - _last_hit.get(host, 0.0))
    if wait > 0:
        time.sleep(wait)
    _last_hit[host] = time.time()


def get(url: str, timeout: int = 25, referer: str = "") -> tuple[bytes, str, str]:
    """返回 (原始字节, Content-Type, 最终URL)。"""
    last: Exception | None = None

    for attempt in range(MAX_RETRIES):
        _throttle(url)
        headers = {
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "close",
        }
        if referer:
            headers["Referer"] = referer

        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=_ctx) as r:
                raw = _decompress(r.read(), r.headers.get("Content-Encoding", ""))
                return raw, r.headers.get("Content-Type", ""), r.geturl()
        except urllib.error.HTTPError as e:
            last = FetchError(f"HTTP {e.code}")
            if e.code in (403, 404, 410):
                break                      # 重试也没用
        except urllib.error.URLError as e:
            last = FetchError(f"连接失败：{e.reason}")
        except TimeoutError:
            last = FetchError("超时")
        except Exception as e:
            last = FetchError(f"{type(e).__name__}: {e}")

        if attempt < MAX_RETRIES - 1:
            time.sleep(1.0 * (2 ** attempt))

    raise last or FetchError("未知错误")


def _decompress(raw: bytes, encoding: str) -> bytes:
    enc = (encoding or "").lower()
    try:
        if "gzip" in enc:
            return gzip.decompress(raw)
        if "deflate" in enc:
            try:
                return zlib.decompress(raw)
            except zlib.error:
                return zlib.decompress(raw, -zlib.MAX_WBITS)
    except (OSError, zlib.error):
        return raw                            # 解压失败就用原始字节
    return raw
