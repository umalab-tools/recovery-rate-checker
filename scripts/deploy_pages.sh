#!/bin/bash
# deploy_pages.sh — umalab-tools/recovery-rate-checker 用の汎用GitHub Pagesデプロイスクリプト
#
# 目的: ローカルで更新した1つのファイル（または複数ファイル）を
#   git add → commit → push → GitHub Pages反映待ち → HTTP 200確認 → 内容確認
#   まで1コマンドで実行する。
#
# 使い方:
#   ./scripts/deploy_pages.sh race-compare.html
#   ./scripts/deploy_pages.sh mpn-go/index.html ankh-go/index.html   （複数ファイルも可）
#
# 前提:
#   - このスクリプトはリポジトリ直下の scripts/ に置く（相対パス解決に利用）
#   - gh CLI がインストール・認証済みであること
#   - 対象ファイルは既にこのリポジトリ内の正しい場所に配置済みであること
#     （このスクリプト自体はファイルの中身を生成・移動しない。add/commit/push/検証のみを行う）
#
# 安全性:
#   - 指定したファイル以外は一切 git add しない（他のUMALABツールやページを壊さない）
#   - リポジトリの再作成・強制上書き（force push等）は行わない
#   - 変更が無いファイルは commit をスキップし、確認フェーズのみ実行する

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PAGES_BASE_URL="https://umalab-tools.github.io/recovery-rate-checker"
BRANCH="main"

if [ "$#" -eq 0 ]; then
  echo "使い方: $0 <ファイルパス> [ファイルパス2 ...]"
  echo "例: $0 race-compare.html"
  exit 1
fi

cd "$REPO_DIR" || { echo "エラー: リポジトリディレクトリに移動できません: $REPO_DIR"; exit 1; }

echo "=== 1. 対象ファイル存在確認 ==="
MISSING=0
for f in "$@"; do
  if [ ! -f "$REPO_DIR/$f" ]; then
    echo "エラー: ファイルが見つかりません: $REPO_DIR/$f"
    MISSING=1
  else
    echo "OK: $f"
  fi
done
if [ "$MISSING" -eq 1 ]; then
  echo "存在しないファイルがあるため中止します。"
  exit 1
fi

echo "=== 2. git / 認証状態確認 ==="
if ! command -v git >/dev/null 2>&1; then
  echo "エラー: git がインストールされていません。"
  exit 1
fi
if ! command -v gh >/dev/null 2>&1; then
  echo "エラー: gh CLI がインストールされていません。"
  exit 1
fi
# 2026-11-03追加: launchd経由の実行でgh認証確認が一時的に失敗することが
# 複数回observed(keychainアクセスのタイミング起因と推定、認証情報自体は
# 正常)。資格情報には一切触れず、確認を数回リトライするだけで解消する
# ケースが多いため、ここで自動再試行する(失敗し続けた場合のみ中止)。
AUTH_OK=0
AUTH_ERR=""
for attempt in 1 2 3; do
  AUTH_ERR="$(gh auth status 2>&1 1>/dev/null)"
  if [ -z "$AUTH_ERR" ]; then
    AUTH_OK=1
    break
  fi
  echo "gh認証確認 ${attempt}/3回目失敗。一時的な問題の可能性があるため再試行します。"
  sleep $((attempt * 10))
done
if [ "$AUTH_OK" -ne 1 ]; then
  echo "エラー: gh CLI が認証されていません（gh auth login が必要、または一時的なkeychainアクセス不可）。3回再試行しましたが解消しませんでした。"
  # 2026-11-03追加: 根本原因調査のため、gh自身が出力した詳細エラーを記録する
  # (これまでは「認証されていません」という固定文言のみでgh自体のエラー内容が
  # 残っていなかった)。資格情報の値そのものは出力しない。
  echo "gh auth statusの詳細出力: $AUTH_ERR"
  exit 1
fi
CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
if [ "$CURRENT_BRANCH" != "$BRANCH" ]; then
  echo "エラー: 現在のブランチが '$CURRENT_BRANCH' です。GitHub Pagesの公開元は '$BRANCH' のため、"
  echo "       '$BRANCH' ブランチで実行してください（自動切替はしません）。"
  exit 1
fi
echo "OK: git=$(git --version | head -1), branch=$CURRENT_BRANCH, gh認証済み"

echo "=== 3. git状態確認 ==="
git status --short -- "$@"

echo "=== 4. git add ==="
git add -- "$@"

echo "=== 5. commit ==="
if git diff --cached --quiet -- "$@"; then
  echo "変更なし（指定ファイルは既にリポジトリの最新状態と一致）。commit/pushをスキップし、公開状態の確認のみ行います。"
  DID_COMMIT=0
else
  COMMIT_MSG="Deploy: $* ($(date '+%Y-%m-%d %H:%M:%S %Z'))"
  git commit -m "$COMMIT_MSG"
  echo "OK: commit作成 — $COMMIT_MSG"
  DID_COMMIT=1
