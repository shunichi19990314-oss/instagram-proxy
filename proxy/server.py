"""
instagram-proxy : Instagram 用 Web プロキシ（FastAPI 実装）

実物 (miruhukaka.ai.studio/instagram/) の構成をブラウザ内エンジン (Scramjet/WISP) から
サーバーサイド実装に置き換えたもの。外形的な API は本家に合わせている。

  GET  /                      -> ビューア（iframe）
  ANY  /p/<encoded-url>       -> HTML/CSS/JS を書き換えて返す透過プロキシ
  POST /instagram/runtime/http-relay   -> [4byte len][meta JSON][body] のバイナリリレー
  POST /instagram/runtime/media-relay  -> メディア専用（k= 鍵 + s= HMAC 署名 + キャッシュ）
  ANY  /instagram/runtime/media-relay  -> GET/HEAD も可
  POST /_diag/instagram       -> クライアントログ受付 (204)
  CONNECT 任意ホスト:443      -> WebSocket / ストリーム用トンネル

起動:  uvicorn proxy.server:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from .config import CFG
from .cookies import SessionStore
from .fetch import UpstreamError, fetch_upstream, open_tunnel
from .rewriter import inject_shim, rewrite_body, should_rewrite, to_proxy_url
from .urlmap import (
    absolute,
    decode_media_key,
    encode_media_key,
    is_http_url,
    proxy_path,
    sign_media_url,
    unpack_frame,
    verify_media_url,
)

APP_DIR = Path(__file__).resolve().parent.parent

app = FastAPI(title="instagram-proxy", version="1.0.0", docs_url="/_docs", openapi_url="/_openapi.json")
app.state.sessions = SessionStore()
app.state.logs = deque(maxlen=500)
app.state.started = time.time()
app.state.stats = {"relay": 0, "media": 0, "proxy": 0, "errors": 0, "rewritten": 0}


@app.on_event("startup")
async def _startup() -> None:
    app.state.client = httpx.AsyncClient(
        timeout=httpx.Timeout(CFG.upstream_timeout, connect=CFG.connect_timeout),
        limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
        headers={"accept-encoding": "gzip, deflate"},  # br は環境依存なので広告しない
        http2=False,
    )


@app.on_event("shutdown")
async def _shutdown() -> None:
    await app.state.client.aclose()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _session(request: Request) -> tuple[str, object]:
    sid = request.cookies.get(CFG.cookie_name) or request.query_params.get("sid")
    return app.state.sessions.get(sid, create=True)


def _set_session_cookie(resp: Response, sid: str) -> None:
    resp.set_cookie(
        CFG.cookie_name, sid, max_age=CFG.session_ttl, path="/",
        httponly=True, samesite="lax",
    )


def _json(body: dict, status: int = 200) -> Response:
    return JSONResponse(body, status_code=status)


def _entity_headers(up, extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = {k: v for k, v in up.headers if k.lower() not in ("location",)}
    if extra:
        headers.update(extra)
    return headers


# ---------------------------------------------------------------------------
# 0) ビューア
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def viewer() -> HTMLResponse:
    return HTMLResponse((APP_DIR / "viewer.html").read_text(encoding="utf-8"))


@app.get("/health")
async def health() -> Response:
    return _json({
        "ok": True,
        "uptime_sec": round(time.time() - app.state.started, 1),
        "mode": CFG.mode,
        "stats": app.state.stats,
        "sessions": app.state.sessions.stats(),
    })


# ---------------------------------------------------------------------------
# 1) 透過プロキシ  /p/<encoded-url>
# ---------------------------------------------------------------------------
@app.api_route(
    "/p/{encoded:path}",
    methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
)
async def proxy(encoded: str, request: Request) -> Response:
    from urllib.parse import unquote

    target = unquote(encoded)
    if not is_http_url(target):
        return _json({"detail": "invalid target url"}, 400)

    sid, jar = _session(request)
    body = await request.body() if request.method not in ("GET", "HEAD") else b""
    incoming = [(k, v) for k, v in request.headers.items()]

    try:
        up = await fetch_upstream(
            app.state.client, target, request.method, incoming, body or None, jar
        )
    except UpstreamError as exc:
        app.state.stats["errors"] += 1
        return _json({"detail": exc.message}, exc.status)

    ctype = up.header("content-type")

    # --- リダイレクトは仮想URL空間に付け替える ---
    location = up.header("location")
    if up.status in (301, 302, 303, 307, 308) and location:
        new_loc = to_proxy_url(location, target)
        headers = _entity_headers(up, {"location": new_loc})
        resp = Response(b"", status_code=up.status, headers=headers)
        _set_session_cookie(resp, sid)
        return resp

    payload = up.body
    if request.method != "HEAD" and should_rewrite(ctype) and len(payload) <= CFG.max_inspect_bytes:
        text = payload.decode("utf-8", "replace")
        rewritten = rewrite_body(text, up.url or target, ctype)
        if "text/html" in ctype.lower():
            rewritten = inject_shim(rewritten)
        payload = rewritten.encode("utf-8")
        app.state.stats["rewritten"] += 1
        app.state.stats["proxy"] += 1
    else:
        app.state.stats["proxy"] += 1

    headers = _entity_headers(up)
    headers["cache-control"] = headers.get("cache-control", "no-store")
    resp = Response(payload, status_code=up.status, headers=headers)
    _set_session_cookie(resp, sid)
    return resp


# ---------------------------------------------------------------------------
# 2) バイナリ HTTP リレー  /instagram/runtime/http-relay
#    [4byte BE: meta 長][meta JSON][body]  ->  [4byte BE][meta JSON][body]
# ---------------------------------------------------------------------------
@app.post(CFG.http_relay)
async def http_relay(request: Request) -> Response:
    raw = await request.body()
    try:
        meta, body = unpack_frame(raw)
    except Exception as exc:
        app.state.stats["errors"] += 1
        return Response(f"Bad frame: {exc}", status_code=400)

    url = str(meta.get("url") or "")
    method = str(meta.get("method") or "GET").upper()
    headers = [
        (str(k), str(v)) for k, v in (meta.get("headers") or [])
        if isinstance(k, str)
    ]
    sid, jar = _session(request)

    if not is_http_url(url):
        return Response("Bad URL", status_code=400)

    app.state.stats["relay"] += 1
    try:
        up = await fetch_upstream(app.state.client, url, method, headers, body or None, jar)
    except UpstreamError as exc:
        app.state.stats["errors"] += 1
        return Response(f"relay error: {exc.message}", status_code=exc.status)

    out_headers: list[list[str]] = []
    for k, v in up.headers:
        if k.lower() in ("set-cookie", "content-encoding", "content-length"):
            continue
        out_headers.append([k, v])
    # Set-Cookie もクライアントへ返す（本物は transport 経由で cookieJar に同期）
    for k, v in up.headers:
        if k.lower() == "set-cookie":
            out_headers.append([k, v])

    meta_out = {
        "status": up.status,
        "statusText": up.reason or "OK",
        "headers": out_headers,
        "url": up.url,
        "elapsed_ms": up.elapsed_ms,
    }
    meta_bytes = json.dumps(meta_out, separators=(",", ":")).encode("utf-8")
    payload = len(meta_bytes).to_bytes(4, "big") + meta_bytes + up.body
    resp = Response(payload, media_type="application/octet-stream")
    _set_session_cookie(resp, sid)
    return resp


# ---------------------------------------------------------------------------
# 3) メディア・リレー  /instagram/runtime/media-relay
#    k = encodeMediaKey(url), s = HMAC 署名（本物の "Bad URL hash" 相当）
# ---------------------------------------------------------------------------
@app.api_route(CFG.media_relay, methods=["GET", "HEAD"])
async def media_relay(request: Request) -> Response:
    q = request.query_params
    url = decode_media_key(q.get("k", "")) if q.get("k") else (q.get("url") or "")
    if not url or not is_http_url(url):
        return Response("Bad URL hash", status_code=403)

    signature = q.get("s", "")
    if not verify_media_url(url, signature, CFG.hash_key):
        app.state.stats["errors"] += 1
        return Response("Bad URL hash", status_code=403)

    host = urlsplit(url).hostname or ""
    if not CFG.is_media_host(host):
        return Response("disallowed media host", status_code=403)

    sid, jar = _session(request)
    fwd: list[tuple[str, str]] = []
    for name in ("range", "accept"):
        v = request.headers.get(name)
        if v:
            fwd.append((name, v))
    fwd.append(("referer", CFG.referer_for_media))

    app.state.stats["media"] += 1
    try:
        up = await fetch_upstream(
            app.state.client, url, request.method, fwd, None, jar
        )
    except UpstreamError as exc:
        return Response(exc.message, status_code=exc.status)

    headers = {k: v for k, v in up.headers}
    headers["access-control-allow-origin"] = "*"
    headers["cross-origin-resource-policy"] = "cross-origin"
    headers["cache-control"] = headers.get("cache-control", "public, max-age=3600")
    body = b"" if request.method == "HEAD" else up.body
    resp = Response(body, status_code=up.status, headers=headers)
    _set_session_cookie(resp, sid)
    return resp


# ---------------------------------------------------------------------------
# 4) 診断ログ（本物の /_diag/instagram）
# ---------------------------------------------------------------------------
@app.post(CFG.diag)
async def diag(request: Request) -> Response:
    try:
        payload = await request.json()
    except Exception:
        payload = {"raw": (await request.body())[:500].decode("utf-8", "replace")}
    payload["_server_ts"] = round(time.time(), 3)
    payload["_client"] = request.client.host if request.client else ""
    app.state.logs.append(payload)
    if CFG.debug:
        print("[diag]", json.dumps(payload, ensure_ascii=False)[:400])
    return Response(status_code=204)


@app.get(CFG.diag)
async def diag_list() -> Response:
    return _json({"count": len(app.state.logs), "logs": list(app.state.logs)[-100:]})


# ---------------------------------------------------------------------------
# 5) セッション操作（デバッグ用）
# ---------------------------------------------------------------------------
@app.get("/session")
async def session_info(request: Request) -> Response:
    sid, jar = _session(request)
    return _json({
        "sid": sid,
        "has_auth": jar.has_auth(),
        "cookies": [
            {k: v for k, v in c.items() if k in ("domain", "name", "path")}
            for c in jar.dump()
        ],
    })


@app.delete("/session")
async def session_clear(request: Request) -> Response:
    sid = request.cookies.get(CFG.cookie_name) or ""
    if sid:
        app.state.sessions.delete(sid)
    resp = _json({"ok": True})
    resp.delete_cookie(CFG.cookie_name, path="/")
    return resp


# ---------------------------------------------------------------------------
# 6) メディア URL 生成ヘルパ（クライアントや自作フロントから利用）
# ---------------------------------------------------------------------------
@app.get("/api/media-url")
async def media_url(url: str) -> Response:
    if not is_http_url(url):
        return _json({"detail": "invalid url"}, 400)
    return _json({
        "k": encode_media_key(url),
        "s": sign_media_url(url, CFG.hash_key),
        "path": f"{CFG.media_relay}?k={quote(encode_media_key(url), safe='')}",
        "signed_path": (
            f"{CFG.media_relay}?k={quote(encode_media_key(url), safe='')}"
            f"&s={sign_media_url(url, CFG.hash_key)}"
        ),
    })


# ---------------------------------------------------------------------------
# 7) CONNECT トンネル（WebSocket 等）: 生 ASGI で処理
# ---------------------------------------------------------------------------
@app.api_route("/{full_path:path}", methods=["CONNECT"])
async def connect_tunnel(full_path: str, request: Request) -> Response:
    return _json({"detail": "CONNECT is handled at the ASGI layer"}, 405)


async def asgi_app(scope, receive, send):
    """uvicorn --factory からは使えないので、CONNECT だけ uvicorn の前に挟む想定"""
    if scope["type"] == "http" and scope["method"] == "CONNECT":
        await _handle_connect(scope, receive, send)
        return
    await app(scope, receive, send)


async def _handle_connect(scope, receive, send) -> None:
    hostport = "".join(
        p.decode() for name, p in scope.get("headers", []) if name == b":authority"
    ) or scope.get("path", "").split("/")[-1]
    host, _, port_s = hostport.partition(":")
    port = int(port_s or 443)
    try:
        reader, writer = await open_tunnel(host, port)
    except Exception as exc:
        await send({"type": "http.response.start", "status": 403,
                    "headers": [(b"content-type", b"text/plain")]})
        await send({"type": "http.response.body", "body": str(exc).encode()})
        return

    await send({"type": "http.response.start", "status": 200,
                "headers": [(b"content-type", b"text/plain")]})
    await send({"type": "http.response.body", "body": b""})

    async def pump_up():
        while True:
            msg = await receive()
            if msg["type"] == "http.disconnect":
                break
            body = msg.get("body", b"")
            if body:
                writer.write(body)
                await writer.drain()

    import contextlib

    with contextlib.suppress(Exception):
        await asyncio.wait_for(pump_up(), timeout=CFG.upstream_timeout)
    writer.close()
    import contextlib as _c

    with _c.suppress(Exception):
        await writer.wait_closed()
