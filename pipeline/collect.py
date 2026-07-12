#!/usr/bin/env python3
"""Daily insight pipeline.

YouTube InnerTube 검색(API 키 불필요)으로 급상승 쇼츠/영상을 수집하고
VPH(시간당 조회수)를 계산한 뒤, Claude가 context.md 기준 "행동 판정"을 내려
무관이 아닌 항목만 Hugo 포스트로 생성한다. (RSS/Reddit/HN/GitHub 수집기도
feeds.yaml에 소스를 추가하면 그대로 동작한다.)

Usage:
    python pipeline/collect.py [--dry-run]

Env:
    JUDGE_BACKEND            "claude-code" | "api" (기본: 자동 — claude CLI가 있으면
                             claude-code, 없으면 api)
    CLAUDE_CODE_OAUTH_TOKEN  claude-code 백엔드 CI 인증 (claude setup-token으로 발급,
                             로컬은 claude 로그인 세션 사용)
    ANTHROPIC_API_KEY        api 백엔드 필수
    CLAUDE_MODEL             판정 모델 (기본 claude-sonnet-4-6)
    MAX_ITEMS                1회 실행당 판정 최대 건수 (기본 30)
    GITHUB_TOKEN             선택 — GitHub Search API rate limit 완화
"""

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser
import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent
FEEDS_FILE = ROOT / "feeds.yaml"
CONTEXT_FILE = ROOT / "context.md"
PROCESSED_FILE = ROOT / "pipeline" / "processed.json"
CONTENT_DIR = ROOT / "content" / "insights"

USER_AGENT = "insight-pipeline/1.0"
FRESH_HOURS = 48          # RSS: 최근 48시간 항목만
PROCESSED_TTL_DAYS = 90   # 처리 기록 보존 기간
SUMMARY_MAX_CHARS = 1500  # 판정 프롬프트에 넣는 본문 상한

VERDICTS = ("즉시조치", "백로그", "학습", "무관")

JUDGE_PROMPT = """아래 항목을 읽고 반드시 다음 JSON 형식으로만 답하라. 다른 텍스트 금지.

{{"verdict": "즉시조치|백로그|학습|무관",
 "reason": "급상승 근거(VPH/조회수)와 내 채널 소재 중 어디에 해당하는지 1줄 (무관이면 빈 문자열)",
 "action": "이번 주 안에 할 구체적 작업 1개 — 벤치마킹할 소재/후킹/포맷 수준으로 구체적으로 (무관이면 빈 문자열)",
 "tags": ["kebab-case-태그", "최대 3개"],
 "title_ko": "한국어 요약 제목"}}

판정 기준:
- 즉시조치: VPH가 매우 높은 초급상승 영상이고 context의 제작 여건으로 소재/포맷을
  재현(벤치마킹)할 수 있음 — 액션은 "어떤 쇼츠/영상을 어떤 후킹으로 만들지" 수준으로 구체적으로
- 백로그: 급상승이지만 재현 난이도가 높거나 당장 만들 소재가 아님
- 학습: 재현 대상은 아니나 후킹/편집/썸네일 등 context의 "관심 분야"에서 배울 점이 뚜렷함
- 무관: 그 외 전부. context의 "명시적 제외" 항목(기획사 MV, 방송사 클립, 스포츠 중계 등)은
  반드시 무관. 억지로 인사이트를 만들지 말 것

제목: {title}
출처: {source_name}
링크: {url}
본문/요약: {summary}"""


def log(msg: str) -> None:
    print(msg, flush=True)