fi

echo "=== 6. push ==="
if [ "$DID_COMMIT" -eq 1 ]; then
  # 2026-11-03追加: ネットワーク一時断(DNS解決失敗等)によるpush失敗を
  # 自動再試行する。コンフリクト等の恒久的な失敗はそのまま3回失敗して中止。
  #
  # 2026-11-14追加: 複数の自動ジョブが同じリポジトリへ同時にpushするため、
  # 「remoteに自分が持っていない変更がある」non-fast-forward拒否が頻発
  # していた(push 3回とも全く同じコミットを再送するだけで、一度も
  # origin/$BRANCHの新しい状態を取り込んでいなかったため、3回とも同じ理由で
  # 失敗していた)。2回目以降の再試行の前にfetch + rebaseで最新のorigin状態を
  # 取り込むことで、この種の一時的な競合を自動的に解消できるようにする。
  # force push・force rebase・履歴の書き換えは行わない。rebaseが
  # コンフリクトした場合は即座にabortし、安全に失敗する(他ジョブの変更を
  # 壊さない)。
  PUSH_OK=0
  for attempt in 1 2 3; do
    if [ "$attempt" -gt 1 ]; then
      echo "push再試行前にfetch + rebaseでoriginの最新状態を取り込みます..."
      if ! git fetch origin "$BRANCH"; then
        echo "fetch失敗。ネットワークの一時的な問題の可能性があるため、そのまま再試行します。"
      elif ! git rebase "origin/$BRANCH"; then
        echo "rebaseでコンフリクトが発生したためabortします（他ジョブの変更を壊さないための安全策）。"
        git rebase --abort
        echo "失敗: リポジトリ側の変更と競合しました。手動確認が必要です。"
        exit 1
      fi
    fi
    if git push origin "$BRANCH"; then
      PUSH_OK=1
      break
    fi
    echo "push ${attempt}/3回目失敗。再試行します。"
    sleep $((attempt * 10))
  done
  if [ "$PUSH_OK" -ne 1 ]; then
    echo "失敗: push できませんでした（権限・ネットワーク・コンフリクトを確認してください）。3回再試行しましたが解消しませんでした。"
    exit 1
  fi
  echo "OK: pushしました"
else
  echo "スキップ（commitなし）"
fi

echo "=== 7. GitHub Pages反映待ち・HTTP 200確認 ==="
url_for_file() {
  local f="$1"
  if [ "$f" = "index.html" ]; then
    echo "$PAGES_BASE_URL/"
  elif [[ "$f" == */index.html ]]; then
    echo "$PAGES_BASE_URL/${f%index.html}"
  else
    echo "$PAGES_BASE_URL/$f"
  fi
}

MAX_WAIT=180   # 秒
INTERVAL=5
FAILED_FILES=()

for f in "$@"; do
  url="$(url_for_file "$f")"
  elapsed=0
  code=""
  while [ "$elapsed" -lt "$MAX_WAIT" ]; do
    code=$(curl -s -o /dev/null -w "%{http_code}" "$url")
    if [ "$code" = "200" ]; then
      echo "OK: $url -> 200 (${elapsed}s)"
      break
    fi
    sleep "$INTERVAL"
    elapsed=$((elapsed + INTERVAL))
  done
  if [ "$code" != "200" ]; then
    echo "NG: $url -> $code（${MAX_WAIT}秒待っても200になりませんでした）"
    FAILED_FILES+=("$f")
  fi
done

if [ "${#FAILED_FILES[@]}" -gt 0 ]; then
  echo ""
  echo "=== 診断: 200にならなかったファイルの原因調査 ==="
  for f in "${FAILED_FILES[@]}"; do
    echo "--- $f ---"
    echo "  リポジトリ内の存在: $([ -f "$REPO_DIR/$f" ] && echo "OK" || echo "見つからない")"
    echo "  git管理下か: $(git ls-files --error-unmatch "$f" >/dev/null 2>&1 && echo "OK（トラッキング済み）" || echo "NG（gitに追加されていない可能性）")"
    echo "  現在のbranch: $CURRENT_BRANCH（Pages公開元と一致: $([ "$CURRENT_BRANCH" = "$BRANCH" ] && echo "OK" || echo "NG")）"
  done
  echo ""
  echo "Pages公開設定を確認するには: gh api repos/umalab-tools/recovery-rate-checker/pages"
  echo "結果: 一部失敗"
  exit 1
fi

echo ""
echo "=== 8. 内容確認（先頭のtitleタグを表示） ==="
for f in "$@"; do
  url="$(url_for_file "$f")"
  title=$(curl -s "$url" | grep -o '<title>[^<]*</title>' | head -1)
  echo "$url"
  echo "  $title"
done

echo ""
echo "=== デプロイ成功: 全ファイルでHTTP 200・内容確認済み ==="
