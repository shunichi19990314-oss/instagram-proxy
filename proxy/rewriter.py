"""HTML / CSS / JS の URL 書換（Scramjet 相当の“その場書き換え”）"""
from __future__ import annotations

import re
import urllib.parse
import urllib.parse as urllib_parse
from html import escape

from .config import CFG
from .urlmap import absolute, encode_media_key, is_http_url, proxy_path, sign_media_url

# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------
URL_ATTRS = {
    "href", "src", "action", "formaction", "poster", "data", "cite", "background",
    "longdesc", "manifest", "srcset", "imagesrcset", "ping", "xlink:href",
}
SRCSET_ATTRS = {"srcset", "imagesrcset"}
# 書換対象から外す属性（JS フレームワークの内部状態を壊さないため）
SKIP_ATTRS = {"integrity", "crossorigin", "nonce", "srcdoc", "sandbox"}

RESERVED_PATHS = (
    CFG.prefix, CFG.http_relay, CFG.media_relay, CFG.diag,
    "/design-system/", "/_next/", "/__shim", "/favicon.ico",
)

TAG_RE = re.compile(r"<([a-zA-Z][\w:-]*)([^>]*)>", re.S)
ATTR_RE = re.compile(
    r"([a-zA-Z_:][-\w:.]*)\s*=\s*(\"([^\"]*)\"|'([^']*)'|([^\s\"'=<>`]+))", re.S
)
BOOL_ATTR_RE = re.compile(r"\s+(?:integrity|crossorigin|nonce)(?=[\s/>]|$)", re.I)
META_CSP_RE = re.compile(
    r"<meta[^>]+http-equiv\s*=\s*[\"']?content-security-policy[\"']?[^>]*>", re.I
)
BASE_RE = re.compile(r"<base[^>]*>", re.I)

JS_STR_RE = re.compile(
    r"""(?P<q>["'`])(?P<u>(?:https?:)?//[^"'`\s\\<>]+|/[^"'`\s\\<>]*)(?P=q)"""
)
CSS_URL_RE = re.compile(r"""url\(\s*(?P<q>['"]?)(?P<u>[^'")]+)(?P=q)\s*\)""", re.I)
CSS_IMPORT_RE = re.compile(
    r"""@import\s+(?:url\(\s*)?(?P<q>['"])(?P<u>[^'"]+)(?P=q)""", re.I
)


# ---------------------------------------------------------------------------
# 1 個の URL を仮想 URL に変換
# ---------------------------------------------------------------------------
def to_proxy_url(ref: str, base_url: str, force_scheme: bool = False) -> str:
    """相対 URL を解決して /p/<encoded> に変換する。対象外はそのまま返す。"""
    if not ref:
        return ref
    raw = ref.strip()
    if not raw:
        return ref
    low = raw.lower()

    # そのまま残すもの
    if low.startswith(("#", "data:", "blob:", "javascript:", "mailto:", "tel:", "about:")):
        return ref
    if raw in ("/", ""):
        return ref
    # 自オリジンのプロキシ系パスは二重変換しない
    if raw.startswith(RESERVED_PATHS):
        return ref

    target = absolute(base_url, raw)
    # protocol-relative (//host/path)
    if target.startswith("//"):
        target = "https:" + target
    if not is_http_url(target):
        return ref
    # 既にプロキシ済みなら触らない
    if target.startswith(("http://", "https://")) and CFG.prefix in target:
        return ref

    # 画像・動画 CDN は media-relay（署名 + キャッシュ）に寄せる
    if CFG.media_via_relay and CFG.is_media_host(urllib_parse.urlsplit(target).hostname or ""):
        k = encode_media_key(target)
        s = sign_media_url(target, CFG.hash_key)
        return f"{CFG.media_relay}?k={urllib_parse.quote(k, safe='')}&s={s}"

    return proxy_path(CFG.prefix, target)


def rewrite_srcset(value: str, base_url: str) -> str:
    parts = []
    for chunk in value.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        bits = chunk.split(None, 1)
        url = bits[0]
        desc = (" " + bits[1]) if len(bits) > 1 else ""
        parts.append(to_proxy_url(url, base_url) + desc)
    return ", ".join(parts)


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------
def rewrite_html(html: str, base_url: str) -> str:
    # (1) 邪魔な meta / base を除去
    html = META_CSP_RE.sub("", html)
    html = BASE_RE.sub("", html)

    def fix_tag(m: re.Match) -> str:
        tag_name = m.group(1)
        raw_attrs = m.group(2)
        if not raw_attrs.strip() and not raw_attrs.endswith("/"):
            return m.group(0)

        def fix_attr(am: re.Match) -> str:
            name = am.group(1)
            lname = name.lower()
            quote = '"'
            value = am.group(3) if am.group(3) is not None else (
                am.group(4) if am.group(4) is not None else am.group(5) or ""
            )
            if am.group(3) is not None:
                quote = '"'
            elif am.group(4) is not None:
                quote = "'"

            if lname in SKIP_ATTRS:
                # integrity / crossorigin / nonce は書換で無効になるので落とす
                return ""
            if lname == "content" and tag_name.lower() == "meta":
                new = _rewrite_meta_refresh(value, base_url)
                return f'{name}={quote}{escape(new, quote=True)}{quote}'
            if lname in URL_ATTRS:
                if lname in SRCSET_ATTRS:
                    new = rewrite_srcset(value, base_url)
                elif lname == "ping":
                    new = " ".join(to_proxy_url(u, base_url) for u in value.split())
                else:
                    new = to_proxy_url(value, base_url)
                return f'{name}={quote}{escape(new, quote=True)}{quote}'
            if lname == "style":
                new_css = rewrite_css(value, base_url)
                return f'{name}={quote}{escape(new_css, quote=True)}{quote}'
            return am.group(0)

        new_attrs = ATTR_RE.sub(fix_attr, raw_attrs)
        # 値を持たない真偽属性（crossorigin / integrity / nonce）も落とす
        new_attrs = BOOL_ATTR_RE.sub("", new_attrs)
        if tag_name.lower() == "meta":
            return f"<{tag_name}{new_attrs}>"
        return f"<{tag_name}{new_attrs}>"

    out = TAG_RE.sub(fix_tag, html)

    # (2) <style> ブロック内 CSS
    def fix_style_block(m: re.Match) -> str:
        return m.group(1) + rewrite_css(m.group(2), base_url) + m.group(3)

    out = re.sub(r"(<style[^>]*>)(.*?)(</style>)", fix_style_block, out, flags=re.S | re.I)

    # (3) インラインイベントハンドラ内の URL も一応変換（安全側で文字列のみ）
    return out


