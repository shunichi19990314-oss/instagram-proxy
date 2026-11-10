#!/usr/bin/env bash
# GitHub リポジトリ作成 + push（トークンは .git/config に残しません）
#
# 使い方:
#   GITHUB_USER=yourname GITHUB_TOKEN=ghp_xxx ./scripts/push_to_github.sh [repo-name] [--private]
#
# 必要なトークン権限:
#   - classic PAT : repo（public なら public_repo でも可）
#   - fine-grained: Administration: Read and write（リポジトリ作成）+ Contents: Read and write
set -euo pipefail

REPO="${1:-instagram-proxy}"
VISIBILITY="public"
if [ "${2:-}" = "--private" ]; then VISIBILITY_PRIVATE="true"; else VISIBILITY_PRIVATE="false"; fi

: "${GITHUB_USER:?GITHUB_USER を設定してください}"
: "${GITHUB_TOKEN:?GITHUB_TOKEN を設定してください}"

API="https://api.github.com"
AUTH_HDR="Authorization: Bearer ${GITHUB_TOKEN}"
ACCEPT_HDR="Accept: application/vnd.github+json"

echo "==> トークン検証"
whoami_json=$(curl -sS -H "$AUTH_HDR" -H "$ACCEPT_HDR" "$API/user")
login=$(printf '%s' "$whoami_json" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("login",""))')
if [ -z "$login" ]; then
  echo "!! 認証に失敗しました。トークンの権限/有効期限を確認してください。"
  printf '%s\n' "$whoami_json" | head -5
  exit 1
fi
echo "    authenticated as: $login"

echo "==> リポジトリ ${login}/${REPO} を確認/作成 (private=${VISIBILITY_PRIVATE})"
code=$(curl -sS -o /dev/null -w '%{http_code}' -H "$AUTH_HDR" -H "$ACCEPT_HDR" "$API/repos/${login}/${REPO}")
if [ "$code" = "200" ]; then
  echo "    既存リポジトリを使用します"
else
  payload=$(python3 - "$REPO" "$VISIBILITY_PRIVATE" <<'PY'
import json,sys
print(json.dumps({
  "name": sys.argv[1],
  "private": sys.argv[2] == "true",
  "description": "Instagram web proxy (FastAPI) — proxy + rewriter + Railway deploy config",
  "has_issues": True, "has_wiki": False, "auto_init": False,
}))
PY
)
  created=$(curl -sS -X POST -H "$AUTH_HDR" -H "$ACCEPT_HDR" -d "$payload" "$API/user/repos")
  printf '%s' "$created" | python3 -c 'import json,sys;d=json.load(sys.stdin);print("    created:", d.get("html_url") or d.get("message"))'
fi

echo "==> push"
# トークン付き URL を「使い捨て」で使い、remote には残さない
git remote remove origin 2>/dev/null || true
git remote add origin "https://github.com/${login}/${REPO}.git"
git -c credential.helper= push \
  "https://x-access-token:${GITHUB_TOKEN}@github.com/${login}/${REPO}.git" \
  HEAD:refs/heads/main --force

echo "==> 完了: https://github.com/${login}/${REPO}"
echo "    Railway: https://railway.app/new -> Deploy from GitHub repo -> ${REPO}"
echo "    設定する環境変数: PROXY_MEDIA_KEY (必須), PROXY_MODE=instagram"
echo "    ※ 使い終わったら必ずトークンを失効させてください: https://github.com/settings/tokens"
