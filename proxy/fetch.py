"""上流への取得（HTTP/CONNECT のブリッジ／SSRF ガード／Cookie 注入）"""
from __future__ import annotations

import asyncio
import gzip
import ipaddress
import socket
import zlib
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlsplit

import httpx

from .cache import CACHE, SOFT_FAIL_PATHS, cache_key, cache_ttl_for
from .config import CFG
from .cookies import CookieJar

# プロキシ経由の埋め込みを壊すヘッダ（上流から素通ししてはいけない）
#   x-frame-options: DENY / CSP(frame-ancestors) は iframe を描画拒否させる
#   COOP/COEP/CORP は別オリジン扱いになるため落とす
STRIP_FROM_UPSTREAM = {
    "x-frame-options",
    "content-security-policy",
    "content-security-policy-report-only",
    "cross-origin-opener-policy",
    "cross-origin-embedder-policy",
    "cross-origin-resource-policy",
    "permissions-policy",
    "feature-policy",
    "report-to",
    "reporting-endpoints",
    "nel",
    "origin-agent-cluster",
    "clear-site-data",
    "alt-svc",
    "public-key-pins",
}

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade", "host", "content-length",
    "accept-encoding",  # 透過させず自前で交渉する
}


def _decompress(body: bytes, encoding: str) -> bytes:
    """accept-encoding で広告した方式だけを自前で解凍する（br 未対応環境対策）"""
    enc = (encoding or "").lower().strip()
    if not enc or enc == "identity" or not body:
        return body
    try:
        if enc == "gzip":
            return gzip.decompress(body)
        if enc == "deflate":
            try:
                return zlib.decompress(body)
            except zlib.error:
                return zlib.decompress(body, -zlib.MAX_WBITS)
        if enc in ("br", "zstd"):
            mod = __import__({"br": "brotli", "zstd": "zstandard"}[enc])
            if enc == "br":
                return mod.decompress(body)
            return mod.ZstdDecompressor().decompress(body)
    except Exception:
        return body
    return body


class UpstreamError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class UpstreamResponse:
    status: int
    reason: str
    headers: list[tuple[str, str]] = field(default_factory=list)
    body: bytes = b""
    url: str = ""
    elapsed_ms: int = 0
    set_cookies: list[str] = field(default_factory=list)

    def header(self, name: str) -> str:
        for k, v in self.headers:
            if k.lower() == name.lower():
                return v
        return ""


