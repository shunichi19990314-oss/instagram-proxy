#!/usr/bin/env bash
# GitHub へプッシュするためのヘルパー
# 使い方:  GITHUB_USER=<あなたのユーザー名> ./push.sh [repo-name]
set -euo pipefail

USER_NAME="${GITHUB_USER:-}"
REPO="${1:-instagram-proxy}"

if [ -z "$USER_NAME" ]; then
  echo "GITHUB_USER を設定してください:  GITHUB_USER=yourname ./push.sh"
  exit 1
fi

if ! git remote get-url origin >/dev/null 2>&1; then
  git remote add origin "https://github.com/${USER_NAME}/${REPO}.git"
fi

echo "==> push to $(git remote get-url origin)"
git push -u origin main
echo "==> done. Railway: https://railway.app/new  ->  Deploy from GitHub repo"
