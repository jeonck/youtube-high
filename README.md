# YouTube 급상승 레이더

매일 아침(KST 07:00) YouTube InnerTube 검색(API 키 불필요)으로 **조회수가 폭발하는
쇼츠·영상**을 수집하고, VPH(시간당 조회수)를 계산한 뒤 Claude가
[context.md](context.md)(내 채널 컨텍스트) 기준으로 **행동 판정**을 내려,
무관 판정을 제외한 항목만 Hugo 포스트로 커밋하여 GitHub Pages에 배포한다.

**사이트:** https://jeonck.github.io/youtube-high/

## 급상승 판별 방식

- feeds.yaml의 검색어별로 "오늘 업로드 + 조회수순" 검색 (InnerTube 공개 엔드포인트, API 키 불필요)
- **VPH(시간당 조회수) = 조회수 ÷ 업로드 후 경과 시간** — 급상승의 핵심 지표
- `min_views`(기본 10,000) / `min_vph`(기본 1,000) 임계를 넘는 항목만 판정 대상
- 쇼츠 판별: 길이 ≤60초, 또는 ≤180초 + 제목에 #shorts/쇼츠 → `YouTube Shorts`로 구분 표기

## 판정 체계

| 판정 | 의미 | 예 |
|---|---|---|
| 🔥 즉시조치 | 초급상승(VPH 1만+)이고 1인 제작으로 재현 가능한 소재/포맷 | 챌린지 포맷, 꿀팁 쇼츠 |
| 📌 백로그 | 급상승이지만 재현 난이도가 높거나 당장 만들 소재 아님 | 장비/출연진 필요한 포맷 |
| 📚 학습 | 재현 대상은 아니나 후킹·편집·썸네일에 배울 점 | 첫 3초 후킹 구성, 제목 패턴 |
| 무관 | 그 외 전부 → **포스트 생성 안 함** | 기획사 MV, 방송사 클립, 지표 낮음 |

각 포스트는 `근거`(급상승 근거와 채널 소재 연관성) · `액션`(이번 주 벤치마킹 작업 1개) ·
`지표`(VPH/조회수/업로드 시각/길이/채널)를 담는다.

## OfflineTube 연동 — 발견에서 제작까지

