"""E2E テスト: ローカル上流サーバーを立てて、プロキシ経由で取得・書換・Cookie を検証"""
from __future__ import annotations

import socket
import sys
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from proxy.config import CFG  # noqa: E402


# --------------------------------------------------------------------------
# ダミー上流サーバー（Instagram の代役）
# --------------------------------------------------------------------------
def build_upstream() -> FastAPI:
    up = FastAPI()

    @up.get("/", response_class=HTMLResponse)
    async def home(request: Request):
        resp = HTMLResponse(
            """<html><head><title>Instagram</title>
            <link rel="stylesheet" href="/static/app.css"></head>
            <body><h1>dummy instagram</h1>
            <a href="/explore/">explore</a>
            <img src="/media/a.jpg" srcset="/media/a.jpg 1x, /media/b.jpg 2x">
            <script src="/static/app.js"></script>
            <p>cookie=%s</p></body></html>""" % request.headers.get("cookie", "")
        )
        resp.set_cookie("csrftoken", "tok123", domain="127.0.0.1", path="/")
        return resp

    @up.get("/explore/", response_class=HTMLResponse)
    async def explore():
        return HTMLResponse("<html><body>explore<video poster='/media/p.jpg'></video></body></html>")

    @up.get("/static/app.css")
    async def css():
        return Response("body{background:url(/media/bg.png)}", media_type="text/css")

    @up.get("/static/app.js")
    async def js():
        return Response('fetch("/api/graphql");', media_type="application/javascript")

    @up.get("/api/graphql")
    async def graphql(request: Request):
        return {"data": {"viewer": None}, "cookie": request.headers.get("cookie", "")}

    @up.get("/media/{name}")
    async def media(name: str):
        return Response(b"\x89PNG\r\n\x1a\n" + name.encode(), media_type="image/png",
                        headers={"cache-control": "public, max-age=60"})

    @up.get("/redirect")
    async def redirect():
        return Response(status_code=302, headers={"location": "/explore/"})

    @up.post("/echo")
    async def echo(request: Request):
        return {"method": "POST", "body": (await request.body()).decode()}

    return up


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture(scope="session")
def servers():
    up_port, px_port = free_port(), free_port()

    up_cfg = uvicorn.Config(build_upstream(), host="127.0.0.1", port=up_port, log_level="warning")
    up_server = uvicorn.Server(up_cfg)
    t1 = threading.Thread(target=up_server.run, daemon=True)
    t1.start()

    from proxy.server import app as proxy_app

    px_cfg = uvicorn.Config(proxy_app, host="127.0.0.1", port=px_port, log_level="warning")
    px_server = uvicorn.Server(px_cfg)
    t2 = threading.Thread(target=px_server.run, daemon=True)
    t2.start()

    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            socket.create_connection(("127.0.0.1", up_port), 0.2).close()
            socket.create_connection(("127.0.0.1", px_port), 0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    yield {"up": f"http://127.0.0.1:{up_port}", "px": f"http://127.0.0.1:{px_port}"}
    up_server.should_exit = True
    px_server.should_exit = True


# --------------------------------------------------------------------------
# テスト
# --------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def local_policy(servers):
    """テスト中だけ「ローカル上流のみ許可」ポリシーに切り替え、終了時に必ず戻す"""
    saved = (CFG.mode, CFG.allow_private, CFG.media_suffixes, CFG.allowed_hosts)
    CFG.mode = "open"
    CFG.allow_private = True
    CFG.media_suffixes = ("127.0.0.1",)
    CFG.allowed_hosts = ("127.0.0.1",)
    try:
        yield
    finally:
        CFG.mode, CFG.allow_private, CFG.media_suffixes, CFG.allowed_hosts = saved


def test_viewer_and_health(servers):
    import httpx

    r = httpx.get(servers["px"] + "/", timeout=10)
    assert r.status_code == 200 and "Instagram Proxy" in r.text
    h = httpx.get(servers["px"] + "/health", timeout=10).json()
    assert h["ok"] is True


def test_proxy_html_rewrite(servers):
    import httpx
    from urllib.parse import quote

    target = servers["up"] + "/"
    r = httpx.get(servers["px"] + "/p/" + quote(target, safe=""), timeout=15)
    assert r.status_code == 200
    body = r.text
    assert "dummy instagram" in body
    assert "__PROXY_SHIMMED__" in body        # 実行時シムが注入される
    assert 'href="/explore/"' in body         # sticky: 同一ホストはパスのまま
    assert CFG.cookie_name in r.cookies       # セッション Cookie が発行される


def test_proxy_css_js_and_redirect(servers):
    import httpx
    from urllib.parse import quote

    q = lambda p: servers["px"] + "/p/" + quote(servers["up"] + p, safe="")
    css = httpx.get(q("/static/app.css"), timeout=10)
    assert "background" in css.text
    # sticky: 同一ホストの url() はパスのまま
    assert 'url("/media/bg.png")' in css.text

    js = httpx.get(q("/static/app.js"), timeout=10)
    assert "fetch(" in js.text

    r = httpx.get(q("/redirect"), follow_redirects=False, timeout=10)
    loc = r.headers["location"]
    # sticky: 同一ホストへのリダイレクトはパスのまま書き換えられる
    assert r.status_code == 302 and loc == "/explore/"


def test_cookie_isolation_and_injection(servers):
    """サーバー側ジャーの Cookie が上流失行し、2 クライアント間で混ざらないこと"""
    import httpx
    from urllib.parse import quote

    a = httpx.Client(timeout=15)
    b = httpx.Client(timeout=15)
    q = lambda p: servers["px"] + "/p/" + quote(servers["up"] + p, safe="")

    ra = a.get(q("/"))                                   # 1 回目: まだ Cookie 無し
    assert ra.status_code == 200
    assert "<p>cookie=</p>" in ra.text
    ra2 = a.get(q("/"))                                  # 2 回目: ジャーの Cookie が注入される
    assert "csrftoken=tok123" in ra2.text

    rb = b.get(q("/"))                                   # 別クライアントには漏れない
    assert "<p>cookie=</p>" in rb.text
    rb2 = b.get(q("/"))                                  # 自分の分だけ持っている
    assert "csrftoken=tok123" in rb2.text


def test_http_relay_binary(servers):
    import httpx

    from proxy.urlmap import pack_frame, unpack_frame

    target = servers["up"] + "/api/graphql"
    frame = pack_frame({"url": target, "method": "GET", "headers": [["x-test", "1"]]})
    r = httpx.post(servers["px"] + "/instagram/runtime/http-relay", content=frame, timeout=15)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/octet-stream")
    meta, body = unpack_frame(r.content)
    assert meta["status"] == 200
    assert b"viewer" in body

    # POST も通ること
    frame = pack_frame({"url": servers["up"] + "/echo", "method": "POST",
                        "headers": [["content-type", "text/plain"]]}, b"hello")
    r = httpx.post(servers["px"] + "/instagram/runtime/http-relay", content=frame, timeout=15)
    meta, body = unpack_frame(r.content)
    assert meta["status"] == 200 and b"hello" in body

    # 壊れたフレームは 400
    assert httpx.post(servers["px"] + "/instagram/runtime/http-relay", content=b"\x00", timeout=10).status_code == 400


def test_media_relay_signature_and_cache(servers):
    import httpx

    from proxy.urlmap import encode_media_key, sign_media_url

    url = servers["up"] + "/media/a.jpg"
    k = encode_media_key(url)
    sig = sign_media_url(url, CFG.hash_key)

    ok = httpx.get(f"{servers['px']}/instagram/runtime/media-relay?k={k}&s={sig}", timeout=10)
    assert ok.status_code == 200
    assert ok.content.startswith(b"\x89PNG")
    assert ok.headers["access-control-allow-origin"] == "*"

    bad = httpx.get(f"{servers['px']}/instagram/runtime/media-relay?k={k}&s=deadbeef", timeout=10)
    assert bad.status_code == 403 and "Bad URL hash" in bad.text

    nosig = httpx.get(f"{servers['px']}/instagram/runtime/media-relay?k={k}", timeout=10)
    assert nosig.status_code == 403


def test_ssrf_and_host_allowlist(servers):
    import httpx

    from proxy.urlmap import pack_frame

    # 許可外ホスト（allowlist モードに一時切替えて検証）
    CFG.mode = "instagram"
    try:
        frame = pack_frame({"url": "https://evil.example/", "method": "GET", "headers": []})
        r = httpx.post(servers["px"] + "/instagram/runtime/http-relay", content=frame, timeout=10)
        assert r.status_code == 403
        from urllib.parse import quote

        r2 = httpx.get(servers["px"] + "/p/" + quote("https://evil.example/", safe=""), timeout=10)
        assert r2.status_code == 403
    finally:
        CFG.mode = "open"


def test_diag_logging(servers):
    import httpx

    r = httpx.post(servers["px"] + "/_diag/instagram",
                   json={"source": "test", "level": "info", "event": "probe"}, timeout=10)
    assert r.status_code == 204
    logs = httpx.get(servers["px"] + "/_diag/instagram", timeout=10).json()
    assert any(x.get("event") == "probe" for x in logs["logs"])


def test_session_endpoints(servers):
    import httpx

    c = httpx.Client(timeout=10)
    c.get(servers["px"] + "/health")
    info = c.get(servers["px"] + "/session").json()
    assert info["sid"].startswith("sps-")
    assert info["has_auth"] is False
    assert c.delete(servers["px"] + "/session").json()["ok"] is True
