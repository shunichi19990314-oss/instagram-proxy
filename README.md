# instagram-proxy — Instagram 用 Web プロキシ（FastAPI 実装）

> リポジトリ: https://github.com/shunichi19990314-oss/instagram-proxy
> Railway へのデプロイ: **[DEPLOY_RAILWAY.md](DEPLOY_RAILWAY.md)**（`PROXY_MEDIA_KEY` の設定が必要）

`https://miruhukaka.ai.studio/instagram/` の解析結果をもとに、**同じ外形的仕様**（エンドポイント名・バイナリフレーム・メディア鍵・署名検証・Cookie ジャー・診断ログ）を **サーバーサイド実装**に置き換えて再現したコードです。

本家はブラウザ内で Scramjet（WASM）＋ WISP（WebSocket）を回す構成ですが、こちらは **Python の ASGI サーバーが“書き換え”と“中継”を担当**するため、依存が少なく読み解きやすい形になっています。

```
ブラウザ
 └ viewer.html（iframe + 22 秒ヘルスチェック + 3 回自動復旧 + URL バー同期）
      └ GET /p/<encoded-url>                ← HTML/CSS/JS をその場で書換
      └ POST /instagram/runtime/http-relay  ← [4byte len][meta JSON][body] のバイナリリレー
      └ GET  /instagram/runtime/media-relay?k=…&s=…  ← 署名 + キャッシュ + Range 対応
      └ POST /_diag/instagram               ← クライアントログ
           │
           └ proxy/fetch.py ── httpx.AsyncClient ──> Instagram / Meta
               （Cookie ジャーはセッション単位でサーバー側に保持）
```

---

## 0. Railway にデプロイする

GitHub にプッシュして Railway に繋ぐだけで動きます（Dockerfile ビルド）。

```bash
git init -b main && git add . && git commit -m "feat: instagram proxy"
gh repo create instagram-proxy --private --source=. --remote=origin --push
```

Railway: **Deploy from GitHub repo** → `PROXY_MEDIA_KEY` を設定 → **Generate Domain**。
詳細は **[DEPLOY_RAILWAY.md](DEPLOY_RAILWAY.md)** を参照。

## 1. クイックスタート

```bash
cd instagram-proxy
python3 -m pip install -r requirements.txt

# 本番想定（Instagram/Meta のホストのみ許可）
python3 run.py --port 8000

# ローカル検証（任意の URL を中継。プライベート IP も許可）
python3 run.py --port 8000 --mode open --allow-private
```

ブラウザで `http://localhost:8000/` を開くとビューアが起動します。

テスト:

```bash
python3 -m pytest -q        # 22 tests（ユニット 13 + E2E 9）
```

| テスト | 内容 |
|---|---|
| `tests/test_unit.py` | URL 変換・メディア鍵・HMAC 署名・フレーム・HTML/CSS/JS 書換・Cookie ジャー（domain/path/secure/期限）・ホスト許可 |
| `tests/test_e2e.py` | ダミー上流サーバーを立て、書換・リダイレクト・**Cookie のセッション分離**・バイナリリレー（GET/POST）・メディア署名検証・SSRF/許可外 403・診断ログ・セッション API |

---

## 2. エンドポイント仕様

### `GET /p/<percent-encoded 絶対URL>`
透過プロキシ本体。本家の `/instagram/~/sj/<encoded>` に相当。

- `text/html` … 属性（href/src/srcset/action/poster/style/…）を書換、`<base>` と CSP の `<meta>` を除去、`integrity`/`crossorigin`/`nonce` を削除（書換で無効になるため）、最小シム JS を `<head>` に注入
- `text/css` … `url()` / `@import` を書換
- `javascript` … 文字列リテラル内の絶対 URL を書換
- 画像/動画 CDN（`cdninstagram.com` `fbcdn.net`）宛は **`media-relay` に寄せる**（署名付き・キャッシュ可）
- `301/302/303/307/308` の `Location` も仮想 URL に付け替え
- 各メソッド（GET/POST/PUT/PATCH/DELETE/OPTIONS）対応