def url_hash(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def slugify(title: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", title.lower()).strip("-")
    if not slug:
        slug = "item"
    return slug[:60].rstrip("-")


# ---------------------------------------------------------------- collection

def collect_rss(feeds: list) -> list:
    items = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=FRESH_HOURS)
    for feed in feeds:
        try:
            parsed = feedparser.parse(
                feed["url"], agent=USER_AGENT, request_headers={"Accept": "*/*"}
            )
            count = 0
            for e in parsed.entries:
                ts = e.get("published_parsed") or e.get("updated_parsed")
                if ts:
                    published = datetime.fromtimestamp(time.mktime(ts), tz=timezone.utc)
                    if published < cutoff:
                        continue
                link = e.get("link", "")
                if not link:
                    continue
                items.append({
                    "title": strip_html(e.get("title", "(no title)")),
                    "url": link,
                    "summary": strip_html(e.get("summary", ""))[:SUMMARY_MAX_CHARS],
                    "source_name": feed["name"],
                })
                count += 1
            log(f"  [rss] {feed['name']}: {count}건")
        except Exception as exc:  # noqa: BLE001 — 소스 하나가 전체를 죽이면 안 됨
            log(f"  [rss] {feed['name']}: 실패 ({exc})")
    return items


def collect_reddit(subs: list) -> list:
    items = []
    for sub in subs:
        url = (
            f"https://www.reddit.com/r/{sub['subreddit']}/{sub.get('listing', 'top')}.json"
            f"?t={sub.get('t', 'day')}&limit=25"
        )
        try:
            resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=15)
            resp.raise_for_status()
            count = 0
            for child in resp.json().get("data", {}).get("children", []):
                post = child.get("data", {})
                if post.get("score", 0) < sub.get("min_score", 0):
                    continue
                if post.get("stickied"):
                    continue
                permalink = "https://www.reddit.com" + post.get("permalink", "")
                summary = strip_html(post.get("selftext", ""))[:SUMMARY_MAX_CHARS]
                if not summary and post.get("url"):
                    summary = f"링크 포스트: {post['url']}"
                items.append({
                    "title": post.get("title", "(no title)"),
                    "url": permalink,
                    "summary": summary,
                    "source_name": f"r/{sub['subreddit']}",
                })
                count += 1
            log(f"  [reddit] r/{sub['subreddit']}: {count}건")
        except Exception as exc:  # noqa: BLE001
            log(f"  [reddit] r/{sub['subreddit']}: 실패 ({exc})")
    return items


def collect_hackernews(queries: list) -> list:
    items = []
    for q in queries:
        url = f"https://hnrss.org/newest?q={requests.utils.quote(q['query'])}&points={q.get('min_points', 50)}"
        try:
            parsed = feedparser.parse(url, agent=USER_AGENT)
            count = 0
            for e in parsed.entries:
                link = e.get("link", "")
                if not link:
                    continue
                items.append({
                    "title": strip_html(e.get("title", "(no title)")),
                    "url": link,
                    "summary": strip_html(e.get("summary", ""))[:SUMMARY_MAX_CHARS],
                    "source_name": f"HN ({q['query']})",
                })
                count += 1
            log(f"  [hn] {q['query']}: {count}건")
        except Exception as exc:  # noqa: BLE001
            log(f"  [hn] {q['query']}: 실패 ({exc})")
    return items


def collect_github(searches: list) -> list:
    items = []
    headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    for search in searches:
        since = (datetime.now(timezone.utc) - timedelta(days=search.get("days_back", 7))).date()
        query = f"{search['query']} created:>{since.isoformat()}"
        try:
            resp = requests.get(
                "https://api.github.com/search/repositories",
                params={"q": query, "sort": "stars", "order": "desc", "per_page": 10},
                headers=headers, timeout=15,
            )
            resp.raise_for_status()
            count = 0
            for repo in resp.json().get("items", []):
                desc = repo.get("description") or ""
                items.append({
                    "title": f"{repo['full_name']} (★{repo.get('stargazers_count', 0)})",
                    "url": repo["html_url"],
                    "summary": desc[:SUMMARY_MAX_CHARS],
                    "source_name": "GitHub Trending",
                })
                count += 1
            log(f"  [github] {search['query']}: {count}건")
        except Exception as exc:  # noqa: BLE001
            log(f"  [github] {search['query']}: 실패 ({exc})")
    return items


# ------------------------------------------------------- YouTube (InnerTube)
# API 키 없이 youtube.com의 공개 내부 검색 엔드포인트를 사용한다.
# hl=en 고정 — viewCountText("1,234 views")/publishedTimeText("3 hours ago")
# 파싱이 언어에 의존하기 때문. gl(지역)은 feeds.yaml에서 지정.

