# Alphred

[English](README.md) | **한국어**

> [Hermes Agent](https://github.com/NousResearch/hermes-agent) 위에 얹는
> **우선순위 큐 미들웨어 + 상태 제어 머신** 래퍼 — **코어는 수정하지 않는다.**

여느 AI 비서처럼 Alphred에게 말을 걸면 됩니다. 빠른 요청("이거 번역해줘", "날씨 알려줘")은
**즉시** 답하고, 무거운 요청("전체 코드베이스 리팩토링해줘", "이 200페이지 크롤링해서 요약해줘")은
큐에 넣어 우선순위대로 백그라운드에서 처리합니다. 급한 게 들어오면 진행 중인 작업을 잠시 멈추고
먼저 처리한 뒤 다시 이어갑니다. **무엇이 급한지는 사용자가 표시하지 않아도 Alphred가 판단합니다.**

전체 설계 문서: [`docs/Alphred-실행기획안.md`](docs/Alphred-실행기획안.md)

---

## Alphred이란?

Hermes Agent는 강력한 단일 턴 에이전트 런타임입니다. Alphred은 여기에 항상 켜져 있는 비서에게
꼭 필요한 한 가지, **무엇을 먼저 할지 아는 능력**을 더합니다.

들어오는 모든 요청을 다음으로 분류합니다.

- **Light** — 즉답이 필요한 요청(대화, 조회, 짧은 Q&A, 번역). 바로 응답.
- **Heavy** — 시간이 걸리는 백그라운드 작업(분석, 리팩토링, 크롤링, 리포트). 우선순위 큐에 등록 후 스케줄.

Alphred은 영속 우선순위 큐 위에서 단일 슬롯 스케줄러를 돌립니다. Heavy 작업 실행 중에 Light 요청
(또는 더 높은 우선순위의 Heavy)이 들어오면 진행 중 작업을 **선점**합니다(일시정지 → 급한 것 처리 → 재개).
상태는 SQLite(단일 진실 공급원)에 저장되고, 사람이 읽을 수 있는 `QUEUE.MD`로도 투영됩니다.

Hermes 코어는 절대 패치하지 않습니다 — Alphred은 Hermes의 HTTP API로 통신하고 Hermes home
디렉터리를 공유하므로, Hermes를 업데이트해도 깨지지 않습니다.

---

## 왜 Alphred인가?

| 기능 | 사용자에게 주는 의미 |
|---|---|
| **자동 Light/Heavy 라우팅** | 그냥 말하면 됩니다. 메시지마다 긴급도를 Alphred이 판단 — 플래그·수동 등록 불필요. 원할 때만 오버라이드. |
| **선점형 우선순위 큐** | 급한 요청이 긴 작업 뒤에서 기다리지 않습니다. 긴 작업은 자동으로 멈췄다 재개됩니다. |
| **작업 심화도 + 검증 (§21)** | 가벼운 작업은 싸게, 무거운 작업은 기획·검증·자가치유. 파일을 *만들었다고 말만* 한 run 은 `Completed` 가 아니라 `NeedsReview` 로 갑니다. |
| **내장된 복원력** | 일시적 실패(429 / 레이트리밋 / 네트워크)는 지수 백오프로 자동 재큐, 크래시·재시작 후 고아 작업 자동 복구. |
| **OpenAI 호환 게이트웨이** | 어떤 OpenAI 클라이언트든 Base URL만 Alphred로 바꾸면 chat/responses/runs 모두 동작. |
| **코어 무수정 리브랜딩** | 사용자측 파일만으로 Hermes UI와 정체성을 "Alphred"로 교체 — Hermes 업데이트에도 유지. |
| **의존성 0 웹 대시보드** | 드래그&드롭 우선순위 변경, 일시정지/재개/폐기, 제출 — 단일 자립형 HTML 한 장. |

---

## 동작 방식

```
                 ┌─────────────────────────── Alphred ───────────────────────────┐
   요청    ──►  │  분류 (키워드 + 길이 + 소스, 모호하면 LLM 폴백)                 │
 (chat / API /   │            │                                                   │
  voice / cron)  │     ┌──────┴───────┐                                           │
                 │   Light          Heavy                                         │
                 │     │              │                                           │
                 │  진행 중        큐 등록 ──► 우선순위 큐 (SQLite + QUEUE.MD)     │
                 │  Heavy            │                    │                       │
                 │  선점             │            단일 슬롯 스케줄러              │
                 │     │             │          (정지 / 재개 / 재시도 / 복구)     │
                 │     ▼             ▼                    ▼                       │
                 └─────┴─────────────┴──────── Hermes HTTP API ───────────────────┘
                                                         │
                                                    LLM provider
```

작업 상태: `Pending → In-Progress → (Paused) → Completed / Discarded`.

---

## 빠른 시작

**한 번만 설치** (Hermes가 설치돼 있어야 함):

**옵션 A — 설치 스크립트** (pip 설치·PATH·브랜딩까지 자동 처리):

```bash
# Windows (PowerShell)
powershell -ExecutionPolicy Bypass -File scripts\install.ps1
# macOS / Linux
bash scripts/install.sh
```

**옵션 B — 수동:**

```bash
pip install -e .
alphred setup     # Hermes 온보딩(LLM provider 설정). Hermes 는 순정 유지.
```

> **`alphred: command not found`(또는 not recognized)?** pip이 `alphred` 를 PATH에 없는 Scripts
> 폴더에 설치한 것입니다(설치 끝에 위치를 경고로 출력함). 그 폴더를 PATH에 추가하거나, 아래의
> 모든 `alphred ...` 를 `python -m alphred.cli ...` 로 바꿔 쓰면 됩니다(동일).

그다음 사용 방식을 고르세요 — 둘 다 **동일한** Alphred 엔진(하나의 큐·하나의 상태)을 씁니다.
터미널에서 시작한 작업이 웹에서 보이고, 그 반대도 됩니다.

**방식 1 — 터미널에서 대화 (전용 Alphred TUI):**

```bash
alphred            # 전용 Alphred TUI: 대화 + 실시간 큐 표 + 완료 알림
alphred chat       # (또는) 라이브 툴 UI가 필요하면 Hermes TUI 직접 진입
```

`alphred` 는 게이트웨이에 붙는 전용 터미널 클라이언트(Textual)를 띄웁니다. 빠른 질문은 즉답,
무거운 요청은 백그라운드 큐로 오프로드되어 **실시간 표**로 보이고, 큐 작업이 끝나면 대화 중에도
**완료 알림**이 옵니다. 무인자 `alphred` 가 백그라운드 데몬도 자동 기동합니다. (`textual` 필요 — 기본 설치됨.)

**방식 2 — 서버 구동 (웹 대시보드 + 앱/디바이스용 OpenAI 호환 API):**

```bash
alphred serve --port 8643      # 게이트웨이 + 스케줄러; Hermes API(:8642) 자동 기동
```

**http://localhost:8643/** 로 웹 대시보드를 열거나, OpenAI 호환 클라이언트(웹앱·안드로이드·ESP32 등)를
`http://localhost:8643/v1` 로 가리키면 됩니다.
(직접 띄운 Hermes API를 쓰려면 `alphred serve --no-auto-hermes`.)

---

## 사용법

### A. 그냥 말하기 — Alphred이 자동 분류

이게 기본 경로입니다. 평소처럼 요청을 보내면 Alphred이 메시지마다 Light/Heavy를 판단합니다.

분류기는 **전용 TUI**(`alphred`)와 **HTTP 게이트웨이**(`:8643`)에서 돕니다. 그냥 말하면 됩니다 —
빠른 질문은 즉답, 무거운 요청("전체 코드베이스 리팩토링")은 자동으로 백그라운드 큐로 오프로드.

> 참고: `alphred chat` 은 *순정 Hermes*(큐 없음)입니다. 큐 결합 대화는 `alphred`(TUI)나 HTTP API를 쓰세요.

OpenAI 호환 HTTP API로도 동일(어떤 클라이언트든 Base URL만 변경):

```bash
curl http://localhost:8643/v1/chat/completions \
  -H "Authorization: Bearer $API_SERVER_KEY" \
  -d '{"messages":[{"role":"user","content":"이 300페이지 리포트 요약해줘"}]}'
# Heavy → 202 {"id": "...", "status": "queued"}   (GET /v1/runs/{id} 로 폴링)
# Light → 일반 OpenAI 응답, 즉시
```

**판단 기준:**

| 신호 | 라우팅 |
|---|---|
| Heavy 키워드 (`리팩토`, `크롤`, `분석`, `리포트`, `마이그레이`, `일괄`, `빌드`, `학습`, …) | **Heavy** |
| Light 키워드 (`안녕`, `뭐야`, `번역`, `계산`, …), 또는 60자 이하, 또는 chat 소스 메시지 | **Light** |
| 모호 | 보수적으로 **Heavy** (LLM 폴백 활성화 시 LLM이 판정) |

### B. 판단 오버라이드 (수동 제어가 필요할 때)

자동 판단이 기본이며, 아래는 강제 지정용입니다.

```bash
# CLI: 우선순위/종류를 직접 지정해 등록 (플래그는 선택 — 생략하면 자동 분류됨)
alphred queue submit "전체 코드베이스 리팩토링" --priority 3   # Heavy, 우선순위 3 강제
alphred queue submit "급한 질문" --priority 10                 # 높은 우선순위 강제

# HTTP: 헤더로 오버라이드
curl http://localhost:8643/v1/chat/completions \
  -H "X-Alphred-Kind: heavy" -H "X-Alphred-Priority: 2" ...
```

### C. 큐 관리

```bash
alphred queue list                # 큐 조회 (우선순위순)
alphred queue show <id>           # 상세 + 상태 전이 이력
alphred queue prio <id> 8         # 우선순위 변경
alphred queue pause <id>          # 일시정지 / 재개 / 폐기
alphred queue resume <id>
alphred queue discard <id>
alphred queue run                 # 스케줄러 루프 직접 실행 (게이트웨이 없이)
```

또는 그냥 자연어로 요청하면 됩니다 — Alphred이 큐를 읽고 요청대로 처리합니다:

```bash
alphred queue ask "지금 큐 어때?"
alphred queue ask "리포트 작업 우선순위 맨 위로 올려줘"
alphred queue ask "크롤링 작업 취소해"
```

`http://localhost:8643/` 의 웹 대시보드에서 위 작업을 드래그&드롭으로 모두 할 수 있습니다.

### D. Hermes 패스스루 (`alphred` = `hermes` 1:1 슈퍼셋)

`queue` / `serve` / `setup` / `tui` / `doctor` 를 제외한 모든 명령은 Hermes로 그대로 위임됩니다
(종료 코드·스트리밍 보존). 새 Hermes 서브커맨드는 자동 노출됩니다.

```bash
alphred version          # == hermes version
alphred gateway run      # == hermes gateway run
alphred chat             # == hermes  (순정 Hermes TUI — Alphred 흔적 없음)
```

**순수 Hermes 보장.** Alphred 정체성은 전용 `alphred` TUI(Alph-RED)에만 있습니다. Hermes를
재스킨/수정하지 **않으므로**, `hermes`(또는 `alphred chat`)는 원본 로고/배너/색상/정체성 그대로,
Alphred 흔적이 전혀 없습니다.

---

## 게이트웨이 API

| 엔드포인트 | 동작 |
|---|---|
| `POST /v1/chat/completions` | **Light** (즉시, 진행 중 Heavy 선점). 무상태 — 매 호출에 전체 `messages` 배열 전송. |
| `POST /v1/responses` | **Light** (멀티모달 input·`previous_response_id` 보존으로 연속성) |
| `POST /v1/runs` | **Heavy** (비동기, `run_id` 반환). `session_id`·`conversation_history` 수용으로 세션 연속성, 검증 루프 적용. |
| `GET /v1/runs/{id}` | 작업 상태 + 검증: `state`, `depth`, `needs_review`, `verify_attempts`, `verify_report`, `session_id` |
| `POST /plan` | **드라이런** — 실행/큐등록 없이 `kind`/`depth`/`plan`/`estimate` 반환 |
| `GET /v1/models`, `GET /models/available` | 모델 목록 (프록시 / provider별 실제 선택 가능) |
| `GET /v1/skills` | 설치된 Hermes 스킬 (에이전트가 작업 중 자동 활용) |
| `GET/POST/DELETE /queue/...` | 큐 관리 (list / prio / pause / resume / discard) |
| `POST /queue/ask` | 자연어 큐 관리 (`{"q": "..."}`) |
| `GET /`, `GET /dashboard` | 웹 대시보드 |
| `GET /safety`, `POST /safety/reset` | 재시작 폭주 가드 상태 / 리셋 |

헤더: `X-Alphred-Priority`(1..10), `X-Alphred-Kind`(`light`|`heavy`), `X-Alphred-Source`, `X-Alphred-Depth`.
인증: `ALPHRED_API_KEY` / `API_SERVER_KEY` 설정 시 `Bearer` 토큰 필수.

### 세션

| 경로 | 세션/맥락 유지 방식 |
|---|---|
| `POST /v1/chat/completions` | **무상태**(OpenAI 방식) — 매번 전체 `messages` 재전송, 서버측 세션 없음. |
| `POST /v1/responses` | `previous_response_id` 로 체이닝(OpenAI Responses API). |
| `POST /v1/runs` | 동일 `session_id` 를 보내면 후속 Heavy run 들이 한 Hermes 세션(서버측 맥락)을 공유. 미지정 시 run 별 독립(`session_id` = `run_id`). `conversation_history` 로 1회성 명시 핸드오프. |
| 전용 TUI | 자동 관리 — 각 대화가 `ALPHRED_HOME/tui_sessions/` 에 저장되고 시작 시 복원, `/sessions` 로 전환. 현재 세션(과 모델)은 출력 패널 테두리 제목에 표시. |

```bash
# API 에서 멀티턴 Heavy 세션: 같은 session_id 재사용
curl -s localhost:8643/v1/runs -H "Authorization: Bearer $KEY" \
  -d '{"input":"이번 분기 GPU 시장 조사","session_id":"gpu-research"}'
curl -s localhost:8643/v1/runs -H "Authorization: Bearer $KEY" \
  -d '{"input":"방금 내용을 한 장짜리 브리프로 정리","session_id":"gpu-research"}'
```

### 검증 & 작업 심화도 (§21)

**큐에 등록된 모든 Heavy run**(`POST /v1/runs`)은 완료 처리 전에 검증 루프를 거칩니다 — Light 동기 호출(`/v1/chat/completions`, `/v1/responses`)은 검증 대상이 **아닙니다**.

- **심화도**(`low`/`mid`/`high`)가 작업별로 정해져 검증·재시도 강도를 게이팅 → 가벼운 작업은 토큰을 아낍니다.
- **Tier 0 (결정적·무비용·기본 ON):** 결과가 파일을 저장했다고 주장하면, 실제 존재·비어있지 않음·형식 시그니처(예: `%PDF`)를 확인. 불량/누락 산출물 → `Completed` 대신 **`NeedsReview`**.
- **Tier 2 (LLM judge·opt-in·`high` 한정):** 별도 모델이 요청에서 추론한 수용기준으로 결과를 채점 → `{passed, score, unmet[]}`.
- **Tier 3 (자가치유 재시도):** 실패 시 `high` 작업은 실행 가능한 힌트(예: "정식 PDF로 생성, 필요시 라이브러리 설치")와 함께 예산까지 재큐 후 `NeedsReview`.

```bash
# 1) 실행 전 미리보기(실행 없음):
curl -s localhost:8643/plan -H "Authorization: Bearer $KEY" \
  -d '{"message":"미국 증시 PDF 보고서 작성"}'
# → {"depth":"high","estimate":{"est_llm_calls":7,...},"plan":{...}}

# 2) 제출 후 폴링 — 상태에 검증 결과가 담깁니다:
RID=$(curl -s localhost:8643/v1/runs -H "Authorization: Bearer $KEY" \
        -d '{"input":"미국 증시 PDF 보고서 작성"}' | jq -r .run_id)
curl -s localhost:8643/v1/runs/$RID -H "Authorization: Bearer $KEY"
# → {"status":"completed","needs_review":false,"depth":"high",
#    "verify_report":{"passed":true,"checks":[...],"judge":{...}}, ...}
#   needs_review=true 면 산출물/수용 검증 실패 — verify_report 확인.
```

---

## 진단

```bash
alphred doctor          # hermes 바이너리·:8642/:8643·모델/provider·플래너·검증/judge·큐+검증통계·안전망 점검
alphred doctor --json   # 기계 판독용
```

**라이브 LLM 호출 없음**(쿼터 안전). 미응답 컴포넌트는 조치 힌트와 함께 표시합니다.

---

## 설정

| 환경변수 | 기본값 | 용도 |
|---|---|---|
| `ALPHRED_HERMES_API` | `http://localhost:8642/v1` | Hermes API Base URL |
| `API_SERVER_KEY` / `ALPHRED_API_KEY` | — | 게이트웨이 인증 토큰 |
| `ALPHRED_HOME` | Hermes home | Alphred 상태 디렉터리 |
| `ALPHRED_HERMES_BIN` | 자동 해석 | 패스스루용 Hermes 실행 파일 |
| `ALPHRED_MAX_RETRIES` | `3` | 일시적 실패 최대 재시도 |
| `ALPHRED_RETRY_BASE_SECONDS` | `5` | 지수 백오프 기준 시간 |
| `ALPHRED_LLM_CLASSIFY` | off | 모호한 입력에 단순 LLM 분류 폴백 활성화 |
| `ALPHRED_PLANNER` | off | 계획 기반 분류: 모호한 요청을 하위작업으로 분해→계획 구조로 Heavy/Light 판정→실행에 계획 재활용 |
| `ALPHRED_VERIFY` | **on** | §21 Tier 0 — 완료 Heavy run 의 산출물 결정적 검증(무비용). `0` 으로 비활성화. |
| `ALPHRED_JUDGE` | off | §21 Tier 2 — `high` 심화도 run 에 LLM 수용 judge(쿼터 사용) + 자가치유 재시도 |
| `ALPHRED_JUDGE_RETRIES` | `2` | §21 Tier 3 — `high` 작업의 검증 재시도 상한(초과 시 `NeedsReview`) |

### 분류 방식 (Heavy/Light 판정)

1. **싼 사전필터(LLM X)** — 상태 조회→Light, 명시적 대규모("전체 코드베이스/마이그레이션/크롤" 또는 heavy 키워드 2개+)→Heavy, 인사/짧음/실시간 채팅→Light.
2. **모호한 중간대** — `ALPHRED_PLANNER` 켜짐 시 LLM이 하위작업으로 분해하고, 결정적 규칙(3단계+ / heavy·compute·edit 단계 / 도구 2단계+ ⇒ Heavy)으로 판정. 미가동 시 보수적 Heavy 폴백.
3. **재활용** — 분해된 계획을 task에 저장 → 실행 시 힌트 주입 → TUI 상세뷰에 표시.

---

## 현재 상태

- **Phase 0 (PoC)**: ✅ Hermes primitive(비동기 실행/중단/재개) 실증 (`poc/`)
- **Phase 1 (큐 + 상태머신 + CLI 패스스루)**: ✅ 실제 Gemini로 end-to-end 검증
- **Phase 2 (선점형 스케줄링)**: ✅ Heavy 중 Light 유입 → 일시정지 → 재개 라이브 검증
- **Phase 3 (Alphred Gateway)**: ✅ OpenAI 호환 HTTP 게이트웨이 + 스케줄러 데몬
- **Phase 4 (운영 안정성 + 대시보드)**: ✅ transient 재큐, 크래시 복구, 웹 대시보드
- **#30719 안전망**: ✅ 라이프사이클 명령 차단 + 재시작 폭주 시 자동 정지 (`/safety`)
- **Cron 인터셉트**: ✅ 주기 작업을 큐 Pending 으로 편입 (`queue cron-tick`)
- **Classifier LLM 폴백 / 멀티모달 / MCP 서브서비스 태깅**: ✅
- **전용 Alphred TUI**: ✅ Textual 터미널 클라이언트(대화 + 실시간 큐 표 + 완료 알림)
- **실시간 작업 과정(SSE)**: ✅ 게이트웨이 `/chat/stream`으로 `tool.started/completed` + 답변 스트리밍
- **슬래시 명령**: ✅ `/` 입력 시 명령 팝업(`/help`,`/model`,`/clear`,`/queue …`,`/skills`,`/quit`)
- **라이브 토큰 + 큐 UX**: ✅ 답변 스트리밍, 큐 표에 우선순위 + ⚠확인필요
- **큐 키보드 조작**: ✅ Tab 으로 큐 패널 이동 → ↑/↓ 선택, Enter 상세, `c` 폐기, `p/r` 일시중지/재개, `+/-` 우선순위
- **계획 기반 분류**: ✅ 모호한 요청을 LLM이 하위작업으로 분해 → 결정적 Heavy/Light + 계획을 실행에 재활용 (`ALPHRED_PLANNER`)
- **단계별 진행 표시**: ✅ 백그라운드 run을 이벤트로 추적 → 큐 표 `진행 k⚙`, 상세뷰에 현재 도구 + 하위작업 체크리스트
- **Hermes 순정 유지**: ✅ Alphred 정체성은 전용 TUI에만; `hermes` 는 Alphred 흔적 0
- 후속: 실제 음성/이미지 디바이스, 업데이트 데몬화

---

## 개발

```bash
pip install -e ".[dev]"
pytest -q
```

## 구조

```
alphred/
  config.py         설정 (Hermes home/bin 해석 재사용)
  models.py         Task / TaskState
  state_machine.py  허용 전이 강제
  db.py             SQLite 저장소 (SSOT, 원자적 전이 + 감사 로그)
  classifier.py     Light/Heavy 분류
  nlq.py            자연어 큐 관리 (queue ask / /queue/ask)
  hermes_client.py  Hermes :8642 API 클라이언트
  queue_manager.py  우선순위 큐 + 단일 슬롯 스케줄러 + 선점/Light 즉시처리
  queue_md.py       QUEUE.MD 투영
  gateway.py        FastAPI 게이트웨이 + 백그라운드 스케줄러 + 크래시 복구
  dashboard.py      웹 대시보드 (단일 HTML, 의존성 없음)
  safety.py         #30719 안전망 (페이로드 필터 + 재시작 폭주 가드)
  cron_intercept.py 주기 작업 → 큐 편입 (자체 cron 매처)
  tui.py            전용 Alphred TUI (Textual 게이트웨이 클라이언트)
  tui_sessions.py   TUI 대화 영속화 (복원 가능한 세션)
  splash.py         TUI 시작화면 Alph-RED ASCII 배너
  cli.py            CLI 패스스루 + queue/serve/setup/tui/doctor 명령
poc/                Phase 0 primitive 검증
tests/              핵심 로직 테스트
```
