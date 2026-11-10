"""上流への取得（HTTP/CONNECT のブリッジ／SSRF ガード／Cookie 注入）"""
from __future__ import annotations

import gzip
import ipaddress
import socket
import zlib
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

from .config import CFG
from .cookies import CookieJar

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
    assert_safe_target(url)
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

    # Set-Cookie を取り込む
    set_cookies = resp.headers.get_list("set-cookie")
    if jar is not None and set_cookies:
        for sc in set_cookies:
            jar.set_cookie(sc, url)

    # 圧縮を解凍（ヘッダから content-encoding を落として素の本文を返す）
    raw_body = _decompress(resp.content, resp.headers.get("content-encoding", ""))

    headers_out: list[tuple[str, str]] = []
    for k, v in resp.headers.multi_items():
        lk = k.lower()
        if lk in HOP_BY_HOP or lk in ("content-encoding", "content-length", "set-cookie"):
            continue
        headers_out.append((lk, v))

    return UpstreamResponse(
        status=resp.status_code,
        reason=resp.reason_phrase,
        headers=headers_out,
        body=raw_body,
        url=str(resp.url),
        elapsed_ms=int(resp.elapsed.total_seconds() * 1000) if resp.elapsed else 0,
    )


# ---------------------------------------------------------------------------
# CONNECT（WebSocket / HTTP のトンネル）
# ---------------------------------------------------------------------------
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
