"""ユニットテスト: URL 変換・フレーム・鍵・署名・書換・Cookie"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from proxy.config import CFG  # noqa: E402
from proxy.cookies import CookieJar  # noqa: E402
from proxy.rewriter import rewrite_css, rewrite_html, rewrite_js, to_proxy_url  # noqa: E402
from proxy.urlmap import (  # noqa: E402
    decode_media_key,
    encode_media_key,
    pack_frame,
    proxy_path,
    sign_media_url,
    unpack_frame,
    verify_media_url,
)


# --------------------------------------------------------------- URL 変換
def test_proxy_path_roundtrip():
    url = "https://www.instagram.com/p/ABC123/?utm=1&x=%E3%81%82"
    p = proxy_path(CFG.prefix, url)
    assert p.startswith("/p/")
    from urllib.parse import unquote

    assert unquote(p[len("/p/"):]) == url


def test_to_proxy_url_keeps_data_and_hash():
    base = "https://www.instagram.com/"
    assert to_proxy_url("#top", base) == "#top"
    assert to_proxy_url("data:image/png;base64,AAA", base) == "data:image/png;base64,AAA"
    assert to_proxy_url("javascript:void(0)", base) == "javascript:void(0)"


def test_to_proxy_url_relative_and_protocol_relative():
    base = "https://www.instagram.com/"
    assert to_proxy_url("/static/app.js", base) == proxy_path(CFG.prefix, "https://www.instagram.com/static/app.js")
    # プロトコル相対 + メディアホスト → media-relay
    assert to_proxy_url("//cdninstagram.com/a.jpg", base).startswith(CFG.media_relay)
    out = to_proxy_url("https://scontent.cdninstagram.com/v/a.jpg?x=1", base)
    # メディア系ホストは media-relay（k= と s= 付き）に寄せる
    assert out.startswith(CFG.media_relay + "?k=")
    assert "&s=" in out
    k = out.split("k=")[1].split("&")[0]
    assert __import__("proxy.urlmap", fromlist=["decode_media_key"]).decode_media_key(k) == (
        "https://scontent.cdninstagram.com/v/a.jpg?x=1"
    )


# ------------------------------------------------------- メディア鍵/署名
def test_media_key_roundtrip():
    url = "https://scontent-nrt1-1.cdninstagram.com/v/t51.2885-19/123_n.jpg?stp=dst-jpg&_nc_ht=x"
    k = encode_media_key(url)
    assert "-" in k or "_" in k or k.isalnum()
    assert decode_media_key(k) == url


def test_media_signature():
    url = "https://scontent.cdninstagram.com/x.jpg"
    sig = sign_media_url(url, CFG.hash_key)
    assert verify_media_url(url, sig, CFG.hash_key)
    assert not verify_media_url(url, "deadbeef", CFG.hash_key)
    assert not verify_media_url(url + "?", sig, CFG.hash_key)


# --------------------------------------------------------------- フレーム
def test_frame_pack_unpack():
    meta = {"url": "https://www.instagram.com/api/graphql", "method": "POST",
            "headers": [["content-type", "application/json"]]}
    body = b'{"variables":{}}'
    raw = pack_frame(meta, body)
    m, b = unpack_frame(raw)
    assert m == meta and b == body
    # メタ長はビッグエンディアン 4 バイト
    meta_len = int.from_bytes(raw[:4], "big")
    assert raw[4:4 + meta_len] == __import__("json").dumps(
        meta, separators=(",", ":")).encode()
    assert raw[4 + meta_len:] == body


def test_frame_unpack_errors():
    with pytest.raises(ValueError):
        unpack_frame(b"\x00\x01")
    with pytest.raises(ValueError):
        unpack_frame((100).to_bytes(4, "big") + b"{}")


# --------------------------------------------------------------- 書換
def test_rewrite_html_attrs_srcset_style():
    html = """<html><head>
      <meta http-equiv="Content-Security-Policy" content="default-src 'self'">
      <base href="https://www.instagram.com/">
      <link rel="stylesheet" href="/static/app.css">
    </head><body>
      <a href="/explore/">探索</a>
      <img src="https://scontent.cdninstagram.com/a.jpg"
           srcset="https://scontent.cdninstagram.com/a.jpg 1x, /b.jpg 2x">
      <div style="background:url('/bg.png')"></div>
      <script src="/static/app.js" integrity="sha384-xxx" crossorigin></script>
    </body></html>"""
    out = rewrite_html(html, "https://www.instagram.com/")
    assert "Content-Security-Policy" not in out      # CSP は除去
    assert "<base" not in out                        # base は除去
    assert out.count("/p/") >= 3                     # href / script src / srcset の相対分
    assert CFG.media_relay in out                    # メディアは media-relay へ
    assert "integrity=" not in out and "crossorigin" not in out
    k = [x for x in out.split('?k=') if x][1].split('&')[0]
    from proxy.urlmap import decode_media_key

    assert "scontent.cdninstagram.com" in decode_media_key(k)


def test_rewrite_html_keeps_js_href():
    html = '<a href="javascript:void(0)">x</a>'
    assert rewrite_html(html, "https://www.instagram.com/") == html


def test_rewrite_css_and_js():
    css = "@import 'x.css'; .a{background:url(/img/a.png)} .b{background:url(https://scontent.cdninstagram.com/b.jpg)}"
    out = rewrite_css(css, "https://www.instagram.com/")
    assert out.count("/p/") == 2          # @import と相対 url()
    assert CFG.media_relay in out         # メディア URL は別経路

    js = 'const a="/api/graphql"; const b="https://scontent.cdninstagram.com/c.jpg";'
    out_js = rewrite_js(js, "https://www.instagram.com/")
    assert "/p/" in out_js


# --------------------------------------------------------------- Cookie
def test_cookiejar_domain_path_secure():
    jar = CookieJar()
    jar.set_cookie("sessionid=ABC; Domain=.instagram.com; Path=/; Secure; HttpOnly",
                   "https://www.instagram.com/")
    jar.set_cookie("csrftoken=T1; Domain=.instagram.com; Path=/", "https://www.instagram.com/")
    assert jar.has_auth()
    assert "sessionid=ABC" in jar.header_for("https://www.instagram.com/api/x")
    assert "sessionid=ABC" not in jar.header_for("http://www.instagram.com/api/x")  # secure
    assert jar.header_for("https://evil.example/") == ""                            # domain


def test_cookiejar_delete_by_maxage0():
    jar = CookieJar()
    jar.set_cookie("a=1; Domain=.instagram.com; Path=/", "https://www.instagram.com/")
    assert jar.header_for("https://www.instagram.com/") == "a=1"
    jar.set_cookie("a=; Domain=.instagram.com; Path=/; Max-Age=0", "https://www.instagram.com/")
    assert jar.header_for("https://www.instagram.com/") == ""


# --------------------------------------------------------------- ホスト許可
def test_host_allowlist():
    from proxy.config import Config

    cfg = Config()  # 既定値（E2E テストの書き換えに影響されないよう独立インスタンス）
    assert cfg.mode == "instagram"
    assert cfg.host_allowed("www.instagram.com")
    assert cfg.host_allowed("scontent-nrt1-1.cdninstagram.com")
    assert cfg.host_allowed("static.fbcdn.net")
    assert cfg.host_allowed("www.facebook.com")
    assert not cfg.host_allowed("evil.com")
    assert not cfg.host_allowed("instagram.com.evil.example")
    assert cfg.is_media_host("scontent.cdninstagram.com")
    assert not cfg.is_media_host("www.instagram.com")
