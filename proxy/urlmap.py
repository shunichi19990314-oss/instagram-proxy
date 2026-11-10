"""URL 変換・メディア鍵・バイナリフレームのユーティリティ"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import struct
import urllib.parse
from typing import Any


# --------------------------------------------------------------------------
# 1) メディア鍵 (実物の encodeMediaKey / decodeMediaKey と同一アルゴリズム)
#    URL をパーセントエンコード → 文字列反転 → Base64URL(パディング無し)
# --------------------------------------------------------------------------
def encode_media_key(url: str) -> str:
    rev = urllib.parse.quote(url, safe="")[::-1]
    b = base64.b64encode(rev.encode("utf-8")).decode("ascii")
    return b.replace("+", "-").replace("/", "_").rstrip("=")


def decode_media_key(key: str) -> str:
    k = (key or "").replace("-", "+").replace("_", "/")
    k += "=" * (-len(k) % 4)
    try:
        rev = base64.b64decode(k).decode("utf-8")
        return urllib.parse.unquote(rev[::-1])
    except Exception:
        return ""


def sign_media_url(url: str, secret: bytes) -> str:
    """実物の `403 Bad URL hash` に相当する改ざん検知用ハッシュ（HMAC-SHA256）"""
    return hmac.new(secret, url.encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def verify_media_url(url: str, signature: str, secret: bytes) -> bool:
    return hmac.compare_digest(sign_media_url(url, secret), (signature or "").lower())


# --------------------------------------------------------------------------
# 2) 仮想 URL 空間 /p/<percent-encoded absolute url>
# --------------------------------------------------------------------------
def proxy_path(prefix: str, url: str) -> str:
    return prefix + urllib.parse.quote(url, safe="")


def unproxy_path(prefix: str, path: str) -> str:
    raw = path[len(prefix):] if path.startswith(prefix) else path
    return urllib.parse.unquote(raw)


def is_http_url(url: str) -> bool:
    try:
        p = urllib.parse.urlsplit(url)
        return p.scheme in ("http", "https") and bool(p.netloc)
    except Exception:
        return False


def absolute(base: str, ref: str) -> str:
    if not ref:
        return ""
    try:
        return urllib.parse.urljoin(base, ref.strip())
    except Exception:
        return ref


# --------------------------------------------------------------------------
# 3) バイナリフレーム（実物 http-relay と同一フォーマット）
#    [4byte BE: メタJSON長][メタJSON(utf-8)][ボディ]
# --------------------------------------------------------------------------
def pack_frame(meta: dict[str, Any], body: bytes = b"") -> bytes:
    meta_bytes = json.dumps(meta, separators=(",", ":")).encode("utf-8")
    return struct.pack(">I", len(meta_bytes)) + meta_bytes + body


def unpack_frame(raw: bytes) -> tuple[dict[str, Any], bytes]:
    if len(raw) < 4:
        raise ValueError("short buffer")
    meta_len = struct.unpack(">I", raw[:4])[0]
    if len(raw) < 4 + meta_len:
        raise ValueError("truncated meta")
    meta = json.loads(raw[4:4 + meta_len].decode("utf-8"))
    return meta, raw[4 + meta_len:]
