#!/usr/bin/env python3
"""오래된 '학습' 항목 자동 완료 처리.

학습 판정은 당장 할 일이 없는 읽을거리라 방치하면 대기 목록에 무한히 쌓인다.
EXPIRE_DAYS(기본 14일)가 지난 학습·대기 항목을 완료로 전환해 홈을 비운다.
즉시조치·백로그는 사람이 처리 여부를 판단해야 하므로 건드리지 않는다.

Usage:
    python pipeline/expire.py [--dry-run]
"""

import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONTENT_DIR = ROOT / "content" / "insights"


def main() -> int:
    days = int(os.environ.get("EXPIRE_DAYS", "14"))
    dry_run = "--dry-run" in sys.argv
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    expired = []
    for path in sorted(CONTENT_DIR.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        if not re.search(r'^verdict: "학습"$', text, re.M):
            continue
        if not re.search(r'^status: "대기"$', text, re.M):
            continue
        m = re.search(r"^date: (.+)$", text, re.M)
        if not m:
            continue
        try:
            posted = datetime.fromisoformat(m.group(1).strip())
        except ValueError:
            continue
        if posted.tzinfo is None:
            posted = posted.replace(tzinfo=timezone.utc)
        if posted < cutoff:
            if not dry_run:
                path.write_text(
                    text.replace('status: "대기"', 'status: "완료"', 1),
                    encoding="utf-8",
                )
            expired.append(path.name)

    suffix = " (dry-run)" if dry_run else ""
    print(f"학습 자동 만료({days}일 경과): {len(expired)}건{suffix}")
    for name in expired:
        print(f"  - {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