YT_ENDPOINT = "https://www.youtube.com/youtubei/v1/search?prettyPrint=false"
YT_UPLOAD = {"hour": 1, "today": 2, "week": 3, "month": 4}
YT_SORT = {"relevance": 0, "date": 2, "viewcount": 3}
YT_TIME_FACTORS = {"second": 1 / 3600, "minute": 1 / 60, "hour": 1,
                   "day": 24, "week": 168, "month": 720, "year": 8760}


def yt_sp_params(sort: str, upload: str) -> str:
    """검색 필터 protobuf(sp) 인코딩: field1=정렬, field2={업로드시기, 타입=영상}."""
    inner = bytes([0x08, YT_UPLOAD.get(upload, 2), 0x10, 0x01])
    buf = bytes([0x08, YT_SORT.get(sort, 3), 0x12, len(inner)]) + inner
    return base64.b64encode(buf).decode()


def yt_find(node, key: str, out: list) -> None:
    """응답 JSON에서 key 렌더러를 재귀 수집 — 응답 구조 변화에 견고하도록."""
    if isinstance(node, dict):
        if key in node:
            out.append(node[key])
        for v in node.values():
            yt_find(v, key, out)
    elif isinstance(node, list):
        for v in node:
            yt_find(v, key, out)


def yt_parse_views(text: str) -> int | None:
    m = re.search(r"([\d,]+)", text or "")
    return int(m.group(1).replace(",", "")) if m else None


def yt_parse_age_hours(text: str) -> float | None:
    m = re.search(r"(\d+)\s+(second|minute|hour|day|week|month|year)", text or "")
    if not m:
        return None
    return int(m.group(1)) * YT_TIME_FACTORS[m.group(2)]


def yt_parse_length_sec(text: str) -> int | None:
    parts = (text or "").split(":")
    if not all(p.strip().isdigit() for p in parts) or not parts[0].strip():
        return None
    sec = 0
    for p in parts:
        sec = sec * 60 + int(p)
    return sec


def yt_context(gl: str) -> dict:
    return {"context": {"client": {"clientName": "WEB",
                                   "clientVersion": "2.20250101.00.00",
                                   "hl": "en", "gl": gl}}}


YT_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}


def yt_approx_views(text: str) -> int | None:
    """쇼츠 카드 accessibilityText의 근사 조회수: '4.7 million views' 등."""
    m = re.search(r"([\d,.]+)\s*(thousand|million|billion|k|m|b)?\s*views", text or "", re.I)
    if not m:
        return None
    n = float(m.group(1).replace(",", ""))
    mult = {"thousand": 1e3, "k": 1e3, "million": 1e6, "m": 1e6,
            "billion": 1e9, "b": 1e9}.get((m.group(2) or "").lower(), 1)
    return int(n * mult)


