"""小さな TTL キャッシュ（429 対策・重複リクエストの抑止）

Instagram は `/ajax/bulk-route-definitions/` などを短時間に何度も叩くと 429 を返す。
同一 URL の多重実行を 1 本にまとめ（single-flight）、成功レスポンスを再利用し、
上流が 429/5xx のときはキャッシュを返す（stale-while-error）。
"""
from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from .config import CFG


@dataclass
class Entry:
    value: object
    expires_at: float


class TTLCache:
    def __init__(self, max_items: int = 512) -> None:
        self._data: dict[str, Entry] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._max = max_items

    def _evict(self) -> None:
        now = time.time()
        stale = [k for k, v in self._data.items() if v.expires_at < now]
        for k in stale:
            self._data.pop(k, None)
        if len(self._data) > self._max:
            oldest = sorted(self._data.items(), key=lambda kv: kv[1].expires_at)
            for k, _ in oldest[: len(self._data) - self._max]:
                self._data.pop(k, None)

    def get(self, key: str, allow_stale: bool = False):
        e = self._data.get(key)
        if not e:
            return None
        if e.expires_at < time.time() and not allow_stale:
            return None
        return e.value

    def set(self, key: str, value, ttl: float) -> None:
        self._data[key] = Entry(value, time.time() + ttl)
        self._evict()

    async def run(self, key: str, factory: Callable[[], Awaitable]):
        """同一キーの同時実行を 1 本化する"""
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            return await factory()


CACHE = TTLCache()

# キャッシュ対象（Instagram の SPA が毎回叩く重いエンドポイント）
CACHE_RULES: tuple[tuple[str, float], ...] = (
    ("/ajax/bulk-route-definitions/", 300.0),
    ("/ajax/bulk-route-definitions_async/", 300.0),
    ("/ajax/", 120.0),
    (".js?", 300.0),
    (".css?", 300.0),
)


def cache_ttl_for(url: str, method: str) -> float:
    if method not in ("GET", "POST"):
        return 0.0
    low = url.lower()
    if any(k in low for k in ("/api/graphql", "/accounts/login", "/accounts/password",
                              "/ajax/bz", "/ajax/qm", "/logging")):
        return 0.0
    for needle, ttl in CACHE_RULES:
        if needle in low:
            return ttl
    return 0.0


def cache_key(url: str, method: str, cookie_header: str, body: bytes = b"") -> str:
    h = hashlib.sha256(cookie_header.encode()).hexdigest()[:16]
    # ルート定義はリクエストボディ違いでも内容は同じなのでまとめる
    if "bulk-route-definitions" in url:
        return f"{method}:{url}:{h}"
    b = hashlib.sha256(body or b"").hexdigest()[:12]
    return f"{method}:{url}:{b}:{h}"


# 429 でも「空レスポンスで継続させる」パス（SPA の起動を止めない）
SOFT_FAIL_PATHS: tuple[tuple[str, bytes, str], ...] = (
    ("/ajax/bulk-route-definitions", b'{"payloads":{}}', "application/json"),
    ("/ajax/bulk-route-definitions_async", b'{"payloads":{}}', "application/json"),
)
