# Alphred

[English](README.md) | **한국어**

![AlphredLogo](alphred/assets/Alphred_Logo.png)

> [Hermes Agent](https://github.com/NousResearch/hermes-agent) 위에 얹는
> **우선순위 큐 미들웨어 + 상태 제어 머신** 래퍼 — **코어는 수정하지 않는다.**

여느 AI 비서처럼 Alphred에게 말을 걸면 됩니다. 빠른 요청("이거 번역해줘", "날씨 알려줘")은
**즉시** 답하고, 무거운 요청("전체 코드베이스 리팩토링해줘", "이 200페이지 크롤링해서 요약해줘")은
큐에 넣어 우선순위대로 백그라운드에서 처리합니다. 급한 게 들어오면 진행 중인 작업을 잠시 멈추고
먼저 처리한 뒤 다시 이어갑니다. **무엇이 급한지는 사용자가 표시하지 않아도 Alphred가 판단합니다.**

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
| **맥락 기반 우선순위 (§22)** | 새 Heavy 작업을 LLM 이 *큐 전체*와 비교해 순위 매김 — 긴급도 **및 의존성**("B를 A보다 먼저")으로 우선순위를 자동 재조정, 실행 중 작업도 선점. |
| **작업 심화도 + 검증 (§21)** | 가벼운 작업은 싸게, 무거운 작업은 기획·검증·자가치유. 파일을 *만들었다고 말만* 한 run 은 `Completed` 가 아니라 `NeedsReview` 로 갑니다. 심화도는 `/depth`·`X-Alphred-Depth`·`--depth` 로 오버라이드. |
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

작업 상태: `(AwaitingInput →) Pending → In-Progress → (Paused) → Completed / NeedsReview / Discarded`.
`AwaitingInput`(§34.4, opt-in)은 착수 전 Alphred 가 추천 답변이 달린 확인 질문을 하는 동안
작업을 대기시키는 상태 — 무응답이면 기록된 가정으로 자동 진행됩니다.

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
hermes             # (또는) 순정 Hermes TUI 가 필요하면 hermes 직접 실행(큐 없음)
```

`alphred` 는 게이트웨이에 붙는 전용 터미널 클라이언트(Textual)를 띄웁니다. 빠른 질문은 즉답,
무거운 요청은 백그라운드 큐로 오프로드되어 **실시간 표**로 보이고, 큐 작업이 끝나면 대화 중에도
**완료 알림**이 옵니다. 무인자 `alphred` 가 백그라운드 데몬도 자동 기동합니다. (`textual` 필요 — 기본 설치됨.)

**방식 2 — 서버 구동 (웹 대시보드 + 앱/디바이스용 OpenAI 호환 API):**

```bash
alphred serve --port 8643      # 게이트웨이 + 스케줄러; Hermes API(:8642) 자동 기동
```

**http://localhost:8643/** 로 웹 대시보드, **http://localhost:8643/chat** 로 웹 챗을 열거나,
OpenAI 호환 클라이언트를 `http://localhost:8643/v1` 로 가리키면 됩니다.
(직접 띄운 Hermes API를 쓰려면 `alphred serve --no-auto-hermes`.)

> `serve` 는 기본적으로 **127.0.0.1(로컬 전용)** 에 바인딩됩니다. 다른 기기에서 접속하려면
> 키를 먼저 발급하고 명시적으로 바인딩하세요 — 아래 *다기기 접속* 참조.

---

## 다기기 접속 — 한 사용자, 여러 기기 (§35)

Alphred 서버 하나, 큐 하나, 상태 하나 — 갖고 있는 모든 기기에서 접속합니다.
모든 클라이언트가 같은 엔진을 공유하므로, 휴대폰에서 넣은 작업이 TUI 에도 보입니다.

| 기기 / 모드 | 방법 |
|---|---|
| 서버 머신에서 TUI | `alphred` (데몬 자동 기동) |
| **다른 머신에서 TUI** | `alphred connect http://<서버>:8643 --key <키>` — 씬클라이언트(로컬 데몬을 절대 띄우지 않음). 세션은 기기에, 큐는 서버에 |
| **웹 챗** | `http://<서버>:8643/chat` — 스트리밍 답변·도구 과정·인테이크 **질문 카드(추천답변 강조)**, 단일 자립형 페이지 |
| 웹 대시보드(큐 운영) | `http://<서버>:8643/` |
| 외부 서비스 / OpenAI 클라이언트 | base URL `http://<서버>:8643/v1` + Bearer 키. Heavy ⇒ `202`(폴링 `GET /v1/runs/{id}` 또는 `"delivery":{"webhook":…}` 로 결과 푸시 수신) |
| **ESP32 / Arduino** | [`examples/esp32/`](examples/esp32/) — 최소 스케치(POST → 200 즉답 / 202 폴링). 임베디드는 인테이크 질문을 받지 않음(api 소스는 가정 기록 후 진행 — 설계) |

**서버에서 준비(기기당 1회):**

```bash
alphred keys issue 노트북             # 키는 지금 한 번만 표시(서버엔 해시만 저장)
alphred keys issue 모니터 --scope read  # read=모니터링 전용(GET만) / control=전부
alphred keys list / revoke <이름>     # 회수 = 그 기기 즉시 차단
alphred serve --host 0.0.0.0          # 외부 바인딩은 명시적으로 — 키 없이는 기동 거부
alphred setup                         # 설정 템플릿을 ~/.hermes/alphred/.env 에 생성
alphred service install               # 선택: 로그온 시 자동 기동(schtasks/systemd/launchd)
```

### `.env` 파일을 통한 영구 설정

다음 경로에 환경변수를 저장하여 설정을 영구적으로 유지할 수 있습니다:
- `~/.hermes/alphred/.env` (또는 `~/.hermes/.env`)

`alphred setup` 명령어를 실행하면 `~/.hermes/alphred/.env` 경로에 사용 가능한 모든 환경변수의 설명과 예시가 포함된 `.env` 템플릿 파일이 자동으로 생성됩니다 (Windows, macOS, Linux 전체 호환).

예를 들어, Tailscale 네트워크 인터페이스에 서버를 바인딩하여 다른 기기에서 접속 가능하게 설정하려면:
1. 외부 기기용 접속 키를 먼저 발급합니다:
   ```bash
   alphred keys issue ipad
   ```
2. `alphred setup`을 실행해 `.env` 템플릿을 생성합니다.
3. `~/.hermes/alphred/.env` 파일을 열고 `ALPHRED_GATEWAY_URL` 주석을 해제하여 아래와 같이 기재합니다:
   ```env
   ALPHRED_GATEWAY_URL=http://<Tailscale-IP-또는-0.0.0.0>:8643
   ```
4. 이후 단순히 `alphred` 명령어로 TUI를 시작하면, 백그라운드 데몬 서버가 자동으로 설정된 Tailscale IP와 포트로 기동되어 같은 Tailscale 네트워크의 다른 장치에서 웹 챗(`http://<Tailscale-IP>:8643/chat`)이나 대시보드에 접근할 수 있게 됩니다.

신뢰 LAN 밖 접속은 :8643 앞에 TLS 리버스 프록시(Caddy/nginx)를 두세요.

---

## 사용법

### A. 그냥 말하기 — Alphred이 자동 분류

이게 기본 경로입니다. 평소처럼 요청을 보내면 Alphred이 메시지마다 Light/Heavy를 판단합니다.

분류기는 **전용 TUI**(`alphred`)와 **HTTP 게이트웨이**(`:8643`)에서 돕니다. 그냥 말하면 됩니다 —
빠른 질문은 즉답, 무거운 요청("전체 코드베이스 리팩토링")은 자동으로 백그라운드 큐로 오프로드.

> 참고: *순정 Hermes*(큐 없음)가 필요하면 `hermes` 를 직접 실행하세요. 큐 결합 대화는 `alphred`(TUI)나 HTTP API.

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
alphred queue discard <id>        # 소프트 삭제 (Discarded 상태로 히스토리 보존)
alphred queue purge <id>          # 영구 삭제 (복구 불가; DB 에서 완전 제거)
alphred queue clear               # 종료된 작업 영구 삭제 (완료/검토필요/폐기)
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

`queue` / `serve` / `setup` / `tui` / `doctor` / `prompt` / `tune` 를 제외한 모든 명령은 Hermes로 그대로 위임됩니다
(종료 코드·스트리밍 보존). 새 Hermes 서브커맨드는 자동 노출됩니다.

```bash
alphred version          # == hermes version
alphred gateway run      # == hermes gateway run
hermes                   # 순정 Hermes TUI 는 hermes 를 직접 실행
```

**순수 Hermes 보장.** Alphred 정체성은 전용 `alphred` TUI(Alph-RED)에만 있습니다. Hermes를
재스킨/수정하지 **않으므로**, `hermes` 는 원본 로고/배너/색상/정체성 그대로,
Alphred 흔적이 전혀 없습니다.

---

## 게이트웨이 API

`alphred serve`(TUI가 자동 기동)는 **`http://localhost:8643`** 에 HTTP 게이트웨이를 엽니다.

### OpenAI 규격 호환 — 예

OpenAI SDK·도구의 base URL 을 **`http://localhost:8643/v1`** 로만 바꾸면 그대로 동작합니다:

- `POST /v1/chat/completions`, `POST /v1/responses`, `GET /v1/models` 는 **표준 OpenAI 요청 바디**를 받습니다. **Light(즉답/대화형)** 요청은 Hermes 로 프록시해 **OpenAI 응답 객체를 그대로 반환** — 진짜 drop-in base URL.
- **유일한 의도적 차이:** **Heavy(긴/백그라운드 작업)** 로 분류된 요청은 인라인 응답 대신 **큐에 등록**되어 HTTP `202` 와 `{"id" 또는 "run_id","status":"queued", ...}` 를 반환합니다. 결과는 `GET /v1/runs/{id}` 로 조회.
- **순수 동기 OpenAI 동작을 원하면?** `X-Alphred-Kind: light`(항상 인라인 응답, 큐 미등록). **전부 큐로?** `X-Alphred-Kind: heavy`. 헤더가 없으면 메시지별 자동 라우팅.

> 요약: 짧은 호출은 OpenAI 와 동일하게 동작하고, 긴 작업은 조회용 작업 id 를 돌려줍니다. 빠른 턴만 쓰는 기존 채팅 UI 는 수정 없이 작동합니다.

### 인증

`ALPHRED_API_KEY` 또는 `API_SERVER_KEY` 가 설정되어 있으면 모든 API 호출에 `Authorization: Bearer $KEY` 가 필요합니다. 키 미설정(개발 모드)이면 인증을 건너뜁니다. 대시보드 페이지(`/`)는 무인증이지만, 페이지 내 JS 는 API 호출 시 키를 포함합니다.

```bash
export KEY=your-token        # 아래 예시에서 사용; 키 미설정이면 헤더 생략
```

### 엔드포인트

| 메서드 & 경로 | 용도 |
|---|---|
| `POST /v1/chat/completions` | OpenAI Chat Completions. Light→동기 OpenAI 응답, Heavy→`202` 큐 등록. 무상태(`messages` 전체 재전송). |
| `POST /v1/responses` | OpenAI Responses API. Light→동기(멀티모달 `input`·`previous_response_id` 보존), Heavy→`202` 큐. |
| `POST /v1/runs` | **항상 비동기** 제출 → `202 {run_id}`. `input`·`session_id`·`conversation_history` 수용. 검증 루프 적용. |
| `GET /v1/runs/{id}` | 작업 상태 + 검증: `status`,`state`,`depth`,`needs_review`,`verify_attempts`,`verify_report`,`output`,`session_id`. |
| `POST /plan` | **드라이런** — 실행/큐등록 없이 `kind`/`priority`/`depth`/`plan`/`estimate` 반환. |
| `GET /v1/models` | 모델 목록(Hermes 프록시, OpenAI 형식). |
| `GET /models/available` | 현재 provider 의 선택 가능 모델 + `current`, 그리고 `reasoning`(사고 토큰을 내는 모델 — TUI 에서 💭 배지)·`current_reasoning`. |
| `GET /models/tiers` | §29.1 depth→모델 매핑(`high`/`mid`/`low` + `base`). |
| `POST /models/tiers` | depth 모델 설정/해제 — body `{"tier":"high\|mid\|low","model":"<이름>\|null","provider"?,"base_url"?}`. |
| `POST /models/default` | **영구** 기본 모델 설정 — body `{"model":"<이름>"}`. `config.yaml` default 기록 + 깊이별 tier 해제로 유지(재시작 후에도, 라우팅이 안 덮어씀). `known`(provider 카탈로그에 있는지) 반환. |
| `GET /v1/skills` | 설치된 Hermes 스킬(에이전트가 작업 중 자동 활용). |
| `GET /queue` | 전체 작업 목록(`{"tasks":[...]}`). |
| `GET /queue/{id}` | 작업 1건 + 상태전이 `events`. |
| `POST /queue/{id}/prio` | 우선순위 변경 — 바디 `{"priority": 1..10}`. |
| `POST /queue/{id}/pause` / `/resume` | In-Progress 일시중지(사용자 보류) / 재개 허용. |
| `POST /queue/{id}/retry` | `NeedsReview` 작업 재큐(Pending). |
| `POST /queue/{id}/answers` | §34.4 인테이크 답변 제출 — body `{"answers":[...]}`(질문 순서 문자열 또는 `[{"q","answer"}]`). `AwaitingInput → Pending` 승격, 답변은 실행 입력에 주입. |
| `DELETE /queue/{id}` | **폐기**(소프트 — `Discarded` 히스토리 보존). |
| `DELETE /queue/{id}/purge` | **영구 삭제** 1건(복구 불가). |
| `POST /queue/clear` | 종료된 작업 영구 삭제 → `{"cleared": n}`. |
| `DELETE /queue/by-session/{session_key}` | 세션에서 생성된 모든 작업 영구 삭제(세션 삭제 연쇄). |
| `POST /queue/ask` | 자연어 큐 제어 — 바디 `{"q":"..."}`. |
| `GET /queue/{id}/stream` | §33 실행 중 작업의 SSE 라이브 스트림(도구 활동·중간 텍스트·최종 결과). Hermes run 이벤트를 팬아웃; 실행 중 아니면 상태+`done` 만. |
| `GET /`, `GET /dashboard` | 웹 대시보드(단일 자립형 페이지). |
| `GET /chat` | §35.9 웹 챗 — 스트리밍 대화 UI+인테이크 질문 카드(단일 자립형 페이지). |
| `GET /safety`, `POST /safety/reset` | 재시작 폭주 가드 상태 / halt 리셋. |
| `GET /capabilities`, `POST /capabilities/refresh` | §34.5 실물 능력 스냅샷(스킬/도구/MCP/코딩 CLI/파이썬 라이브러리 + 형식별 생성 가능 여부) / 강제 재수집. |

### 요청 옵션 레퍼런스

**오버라이드 헤더**(모두 선택; `/v1/chat/completions`·`/v1/responses`·`/v1/runs` 에 적용):

| 헤더 | 값 | 효과 |
|---|---|---|
| `X-Alphred-Kind` | `light` \| `heavy` | 동기 응답 강제 vs 백그라운드 큐 강제(자동 분류 건너뜀). |
| `X-Alphred-Priority` | `1`..`10` | 우선순위(10=가장 급함). **주의:** 단독 지정 시 kind 도 결정 — `≥7 ⇒ Light`(동기), `<7 ⇒ Heavy`(큐). `X-Alphred-Kind` 와 함께 쓰면 둘을 독립 지정. |
| `X-Alphred-Depth` | `low` \| `mid` \| `high` | 작업 심화도 강제(검증/재시도 강도 게이팅) — 자동 판정 대체. |
| `X-Alphred-Source` | `chat`\|`api`\|`cron`\|`subservice`\|`tui` | 출처 태깅(MCP 등 하위 서비스 감사/라우팅). |

**바디 필드:**

| 필드 | 엔드포인트 | 의미 |
|---|---|---|
| `messages` / `input` | chat/completions, responses, runs | 표준 OpenAI 페이로드(문자열·메시지 배열·멀티모달 content 파트). |
| `model` | chat/completions, responses | 표준 OpenAI model 필드(Hermes 로 전달). |
| `previous_response_id` | responses | OpenAI Responses 체이닝(멀티턴 맥락). |
| `session_id` | runs | 동일 id 면 후속 Heavy run 이 한 Hermes(서버측) 세션 공유. 생략 → 독립(`session_id`=`run_id`). |
| `conversation_history` | runs | 명시적 1회성 맥락 핸드오프(메시지 리스트). |
| `priority` | queue/{id}/prio | 새 우선순위 `1..10`. |
| `q` | queue/ask | 자연어 지시. |

### 예시

#### 1) 빠른 채팅 — 동기, 표준 OpenAI

```bash
# 짧음/대화형 → 인라인 응답, 일반 OpenAI chat.completion 객체 반환
curl -s localhost:8643/v1/chat/completions -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"한 줄로 요약해줘: ..."}]}'
```

```python
# OpenAI SDK drop-in
from openai import OpenAI
client = OpenAI(base_url="http://localhost:8643/v1", api_key="your-token")
print(client.chat.completions.create(
    model="hermes-agent",
    messages=[{"role": "user", "content": "2+2는?"}],
).choices[0].message.content)
```

#### 2) 라우팅 강제 — kind / priority / depth

```bash
# 긴 프롬프트도 동기 강제(순수 OpenAI 동작, 큐 미등록)
curl -s localhost:8643/v1/chat/completions -H "Authorization: Bearer $KEY" \
  -H "X-Alphred-Kind: light" \
  -d '{"messages":[{"role":"user","content":"큐에 대한 하이쿠 하나"}]}'

# Heavy(백그라운드 큐) 강제 + 우선순위 + 심화도
curl -s localhost:8643/v1/chat/completions -H "Authorization: Bearer $KEY" \
  -H "X-Alphred-Kind: heavy" -H "X-Alphred-Priority: 9" -H "X-Alphred-Depth: high" \
  -d '{"messages":[{"role":"user","content":"전체 코드베이스 리팩토링"}]}'
# → 202 {"id":"<task>","status":"queued","object":"alphred.task"}
```

#### 3) 멀티모달 (이미지/오디오) — OpenAI content 파트

```bash
curl -s localhost:8643/v1/chat/completions -H "Authorization: Bearer $KEY" \
  -d '{"messages":[{"role":"user","content":[
        {"type":"text","text":"이 이미지에 뭐가 있어?"},
        {"type":"image_url","image_url":{"url":"https://example.com/cat.png"}}]}]}'
# 텍스트+이미지는 Light(즉답). 깊은 분석을 큐로 보내려면 X-Alphred-Kind: heavy 추가.
```

#### 4) Responses API — previous_response_id 멀티턴

```bash
R1=$(curl -s localhost:8643/v1/responses -H "Authorization: Bearer $KEY" \
       -d '{"input":"스타트업 아이디어 3개"}')
RID=$(echo "$R1" | jq -r '.id')
curl -s localhost:8643/v1/responses -H "Authorization: Bearer $KEY" \
  -d "{\"input\":\"2번 아이디어 확장\",\"previous_response_id\":\"$RID\"}"
```

#### 5) 백그라운드 runs — 항상 비동기

```bash
# 최소
curl -s localhost:8643/v1/runs -H "Authorization: Bearer $KEY" \
  -d '{"input":"미국 주식 PDF 리포트 작성"}'
# → 202 {"run_id":"...","status":"queued","kind":"heavy","priority":4,"depth":"high","session_id":"..."}

# 전체 조합: 헤더로 우선순위 + 심화도 + 출처
curl -s localhost:8643/v1/runs -H "Authorization: Bearer $KEY" \
  -H "X-Alphred-Priority: 8" -H "X-Alphred-Depth: mid" -H "X-Alphred-Source: subservice" \
  -d '{"input":"이 200페이지를 크롤링해서 요약"}'
```

#### 6) 세션 & 맥락 (멀티턴 Heavy)

```bash
# 동일 session_id 재사용 → 후속 Heavy run 이 한 서버측 Hermes 세션 공유
curl -s localhost:8643/v1/runs -H "Authorization: Bearer $KEY" \
  -d '{"input":"이번 분기 GPU 시장 조사","session_id":"gpu-research"}'
curl -s localhost:8643/v1/runs -H "Authorization: Bearer $KEY" \
  -d '{"input":"방금 내용을 한 장짜리 브리프로 정리","session_id":"gpu-research"}'

# 공유 세션 없이 명시적 1회성 맥락 핸드오프(conversation_history)
curl -s localhost:8643/v1/runs -H "Authorization: Bearer $KEY" \
  -d '{"input":"이어서 진행해줘",
       "conversation_history":[{"role":"user","content":"런치 플랜을 짜던 중이었어"},
                               {"role":"assistant","content":"1단계는 시장 규모 산정이었습니다"}]}'
```

| 경로 | 맥락 유지 방식 |
|---|---|
| `/v1/chat/completions` | 무상태 — 매 호출에 `messages` 전체 재전송. |
| `/v1/responses` | `previous_response_id` 로 체이닝. |
| `/v1/runs` | `session_id` 면 한 Hermes 세션 공유, 생략 시 독립, `conversation_history` = 명시 핸드오프. |
| 전용 TUI | `ALPHRED_HOME/tui_sessions/` 에 자동 영속·복원, `/sessions` 전환, `/sessions delete <번호|ID>` 삭제(그 세션의 큐 작업도 연쇄 삭제). |

#### 7) 드라이런 기획(실행 없음)

```bash
curl -s localhost:8643/plan -H "Authorization: Bearer $KEY" \
  -d '{"message":"미국 주식 PDF 리포트 작성"}'
# → {"kind":"heavy","priority":4,"depth":"high","classify_reason":"...",
#    "plan":{"subtasks":[...]},"estimate":{"steps":3,"est_llm_calls":7,"band":"높음"}}
```

#### 8) run 폴링 & 검증 결과

```bash
RID=$(curl -s localhost:8643/v1/runs -H "Authorization: Bearer $KEY" \
        -d '{"input":"미국 주식 PDF 리포트 작성"}' | jq -r .run_id)
curl -s localhost:8643/v1/runs/$RID -H "Authorization: Bearer $KEY"
# → {"status":"completed","needs_review":false,"depth":"high",
#    "output":"...","verify_report":{"passed":true,"checks":[...],"judge":{...}}, ...}
# needs_review=true ⇒ 산출물/수용 검증 실패 — verify_report 확인.
```

#### 9) 큐 관리

```bash
curl -s localhost:8643/queue -H "Authorization: Bearer $KEY"                 # 목록
curl -s localhost:8643/queue/$RID -H "Authorization: Bearer $KEY"            # 1건 + events
curl -s localhost:8643/queue/$RID/prio -H "Authorization: Bearer $KEY" -d '{"priority":9}'
curl -s localhost:8643/queue/$RID/pause  -H "Authorization: Bearer $KEY" -X POST
curl -s localhost:8643/queue/$RID/resume -H "Authorization: Bearer $KEY" -X POST
curl -s localhost:8643/queue/$RID/retry  -H "Authorization: Bearer $KEY" -X POST   # NeedsReview → Pending
curl -s localhost:8643/queue/$RID -H "Authorization: Bearer $KEY" -X DELETE         # 폐기(소프트)
curl -s localhost:8643/queue/$RID/purge -H "Authorization: Bearer $KEY" -X DELETE   # 영구 삭제
curl -s localhost:8643/queue/clear -H "Authorization: Bearer $KEY" -X POST          # 종료작업 정리
curl -s localhost:8643/queue/by-session/gpu-research -H "Authorization: Bearer $KEY" -X DELETE
```

#### 10) 자연어 큐 제어

```bash
curl -s localhost:8643/queue/ask -H "Authorization: Bearer $KEY" \
  -d '{"q":"리포트 작업을 최우선으로 올리고 크롤링 작업은 취소해"}'
```

#### 11) 모델·스킬·안전망

```bash
curl -s localhost:8643/v1/models -H "Authorization: Bearer $KEY"
curl -s localhost:8643/models/available -H "Authorization: Bearer $KEY"   # {current, provider, models[]}
curl -s localhost:8643/v1/skills -H "Authorization: Bearer $KEY"
curl -s localhost:8643/safety -H "Authorization: Bearer $KEY"             # halt 여부 / 재시작 횟수
curl -s localhost:8643/safety/reset -H "Authorization: Bearer $KEY" -X POST
```

### 검증 & 작업 심화도 (§21)

**큐에 등록된 모든 Heavy run**(`POST /v1/runs`)은 완료 처리 전에 검증 루프를 거칩니다 — Light 동기 호출(`/v1/chat/completions`, `/v1/responses`)은 검증 대상이 **아닙니다**.

- **심화도**(`low`/`mid`/`high`)가 작업별로 정해져 검증·재시도 강도를 게이팅 → 가벼운 작업은 토큰을 아낍니다. 자동 판정을 덮어쓰려면 TUI `/depth low|mid|high`(`/depth auto` 해제), `X-Alphred-Depth` 헤더, 또는 `alphred queue submit --depth high`.
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

### 결과물 품질 — 실행 하네스 (§26)

모든 백그라운드 **Heavy** 작업은 **시스템 프롬프트(하네스)** 가 사용자 요청 앞에 붙은 채로 실행됩니다. 이 하네스는 얕은 요약이 아니라 **깊이 있고 근거 있고 완결된** 결과를 내도록 모델을 가이드합니다(업계 대표 챗봇의 운영 방식에서 영감). 동시에 Alphred 의 강제 규칙 — 되묻지 않기, 도구로 산출물 **실제 생성**, 존재 **검증**, 실제 경로 보고 — 도 담고 있습니다.

하네스는 **편집 가능한 외부 파일**이라, 자기 분야에 맞게 품질 기준을 튜닝할 수 있습니다:

```bash
alphred prompt              # 요약: 적용 소스 + 경로
alphred prompt --show       # 적용 중인 하네스 전문 출력
alphred prompt --path       # 편집본 경로 / 적용 소스
alphred prompt --init       # 기본값을 ALPHRED_HOME/system_prompt.md 로 복사해 편집
#   (그 파일을 수정 → 이후 모든 Heavy 작업에 반영, 업데이트와 무관하게 유지. 데몬 재기동 시 로드.)
```

- 우선순위: `ALPHRED_HOME/system_prompt.md`(내 편집본) → 패키지 기본값(`alphred/assets/system_prompt.md`).
- 기본 하네스는 **영어**로 작성(토큰 효율·성능 유도)되며, 출력은 사용자 언어를 따르도록 지시합니다.
- **매칭되는 스킬 우선 사용**을 지시하고, **포맷별 디자인 가이드**(Markdown/PDF/DOCX/PPTX/XLSX)와 **OS별 명령 가이드**(Windows PowerShell / Linux bash / macOS zsh)를 포함합니다. 스킬/도구 목록은 **더 이상 하드코딩이 아닙니다**: 하네스의 `{{CAPABILITIES}}` 마커가 디스패치 시점에 실물 인벤토리(§34.5 — 설치 스킬, PATH 에 실제 있는 코딩 CLI, 실제 임포트 가능한 파이썬 라이브러리, 지금 진짜 만들 수 있는 파일 형식)로 치환됩니다(PDF 라이브러리가 없는데 가짜 PDF 를 "저장"하는 환각 차단). 편집본에 마커가 없으면 아무것도 치환하지 않습니다(하위호환).
- 작업의 **심화도**가 추가 지시를 덧붙입니다(`high` ⇒ 조사·다각도 분석·방법론 포함·엄격한 자기검증).
- 맨 아래 `## REQUEST` 구분선은 남겨두세요 — 실제 요청이 그 뒤에 붙습니다.

### 상업용 에이전트와의 품질 격차 좁히기 (§29)

품질 ≈ *50% 모델 + 50% 하네스*. §26 하네스가 Heavy 작업의 하네스 절반을 담당하고, §29가 나머지를 메웁니다 — 전부 **Hermes 코어 무수정**(Alphred 가 자기 파일/설정만 편집, 게이트 기본값=동작 무변화):

- **모델 선택 (§29.1).** TUI에서 **`/model <이름>` 은 기본 모델을 영구 설정**합니다(`config.yaml` 기록, 재시작 후에도 유지, 다시 바꾸기 전까지). **provider 접두 id**(예: `meta/llama-3.3-70b-instruct`, `google/gemma-4-31b-it`)를 쓰세요 — 접두어 없는 이름은 provider에서 404 날 수 있고, `/model`(무인자)이 유효 목록을 보여주며 없는 이름은 경고합니다. 깊이별 라우팅은 `ALPHRED_MODEL_HIGH/MID/LOW` 또는 `/model high|mid|low <이름>`(`/model high auto`=해제; 접두어 없는 `/model <이름>`은 깊이별 tier를 모두 해제). (depth 이름은 작업 무게 Heavy/Light 와 구분되는 high/mid/low). 무인자 `/model`=현재 매핑 표시. Alphred 가 디스패치 직전 `config.yaml` 의 `model.default` 를 해당 모델로 교체(Hermes 가 run 마다 재읽기) — 단일슬롯 스케줄링이라 안전. 같은 프로바이더 model-id 전환은 즉시 동작, 크로스 프로바이더는 `.env` 에 해당 자격증명 필요. tier 미설정 시 `config.yaml` 무손질.
- **Light 하네스 (§29.2).** 즉답에 짧은 Alphred 시스템 메시지를 붙여 바닐라 챗봇 품질(빈 `SOUL` 콜드스타트)을 막음. 기본 on, 호출자 `system` 메시지/`X-Alphred-Harness: off` 면 미주입. 편집: `alphred prompt --light --init`.
- **설정 품질 감사 — `alphred tune` (§29.3).** 품질 저하 설정(압축 보호 #1, 약한 보조모델 #2, 툴서치 과부하 #4, 웹검색 백엔드 #5)을 진단하고 `--apply` 시 *내* `config.yaml` 을 백업·멱등 편집으로 교정(`--revert` 원복). 기본 읽기전용, LLM 호출 없음.
- **Alphred-side MoA (§29.4).** `high` 심화도 run 에 `ALPHRED_MOA=1` 이면 비평→종합 패스로 단일 모델 첫 시도 이상으로 품질 상향(opt-in, `ALPHRED_MOA_SAMPLES` 예산).

### 스킬 — 기본 제공 · 옵션 · 설치형

Alphred 은 **Hermes 에이전트** 위의 얇은 레이어라, Hermes 의 모든 역량을 TUI 채팅과 큐 작업 양쪽에서 그대로 씁니다. 여기엔 에이전트의 **스킬 관리 툴**(`skill_manage` + 스킬 허브)도 포함되므로, **Alphred 를 통해 직접** 스킬을 설치·관리할 수 있습니다 — 그냥 말로 요청하면 됩니다.

| 계층 | 위치 | 기본 노출? |
|---|---|---|
| **번들** | `hermes-agent/skills/` | 예 (예: `nano-pdf`, `powerpoint`, `ocr-and-documents`, `claude-code`, `codex`, `opencode`). |
| **활성/사용자** | `~/.hermes/skills/` | 예 — 설치/생성한 스킬이 여기 위치. |
| **옵션** | `hermes-agent/optional-skills/` | **아니오** — 포함되나 비활성 (예: `antigravity-cli`=`agy`, `excel-author`, `dcf-model`). |
| **외부/허브** | GitHub 등 | 아니오 — 설치 시 보안 스캔 후 편입. |

**옵션(또는 외부) 스킬 설치 — 세 가지 방법:**

1. **Alphred 를 통해(가장 쉬움).** TUI(또는 채팅)에서 *"antigravity-cli 스킬 설치해줘"* 라고 요청 → Hermes 에이전트가 `skill_manage`/허브 툴로 `~/.hermes/skills/` 에 설치(`allow_lazy_installs` 기본 on). 짧은 요청이라 동기(Light)로 처리됩니다.
2. **설정 — 디렉터리 노출.** `~/.hermes/config.yaml` 에 옵션 경로 추가:
   ```yaml
   skills:
     external_dirs:
       - <HERMES_HOME>/hermes-agent/optional-skills/autonomous-ai-agents
   ```
3. **수동 복사.** 스킬 폴더를 `~/.hermes/skills/<name>` 으로 복사(스킬 재인덱스/재기동 필요할 수 있음).

> **CLI 래퍼 스킬은 2개 레이어.** `antigravity-cli`(`agy`)·`claude-code`·`codex` 같은 스킬은 *사용 절차 가이드*일 뿐 프로그램 자체를 포함하지 않습니다. **바이너리도 PATH 에 설치**되어 있어야 합니다(예: `agy --version` 확인). 스킬은 에이전트가 `terminal` 툴로 그 바이너리(`agy --print "..."`)를 올바르게 호출하도록 안내합니다.

스킬 설치/활성화 후엔 **Alphred 데몬을 재기동**(예: `alphred` 재실행, 또는 백그라운드 `serve` 종료)해야 에이전트가 스킬 세트를 다시 로드합니다. 필요하면 `system_prompt.md` 에 nudge(예: *"대형 코딩 작업은 가용 시 `agy` 에이전트 우선"*)를 넣어 유도할 수 있습니다.

#### Alphred 로 코딩 에이전트(Antigravity `agy`) 구동

아래가 갖춰지면 일반 Alphred 채팅에서 코딩 작업을 Antigravity CLI 에 넘길 수 있습니다 — Hermes 에이전트가 `terminal` 툴로 `agy` 를 구동합니다.

**사전 준비(1회):**
- **스킬 노출** — `antigravity-cli` 가 에이전트에 보여야 함(예: `~/.hermes/config.yaml` 의 `skills.external_dirs` 에 `optional-skills/autonomous-ai-agents` 추가). 확인: `curl localhost:8643/v1/skills | grep antigravity`.
- **바이너리 설치·인증** — `agy --version` 동작. Antigravity 는 자체 로그인(OS 키링/브라우저) 관리. `agy --print` 가 인증에서 실패하면 `agy` 를 한 번 대화형으로 실행해 로그인.
- **자율 실행 ON** — `ALPHRED_AUTONOMOUS_EXEC`(기본 on) 이어야 백그라운드 run 이 승인 게이트에 막히지 않음.
- 설정 변경 후 Alphred 데몬 재기동.

**요청문(작은 모델은 명시적으로).** 도구를 직접 지목하면 안정적입니다:

```text
Antigravity CLI(agy)를 사용해서 문자열을 뒤집는 Python 함수를
C:/Users/alpha/agy_demo/reverse.py 새 파일로 만들어줘. terminal 툴로 비대화형 실행 —
agy --print "..." 에 workdir 를 그 폴더로 설정. 그다음 파일을 다시 읽어 agy 출력과
파일 경로를 보고해줘.
```

nudge/스킬이 활성이면 더 짧게도 됩니다: *"`agy` 코딩 에이전트로 … 만들고 결과 보고해줘."* 이 작업은 Heavy 로 분류되어 백그라운드에서 실행됩니다 — 큐 패널에서 보거나 `GET /v1/runs/{id}` 로 폴링하세요. `agy` 가 없으면 에이전트는 `claude-code`/`codex`/`opencode` 또는 `execute_code` 로 폴백합니다.

---

## 진단

```bash
alphred doctor          # hermes 바이너리·:8642/:8643·모델/provider·depth별 모델·플래너·검증/judge·Light 하네스·MoA·큐+검증통계·안전망
alphred doctor --json   # 기계 판독용

alphred tune            # §29.3 Hermes config 품질 감사(읽기전용)
alphred tune --apply    # 권장 설정 적용(config.yaml 백업; --revert 로 원복)
```

**둘 다 라이브 LLM 호출 없음**(쿼터 안전). `doctor` 는 미응답 컴포넌트를 조치 힌트와 함께 표시합니다.

---

## 설정

| 환경변수 | 기본값 | 용도 |
|---|---|---|
| `ALPHRED_PROFILE` | `basic` | §35.4 §34 파이프라인 플래그 프리셋 — `basic`(큐/선점/검증만), `smart`(+의도판정·계획, 권장), `full`(+인테이크 질문·스텝 실행·감시). `alphred setup --profile <이름>` 으로 영속. 개별 `ALPHRED_*` env 가 항상 우선. |
| `ALPHRED_HERMES_API` | `http://localhost:8642/v1` | Hermes API Base URL |
| `API_SERVER_KEY` / `ALPHRED_API_KEY` | — | 게이트웨이 인증 토큰 |
| `ALPHRED_HOME` | Hermes home | Alphred 상태 디렉터리 |
| `ALPHRED_HERMES_BIN` | 자동 해석 | 패스스루용 Hermes 실행 파일 |
| `ALPHRED_MAX_RETRIES` | `3` | 일시적 실패 최대 재시도 |
| `ALPHRED_RETRY_BASE_SECONDS` | `5` | 지수 백오프 기준 시간 |
| `ALPHRED_CLIENT_TIMEOUT` | `300` | Hermes HTTP 클라이언트 타임아웃(초) — 긴 도구턴/설치 대비 여유 |
| `ALPHRED_STREAM_READ_TIMEOUT` | `600` | §32 — Alphred가 띄운 Hermes 게이트웨이에 `HERMES_STREAM_READ_TIMEOUT`로 주입. LLM 토큰간 스트림 읽기 타임아웃(Hermes 기본 120s)을 상향해, 느린 free-tier 모델(예: NVIDIA NIM의 70B)이 큐 작업을 `APITimeoutError: Request timed out.`로 실패시키지 않게 함. 단일 호출만 제한(전체 run 상한은 별도 유지). |
| `ALPHRED_AUTONOMOUS_EXEC` | **on** | Alphred 가 띄우는 Hermes 게이트웨이에 `HERMES_YOLO_MODE` 주입 → 백그라운드 run 이 `execute_code`/명령을 실제로 실행(manual 승인 모드면 승인 대기→타임아웃 차단됨). 하드라인(디스크삭제·셧다운)은 여전히 차단, Alphred 가 띄운 게이트웨이에만 적용. `0` 으로 Hermes 승인 프롬프트 유지. |
| `ALPHRED_LLM_CLASSIFY` | off | 모호한 입력에 단순 LLM 분류 폴백 활성화 |
| `ALPHRED_PLANNER` | off | 계획. §19: 모호 요청은 LLM 분해로 Heavy/Light 판정. §34.3(Plan v2): **모든 Heavy 작업이 디스패치 직전 실행 가능한 계획**을 받음 — 스텝별 목표·도구 힌트·기대 산출물·완료 기준(accept), **실물 능력 인벤토리에 접지**(라이브러리 없으면 설치 스텝 자동 삽입, 없는 스킬/CLI 힌트는 execute_code 강등, 수리 내역은 plan.gaps). 계획은 실행에 주입되고 TUI 상세뷰·`POST /plan` 드라이런에 표시. |
| `ALPHRED_VERIFY` | **on** | §21 Tier 0 — 완료 Heavy run 의 산출물 결정적 검증(무비용). `0` 으로 비활성화. |
| `ALPHRED_JUDGE` | off | §21 Tier 2 — `high` 심화도 run 에 LLM 수용 judge(쿼터 사용) + 자가치유 재시도 |
| `ALPHRED_JUDGE_RETRIES` | `2` | §21 Tier 3 — `high` 작업의 검증 재시도 상한(초과 시 `NeedsReview`) |
| `ALPHRED_RANK` | **on** | §22 LLM 큐 랭커 — Heavy 제출 시 긴급도+의존성으로 Heavy 작업들을 상대 재정렬. 경쟁할 다른 Heavy 가 없으면 LLM 미호출(no-op). `0` 으로 비활성화. |
| `ALPHRED_LIGHT_HARNESS` | **on** | §29.2 — **Light**(즉답) 응답 앞에 간결한 Alphred 시스템 메시지를 주입해 바닐라 챗봇 톤을 막음(빈 `SOUL` 콜드스타트 해소). 호출자가 이미 `system` 메시지를 보냈거나 `X-Alphred-Harness: off` 면 미주입. `0`=순수 패스스루. 편집: `alphred prompt --light --init`. |
| `ALPHRED_MODEL_HIGH` / `_MID` / `_LOW` | — | §29.1 depth별 모델 라우팅 — `high`/`mid`/`low` 작업에 쓸 모델(작업 무게 Heavy/Light 와 구분되는 depth 이름). `models.json`(=`/model high\|mid\|low <이름>`)보다 우선. 미설정 depth 는 base 기본값 사용. tier 를 **하나도** 안 정하면 `config.yaml` 을 전혀 건드리지 않음. |
| `ALPHRED_MOA` | off | §29.4 Alphred-side MoA — `high` 심화도 run 한정, 비평→종합 패스로 결과를 한 번 더 끌어올림(쿼터 사용). 기본 off. |
| `ALPHRED_MOA_SAMPLES` | `2` | §29.4 예산 상한(최대 후보/정제 패스 수). |
| `ALPHRED_CAPS` | **on** | §34.5 능력 레지스트리 — 에이전트가 *지금 실제로* 쓸 수 있는 것의 무LLM 스냅샷(설치 스킬·활성 도구·MCP 서버·PATH의 코딩 CLI·Hermes venv 파이썬 라이브러리 → 형식별 "PDF 진짜 만들 수 있나?" 매트릭스). Heavy 하네스의 `{{CAPABILITIES}}` 마커에 주입되고, `GET /capabilities`·`alphred doctor`·검증 실패 시 결정적 설치 힌트에도 쓰임. `0`이면 정적 하네스 텍스트. |
| `ALPHRED_CAPS_TTL` | `3600` | §34.5 능력 스냅샷 캐시 TTL(초). 데몬 시작·설치류 작업 완료 직후에도 갱신. |
| `ALPHRED_INTENT` | off | §34.2 IntentCard — LLM-first 의도 판정: 구조화 콜 1회로 kind+우선순위+심화도(+되묻기용 부족정보 신호)를 통합 판정, 정규식을 1차 판정자에서 강등. 정규식은 fast-path(상태조회/설치류/아주 짧은 인사)와 콜 실패 시 폴백으로 유지. 판정은 `intent_log`에 기록되어 정확도 측정에 쓰임. |
| `ALPHRED_CLARIFY` | off | §34.4 인테이크 질문 — IntentCard 가 대화형(TUI/chat) Heavy 요청에서 **critical** 부족정보를 표시하면, 착수 전 **추천 답변이 달린 질문 ≤3개**를 먼저 묻는다(Claude Code 식: Enter 만 치면 추천 채택). 작업은 `AwaitingInput` 에서 대기하고, 무응답 타임아웃 시 기록된 가정으로 진행(보고서에 표면화). 비대화형 소스(api/cron/subservice)는 질문 0. `ALPHRED_INTENT=1` 필요. |
| `ALPHRED_CLARIFY_TIMEOUT` | `600` | §34.4 답변 대기 시간(초) — 경과 시 추천값을 가정하고 진행. |
| `ALPHRED_ORCHESTRATE` | off | §34.6 StepRunner — Plan v2 가 있는 `high` 심화도 작업을 **스텝 단위로 실행**: 각 스텝이 좁은 Hermes run(세션 공유)으로 돌고, 직후 완료 기준을 결정적으로 검사(file/content/exit_code), 실패하면 **그 스텝만 피드백과 함께 재시도**(전체 재실행 없음). 선점/일시 장애 후엔 **현재 스텝부터 재개**(완료 스텝은 요약으로 맥락만 계승, 재실행 없음). 전체 검증(§21 Tier0/judge/MoA)은 마지막에 그대로 수행 — judge 실패 시 전체 재실행 대신 `fix` 스텝이 추가됨. `ALPHRED_PLANNER=1` 필요. |
| `ALPHRED_TASK_BUDGET` | `25` | §34.6 오케스트레이션 작업당 Hermes run 예산 — 초과 시 부분 성공 보고(`steps_done/steps_total`)와 함께 `NeedsReview`(무한 루프 불가). |
| `ALPHRED_STEP_RETRIES` | `2` | §34.6 스텝 수용검사 실패 시 스텝별 재시도 상한. 소진 ⇒ **재계획 1회**(완료 작업+실패 맥락을 플래너에 주고 남은 작업을 다른 접근으로 재계획, 예산 승계) ⇒ 그래도 실패 시 부분 성공 `NeedsReview`. |
| `ALPHRED_WATCHDOG` | off | §34.6 E3 실행 중 감시 — 잘못 가는 실행을 **도중에** 감지: 연속 도구 실패 ≥N(이벤트 스트림) 또는 `ALPHRED_STALL_SECONDS` 동안 무진전 ⇒ run 중단 후 교정 힌트("같은 접근 반복 금지 — 원인 진단 후 다른 도구/라이브러리로")와 함께 재큐. 오케스트레이션 작업은 현재 스텝에 힌트 주입; 반복 개입은 `ALPHRED_MAX_RETRIES` 상한 ⇒ `NeedsReview`. |
| `ALPHRED_STALL_SECONDS` | `600` | §34.6 E3 무진전 판정 기준(run 이벤트/DB 활동 없는 초). |
| `ALPHRED_TOOL_FAIL_LIMIT` | `3` | §34.6 E3 개입을 트리거하는 연속 도구 실패 임계. |
| `ALPHRED_SLOTS` | `1` | §38.2. 동시 실행할 Heavy 작업 슬롯 개수 (`auto` 혹은 정수). |
| `ALPHRED_SLOTS_MAX` | `4` | `ALPHRED_SLOTS="auto"`일 때 자동 스케일링 동시 실행 상한. |


### 분류 방식 (Heavy/Light 판정)

0. **IntentCard (opt-in, `ALPHRED_INTENT`)** — 켜면 아래 fast-path 를 제외한 모든 입력을 구조화 LLM 콜 1회가 판정(kind+우선순위+심화도+부족정보). 사전필터는 폴백 전용이 된다. "짧은 채팅인데 실제론 무거운 작업" 류의 오분류를 잡는다. `ALPHRED_CLARIFY=1` 이면 대화형 Heavy 요청의 critical 부족정보에 대해 착수 전 **추천 답변이 달린 인테이크 질문**(§34.4)까지 이어진다.
1. **싼 사전필터(LLM X)** — 상태/목록 조회→Light, **스킬/패키지 설치·활성화 요청→Heavy**(느린 관리 작업은 백그라운드로 → 동기 타임아웃 회피), 명시적 대규모("전체 코드베이스/마이그레이션/크롤" 또는 heavy 키워드 2개+)→Heavy, 인사/짧음/실시간 채팅→Light.
2. **모호한 중간대** — `ALPHRED_PLANNER` 켜짐 시 LLM이 하위작업으로 분해하고, 결정적 규칙(3단계+ / heavy·compute·edit 단계 / 도구 2단계+ ⇒ Heavy)으로 판정. 미가동 시 보수적 Heavy 폴백.
3. **디스패치 시점 실행 계획(§34.3)** — 플래너가 켜져 있으면 모든 Heavy 작업이 실행 직전 **Plan v2** 를 받는다: 요청+인테이크 답변+실물 능력 인벤토리로 구체적 스텝(도구 힌트·기대 산출물·완료 기준)을 만들고, 실물과 대조해 결정적으로 수리(설치 스텝 삽입/힌트 강등). task 에 저장·실행에 주입·`POST /plan` 으로 미리보기.
4. **상대 재정렬 (§22)** — 큐에 다른 Heavy 작업이 있는 상태에서 Heavy 가 제출되면, LLM 이 긴급도와 **의존성**으로 전체를 재정렬한다(예: "B를 먼저" 또는 "B가 끝나야 A가 의미 있음" → B가 A보다 높아지고, 실행 중 작업도 선점 가능). 신규·기존 작업의 우선순위가 함께 조정되고 스케줄러 선점이 실행 순서를 재정렬. 기본 ON(`ALPHRED_RANK`); 경쟁이 없으면 LLM 호출 없이 건너뜀.

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
- **슬래시 명령**: ✅ `/` 입력 시 명령 팝업(`/help`,`/model`,`/depth`,`/plan`,`/clear`,`/queue …`,`/answer`,`/sessions`,`/skills`,`/export`,`/banner`,`/exit`,`/quit`)
- **TUI 대개편 T1(§36)**: ✅ 위젯 채팅(도구 블록 `●/⎿` 제자리 갱신), 최종 답변 마크다운 렌더, 터미널 적응 테마(배경 강제 제거), 컴팩트 웰컴 패널(전체 아트는 `/banner`), 상태줄(스피너+경과시간 · 큐 배지 `▶⏳❓⚠`)
- **TUI 대개편 T2(§36)**: ✅ Esc 응답 즉시 중단, 응답 중 제출 = 완료 후 자동 전송, 인테이크 질문 카드(↑↓+Enter · ✦추천 기본 선택 · 직접 입력), 슬래시 fuzzy 매칭+인자 자동완성(`/model`·`/depth`·`/sessions`·`/queue`), 세션 피커 모달, Shift+Tab 심화도 순환, Ctrl+O 상세 토글(사고/도구 결과 전문)
- **TUI 대개편 T3 — 미션 덱(§36)**: ✅ 상주 큐 패널 폐지 → 큐 3계층: 상태줄 배지 + **대화 속 인라인 작업 카드**(견적/DoD·스텝 진행바·현재 스텝·선점 사유·검증 뱃지를 제자리 갱신) + **큐 덱 모달**(`Ctrl+T`/`/queue`: 리스트+상세+단일 실행 슬롯 시각화, 조작 키 상시 표시) · `/answer` 로 답변 대기 작업 소환 · 완료/검토/폐기/대기 전이 토스트+벨(`ALPHRED_TUI_BELL`)
- **TUI 대개편 T4 — 마감(§36)**: ✅ `Ctrl+Y` 마지막 답변 복사, `/export` 세션 Markdown 저장, 마우스 복귀(휠 스크롤/클릭 펼침), 저폭 터미널 상태줄 축약, 긴 세션용 채팅 위젯 상한
- **라이브 토큰 스트리밍**: ✅ 답변 스트리밍 + 큐 배지 + ⚠확인필요
- **계획 기반 분류**: ✅ 모호한 요청을 LLM이 하위작업으로 분해 → 결정적 Heavy/Light + 계획을 실행에 재활용 (`ALPHRED_PLANNER`)
- **단계별 진행 표시**: ✅ 백그라운드 run을 이벤트로 추적 → 인라인 작업 카드에 스텝 진행바+현재 스텝, 덱 상세에 계획 체크리스트+검증 증거
- **결과물 품질 (§29)**: ✅ depth별 모델 라우팅(`/model high|mid|light`, `ALPHRED_MODEL_*`), Light 하네스(즉답 시스템 메시지), `alphred tune`(Hermes config 품질 감사/적용), high 한정 Alphred-side MoA — 전부 코어 무수정
- **멀티에이전트 병렬화 및 한도 관리 (§38)**: ✅ `ALPHRED_SLOTS` 멀티에이전트 병렬 실행, AIMD 용량 제어, 일일 RPD 한도 게이팅(OpenRouter/NVIDIA NIM), Reasoning Gate
- **카테고리 특화 모델 라우팅 (§39)**: ✅ 9대 카테고리(Scout) 분류 및 `auto` 설정 시 자동 모델 라우팅, `scout-update` CLI
- **세션 컨텍스트 연속성 (§40)**: ✅ 세션 작업 원장(Session Ledger) 및 지시어 해소(Reference Resolution)를 통한 맥락 누수 해소
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
  budget.py         §38 프로바이더 일일 예산(RPD) 원장 및 AIMD 제어
  scout.py          §39 카테고리 특화 모델 카탈로그 및 Scout 업데이트
  classifier.py     Light/Heavy 분류
  nlq.py            자연어 큐 관리 (queue ask / /queue/ask)
  hermes_client.py  Hermes :8642 API 클라이언트
  prompt.py         실행 하네스 로더 (§26/§29.2) + 백그라운드 입력 조립
  verify.py         §21 산출물 검증 + 환각/되물음 휴리스틱
  llm_calls.py      Hermes 기반 보조 LLM 콜러블 (분류/계획/judge/랭킹/MoA)
  queue_manager.py  우선순위 큐 + 단일 슬롯 스케줄러 + 선점/Light 즉시처리
  queue_md.py       QUEUE.MD 투영
  eventbus.py       §33 인프로세스 run 이벤트 팬아웃 (Heavy 라이브 스트리밍)
  runtime.py        매니저 조립 + depth별 모델 적용기 (§29.1) + 이벤트 버스
  gateway.py        앱 조립(create_app) + 스케줄러 + Hermes(:8642) 업스트림 생명주기
  server/           FastAPI 라우터(그룹별)
    deps.py         GatewayDeps + 인증 + 공유 요청 헬퍼(route_realtime·task_view 등)
    routes_openai.py  /v1/chat|responses|runs|plan|models|skills + /chat/stream(SSE)
    routes_queue.py   /queue/* (조회/우선/일시중지/재개/재시도/폐기/영구삭제/자연어)
    routes_models.py  /models/available + /models/tiers (§29.1)
    routes_admin.py   / · /dashboard(무인증) + /safety(인증)
  tune.py           §29.3 Hermes config 품질 감사/적용 (코어 무수정)
  dashboard.py      웹 대시보드 (단일 HTML, 의존성 없음)
  safety.py         #30719 안전망 (페이로드 필터 + 재시작 폭주 가드)
  cron_intercept.py 주기 작업 → 큐 편입 (자체 cron 매처)
  tui.py            전용 Alphred TUI — App 코어(수명주기/렌더/세션 상태)
  tui_base.py       TUI 상수 + 명령 레지스트리 + 채팅/도구/카드/질문 위젯
  tui_commands.py   CommandsMixin — 슬래시 팔레트·입력 히스토리·명령 핸들러
  tui_queue.py      QueueMixin + QueueDeck — 미션 덱(배지/카드/덱/라이브/알림)
  tui_chat.py       ChatMixin — /chat/stream SSE 소비 + 렌더
  tui_sessions.py   TUI 대화 영속화 (복원 가능한 세션)
  splash.py         TUI 시작화면 Alph-RED ASCII 배너
  cli.py            CLI 패스스루 + queue/serve/setup/tui/doctor/prompt/tune
poc/                Phase 0 primitive 검증
tests/              핵심 로직 테스트
```

---

## TUI 사용 팁 및 가이드

### 1. 카테고리별 모델 자동 라우팅 (`auto`) 활성화 및 사용법
Alphred는 Heavy 작업을 9가지 카테고리로 분석하여 가장 알맞은 무료 모델로 자동 분기하는 기능을 갖추고 있습니다.
- **활성화**: TUI 대화창에서 `/model high auto` 또는 `/model mid auto` 명령을 실행하면 해당 depth(심화도)의 모델 매핑이 `"auto"` 센티널로 설정됩니다.
- **카탈로그 업데이트**: 카테고리별 매핑 목록을 최신화하려면, 셸(터미널)에서 `alphred queue scout-update` 명령을 실행하세요. 기본적으로 유료를 포함한 전체 최고 성능 모델(Claude 3.5 Sonnet, Gemini 2.5 Pro 등)을 평가 및 선정합니다. 만약 무료 모델로만 구성하고 싶다면 `alphred queue scout-update --free` 옵션으로 실행하세요. 자동으로 NVIDIA NIM 및 OpenRouter의 무료 모델 인벤토리를 실측 검증하고 사용 가능 제공자를 상세 출력(with `-v`)하며 로컬 카탈로그를 갱신합니다. 갱신된 매핑 정보는 TUI에서 `/model`을 치거나 CLI에서 `alphred model` 명령을 실행하여 이쁜 표 형태로 확인하실 수 있습니다.
- **동작**: 이후 큐에 Heavy 작업이 추가되면 자동으로 분류(예: 코딩 질문 -> `coding`)되고, 스케줄러가 디스패치할 때 해당 카테고리에 특화된 모델로 매핑되어 실행됩니다.

### 2. 동시(병렬) 실행 작업 설정 및 확인
- **병렬 슬롯 개수 확인**: `Ctrl+T` 또는 `/queue` 명령으로 **큐 덱(Queue Deck)** 모달을 열면, 상단 헤더에 `▶ 실행 슬롯 (active_slots/max_slots)` 형태로 노출됩니다. 예: `▶ 실행 슬롯 (2/4)`는 전체 4개 병렬 슬롯 중 2개가 작업 진행 중임을 뜻합니다.
- **병렬 실행 설정**: Alphred를 기동할 때 환경 변수 `ALPHRED_SLOTS` 값을 설정하세요.
  ```bash
  # 4개 작업을 동시에 병렬 수행하도록 설정
  $env:ALPHRED_SLOTS="4"
  # 대기 작업 수와 프로바이더의 속도 제한(RPM)에 맞춰 1~4개 사이에서 자동 조절
  $env:ALPHRED_SLOTS="auto"
  alphred serve
  ```

### 3. 작업 중 "확인 필요" 상태 처리 가이드
확인 필요 상태는 큐의 작업 상태에 따라 크게 두 가지로 나뉩니다:

- **입력 대기 (`AwaitingInput` - ❓ 배지)**:
  - **원인**: 작업 착수 전 에이전트가 실행에 필수적인 추가 정보를 요청하는 단계입니다 (`ALPHRED_CLARIFY=1` 인테이크 모드 활성 시).
  - **처리**: 큐 덱(`Ctrl+T`)에서 해당 작업을 선택한 뒤 단축키 `a`를 누르거나, TUI 입력창에 `/answer` (또는 `/answer <id>`)를 입력하면 **인테이크 질문 카드**가 소환됩니다. 여기서 추천 값을 선택하거나 직접 답변을 작성하면 작업이 Pending 상태로 변경되어 실행 대기열에 들어갑니다.
  
- **검토 필요 (`NeedsReview` - ⚠ 배지)**:
  - **원인**: 작업 수행이 끝났으나 **자동 완료 기준(DoD/verify/judge) 검증을 통과하지 못했거나**, **태스크당 실행 예산(Hermes run 제한)을 초과**하여 정지된 상태입니다.
  - **처리**: 큐 덱(`Ctrl+T`)의 우측 상세 패널(Detail)에 실패한 스텝 목록과 검증 검사 결과 및 에러 내용이 나타납니다. 확인 후 다음과 같이 대응할 수 있습니다:
    - **`R` (재시도/Retry)**: 환경 또는 소스 코드를 수정한 뒤 작업을 다시 처음부터 실행합니다.
    - **`d` (폐기/Discard)**: 작업을 대기열에서 제거(취소)합니다.
    - **`r` (재개/Resume)**: 검증 실패를 무시하고 강제로 작업을 재개하여 다음 단계를 밟게 합니다.

- **이전 작업 진행 상황 확인법**:
  - `L` 단축키를 통한 **라이브(Live) 스트림 뷰**는 접속한 시점부터 발생하는 이벤트 스트림을 실시간 중계하므로 연결 전 과거 출력은 다시 보여주지 않습니다.
  - 대신, TUI 대화창 내의 **인라인 작업 카드**의 진행바 또는 **큐 덱(`Ctrl+T`) 우측 상세 패널**을 통해 이미 완료된 계획(Plan) 스텝 목록과 각 스텝별 생성 파일 및 중간 출력을 스크롤하며 손쉽게 확인할 수 있습니다.