def collect_youtube_shorts(cfg: dict) -> list:
    """쇼츠 수집 — 2단계.

    타입 필터를 건 검색에는 진짜 쇼츠가 인덱스에서 제외되므로, (1) 필터 없는
    관련도 검색에서 shortsLockupViewModel 후보를 모으고 (2) player 엔드포인트로
    정확한 조회수·업로드 시각을 받아 VPH와 신선도를 계산한다.
    """
    if not cfg or not cfg.get("searches"):
        return []
    gl = cfg.get("gl", "KR")
    min_views = cfg.get("min_views", 10000)
    min_vph = cfg.get("min_vph", 1000)
    fresh_hours = cfg.get("shorts_fresh_hours", 72)
    max_probe = cfg.get("shorts_max_probe", 12)   # 검색어당 player 상세조회 상한
    max_per_search = cfg.get("max_per_search", 10)
    ctx = yt_context(gl)
    now = datetime.now(timezone.utc)
    seen_ids: set[str] = set()
    items = []
    for search in cfg["searches"]:
        query = search.get("shorts_query") or f"{search['query']} 쇼츠"
        try:
            resp = requests.post(YT_ENDPOINT, json={**ctx, "query": query},
                                 headers=YT_HEADERS, timeout=20)
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            log(f"  [youtube-shorts] {query}: 검색 실패 ({exc})")
            continue
        cands = []
        yt_find(resp.json(), "shortsLockupViewModel", cands)
        picked = probed = 0
        for c in cands:
            if probed >= max_probe or picked >= max_per_search:
                break
            m = re.search(r'"videoId":\s*"([\w-]{11})"', json.dumps(c))
            if not m or m.group(1) in seen_ids:
                continue
            video_id = m.group(1)
            seen_ids.add(video_id)
            approx = yt_approx_views(c.get("accessibilityText", ""))
            if approx is not None and approx < min_views:
                continue
            probed += 1
            try:
                pr = requests.post(
                    "https://www.youtube.com/youtubei/v1/player?prettyPrint=false",
                    json={**ctx, "videoId": video_id},
                    headers=YT_HEADERS, timeout=20).json()
            except Exception as exc:  # noqa: BLE001
                log(f"  [youtube-shorts] {video_id}: 상세조회 실패 ({exc})")
                continue
            vd = pr.get("videoDetails", {})
            mf = pr.get("microformat", {}).get("playerMicroformatRenderer", {})
            pub = mf.get("publishDate")
            if not pub or not vd.get("viewCount", "").isdigit():
                continue
            try:
                age_h = (now - datetime.fromisoformat(pub).astimezone(timezone.utc)
                         ).total_seconds() / 3600
            except ValueError:
                continue
            views = int(vd["viewCount"])
            vph = round(views / max(age_h, 0.5))
            if age_h > fresh_hours or views < min_views or vph < min_vph:
                continue
            length = int(vd.get("lengthSeconds", "0") or 0)
            stats = (f"VPH {vph:,}/h · 조회수 {views:,} · {age_h:.0f}시간 전"
                     f" · 길이 {length}초 · 채널 {vd.get('author', '?')}")
            items.append({
                "title": vd.get("title", "(no title)"),
                "url": f"https://www.youtube.com/shorts/{video_id}",
                "summary": f"[Shorts] {stats} (검색어: {query})",
                "source_name": "YouTube Shorts",
                "vph": vph,
                "stats_line": stats,
            })
            picked += 1
        log(f"  [youtube-shorts] {query}: {picked}건 (후보 {len(cands)}건, 상세조회 {probed}건)")
    items.sort(key=lambda i: i["vph"], reverse=True)
    return items


def collect_youtube(cfg: dict) -> list:
    if not cfg or not cfg.get("searches"):
        return []
    gl = cfg.get("gl", "KR")
    min_views = cfg.get("min_views", 10000)
    min_vph = cfg.get("min_vph", 1000)
    max_per_search = cfg.get("max_per_search", 10)
    items = []
    for search in cfg["searches"]:
        query = search["query"]
        payload = {
            "context": {"client": {"clientName": "WEB",
                                   "clientVersion": "2.20250101.00.00",
                                   "hl": "en", "gl": gl}},
            "query": query,
            "params": yt_sp_params(search.get("sort", "viewcount"),
                                   search.get("upload", "today")),
        }
        try:
            resp = requests.post(
                YT_ENDPOINT, json=payload, timeout=20,
                headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
            )
            resp.raise_for_status()
            videos = []
            yt_find(resp.json(), "videoRenderer", videos)
            picked = 0
            for v in videos:
                video_id = v.get("videoId")
                title = "".join(r.get("text", "") for r in v.get("title", {}).get("runs", []))
                views = yt_parse_views(v.get("viewCountText", {}).get("simpleText", ""))
                pub_text = v.get("publishedTimeText", {}).get("simpleText", "")
                hours = yt_parse_age_hours(pub_text)
                length_sec = yt_parse_length_sec(v.get("lengthText", {}).get("simpleText", ""))
                if not video_id or not title or views is None or hours is None:
                    continue  # 라이브/예정 영상 등 지표 없는 항목
                if pub_text.lower().startswith("streamed"):
                    continue  # 종료된 라이브 스트림 — 급상승 벤치마킹 대상 아님
                vph = round(views / max(hours, 0.5))
                if views < min_views or vph < min_vph:
                    continue
                # 쇼츠 판별: 검색 결과에 별도 마커가 없어 길이 기반 휴리스틱 사용
                is_shorts = length_sec is not None and (
                    length_sec <= 60
                    or (length_sec <= 180 and re.search(r"#?(shorts|쇼츠)", title, re.I))
                )
                url = (f"https://www.youtube.com/shorts/{video_id}" if is_shorts
                       else f"https://www.youtube.com/watch?v={video_id}")
                channel = "".join(
                    r.get("text", "") for r in v.get("ownerText", {}).get("runs", []))
                kind = "Shorts" if is_shorts else "영상"
                length_text = v.get("lengthText", {}).get("simpleText", "?")
                stats = (f"VPH {vph:,}/h · 조회수 {views:,} · {pub_text}"
                         f" · 길이 {length_text} · 채널 {channel}")
                items.append({
                    "title": title,
                    "url": url,
                    "summary": f"[{kind}] {stats} (검색어: {query})",
                    "source_name": f"YouTube {kind}",
                    "vph": vph,
                    "stats_line": stats,
                })
                picked += 1
                if picked >= max_per_search:
                    break
            log(f"  [youtube] {query}: {picked}건 (후보 {len(videos)}건)")
        except Exception as exc:  # noqa: BLE001
            log(f"  [youtube] {query}: 실패 ({exc})")
    items.sort(key=lambda i: i["vph"], reverse=True)  # 소스 버킷 내 VPH 높은 순
    return items