def _rewrite_meta_refresh(value: str, base_url: str) -> str:
    m = re.match(r"\s*(\d+)\s*;\s*url\s*=\s*(.+)$", value, re.I)
    if not m:
        return value
    return f"{m.group(1)}; url={to_proxy_url(m.group(2).strip('\'"'), base_url)}"


# ---------------------------------------------------------------------------
# CSS
# ---------------------------------------------------------------------------
def rewrite_css(css: str, base_url: str) -> str:
    def fix_url(m: re.Match) -> str:
        u = m.group("u").strip()
        new = to_proxy_url(u, base_url)
        return f'url("{new}")'

    def fix_import(m: re.Match) -> str:
        new = to_proxy_url(m.group("u"), base_url)
        return f'@import "{new}"'

    css = CSS_URL_RE.sub(fix_url, css)
    css = CSS_IMPORT_RE.sub(fix_import, css)
    return css


# ---------------------------------------------------------------------------
# JS（文字列リテラル中の URL）
# ---------------------------------------------------------------------------
def rewrite_js(js: str, base_url: str) -> str:
    def fix(m: re.Match) -> str:
        q = m.group("q")
        u = m.group("u")
        new = to_proxy_url(u, base_url)
        if new == u:
            return m.group(0)
        if q in new:
            return m.group(0)
        return f"{q}{new}{q}"

    # 末尾が / の裸パス等は対象外（誤変換を避ける）
    def guard(m: re.Match) -> str:
        u = m.group("u")
        if len(u) < 2:
            return m.group(0)
        return fix(m)

    return JS_STR_RE.sub(guard, js)


# ---------------------------------------------------------------------------
# エントリポイント
# ---------------------------------------------------------------------------
def rewrite_body(text: str, base_url: str, content_type: str) -> str:
    ct = (content_type or "").lower()
    if "text/html" in ct or "application/xhtml" in ct:
        return rewrite_html(text, base_url)
    if "text/css" in ct:
        return rewrite_css(text, base_url)
    if "javascript" in ct or "ecmascript" in ct or "json" in ct:
        # JSON は API レスポンスなので書換不要（誤爆防止）
        if "json" in ct:
            return text
        return rewrite_js(text, base_url)
    if "xml" in ct and "html" not in ct:
        return text
    return text


def should_rewrite(content_type: str) -> bool:
    ct = (content_type or "").lower()
    return any(
        k in ct
        for k in ("text/html", "application/xhtml", "text/css", "javascript", "ecmascript")
    )


SHIM = """
<script>
/* minimal proxy shim (Scramjet の controller.inject.js 相当の最小版) */
(function () {
  "use strict";
  window.__PROXY_PREFIX__ = "%(prefix)s";
  // pushState で入る相対パスを仮想URL空間に寄せる
  const push = history.pushState.bind(history);
  history.pushState = function (state, title, url) {
    if (url && typeof url === "string" && url.startsWith("/") && !url.startsWith("%(prefix)s")) {
      url = "%(prefix)s" + encodeURIComponent(location.origin + url);
    }
    return push(state, title, url);
  };
  // 相対リンク対策：クリック時に base が無いので絶対URLで開く
  document.addEventListener("click", function (e) {
    const a = e.target && e.target.closest ? e.target.closest("a[href]") : null;
    if (!a) return;
    const h = a.getAttribute("href");
    if (!h || !h.startsWith("%(prefix)s")) return;
    if (a.target === "_blank") a.rel = "noopener noreferrer";
  }, true);
  // 例外はテレメトリへ（本物の /_diag/instagram 相当）
  window.addEventListener("error", function (ev) {
    try {
      fetch("%(diag)s", { method: "POST", credentials: "omit", keepalive: true,
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ source: "browser-shim", level: "error",
          event: "script-error", details: { message: String(ev.message || "") } }) });
    } catch (_) {}
  });
})();
</script>
"""


def inject_shim(html: str) -> str:
    shim = SHIM % {"prefix": CFG.prefix, "diag": CFG.diag}
    idx = html.lower().find("<head")
    if idx < 0:
        return shim + html
    end = html.find(">", idx)
    if end < 0:
        return shim + html
    return html[: end + 1] + shim + html[end + 1:]