### `POST /instagram/runtime/http-relay`
本家と同一のバイナリフレーム。

```
request  : [4byte BE: meta長][meta JSON][body]
meta     = {"url":"https://www.instagram.com/api/graphql","method":"POST","headers":[["content-type","application/json"]]}
response : [4byte BE][meta JSON(status/statusText/headers)][body]
```

- 未対応フレームは `400 Bad frame`
- 上流 `Set-Cookie` は meta の headers に含めて返し、サーバー側ジャーにも取り込み

### `GET|HEAD /instagram/runtime/media-relay?k=<key>&s=<hmac>`
本家の `403 Bad URL hash` と同じ思想。

- `k` = URL をパーセントエンコード → 反転 → Base64URL（本家と**同一アルゴリズム**）
- `s` = `HMAC-SHA256(key, url)` の先頭 32 hex（本家の「Bad URL hash」に相当する改ざん検知）
- 署名不一致 → `403 Bad URL hash` / 対象外ホスト → `403 disallowed media host`
- `Range` / `Accept` を転送（動画のシーク対応）、`Access-Control-Allow-Origin: *`

> 本家の署名が何で計算されているかは公開コードからは特定できませんでした（URL ハッシュ検証があることだけ確認）。ここでは HMAC-SHA256 で実装しています。`PROXY_MEDIA_KEY` で鍵を変更できます。

### `POST /_diag/instagram` → `204`
`{source, level, event, seq, ts, transport, details}` を受け取り、サーバーメモリ（直近 500 件）に保存。`GET` で一覧。

### その他
| パス | 用途 |
|---|---|
| `GET /` | ビューア（`viewer.html`） |
| `GET /health` | 稼働状況・統計・セッション数 |
| `GET /session` | 現在のセッション ID と保持 Cookie（`sessionid` の有無） |
| `DELETE /session` | サーバー側 Cookie を破棄（ログアウト相当） |
| `GET /api/media-url?url=…` | 署名付きメディア URL を生成 |
| `CONNECT host:443` | WebSocket/ストリーム用トンネル |

---

## 3. 本家との対応表

| 本家（miruhukaka.ai.studio/instagram/） | 本実装 |
|---|---|
| Scramjet（WASM）で HTML/JS を書換 | `proxy/rewriter.py`（サーバー側で書換） |
| Scramjet `cookieJar` + localStorage `__sh_cookie_vault_instagram` | `proxy/cookies.py` のサーバー側ジャー + `spsid` Cookie（HttpOnly） |
| `/instagram/runtime/http-relay`（第 1 経路） | 同パス・同フォーマットで実装 |
| WISP + libcurl/epoxy フォールバック | 不要（サーバー中継）／`CONNECT` トンネルで代替 |
| `/instagram/runtime/media-relay?k=` | 同パス + `s=` 署名 |
| `IG_MEDIA_SUFFIXES = ["cdninstagram.com","fbcdn.net"]` | `config.MEDIA_SUFFIXES` |
| `/_diag/instagram` | 同パス（204） |
| `syncInputReactState()`（React 入力同期） | `viewer.html` に同ロジック移植 |
| 22 秒ヘルスチェック / 最大 3 回復旧 | `viewer.html` に同ロジック移植 |
| `<title>Google` 偽装・F5 ガード | **意図的に未実装**（フィルタ回避目的のため） |

---

## 4. 設定（環境変数）

| 変数 | 既定 | 説明 |
|---|---|---|
| `PROXY_MODE` | `instagram` | `instagram`（Meta 系のみ許可）/ `open`（任意ホスト・検証用） |
| `PROXY_ALLOW_PRIVATE` | `0` | `1` でプライベート IP 宛も許可（SSRF ガード解除） |
| `PROXY_ALLOWED_HOSTS` | 空 | 追加で許可するホスト（カンマ区切り） |
| `PROXY_MEDIA_KEY` | `schoolhub-instagram-media-key` | メディア署名鍵（**必ず変更してください**） |
| `PROXY_UA` | Chrome 131 / Windows | 上流へ送る User-Agent |
| `PROXY_TIMEOUT` / `PROXY_CONNECT_TIMEOUT` | 30 / 10 | 上流タイムアウト（秒） |
| `PROXY_MEDIA_RELAY` | `1` | メディアを `media-relay` 経由にする |
| `PROXY_DEBUG` | `0` | 診断ログを標準出力へ |