def interleave_by_source(items: list) -> list:
    """소스별 라운드로빈 — MAX_ITEMS 컷에서 특정 소스가 독식하지 않도록."""
    buckets: dict[str, list] = {}
    for item in items:
        buckets.setdefault(item["source_name"], []).append(item)
    result = []
    while any(buckets.values()):
        for name in list(buckets):
            if buckets[name]:
                result.append(buckets[name].pop(0))
    return result


# ------------------------------------------------------------------ judgment

class FatalAPIError(Exception):
    """재시도가 무의미한 오류(크레딧 부족, 인증 실패) — 실행 전체 중단."""


def is_fatal_api_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(marker in msg for marker in (
        "credit balance", "authenticat", "invalid x-api-key",
        "invalid api key", "invalid bearer token", "oauth token", "/login",
        "401",
    ))


def parse_judgment(text: str) -> dict | None:
    """모델 응답에서 JSON을 추출·검증. 실패 시 None."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if data.get("verdict") not in VERDICTS:
        return None
    if not isinstance(data.get("title_ko"), str) or not data["title_ko"].strip():
        return None
    tags = data.get("tags") or []
    if not isinstance(tags, list):
        tags = []
    # 한글 태그 보존 slugify (기본 slugify는 비ASCII를 지워 전부 "item"이 됨)
    cleaned = []
    for t in tags[:3]:
        slug = re.sub(r"[^0-9a-zA-Z가-힣]+", "-", str(t).lower()).strip("-")[:30].rstrip("-")
        if slug and slug not in cleaned:
            cleaned.append(slug)
    data["tags"] = cleaned
    data["reason"] = str(data.get("reason", "")).strip()
    data["action"] = str(data.get("action", "")).strip()
    return data


def build_prompt(item: dict) -> str:
    return JUDGE_PROMPT.format(
        title=item["title"],
        source_name=item["source_name"],
        url=item["url"],
        summary=item["summary"] or "(요약 없음 — 제목으로 판단)",
    )


def judge_item_api(client, model: str, system_blocks: list, item: dict) -> dict | None:
    prompt = build_prompt(item)
    for attempt in (1, 2):  # JSON 파싱 실패 시 1회 재시도
        try:
            response = client.messages.create(
                model=model,
                max_tokens=512,
                system=system_blocks,
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as exc:  # noqa: BLE001
            if is_fatal_api_error(exc):
                raise FatalAPIError(str(exc)) from exc
            log(f"    API 오류 (시도 {attempt}): {exc}")
            if attempt == 2:
                return None
            time.sleep(3)
            continue
        text = next((b.text for b in response.content if b.type == "text"), "")
        judgment = parse_judgment(text)
        if judgment:
            return judgment
        log(f"    JSON 파싱 실패 (시도 {attempt}): {text[:120]!r}")
    return None


def judge_item_cli(model: str, system_text: str, item: dict) -> dict | None:
    """Claude Code CLI(claude -p)로 판정 — API 크레딧 대신 구독 인증 사용."""
    prompt = build_prompt(item)
    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)  # CLI가 API 키 과금으로 빠지지 않도록
    cmd = ["claude", "-p", "--model", model, "--tools", "",
           "--output-format", "text", "--append-system-prompt", system_text]
    for attempt in (1, 2):
        try:
            result = subprocess.run(cmd, input=prompt, env=env, timeout=180,
                                    capture_output=True, text=True)
        except subprocess.TimeoutExpired:
            log(f"    CLI 타임아웃 (시도 {attempt})")
            continue
        if result.returncode != 0:
            err = (result.stderr or result.stdout).strip()
            if is_fatal_api_error(RuntimeError(err)):
                raise FatalAPIError(err[:300])
            log(f"    CLI 오류 (시도 {attempt}): {err[:200]}")
            if attempt == 2:
                return None
            time.sleep(3)
            continue
        judgment = parse_judgment(result.stdout)
        if judgment:
            return judgment
        log(f"    JSON 파싱 실패 (시도 {attempt}): {result.stdout[:120]!r}")
    return None


# -------------------------------------------------------------------- output

def yaml_quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def write_post(item: dict, judgment: dict, date: datetime) -> Path:
    CONTENT_DIR.mkdir(parents=True, exist_ok=True)
    slug = slugify(item["title"])
    if slug == "item" and item.get("hash"):  # 한글 등 비ASCII 제목 → URL 해시로 구분
        slug = item["hash"][:12]
    base = f"{date.date().isoformat()}-{slug}"
    path = CONTENT_DIR / f"{base}.md"
    n = 2
    while path.exists():
        path = CONTENT_DIR / f"{base}-{n}.md"
        n += 1
    tags = ", ".join(yaml_quote(t) for t in judgment["tags"])
    body = f"""---