# ---------------------------------------------------------------------------
# SSRF ガード
# ---------------------------------------------------------------------------
def assert_safe_target(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise UpstreamError(400, "unsupported scheme")
    host = parts.hostname or ""
    if not host:
        raise UpstreamError(400, "missing host")
    if not CFG.host_allowed(host):
        raise UpstreamError(403, "host not allowed: " + host)
    if CFG.allow_private:
        return
    try:
        infos = socket.getaddrinfo(host, parts.port or (443 if parts.scheme == "https" else 80))
    except socket.gaierror as exc:
        raise UpstreamError(502, f"dns failure: {exc}") from exc
    seen: set[str] = set()
    for info in infos:
        ip = info[4][0]
        if ip in seen:
            continue
        seen.add(ip)
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if (
            addr.is_private or addr.is_loopback or addr.is_link_local
            or addr.is_reserved or addr.is_multicast or addr.is_unspecified
        ):
            raise UpstreamError(403, f"private address blocked: {ip}")


# ---------------------------------------------------------------------------
# ヘッダ整形
# ---------------------------------------------------------------------------
def build_upstream_headers(
    incoming: list[tuple[str, str]], url: str, jar: CookieJar | None
) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in incoming:
        lk = k.lower()
        if lk in HOP_BY_HOP or lk.startswith("proxy-") or lk.startswith("x-forwarded-"):
            continue
        out[lk] = v

    # --- UA / Accept-Language を強制（実物の normalizeHeadersList 相当）---
    ua = out.get("user-agent", "")
    if not ua:
        out["user-agent"] = CFG.user_agent
    else:
        out["user-agent"] = ua.replace("HeadlessChrome/", "Chrome/")
    if "accept-language" not in out:
        out["accept-language"] = CFG.accept_language

    # --- Cookie 注入（クライアント由来は捨ててジャーを使う）---
    if jar is not None:
        cookie = jar.header_for(url)
        if cookie:
            out["cookie"] = cookie
        else:
            out.pop("cookie", None)

    # --- メディア系は Referer を付ける（CDN によっては必須）---
    if CFG.is_media_host(urlsplit(url).hostname or "") and "referer" not in out:
        out["referer"] = CFG.referer_for_media

    return out


# ---------------------------------------------------------------------------
# 実行
# ---------------------------------------------------------------------------
async def fetch_upstream(
    client: httpx.AsyncClient,
    url: str,
    method: str = "GET",
    headers: list[tuple[str, str]] | None = None,
    body: bytes | None = None,
    jar: CookieJar | None = None,
    follow_redirects: bool = False,
) -> UpstreamResponse:
    """上流取得。429/5xx は指数バックオフで再試行し、対象 URL は TTL キャッシュする。"""
    assert_safe_target(url)
    cookie_header = jar.header_for(url) if jar is not None else ""
    ttl = cache_ttl_for(url, method.upper())
    key = cache_key(url, method.upper(), cookie_header, body or b"") if ttl else ""

    # 1) キャッシュヒット
    if key:
        hit = CACHE.get(key)
        if hit is not None:
            return hit

    async def do_request() -> UpstreamResponse:
        last: Optional[Exception] = None
        for attempt in range(CFG.max_retries + 1):
            try:
                resp = await _once(client, url, method, headers, body, jar, follow_redirects)
            except UpstreamError as exc:
                last = exc
                # 429 はリトライすると悪化するので即座に返す（上位でフォールバック）
                if attempt >= CFG.max_retries or exc.status not in (502, 503, 504):
                    raise
                await asyncio.sleep(min(0.6 * (2 ** attempt), 4.0))
                continue
            if resp.status in (502, 503, 504) and attempt < CFG.max_retries:
                await asyncio.sleep(min(0.6 * (2 ** attempt), 4.0))
                continue
            return resp
        if last:
            raise last  # pragma: no cover
        raise UpstreamError(502, "upstream failed")  # pragma: no cover

    if key:
        # 同一 URL の多重実行を 1 本化
        resp = await CACHE.run(key, do_request)
        if resp.status == 200:
            CACHE.set(key, resp, ttl)
        elif resp.status in (429, 502, 503, 504):
            stale = CACHE.get(key, allow_stale=True)
            if stale is not None:
                return stale          # 上流が落ちてもキャッシュで継続
        return resp

    resp = await do_request()

    # 429 で SPA が止まるのを避ける（route definitions が無くても描画は続く）
    if resp.status == 429:
        for needle, payload, ctype in SOFT_FAIL_PATHS:
            if needle in url:
                return UpstreamResponse(
                    status=200,
                    reason="OK",
                    headers=[("content-type", ctype), ("x-proxy-softfail", "ratelimited")],
                    body=payload,
                    set_cookies=[],
                    url=url,
                )
    return resp


async def _once(
    client: httpx.AsyncClient,
    url: str,
    method: str,
    headers: list[tuple[str, str]] | None,
    body: bytes | None,
    jar: CookieJar | None,
    follow_redirects: bool,
) -> UpstreamResponse:
    up_headers = build_upstream_headers(headers or [], url, jar)
    try:
        resp = await client.request(
            method.upper(),
            url,
            headers=up_headers,
            content=body if body else None,
            follow_redirects=follow_redirects,
        )
    except httpx.TimeoutException as exc:
        raise UpstreamError(504, f"upstream timeout: {exc}") from exc
    except httpx.HTTPError as exc:
        raise UpstreamError(502, f"upstream error: {exc}") from exc

    # httpx.AsyncClient は内部に共有 Cookie ジャーを持つため、
    # セッション間で Cookie が混ざらないよう毎回破棄する（重要）
    client.cookies.clear()

    set_cookies = resp.headers.get_list("set-cookie")
    if jar is not None and set_cookies:
        for sc in set_cookies:
            jar.set_cookie(sc, url)

    raw_body = _decompress(resp.content, resp.headers.get("content-encoding", ""))

    headers_out: list[tuple[str, str]] = []
    for k, v in resp.headers.multi_items():
        lk = k.lower()
        if lk in HOP_BY_HOP or lk in ("content-encoding", "content-length", "set-cookie"):
            continue
        if lk in STRIP_FROM_UPSTREAM:
            continue
        headers_out.append((lk, v))

    return UpstreamResponse(
        status=resp.status_code,
        reason=resp.reason_phrase,
        headers=headers_out,
        body=raw_body,
        set_cookies=set_cookies,
        url=str(resp.url),
        elapsed_ms=int(resp.elapsed.total_seconds() * 1000) if resp.elapsed else 0,
    )


# ---------------------------------------------------------------------------
# CONNECT（WebSocket / HTTP のトンネル）
# ---------------------------------------------------------------------------
def rewrite_set_cookie(value: str, *, secure_ok: bool) -> str:
    """Domain を外してホスト限定にし、localhost(http) でも保存できる形に整える"""
    parts = [p.strip() for p in value.split(";")]
    if not parts:
        return value
    out = [parts[0]]
    for p in parts[1:]:
        low = p.lower()
        if low.startswith("domain="):
            continue                      # ホスト限定 Cookie にする
        if low.startswith("partitioned"):
            continue
        if low.startswith("secure") and not secure_ok:
            continue
        if low.startswith("samesite=none") and not secure_ok:
            out.append("SameSite=Lax")
            continue
        out.append(p)
    return "; ".join(out)


async def open_tunnel(host: str, port: int) -> tuple:
    if not CFG.host_allowed(host):
        raise UpstreamError(403, "host not allowed: " + host)
    assert_safe_target(f"https://{host}:{port}/")
    reader, writer = await asyncio_open_connection(host, port)
    return reader, writer


async def asyncio_open_connection(host: str, port: int):
    import asyncio

    return await asyncio.wait_for(
        asyncio.open_connection(host, port), timeout=CFG.connect_timeout
    )
