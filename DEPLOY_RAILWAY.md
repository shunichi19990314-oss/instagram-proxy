# Railway へのデプロイ手順

このリポジトリは Railway にそのまま載せられる構成にしてあります（Dockerfile ビルド）。

## 0. 構成ファイル

| ファイル | 役割 |
|---|---|
| `Dockerfile` | `python:3.12-slim` / 非 root 実行 / `$PORT` で待受 / `HEALTHCHECK /health` |
| `railway.json` | ビルダー = DOCKERFILE、ヘルスチェック `/health`、失敗時リスタート |
| `Procfile` | Docker を使わない Nixpacks ビルド時のフォールバック（`web: python run.py`） |
| `requirements.txt` | fastapi / uvicorn / httpx / brotli |
| `.env.example` | 設定すべき環境変数の一覧 |

## 1. GitHub へプッシュ

```bash
cd instagram-proxy
git init -b main
git add .
git commit -m "feat: Instagram web proxy (FastAPI) with Railway deploy config"

# GitHub CLI を使う場合（リポジトリ作成 + push を一度に）
gh auth login
gh repo create instagram-proxy --private --source=. --remote=origin --push

# 手動で作った空リポジトリへ push する場合
git remote add origin https://github.com/<YOUR_NAME>/instagram-proxy.git
git push -u origin main
```

> 公開リポジトリにすると「Instagram 用プロキシ」が誰でも使える状態になり、Meta 規約・悪用の観点でリスクが上がります。**Private を推奨**します。

## 2. Railway にデプロイ

### 方法 A: ダッシュボード（簡単）
1. https://railway.app/new → **Deploy from GitHub repo** → 作成したリポジトリを選択
2. Railway が `railway.json` / `Dockerfile` を検出してビルド（初回 2〜4 分）
3. **Settings → Networking → Generate Domain** で公開 URL を発行
4. **Variables** に以下を設定（`.env.example` 参照）

| 変数 | 値（推奨） | 必須 |
|---|---|---|
| `PROXY_MEDIA_KEY` | ランダム 32 文字以上 | ✅ 必ず変更 |
| `PROXY_MODE` | `instagram` | ✅ |
| `PROXY_DEBUG` | `0` | – |
| `PORT` | 設定しない（Railway が注入） | – |

`PROXY_MEDIA_KEY` の生成例:
```bash
python -c "import secrets;print(secrets.token_urlsafe(32))"
```

### 方法 B: CLI
```bash
npm i -g @railway/cli
railway login
railway init            # 新規プロジェクト作成
railway variables --set PROXY_MEDIA_KEY=$(python -c "import secrets;print(secrets.token_urlsafe(32))")
railway up              # カレントをデプロイ
railway domain          # 公開 URL を発行
```

## 3. デプロイ後の確認

```bash
BASE=https://<your-app>.up.railway.app

curl -s $BASE/health                 # {"ok":true,...}
curl -s -o /dev/null -w "%{http_code}\n" $BASE/            # 200（ビューア）
curl -s -o /dev/null -w "%{http_code}\n" "$BASE/p/https%3A%2F%2Fwww.instagram.com%2F"   # 200

# 署名付きメディア URL の生成と取得
curl -s "$BASE/api/media-url?url=https%3A%2F%2Fstatic.cdninstagram.com%2Frsrc.php%2Fyr%2Fr%2FrzWiSjZRxk5.webp"
```

## 4. 注意点（Railway 固有）

- **メモリ**: 無料枠は 512MB 程度。Instagram の HTML は 300〜400KB、CSS は 800KB 級なので、同時アクセスが多いと書換処理でメモリを使います。`numReplicas: 1` のまま、必要なら `PROXY_TIMEOUT` を短くしてください。
- **セッションは揮発**: Cookie ジャーはメモリ保持です。再デプロイ・再起動・スケール時にログイン状態は消えます（本家と同じ挙動）。永続化するなら Redis 等に置き換えてください（`proxy/cookies.py` の `SessionStore` を差し替えるだけです）。
- **IP 共有**: Railway の外向き IP は他の利用者と共有され得ます。Instagram 側でレート制限やチェックポイント要求が起きやすい条件になります。
- **ヘルスチェック**: `/health` は外部通信しないので、上流の不調で Railway が再起動ループに入ることはありません。
- **WebSocket / CONNECT**: Railway は HTTP/1.1・HTTP/2 のプロキシですが、`CONNECT` メソッドは通りません。WebSocket が必要な機能は現状未使用なので影響ありません。

## 5. 代替ホスティング

| サービス | 設定 |
|---|---|
| Render | New → Web Service → Docker。Health Check Path `/health` |
| Fly.io | `fly launch --dockerfile Dockerfile` → `fly deploy` |
| Google Cloud Run | `gcloud run deploy --source .`（`$PORT` 対応済み） |
| Vercel | **不可**（この構成は常駐 ASGI サーバー。Vercel Functions はストリーミング/長時間接続に不向き） |

---

## 使う前に

Meta の利用規約はプロキシ経由のアクセスを禁じています。**自分専用の検証環境**に留め、公開サービス化・収益化・大量アクセスは行わないでください。`PROXY_MODE=instagram`（既定）を維持し、`open` モードで公開しないこと。