title: {yaml_quote(judgment["title_ko"])}
date: {date.isoformat()}
verdict: {yaml_quote(judgment["verdict"])}
tags: [{tags}]
source: {yaml_quote(item["url"])}
source_name: {yaml_quote(item["source_name"])}
status: "대기"
---
- **근거:** {judgment["reason"]}
- **액션:** {judgment["action"]}
"""
    if item.get("stats_line"):
        body += f"- **지표:** {item['stats_line']}\n"
    path.write_text(body, encoding="utf-8")
    return path


# ---------------------------------------------------------------------- main

def load_processed() -> dict:
    if PROCESSED_FILE.exists():
        try:
            return json.loads(PROCESSED_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log("processed.json 파싱 실패 — 초기화")
    return {}


def prune_processed(processed: dict) -> dict:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=PROCESSED_TTL_DAYS)).isoformat()
    return {k: v for k, v in processed.items() if v >= cutoff}


def main() -> int:
    parser = argparse.ArgumentParser(description="Daily insight pipeline")
    parser.add_argument("--dry-run", action="store_true",
                        help="파일 생성/processed.json 갱신 없이 판정 결과만 출력")
    parser.add_argument("--max-items", type=int,
                        default=int(os.environ.get("MAX_ITEMS", "30")),
                        help="1회 실행당 판정 최대 건수")
    args = parser.parse_args()

    # 판정 백엔드: claude-code(구독 인증, 기본) 또는 api(ANTHROPIC_API_KEY 과금)
    backend = os.environ.get("JUDGE_BACKEND", "").strip() or (
        "claude-code" if shutil.which("claude") else "api"
    )
    client = None
    if backend == "api":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            log("오류: api 백엔드에는 ANTHROPIC_API_KEY 환경변수가 필요합니다")
            return 1
        import anthropic  # 지연 임포트 — claude-code 백엔드에서는 불필요

        client = anthropic.Anthropic()
    elif backend == "claude-code":
        if not shutil.which("claude"):
            log("오류: claude-code 백엔드에는 claude CLI가 PATH에 있어야 합니다")
            return 1
    else:
        log(f"오류: 알 수 없는 JUDGE_BACKEND={backend!r} (claude-code | api)")
        return 1

    model = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")
    feeds = yaml.safe_load(FEEDS_FILE.read_text(encoding="utf-8"))
    context_md = CONTEXT_FILE.read_text(encoding="utf-8")

    system_text = (
        "당신은 아래 채널 컨텍스트를 기준으로 유튜브 급상승 쇼츠/영상의 벤치마킹 가치를 판정하는 "
        "유튜브 트렌드 분석 어시스턴트다.\n\n"
        + context_md
    )
    # api 백엔드: context.md는 모든 호출에서 동일 → prompt cache로 비용 절감
    system_blocks = [{
        "type": "text",
        "text": system_text,
        "cache_control": {"type": "ephemeral"},
    }]

    log(f"=== 수집 시작 (backend={backend}, model={model}, max_items={args.max_items}, dry_run={args.dry_run}) ===")
    collected = []
    collected += collect_youtube_shorts(feeds.get("youtube", {}))
    collected += collect_youtube(feeds.get("youtube", {}))
    collected += collect_rss(feeds.get("rss", []))
    collected += collect_reddit(feeds.get("reddit", []))
    collected += collect_hackernews(feeds.get("hackernews", []))
    collected += collect_github(feeds.get("github_search", []))

    # URL 기준 중복 제거 (동일 실행 내)
    seen_urls = set()
    unique = []
    for item in collected:
        h = url_hash(item["url"])
        if h in seen_urls:
            continue
        seen_urls.add(h)
        item["hash"] = h
        unique.append(item)

    processed = prune_processed(load_processed())
    fresh = [i for i in unique if i["hash"] not in processed]
    queue = interleave_by_source(fresh)[: args.max_items]

    log(f"\n수집 {len(collected)}건 / 중복 제거 후 {len(unique)}건 / 신규 {len(fresh)}건 / 판정 대상 {len(queue)}건\n")

    now = datetime.now(timezone.utc).astimezone()  # 로컬(러너) 타임존 ISO
    verdict_counts = {v: 0 for v in VERDICTS}
    skipped = 0
    created_files = []

    fatal_error = None
    for i, item in enumerate(queue, 1):
        log(f"[{i}/{len(queue)}] {item['source_name']} | {item['title'][:80]}")
        try:
            if backend == "claude-code":
                judgment = judge_item_cli(model, system_text, item)
            else:
                judgment = judge_item_api(client, model, system_blocks, item)
        except FatalAPIError as exc:
            fatal_error = exc
            break
        if judgment is None:
            skipped += 1
            log("    → 스킵 (판정 실패)")
            continue
        verdict_counts[judgment["verdict"]] += 1
        log(f"    → {judgment['verdict']} | {judgment['title_ko']}")
        if judgment["reason"]:
            log(f"      근거: {judgment['reason']}")
        if judgment["action"]:
            log(f"      액션: {judgment['action']}")

        if not args.dry_run:
            processed[item["hash"]] = now.isoformat()
            if judgment["verdict"] != "무관":
                created_files.append(write_post(item, judgment, now))

    if not args.dry_run:
        PROCESSED_FILE.parent.mkdir(parents=True, exist_ok=True)
        PROCESSED_FILE.write_text(
            json.dumps(processed, indent=1, sort_keys=True), encoding="utf-8"
        )

    log("\n=== 실행 결과 요약 ===")
    judged = sum(verdict_counts.values())
    log(f"수집: {len(collected)}건 / 신규: {len(fresh)}건 / 판정: {judged}건 (실패 스킵 {skipped}건)")
    log("판정 분포: " + " / ".join(f"{v} {c}" for v, c in verdict_counts.items()))
    if args.dry_run:
        log("(dry-run — 파일 생성/기록 갱신 없음)")
    elif created_files:
        log("생성 파일:")
        for f in created_files:
            log(f"  - {f.relative_to(ROOT)}")
    else:
        log("생성 파일 없음")

    if fatal_error:
        log(f"\n중단: 복구 불가능한 API 오류 — {fatal_error}")
        log("→ Anthropic 크레딧/API 키를 확인하세요. 미처리 항목은 다음 실행에서 재시도됩니다.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
