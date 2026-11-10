"""ヘッドレスブラウザでビューア経由の描画を確認するスモークテスト

使い方: python3 scripts/browser_check.py http://127.0.0.1:8090
"""
from __future__ import annotations

import sys

from playwright.sync_api import sync_playwright

TARGET = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8090"
WAIT_MS = int(sys.argv[2]) if len(sys.argv) > 2 else 25000


def main() -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        errors: list[str] = []
        page.on("console", lambda m: errors.append(f"[{m.type}] {m.text}") if m.type in ("error", "warning") else None)
        page.on("pageerror", lambda e: errors.append(f"[pageerror] {e}"))

        page.goto(TARGET, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(WAIT_MS)

        info = page.evaluate(
            """() => {
              const f = document.getElementById('frame');
              let inner = null;
              try {
                const d = f.contentDocument;
                inner = d ? {
                  title: d.title,
                  bodyLen: (d.body ? d.body.innerText : '').trim().length,
                  imgs: d.querySelectorAll('img').length,
                  scripts: d.querySelectorAll('script').length,
                  links: d.querySelectorAll('a').length,
                  firstText: (d.body ? d.body.innerText : '').slice(0, 160).replace(/\\n/g, ' | '),
                } : null;
              } catch (e) { inner = { error: String(e) }; }
              return {
                coverHidden: document.getElementById('cover').classList.contains('hidden'),
                status: document.getElementById('status').innerText,
                url: document.getElementById('url').value,
                frameSrc: f.getAttribute('src'),
                inner,
              };
            }"""
        )
        page.screenshot(path="/home/user/instagram-proxy/scripts/smoke.png", full_page=False)
        print("cover hidden :", info["coverHidden"])
        print("status       :", info["status"])
        print("frame src    :", (info["frameSrc"] or "")[:80])
        print("iframe       :", info["inner"])
        print("console      :", errors[:6] if errors else "no errors")
        browser.close()
        ok = info["coverHidden"] and (info["inner"] or {}).get("bodyLen", 0) > 40
        print("\nRESULT:", "OK" if ok else "FAILED")
        return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
