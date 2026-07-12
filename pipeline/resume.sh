#!/usr/bin/env bash
# 수집 재개 — pause.sh로 중지한 수집을 다시 활성화
# 웹에서도 가능: Actions 탭 → Pipeline Control → resume
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .collect-paused ] || { echo "이미 수집 동작 중입니다"; exit 0; }
git pull -q --rebase
git rm -q .collect-paused
git commit -q -m "chore: 수집 재개"
git push -q
echo "수집 재개됨 — 다음 아침 cron부터 정상 수집"
echo "- 지금 즉시 1회 수집하려면: gh workflow run daily.yml"
