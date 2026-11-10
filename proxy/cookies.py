"""サーバー側 Cookie ジャー（本物はブラウザ側の cookieJar + localStorage vault）"""
from __future__ import annotations

import threading
import time
import uuid
from http.cookies import SimpleCookie
from typing import Iterable

from .config import CFG


class CookieJar:
    __slots__ = ("cookies", "updated")

    def __init__(self) -> None:
        self.cookies: dict[tuple[str, str], dict[str, str]] = {}
        self.updated = time.time()

    # ---- 取り込み ----
    def set_cookie(self, set_cookie_value: str, request_url: str) -> None:
        try:
            sc = SimpleCookie()
            sc.load(set_cookie_value)
        except Exception:
            return
        default_domain = urllib_netloc(request_url)
        for name, morsel in sc.items():
            domain = (morsel["domain"] or default_domain).lstrip(".").lower()
            path = morsel["path"] or "/"
            value = morsel.value
            max_age = morsel["max-age"]
            expires = morsel["expires"]
            # 削除指示
            if (max_age and max_age.strip() in ("0", "-1")) or _is_past(expires):
                self.cookies.pop((domain, name), None)
                continue
            self.cookies[(domain, name)] = {
                "value": value,
                "path": path,
                "domain": domain,
                "secure": "1" if morsel["secure"] else "0",
                "expires": expires or "",
            }
        self.updated = time.time()

    # ---- 送信 ----
    def header_for(self, url: str) -> str:
        host = urllib_host(url)
        path = urllib_path(url)
        secure = url.lower().startswith("https:")
        out: list[str] = []
        for (domain, name), c in self.cookies.items():
            if not _domain_match(host, domain):
                continue
            if not path.startswith(c["path"]):
                continue
            if c["secure"] == "1" and not secure:
                continue
            if _is_past(c["expires"]):
                continue
            out.append(f"{name}={c['value']}")
        return "; ".join(out)

    # ---- 管理 ----
    def dump(self) -> list[dict]:
        return [
            {"domain": d, "name": n, **c} for (d, n), c in self.cookies.items()
        ]

    def clear(self) -> None:
        self.cookies.clear()
        self.updated = time.time()

    def has_auth(self) -> bool:
        return any(n in ("sessionid", "ds_user_id") for (_, n) in self.cookies)


class SessionStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, tuple[CookieJar, float]] = {}
        self._hits = {"created": 0, "reused": 0}

    def gen_id(self) -> str:
        return "sps-" + uuid.uuid4().hex[:24]

    def get(self, sid: str | None, create: bool = True) -> tuple[str, CookieJar]:
        now = time.time()
        with self._lock:
            if sid and sid in self._sessions:
                jar, _ = self._sessions[sid]
                self._sessions[sid] = (jar, now)
                self._hits["reused"] += 1
                return sid, jar
            new = sid if (sid and sid.startswith("sps-")) else self.gen_id()
            jar = CookieJar()
            self._sessions[new] = (jar, now)
            self._hits["created"] += 1
            if len(self._sessions) > CFG.max_sessions:
                self._evict(now)
            return new, jar

    def delete(self, sid: str) -> bool:
        with self._lock:
            return self._sessions.pop(sid, None) is not None

    def stats(self) -> dict:
        with self._lock:
            now = time.time()
            return {
                "sessions": len(self._sessions),
                "created": self._hits["created"],
                "reused": self._hits["reused"],
                "oldest_age_sec": round(
                    max((now - ts for _, ts in self._sessions.values()), default=0.0), 1
                ),
            }

    def _evict(self, now: float) -> None:
        stale = [
            sid for sid, (_, ts) in self._sessions.items()
            if now - ts > CFG.session_ttl
        ]
        for sid in stale:
            self._sessions.pop(sid, None)
        if len(self._sessions) > CFG.max_sessions:
            oldest = sorted(self._sessions.items(), key=lambda kv: kv[1][1])
            for sid, _ in oldest[: len(self._sessions) - CFG.max_sessions]:
                self._sessions.pop(sid, None)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def urllib_netloc(url: str) -> str:
    from urllib.parse import urlsplit

    return urlsplit(url).hostname or ""


def urllib_host(url: str) -> str:
    from urllib.parse import urlsplit

    return (urlsplit(url).hostname or "").lower()


def urllib_path(url: str) -> str:
    from urllib.parse import urlsplit

    p = urlsplit(url).path or "/"
    return p


def _domain_match(host: str, domain: str) -> bool:
    host = host.lower()
    domain = domain.lower().lstrip(".")
    return host == domain or host.endswith("." + domain)


def _is_past(expires: str) -> bool:
    if not expires:
        return False
    from email.utils import parsedate_to_datetime

    try:
        return parsedate_to_datetime(expires).timestamp() < time.time()
    except Exception:
        return False


def merge_set_cookie(values: Iterable[str], url: str, jar: CookieJar) -> None:
    for v in values:
        jar.set_cookie(v, url)