CLI: `python3 run.py --port 8000 --mode open --allow-private --debug`

---

## 5. セキュリティ設計（本家との違い）

本家の弱点を踏まえて、以下を実装しています。

1. **SSRF ガード** … ホスト許可リスト（`instagram` モード）＋ DNS 解決後の **プライベート/ループバック/リンクローカル/予約 IP を遮断**（`fetch.assert_safe_target`）
2. **リレーの悪用防止** … `open` モードは既定で無効。`media-relay` は HMAC 署名必須・対象ホスト限定
3. **Cookie のセッション分離** … httpx の共有ジャーを毎リクエスト破棄し、`spsid` ごとのサーバー側ジャーだけを使う（E2E テストで検証済み）
4. **Cookie を localStorage に置かない** … 本家は `sessionid` を**平文で localStorage** に保存していたが、こちらは HttpOnly Cookie + サーバー側保持
5. **ホップバイホップヘッダ除去**・`Set-Cookie` の扱い・`content-length` 再計算
6. **入力検証** … フレーム長・URL スキーム（http/https のみ）・クエリ検証

---

## 6. 制限・既知の挙動

- Instagram は **データセンター IP / 既定 UA / Cookie 不整合を厳しく検査**します。ログインが必要な画面は「成功する時としない時がある」「しばらくすると 401/429 やチェックポイント要求」になりがちです（本家が復旧機構を 3 回持っている理由でもあります）。
- 書換は**正規表現ベース**（本家の Scramjet は WASM でストリーム書換）。Instagram の巨大な React バンドルは概ね通りますが、`srcdoc`・`import()` の動的 URL・Service Worker 内の fetch までは追いません。
- **圧縮**（gzip/deflate）は自前で解凍。`br` は環境に `brotli` がある場合のみ有効（`requirements.txt` に含めています）。
- WebSocket は `CONNECT` トンネルでのみ対応（ブラウザ JS からは直接張れません）。

---

## 7. 使う前に（重要）

- **Meta の利用規約はプロキシ経由のアクセスや自動取得を禁じています**。自分のアカウントでの私的検証の範囲を超える利用（公開サービスとしての提供、収益化、大量アクセス）は規約違反・著作権・不正アクセス関連のリスクがあります。
- 学校・職場・家族のフィルタリングを回避する目的での配布は行わないでください（本実装は `<title>` 偽装や F5 ガードのような回避工作を意図的に含めていません）。
- 本番で動かす場合は `PROXY_MEDIA_KEY` を変更し、`--mode instagram` のまま、リバースプロキシ配下でレート制限をかけてください。

---

## 8. ファイル構成

```
instagram-proxy/
├── Dockerfile             Railway / Render / Cloud Run 用
├── railway.json           ビルダー・ヘルスチェック・リスタート設定
├── Procfile               Nixpacks フォールバック
├── .env.example           環境変数の雛形
├── DEPLOY_RAILWAY.md      デプロイ手順
├── run.py                 起動スクリプト（CONNECT 対応 ASGI を uvicorn で起動）
├── viewer.html            ビューア（iframe / ヘルスチェック / 復旧 / URL バー）
├── requirements.txt
├── pytest.ini
├── proxy/
│   ├── config.py          設定・ホスト許可・メディア判定
│   ├── urlmap.py          /p/ 変換・メディア鍵・HMAC・バイナリフレーム
│   ├── cookies.py         Cookie ジャー / セッションストア
│   ├── rewriter.py        HTML・CSS・JS の URL 書換 + シム注入
│   ├── fetch.py           上流取得・SSRF ガード・ヘッダ整形・解凍
│   └── server.py          FastAPI（全エンドポイント + CONNECT トンネル）
└── tests/
    ├── test_unit.py
    └── test_e2e.py
```
