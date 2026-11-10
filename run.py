"""起動スクリプト（CONNECT トンネル対応）"""
from __future__ import annotations

import argparse

import uvicorn

from proxy.config import CFG
from proxy.server import asgi_app


def main() -> None:
    ap = argparse.ArgumentParser(description="instagram-proxy server")
    ap.add_argument("--host", default=CFG.host)
    ap.add_argument("--port", type=int, default=CFG.port)
    ap.add_argument("--mode", choices=["instagram", "open"], default=CFG.mode,
                    help="instagram: Instagram/Meta のみ許可, open: 任意ホスト（検証用）")
    ap.add_argument("--allow-private", action="store_true", help="プライベートIP宛も許可（ローカル検証用）")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    CFG.mode = args.mode
    if args.allow_private:
        CFG.allow_private = True
    CFG.debug = args.debug

    print(f"[instagram-proxy] listening on {args.host}:{args.port}  mode={CFG.mode}")

    # Railway / Render などのリバースプロキシ配下で正しいスキーム・IP を得る
    uvicorn.run(
        asgi_app,
        host=args.host,
        port=args.port,
        log_level="info",
        proxy_headers=True,
        forwarded_allow_ips="*",
        timeout_keep_alive=75,
    )


if __name__ == "__main__":
    main()
