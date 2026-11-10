"""設定（環境変数 / CLI 引数で上書き可能）"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# 実物サイトの IG_MEDIA_SUFFIXES 相当
MEDIA_SUFFIXES: tuple[str, ...] = ("cdninstagram.com", "fbcdn.net")

# mode=instagram のときに許可するホスト（サブドメインは末尾一致で許可）
INSTAGRAM_SUFFIXES: tuple[str, ...] = (
    "instagram.com",
    "cdninstagram.com",
    "fbcdn.net",
    "facebook.com",
    "fb.com",
)


@dataclass
class Config:
    # --- サーバー ---
    # Railway / Render / Fly などは PORT を注入してくるので優先して読む
    host: str = os.getenv("PROXY_HOST", os.getenv("HOST", "0.0.0.0"))
    port: int = int(os.getenv("PROXY_PORT", os.getenv("PORT", "8000")))

    # --- ポリシー ---
    # instagram : Instagram/Meta 関連ホストのみ許可（本番想定）
    # open      : 任意ホスト（ローカル検証用。allow_private と併用）
    mode: str = os.getenv("PROXY_MODE", "instagram")
    allow_private: bool = os.getenv("PROXY_ALLOW_PRIVATE", "0") == "1"
    allowed_hosts: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            h.strip() for h in os.getenv("PROXY_ALLOWED_HOSTS", "").split(",") if h.strip()
        )
    )
    media_suffixes: tuple[str, ...] = MEDIA_SUFFIXES
    host_suffixes: tuple[str, ...] = INSTAGRAM_SUFFIXES

    # --- メディアリレーの署名鍵（実物の "Bad URL hash" 相当）---
    hash_key: bytes = os.getenv(
        "PROXY_MEDIA_KEY", "schoolhub-instagram-media-key"
    ).encode()

    # --- HTTP ---
    user_agent: str = os.getenv("PROXY_UA", DEFAULT_UA)
    accept_language: str = os.getenv("PROXY_AL", "ja,en-US;q=0.9,en;q=0.8")
    upstream_timeout: float = float(os.getenv("PROXY_TIMEOUT", "30"))
    connect_timeout: float = float(os.getenv("PROXY_CONNECT_TIMEOUT", "10"))
    referer_for_media: str = "https://www.instagram.com/"
    max_retries: int = int(os.getenv("PROXY_MAX_RETRIES", "2"))
    # 上流（Instagram）への出口プロキシ。データセンター IP が 429 になる場合に必要
    #   例: socks5://user:pass@host:1080 / http://host:8080
    upstream_proxy: str = os.getenv("PROXY_UPSTREAM_PROXY", "")

    # --- セッション ---
    default_target: str = os.getenv("PROXY_DEFAULT_TARGET", "https://www.instagram.com/")
    session_ttl: int = 6 * 3600
    max_sessions: int = 2000
    cookie_name: str = "spsid"

    # --- ルーティング ---
    # 同一ホストのリンクは「元のパスのまま」返し（SPA のルーターを壊さない）、
    # 別ホストへの参照だけ /_ext/<encoded> に退避する “sticky host” 方式。
    ext_prefix: str = "/_ext/"                           # 別ホスト用の明示プロキシ
    prefix: str = "/p/"                                  # 互換エイリアス（絶対URL指定）
    http_relay: str = "/instagram/runtime/http-relay"    # バイナリリレー
    media_relay: str = "/instagram/runtime/media-relay"  # メディア専用
    diag: str = "/_diag/instagram"

    # --- その他 ---
    debug: bool = os.getenv("PROXY_DEBUG", "0") == "1"
    max_inspect_bytes: int = 8 * 1024 * 1024  # 書換対象としてバッファする上限

    # メディア系ホストは /p/ ではなく media-relay（キャッシュ・署名付き）へ寄せる
    media_via_relay: bool = os.getenv("PROXY_MEDIA_RELAY", "1") == "1"
    # 同一ホストのリンクを元パスのまま返す（SPA を壊さない）。0 で従来の /p/ 方式
    sticky_host: bool = os.getenv("PROXY_STICKY_HOST", "1") == "1"

    def host_allowed(self, host: str) -> bool:
        h = (host or "").lower().rstrip(".")
        if not h:
            return False
        if self.mode == "open":
            return True
        if h in self.allowed_hosts:
            return True
        return any(h == s or h.endswith("." + s) for s in self.host_suffixes)

    def is_media_host(self, host: str) -> bool:
        h = (host or "").lower().rstrip(".")
        return any(h == s or h.endswith("." + s) for s in self.media_suffixes)


CFG = Config()