🔥 **즉시조치** 판정 항목은 완성된 **영어 영상 스크립트**(90~150단어, 강한 후킹으로 시작)를
함께 생성한다. 이 스크립트를 [OfflineTube](https://github.com/jeonck/OfflineTube)
(로컬 AI 영상 생성 도구, Ollama+Chatterbox TTS+faster-whisper 기반)의 WebUI에
그대로 붙여넣으면 나레이션·자막·BGM이 포함된 쇼츠 mp4가 자동 생성된다.

```bash
# OfflineTube 쪽 (별도 Mac, 최초 1회)
git clone https://github.com/jeonck/OfflineTube.git && cd OfflineTube && ./setup.sh && ./start.sh
# http://127.0.0.1:8501 접속 → Video Subject란에 위 스크립트 붙여넣기 → Generate Video
```

즉, 이 사이트는 "무엇을 만들지"(급상승 소재 발견 + 스크립트 초안)를,
OfflineTube는 "어떻게 만들지"(실제 영상 렌더링)를 담당한다.

## 구조

```
.
├── context.md                  # 내 채널/제작 여건 = 판정 기준 (여기를 고치면 판정이 바뀜)
├── feeds.yaml                  # 검색어·임계값 정의 (youtube 섹션, RSS 등 추가 가능)
├── pipeline/
│   ├── collect.py              # InnerTube 수집 → VPH 계산 → 판정 → 포스트 생성
│   ├── requirements.txt
│   ├── processed.json          # 처리한 URL 해시 기록 (중복 방지, 90일 보존)
│   ├── expire.py               # 학습 항목 14일 경과 시 자동 완료 (EXPIRE_DAYS로 조정)
│   └── done.sh                 # 주간 리뷰: status 대기 → 완료
├── content/insights/           # 생성된 포스트
├── layouts/                    # 자체 Hugo 레이아웃 (외부 테마 없음)
├── hugo.toml                   # taxonomy: verdict / status / tags
└── .github/workflows/daily.yml # cron 수집 + Pages 배포
```

## 최초 세팅

1. **판정 인증 등록** — 둘 중 하나 (repo Settings → Secrets and variables → Actions)
   - **권장: Claude 구독 (Pro/Max)** — 로컬에서 `claude setup-token` 실행 → 브라우저 인증 →
     출력된 토큰을 `CLAUDE_CODE_OAUTH_TOKEN` Secret으로 등록.
     API 크레딧 불필요, 구독 사용량으로 차감됨.
   - **대안: Claude API 키** — `ANTHROPIC_API_KEY` Secret 등록 (계정에 크레딧 필요).
     `CLAUDE_CODE_OAUTH_TOKEN`이 없을 때만 사용됨.
2. **Pages 활성화** — Settings → Pages → Source: **GitHub Actions**
3. **첫 실행** — Actions 탭 → `Daily Insights` → Run workflow (수동 실행은 신규 항목이 없어도 배포함)

이후 매일 UTC 22:00 (KST 07:00) 자동 실행.

## 로컬 실행

```bash
pip install -r pipeline/requirements.txt

# claude CLI가 로그인돼 있으면 그대로 동작 (구독 인증, API 키 불필요)
# dry-run: 파일 생성/기록 갱신 없이 판정 결과만 stdout 출력
python pipeline/collect.py --dry-run

# 판정 건수 제한 (기본 30, 비용 안전장치)
MAX_ITEMS=5 python pipeline/collect.py --dry-run

# 실제 생성 후 로컬 미리보기
python pipeline/collect.py
hugo server        # → http://localhost:1313/youtube-high/
```

환경변수:
- `JUDGE_BACKEND`: `claude-code`(구독, 기본 — claude CLI가 PATH에 있을 때) | `api`(API 키 과금)
- `CLAUDE_MODEL`(기본 `claude-sonnet-4-6`), `MAX_ITEMS`(기본 30), `GITHUB_TOKEN`(선택)

## 운영 루틴

**매일 아침 (2분)**
1. 사이트 접속 → 🔥 즉시조치 · 대기 확인 → 있으면 액션(벤치마킹 제작 항목) 그대로 실행
2. 📌 백로그는 눈으로만 훑기

**자동 정리**
- 학습 항목은 14일이 지나면 자동으로 완료 처리됨 (매일 아침 실행, `EXPIRE_DAYS`로 조정)
- 즉시조치·백로그는 자동 만료 없음 — 사람이 처리 여부를 결정

**수집 일시중지/재개** (휴가, 소스 점검 등)
- 웹/모바일: Actions 탭 → **Pipeline Control** → Run workflow → pause / resume / status 선택
- 터미널:
  ```bash
  ./pipeline/pause.sh    # cron의 수집·판정만 중지 (완료 처리 푸시 배포는 계속 동작)
  ./pipeline/resume.sh   # 재개
  ```

**매주 금요일 (15분)**
1. 백로그/학습 중 처리한 항목 완료 처리:
   ```bash
   ./pipeline/done.sh content/insights/2026-07-12-abcdef.md
   git commit -am "review: weekly done" && git push
   ```
2. 판정 품질이 어긋나면 **context.md를 수정** (소재 후보, 제작 여건, 명시적 제외 보강)
   — 다음 실행부터 반영됨
3. 검색어가 노이즈만 내면 feeds.yaml에서 교체, 급상승 기준이 느슨/빡빡하면 `min_vph` 조정

## 비용

- 항목당 1회 Claude 호출, 실행당 최대 `MAX_ITEMS`(30)건
- **claude-code 백엔드(기본)**: 별도 과금 없음 — Claude 구독(Pro/Max) 사용량으로 차감
- **api 백엔드**: context.md prompt cache 적용, 일 30건 × sonnet 기준 ≈ $0.05~0.1/일

## 알려진 제약

- **InnerTube 응답 구조**: YouTube 내부 API라 예고 없이 바뀔 수 있음. 렌더러를 재귀 탐색해
  구조 변화에 견고하게 파싱하며, 실패 시 해당 검색어만 0건 (검색어별 오류 격리).
- **VPH 정밀도**: 업로드 시각이 "N hours ago" 단위라 ±30분 오차 존재. 급상승 판별에는 충분.
- **쇼츠 판별**: 검색 결과에 쇼츠 마커가 없어 길이 기반 휴리스틱 사용 — 1~3분 일반 영상이
  드물게 Shorts로 표기될 수 있음.
- **크레딧 부족/인증 오류**: 즉시 중단하고 워크플로를 실패로 표시함 (의도된 동작 —
  Actions 실패 알림으로 인지). 인증 복구 후 다음 실행에서 미처리 항목 자동 재시도.
- **OAuth 토큰 만료**: `claude setup-token`으로 재발급 후 Secret 갱신.
