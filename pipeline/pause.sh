#!/usr/bin/env bash
# 수집 일시중지 — .collect-paused 마커 커밋으로 매일 아침 cron의 수집·판정을 스킵
# (배포/완료 처리 푸시는 계속 동작. 재개: pipeline/resume.sh)
# 웹에서도 가능: Actions 탭 → Pipeline Control → pause
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .collect-paused ] && { echo "이미 일시중지 상태입니다"; exit 0; }
git pull -q --rebase
date -u +%FT%TZ > .collect-paused
git add .collect-paused
git commit -q -m "chore: 수집 일시중지"
git push -q
echo "수집 일시중지됨 (.collect-paused 커밋)"
echo "- 매일 아침 cron: 수집·판정 스킵"
echo "- 완료 처리 푸시 배포: 정상 동작"
echo "- 재개: ./pipeline/resume.sh"
