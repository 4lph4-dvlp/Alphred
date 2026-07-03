# Alphred 실행 기획안 (Execution Plan)

> 기반 문서: `Alphred 프로젝트 상세 기획안.pdf` (Antigravity 개발본부 전달용)
> 작성일: 2026-06-20 / 작성: Claude (Opus 4.8)
> 성격: 상세 기획안(설계도)을 **구현 가능한 실행 계획**으로 번역한 문서

---

## 0. TL;DR

- **Alphred = Hermes Agent v0.16.0 위에 얹는 "우선순위 큐 미들웨어 + 상태 제어 머신" 래퍼.** Hermes 코어는 **수정하지 않는다.**
- 사용자 핵심 의도: 들어오는 모든 작업을 **Light(즉시 처리) / Heavy(지연 가능)** 로 분류하고, Heavy는 우선순위 큐(`QUEUE.MD` + DB)에서 상태(`시작 전·진행 중·일시중지·완료·폐기`)로 관리한다. **Heavy 실행 중 Light가 들어오면 Heavy를 일시중지 → Light 처리 → Heavy 재개**(선점형 스케줄링).
- 실측 결과, Hermes는 이 워크플로우에 필요한 **토대(primitive)를 이미 다 갖추고 있다**:
  - 비동기 실행 + 중단: `POST /v1/runs` → `run_id` 즉시 반환, `POST /v1/runs/{id}/stop`, `GET /v1/runs/{id}/events`(SSE)
  - 무손실 재개: `POST /v1/responses` + `previous_response_id`(서버측 대화/툴 상태 보존)
  - 주기 작업 인터셉트: `cron/scheduler.py`의 `tick()`(60초) / `run_job()`
- 단, **`priority` 개념과 큐 자체는 Hermes에 없다.** → 이것이 Alphred가 추가하는 핵심 가치.
- 권장 스택: **Python 3.11 + FastAPI + SQLite** (Hermes와 동일 런타임 → 모듈 직접 import 가능, 배포 단순).

---

## 1. 현황 파악 (실측 기준)

### 1.1 사용자의 핵심 의도
| 개념 | 정의 | Hermes 매핑 |
|---|---|---|
| **Light 작업** | 짧은 시간 내 즉시 응답해야 하는 대화/조회 | 동기 `chat/completions` 또는 즉시 실행 run, 높은 priority |
| **Heavy 작업** | 시간이 걸리고 지금 당장일 필요 없는 백그라운드 작업 | 비동기 `/v1/runs`, 낮은 priority, 큐 대기 |
| **선점(Preemption)** | Heavy 진행 중 Light 유입 시 Heavy 일시중지 → Light 우선 → Heavy 재개 | `stop` + `previous_response_id` 재개 |
| **상태 관리** | 시작 전 / 진행 중 / 일시중지 / 완료 / 폐기 | Alphred 상태머신(신규) |

### 1.2 실물 Hermes 설치 확인
- 위치: `C:\Users\alpha\AppData\Local\hermes\hermes-agent`
- 버전: **Hermes Agent v0.16.0 (2026.6.5)**, Python 3.11.15, OpenAI SDK 2.24.0, "Up to date"
- **`~/.hermes` 미생성** → 아직 `hermes init`/최초 실행 전. (Phase 0에서 초기화 필요)

### 1.3 기획서 ↔ 실물 대조 (검증 완료)
| 기획서 주장 | 실물 확인 | 비고 |
|---|---|---|
| OpenAI 호환 서버, 포트 8642 | `gateway/platforms/api_server.py` (4,316 LOC) | 라우트 실재 |
| `/v1/chat/completions`, `/v1/responses`, `/v1/runs/{id}` | 모두 존재 (+`/events`, `/stop`, `/approval`) | **`/v1/runs`가 큐의 핵심 토대** |
| `previous_response_id`로 컨텍스트 영속 | `/v1/responses` 본문에서 처리 확인 | 선점 재개의 근거 |
| 크론 tick 60초 주기 | `cron/scheduler.py: tick()` / `run_job()` | 인터셉트 지점 |
| SQLite 상태 저장 | `hermes_state.py: SessionDB` (sessions/messages 테이블) | Alphred는 별도 DB 권장 |
| update 파이프라인 / 이슈 #30719 (재시작 루프) | `gateway/restart.py` 존재, 위험 실재 | 페이로드 필터 안전망 필요 |
| `/v1/runs` POST에 `priority` | **없음** | 큐/우선순위는 Alphred 신규 영역 |

> 결론: 기획서는 실물과 정확히 일치하며 과장이 없다. Alphred가 채워야 할 빈칸은 **우선순위 큐 + 상태머신 + 선점 로직 + 분류기** 4가지로 명확히 좁혀진다.

---

## 2. 아키텍처 핵심 결정

### 2.1 통합 전략: "외부 미들웨어 우선 + 얇은 cron 인터셉트"
```
[Android앱 / ESP32 / 웹 대시보드 / API 클라이언트]
        │  OpenAI 규격 요청 (Base URL = Alphred)
        ▼
┌──────────────────────────────────────────────┐
│  Alphred Gateway  (FastAPI, :9000)             │  ← Alphred가 만드는 신규 계층
│  ├─ Classifier        (Light/Heavy 판정)        │
│  ├─ Priority Queue Manager + 상태머신 (SQLite)  │
│  ├─ Preemption Engine (pause/resume 조율)       │
│  └─ QUEUE.MD 동기화 + 대시보드 API              │
└──────────────────────────────────────────────┘
        │  내부적으로 OpenAI 규격 재호출
        ▼
┌──────────────────────────────────────────────┐
│  Hermes Gateway (:8642)  ── 수정 없음(black-box)│
│  /v1/runs · /v1/responses · /v1/runs/{id}/stop │
└──────────────────────────────────────────────┘
        ▲
        │ cron tick 인터셉트(얇은 어댑터)
   cron/scheduler.py (주기 작업 → 큐로 우회)
```
- **원칙: Hermes 코어 수정 0.** 업스트림 자동 업데이트(기획 1장)와 충돌 방지. Hermes는 "실행 엔진"으로만 사용.
- **예외: cron 인터셉트 1곳.** 주기 작업을 즉시 실행이 아니라 Alphred 큐의 `Pending`으로 넣으려면 tick 가로채기가 필요(기획 5.1). 이는 코어 패치가 아니라 **어댑터/래퍼 훅**으로 구현(모듈 import 후 등록, 또는 Alphred가 자체 스케줄러를 돌리고 Hermes cron은 비활성).

### 2.2 기술 스택 (권장)
| 영역 | 선택 | 이유 |
|---|---|---|
| 언어/런타임 | Python 3.11 (Hermes venv 재사용 또는 분리 venv) | Hermes 모듈 직접 import 가능, 배포 단순 |
| 웹 프레임워크 | FastAPI + Uvicorn | OpenAI 규격 SSE 스트리밍/비동기 친화, Hermes와 동형 |
| 큐/상태 영속 | SQLite (`alphred.db`, WAL 모드) | Hermes와 동일 패턴, 단일 노드 충분, 추후 Postgres 이관 용이 |
| 작업 실행 | Hermes `/v1/runs` 비동기 API | 중단/재개/이벤트가 이미 구현됨 |
| Light/Heavy 분류 | 휴리스틱 + 소형 LLM 라우터(폴백) | 빠르고 저비용, 모호하면 LLM 1회 호출 |

### 2.3 Light/Heavy 분류 정책 (초안)
1. **명시 우선**: 요청 헤더/필드 `X-Alphred-Priority`(1–10)가 있으면 그대로 사용.
2. **휴리스틱**: 실시간 대화 채널(chat/completions, 음성)·짧은 입력·즉답 키워드 → Light(8–10). 크론/서브서비스 트리거·"분석/리팩토링/크롤링/리포트" 의도 → Heavy(1–5).
3. **LLM 폴백**: 모호 시 소형 모델에 `{light|heavy, priority 1-10, 사유}` 1회 질의.
4. 분류 결과는 작업 레코드에 기록(감사/튜닝용).

---

## 3. 컴포넌트 설계

### (A) Alphred Gateway (OpenAI 호환 프록시)
- Hermes와 동일한 OpenAI 규격 표면 노출(`/v1/chat/completions`, `/v1/responses`, `/v1/runs*`, `/v1/models`).
- 프론트엔드는 Base URL만 `http://<alphred>:9000/v1`로 바꾸면 됨(기획 3.1과 동일 약속 유지).
- 동작: 요청 수신 → Classifier → Light면 즉시 통과(필요 시 선점 트리거), Heavy면 큐 등록 후 `run_id`(=Alphred task UUID) 반환.

### (B) Priority Queue Manager + 상태머신
- 상태: `Pending(시작 전) → In-Progress(진행 중) → Paused(일시중지) → Completed(완료) / Discarded(폐기)`.
- 전이 규칙(기획 2.1 준수):
  - `Pending → In-Progress, Discarded`
  - `In-Progress → Paused, Completed, Discarded`
  - `Paused → In-Progress, Discarded`
  - `Completed`/`Discarded`는 최종 상태.
- 동시 실행 슬롯(초기 1슬롯 권장: LLM/샌드박스 단일 점유 가정). 슬롯 가용 시 Pending 중 최고 priority를 꺼내 실행.

### (C) Classifier — 2.3 정책 구현.

### (D) Preemption Engine (선점 핵심)

> ⚠️ **실측으로 발견한 핵심 제약 (2026-06-20, 정적 분석):**
> `_response_store.put()`(재개용 컨텍스트 저장)은 **`/v1/responses` 핸들러에만** 존재하고
> 비동기 **`/v1/runs` 핸들러에는 없다.** `/v1/runs/{id}/stop`은 `agent.interrupt()`+`task.cancel()`만 수행하며
> response_id를 남기지 않는다. 따라서 **"runs로 실행 → stop → previous_response_id 재개"는 실물에서 그대로 성립하지 않는다.**
> 또한 진정한 토큰 단위 일시정지는 불가능하며, 현실적 선점 단위는 **턴(turn) 경계**다.

**그래서 채택하는 설계: "Alphred가 conversation_history의 단일 진실원천(SSOT)이 된다."**
- `/v1/runs`와 `/v1/responses` **둘 다 요청 본문에 `conversation_history`를 받는다**(실측 확인). 이를 이용해 Alphred가 누적 대화/툴 결과를 직접 보관한다.
- Heavy 작업은 **턴 단위**로 실행한다. 각 턴 완료 시 Hermes가 돌려준 결과를 Alphred가 conversation_history에 append.
- 선점 시퀀스(수정안):
  1. 고순위 유입 → 현재 In-Progress priority와 비교.
  2. 신규 priority > 현재 → `POST /v1/runs/{id}/stop`으로 현재 턴 중단, 상태 `Paused`. (진행 중이던 턴은 폐기 또는 부분 결과 보존)
  3. 신규 작업 슬롯 할당 → `In-Progress` → 즉시 응답.
  4. 신규 `Completed` → 스케줄러 재평가 → `Paused` 작업을 **Alphred가 보관한 conversation_history를 본문에 실어** 새 run으로 재개 → `In-Progress`.
- `previous_response_id`는 폐기하지 않는다: 정상 멀티턴 연속성·대역폭 절감(특히 모바일)에는 여전히 유용. 단 **선점-재개의 신뢰 경로는 Alphred-보관 conversation_history**다.
- **Phase 0 PoC가 실증할 것**: (A) `/v1/responses`+previous_response_id 재개 vs (B) conversation_history 패스스루 재개를, 실제 stop 이후 각각 시도해 어느 쪽이 손실 없이 재개되는지 확정.

### (E) Cron Intercept Adapter
- 기본 Hermes는 tick 시 작업을 즉시 강제 실행. Alphred는 만료 작업을 **큐의 Pending으로 편입**(기획 5.1).
- 구현 옵션 비교(§8 결정 필요):
  - (a) Hermes cron 비활성 + Alphred가 `jobs.json`을 읽어 자체 스케줄러 운영 ← **권장**(코어 무수정).
  - (b) `cron/scheduler.py`에 어댑터 훅 등록.
- 안전장치: 격리 세션 내 cronjob 재귀 생성 차단(기획 5.2), 작업 결과는 비동기 전달처(텔레그램/디스코드/이메일)로 발송.

### (F) QUEUE.MD 동기화 + 대시보드
- DB가 단일 진실원천(SSOT), `QUEUE.MD`는 사람이 읽는 **투영(projection)** 으로 자동 생성/갱신.
- 대시보드 API: 큐 조회, 드래그&드롭 순서 변경(priority 재배정), 강제 일시중지/재개/폐기.

### (G) CLI 패스스루 계층 (`alphred` = `hermes`의 1:1 슈퍼셋)
> QA 1.x를 충족하기 위한 설계. Alphred는 **Hermes CLI를 그대로 감싸는 drop-in CLI**여야 한다.
- **명령 1:1 매핑**: `alphred <sub> ...`는 기본적으로 `hermes <sub> ...`로 **verbatim 위임**(동일 인자/옵션/종료코드/표준출입력 패스스루). Alphred가 가로채는 것은 소수의 확장/오버라이드 서브커맨드뿐:
  - `alphred gateway` → Hermes 게이트웨이 + Alphred 미들웨어를 함께 기동.
  - `alphred queue ...` → 신규(큐 조회/우선순위 변경/일시중지/재개/폐기).
  - 나머지(`chat, model, cron, mcp, secrets, update, ...`)는 전부 Hermes로 통과.
- **설정 자동 발견/생성(1.2)**: Hermes의 `get_hermes_home()` 해석 규칙을 **그대로 재사용**(`HERMES_HOME` 우선 → Windows `LOCALAPPDATA\hermes`, Unix `~/.hermes`). 미설정이면 Hermes의 초기화 흐름을 그대로 트리거해 설정을 생성·로드. Alphred는 별도 home을 만들지 않고 Hermes home을 공유.
- **버전/업데이트 패스스루**: `alphred update`/`alphred version`은 Hermes 파이프라인을 그대로 호출(기획 1장), 단 #30719 안전망 래핑 적용.
- **불변식**: 어떤 Hermes 서브커맨드도 Alphred에서 "사라지거나" 동작이 달라져선 안 된다(QA 1.3의 1:1 검증 대상). 신규 Hermes 버전에서 서브커맨드가 추가되면 자동으로 패스스루되도록 **하드코딩 목록이 아니라 동적 위임**으로 구현.

---

## 4. 데이터 모델 (초안)
```sql
CREATE TABLE tasks (
  id            TEXT PRIMARY KEY,        -- UUID
  source        TEXT,                    -- chat | api | cron | subservice
  kind          TEXT,                    -- light | heavy
  priority      INTEGER,                 -- 1..10
  state         TEXT,                    -- Pending|In-Progress|Paused|Completed|Discarded
  prompt        TEXT,
  hermes_run_id TEXT,                    -- /v1/runs id
  response_id   TEXT,                    -- 재개용 previous_response_id
  session_key   TEXT,                    -- 장기 메모리 스코프
  delivery      TEXT,                    -- 결과 전달처(JSON)
  result        TEXT,
  created_at    TEXT, updated_at TEXT, started_at TEXT, finished_at TEXT,
  paused_reason TEXT, error TEXT
);
CREATE TABLE task_events (               -- 상태 전이 감사 로그
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT, from_state TEXT, to_state TEXT, reason TEXT, at TEXT
);
```

---

## 5. 핵심 워크플로우: 선점 시나리오 (기획 2.2 재현)
```
[Heavy: 레벨3 대규모 리팩토링 = In-Progress]
   │  사용자가 레벨10 "즉시 웹검색+요약" 요청
   ▼
1. Classifier → Light, priority=10
2. Preemption: 10 > 3 → /v1/runs/{refactor}/stop, response_id 저장, state=Paused
3. 웹검색 작업 슬롯 할당 → In-Progress → 즉시 응답 스트리밍
4. 웹검색 Completed → 슬롯 반환
5. 스케줄러 재평가 → 최고 priority Paused = 리팩토링
6. /v1/responses(previous_response_id) 재개 → In-Progress (컨텍스트 무손실)
```

---

## 6. 단계별 로드맵

> 우선순위는 기획서·사용자 의도에 따라 **2장(큐)이 최상위**. Pillar 순서가 아니라 가치 순서로 배치.

### Phase 0 — 토대 (0.5주)  ✅ **PoC 완료 (2026-06-20)**
- [x] LLM 자격증명 확인: Gemini(Google AI Studio) `gemini-3.5-flash` 정상(curl HTTP 200 + `hermes chat -q` 응답).
- [x] API 서버 가동: `API_SERVER_ENABLED=true`+`API_SERVER_KEY`로 `hermes gateway run` → :8642 정상(`/v1/models` 200).
- [x] primitive 실증(`poc/verify_primitives.py`):

| 테스트 | 결과 | 의미 |
|---|---|---|
| T1 `/v1/runs` 비동기 실행 | ✅ PASS | 202 + run_id 즉시 수신 → Heavy 비동기 투입 가능 |
| T2 `/v1/runs/{id}/events` SSE | ⚠️ 불안정 | 이벤트가 간헐적(1건 후 404 또는 0건). **진행률은 상태 폴링/`/v1/responses` 스트리밍으로 대체** |
| T3 `/v1/runs/{id}/stop` | ✅ PASS | `running → stopping → cancelled` 실측 → **선점(일시중지) 토대 확인** |
| T4-A `/v1/responses` + `previous_response_id` 재개 | ✅ PASS | 완료 턴은 무손실 재개(`7` 회상 성공) |
| T4-B `conversation_history` 패스스루(`/v1/runs`) | ✅ 수락(202) | Alphred SSOT 재개 경로 성립 |

- **PoC 결론**:
  1. 큐+선점에 필요한 핵심 primitive(비동기 실행·중단·재개)는 **실물에서 모두 동작**한다.
  2. **선점-재개 기준 채택**: 정상 멀티턴은 T4-A(`previous_response_id`), **중단(cancelled)된 Heavy의 재개는 T4-B(Alphred 보관 conversation_history)** — 정적 분석 가설(§3-D)이 실증으로 확정됨(`/v1/runs`는 재개용 response_id를 저장하지 않으므로).
  3. **진행률 표시**는 `/v1/runs/{id}/events`에 의존하지 말고 **상태 폴링(`GET /v1/runs/{id}`, 정상 동작) + chat/responses 스트리밍**으로 구현(QA-5.2 영향).
- [ ] Alphred 레포 스캐폴딩(FastAPI, SQLite, 설정) — *Phase 1 착수*.

### Phase 1 — MVP: 우선순위 큐 + 상태머신 (1.5주) ★핵심  ✅ **MVP 완료 (2026-06-20)**
- [x] tasks/task_events DB + 상태머신(전이 규칙 강제). → `db.py`, `state_machine.py` (원자적 전이 + 감사 로그)
- [x] Heavy → 큐 등록, Light → 높은 우선순위 분류. → `classifier.py`, `queue_manager.submit()`
- [x] 단일 슬롯 스케줄러(Pending에서 최고 priority 실행 → Completed). → `queue_manager.tick()`
- [x] `QUEUE.MD` 자동 생성 + CLI 조회/우선순위변경/폐기. → `queue_md.py`, `cli.py queue`
- [x] **CLI 패스스루**(`alphred`=`hermes` 1:1 동적 위임). → `cli.py`
- [x] 테스트 13종 통과 + **실제 Gemini로 end-to-end 검증**(Heavy 2건 우선순위순 실행, 결과 수집).
- **검증 결과**: `HIGHPRIO`(prio6)가 `LOWPRIO`(prio2)보다 먼저 실행·완료, 상태 전이/QUEUE.MD 반영 확인.
- **이월(Phase 2)**: 실시간 Light 즉시통과/선점, Alphred Gateway(FastAPI) OpenAI 표면, LLM 분류 폴백.

### Phase 2 — 선점형 스케줄링 (1.5주) ★차별점  ✅ **완료 (2026-06-20)**
- [x] Preemption Engine: 진행 중 Heavy보다 높은 Pending 유입 시 `stop_run`→`Paused`(paused_reason 기록), Light 완료 후 재개. → `queue_manager._maybe_preempt/_preempt`
- [x] 재개: Paused→In-Progress 를 `next_runnable()`(Pending+자동재개 Paused)로 우선순위순 선택, 새 run 으로 이어감(T4-B history 경로). → `db.next_runnable`, `_start`
- [x] 동급/저순위 비선점(QA-4.4), 연쇄 선점(QA-4.3), 사용자 명시 pause/resume(user hold 자동재개 제외).
- [x] 테스트 4종(`tests/test_preemption.py`) + **실제 Gemini 라이브 검증**.
- **검증 결과**: Heavy(prio3) 상태전이 `Pending→In-Progress→Paused(prio10에 선점)→In-Progress(재개)→Completed`, 그 사이 Light(`PING`) 완료. §5 시나리오 그대로 재현.
- **이월(Phase 3)**: Classifier LLM 폴백, 토큰 단위가 아닌 turn 단위 선점의 부분결과 보존 고도화.

### Phase 3 — Alphred Gateway / 다중 프론트엔드 (2주)  🔵 **게이트웨이 완료 (2026-06-20)**
- [x] **Alphred Gateway(FastAPI, :8643)** — OpenAI 호환 표면 + 백그라운드 스케줄러 스레드 한 프로세스. → `gateway.py`, `alphred serve`
  - `/v1/chat/completions`·`/v1/responses` = Light(즉시, 선점 동반) Hermes 프록시
  - `/v1/runs` = Heavy(비동기) 큐 등록 → task_id, `/v1/runs/{id}` 상태 매핑
  - `/queue/*` = 큐 관리(list/prio/pause/resume/discard), `/v1/models` 프록시
  - 헤더 오버라이드 `X-Alphred-Priority/Kind`, Bearer 인증(QA-7.8)
  - 동시성: sync 핸들러(스레드풀) + QueueManager 락 직렬화(QA-7.5)
- [x] 테스트 8종(`tests/test_gateway.py`) + **실제 Gemini 라이브 검증**: Light `HELLO` 즉시응답, Heavy 큐 등록, **게이트웨이 경유 실시간 선점**(상태이력 `In-Progress→Paused(light 선점)→In-Progress(재개)`) 확인.
  - ⚠️ 라이브 중 Gemini **무료 티어 쿼터(429)** 소진으로 일부 재개 run 이 실패 → Alphred 로직 정상, 다운스트림 LLM 한도 문제. **Phase 4 에 transient 실패 재큐(QA-4.6) 추가 필요.**
- [ ] 대시보드 UI(큐 드래그&드롭), 음성(STT/TTS)·이미지(vision_analyze) 파이프라인, ESP32/안드로이드 샘플 — *후속.*

### Phase 4 — 운영 안정성 / 대시보드 / 오케스트레이션  🔵 **안정성·대시보드 완료 (2026-06-20)**
- [x] **운영 안정성**: transient 실패(429/네트워크 등) 시 Discard 대신 **지수 백오프 재큐**(QA-4.6), **크래시 복구**(시작 시 고아 In-Progress 정리: 완료면 마감, 아니면 재큐, QA-7.7). → `queue_manager._handle_failure/recover`, `is_transient_error`
- [x] **대시보드 UI**: 의존성 없는 단일 페이지(`dashboard.py`) — 큐 조회/자동새로고침, **드래그&드롭 우선순위 조정**, 일시중지/재개/폐기, 작업 제출. 게이트웨이 `GET /` 서빙. → 라이브 확인(서버 부팅·제출·분류·우선순위·폐기).
- [x] 테스트 6종(`tests/test_robustness.py`) + 대시보드 라우트 테스트. 전체 **pytest 32종 통과**.
- [x] **#30719 안전망**(기획 1): 라이프사이클 명령 페이로드 필터(큐 진입 차단), 재시작 폭주 가드(윈도우 내 임계 초과 시 자동 시작/재개 halt). → `safety.py`, `queue_manager`(submit 차단/halt), gateway `/safety`·`/safety/reset`, CLI `queue safety`. 테스트 16종(`tests/test_safety.py`)+게이트웨이 3종.
- [x] **Cron Intercept**(기획 5): Hermes `jobs.json` 정의를 읽어 만료 작업을 즉시 실행 대신 큐 `Pending` 편입. 의존성 없는 5필드 cron 매처(범위/스텝/목록/요일 OR), 분 단위 중복 방지, disabled/paused 제외, 라이프사이클 작업은 안전망이 차단. → `cron_intercept.py`, Scheduler 통합, CLI `queue cron-tick`. 테스트 10종(`tests/test_cron.py`).
- [x] **Classifier LLM 폴백**(QA-2.3): 휴리스틱이 모호할 때만 LLM 1회 질의(주입식, 기본 OFF `ALPHRED_LLM_CLASSIFY`). `classifier.parse_classification`, `make_hermes_classifier`, `classify_only` 통합. 테스트 8종.
- [x] **멀티모달 라우팅**(기획 3.2): 게이트웨이가 OpenAI 멀티모달 파트(text/image_url/input_audio)에서 텍스트 추출→분류, 음성 전용은 Light 라우팅. 실제 STT/TTS/비전은 Hermes 프록시. 테스트 3종.
- [x] **MCP/서브서비스 enablement**(기획 4): `X-Alphred-Source` 헤더로 하위 서비스 트리거를 큐 일급 작업으로 태깅. MCP 등록 자체는 Hermes `config.yaml`(passthrough `alphred mcp …`). 테스트 1종.
- [ ] 업데이트 데몬(기획 1 나머지) — Hermes update 파이프라인은 passthrough 로 동작, 데몬화는 후속.

> **전체 테스트 73종 통과.** 기획서 핵심 가치(우선순위 큐·선점·다중 프론트엔드·운영안정성·시각관리·주기작업·외부연동·멀티모달)가 모두 동작.

---

## 7. 리스크 & 대응
| 리스크 | 영향 | 대응 |
|---|---|---|
| **선점 재개 정합성** — `/v1/runs`는 재개용 response_id를 저장하지 않음(실측) | Heavy 작업 컨텍스트 손실 | **Alphred가 conversation_history SSOT 보유**, 턴 단위 선점, Phase 0 PoC로 A/B 재개 경로 실증 |
| **이슈 #30719 재시작 루프** | 시스템 무한 재시작 | 에이전트의 lifecycle 명령 예약 차단(페이로드 필터), SIGTERM 폭주 감지 시 KeepAlive 차단 |
| Hermes 업스트림 업데이트로 API 변동 | 프록시 깨짐 | 코어 무수정 + `/v1/capabilities` 기반 버전 가드, 통합 테스트 |
| `/v1/runs` graceful stop 미보장 | 작업 중간 손상 | stop 후 상태 검증, 비정상 시 Discarded+재큐 |
| 동시성(여러 Light 동시 유입, 슬롯 경합) | 데드락/우선순위 역전 | 초기 단일 슬롯, 전이 트랜잭션 직렬화, 추후 슬롯 N 확장 |
| Windows 환경(파일락/시그널 차이) | cron/락 동작 차이 | 실물 확인됨(msvcrt 폴백 존재), Windows에서 테스트 우선 |

---

## 8. 확정된 결정사항 (2026-06-20)
1. **스택**: Python 3.11 + FastAPI + SQLite. ✅
2. **통합 깊이**: **외부 프록시 우선** — Hermes 코어 무수정, cron만 얇게 인터셉트. ✅
3. **MVP 범위**: **큐 먼저(Phase 1) → 선점 분리(Phase 2).** ✅
4. **다음 단계**: **Phase 0 PoC 먼저** — `/v1/runs → stop → /v1/responses` 재개를 스크립트로 검증. ✅
5. **배포 타깃**: **처음부터 클라우드/다중 디바이스 가정.** ✅ → 설계 함의:
   - 인증을 1급 관심사로(요청별 API 키/세션 키, 다중 클라이언트 격리).
   - 네트워크 노출 전제: TLS, 레이트리밋, CORS, 원격 디바이스(안드로이드/ESP32) 등록·인증.
   - 영속화 추상화 계층을 두어 SQLite → Postgres 이관 용이하게(초기엔 SQLite 단일 노드로 시작하되 인터페이스 분리).
   - 단, **단일 슬롯 스케줄러로 시작**(LLM/샌드박스 점유 단순화) 후 다중 슬롯·다중 세션 확장.

---

## 9. QA 테스트 리스트 (인수 기준)

> 표기: **[U]** = 사용자가 제시한 항목, **[+]** = 보강 추가 항목. 각 항목은 통과/실패가 명확히 판정되는 인수 기준이다.
> Phase 매핑은 해당 기능이 구현되는 단계.

### QA-1. Hermes 기능 100% 활용 (래퍼/패스스루 정합성)  — Phase 0~1
| # | 시나리오 | 합격 기준 | Phase |
|---|---|---|---|
| 1.1 **[U]** | `alphred`로 호출 시 Hermes가 그대로 호출되는가 | `alphred chat "hi"` 결과 == `hermes chat "hi"` 결과(동일 동작/출력 형식) | P1 |
| 1.2 **[U]** | Hermes 미설정 상태에서 `alphred` 실행 시 설정을 불러/생성하는가 | 설정 없는 깨끗한 환경에서 `alphred` 최초 실행 → `get_hermes_home()` 규칙대로 config.yaml 자동 발견·생성, 정상 기동 | P1 |
| 1.3 **[U]** | 모든 명령어·인자·옵션이 Hermes와 1:1 매칭되는가 | `hermes`의 전체 서브커맨드 목록 == `alphred`에서 호출 가능(동적 위임). 무작위 샘플 명령의 `--help`/인자/종료코드 일치 | P1 |
| 1.4 **[+]** | 종료코드·stdout/stderr·스트리밍이 손실 없이 패스스루되는가 | 파이프(`alphred ... \| grep`), 비-0 종료코드, 색상/SSE 출력이 hermes 직접 호출과 동일 | P1 |
| 1.5 **[+]** | `HERMES_HOME` 오버라이드·다중 프로파일을 존중하는가 | `HERMES_HOME` 지정 시 그 경로 사용, Alphred가 별도 home을 만들지 않음 | P1 |
| 1.6 **[+]** | `alphred update` 시 Hermes 업데이트 파이프라인이 그대로 동작하는가 | 스냅샷 백업→pull→구문검증→롤백 체인 정상, 단 #30719 안전망 적용 | P4 |
| 1.7 **[+]** | 신규 Hermes 버전에서 추가된 서브커맨드가 자동 노출되는가 | 하드코딩 목록이 아니라 동적 위임이므로 신규 명령도 즉시 `alphred`로 사용 가능 | P1 |

### QA-2. 작업 분류 (Light / Heavy)  — Phase 2
| # | 시나리오 | 합격 기준 |
|---|---|---|
| 2.1 **[U]** | Heavy/Light를 구분하는가 | 대표 입력셋(즉답 질의 vs 대규모 분석/리팩토링/크롤링)에 대해 분류 정확도 측정, 라벨드 셋 기준 ≥ 목표치 |
| 2.2 **[+]** | 명시 우선순위 오버라이드를 존중하는가 | `X-Alphred-Priority`(또는 CLI 플래그) 지정 시 분류기 무시하고 그 값 사용 |
| 2.3 **[+]** | 모호한 입력에 LLM 폴백이 작동하는가 | 휴리스틱 미결 입력 → LLM 1회 질의로 `{kind, priority, 사유}` 산출, 결과가 작업 레코드에 기록 |
| 2.4 **[+]** | 분류 결과가 감사 가능한가 | 각 작업에 분류 근거/점수가 저장되어 사후 튜닝 가능 |

### QA-3. 큐 등록·조회·우선순위 변경  — Phase 1
| # | 시나리오 | 합격 기준 |
|---|---|---|
| 3.1 **[U]** | Heavy 작업 여러 개를 큐에 등록하는가 | N개 Heavy 등록 → 전부 `Pending`으로 적재, 각자 고유 UUID 부여 |
| 3.2 **[U]** | 큐 내용을 요청 시 표시하는가 | `alphred queue list`/대시보드/`QUEUE.MD`에서 작업·우선순위·상태가 일관되게 조회 |
| 3.3 **[U]** | 우선순위 변경을 반영하는가 | 우선순위 변경(드래그&드롭/CLI) → 큐 정렬 즉시 갱신, QUEUE.MD/DB 동기화 |
| 3.4 **[+]** | DB ↔ QUEUE.MD 정합성 | DB(SSOT) 변경이 QUEUE.MD에 항상 반영, 수동 편집 충돌 시 정책대로 처리 |
| 3.5 **[+]** | 우선순위에 따른 실행 순서 | 슬롯 가용 시 항상 최고 priority Pending이 먼저 In-Progress로 전환 |
| 3.6 **[+]** | 게이트웨이 재시작 후 큐 영속성 | 재시작해도 Pending/Paused 작업과 순서가 보존(SQLite 영속) |
| 3.7 **[+]** | 작업 취소/폐기 | 사용자 강제 취소 → `Discarded`, 리소스 회수, 최종 상태 불변 |

### QA-4. 선점형 스케줄링 (핵심 차별점)  — Phase 2
| # | 시나리오 | 합격 기준 |
|---|---|---|
| 4.1 **[U]** | Heavy 중 Light 요청 시 일시정지→Light 수행→Heavy 재개 | §5 시나리오 그대로 재현: Heavy `In-Progress`→`Paused`, Light `Completed`, Heavy `In-Progress` 재개 |
| 4.2 **[+]** | **재개 후 컨텍스트 무손실** | 재개된 Heavy가 중단 지점의 컨텍스트(이전 툴 결과 포함)를 유지(Alphred conversation_history SSOT 검증). PoC T4-A/T4-B 중 채택 경로로 확인 |
| 4.3 **[+]** | 다중/연쇄 선점 | Light가 연달아 여러 번 들어와도 Heavy가 매번 안전 일시정지/재개, 우선순위 역전 없음 |
| 4.4 **[+]** | 동급/저순위 유입은 선점하지 않음 | 신규 priority ≤ 현재면 선점 없이 큐 대기 |
| 4.5 **[+]** | 툴 호출 중 선점의 안전성 | 진행 중 턴/도구 호출이 graceful stop으로 손상 없이 중단, 부분상태 정책대로 보존/폐기 |
| 4.6 **[+]** | 재개 실패 시 폴백 | 재개 실패(컨텍스트 손상 등) 시 안전 재시작 또는 명시적 실패 보고, 무한 루프 없음 |
| 4.7 **[+]** | 선점 지연(latency) | Light 요청~응답 시작까지의 선점 지연이 목표치 이내 |

### QA-5. 다중 프론트엔드 / 멀티모달  — Phase 3
| # | 시나리오 | 합격 기준 |
|---|---|---|
| 5.1 **[+]** | OpenAI 규격 호환 | 표준 OpenAI 클라이언트가 Base URL만 바꿔 `/v1/chat/completions`·`/v1/responses`·`/v1/runs` 정상 사용 |
| 5.2 **[+]** | 진행률 스트리밍 | `hermes.tool.progress`/SSE가 프론트로 전달되어 "웹 검색 중…" 등 인디케이터 렌더 가능 |
| 5.3 **[+]** | 비동기 폴링 | `GET /v1/runs/{id}`로 스트리밍 없이 상태·usage 조회 |
| 5.4 **[+]** | 음성 STT→TTS 왕복 | 오디오 업로드→전사→큐 처리→TTS 응답 페이로드 정상(기획 3.2) |
| 5.5 **[+]** | 이미지 분석 | `image_url`(base64) 페이로드 → `vision_analyze` 결과 반환 |
| 5.6 **[+]** | 다중 디바이스 동시성 | 안드로이드+ESP32+웹 동시 접속 시 세션 격리, 충돌 없음 |

### QA-6. 주기 스케줄링 / 오케스트레이션  — Phase 4
| # | 시나리오 | 합격 기준 |
|---|---|---|
| 6.1 **[+]** | cron 인터셉트 | 만료된 주기 작업이 즉시 강제 실행이 아니라 큐 `Pending`으로 편입(우선순위 부여) |
| 6.2 **[+]** | 격리 세션 실행 | 주기 작업이 사용자 대화 히스토리에 영향 없이 격리 세션에서 실행 |
| 6.3 **[+]** | 재귀 스케줄 폭발 방지 | 격리 세션 내 cronjob 생성 도구 비활성, 무한 재귀 차단(기획 5.2) |
| 6.4 **[+]** | 결과 전달처 발송 | 완료 결과가 텔레그램/디스코드/이메일 등 설정 전달처로 발송, 상태 `Completed` |
| 6.5 **[+]** | MCP 서브서비스 | stdio/HTTP MCP 서버 자동 발견(list_tools), `tools.include/exclude` 방화벽 적용 |
| 6.6 **[+]** | execute_code 제한 | 300초 타임아웃·50KB stdout 제한 준수, 중간 데이터 폐기·요약본만 반환 |

### QA-7. 안정성 / 안전망 / 동시성  — Phase 1~4
| # | 시나리오 | 합격 기준 |
|---|---|---|
| 7.1 **[+]** | **#30719 재시작 루프 방지** | 에이전트가 gateway restart/systemctl 등 lifecycle 명령 예약 시 페이로드 필터로 차단 |
| 7.2 **[+]** | SIGTERM 폭주 안전망 | 60초 내 비정상 SIGTERM 3회 감지 시 auto-resume 비활성화 |
| 7.3 **[+]** | 타임아웃→폐기 | `terminal.timeout` 초과 작업이 `Discarded`로 정리, 리소스 회수 |
| 7.4 **[+]** | 상태머신 불변식 | 허용된 전이만 가능(예: `Completed→In-Progress` 거부), 모든 전이 audit 로그 기록 |
| 7.5 **[+]** | 동시 Light 다발 유입 | 여러 Light 동시 도착 시 데드락/이중 실행 없이 직렬화 처리 |
| 7.6 **[+]** | DB 트랜잭션 무결성 | 상태 전이가 원자적, 크래시 중 부분 전이로 인한 손상 없음(WAL) |
| 7.7 **[+]** | 크래시 복구 | 게이트웨이 비정상 종료 후 재기동 시 In-Progress였던 작업을 정책대로 재개/재큐 |
| 7.8 **[+]** | 인증/격리(클라우드) | 잘못된/누락 API 키 거부, 클라이언트 간 작업·메모리 격리, 레이트리밋 동작 |
| 7.9 **[+]** | Windows 환경 | 파일락(msvcrt)·시그널 차이에도 cron/큐 정상(우선 테스트 플랫폼) |

### 회귀(Regression) 기준
- 위 모든 항목을 **자동화 테스트 스위트**로 구성(가능한 것은 통합 테스트, 선점/재개는 시나리오 테스트).
- Hermes 업스트림 업데이트 후 QA-1 전체 + QA-5.1 재실행을 게이트로 삼는다.
```

---

## 10. Alphred 브랜딩 & 정체성 통합 (UI/Identity Re-skin)  ✅ **구현 완료 (2026-06-21, 옵션 A)**

> 목표: Hermes에 내장된 Alphred를 **시각(메인화면 로고·배너·문구)과 자기인식(system prompt 정체성)** 양면에서 "Alphred"로 보이고 행동하게 한다. **Hermes 코어 무수정** 원칙과 **업데이트 내성**(업데이트마다 재작업 불필요)을 유지한다.
>
> **구현 결과:** 옵션 A 채택. `alphred/branding/`(skin YAML+SOUL.md+원본 ascii+빌더), `alphred/brand.py`(설치기), `alphred brand apply|status|revert` CLI, `tests/test_brand.py`(11종). 실제 적용·검증 완료 — Hermes 자체 skin engine이 `alphred` 스킨 로드 확인(agent_name=Alphred, 로고/hero/welcome/응답라벨 교체), `display.skin=default→alphred`, `SOUL.md`=Alphred 정체성. 전체 pytest 85종 통과. 잔여 갭(패널 버전 라벨 등 §10.3)은 옵션 A대로 수용.

### 10.1 조사 결과 — Hermes의 확장점 (소스 실측, 2026-06-21)

핵심 결론: **둘 다 코어를 건드리지 않고 HERMES_HOME(`C:\Users\alpha\AppData\Local\hermes\`)의 사용자 파일만으로 교체 가능**하다. Hermes에 이미 데이터 기반 브랜딩 시스템이 내장돼 있다.

**(1) 메인화면 = "skin engine"** (`hermes_cli/skin_engine.py`)
- 데이터 기반 스킨 시스템. `~/.hermes/skins/<name>.yaml` 에 YAML을 떨구면 **코드 수정 없이** 새 스킨이 추가된다(사용자 스킨이 내장 스킨보다 우선).
- 이미 `ares/poseidon/sisyphus/charizard` 등 **에이전트명·로고·배너까지 통째로 바꾼 대체 스킨 선례**가 존재 → Alphred도 동일 방식으로 정당.
- 활성화: `config.yaml`의 `display.skin: <name>` (현재 `default`) 또는 런타임 `/skin <name>`.
- 스킨이 덮는 항목(우리 요구와 직접 매핑):
  - `banner_logo` → 상단 대형 ASCII 로고(`HERMES_AGENT_LOGO` 대체). ← `Alphred_Banner.txt`
  - `banner_hero` → 패널 좌측 ASCII 아트(`HERMES_CADUCEUS` 대체). ← `Alphred_Logo.txt`
  - `branding.agent_name` / `welcome` / `goodbye` / `response_label` / `prompt_symbol` / `help_header` → "Welcome to Hermes Agent!", 응답 박스 라벨 ` ⚕ Hermes ` 등 화면 문구 전부.
  - `colors.*` → 배너/상태바/응답박스 색상 테마.
- 렌더링 확인: `banner.py:768`(로고), `:553`(hero), `cli.py:10572`(welcome)가 모두 활성 스킨에서 값을 읽음.

**(2) 자기인식 = system prompt 정체성 슬롯** (`agent/system_prompt.py`, `agent/prompt_builder.py`)
- 시스템 프롬프트 "stable" 티어의 **1번 슬롯이 정체성**이며, 우선순위가 명확하다(`system_prompt.py:88-100`):
  1. `~/.hermes/SOUL.md` 에 내용이 있으면 **그것이 정체성으로 사용됨**(하드코딩 대체).
  2. 없으면 `DEFAULT_AGENT_IDENTITY`(= `"You are Hermes Agent, ... created by Nous Research..."`, `prompt_builder.py:122`) 폴백.
- 즉 **`SOUL.md`에 "너는 Alphred다"를 쓰면 코어 수정 없이 자기인식이 바뀐다.** `SOUL.md`는 매 메시지마다 새로 읽혀 재시작도 불필요(`prompt_builder.py:1401`).
- 현재 상태: `~/.hermes/SOUL.md`는 **존재하지만 플레이스홀더 주석만** 있음(실질 정체성 미정의) → 우리가 채우면 됨.

### 10.2 적용 방안 (권장) — "Alphred가 브랜딩을 소유"

브랜딩 자산을 **Alphred 저장소가 소유**하고, HERMES_HOME에 **설치(install)** 하는 구조. 자산이 Hermes 저장소 밖(사용자 홈)에 살기 때문에 **Hermes 업데이트가 덮어쓰지 않는다** = 업데이트 내성 확보. 동시에 저장소에 원본이 있어 재현·버전관리 가능.

```
alphred/branding/
  alphred.skin.yaml   # skin engine 스펙 (logo/hero/branding/colors)
  SOUL.md             # Alphred 정체성(자기인식) 텍스트
  assets/             # 원본 ascii art (Alphred_Banner.txt, Alphred_Logo.txt)
```

설치 경로(자동):
- `alphred.skin.yaml` → `~/.hermes/skins/alphred.yaml`
- `SOUL.md`           → `~/.hermes/SOUL.md` (기존 플레이스홀더만 있을 때만 덮어씀; 사용자 커스텀 보존)
- `config.yaml`의 `display.skin: default` → `alphred` 로 1줄 변경(기존 값 백업).

신규 CLI: **`alphred brand`** (intercept 서브커맨드에 추가)
- `alphred brand apply` — 위 3개 설치 + 스킨 활성화(idempotent; 업데이트 후 재실행으로 재-적용).
- `alphred brand status` — 현재 skin / SOUL.md / config 상태 점검.
- `alphred brand revert` — `display.skin`을 백업값으로 되돌림(자산은 남김).

ASCII 변환 메모: `*.txt` 원본을 skin YAML의 `banner_logo`/`banner_hero` 형식(Rich 마크업, 줄별 `[color]...[/]`)으로 변환해 넣는다. 색상은 Alphred 테마 1벌을 `colors.*`에 정의. (최종 팔레트는 §10.7 참조.)

### 10.3 잔여 갭 (스킨/SOUL로 못 덮는 하드코딩) 과 처리 방침

| 위치 | 내용 | 스킨화? | 방침(권장) |
|---|---|---|---|
| `banner.py:425` `format_banner_version_label()` | 패널 **제목** `Hermes Agent v0.16.0 (날짜)` | ❌ 하드코딩 | **수용**(잔여 1줄). 완전 제거 원하면 §10.5 옵션 B. |
| `banner.py:564` | `· Nous Research` (모델 줄) | ❌ | 수용(저강도). |
| `banner.py` 업데이트 경고 | `⚠ … run hermes update` | ❌ | 수용(기능적 안내). |
| `cli.py` 팁 라인 | `✦ Tip: hermes chat …` | ❌(별도 팁목록) | 수용. |
| `prompt_builder.py:132` `HERMES_AGENT_HELP_GUIDANCE` | 정체성 뒤에 항상 붙는 "You run on Hermes Agent (by Nous Research)…" | ❌ 항상 주입 | **수용 권장** — 사실(=Alphred는 Hermes 위에서 구동)이며 self-help 문서 링크 기능. SOUL.md에서 "표면 정체성은 Alphred"로 규정하면 사용자 대면 호칭은 Alphred로 유지됨. |

핵심: **사용자가 실제 보는 표면(상단 로고/배너/welcome/응답라벨/프롬프트기호)과 에이전트의 자기소개는 100% Alphred로 전환**된다. 남는 건 패널 *제목 줄의 버전 라벨* 등 비핵심 메타 1~2줄뿐.

### 10.4 SOUL.md(정체성) 설계 초안

```
너는 "Alphred" 다 — 사용자의 개인 AI 에이전트.
- 자기소개·서명·호칭은 항상 "Alphred". 스스로를 "Hermes"라 부르지 않는다.
- 누가 물으면: "저는 Alphred입니다." (구동 기반을 묻는 기술적 질문에 한해 Hermes 위에서 동작한다고 사실대로 답해도 됨)
- 톤/원칙: (사용자 취향 반영 — 직설적·간결·한국어 우선 등)
```
> SOUL.md는 **정체성+페르소나** 슬롯이므로 호칭 규정과 말투를 함께 정의. (말투는 추후 사용자가 조정)

### 10.5 구현 옵션 비교

- **옵션 A (권장): 스킨 + SOUL.md + config 1줄** — 코어 무수정, 업데이트 내성 ◎, 잔여 갭(버전 라벨) 수용. 우리 원칙에 정확히 부합.
- **옵션 B: 옵션 A + 코어 몽키패치** — `format_banner_version_label()` 등 하드코딩까지 런타임 패치(부트스트랩 시 주입). 100% 제거 가능하나 **업데이트마다 깨질 수 있고** "코어 무수정" 원칙과 충돌 → 사용자가 "버전 라벨까지 반드시" 요구할 때만.
- **옵션 C: PR 업스트림** — `agent_name`을 패널 제목에도 쓰도록 Hermes에 기여. 근본적이나 외부 일정 의존 → 후순위.

### 10.6 작업 항목 (Phase B1: 브랜딩)
1. `Alphred_Banner.txt`/`Alphred_Logo.txt` → Rich 마크업 변환 + Alphred 컬러 테마 정의 → `alphred/branding/alphred.skin.yaml`.
2. `alphred/branding/SOUL.md` 작성(§10.4).
3. `alphred brand apply|status|revert` CLI 구현(설치/백업/idempotent).
4. QA-8 추가:
   - **QA-8.1** `brand apply` 후 `~/.hermes/skins/alphred.yaml` 생성 & `display.skin=alphred`.
   - **QA-8.2** `hermes`(또는 `alphred chat`) 기동 시 상단 로고/배너/welcome/응답라벨이 Alphred로 표시(수동 1회 + 스냅샷).
   - **QA-8.3** `SOUL.md` 적용 후 "너 누구야?" → "Alphred" 자기소개(라이브, 쿼터 1회).
   - **QA-8.4** 멱등성: `brand apply` 2회 실행해도 config 중복/오염 없음.
   - **QA-8.5** 사용자 커스텀 SOUL.md가 이미 있으면 덮어쓰지 않음(보존).
   - **QA-8.6** `brand revert` 후 `display.skin` 백업값 복원.

### 10.7 As-Built (실제 구현·확정값, 2026-06-21)

**파일 구조(확정):**
```
alphred/branding/
  build_skin.py        # 빌더: assets/*.txt → alphred.skin.yaml (Rich 마크업+그라데이션+dedent)
  alphred.skin.yaml    # 생성물(스킨). 직접 수정 금지 — 원본 바꾸고 빌더 재실행
  SOUL.md              # Alphred 정체성(자기인식)
  assets/Alphred_Banner.txt  # 상단 대형 로고 원본 → banner_logo
  assets/Alphred_Logo.txt    # 패널 좌측 초상화 원본 → banner_hero
alphred/brand.py       # 설치기(apply/status/revert), config.yaml 라인편집+백업
```

**색상 팔레트 "Alph-RED"(확정):** 메인 레드 **`#E63946`**(코랄빛이 도는 눈 편한 톤). 그라데이션(밝음→어두움) 8단계: `#FF9F45`(앰버) → `#FF7A3D`(주황) → `#FB5A3C` → `#F03E41` → **`#E63946`** → `#D32F3C`(크림슨) → `#B22232`(딥레드) → `#8E1B28`(마룬). 아트 각 줄에 세로 위치 비율로 색을 매핑(상단 앰버 → 하단 마룬). 스킨 `colors.*`: border `#B22232`, title `#FF7A3D`, accent `#E63946`, text `#FFE2D6`, status_bar_bg `#1F0D0D` 등.

**로고 좌측 여백(dedent):** 빌더가 비어있지 않은 아트 줄들의 **공통 좌측 공백(예: 초상화 23칸)을 계산해 일괄 제거** → 아트가 왼쪽에 밀착. 내부 실루엣은 보존.

**설치 동작(확정):** 스킨은 매번 덮어씀(재현). SOUL.md는 **플레이스홀더/부재일 때만** 설치(사용자 커스텀 보존; `--force-soul` 로 강제). config는 `display.skin` 라인만 교체하고 최초값을 `~/.hermes/alphred/brand_backup.json` 에 백업. 전부 멱등.

**검증(완료):** Hermes **자체** `skin_engine.load_skin('alphred')` 로 로드 확인(agent_name=Alphred, accent=#E63946, 로고/hero 존재, dedent 후 공통여백 0). `display.skin=alphred`, SOUL=Alphred 정체성. 단위테스트 `tests/test_brand.py` 11종 + 전체 **pytest 85종 통과**.

**잔여(미적용·후속):** §10.5 옵션 B(패널 버전 라벨 등 하드코딩 몽키패치)는 보류. 그라데이션 방향/톤 미세조정은 원본 교체 또는 `build_skin.py` 의 `GRADIENT` 수정 후 `python -m alphred.branding.build_skin && alphred brand apply` 로 반영.

---

## 11. 큐가 결합된 Alphred TUI (Queue-aware TUI)  ✅ **B2-1 구현 완료 (2026-06-21)**

> 목표: `alphred` 한 명령으로 **외형·조작은 순정 Hermes TUI 100%** 이면서 **Light/Heavy 큐·선점이 결합된** 대화 화면을 띄운다. 새 TUI를 만들지 않고 Hermes 네이티브 TUI를 그대로 쓴다.

### 11.0 절대 원칙 — "단일 Alphred 코어"

모든 진입점(ESP32·웹·API·**TUI**)의 큐 작업은 **단 하나의 Alphred 서버(`:8643`)** 를 거친다. TUI는 입력 수단만 다른 동일 코어의 **클라이언트**일 뿐이다. → ESP32에서 넣은 작업을 TUI에서 수정하고, 웹에서 추가한 큐가 TUI 조회에 보이는 불변식이 유지된다. **금지: TUI 쪽에 별도 스케줄러/DB를 두어 코어를 둘로 가르는 것.**

### 11.1 왜 TUI만 구조가 다른가 — "끼어드는 위치"

도구/MCP/스킬 사용 능력은 API 경로·TUI 모두 동일(둘 다 풀 Hermes 런타임). 차이는 **Alphred가 에이전트의 앞에 끼느냐 안에 끼느냐**뿐이다.

| 경로 | Alphred 위치 | 라우팅 방식 |
|---|---|---|
| API (web/ESP32) | 에이전트 **앞** (네트워크 프록시 지점 존재) | `:8643`이 요청 전에 classify → forward/queue |
| **TUI** | 에이전트 **안** (키보드→에이전트 직행, 앞에 낄 자리 없음) | **훅**으로 내부에서 `:8643`에 위임 |

→ TUI에선 Hermes 확장점(**훅**)으로 라우팅을 `:8643`에 위임하면, API와 **동일한 classifier 하나**로 판정이 통일된다(= 결정적 라우팅, 단일 엔진).

### 11.2 채택 아키텍처

```
  alphred  (한 명령)
     ├─ (없으면) alphred serve 데몬 자동 기동
     │     └─ :8643 Alphred 게이트웨이 + 스케줄러  (+ 자동 기동 :8642 Hermes API, ALPHRED_HOOK_DISABLE=1)
     └─ 네이티브 Hermes TUI 실행 (Alphred 스킨/SOUL 적용)
           │   매 턴:  pre_llm_call 훅 ──HTTP──► :8643 /route   (결정적 1차 라우터)
           │            post_llm_call 훅 ──────► :8643 /light/end
           └─ (보조) Alphred MCP 브리지 ──────► :8643 /queue, /queue/ask  (명시적 큐 관리)
```

- **1차(결정적): `pre_llm_call` 훅 → `/route`.** Heavy면 훅이 **직접 큐에 등록**(모델 행동과 무관, 결정성 보장)하고 "큐에 넣었음" 컨텍스트를 주입. Light면 진행 중 Heavy를 선점(pause)하고 큐 스냅샷을 주입.
- **보조: Alphred MCP 브리지.** "내 큐 취소/우선순위 변경" 같은 **명시적 변경**을 대화로 처리(에이전트가 도구 호출 → `:8643`). status는 훅 주입으로 무료 제공되므로 MCP는 변경 위주.

### 11.3 컴포넌트 상세 (개발 직전)

**C1. 라우팅 훅 핸들러 — `alphred hook-route` (신규 CLI, stdio)**
- Hermes가 `pre_llm_call` 마다 subprocess 로 실행, **stdin 으로 JSON** 수신: `{"user_message": str, "session_id"?: str, ...}`, **stdout 으로 JSON** 반환: `{"context": str}` 또는 `{}`.
- 로직(의사코드):
  ```
  payload = json.load(stdin)
  if os.environ.get("ALPHRED_HOOK_DISABLE"):   # 재진입 차단(11.5 참조)
      print("{}"); return 0
  r = POST :8643/route {prompt: payload["user_message"], source: "tui",
                        session_id: payload.get("session_id")}
  print(json.dumps({"context": r["context"]}) if r.get("context") else "{}")
  return 0   # 훅 오류는 절대 턴을 깨지 않음 → 예외 시 "{}" 출력 후 0
  ```
- **의존성 0**(stdlib + 기존 httpx). `:8643` 미응답이면 조용히 `{}`(채팅은 정상, 큐만 비활성 — graceful degrade).

**C2. 게이트웨이 신규 엔드포인트 (`:8643`, 단일 코어)**
- `POST /route` — TUI 라우팅의 단일 결정 지점:
  - classify(동일 classifier + 모호 시 LLM 폴백) →
  - **Heavy**: `mgr.submit(prompt, source="tui", ...)` 후
    `{"kind":"heavy","id":<id>,"context":"[Alphred] 이 요청은 백그라운드 작업으로 큐에 등록됨(id=<id8>, prio=N). 지금 직접 수행하지 말고 사용자에게 큐 등록 사실만 간단히 알릴 것."}`
  - **Light**: `mgr.light_begin()`(진행 중 Heavy 선점) 후
    `{"kind":"light","context":"[Alphred 큐 현황]\n"+snapshot}` (스냅샷은 짧게/옵션)
- `POST /light/end` → `mgr.light_end()` (post_llm_call 에서 호출, 선점 해제). 큐 미선점 턴엔 no-op(클램프).
- `TaskSource.TUI` 값 추가.

**C3. 선점 패리티 훅 — `alphred hook-light-end`**
- `post_llm_call`(턴 종료 후, 반환값 무시) → `POST :8643/light/end`. 이로써 TUI 빠른 대화도 web/ESP32 Light 와 **동일하게** 백그라운드 Heavy를 pause→resume.

**C4. Alphred MCP 브리지 — `alphred mcp` (신규, 보조/후속)**
- stdio MCP 서버. 노출 도구(최소):
  | 도구 | 백엔드 |
  |---|---|
  | `queue_status()` | `GET :8643/queue` |
  | `queue_ask(request)` | `POST :8643/queue/ask` (자연어 우선순위/취소/일시정지) |
  | `task_result(id)` | `GET :8643/v1/runs/{id}` |
- **상태 없는 브리지**(자체 스케줄러/DB 금지 — 11.0). `config.yaml` `mcp_servers.alphred` 로 등록.
- 의존성 결정(이 단계에서만): **권장 = 공식 `mcp` 파이썬 SDK를 옵션 extra `alphred[tui]` 로** (외부 프로토콜이라 정확성·호환성 우선, 기본 설치는 경량 유지). 폴백 = 최소 stdio JSON-RPC 자체 구현(의존성 0). **B2-1(훅) 단계는 MCP 불필요 → 결정은 B2-2 로 지연.**

**C5. SOUL.md 운영 정책 추가(기존 정체성 블록에 덧붙임)**
- 빠른 질문/조회/번역/대화는 **직접 답하라.**
- 시간이 오래 걸리는 작업은 **이미 큐에 등록되었으니**(훅이 처리) 다시 수행하지 말고 등록 사실(id)만 알려라.
- 큐 변경(취소/우선순위)은 `queue_ask` 도구를 써라. 현재 큐 현황은 주입된 컨텍스트를 참고하라.

**C6. 설치 통합 — `brand.apply` / `alphred setup` 확장 (코어 무수정)**
- `config.yaml` 을 **라인 안전 편집**(display.skin 과 동일 방식, `brand_backup.json` 백업)으로:
  - `hooks.pre_llm_call` 에 `{command: "<sys.executable> -m alphred.cli hook-route", timeout: 10}` 추가(중복 방지).
  - `hooks.post_llm_call` 에 `hook-light-end` 추가.
  - (B2-2) `mcp_servers.alphred` 추가.
- **consent 처리**: 셸 훅 최초 사용 시 TTY 동의가 필요 → setup 이 우리 (event,command) 쌍을 `~/.hermes/shell-hooks-allowlist.json` 에 **선등록**(전체 auto-accept 는 보안상 지양).
- `brand revert` 가 위 hooks/mcp 항목도 함께 제거.

**C7. `alphred`(무인자) = 데몬 보장 + TUI**
- 실행 시 `:8643` 미가동이면 `alphred serve` 를 백그라운드 자동 기동(이미 구현된 serve→Hermes 자동기동 패턴 재사용) 후 네이티브 TUI(`hermes`)로 위임. `--no-daemon` 으로 비활성.

### 11.4 요청 처리 흐름

- **TUI Heavy**: 입력 → `pre_llm_call`→`/route`(Heavy) → 훅이 큐 등록 + "큐 등록됨" 주입 → 에이전트가 사용자에게 등록 사실 통지(수행 안 함) → 스케줄러가 백그라운드 실행 → 결과는 `queue_status`/알림으로 회수.
- **TUI Light**: 입력 → `/route`(Light) → 진행 중 Heavy pause → 에이전트가 즉답 → `post_llm_call`→`/light/end` → Heavy resume.
- **교차 디바이스**: 모든 등록/상태가 `:8643` 한 DB → ESP32·웹·TUI 어디서나 동일 큐.

### 11.5 재진입(루프) 차단 — **필수**

셸 훅은 Gateway 컨텍스트에서도 발화하므로, 스케줄러가 `:8642` 로 돌리는 **백그라운드 Heavy 실행이 다시 `/route` 를 호출해 무한 재등록**될 수 있다. 차단:
- `alphred serve` 가 `:8642` Hermes API 를 **자동 기동할 때 `ALPHRED_HOOK_DISABLE=1` 주입**(우리 코드 `_spawn_hermes_gateway`). 훅 핸들러는 이 env 가 있으면 즉시 `{}` 반환.
- TUI 의 Hermes(=`alphred` 가 직접 띄움)에는 이 env 가 없으므로 훅 정상 동작.
- **주의/제약**: 사용자가 `alphred serve --no-auto-hermes` 로 직접 `hermes gateway run` 을 띄우는 경우, 그 프로세스에 `ALPHRED_HOOK_DISABLE=1` 을 직접 export 해야 한다(문서화). (대안: 스케줄러가 보내는 run 에 센티넬을 실어 훅이 식별 — env 가드보다 복잡, 보류.)

### 11.6 정합성 (API ↔ TUI)

| 항목 | API(web/ESP32) | TUI | 동일? |
|---|---|---|---|
| 큐/SSOT/스케줄러 | `:8643` | `:8643`(훅·MCP 경유) | ✅ |
| Light/Heavy 판정 | `:8643` classifier | `:8643` classifier(`/route`) | ✅ |
| Heavy 선점 | 스케줄러 | 스케줄러 | ✅ |
| Light→진행중 Heavy 선점 | light_scope | `/light/begin·end` 훅 | ✅ |
| 도구/MCP/스킬 | 풀 Hermes | 풀 Hermes | ✅ |
| Heavy 결과 전달 | 비동기(폴링/응답) | 비동기(`queue_status`/알림) | ✅(둘 다 비동기) |
| Heavy 실행 시 대화맥락 상속 | 호출자 책임 | §11.7 핸드오프 | △ |

### 11.7 맥락 핸드오프 (우려점 #3 대응)

TUI에서 큐로 넘긴 Heavy 는 스케줄러가 `:8642` 의 **새 run** 으로 실행 → TUI 세션 대화를 자동 상속하지 않는다. 처리:
- `/route` 가 `session_id`(훅 payload) 를 함께 받아 `mgr.submit(..., session_key=session_id)` 로 보관.
- 스케줄러 실행 시 가능한 경로(우선순위): ① Hermes run 에 동일 `session_id` 연결(가능 여부 실측 필요) → ② 불가하면 직전 대화 요약/핵심 컨텍스트를 prompt 프리앰블로 주입(Alphred 가 conversation_history SSOT 보유 — 기존 PoC T4-B 재사용).
- **설계 항목으로 명시**: B2 1차 릴리스는 ②(프리앰블)로 충분, ①은 후속 최적화.

### 11.8 우려점 & 완화 (요약)

| # | 우려 | 완화 |
|---|---|---|
| 1 | Heavy 인라인 수행 억제는 주입 지시 기반(소프트) — 등록 자체는 결정적 | SOUL 정책 + 강한 주입문구; (옵션) `pre_tool_call` 로 등록 직후 장시간 툴 차단 |
| 2 | Heavy 결과 비인라인 | `queue_status` 도구 + 완료 알림(후속) |
| 3 | 대화맥락 단절 | §11.7 핸드오프 |
| 4 | 훅 재진입 루프 | §11.5 env 가드 (**필수**) |
| 5 | 훅 subprocess 턴당 지연(~수백 ms) | 핸들러 경량화, 타임아웃 10s |
| 6 | MCP consent 마찰 | setup 이 allowlist 선등록 |
| 7 | LLM 쿼터 이중 소모(TUI+백그라운드) | 기존 제약(라이브 테스트 절약), 선점으로 동시성 완화 |

### 11.9 작업 항목 (Phase B2: Queue-aware TUI)

- **B2-1 (MVP, 의존성 0):** `TaskSource.TUI` / 게이트웨이 `/route`·`/light/end` / `alphred hook-route`·`hook-light-end` CLI / `brand.apply` 의 hooks 등록·revert·allowlist 선등록 / SOUL 정책 추가 / `alphred` 무인자 데몬 보장(C7) / **재진입 env 가드(11.5)**.
- **B2-2 (보조):** `alphred mcp` 브리지 + `mcp_servers.alphred` 등록 (의존성 결정: `mcp` SDK extra 권장).
- **B2-3 (다듬기):** 맥락 핸드오프 ①(세션 연결) / 완료 알림 경로 / (옵션)`pre_tool_call` 하드닝.

### 11.10 QA 인수 기준 (QA-11)

- QA-11.1 TUI에서 "전체 코드베이스 리팩토링" → 즉시 "큐 등록됨(id)" 응답, 채팅 안 막힘, `:8643` 큐에 1건.
- QA-11.2 TUI에서 "안녕" → 즉답, 진행 중 Heavy 가 pause→resume(선점 패리티).
- QA-11.3 웹에서 등록한 작업이 TUI `queue_status`(또는 주입 스냅샷)에 보임 / TUI에서 `queue_ask "X 취소"` → 웹·ESP32에서 폐기 확인(단일 코어).
- QA-11.4 스케줄러 백그라운드 실행이 `/route` 를 재호출하지 않음(재진입 차단, env 가드).
- QA-11.5 `:8643` 다운 시 TUI 채팅은 정상(큐만 비활성, 오류로 턴 안 깨짐).
- QA-11.6 외형이 순정 Hermes TUI 와 동일(스킨/SOUL 반영). Hermes 업데이트 후에도 hooks/skin/SOUL 유지.
- QA-11.7 `brand revert` 후 hooks/mcp 항목 제거되고 순정 Hermes 동작 복귀.

### 11.11 미해결 / 리스크
- `pre_llm_call` 의 정확한 payload 필드(특히 `session_id` 포함 여부)·`post_llm_call` 발화 시점은 구현 착수 시 실측 필요(문서 + 실제 1회 검증).
- 세션 연결 기반 맥락 상속(①) 가능 여부 미확인 → 1차는 프리앰블(②).
- `--no-auto-hermes` 수동 경로의 env 가드 수동 설정(문서화 필요).

### 11.12 As-Built (B2-1 실제 구현·검증, 2026-06-21)

> **실측으로 확정된 훅 wire 프로토콜** (`website/docs/user-guide/features/hooks.md`):
> `pre_llm_call` 은 **턴당 1회** 발화(툴 루프 중간 미발화 → 가드 불필요), stdin =
> `{"hook_event_name","session_id","cwd","extra":{user_message, conversation_history, is_first_turn, model, platform,...}}`,
> stdout = `{"context": str}`(사용자 메시지에 덧붙임) 또는 `{}`. 턴 전체 대체는 불가(주입만).
> `pre_tool_call` 만 `{"action":"block","message"}` 로 차단 가능.
> allowlist(`~/.hermes/shell-hooks-allowlist.json`) 형식 = `{"approvals":[{"event","command"}]}`, command 정확 매칭.

**구현 파일:**
- `alphred/hookcfg.py`(신규) — config.yaml `hooks:` 주석 보존 라인 편집(없으면 블록 추가/있으면 서브키 삽입, 모든 추가 줄에 `# alphred-hook` 태그 → 정확 제거) + allowlist 선등록/제거. command 문자열 = `'"<python>" -m alphred.cli <sub>'`(forward-slash 경로+큰따옴표, YAML 작은따옴표 스칼라로 감쌈).
- `alphred/gateway.py` — `POST /route`(단일 결정 지점: Heavy=결정적 큐등록+컨텍스트, Light=`light_begin`+큐 스냅샷), `POST /light/begin`·`/light/end`. `_spawn_hermes_gateway` 가 자동기동 :8642 에 `ALPHRED_HOOK_DISABLE=1` 주입(재진입 차단).
- `alphred/cli.py` — `hook-route`/`hook-light-end`(stdio 핸들러, 오류 시 무조건 `{}`·exit 0), `_hook_disabled`(env `ALPHRED_HOOK_DISABLE` OR `extra.platform != "cli"`), `_ensure_daemon`(무인자 `alphred` 시 :8643 미가동이면 `alphred serve` 백그라운드 기동; `ALPHRED_NO_DAEMON`/`--no-daemon` 으로 비활성).
- `alphred/brand.py` — `apply` 가 skin+SOUL 에 더해 hooks 등록+allowlist 선등록, `revert` 가 제거, `status` 가 `hooks_installed` 보고.
- `alphred/branding/SOUL.md` — "큐 운영 정책(TUI)" 추가(Light 즉답 / Heavy 는 이미 큐등록됨→id만 통지 / `[Alphred ...]` 주입 컨텍스트 준수).
- `alphred/models.py` `TaskSource.TUI`, `alphred/config.py` `gateway_url`(`ALPHRED_GATEWAY_URL`, 기본 `http://localhost:8643`).

**검증:** 전체 **pytest 110종 통과**(신규 `test_hookcfg.py` 13 + 게이트웨이 `/route`·`/light` 4 + nlq/기존). **YAML 안전성**: 생성된 hooks 를 실제 pyyaml(Hermes venv)로 파싱 — 신규 추가/기존 블록 삽입/제거 라운드트립 모두 파싱 성공, command 가 allowlist 와 정확 일치, 기존 훅 보존 확인. **E2E**: `serve --no-auto-hermes` 게이트웨이에 hook-route(stdin) heavy → `/route` → 큐 등록까지 실동작 확인. env/platform 가드·게이트웨이 다운 시 graceful `{}` 확인.

**재진입 차단(개선):** 기획 §11.5 의 env 가드에 더해, 훅 payload 의 `extra.platform != "cli"` 도 차단 조건으로 사용 → API 서버/게이트웨이 실행은 platform 으로도 걸러져 `--no-auto-hermes` 수동 경로의 취약점이 상당 부분 보완됨(env 설정은 belt-and-suspenders).

**남은 후속(B2-2/B2-3):** `alphred mcp` 브리지(명시적 큐 관리; 의존성은 공식 `mcp` SDK extra 권장) / 맥락 핸드오프 ①(세션 연결) / 완료 알림 경로.

### 11.13 라이브 실측 버그픽스 (Fix A/B/C, 2026-06-21)

실제 TUI 실행에서 **무거운 작업이 큐로 안 가고 인라인 실행 → 인터럽트 → 429 연쇄** 발생. 원인·조치:

- **원인 1(치명): `serve()`가 :8643 바인딩 전에 Hermes 준비를 최대 60s 블로킹.** `alphred` 직후 입력 시 :8643이 죽어 있어 라우팅 훅이 graceful `{}` → 인라인 실행(큐 0건으로 확인).
- **Fix A:** `serve()`가 Hermes 자동기동을 **백그라운드**로 돌리고 `uvicorn`을 즉시 바인딩. 준비 전에는 `QueueManager.pause_scheduling=True`로 **스케줄링만 보류**(고아·retry 소진 방지), 준비/시간초과 시 해제. /route 의 분류·큐등록은 Hermes 불필요라 즉시 동작.
- **Fix B:** `_ensure_daemon`이 spawn 후 **:8643 준비를 ~15s 폴링**한 뒤 TUI 진입 → 첫 메시지부터 라우팅 보장.
- **원인 2(soft): `pre_llm_call`은 턴 차단 불가** → 라우팅돼도 에이전트가 인라인 수행 위험.
- **Fix C:** `pre_tool_call` 훅(`alphred hook-guard`) 추가. `hook-route`가 Heavy 큐등록 시 `alphred_home/queued_turns/<session_id>` 마커 기록(매 턴 시작에 클리어→Heavy일 때만 설정) → 같은 턴의 모든 도구 호출을 `{"action":"block"}`으로 **하드 차단** → 에이전트는 ack 텍스트만. SOUL 정책도 강화.
- **부수 발견: Hermes가 config.yaml 을 자체 직렬화로 재기록하며 주석(`# alphred-hook` 태그)을 제거함.** → hookcfg 의 탐지/제거를 **태그가 아니라 command substring(`-m alphred.cli hook-`) 기반**으로 변경(추가는 이벤트별 멱등). 비게 된 이벤트 키(null)는 무해하므로 잔존 허용.
- **Note D(쿼터):** 코드 외 제약. 오프로드해도 백그라운드 실행+TUI가 Gemini 무료 20요청 공유. 권장: 유료/별도·소형 모델, `ALPHRED_MAX_RETRIES` 축소.

**검증:** pytest **117종**. 라이브 config 에 3개 훅(pre_llm_call/post_llm_call/pre_tool_call) 무중복 설치·pyyaml 파싱·allowlist 정확일치 확인. 스테일 데몬 종료(다음 실행 시 새 코드 기동). E2E(쿼터 회복 후): heavy→큐등록+인라인 미실행, light→즉답.

---

## 12. 런타임 견고화 — 평가 & 개선 (2026-06-22)

### 12.1 자체 e2e 테스트 결과
부품은 정상이나 **데몬 런타임 조립부에서 멈춤** 발견: health=True·슬롯빔·halted=False·next_runnable 정상인데 작업이 100s+ Pending 정지. fresh manager 의 `tick()` 1회는 즉시 시작(In-Progress) → 로직은 맞고 **데몬 인스턴스의 스케줄링만 갇힘**.

### 12.2 초기 기획 대비 평가
- **원래 핵심(게이트웨이+우선순위 큐+선점+상태머신+대시보드+OpenAI 호환)** = 거의 그대로 달성, 견고함.
- **복잡도/취약성**은 이후 추가한 **"TUI 자체를 큐-인식화"(§11 retrofit) + Hermes `:8642` 자동관리** 조립부에 집중. 원래 "서버형 다중 디바이스" 비전은 단단하고, 로컬 TUI retrofit + 별도 프로세스 오케스트레이션이 약한 고리.

### 12.3 D1 — 런타임 단일화 (구현 완료)
- **원인**: 별도 watcher 스레드가 세팅하는 `pause_scheduling` 플래그가 안 풀리는 갇힘 상태(이중 구조).
- **수정**: watcher+플래그 제거 → `QueueManager.ensure_upstream` 콜러블을 `tick()` 이 **매 틱 직접 호출**(단일 경로). `gateway._make_upstream_ensurer` = health 3s 캐시 + 미가동 시 `:8642` 자동(재)기동(20s 백오프) + 종료 정리. 단일 경로라 "플래그 안 풀림" 갇힘 클래스 자체가 소멸.
- **검증**: DB 클리어→새 데몬→heavy 제출→`Pending→In-Progress→Completed`(실결과) + P1 상태조회가 완료결과 정확 주입. pytest **126종**.

### 12.4 개선 백로그 (우선순위)
- **D2 `alphred doctor`** ✅ **구현(2026-06-22, §20)**: hermes 바이너리/:8642/:8643/모델·provider/플래너·LLM분류 플래그/큐 상태/안전망을 라이브 LLM 호출 없이 일괄 점검·출력. `--json` 지원. → `cli._cmd_doctor`/`_collect_doctor`.
- **D5 통합 테스트** ✅ **구현(2026-06-22, §20)**: 데몬 조립부(업스트림 게이팅→스케줄러→완주)를 fake 업스트림으로 끝까지 구동. heavy 완주·업스트림 down 보류→복구·크래시 복구(마감/재큐) 4종. → `tests/test_integration.py`.
- **D3 제품 분리**: (A) Alphred 서버(견고) / (B) 큐-인식 TUI(best-effort) 역할 명확화.
- **D4 작업별 모델 오버라이드** ⏸ **보류(사용자 결정 2026-06-22)**: heavy=강한 모델(결제 시), 약한 모델일 땐 P4로 솔직 표면화. (후속)

### 12.5 모델 현실
무료 `flash`/`gemma` 는 툴 적은 텍스트 작업은 완주하나, 브라우저/파일 생성 등 무거운 툴 루프는 타임아웃/부실. 무거운 실작업엔 강한 모델(결제) 필요 — 단 상태/결과 보고(P1·P4)는 모델 무관하게 정확.

---

## 13. 전용 Alphred TUI (Textual) — Hermes retrofit 대체 (2026-06-22)

### 13.1 결정 배경
"100% Hermes TUI" 고집을 버림. 과거 직접 TUI에서 깨진 건 **실시간 코드/명령 실행 스트리밍·긴 툴 출력**인데, **그건 Alphred 가 무거운 작업으로 백그라운드 큐에 빼는 대상**이다. 따라서 전용 TUI 전경은 텍스트 대화 + 큐 표 + 완료 알림만 그리면 되고(가장 깨지기 쉬운 라이브 툴 표시가 애초에 없음), 성숙한 프레임워크(**Textual**)로 스크롤/리사이즈/테이블을 위임하면 과거 버그 클래스가 사라진다.

### 13.2 구조 (구현 완료)
- `alphred/tui.py`(Textual `App`) = **게이트웨이(:8643) 클라이언트**(web/ESP32 와 동일한 단일 코어 클라이언트, retrofit 아님).
  - 입력 → `POST /v1/chat/completions` → **200**(Light 즉답) 렌더 / **202**(Heavy) 큐 등록 안내(대화 스레드에서 분리).
  - 큐 패널 → `GET /queue` 2초 폴링 → DataTable(상태 색상).
  - 유휴 시 상태 diff → 완료/폐기 알림을 대화 로그에 한 줄. Alph-RED 테마.
- CLI: 무인자 `alphred` → 전용 TUI(`_cmd_tui`, :8643 데몬 자동보장), `alphred tui` 명시. **`alphred chat` 은 Hermes TUI 직접 진입**으로 유지(라이브 툴 UI 원하면). `textual` 의존성 추가(core).
- 검증: Textual 헤드리스 마운트 스모크(`tests/test_tui.py`) + 라이브 게이트웨이 계약(heavy→202·/queue 표) 확인. pytest **128종**.

### 13.3 효과 / 트레이드오프
- **폐기 가능(후속 정리):** §11 Hermes 훅 retrofit(hook-route/guard·:8642 훅발화·cp949 훅인코딩·맥락핸드오프 배선)은 전용 TUI 경로에선 불필요 → 복잡도↓ 안정성↑. (단, `alphred chat`=Hermes 경로를 남기는 한 훅 설치는 선택적으로 유지 가능.)
- **잃는 것:** Light 질의의 라이브 툴 활동 표시(스피너+최종텍스트로 대체), Hermes 슬래시/스킬 UI(전경). 필요 시 `alphred chat`.
- 단일 코어 일관성: TUI 도 그냥 게이트웨이 클라이언트.

### 13.4 후속 → 구현됨(2026-06-22, §20)
- 큐 조작 단축키·작업 결과 상세 뷰 = §18 완료. **ASCII 메인화면·세션 복원/관리·멀티라인 입력 = §20 완료.** 훅 retrofit 코드 정리 = §20 에서 완전 제거.

---

## 15. 컨셉 전환 — Hermes 순정 유지, Alphred 정체성은 전용 TUI (2026-06-22)

### 15.1 결정
Alphred 는 Hermes 래퍼다. **순수 Hermes 는 `hermes` 로 그대로 쓰고, Hermes 측에는 Alphred 흔적을 남기지 않는다.** Alphred 의 정체성/UX 는 **전용 TUI(§13)** 가 담당(Alph-RED 테마, "◆ Alphred" 라벨). 따라서 §10 브랜딩(skin/SOUL)·§11 훅 retrofit 을 **Hermes 에서 제거**한다.

근거: §1~§9(게이트웨이+큐 코어)은 견고하나, Hermes TUI 를 Alphred 로 재포장하려던 retrofit 이 복잡도/취약성의 원천이었다(스킨·SOUL·훅·cp949·:8642 오케스트레이션). 전용 TUI 가 생기면 그 재포장이 불필요 → Hermes 는 순정으로 돌려 "순수 Hermes 사용"을 보장.

### 15.2 구현 (완료)
- `brand.revert` **완전 순정 복원**으로 강화: display.skin 백업복원 + Alphred 훅/allowlist 제거 + **SOUL 백업 복원**(없으면 우리 SOUL 삭제) + **설치한 스킨 파일 삭제**.
- `alphred setup`: 브랜딩 단계 제거(이제 Hermes provider 온보딩만; Hermes 순정 유지 명시).
- 무인자 `alphred` 첫 실행 자동 브랜딩 제거(`_maybe_first_run_brand`/마커 삭제). 무인자 `alphred` = 전용 TUI.
- `alphred brand apply` 는 **옵트인 레거시**로만 잔존(원하는 사용자가 명시 실행 시에만 Hermes 재스킨). 기본 경로 어디서도 자동 적용하지 않음.
- 라이브 검증: 실 Hermes config `display.skin=default`·Alphred 훅 NONE·스킨파일 삭제·SOUL=`# Hermes Agent Persona`(순정), pyyaml 파싱 OK. pytest **129종**(`test_revert_restores_stock` 추가).

### 15.3 사용 모델 (확정)
- **`alphred`** → 전용 Alphred TUI(큐 결합, Alph-RED). 백그라운드 데몬 자동.
- **`hermes`** (또는 `alphred chat`) → **순정 Hermes**(로고/배너/색상/정체성 원본, Alphred 흔적 0).
- 무거운 작업의 자율 프리앰블(P3a)은 per-request 프롬프트라 영구 흔적이 아님(유지). 에이전트 자기소개는 순정 "Hermes Agent" — Alphred 색은 전용 TUI 외형이 담당.

### 15.4 후속 정리 → 완료(2026-06-22, §20)
- §11 훅 코드(hook-route/guard·게이트웨이 `/route`·`/light/begin`)·`hookcfg.py`·§10 브랜딩(`brand.py`·`branding/` 스킨 머신·SOUL.md·skin.yaml·build_skin)·`alphred brand`/`setup` 브랜딩 = **전부 제거**(§20). 단, 배너 ASCII 아트만은 전용 TUI 스플래시(`splash.py`)로 재활용. 게이트웨이의 `light_begin/end`(§16 `/chat/stream` Light 선점)는 정상 코드라 유지.

---

## 16. 전용 TUI 작업 과정 실시간 표시 (#2, 2026-06-22)

### 16.1 구현
- 게이트웨이 **`POST /chat/stream`(SSE)** 추가. 분류 후:
  - **Heavy** → 큐 등록 + `event: queued`(id) 1개. (무거운 작업은 백그라운드, 전경 렌더 부담 0)
  - **Light** → `light_begin()`(진행 중 Heavy 선점) 후 Hermes `:8642 /api/sessions/{id}/chat/stream` 의 SSE를 **그대로 릴레이**. 세션은 `POST /api/sessions {"id":sid}`(409 무시)로 보장.
- Hermes 이벤트(실측): `run.started`·`message.started`·`assistant.delta`·`tool.progress`·`tool.started`·`tool.completed`·`tool.failed`·`assistant.completed`·`run.completed`·`done`. 형식 `event:{name}\ndata:{json}\n\n`.
- 전용 TUI `send()` 가 `/chat/stream` SSE를 소비해 **툴 실행을 실시간 렌더**: `🔧 {tool}…`(started) / `✓ {tool} — preview`(completed) / `✗ 실패`(failed), 최종 답변은 `assistant.completed.content`(◆ Alphred). 대화 연속성은 Hermes 세션(session_id)이 서버측 보관.

### 16.2 검증
- pytest **111종**(`/chat/stream` heavy=queued / light-upstream-down=graceful error 2개 추가).
- 라이브: light 질의 → `run.started→assistant.delta→assistant.completed("안녕!")→done` 까지 :8643 경유 정상 스트리밍 확인. 툴 사용 질의는 동일 스트림으로 `tool.started/completed` 전달.

### 16.3 후속
- `assistant.delta` 라이브 토큰 표시(현재는 completed에 최종 텍스트 일괄). 큐 작업 결과 ⚠확인필요 표시(result_needs_attention 재사용). #1 슬래시 명령(§17 추가 기획).

---

## 17. 전용 TUI 슬래시 명령 팝업 (#1, 2026-06-22)

### 17.1 구현 (c1 팝업 UX + c2 API 명령)
- **팝업**: `/` 입력 시 입력창 위 `OptionList` 가 명령 목록+설명 표시, 타이핑 실시간 필터, ↑/↓ 이동, Enter/Tab 선택(무인자=즉시 실행, 인자=`/cmd ` 채움), Esc 닫기. `PromptInput(Input)` 가 ↑/↓·Tab·Esc 가로채고 Enter 는 App 에서 처리.
- **레지스트리**(`_COMMANDS`) 한 곳이 팝업·`/help`·디스패치 공유.
- **명령**: `/help`(목록), `/model [이름] [--global]`(GET /v1/models 목록·세션 재생성으로 전환·config.yaml 라인편집), `/clear`·`/new`(대화+새 세션), `/queue`(새로고침), `/skills`(GET /v1/skills 목록), `/quit`. Hermes 전용(/browser,/skills install,/mcp)은 팝업 제외, `/help` 가 `alphred chat` 안내.
- **모델 전환 경로**: TUI `send` 가 `/chat/stream` 에 `model` 동봉 → 게이트웨이가 새 세션 생성 시 그 모델 사용(세션 모델은 생성 시 고정이므로 `/model` 은 새 session_id 로 적용).

### 17.2 검증
- pytest **112종**(헤드리스 `run_test`: `/` 팝업 표시·`/m` 필터(model only)·Esc 닫힘).
- 의존성: 기존 textual.

### 17.3 후속 → 구현됨(2026-06-22, §18)

## 18. 전용 TUI 마무리 4종 (2026-06-22)
- **라이브 토큰**: `assistant.delta` 를 `#streaming` Static 위젯에 누적 표시, `assistant.completed` 시 대화 로그로 확정(+스트림 클리어).
- **큐 조작 명령**: `/queue [list | cancel <id> | pause <id> | resume <id> | prio <id> <n>]` — 8자 prefix 를 `/queue` 목록에서 풀-id 로 해석(`_resolve_task_id`)해 게이트웨이(`/queue/{id}*`) 호출.
- **우선순위 열**: 큐 표에 `우선` 열 추가, 진행/대기 작업은 우선순위 내림차순 정렬.
- **⚠확인필요**: 완료 작업이 `result_needs_attention`(되물음/실패/산출물 없음)이면 `완료⚠` 로 표기.
- **큐 키보드 조작 + 상세 뷰**: `QueueTable(DataTable)` 포커스(Tab) 시 ↑/↓ 선택, **Enter/v 상세**(GET `/queue/{id}` → 결과 전문·이력 표시), **c 폐기·p/r 일시중지/재개·+/- 우선순위**, Esc 입력 복귀. 행↔id 매핑(`_rows`) + 자동 새로고침 중 커서 위치 보존.
- 검증: pytest **114종**(헤드리스: 큐 5열·포커스/액션키 무crash·Esc 복귀·슬래시 팝업·마운트).

---

## 19. 계획 기반(plan-aware) 분류 (2026-06-22)

기존: 하드코딩 키워드 정규식 + 길이(≤60) 휴리스틱이 메인, LLM 폴백은 잔여+기본 off → 키워드 취약성(오탐/미탐).
신규: **"해야 할 하위작업을 LLM이 분해 → 계획 구조로 Heavy/Light 판정 → 계획을 실행에 재활용"** (사용자 제안).

### 19.1 3-tier 사전필터 (`classifier.prefilter`, LLM 0비용)
1. 상태/큐 조회 → 즉답 Light (heavy 키워드 포함해도)
2. **확신-Heavy**: 명시적 대규모 마커(`전체 코드베이스/모든 파일/마이그레이션/크롤/...`) 또는 heavy 키워드 ≥2 → Heavy. (단축-Light 보다 **우선** — 한국어는 짧아도 무거울 수 있음)
3. 확신-Light: 실시간 짧은 채팅 / 인사·≤25자
4. **모호** → 플래너 위임(미가동 시 보수적 Heavy 폴백 = 기존 동작)

### 19.2 플래너 + 결정적 매핑 (모호 케이스만)
- `make_hermes_planner(client)`: 요청을 하위작업으로 분해(JSON: `subtasks[{title,kind,effort,tools}], urgent`). `classifier.parse_plan` 정규화(잘못된 kind/effort→기본값). 프롬프트별 1회 캐시.
- `classifier.plan_to_weight`: **코드가** 계획 구조로 판정(LLM이 heavy라 말하지 않음). Heavy 조건: `len≥3 || effort=heavy || kind∈{edit,compute} || tool_steps≥2`. → 경계 튜닝 가능.

### 19.3 계획 재활용 (P2)
- `Task.plan`(JSON 컬럼) 저장. `_start` 가 `_plan_hint(plan)` 로 실행 입력에 **제안 힌트**로 주입(강제 아님 → Hermes 자체 계획과 충돌 회피). TUI 상세뷰(Enter)에 하위작업 목록 표시. `_task_view` 에 `plan` 포함.

### 19.4 게이팅/안전
- `ALPHRED_PLANNER`(기본 off). 미설정/플래너 실패/타임아웃 → 사전필터 기본값(Heavy)으로 안전 폴백. 확신 tier 는 플래너 skip. 캐시로 재분해 방지.

### 19.5 검증
- pytest **127종**(+13: 3-tier·파서·매핑·통합(확신=플래너 skip, 모호=호출+plan저장)·캐시·힌트). 기존 분류 테스트 정합(reorder 후 회복).
- **라이브**: "이번 주 AI 뉴스 요약 보고서" → prefilter 모호 → 실모델이 3단계 계획(search/io/compute, web_search·web_extract) JSON 반환 → `plan_to_weight`=Heavy(3 steps, compute, 2 tool steps). 전 파이프라인 실모델 작동 확인.

### 19.6 후속 → P3 구현됨(§19.7)

### 19.7 단계별 진행 추적/표시 (P3, 2026-06-22)
- Hermes `/v1/runs/{id}/events`(SSE)는 **tool.started/completed/reasoning** 만 보낸다(어시스턴트 텍스트 없음 → `[STEP k]` 파싱 불가). 따라서 **도구 활동 추적**으로 구현: `_start` 가 run 시작 후 `_spawn_progress_tracker`(실 클라이언트만; fake 는 `_http`/`base_url` 없어 skip) → 데몬 스레드 `_track_run` 이 /events 를 소비해 **완료한 도구 수=`plan_progress`, 현재 도구=`plan_activity`** 를 `_set_progress`(락+In-Progress 일 때만)로 갱신.
- 컬럼 추가: `plan_progress`(int), `plan_activity`(text). `_task_view`·`/queue` 노출. TUI: 큐 표 In-Progress = `진행 k⚙`, 상세뷰(Enter) = 활동 라인(`현재 🔧 web_search`) + 하위작업 체크리스트(완료 도구 수만큼 ✓/▶/○).
- **통합 버그 수정**: 게이트웨이가 `classify_only` 로 분류(플래너 실행하나 plan 폐기) 후 그 kind/priority 를 **explicit 으로 submit** → submit 이 explicit override 분기로 빠져 plan 유실. → `classify_full`(plan 포함) 노출 + `submit(plan=, classify_reason=)` 사전계산 분류 수용. `_route_realtime`·`/chat/stream` 이 plan 을 그대로 전달.
- 검증: pytest **130종**(+3: 진행 In-Progress 한정 갱신·fake skip·사전계산 plan 저장). **라이브**: "파이썬 새 기능 정리" → 플래너 3단계 계획 저장(reason="plan: 3 steps, mutating/compute, 2 tool steps") + 진행 web_search→progress1→web_extract progress2 실시간 갱신 확인.
- 환경변수 `ALPHRED_PLANNER=1` 로 데몬 가동 중.

---

## 20. 관측성·통합테스트 + 전용 TUI 마무리 + 폐기 정리 (2026-06-22)

> 사용자 지시: D2/D5 개발, D4 보류, 전용 TUI 3종(ASCII 메인화면·세션 복원/관리·멀티라인 입력) 개발, "개발 불필요" 기획(§10 브랜딩·§11 훅 retrofit 잔재) 폐기 처분.

### 20.1 `/model` provider 버그픽스 (선행, 2026-06-22)
- `/models/available` 가 provider 를 모델 접두(`google/...`)에서 추론 → NVIDIA NIM 사용 중인데 Google Gemini 목록을 표시하는 버그. NIM 은 `google/gemma`·`meta/llama` 등 다벤더를 호스팅하므로 접두≠provider.
- 수정: `_read_model_cfg(cfg)`(config `model` 블록의 default/provider/base_url 동시 읽기) 신설, `models_available` 가 **`model.provider` 우선** 사용. 라이브: provider="NVIDIA NIM", nvidia 카탈로그(~120종) 반환 확인.

### 20.2 D2 — `alphred doctor` (관측성)
- 신규 CLI `alphred doctor`(`--json`). `_collect_doctor(cfg)` 가 **라이브 LLM 호출 없이**: hermes 바이너리 유무 / Hermes API(:8642) / 게이트웨이(:8643) 핑(2.5s) / 모델·provider(config 읽기) / 플래너·LLM분류 플래그 / 큐 상태(게이트웨이 또는 DB 직접) / 안전망(#30719) 트립 여부 점검. `[OK]`/`[!!]` 표로 출력 + 미응답 시 조치 힌트.

### 20.3 D5 — 데몬 조립부 통합테스트
- `tests/test_integration.py`: §12.3 D1(단일 경로 `tick()`→`ensure_upstream()`) 회귀 방지. fake 업스트림으로 (1) heavy 제출→tick→완주, (2) 업스트림 down 시 Pending 보류(폐기 안 함)→복구 시 완주 + ensure_upstream 매 틱 호출 확인, (3) 크래시 복구 마감(완료 run), (4) 크래시 복구 재큐(사망 run). 실 네트워크/Hermes 불필요.

### 20.4 전용 TUI 3종 (§13.4)
- **ASCII 메인화면**: 과거 브랜딩 배너 아트("ALPHRED AGENT")를 `alphred/splash.py` 에 임베드(Alph-RED 세로 그라데이션) → TUI 시작/새 세션/세션 복원 시 `_render_banner()` 로 표시.
- **멀티라인 입력**: 입력 위젯을 `Input`→`TextArea`(클래스명 `PromptInput` 유지). **Enter=전송 / Ctrl+J(또는 Shift+Enter)=줄바꿈**. `on_key` 에서 `prevent_default()` 로 Enter 의 기본 개행을 가로채고, 팝업 표시 중엔 ↑/↓·Enter·Tab·Esc 를 팝업 조작으로 전환. 팝업 필터는 `on_text_area_changed` 가 구동. CSS `height:auto; min 3 / max 10`.
- **세션 복원/관리**: `alphred/tui_sessions.py`(`SessionStore`) 가 화면 대화 기록을 `alphred_home/tui_sessions/<id>.json` 에 저장(첫 사용자 메시지=title). TUI 시작 시 직전 세션 자동 복원(화면 로그 재생), `/sessions`(목록)·`/sessions <번호>`(전환), `/clear`·`/new`(새 세션), `/model` 전환 시 새 세션 생성. `run_tui(url, key, sessions_dir)` 로 경로 주입(미주입=임시 모드, 테스트 호환).

### 20.5 "개발 불필요" 폐기 처분 (§15 방식)
- **삭제**: `alphred/brand.py`, `alphred/branding/`(skin.yaml·SOUL.md·build_skin.py·assets 전체), `tests/test_brand.py`. CLI `alphred brand` 명령·`_INTERCEPT` 에서 brand 제거. (게이트웨이 `/route`·`/light/begin`·`hookcfg.py` 는 이전에 이미 제거됨 — 확인.)
- **보존/재활용**: 배너 ASCII 아트만 `splash.py` 로 옮겨 전용 TUI 스플래시에 사용. `light_begin/end`(§16 정상 코드) 유지. `alphred setup`(Hermes 온보딩, 순정 유지)·`alphred chat`(순정 Hermes) 유지.

### 20.6 검증
- pytest **128종 통과**(브랜딩 11종 제거, 신규: 통합 4 + doctor 2 + TUI 4[멀티라인·스플래시·세션 영속·/sessions]). `alphred doctor` 라이브 정상 출력 확인.

### 20.7 실사용 버그픽스 — 파일 환각 안전망 + TUI UI (2026-06-23)
- **파일 생성 환각**(실사용 발견): 작은 모델이 `write_file` 을 실제 호출하지 않고 "저장했다 + 경로"만 산문으로 보고 → Alphred 가 거짓 완료로 표시. 원인은 Hermes 요청 덤프(`sessions/request_dump_*.json`)로 특정(인코딩·툴셋 정상, NVIDIA NIM `APIConnectionError` 도 병존). 대응(코어 무수정):
  - 자율작업 프리앰블 강화: 도구로 **실제 생성 + read_file 검증 후에만 완료 보고**, 안 한 일 보고 금지, 특정 형식(PDF 등)은 유효 파일 생성.
  - `claimed_missing_files()`: 결과가 주장한 절대경로가 **파일시스템에 실제 존재**하는지 검증 → 없으면 `result_needs_attention`=True. TUI 는 `완료⚠` + 완료 로그에 경고.
- **TUI UI 4종**: (1) 큐 패널 폭 축소(`width:34`)로 배너(폭114) 출력 패널에 맞춤, (2) 메인화면에 로고 엠블럼 추가(`splash.logo_lines`, 원본 `Alphred_Logo.txt` 삭제분 대체) — 출력 패널은 배너+로고만, (3) 현재 모델을 출력 패널 테두리 제목에 상시 표시(`_update_model_display`), (4) `ctrl+c` 종료 바인딩 제거→Textual 기본 선택복사 복원 + 입력창 포커스 시에도 화면 선택을 클립보드로 복사. 종료는 `ctrl+q`.
- 검증: pytest **133종 통과**(TUI +4: 로고·배너맞춤·복사·모델표시, core 환각 적발 +1).

### 20.8 TUI UI 2차 정리 (2026-06-23)
- **로고 깨짐**: 엠블럼이 `▟▙▰▔` 등 폰트 호환 낮은 글리프라 깨짐 → 배너와 동일한 `█`·`═`만 사용한 삼각+큐 엠블럼으로 교체.
- **화면 긁기 불가**: Textual 마우스 캡처가 터미널 네이티브 선택을 가로챔 → `App.run(mouse=False)` 로 실행(큐 패널은 키보드 Tab/↑↓ 로 조작; 마우스 휠 스크롤은 포기). ctrl+c 자체 복사 핸들러 제거, 종료는 `ctrl+q`.
- **줄바꿈 단축키**: Ctrl+J → **Shift+Enter** (Ctrl+J 는 호환 폴백 유지).
- **큐 표**: 모든 작업이 heavy 라 '종류' 열 삭제(ID/우선/상태/요청 4열), 요청문에 폭 양보(18자 컷). 큐 패널 폭 34→40 으로 소폭 확대.
- **반응형 배너**: 가용 폭에 따라 full(ALPHRED-AGENT, 114)/half(ALPHRED, 56)/mini(A, 8) 자동 선택(`splash.pick_banner`, 원본 배너 컬럼 슬라이스), 세로 여유(영역 ≥18줄)일 때만 로고 표시. 리사이즈 시 스플래시 한정 재렌더(`on_resize`→`call_after_refresh`). **버그**: RichLog 기본 `min_width=78` 로 114폭 배너가 잘림 → `write(..., expand=True)` 로 영역 폭까지 확장.
- 검증: pytest **134종 통과**(반응형 변형/좁은화면 무crash/4열/Shift+Enter).

### 20.9 `/skills` 버그 수정 + 스킬/자가개선 구조 규명 (2026-06-23)
- **버그:** TUI `/skills` 가 `:8643/v1/skills` 를 부르나 게이트웨이에 그 엔드포인트가 없어 404. → `HermesClient.skills()` + 게이트웨이 `GET /v1/skills`(:8642 프록시, 업스트림 실패 시 빈 목록+error graceful) 추가. TUI `cmd_skills` 가 이름·카테고리·설명 표시.
- **규명(스킬 사용 가능 여부):** Alphred 는 Hermes 에이전트 프록시라 **이미 스킬 사용 가능** — `hermes-api-server` 툴셋의 `skills_list/skill_view/skill_manage` + 공유 `~/hermes/skills/`. 시스템 프롬프트가 스킬을 이름+설명으로 광고, 에이전트가 `skill_view` 로 on-demand 로드.
- **규명(자가개선):** Hermes **Curator**(`agent/curator.py`)가 게이트웨이 cron 틱에 piggy-back(7일 간격, 유휴 트리거)으로 **에이전트 생성 스킬을 통합/아카이브**(새 생성 아님, 보조모델 fork). 새 스킬은 메인 에이전트가 `skill_manage(create)` 로, 턴 종료 `turn_finalizer→background_review` 가 저장 제안. Alphred 가 :8642 를 24h 살려두므로 **순정과 동일 파이프라인 작동**(모델 능력 의존).
- 검증: pytest **137종 통과**(gateway skills 2 + tui skills 1).
- 후속(미착수, 사용자와 합의): §21 작업 검증·수용 루프(Tier0 결정적 산출물 검사→Tier1 플래너 수용기준→Tier2 LLM-judge→Tier3 폐루프 재시도). 검증 실패가 곧 스킬 생성 신호 → Curator 와 결합 시 진짜 자가개선.

---

## 21. 작업 심화도(Depth Mode) + 검증·수용 루프 (기획, 2026-06-23)

> 목표: Claude Code 처럼 (a) 가벼운 작업은 적은 토큰으로 직답하고, (b) 무거운/고위험 작업은 **조사 → 하위작업 기획 → 철저 수행 → 검증**을 거쳐 성공을 보장한다. Hermes 코어 무수정, Alphred 게이트웨이/큐 계층에 구현.

### 21.0 Hermes 기존 기능 조사 결론 (선행 확인)
- **`reasoning_effort`**: `none/minimal/low/medium/high/xhigh` 6단계(`hermes_constants.parse_reasoning_effort`, `VALID_REASONING_EFFORTS`). 그러나 ⓐ **모델 추론(thinking) 예산**이라 추론모델 전용 → NVIDIA gemma/llama 엔 거의 무효, ⓑ **전역/에이전트 설정**(`agent.reasoning_config`, CLI 메뉴 `test_reasoning_effort_menu`)이지 **API 요청별 설정 아님**(chat_completion_helpers 전부 `agent.reasoning_config` 사용), ⓒ `delegate_tool` 이 자식 에이전트에 한해 override 가능.
- **`delegate_tool`**(하위에이전트 spawn: 자체 toolset/depth/reasoning, `delegation.max_spawn_depth`), **`mixture_of_agents`**(다중에이전트): 에이전트 **자율** 도구이지 작업 심화도 오케스트레이션은 아님.
- **결론**: "조사→기획→수행→검증을 심화도별로 켜고 끄는" 오케스트레이션은 **Hermes에 없음** → Alphred 담당(이미 §19 플래너 토대 보유). 또한 사용자 모델이 추론모델이 아니므로 **심화도는 reasoning_effort 가 아니라 '파이프라인 단계 수'로 구현**한다(혼동 금지). 추론모델 사용 시에만 depth→reasoning_effort 보조 매핑(옵션).

### 21.1 세 가지 작업 심화도 (Depth Tier)
| Tier | 트리거 | 파이프라인 | 비용 | 검증 | 재시도 |
|---|---|---|---|---|---|
| **low** | 단순질의·상태조회·인사 (prefilter Light) | 직접 1콜 응답 | 최소 | 없음 | 0 |
| **mid** | 일반 heavy (planner: 단일~소수 subtask) | (간단 plan) → 실행 → Tier0 | 보통 | 결정적(존재/형식) | 1 |
| **high** | 복합·대규모·고위험·산출물요구 | 조사 → subtask 기획(+수용기준) → 철저 실행 → Tier0+Tier2 → 미달 시 폐루프 | 많음 | 결정적+LLM-judge | 2 |

- **심화도 결정 `plan_to_depth(plan, prefilter)`**: §19 prefilter/planner 확장 — subtask 수, effort, mutating 여부, 산출물 유무, 위험도(파일쓰기/외부전송/되돌리기 어려움)로 low/mid/high 산출.
- **사용자 오버라이드**: TUI `/depth low|mid|high`, 헤더 `X-Alphred-Depth`, 자연어("대충/빠르게"→low, "철저히/제대로"→high).
- **토큰 절약 핵심**: low 는 plan/verify 절대 안 함(현 Light 유지). 심화도가 비용을 게이팅한다.

### 21.2 high 모드 파이프라인 (4단계)
1. **조사(Research)** — 실행 전 컨텍스트 수집: 관련 스킬 탐색(`skills_list`), 파일/코드 정찰(`read_file`/`search_files`), 필요시 `web_search`. 산출 = 조사노트(제약·자원·접근법). Alphred 가 가벼운 별도 run 으로 수행하거나 실행 프롬프트에 "먼저 조사" 지시.
2. **하위작업 기획(Plan)** — §19 planner 재사용 + 각 subtask 에 **수용기준(acceptance criterion)** 부여(검증가능 조건). 조사노트 반영.
3. **철저 수행(Execute)** — plan_hint + 강화된 자율 프리앰블(실제 도구 사용+자체 검증). 필요시 `delegate_tool` 위임.
4. **검증(Verify)** — 21.3.

### 21.3 검증·수용 루프 (계층)
- **Tier 0 — 결정적 산출물 검사(무비용, 게이트)**: 주장 경로 존재 + 비어있지 않음 + **형식 유효성**(PDF `%PDF`, docx/xlsx zip 시그니처, JSON 파싱, 코드 syntax-check). 명령형 작업은 종료코드. 실패→`NeedsReview`/재큐. (`claimed_missing_files` 확장)
- **Tier 1 — 수용기준 평가**: plan 의 각 criterion 을 결정적 검사 우선, 불가 시 Tier2 로.
- **Tier 2 — LLM-judge(2차 의견)**: 원요청+결과+산출물목록+조사노트 → 검증 run → `{verdict: pass|fail, score, unmet:[...], evidence}`. 보조모델/저비용, **high 한정**(쿼터).
- **Tier 3 — 폐루프 재시도**: unmet 을 피드백으로 새 run("이전 시도가 X 미달, 보완해 완수") — `conversation_history` 핸드오프(T4-B) + retry예산 재활용. 상한(high=2, mid=1). 미수렴 시 `NeedsReview` 로 사람에게.

### 21.4 추가 기획 기능 (검증 프로세스 보강)
1. **수용기준 명세(Definition of Done)**: 제출 시 LLM 이 요청에서 검증가능 기준 추출 → `Task.acceptance`(JSON) 저장 → 검증 기준점. TUI 에서 편집 가능.
2. **사전 비용 견적 + 게이트**: high 는 실행 전 예상 단계/콜수 추정 표시 → (옵션) 사용자 승인. 쿼터 보호.
3. **검증 증거 패널(Evidence)**: 완료 시 "무엇을 어떻게 검증했는지"(파일 열림·테스트 통과·judge 점수)를 TUI 상세뷰/대시보드에 노출 → 신뢰성 가시화(Claude Code 의 'verified' 감각).
4. **드라이런/플랜 프리뷰**: high 작업은 실행 전 plan+acceptance 를 보여주고 승인/수정(Claude Code plan mode 유사). 백그라운드 자율은 자동승인.
5. **재시도 예산 + 서킷브레이커**: depth별 상한 + 연속 실패 시 halt(기존 RestartGuard/safety 재사용)로 쿼터 폭주 방지.
6. **검증 실패 → 자가개선 피드백**: 반복 실패 패턴(예: PDF 생성불가) 감지 → "환경 보강 제안"(라이브러리 설치) 또는 `skill_manage` 로 레시피 스킬화 → Curator 유지. = §20.9 자가개선 결합.
7. **검증기 독립성**: 실행과 다른(또는 같은) 모델로 judge, 자기검증 편향 완화. **결정적 검사를 LLM 보다 우선**(환각 방지).
8. **부분 성공 처리**: all-or-nothing 대신 criterion별 통과/실패 → "3개 중 2개 완료, X 미달" 보고. `result_needs_attention` 확장.
9. **검증 캐시/스킵**: 동일 산출물 재검증 회피, low/순수조회 검증 스킵.
10. **관측성**: `alphred doctor` 에 검증 통계(통과율·평균 재시도·judge 비용) 추가.

### 21.5 데이터모델 / 배선
- `Task` 에 `depth`(low/mid/high), `acceptance`(JSON), `verify_state`(none/pending/passed/failed), `verify_report`(JSON), `attempts` 컬럼 + 마이그레이션.
- 새 상태 `Verifying`, `NeedsReview`(Completed 와 구분), 상태머신 전이 추가.
- `QueueManager._finalize_active`: run done → depth≥mid 면 verify → pass=Completed / fail+예산=재큐(Tier3) / fail+소진=NeedsReview.
- 게이트 플래그 `ALPHRED_VERIFY`(기본 off), `ALPHRED_DEPTH_DEFAULT`. 쿼터 인지: Tier2 는 high 만, low 는 검증 전무.

### 21.6 단계적 구현 제안
- **V1 (무비용·즉시가치)**: `plan_to_depth` 심화도 분류 + Tier0 결정적 검증 게이트 + `NeedsReview` 상태 + 증거 패널. **LLM 추가콜 0**. ✅ **구현 완료(2026-06-23)**.
- **V2**: 수용기준 명세(Tier1) + LLM-judge(Tier2, high 한정) + 폐루프 재시도(Tier3). ✅ **구현 완료(2026-06-23)**.
- **V3**: 비용 견적/승인, 드라이런 프리뷰, 자가개선 피드백, doctor 통계. ✅ **구현 완료(2026-06-23)**.

### 21.8 V1 구현 (2026-06-23)
- **심화도**: `classifier.plan_to_depth(plan, kind)` → low(Light)/mid(Heavy 단순)/high(다단계·heavy·mutating). `submit()` 가 `Task.depth` 저장.
- **Tier0 검증**: `queue_manager.verify_artifacts(result)` — 저장동사+절대경로 추출 후 존재·비어있지않음·**형식 시그니처**(PDF `%PDF`, OOXML/zip `PK`, png/jpg/gif, json 파싱) 결정적 확인. `{passed, checks[], summary}`. 산출물 주장 없으면 통과.
- **마감 게이트**: `_finalize_done()` 신설(=`_finalize_active`·`recover` 공용) — done run → verify → 통과=`Completed` / 미통과=`NeedsReview`(신규 상태). `verify_report`(JSON) 저장.
- **상태/DB**: `TaskState.NEEDS_REVIEW`(is_terminal, In-Progress→NeedsReview / NeedsReview→Pending|Discarded 전이), `tasks.depth`·`tasks.verify_report` 컬럼+마이그레이션.
- **게이트 플래그**: `cfg.verify`(env `ALPHRED_VERIFY`, 기본 ON — Tier0 무비용). gateway/cli QueueManager 배선.
- **표면화**: gateway `_task_view` 에 `depth`·`verify_report`·`error`; TUI 큐표 `검토필요` 라벨 + done 목록 포함 + 완료알림에 검증요약 + 상세뷰 **검증 증거 패널**(`_render_verify_report`); 대시보드 `.NeedsReview` 색상; `alphred doctor` 에 검증 플래그.
- 검증: pytest **145종**(+8: plan_to_depth·verify_artifacts·finalize NeedsReview/Completed/disabled·state머신·gateway depth·TUI 라벨/증거). 데몬 재기동 필요(게이트웨이/큐 변경).

### 21.9 Tier0 유연화 리팩터(A안, 2026-06-23)
- 동기: 초기 V1 이 "파일(특히 PDF) 생성" 가정에 치우쳐 경직 — 사용자 지적 수용.
- **형식 검증 레지스트리화**: `_FORMAT_VALIDATORS`(ext→매직바이트|콜러블) + `register_format(ext, validator)` 확장 지점. 미등록 확장자는 존재+비어있지않음만(graceful). PDF색 문구 중립화. (pdf/png/jpg/gif/docx/xlsx/pptx/zip/gz/json 기본)
- **플러그형 체커**: `_CHECKERS=[_check_files]` — 산출물 종류별 검증기를 골격 변경 없이 추가(향후 url/exit-code/no-op). 체크 스키마 통일 `{check,target,ok,detail,exists?,nonempty?}`.
- **탐지 확장**: `~/` 홈 전개, 따옴표/백틱 안 경로(공백 허용), URL(`://`) 제외(버그픽스: `https` 의 `s:/` 를 Windows 드라이브로 오인하던 것). 상대경로는 서버측 cwd 불명확이라 의도적 미해석(오탐 방지).
- **"검증 안 함" 구분**: `checked` 카운트 추가 — 파일 주장 없는 작업은 passed=True 이되 summary 로 "검증 안 함(의미 검증 미적용)" 명시(거짓 안심 방지). 의미·비파일 검증은 V2(Tier1/2) 몫임을 명문화.
- 검증: pytest **147종**(+2: flexible_detection·excludes_urls; 스키마 변경 반영).

### 21.10 V2 구현 — LLM-judge(Tier2) + 폐루프(Tier3) (2026-06-23)
- **수용 judge(Tier1+2 통합)**: `classifier.build_judge_prompt(request, result)` + `parse_verdict()` — judge 가 요청에서 수용기준(DoD)을 추론하고 RESULT 충족 여부를 채점, `{passed, score, criteria:[{name,met,note}], unmet[], summary}` 반환(verdict 없으면 score≥70 보정, 파싱 실패 None). `make_hermes_judge(client)` 콜러블.
- **마감 흐름**(`_finalize_done`): Tier0 통과 → **high 심화도 & judge opt-in 시** judge 호출(`_run_judge`, **fail-open**: 오류/None=통과 처리 → 좋은 작업 안 막음). Tier0 실패 시 judge 호출 안 함(쿼터 절약).
- **폐루프(Tier3)**: judge 미통과 + 재시도 예산(`judge_max_retries`, 기본 2) 남으면 `_requeue_for_verify` — 미흡 항목(unmet)을 `verify_feedback` 로 저장하고 Paused(자동재개)+**백오프**(retry_base_seconds)로 재큐, `hermes_run_id`/plan_progress 리셋. `_start` 가 `_autonomous_input(prompt, plan, feedback)` 로 피드백 주입해 재실행. 예산 소진 시 NeedsReview.
- **데이터**: `Task.verify_attempts`(int)·`verify_feedback`(text) 컬럼+마이그. `verify_report.judge` 에 verdict 저장.
- **게이트/배선**: `cfg.judge`(env `ALPHRED_JUDGE`, 기본 **OFF** — 쿼터)·`judge_max_retries`(`ALPHRED_JUDGE_RETRIES`). gateway/cli `make_hermes_judge` 배선. doctor 에 judge 플래그. TUI 증거 패널에 judge 점수·기준·미흡 표시. `_task_view` 에 `verify_attempts`.
- **알려진 한계(V3 개선 대상)**: judge 호출이 스케줄러 tick(`_lock`) 내 동기 실행 → judge 동안 submit 가 잠깐 블록(단일 슬롯이라 수용 가능, opt-in). 향후 비동기 워커로 분리.
- 검증: pytest **154종**(+7: parse_verdict·feedback주입·judge pass/fail-requeue-NeedsReview/high게이팅/fail-open/Tier0우선; TUI judge 렌더).

### 21.11 V3 구현 — 견적·드라이런·자가개선·통계 (2026-06-23)
- **비용 견적**: `classifier.estimate_cost(plan, depth, judge_enabled)` — 결정적 러프 추정 {steps, tool_steps, est_llm_calls, band}. `_task_view` 와 `/plan` 에 노출, TUI 상세/큐에 "~N콜" 표시(정밀 토큰 추정은 신뢰도 낮아 생략).
- **드라이런 프리뷰**: 게이트웨이 `POST /plan` — 분류+계획+심화도+견적만 반환, **실행/큐등록 없음**(모호 입력이면 플래너 1콜은 들 수 있음). TUI `/plan <요청>` 명령으로 심화도·예상 하위작업·견적 미리보기.
- **자가개선 피드백 + 통합 자가치유**: `_failure_suggestion(report, verdict)` 가 실패에서 실행가능 힌트 도출(없는 파일→실제 생성+확인, 형식 불일치→정식 도구/라이브러리 사용, judge 미흡→unmet). **Hermes 환경 자동변경 안 함**(설치는 에이전트가 스스로) — 사용자 "무수정" 방침 준수. 힌트는 Tier3 재시도 입력에 주입 → 성공 시 Hermes background_review/Curator 가 스킬로 보존(자가개선 루프 폐합). **Tier0 실패도 high 면 자가치유 재시도**로 통합(`_verify_retry_budget`: high=judge_max_retries, mid/low=0 → 쿼터 보호). report 에 `suggestion` 저장, TUI "제안:" 표시.
- **doctor 통계**: `_collect_doctor` 에 검증 통계 섹션(DB 집계, 무비용) — 통과율(Completed/(Completed+NeedsReview)), 검토필요 수, 심화도 분포, 평균 재시도, judge 평균 점수.
- 검증: pytest **160종**(+6: estimate_cost·suggestion·tier0 self-heal/mid-no-retry·plan_preview·doctor stats·TUI /plan).
- 결론: §21 검증·수용 루프 V1~V3 완성 — low/mid/high 심화도로 토큰 게이팅, Tier0(무비용)+Tier2(judge,opt-in)+Tier3(자가치유 재시도) + 드라이런/견적/통계로 관측·예측 가능.

### 21.12 API 세션 + 검증 노출 + README (2026-06-23)
- **API 세션(갭 수정)**: `/v1/runs` 가 `session_id`·`conversation_history` 를 받도록 — 같은 session_id 로 후속 Heavy run 이 한 Hermes 세션(서버측 맥락) 공유, 미지정 시 run별 독립(=run_id). (`/v1/chat/completions`=무상태 messages 재전송, `/v1/responses`=`previous_response_id` 체이닝.)
- **검증 결과 API 노출(갭 수정)**: `GET /v1/runs/{id}` 가 `state`·`depth`·`needs_review`·`verify_attempts`·`verify_report`·`session_id` 반환(기존엔 NeedsReview→completed 로 가려져 보이지 않던 것). API로 검증 루프 사용: `/plan` 미리보기 → `/v1/runs` 제출 → `/v1/runs/{id}` 폴링.
- **TUI 현재 세션 표시**: 출력 패널 테두리 제목에 모델+세션(`_set_titlebar`/`_session_label`), 새/전환/첫메시지 시 갱신. `SessionStore.save` 가 원본 dict 의 title 을 직접 갱신하도록 수정(메모리·디스크 일관).
- **README(EN/KO)**: 게이트웨이 API 표 갱신(+/plan·/v1/skills·세션·검증 필드), **세션** 표·예시, **검증 & 작업 심화도** 섹션·curl 예시, 설정(ALPHRED_VERIFY/JUDGE/JUDGE_RETRIES), doctor 설명, 역량 표 행 추가.
- 검증: pytest **163종**(+3: runs session_id·run_status 검증노출·TUI 세션 타이틀).

### 21.7 리스크 / 주의
- **쿼터**: judge/재시도가 콜 배증 → depth 게이팅·캐시·상한 필수.
- **약한 모델**: judge 자체 부정확 → 결정적 검사 우선, judge 는 보조.
- **지연**: high 길어짐 → 백그라운드 큐라 수용 가능, 진행표시 활용.
- **무한루프**: 재시도 상한 + 서킷브레이커.
- **reasoning_effort 혼동 금지**: NVIDIA 모델엔 무효 → depth 는 오케스트레이션으로 구현(추론모델 한정 보조 매핑만).

## 22. 코드베이스 정비 + 운영 안정화 + 큐/세션 삭제 + 로고 복원 (2026-06-27)

> 목표: (a) 흩어진 중복/취약 로직을 단일화하고, (b) `alphred` 실행 시 뜨던 백그라운드 서버 창의 잔존·무한 부활 문제를 구조적으로 제거하며, (c) 큐 영구삭제·세션삭제 등 누락 기능을 채우고, (d) 시작화면 로고를 원본 아스키 아트로 복원한다. Hermes 코어 무수정 유지.

### 22.1 리팩토링 후속 — 중복/취약 로직 단일화
- **JSON 추출 통합**: 신규 `alphred/jsonutil.py` `parse_json_object(text)` — `classifier.parse_classification/parse_plan/parse_verdict`·`nlq.parse` 4곳의 `re.search(r"\{.*\}", DOTALL)+json.loads` 중복 제거. `json.JSONDecoder.raw_decode` 스캔이라 문자열 내 중괄호·뒤따르는 산문에도 안 깨짐(기존 greedy 정규식보다 견고). 순환 import 회피 위해 leaf 모듈로 분리.
- **config.yaml model 블록 헬퍼 통합**: `config.py` `read_model_config`/`read_default_model`/`set_model_default(hermes_home, ...)` 신설 — gateway 읽기 regex 와 tui 쓰기 regex 두 구현을 한 곳으로. 주석·순서 보존이 필요해 전체 YAML 라운드트립 대신 라인 단위 처리. gateway `_read_model_cfg`/tui `_set_global_model` 은 위임 래퍼로 축소.
- **DB 스키마 drift 방지**: `db._migrate()` 가 하드코딩 컬럼 목록 대신 `Task.__dataclass_fields__` 를 순회해 누락 컬럼을 자동 `ALTER`(타입/기본값은 `_col_ddl` 도출). 삽입 컬럼(`_TASK_COLS`)·마이그레이션이 모두 dataclass 한 곳에서 파생 → 필드 추가 시 스키마 자동 추종, 구버전 DB self-heal.

### 22.2 백그라운드 서버 창/무한 부활 해결 (TUI 세션 종속)
- **근본 원인**: `_ensure_daemon` 이 `serve` 를 DETACHED_PROCESS 로 분리 → TUI 와 수명 단절된 orphan(종료 경로 전무). 창 없는 serve 가 콘솔앱 `hermes.exe` 를 창 억제 없이 spawn → 새 콘솔 창 발생. `_make_upstream_ensurer` 가 Hermes 다운 감지 시 20s 백오프로 **무제한 재기동**(RestartGuard 미연결).
- **수명 종속(Job Object)**: 신규 `alphred/childproc.py`(ctypes만, 무의존성) — `spawn_managed`/`terminate_managed`. Windows 는 `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` Job 에 자식 편입 → TUI 가 정상/강제/크래시 어느 식으로 끝나도 OS 가 트리(serve + 상속된 hermes)를 함께 종료. POSIX 는 `start_new_session`+`killpg`.
- **창 숨김**: serve(`CREATE_NO_WINDOW`)·hermes(`CREATE_NO_WINDOW`+`hermes.log` 리다이렉트) 양쪽 콘솔 창 제거.
- **정리 배선**: `_ensure_daemon` 은 자기가 띄웠을 때만 Popen 반환(이미 떠 있으면 None → 영속 데몬 보존), `_cmd_tui` 가 try/finally 로 종료 시 정리.
- **서킷 브레이커**: `_make_upstream_ensurer` 에 전용 가드(`hermes_restarts.json`, 게이트웨이 자체 안전망 `restarts.json` 과 파일 분리) 연결 — 임계 초과 시 재기동 중단+1회 경고(세션 내 폭주 차단).
- 검증(실측): 부모 `os._exit`(정리 미실행)에도 손자 프로세스가 OS 에 의해 종료됨(kill-on-close backstop) 확인. 기획: `C:\Users\alpha\.claude\plans\jaunty-hugging-flask.md`.

### 22.3 큐 영구 삭제(purge) + 히스토리 비우기
- 기존 `discard`(소프트, Discarded 상태로 history 보존) 외에 **영구 삭제** 추가.
- `db.delete(task_id)`/`db.delete_by_states(states)` — 작업행+`task_events` 원자적 삭제(BEGIN IMMEDIATE).
- `QueueManager.purge(task_id)`(진행 중이면 `stop_run` 후 삭제)·`clear_history()`(Completed/NeedsReview/Discarded 일괄 삭제, 건수 반환).
- 배선: CLI `alphred queue purge <id>`·`alphred queue clear`; gateway `DELETE /queue/{id}/purge`·`POST /queue/clear`; TUI `/queue purge <id>`·`/queue clear`(오삭제 방지 위해 단축키 없이 명령 전용).

### 22.4 세션 삭제 (TUI 노출) + 작업 연쇄 삭제 + 세션 ID
- `SessionStore.delete()` 는 있었으나 UI 미노출이던 갭 해소. `cmd_sessions` 에 `/sessions delete|rm|del <번호|ID>` 추가 — 현재 세션 삭제 시 `_new_session()` 으로 새 세션 시작. 목록/사용법 안내·슬래시 팝업 설명 갱신.
- **세션 ID 노출/참조(2026-06-27)**: 세션은 `alphred-tui-<hex8>` ID 를 갖지만 ⓐ 목록에 미표시였고 ⓑ 표시용 단축이 `id[:8]`=`"alphred-"` 로 잘려 비유일이던 버그. `_short_sid()`(접두사 제거→hex8) 도입, ID 생성은 `_new_sid()` 로 단일화. `/sessions` 목록에 단축 ID 표시, `_session_label`(타이틀바)도 단축 ID 사용. `_resolve_session(items, token)` 으로 **번호 또는 ID(단축/전체 prefix)** 양쪽 참조 가능(큐 작업 ID 해석과 동일 규칙).
- **세션→작업 연쇄(2026-06-27 보강)**: 작업은 세션을 출처로 생성되므로(`Task.session_key` = TUI `session_id`), 세션 삭제 시 그 세션에서 만든 큐 작업도 함께 제거한다. `QueueManager.purge_session(session_key)`(진행 중이면 `stop_run` 후 삭제, 건수 반환) + gateway `DELETE /queue/by-session/{session_key}`. TUI 세션 삭제가 이 엔드포인트를 호출해 연쇄 삭제 후 큐를 새로고침("연결된 작업 N건 삭제" 표시). 다른 세션 작업은 보존.

### 22.5 시작화면 로고 복원 + 여백/게이트 정비
- 증상: 배너는 정상이나 로고는 이전에 만든 삼각형 placeholder 였음(원본 `Alphred_Logo.txt` 는 삭제·git/휴지통 복구 불가). 사용자가 원본 아트를 `docs/Alphred_Logo.txt` 로 복원 제공.
- 처리: 원본을 패키지 자산 `alphred/assets/Alphred_Logo.txt` 로 편입(pyproject `package-data`), `splash._load_logo()` 가 로드(상하 빈 줄 제거+공통 들여쓰기 dedent, 자산 없으면 폴백). 초상화형 아트라 **줄별 센터링은 왜곡** → `_gradient_block`(블록 동일 패딩) 으로 정렬 보존하며 세로 그라데이션. 가용폭 부족 시 로고 생략(짤림 방지).
- **여백/게이트 보강(2026-06-27)**: 자산·docs 파일을 외곽 여백 제거 형태로 정규화(상하 빈줄·좌측 공통여백·우측 패딩 제거 → 33줄×34폭 타이트). 스플래시 표시 게이트가 옛 ~10줄 로고 기준 고정값(`h>=18`)이라 큰 아트에서 배너를 밀어내던 버그 수정.
- **반응형 로고(2026-06-27)**: 배너의 `pick_banner`(full/half/mini) 전략을 로고에도 적용. 100%(33×34)·75%(`Alphred_Logo_75.txt`, 25×26)·50%(`Alphred_Logo_50.txt`, 16×17) 변형 파일을 블록 평균 다운스케일로 생성(`assets/`, package-data). `splash.pick_logo(avail_width, avail_height)` 가 배너 아래 남은 세로·가로 공간에 **모두 들어가는 가장 큰 변형**을 선택, 어느 것도 안 들어가면 생략. `_paint_splash` 가 `h - 배너줄수 - 2` 를 가용 높이로 전달 → 터미널 크기에 맞춰 자동 축소(오버플로 방지).

### 22.6 검증
- pytest **174종**(+6: gateway purge/clear-history/by-session, TUI 세션 삭제, 반응형 로고 선택, 세션 단축 ID/참조) 전부 통과. 로고 렌더·블록 센터링·반응형 변형 선택·세션 연쇄 삭제·세션 ID 해석·구버전 DB self-heal·Job kill-on-close 실측 확인.
- 데몬/게이트웨이·TUI 변경 포함 → 실행 환경에서 재기동 필요. 창 미표시·영속 데몬 보존·로그 적재(`hermes.log`/`serve.log`)는 실제 `alphred` 실행으로 수동 확인 권장.

## 23. 큐 상대 우선순위 재정렬 — LLM Queue Ranker (2026-06-27)

> 목표: Heavy 작업의 우선순위를 큐의 다른 작업과 **비교해 상대적으로** 매긴다. 긴급도뿐 아니라 **작업 간 의존성·효율적 선후관계**까지 LLM 이 판단해 신규·기존 우선순위를 유연하게 재배정한다.

### 23.0 문제 (실측)
- Heavy 우선순위가 큐를 보지 않고 절대값 고정: `prefilter` ambiguous→5, confident-heavy→3, `plan_to_weight` heavy→4. 비교 단계 부재.
- 재현: "침착맨 분석"(실행 중) + "육식맨 먼저 분석"(추가) → **둘 다 prio 5**. "먼저"라는 의도 무시, 순서 무의미 → LLM 큐의 핵심 기능 누락.
- 단, 스케줄러는 이미 우선순위 기반 Heavy 선점(`_maybe_preempt`) 보유 → **우선순위만 제대로 매기면 실행 중 작업까지 재정렬됨**(실행엔진 변경 불필요).

### 23.1 구현
- **랭킹 LLM**: `classifier.build_rank_prompt(new_task, queue)` + `parse_rank(text)`(공용 `jsonutil` 재사용, priority 클램프·미지 id 무시). 프롬프트가 긴급도 + **의존성/선후관계**("B가 A의 선행이면 B>A")를 명시 지시, 차등 우선순위 1..10 요청.
- **콜러블**: `queue_manager.make_hermes_ranker(client)`(planner/judge 팩토리 패턴) → `(new_task, queue) -> [{id,priority,reason}]|None`.
- **재정렬**: `QueueManager._rerank_heavy_queue(trigger_id)` — 활성 Heavy(Pending/In-Progress/Paused) 스냅샷(락) → 다른 Heavy 없으면 **no-op(LLM 미호출)** → LLM 호출(락 밖) → 결과를 `store.set_priority` 로 신규+기존에 적용(락) + 신규 `classify_reason` 에 랭킹 사유 보강. 실패/None 시 기존 유지(graceful). `submit()` 의 락 블록 밖에서 `kind==heavy && ranker` 일 때 호출.
- **의존성=우선순위로 인코딩**: 별도 dependency 그래프 없이 prio(B)>prio(A) 로 표현, 기존 선점이 실행 순서 재정렬.
- **게이트(기본 ON)**: `cfg.rank`(env `ALPHRED_RANK`, 기본 on; `0/false/no` 만 끔). `runtime.build_manager` 가 `make_hermes_ranker` 주입. HermesClient 만 있으면 동작 → 추가 설정 불필요. 큐에 비교 Heavy 가 없으면 LLM 호출 자체가 없어 단일 작업은 **무비용**.

### 23.2 검증
- pytest **181종**(+7: parse_rank ok/clamp/bad, build_rank_prompt, 재정렬 신규>기존, 단일 시 미호출, 예외 graceful, **랭킹 상향 신규가 실행 중 Heavy 선점**) 전부 통과.
- 게이트웨이/큐 변경 포함 → 데몬 재기동 필요. 실측(수동): "A 분석" 실행 중 "B 먼저" 요청 → B prio>A 로 B 가 선점 실행되는지 TUI 큐 표 확인.

## 24. 작업 심화도 사용자 오버라이드 — /depth (2026-06-27)

> §21.1 에 "사용자 오버라이드: TUI `/depth low|mid|high`, 헤더 `X-Alphred-Depth`" 로 기획됐으나 **미구현**이던 갭 해소(실제로는 depth 가 `plan_to_depth` 자동 판정만 됐고 수동 지정 경로가 없었음).

- **submit 오버라이드**: `QueueManager.submit(depth=...)` 추가 — `low/mid/high` 유효값이면 자동 판정(`plan_to_depth`)을 덮어쓰고, 그 외/None 이면 기존대로 자동.
- **TUI `/depth`**: `cmd_depth` + `_COMMANDS` 등록. `self.depth_override`(세션 런타임, 모델처럼 유지) 를 `/chat/stream` 바디로 전송. `/depth`(현재 표시)·`/depth low|mid|high`(고정)·`/depth auto`(해제).
- **게이트웨이**: `_depth_ov(request)` 가 `X-Alphred-Depth` 헤더(low/mid/high) 해석 → `_route_realtime`·`/v1/runs` 에 적용. `/chat/stream` 은 바디 `depth` 필드.
- **CLI**: `alphred queue submit --depth low|mid|high`.
- 검증: pytest **182종**(+1: `X-Alphred-Depth` 헤더가 대규모 입력의 depth 를 low 로 강제). submit 단위(auto/override/invalid→auto) 실측 확인.

## 25. TUI 큐 패널 세션 표시 + 타이틀바 세션 ID (2026-06-27)

> 큐는 전역 단일 슬롯이라 여러 세션의 작업이 한 큐에 섞인다(세션 전환은 뷰만 바꿈, 작업은 백그라운드 지속). 어느 작업이 어느 세션에서 왔는지 식별 가능하도록 표시를 보강.

- **큐 패널 폭 확대**: `#queuepanel` `width: 40 → 54`(세션 열 수용).
- **세션 열 추가**: 큐 표 컬럼 `ID·우선·상태·요청` → `ID·세션·우선·상태·요청`. 각 행에 작업의 `session_key` 단축 ID(`_short_sid`) 표시(없으면 `-`). 표시원: 게이트웨이 `_task_view` 에 `session_key` 노출(신규).
- **타이틀바 세션 ID**: 상단 출력 패널 제목이 세션 *제목* 대신 **단축 세션 ID** 를 표시(`_session_label` → `_short_sid` 항상). 제목 없는 세션도 고유 식별.
- 검증: pytest **183종**(+1: `/queue` 뷰가 `session_key` 노출; 큐 5열·타이틀바 ID 테스트 갱신).

## 26. 실행 하네스(시스템 프롬프트) 외부화 — 결과물 품질 (2026-06-27)

> 문제: 실측 결과 백그라운드 산출물이 얕고 빈약함(깊이 부족). 기존 실행 프리앰블은 "되묻지 말고 완수 + 파일 실제 생성·검증"만 담은 짧은 하드코딩 문자열이라 **품질/깊이 가이드가 없었음**. Gemini·Claude·ChatGPT 급의 "깊이 있고 근거 있고 완결된" 결과를 광범위 분야에서 내도록, 업계 모범 사례 기반의 풍부한 시스템 프롬프트를 작성하고 **사용자 편집 가능 외부 파일**로 분리.

- **하네스 자산**: `alphred/assets/system_prompt.md`(패키지 기본, package-data). 섹션 구조 — 역할 / 실행 맥락(자율·실제 생성·검증) / 핵심 원칙(정직·깊이·근거) / 작업 절차(이해→조사→계획→실행→자기검증→보고) / **깊이·충실도 기준(얕음 방지)** / 출력·형식 / 분야별 수월성 / 마무리 자기점검 / `## 작업 요청` 구분선. 편집 가이드 주석 포함.
- **로더** `alphred/prompt.py`: `load_harness(alphred_home)` — 사용자 편집본(`ALPHRED_HOME/system_prompt.md`) 우선 → 패키지 기본 → 최소 폴백. `default_prompt_text`/`init_user_prompt`/`user_prompt_path`.
- **주입**: `queue_manager._autonomous_input(prompt, plan, feedback, *, harness, depth)` 가 하네스를 prepend(기존 하드코딩 `_AUTONOMOUS_PREAMBLE` 제거). `QueueManager(system_prompt=...)` 저장, `_start` 가 `harness=self.system_prompt, depth=task.depth` 전달. `runtime.build_manager` 가 `load_harness(cfg.alphred_home)` 주입.
- **심화도 연동**: `_DEPTH_DIRECTIVE`(high/mid) 를 요청 뒤에 덧붙여 철저함·검증 강도 차등.
- **편집 UX**: `alphred prompt`(요약) · `--show`(전문) · `--path`(적용 소스/경로) · `--init [--force]`(편집본 생성). `cfg.system_prompt_path` 속성. 편집본은 업데이트와 무관하게 우선 적용.

### 26.1 영어 전환 + 스킬 연동 + OS별 명령 (2026-06-27)
- **영어로 전환 + 상세화**: 토큰 효율·성능 유도를 위해 하네스를 영어로 재작성(13.8KB). 마커 `autonomous background task`, 구분선 `## REQUEST`. (출력은 사용자 언어 유지 지시.)
- **실제 스킬 조사·연동**(Hermes home snapshot 기준): PDF 편집=`nano-pdf`, **PPTX=`powerpoint`**(생성/읽기/편집), 문서 파싱=`ocr-and-documents`(markitdown), XLSX=`excel-author`(optional), 코딩 에이전트=`claude-code`/`codex`/`opencode` 노출 확인 → "스킬 있으면 우선 사용" 지시.
- **포맷 디자인 가이드(스킬 없는 형식)**: MD(구조·표·근거), **PDF 신규 생성**(authoring 스킬 없음 → `weasyprint`/`reportlab`+`matplotlib`, CJK 폰트 등록), **DOCX**(전용 스킬 없음 → `python-docx` 스타일/표/TOC), 디자인 원칙 상세 수록.
- **코드/명령 실행**: `agy`(Antigravity)는 **optional·비활성**(snapshot 미노출) → 개발 요청에 자동 실행 안 됨을 명시. 코드 실행은 `execute_code`/`terminal` **툴**(스킬 아님) + 코딩 에이전트 스킬. 전용 shell 스킬이 없어 **Windows(PowerShell/cmd)·Linux(bash)·macOS(zsh) 명령 사용법**을 하네스에 상세 수록(포터블 규칙: OS 감지·절대경로·`execute_code` 우선).
- pyproject `package-data` 에 `assets/*.md` 추가(하네스 패키징).
- 검증: pytest **188종** 유지(마커/구분선·심화도 테스트 영어 마커로 갱신).

## 27. 옵션/설치형 스킬 사용법 정리 + 스킬 관리 툴 가용성 (2026-06-27)

> 질문: Alphred TUI 에서 Hermes 의 `skills_hub`/`skill_manage` 툴을 쓸 수 있나? 옵션 스킬(`agy` 등)은 어떻게 활성화하나?

- **결론(정적 검증)**: 가능. `skill_manage` 가 에이전트 툴 레지스트리에 등록(`registry.register(name="skill_manage", ...)`)되어 있고 스킬 허브도 존재. `config.yaml` `disabled_toolsets: []`, `allow_lazy_installs: true`. Alphred 는 Hermes 에이전트의 얇은 레이어라 **TUI 채팅·큐 run 양쪽에서 동일 툴 세트 사용** → "스킬 설치해줘" 요청 시 에이전트가 `skill_manage`/허브로 `~/.hermes/skills/` 에 설치.
- **스킬 계층**: 번들(`hermes-agent/skills/`, 기본 노출) / 활성·사용자(`~/.hermes/skills/`) / **옵션(`hermes-agent/optional-skills/`, 비활성)** / 외부·허브(GitHub, 스캔 후 설치).
- **옵션 활성화 3경로**: ① Alphred 채팅으로 요청(에이전트 자가 설치) ② `skills.external_dirs` 에 경로 추가 ③ `~/.hermes/skills/` 로 수동 복사(재인덱스 필요할 수 있음).
- **CLI 래퍼 스킬 2레이어**: `antigravity-cli`(`agy`)·`claude-code`·`codex` 는 절차 가이드일 뿐 → **바이너리 별도 설치** 필요. 실측: 이 환경에 **`agy` 1.0.12 이미 설치됨**. 즉 레이어2 충족, 스킬 활성화(레이어1)만 하면 사용 가능.
- **라이브 기능 테스트**: Hermes(:8642)+Alphred(:8643) 가동 + LLM 쿼터 소모 + 에이전트(gemma) 비결정성이 필요해 자동 수행 보류(사용자 확인/그들의 TUI 에서 직접 실행이 자연스러움).
- README(EN/KO) 에 "Skills — built-in/optional/installable" 섹션 추가(계층 표 + 3경로 + 2레이어 주의 + 재기동/nudge).

### 27.1 라이브 검증 + 클라이언트 타임아웃 + 설치류 Heavy 라우팅 + antigravity 활성화 (2026-06-27)
- **라이브 검증(Alphred 경유)**: serve 기동(:8643+자동 :8642) 후 `/v1/chat/completions`(Light) 로 에이전트가 `terminal` 툴로 `agy --version`→"1.0.12" 보고(HTTP 200). **Alphred→에이전트 툴 경로 실작동 확인.** 단, gemma 에 *optional 스킬 설치* 자동 지시는 1회 건너뜀/1회 ReadTimeout(아래로 개선).
- **클라이언트 타임아웃 상향**: 설치/긴 도구턴이 동기 Light 경로의 `HermesClient`(기존 120s)에서 끊기던 문제 → `cfg.client_timeout`(env `ALPHRED_CLIENT_TIMEOUT`, 기본 **300s**) 신설, `runtime.build_manager` 가 주입. (health 체크 5s 는 유지.)
- **설치·활성화류 자동 Heavy 라우팅**: `classifier._ADMIN_HEAVY_PAT`(스킬/플러그인/패키지/툴 + 설치/활성화/셋업/업그레이드, 양 언어·양 순서) → prefilter 가 Heavy(prio 6)로. 느린 관리 작업을 동기 대신 **백그라운드 큐**로 보내 타임아웃 회피. 조회/목록은 `_ADMIN_QUERY_PAT` 가드로 Light 유지.
- **antigravity 결정적 활성화**: `~/.hermes/config.yaml` `skills.external_dirs` 에 `optional-skills/autonomous-ai-agents` 추가 → **`/v1/skills` 에 `antigravity-cli` 노출 확인(총 65개, autonomous 7종)**. agy 바이너리(1.0.12) 이미 설치 → 즉시 사용 가능 상태. (LLM 없이 `/v1/skills` 로 검증.)
- 검증: pytest **190종**(+2: 설치류 Heavy 라우팅, 목록 Light 유지).

## 28. 자율 백그라운드 실행 차단 해소 — Hermes 승인 게이트 (2026-06-28)

> 문제: `approvals.mode: manual` 환경에서 Alphred 백그라운드 run 의 `execute_code`(및 위험 터미널 명령)가 **승인 대기→타임아웃 차단**("BLOCKED: ... has NOT consented")되어 자율 작업이 실제 코드를 못 돌림 → 산출물 부실의 핵심 원인.

### 28.0 근본 원인 (Hermes 코드 분석)
- `tools/approval.py:check_execute_code_guard()` 통과(자동승인) 조건: sandbox env **또는 `HERMES_YOLO_MODE`(import 시 freeze)/세션 YOLO/`approvals.mode=off`** 또는 비-게이트웨이 컨텍스트.
- Alphred 가 띄운 Hermes 는 `platform=api_server` → `_is_gateway_approval_context()`=True(게이트웨이). `mode=manual` → 승인요청 emit 후 블록 대기. **Alphred 는 승인 요청을 응답하는 클라이언트가 아니라** 60s 타임아웃 차단.
- 승인 모드는 **config.yaml 에서만** 읽고 env 오버라이드 없음(`_get_approval_mode`). → per-process 레버는 **`HERMES_YOLO_MODE` env 가 유일**. 하드라인(rm -rf /·셧다운 등)은 YOLO 보다 먼저 무조건 차단(안전 바닥 유지).

### 28.1 해결 (A안: Alphred-spawn 게이트웨이에 YOLO 주입)
- `config.autonomous_exec`(env `ALPHRED_AUTONOMOUS_EXEC`, **기본 on**). `gateway._spawn_hermes_gateway` 가 켜져 있으면 spawn env 에 `HERMES_YOLO_MODE=1` 주입.
- **범위**: Alphred 가 직접 띄운 :8642 전용. 대화형 `hermes`/`alphred chat`(별도 프로세스)은 무영향. 이미 떠 있는 사용자 게이트웨이를 재사용하면 미적용.
- **방어층 유지**: ① Hermes 하드라인 무조건 차단 ② Alphred submit 시 `safety.scan_payload` 라이프사이클 명령 차단.
- 끄려면 `ALPHRED_AUTONOMOUS_EXEC=0`.

### 28.2 검증
- 라이브(Alphred 경유): 이전엔 `execute_code ... BLOCKED(231s) has NOT consented` → 적용 후 **`execute_code completed (3.23s)`**, 작업 Completed, 산출 파일 실존(합계 338350) 확인. 차단 해소 end-to-end 확인.
- pytest **192종**(+2: autonomous_exec → spawn env 의 `HERMES_YOLO_MODE` 주입/미주입).

## 29. 결과물 품질을 commercial 수준으로 — 모델 라우팅 · Light 하네스 · 설정 튜닝 · MoA (2026-06-29)

> 출처: `docs/Hermes 에이전트 성능 비교 분석.md` — 순정 Hermes 가 Claude Code/Gemini Web/Antigravity 대비 산출물 품질이 떨어지는 5대 구조적 원인 분석. 본 섹션은 그 원인들을 **Hermes 코어 무수정**으로 Alphred 래퍼 층에서 메우는 계획.

### 29.0 진단 — 보고서 5대 원인 ↔ 현 Alphred

보고서 대전제: **체감 품질 = 50% 모델 + 50% 하네스**.

| 보고서 원인 | 현 Alphred 대응 | 남은 갭 → 본 섹션 |
|---|---|---|
| ① 컨텍스트 압축 함정(제약·추론 상실) | Heavy 는 conversation_history SSOT 보유 + 검증 피드백 재주입(§21) | Hermes 자체 압축 임계치 무손질 → **§29.3** |
| ② 보조모델 하향 라우팅(인지 오염) | Alphred 자체 보조작업(분류/플래너/judge/ranker)은 **메인 모델** 사용 → 오염 없음 | Hermes 내부 `auxiliary.*` 품질 플로어 미관리 → **§29.3** |
| ③ 아이덴티티/콜드스타트(빈 SOUL) | **Heavy: §26 하네스로 해결** | **Light(즉답)=순수 패스스루 → 바닐라** → **§29.2** |
| ④ 툴서치 인지 과부하 | 미대응(`tool_search` 무손질) | → **§29.3** |
| ⑤ 툴규율 + 웹검색 + MoA | 툴규율=§26 하네스로 해결 | 웹검색 엔진·MoA off → **§29.3 / §29.4** |
| (대전제) 모델 50% | 단일 config 모델 고정, D4 보류 | **작업 깊이별 모델 라우팅 부재 = 최대 갭** → **§29.1** |

핵심: 하네스 절반은 §26 으로 commercial 에 근접. 남은 척추는 **모델 축(§29.1)** + Light 콜드스타트(§29.2) + Hermes 설정(§29.3) + 단일모델 직관 탈피(§29.4).

### 29.0.1 실측(디스크 검사로 확정 — 라이브 LLM 0콜)

- **`/v1/runs` 는 요청별 `model` 을 실행에 반영하지 않음.** `api_server._handle_runs`(L3735)는 `body.get("model")` 을 **run 상태 메타데이터(광고)** 로만 저장. 실제 agent 는 `_create_agent`(L1046) → `model = _resolve_gateway_model()` 로 생성 → **config.yaml `model.default` 만 사용**(세션 model 도 무시). chat/responses 도 동일.
- **그러나 `_resolve_gateway_model` → `_load_gateway_config` 는 config.yaml 을 mtime-keyed 캐시로 재읽기**(매 agent 생성 시). → **Alphred 가 디스패치 직전 `model.default` 를 교체하면 그 run 에 반영**됨(파일 안 바꾸면 캐시 유지=효율적). 이게 §29.1 의 메커니즘.
- 현 config 실측: `model.default=google/gemma-4-31b-it`, `provider=nvidia`(NIM, ~120모델 단일 프로바이더). `compression`(enabled/threshold:0.5/protect_first_n:3/protect_last_n:20/…) #1, `auxiliary.{vision,web_extract,compression,skills_hub,approval,mcp,title_generation,…}`(provider:auto/model:'') #2, `web.backend: brave-free`(Firecrawl 아님)/`search_backend:''` #5, `tool_search:`(L507) #4 키 **존재 확인**. **MoA 키는 config 에 없음**(이 버전) → 보고서의 Hermes MoA 토글은 미해당 → §29.4 가 그 자리를 Alphred 층에서 채움.

### 29.1 depth별 모델 라우팅 — 전적 사용자 설정 (P0, 척추)

목표: 사용자가 업무 깊이(High/Mid/Light)별로 **쓸 모델을 직접 배치**. 기본 미설정 = 동작 무변화.

- **설정 표면 2개, 단일 해석기:** ① 환경변수 `ALPHRED_MODEL_HIGH/MID/LOW`(config.py) ② 영속 `ALPHRED_HOME/models.json`(TUI/대시보드에서 설정, 재시작 내성). 우선순위 `env > models.json > base_default(현 config.default)`. `Config.model_for_depth(depth) -> spec{model, provider?, base_url?}|None`. (tier 이름 = depth 와 동일 high/mid/low — 작업 무게 Heavy/Light 혼동 회피.)
- **메커니즘(실측 29.0.1):** QueueManager 에 `apply_model(spec)` 콜백 주입. `_start`(tick `_lock` 하 직렬화)에서 `task.depth` 의 spec 을 디스패치 직전 `config.set_model_fields()` 로 `model.default`(+옵션 provider/base_url) 교체 후 `start_run`. **단일슬롯 + Light 선점(`_active_lights>0` 면 Heavy 미시작)** 불변식이 config.default 교체를 자연 직렬화 → 레이스 안전. 값이 같으면 미기록(mtime 캐시 유지).
- **프로바이더 결합 주의:** 같은 프로바이더 내 model-id 전환(예 NIM: Light=gemma-4-31b ↔ High=llama-3.3-70b)은 model.default 만 바꾸면 됨. **크로스 프로바이더**(High=Claude/Gemini Pro)는 spec 에 provider/base_url 동봉 + 해당 자격증명이 `.env` 에 선설정돼 있어야 함(없으면 자문). v1 은 model-id 중심, provider override 는 고급 옵션.
- **Light 경로:** 게이트웨이 Light 디스패치 전 `model_light` 설정 시 `light_scope` 안에서 apply. 미설정 depth → base_default 로 복원(config.default 항상 현재 작업 의도 모델 반영).
- **`/model` 명령 강화(TUI):** `/model`(무인자)=현재 depth 매핑 + `/models/available` 목록. `/model high|mid|low <name>`=depth 설정(models.json, 유효성=available 검증). `/model high auto`=해제. 기존 `/model <name>`(세션)·`--global`(config.default) 유지. (tier 이름은 작업 무게 Heavy/Light 와 혼동을 피해 light 대신 **low** 사용.)
- **게이트웨이/CLI/관측:** `GET·POST /models/tiers`(매핑 조회/설정), `alphred model tiers` CLI, `doctor` 에 매핑 + 카탈로그 부재 모델 경고.
- **코어 무수정·업데이트 내성:** model.default 는 표준 사용자 설정. models.json 은 Alphred 소유. tier 미설정 시 config.yaml 절대 안 건드림(완전 무변화).

### 29.2 Light 하네스 — 콜드스타트(#3) 마감 (P0, 기본 ON)

- `prompt.py`(§26)와 대칭: `ALPHRED_HOME/light_prompt.md` + 패키지 기본 `alphred/assets/light_prompt.md`. `prompt.load_light_harness()`, CLI `alphred prompt --light {--show|--init|--path}`.
- Light 경로(`gateway._route_realtime`)에서 **시스템 메시지 1개**(≤~300토큰: 유능·직답 전문가/과잉거절·과잉부연 금지/근거 제시/사용자 언어/빠른 질의 간결+완결)를 messages 앞에 주입. = Hermes 무수정 게이트웨이 소유 SOUL.
- **계약 보존:** ① **호출자가 이미 system 메시지를 넣었으면 미주입**(의도 존중) ② 요청별 opt-out `X-Alphred-Harness: off` ③ chat/completions·responses(input/멀티모달·previous_response_id) 보존. config `ALPHRED_LIGHT_HARNESS`(**기본 on**, 사용자 결정).

### 29.3 `alphred tune` — Hermes 설정 품질 감사·적용 (#1·#2·#4·#5, P1)

- `config.set_model_default` 의 안전 라인편집을 **블록 스칼라 일반 편집기**(`set_yaml_scalar(block, key, value)`)로 일반화. `alphred tune`=읽기전용 감사(doctor 스타일 표: 항목·현재값·권장·이유, LLM 0콜). `alphred tune --apply <knob...>`=opt-in(백업→멱등→pyyaml 파싱 검증), `alphred tune --revert`=백업 복원.
- knob(실측 키 기준): **#1** `compression.protect_first_n`(3→상향)·`protect_last_n`·`threshold` / **#2** `auxiliary.*.{provider,model}` 약모델이면 메인 고정 권장 / **#4** `tool_search` 임계 상향/강모델·소툴셋시 비활성 / **#5** `web.backend brave-free → Tavily/SearXNG` 안내(키 필요→자문, 키 있으면 적용). MoA 키 부재 → 자문 제외(§29.4 가 대체).
- 코어 무수정: 사용자 config.yaml 만 백업 후 편집, 멱등, Hermes 소스 미수정.

### 29.4 Alphred-side MoA — high-depth 멀티에이전트 (#5, P1)

Hermes MoA(이 버전 부재)에 의존하지 않고 Alphred 가 게이트웨이 위에서 오케스트레이션. **단일코어 불변식 유지** → 별도 큐 항목/스케줄러 아님, **한 작업 실행 파이프라인 내부 단계**(검증 루프와 동형).

- **Mode A(기본, 추천): 초안→비평→종합.** 실행 결과 → judge/critique(`make_hermes_judge` 인프라 재활용)로 약점 도출 → 종합/개정 패스. 기존 Tier3 self-heal 확장선.
- **Mode B(옵션): N-표본→집계.** high-depth 에서 K개 독립 시도(§29.1 의 다른 tier 모델 활용 시 다양성↑) → 집계 LLM 종합.
- 게이팅: `ALPHRED_MOA`(**기본 off**, 비용) + high-depth 한정 + 예산 상한(K·최대 콜). 단일슬롯 존중(작업 내부 순차).

### 29.5 QA 인수 기준 (QA-29)

- **QA-29.1**(모델 라우팅): (a) 해석 우선순위 env>json>base 단위테스트 (b) `_start` 가 depth spec 으로 apply_model 호출 후 start_run (fake 로 검증) (c) tier 전무 시 config.yaml 미변경(무변화) (d) 잘못된 모델명 거부+available 제시 (e) `/model high X` 재시작 후 유지 (f) `/models/tiers` GET/POST 라운드트립.
- **QA-29.2**(Light 하네스): (a) Light 호출에 system 메시지 주입 (b) 기존 system 있으면 미주입 (c) `X-Alphred-Harness: off` opt-out (d) responses 멀티모달/previous_response_id 보존 (e) 주입 토큰 상한 회귀.
- **QA-29.3**(tune): (a) 감사 LLM 0콜 표 출력 (b) `--apply` 백업+멱등+유효 YAML(pyyaml 파싱) (c) `--revert` 복원 (d) 미지원 키 graceful.
- **QA-29.4**(MoA): (a) high-depth + `ALPHRED_MOA` → Mode A 비평·종합 패스 실행 (b) 기본 off (c) 예산 준수 (d) 별도 스케줄러/DB 없음(단일코어 불변식).

### 29.6 시퀀스 · 리스크

- **순서:** §29.1 → §29.2 → §29.3 → §29.4. ①②가 체감 품질 즉효, ③④가 천장 상향.
- **리스크:** ① config.default 교체가 사용자 파일 변경 → tier 미설정 시 절대 안 건드려 완화; base_default 복원으로 일관성. ② 크로스 프로바이더는 자격증명 의존 → v1 동일프로바이더 중심+자문. ③ Light 하네스 토큰 비용 → 짧게 유지+opt-out. ④ MoA 비용 → opt-in+high 한정+예산.

## 30. 코드베이스 정비 — UX 다듬기 · 중복 제거 · 정리 (2026-06-29)

§29 직후 전체 코드베이스 점검 + 사용자 지정 UX 개선. **현재 상태 기준**(아래 결정으로 일부 이전 기록이 갱신됨).

### 30.1 사용자 지정 UX 변경
- **`/clear` ↔ `/new` 분리:** 기존엔 둘 다 새 세션(중복). 변경 — `/clear`=화면 출력만 비우고 **현재 세션·대화 맥락 유지**(`Ctrl+L`과 동일), `/new`=새 세션. (`cmd_clear`/`cmd_new` 분리.)
- **입력 히스토리 ↑/↓:** `PromptInput` 에서 팝업 없을 때 **커서가 맨 윗줄이면 ↑=이전 입력, 맨 아랫줄이면 ↓=다음**, 끝을 넘으면 작성 중 초안 복원, 연속 중복 제외(멀티라인 편집 보존). `_history`/`_hist_idx`/`_hist_draft` + `history_prev/next`.
- **depth tier `light → low` 리네임:** 작업 무게 Heavy/**Light** 와의 혼동 제거. tier 이름 = depth 이름(high/mid/low) 1:1. `ALPHRED_MODEL_LIGHT→LOW`, `/model high|mid|low`, `/models/tiers`, config/gateway/doctor/README/테스트 일괄. (작업 KIND light/heavy 는 그대로 — 의미 다름.)

### 30.2 중복/죽은 코드 정리
- **죽은 코드 제거:** `gateway.Store`·`childproc.sys`(미사용 import), `gateway._read_default_model`(미호출), `queue_manager.run_light`(미호출)+`hermes_client.respond`(유일 호출처가 run_light)+`hermes_client.extract_text`+`import json`(전체 미사용). → `alphred/` pyflakes **클린**.
- **큐 조작 공용 헬퍼(①):** `tui._queue_op(op, tid, priority=)` 추출 — 슬래시 `cmd_queue` 와 키보드 `_do_queue_action` 이 같은 게이트웨이 호출(cancel/pause/resume/prio)을 공유. 고유부(상세뷰 렌더·prefix 해석·+/- 계산)는 유지.

### 30.3 `alphred chat` 제거(②) — 이전 §13/§15/§28의 "유지" 결정 철회
- **이유:** `alphred chat` 은 전용 코드 없이 `chat` 토큰을 그대로 Hermes 로 위임했는데, **Hermes 에 `chat` 서브커맨드가 없어**(`hermes_cli/main.py`: unknown token→chat prompt 처리) 순정 TUI 가 아니라 'chat' 프롬프트로 오해석될 수 있었음(문서-동작 불일치).
- **변경:** cli 가 `chat` 을 인터셉트해 **"`alphred chat` 은 제거됨 — 순정 Hermes TUI 는 `hermes` 실행" 안내 후 종료(rc=2)**. README EN/KO·cli help·TUI `/help` 에서 `alphred chat` 언급 제거 → 전부 `hermes` 안내. (§13.4/§15.3/QA-1.1 등 이전 기록은 그 시점 결정의 history 로 보존.)

### 30.4 자연어 큐 제어 TUI 노출(③)
- `nlq`(자연어 큐 제어)는 CLI `alphred queue ask`/`POST /queue/ask` 에만 있고 TUI 엔 없었음(사각지대; TUI 입력은 에이전트로 가 큐 도구 부재). → 전용 TUI 에 **`/queue ask "<요청>"`** 추가(`POST /queue/ask` 호출, reply/results 표시). `/queue` 사용법·명령 설명 갱신.

### 30.5 검증
- 신규 테스트: `_queue_op` 엔드포인트 매핑, `/queue ask` 자연어, `alphred chat` 제거(rc=2+안내), `/clear` 세션유지·`/new` 리셋, ↑/↓ 히스토리. `tests/test_cli.py` 신설.
- `alphred/` pyflakes 0 경고. 전체 pytest 통과.

### 30.6 구조 리팩터 A — `queue_manager` God-class 분할 (행동 보존)
`QueueManager`(1056줄)가 분류·스케줄·선점·검증·judge·MoA·진행추적·복구·모델적용을 전부 담당 → **순수 함수/팩토리를 모듈로 분리**(동작 무변경, 호출처는 새 위치에서 import).
- **`verify.py`(신규)**: §21 Tier0 검증(`verify_artifacts`·`register_format`·형식검증기·`_check_files`)과 환각/되물음 휴리스틱(`result_needs_attention`·`claimed_missing_files`·`_claimed_file_paths`), `failure_suggestion`. Task/QueueManager 의존 없는 순수 함수.
- **`llm_calls.py`(신규)**: `make_hermes_{classifier,planner,judge,ranker,moa}`. 5종이 동일 골격(프롬프트→chat_completion→파서)이라 **공통 `_chat()` 으로 5중 중복 제거**. (보조작업도 메인 모델 사용 — #2 오염 회피 명시.)
- **`prompt.py`(+)**: 실행 입력 조립 이전(`autonomous_input`(구 `_autonomous_input`)·`_plan_hint`·`_DEPTH_DIRECTIVE`) — 프롬프트 구성 로직과 한 곳에.
- **`queue_manager.py`**: QueueManager + 스케줄러 + `_TRANSIENT`/`is_transient_error` + `_LightMarker` 만 남김(**1056→721줄**). 내부 호출은 새 모듈에서 import.
- **호출처 정리(re-export 셰임 없이):** runtime→`llm_calls`, tui→`verify`, 테스트(test_core/classifier_llm/planner/prompt)→각 새 모듈. `is_transient_error` 는 queue_manager 잔류.
- 결과: `alphred/` pyflakes 0, 전체 **212 pass**(회귀 0). 남은 구조 리팩터: **B**(gateway create_app 라우터 분할)·**C**(tui App 분리)·**D**(표현 상수 통합·지연 import 정리).

### 30.7 구조 리팩터 B — `gateway.py` 라우터 분할 (server/ 패키지, 행동 보존)
`create_app`(438줄) 하나에 인증·분류/라우팅 헬퍼 8개·**라우트 핸들러 26개**가 중첩 클로저로 뭉쳐 있던 것을 그룹별 `APIRouter` 모듈로 분리. **API 표면·동작 무변경**(라우트 핸들러 본문 1:1 이전, 캡처 변수만 `deps.*` 로 치환).
- **`server/` 패키지 신설:** `deps.py`(`GatewayDeps` 데이터클래스 + `make_auth` + 공유 요청 헬퍼 `overrides/depth_ov/source/extract/route_kind_hint/submit/apply_light_harness/route_realtime` + `task_view/sse_event/aiter/STATE_TO_RUN`), `routes_admin/models/openai/queue.py`(각 `build_router(deps)->APIRouter`).
- **인증 중복 제거:** 26곳의 `dependencies=[Depends(auth)]` → 라우터 단위 1회(`APIRouter(dependencies=[Depends(make_auth(cfg))])`). 단 `routes_admin` 은 대시보드 무인증 유지 위해 라우터 전역 미부착 + safety만 개별 부착.
- **`gateway.py` 얇아짐(734→253줄):** `Scheduler`·`create_app`(deps 생성→4개 라우터 include)·`serve`+업스트림 생명주기(`_make_upstream_ensurer`·`_spawn_hermes_gateway` 등)만 잔류. `_read_model_cfg`/`_curated_models`/`_task_view`/`_sse_event`/`_STATE_TO_RUN` 제거(→ deps/routes_models). `_curated_models`→`routes_models`, `_read_model_cfg`→제거(`config.read_model_config` 직접).
- **외부 영향 최소:** `create_app`/`Scheduler`/`serve`/`_spawn_hermes_gateway` 는 잔류 → 테스트 무변경. 유일한 코드 변경은 `cli.py` 의 `_read_model_cfg`→`config.read_model_config`.
- **검증:** import·순환 OK, `alphred/` pyflakes 0, 전체 **212 pass**. 라이브 스모크(TestClient): `/`(200 무인증)·`/v1/models`(무키 401→키 200)·`/models/tiers`·`/queue`(200)·`/safety`(무키 401) 확인. 남은: **C**(tui App 분리)·**D**(횡단 정리).

### 30.8 구조 리팩터 C — `AlphredTUI` Mixin 분해 (행동 보존)
`AlphredTUI(App)` 한 클래스(1030줄, ~50 메서드)에 팔레트·히스토리·명령·세션·큐패널·SSE가 뭉쳐 있던 것을 **Mixin으로 분리**(App 메서드는 `self` 상태 공유라 함수 추출 불가 → Mixin이 정석).
- **`tui_base.py`(신설):** 상수(`_ACCENT`/`_AMBER`/`_BORDER`)·명령 레지스트리 `_COMMANDS`·헬퍼(`_new_sid`/`_short_sid`/`_state_label`)·위젯(`PromptInput`/`QueueTable`). App/Mixin을 import하지 않아 **순환 차단**(위젯은 `self.app.<메서드>` 런타임 참조).
- **Mixin 3개:** `CommandsMixin`(tui_commands — 슬래시 팝업·↑↓ 히스토리·디스패치·명령 핸들러), `QueueMixin`(tui_queue — 큐 패널/조작/상세/검증패널/알림), `ChatMixin`(tui_chat — `/chat/stream` SSE 소비). 각 Mixin은 **자체 `__init__` 없음**(상태는 `AlphredTUI.__init__`에서 초기화), 교차 호출은 결합 클래스 MRO로 해결.
- **`tui.py`(1030→231줄):** `AlphredTUI(CommandsMixin, QueueMixin, ChatMixin, App)` 코어(수명주기·compose·렌더/스플래시·세션 상태)만. `run_tui` 유지.
- **Textual 주의:** 메시지 핸들러(`on_text_area_changed`)·`@work`(`send`)가 Mixin에 있어도 App MRO로 `getattr` 탐색됨 → 정상. `action_clear_chat`(BINDINGS)은 코어 잔류.
- **호출처:** 테스트 대부분 재수출로 무변경, `_state_label`만 `alphred.tui_base` 로 갱신(A와 동일 클린 방식).
- **검증:** import·순환·MRO OK(`AlphredTUI→CommandsMixin→QueueMixin→ChatMixin→App`), pyflakes 0, `test_tui.py` **25종 헤드리스**(마운트·팔레트·히스토리·큐키·세션·clear/new·슬래시·스킬·plan) + 전체 **212 pass**. 남은: **D**(횡단 정리 — 표현 상수 통합·지연 import 정리).

### 30.9 구조 리팩터 D — 횡단 정리 (색상 팔레트·지연 import)
- **D1 의미론적 색상 팔레트:** 상태색 4종(성공/정보/경고/오류)이 markup 문자열로 tui 4파일에 ~50곳 산재 → `tui_base` 에 `_OK/_INFO/_WARN/_ERR`(+`_SOFT`) 상수화. **안전 스크립트**로 `[...#HEX...]` markup 태그의 hex만 `{_CONST}` 로 치환 + 필요 시 f-prefix 부여(값-문자열 `"#HEX"`·CSS 배경·상수 정의는 미변경). 각 파일에 상수 import 추가, `cmd_plan` 의 dcolor 값-딕셔너리는 수동으로 `_OK/_INFO/_WARN/_SOFT` 로 정리. **검증:** 잔여 raw 상태-hex 0(정의+CSS 배경 제외), **비-f 문자열의 `{_CONST}` 0건**(스크립트로 확증 — 테스트가 못 잡는 미보간 버그 차단), 런타임 `_state_label("Completed")==[#7BC96F]완료[/]`.
- **D2 지연 import 정리:** 리팩터 모듈(server/deps·routes_openai·routes_models·tui_chat·tui_base·queue_manager·gateway)의 함수내 stdlib/httpx 지연 import(`json/os/subprocess/secrets/time/uuid/httpx`)를 모듈 상단으로 이동. **보존(의도적):** `cli.py` 의 무거운 모듈 지연 import(`serve/nlq/build_manager/cron_intercept` — 시작 지연·순환 회피)와 stdlib alias(`import json as _json`)는 유지.
- **D3 중복 점검:** `_state_label`(TUI 표시) vs `STATE_TO_RUN`(API 매핑)·`dashboard.py`(자립 HTML) → **목적·계층 상이, 실질 중복 없음**(확인만).
- **검증:** `alphred/` pyflakes 0, 전체 **212 pass**(회귀 0). **A~D 완료.**

## 31. `/model` 영구 설정 + 모델명 404 진단 (2026-07-01)

> 증상: 백그라운드 Heavy 작업이 전부 `Discarded`, 에러 `HTTP 404: 404 page not found`. TUI에서 `/model`로 모델을 바꿔도 재시작하면 gemma로 되돌아감.

### 31.1 진단 (리팩터 무관 — 모델명/설정 문제)
- 실패 run 덤프(`sessions/request_dump_*.json`): `model=llama-3.3-70b-instruct`(접두어 없음), `url=https://integrate.api.nvidia.com/v1/chat/completions`(정상), `NotFoundError 404 "404 page not found"`. 폐기 작업 모두 정상 `hermes_run_id` 보유 → **Alphred 큐→run 경로(리팩터 후)는 정상**, 404는 **NVIDIA NIM이 모델을 못 찾아** 반환한 것.
- **근본 원인:** NIM은 벤더 접두 id(`meta/llama-3.3-70b-instruct`)를 요구(잘 되던 base가 `google/gemma-4-31b-it`로 접두 있음). `/model high|mid`로 **접두어 없는** `llama-3.3-70b-instruct`를 tier에 저장 → §29.1 라우팅이 그대로 config.default에 적용 → NIM 404. (라우팅 로직은 정상, 사용자 입력 모델명 오류를 전파.)
- **"gemma로 되돌아감" 원인:** ① `/model <이름>`(bare)이 세션 전용이라 미영속 ② §29.1 apply_model이 매 작업 config.default를 tier 값으로 덮어씀(Light→low tier=gemma) → 사용자가 바꾼 값이 리셋.

### 31.2 수정
- **`Config.set_default_model(name)`**: config.yaml `model.default` + models.json `base` 설정 + **깊이별 tier(high/mid/low) 해제**(+구 `light` 키 정리) → `has_model_tiers()=False` → apply_model no-op → config.default 유지(재시작 후에도). 같은 provider 내 model-id 전환 전제.
- **`POST /models/default {model}`**(routes_models): `set_default_model` 호출 + 큐레이션 목록으로 `known`(오타/접두어 검증, 비차단) 반환.
- **TUI `/model <이름>`**: 세션 전용 → **영구 기본값 설정**으로 변경(엔드포인트 호출). "영구 설정(모든 작업·재시작 유지)" 메시지 + `known=False`면 "⚠ 목록에 없음 — 접두어(meta//google/) 확인" 경고. `_set_global_model`·`--global` 분기 제거(이제 기본 동작).
- **즉시 조치(A):** 실 config를 `set_default_model("meta/llama-3.3-70b-instruct")`로 교정(default+base 세팅, 깨진 tier·stale `light` 제거).
- **검증:** `test_set_default_model_persists_and_clears_tiers`·`test_models_default_endpoint` 추가, pyflakes 0, 전체 **214 pass**. README EN/KO(엔드포인트·§29.1·provider 접두 안내) 갱신.

## 32. LLM 스트림 타임아웃 완화 — "Request timed out." (2026-07-01)

> 증상: Heavy 작업이 `에러: Request timed out.` 로 자주 실패/재큐(`Pending→In-Progress→Paused→…` 반복).

### 32.1 진단
- `"Request timed out."` = **OpenAI SDK `APITimeoutError`**(Alphred 아님 — 실패 run 덤프 `type: APITimeoutError`). Hermes가 `meta/llama-3.3-70b-instruct`(70B) LLM 호출 시 타임아웃 → `api_max_retries:3` 소진(`max_retries_exhausted`) → run 실패 → Alphred가 transient로 재큐.
- **원인 knob:** Hermes `get_provider_request_timeout` 는 `providers.<id>.request_timeout_seconds` 없으면 스트림 읽기(토큰간) 타임아웃으로 `HERMES_STREAM_READ_TIMEOUT`(**기본 120s**)를 씀. config에 nvidia provider timeout 미설정 → 120s. 느린 free-tier 70B가 120s 안에 다음 토큰을 못 내면 httpx read timeout 발생.
- **성격:** Alphred 로직/리팩터 버그 아님 = **provider/모델 속도 문제**(free NIM 70B는 느리고 APIConnectionError도 잦음). 복잡 요청 품질 저하도 근본은 모델(70B < 프런티어).

### 32.2 수정(코어 무수정)
- `cfg.stream_read_timeout`(env `ALPHRED_STREAM_READ_TIMEOUT`, 기본 **600s**) 추가. `gateway._spawn_hermes_gateway` 가 **Alphred가 띄운 Hermes 게이트웨이 env 에 `HERMES_STREAM_READ_TIMEOUT` 주입**(PYTHONUTF8/YOLO 와 동일 지점). 전체 요청 상한(`HERMES_API_TIMEOUT`=1800s)은 그대로라 무한대기 아님. 데몬 재기동 시 반영.
- `test_stream_read_timeout_injected` 추가. README EN/KO 설정표 갱신.
- **한계(솔직):** 타임아웃 상향은 완주율을 높일 뿐 속도/품질을 근본 개선하진 않음 — Claude Code/Gemini 수준엔 빠르고 강한 모델(유료/프런티어)이 필요.

## 33. Heavy/Light 실시간 과정 스트리밍 (2026-07-01)

> 요청: Heavy 작업을 큐에서 선택하면 진행(생각·도구)을 실시간으로, Light도 과정 텍스트를 회색·최종은 흰색으로 — "일하는 걸 실시간으로 보는" UX.

### 33.1 아키텍처 (팬아웃 버스 — 필수)
- **실측:** Hermes `/v1/runs/{id}/events` 는 **단일 소비자**(`api_server._handle_run_events`가 run당 asyncio.Queue 하나를 drain, 구독 종료 시 큐 pop). 두 곳이 동시에 붙으면 서로 이벤트를 훔치고 한쪽 끊김이 전체를 깬다. → 백그라운드 진행추적기 `_track_run`(이미 이 SSE 소비)이 **유일 소비자**로서 파싱 이벤트를 인프로세스 버스로 publish, 게이트웨이 라이브 엔드포인트가 subscribe해 TUI로 팬아웃.
- **`eventbus.py RunEventBus`**: `subscribe/unsubscribe/publish/close`. 진행추적기=데몬 스레드, 구독자=asyncio 루프 → `loop.call_soon_threadsafe`로 스레드→루프 브리지. 구독자 없으면 publish는 no-op(비용 0).
- **배선:** `build_manager`가 `RunEventBus()` 생성→`QueueManager(event_bus=)`. `_track_run`이 매 이벤트 `publish(task_id, ev)` + 종료 시 `close`. 게이트웨이 `GET /queue/{id}/stream`이 subscribe→SSE 릴레이(실행 중 아니면 상태+`done`만; 종료 센티널/25s 유휴 시 상태 재확인으로 마감).

### 33.2 TUI (2A 라이브 뷰 + 2B 회색/흰색)
- **공용 렌더러 `_render_run_event`(tui_chat/ChatMixin):** 도구(`🔧/✓`)·생각(`💭 reasoning`)=**회색(dim)**, 오류=빨강, 최종 답변(`assistant.completed`)=**흰색(◆ Alphred)**. Light `_handle_event`가 `queued`만 특수처리 후 이 렌더러로 위임(중복 제거).
- **Heavy 라이브 뷰(tui_queue/QueueMixin):** 큐 패널에서 **실행 중 작업 Enter → `_start_live(tid)`** — `/queue/{id}/stream` SSE 소비 워커(`_live_run`). 도구/생각은 실시간 회색, 중간 어시스턴트 텍스트는 회색 블록으로 누적·마감, `done`에 최종 결과(흰색). **Esc**로 `stop_live`(워커 취소). 실행 중 아닌 작업 Enter는 기존 정적 상세뷰 유지.
- 이벤트 어휘 차이 흡수: `message.delta`/`assistant.delta`, `message.completed`/`assistant.completed`, `tool_name`/`tool` 등 동의어 처리.

### 33.3 검증
- `test_eventbus`(팬아웃·격리·정적 스트림 경로)·`test_tui`(회색/흰색 렌더러·라이브 시작/중단 무크래시) 추가. pyflakes 0, 전체 **219 pass**. README EN/KO(엔드포인트·아키텍처) 갱신.
- **알려진 한계:** 라이브 뷰 진입은 실행 중 작업 대상(늦게 붙으면 그 시점부터). 스케줄러 틱과 읽기 엔드포인트의 sqlite 동시 접근은 기존 구조의 잠재 이슈(본 작업 범위 밖). 데몬 재기동 시 §33 반영.

### 33.4 사고 과정(thinking) 렌더링 — 회색/흰색 정교화
> 피드백: 도구 사용만 보이고 "왜 그 도구를 썼는지" 사고가 안 보인다.
- **실측:** Hermes 세션 chat/stream 은 모델이 추론을 내면 `tool.progress {tool_name:"_thinking", delta:"<추론>"}` 로 방출(게이팅 없음). runs 이벤트는 `reasoning.available`. **TUI가 그 `delta`(추론 텍스트)를 버리고 상태바에 "_thinking"만** 띄우고 있었음.
- **수정:** `_render_run_event` 를 **사고/중간텍스트=회색, 최종답변=흰색** 모델로 재구성 — 사고(`_thinking`/`reasoning.available`)는 별도 `_think_buf`, 어시스턴트 텍스트는 `_proc_buf`. 도구가 시작되면 앞 텍스트는 '근거'였으므로 회색 확정(`flush-on-tool`), 턴 완료(`assistant.completed`)면 최종답변 흰색. **도구 없이 바로 답해도 사고=회색·답변=흰색** 분리. Light `_handle_event` 와 Heavy `_live_run` 이 이 공용 렌더러를 쓴다.
- **핵심 한계(모델 의존):** `llama-3.3-70b-instruct` 는 **추론 토큰을 아예 안 냄** → 보여줄 사고가 없음(그래서 도구만 보였던 것). 사용자가 원한 "사고 → 검증 → 답변" 흐름은 **추론 모델**(deepseek-v4-pro·nvidia nemotron `*-reasoning`·qwen3.5 thinking 등)이 내는 `reasoning.available`/`_thinking` 를 위 렌더러가 회색으로 표시할 때 나타난다. 렌더링은 고쳤고, 실제 사고 출력은 모델 선택의 문제.
- 검증: `test_tui_thinking_rendered_gray` 등, 전체 **220 pass**.

### 33.5 Light 하네스 갭 수정 — TUI 대화 세션 주입
> 문제: §29.2 Light 하네스가 `/v1/chat/completions`·`/v1/responses` 에만 적용되고, **TUI 대화(`/chat/stream` → Hermes 세션)** 엔 빠져 있어 약한 모델이 스티어링 없이 도구를 남용(사실 질문에 terminal 실행 등).
- **실측:** Hermes `POST /api/sessions` 가 본문 `system_prompt` 를 수용해 세션에 저장(api_server L1408), 이후 그 세션 모든 턴에 적용.
- **수정:** `routes_openai.chat_stream` 이 세션 생성 시 `sbody["system_prompt"] = deps.light_harness`(하네스 on 일 때) 주입 → 대화에도 "직답·과잉거절/도구남용 억제" 스티어링. opt-out(ALPHRED_LIGHT_HARNESS=0)이면 미주입.
- 검증: `test_light_harness_injected_into_chat_session`(세션 생성 본문에 system_prompt 확인), 전체 **221 pass**.

### 33.6 추론 모델 💭 배지 (/model 목록)
> 어떤 모델이 사고 토큰(§33.4 회색 💭)을 내는지 사용자가 구분할 방법이 없었다.
- **판별 소스:** Hermes `models_dev_cache.json`(models.dev 카탈로그, 모델별 `reasoning: bool`). **주의(교훈):** 카탈로그는 147개 프로바이더(리셀러 포함)를 담아 bare-이름 전역 매칭은 오염됨(리셀러가 같은 모델을 reasoning=True 로 표기 → 전부 💭 오탐). → **현재 provider 엔트리로 스코프 한정 + full id 정확 매칭**(`_reasoning_model_ids(hermes_home, provider)`, mtime+provider 캐시).
- **정정:** 이 카탈로그(nvidia 스코프) 기준 `google/gemma-4-31b-it` 는 **reasoning=True**(직전 분석에서 리셀러 오염 매칭으로 False 라 했던 것 정정). `meta/llama-3.3-70b-instruct`=False. NIM 121종 중 reasoning 19종(deepseek-v4·nemotron-3 계열·qwen3.5·kimi-k2.6·minimax-m3 등).
- **표면:** `/models/available` 응답에 `reasoning: [ids]`·`current_reasoning: bool` 추가(routes_models). TUI `/model` 무인자 — 현재 모델에 `💭 추론` 배지, 목록 각 모델 뒤 `💭`, 범례 표시.
- 검증: `test_reasoning_model_ids_provider_scoped`(스코프·오염무시·graceful), 라이브(TestClient+실캐시): NIM 120종 중 19종 💭, gemma-4 포함·llama-3.3 제외. 전체 **222 pass**.

---

## 34. 차세대 에이전트 소양 — "Conductor" 기획 (2026-07-02, 미구현)

> **목표:** gemma-4급(소형/무료) 모델을 쓰면서도 Claude Code·Codex·Gemini CLI·Antigravity가
> 보여주는 "에이전트 소양" 5가지 — ①간단한 입력에서 니즈 정확 파악 ②다양한 분야에 대한
> 정형화된 세부 계획 수립 ③부족한 정보를 묻는 질문 + 추천 답변 제시 ④가용 MCP/스킬의
> 정확한 실시간 인지 ⑤도구 실행 후 성공/실패 자가 판정 → 수정 → 재시도 — 를 갖추고,
> 상용 가능한 수준으로 가는 다음 단계 로드맵. **큰 틀(제어 구조) 전환을 포함한다.**

### 34.0 현재 위치 진단 — 5대 소양 갭 매트릭스

| # | 소양 | 상용 에이전트 방식 | Alphred 현재 (as-is) | 갭 |
|---|---|---|---|---|
| ① | 니즈 파악 | 메인 모델이 대화 맥락 포함 in-context로 의도 해석 | 정규식 3-tier 사전필터(`classifier.prefilter`, 한/영 키워드 하드코딩) + **모호할 때만** LLM 플래너. 대화 맥락 미사용(현재 메시지 텍스트만) | 키워드 취약("이거 마저 해줘" 해석 불가). **확신-Heavy일수록 계획 없이 진입하는 역설**(§19는 분류용이라 확신 케이스는 플래너 skip → plan=None → depth=mid) |
| ② | 정형화된 세부 계획 | plan → todo 상태 추적 → 단계별 실행 → 필요 시 replan | 1~7개 coarse subtask `{title,kind,effort,tools}`를 실행 입력에 **'제안 힌트'로만** 주입(`prompt._plan_hint`). 진행표시는 도구 호출 수 카운트(계획 단계와 무관) | 계획이 실행 가능한 객체가 아님. 수용기준·산출물 스펙·의존관계 없음. replan 개념 없음 |
| ③ | 질문 + 추천 답변 | 착수 전 결정적 모호성만 질문, 선택지+추천 제시(예: Claude Code AskUserQuestion) | **완전 부재.** 하네스가 "Never ask back" 강제(백그라운드 특성상 필요). 되물음은 사후 `result_needs_attention`으로 적발만 | 인테이크(착수 전) 단계 자체가 없음. 잘못된 가정으로 완주 → NeedsReview 낭비 |
| ④ | 가용 MCP/스킬 인지 | 툴 정의를 매 턴 실제 인벤토리에서 주입 | Hermes 시스템 프롬프트가 스킬 광고(이름+한줄), Alphred 하네스에 스킬 목록 **하드코딩**(`assets/system_prompt.md` — nano-pdf/powerpoint/…). 플래너의 `tools` 필드는 LLM 추측(실물 대조 없음) | 하드코딩 목록은 stale(§20.8에서 "PDF 생성 수단 물리적 부재"를 사후에야 발견). MCP 서버 목록 미인지. 계획-능력 불일치 감지 불가 |
| ⑤ | 도구 실행 후 자가 판정·수정·재시도 | 루프 안에서 도구 결과를 매 스텝 판정, 즉시 경로 수정 | §21 Tier0(파일 결정검증)+Tier2(judge, opt-in)+Tier3(자가치유 재큐) — **전부 작업 종료 후**. 실행 중엔 이벤트 관전만(`_track_run`) | 스텝 내 개입 불가. 실패 시 통째 재실행(비쌈). 도구 오류 루프/무진전 감지 없음 |

**종합:** 스케줄링(큐·선점·복구·검증 아웃루프)은 견고하나, "에이전트 소양"은 Hermes 블랙박스
안에 있어 Alphred가 관여하지 못한다. 소양 5개는 전부 **실행 루프에 대한 통제권** 문제로 수렴한다.

### 34.1 큰 틀 결정 — 제어 역전 (Alphred Conductor)

**대안 비교:**

| 옵션 | 내용 | 평가 |
|---|---|---|
| A. 현상 유지 + 하네스 보강 | 프롬프트만 강화 | 소형 모델은 긴 지시를 안정적으로 못 따름(§20.7 환각 사례). ③④⑤ 원천 불가 — **기각** |
| **B. 제어 역전 (권고)** | Alphred가 "무엇을"(의도→질문→계획→단계→검증→수정)을 소유, Hermes는 "어떻게"(도구 루프)를 담당하는 **executor**로 사용. 1 task = N개의 좁은 scope Hermes run | 코어 무수정 불변식 유지. 소양 5개 전부 Alphred 코드로 구현 가능. LLM 콜 증가는 단일슬롯이 자연 제한 |
| C. 자체 에이전트 루프 재작성 | Hermes 대체(툴 루프 직접 구현) | execute_code/terminal/web_search/skills/승인게이트가 이미 동작하는 자산. 수개월 재작업 + "래퍼" 정체성 폐기 — **기각** |

**결정 제안: B.** 큰 틀 전환의 본질은 *교체가 아니라 제어 역전* — 지금은 "Alphred가 표를 끊고
Hermes가 여행 전체를 알아서" 구조인데, 이를 "Alphred가 여정표를 만들고 구간마다 Hermes를
태우는" 구조로 뒤집는다. 기존 자산이 그대로 재사용된다:

- 선점/재개 프리미티브(Phase 0 stop/resume, T4-B conversation_history) → **스텝 경계 재개**로 승격
- eventbus(§33) → 실행 중 감시(watchdog) 입력
- verify.py 체커 → 스텝 단위 수용검사로 확장
- 큐/상태머신/단일슬롯 → 그대로 (스텝은 task 내부 구조)

**불변식(유지):** ①Hermes 코어 무수정 ②단일 Alphred 코어(별도 스케줄러/DB 금지) ③Light는
비오케스트레이션(지연 민감) ④신기능 전부 플래그 게이팅, 기본 경로 무변경 ⑤fail-open(보조
LLM 실패가 작업을 막지 않음).

### 34.2 Track A — 의도 파악: LLM-first IntentCard

**A1. IntentCard 단일 구조화 콜.** 정규식을 1차 판정자에서 **fast-path**(인사/상태조회/초단문
즉결 + LLM 불가 시 폴백)로 강등하고, 나머지는 LLM 1콜로 통합 판정:

```json
{"goal":"…한 문장 요약","domain":"coding|research|document|data|admin|chat|…",
 "deliverable":{"type":"file|answer|action","format":"pdf|md|…|null"},
 "kind":"light|heavy","priority":1-10,"depth":"low|mid|high",
 "missing_info":[{"what":"…","critical":true}],"confidence":0-100}
```

- 현재 분류(kind)+심화도(depth)+모호성(→Track C 입력)을 **콜 1회로 통합** — 지금은 분류 따로,
  플래너 따로, judge가 수용기준 재추론 따로(중복 비용).
- 소형 모델 전제: 좁은 단일 임무 + few-shot 2~3개 + `parse_json_object` 재사용 + 필드별
  정규화/기본값(기존 `parse_plan` 패턴). 실패 시 기존 prefilter 결과로 폴백(fail-open).
- 게이트: `ALPHRED_INTENT`(기본 off → M2에서 on 전환 검토). `tasks.intent` JSON 컬럼 추가.

**A2. 대화 맥락 반영.** TUI `/chat/stream`은 세션 최근 N턴(기존 conversation_history 추출
로직 재사용, 6개/800자컷)을 IntentCard 입력에 동봉 — "그거 이어서 해줘"류 해석.

**A3. 정확도 텔레메트리 + 골든셋.** 판정 로그(`intent_log` 테이블: 입력요약/판정/사용자
오버라이드 여부). 사용자의 `/depth`·`X-Alphred-Kind` 오버라이드 = 암묵 정답 라벨.
`tests/eval/intents.jsonl` 골든셋(한/영 50케이스+)으로 회귀 측정(Track F).

### 34.3 Track B — 정형화된 세부 계획: Plan v2 (실행 가능한 계획)

**B1. 스키마 승격.** subtask → **step** (실행·검증 가능한 단위):

```json
{"version":2,"dod":"전체 완료 정의(한 문장)",
 "steps":[{"id":"s1","goal":"…","tool_hint":"execute_code|skill:powerpoint|…",
   "needs":["s0"],"expected":{"type":"file|text|action","path_hint":"…","format":"pptx"},
   "accept":[{"check":"file|exit_code|content|judge_lite","arg":"…"}]}]}
```

- `expected`+`accept`가 §21 검증과 계획을 **한 몸으로** 만든다(지금은 judge가 수용기준을
  사후 재추론 — 계획 시점에 확정하는 게 정확하고 싸다).
- **모든 Heavy에 계획 생성**(현재는 모호 케이스만): 분류는 IntentCard가 하므로 플래너는
  분류 의무에서 해방 → 디스패치 직전(`_start`) 생성으로 이동 가능(큐 대기시간은 공짜,
  Pending 중 취소되면 콜 절약). 게이트: `ALPHRED_PLANNER` 유지(v2로 의미 확장).
- **능력 접지(grounding):** 플래너 프롬프트에 Track D 인벤토리를 동봉 — "가용 스킬/도구/
  라이브러리 중에서만 tool_hint를 골라라". 없는 능력 참조 시 계획 수리 1회 → 실패 시
  가용성 갭으로 표면화(D4).
- 저장: 기존 `tasks.plan` 컬럼 재사용(version 필드로 구분, v1 하위호환). replan 시 이력 보존
  (`plan_history` JSON 배열 or events 테이블 재사용).

**B2. TUI/대시보드 연동.** §19.7 체크리스트를 도구 카운트가 아닌 **실제 스텝 상태**에 바인딩
(E-track 오케스트레이션 시). `/plan` 드라이런 응답에 v2 계획+수용기준 표시.

### 34.4 Track C — 부족 정보 질문 + 추천 답변 (Intake Clarification)

**C1. 인테이크 게이트.** IntentCard의 `missing_info[critical=true]`가 있고 소스가 대화형
(TUI/chat)이면, 착수 전 질문 생성 LLM 1콜:

```json
{"questions":[{"q":"보고서 대상 독자는?","header":"독자",
   "options":[{"label":"경영진(요약 중심)","recommended":true},
              {"label":"실무자(상세 데이터)"},{"label":"외부 공개용"}],
   "why":"구성·톤 결정에 필요"}],"assumptions_if_silent":["경영진용으로 가정"]}
```

- **질문 ≤3개, 선택지 2~4개+추천 1개 표기, '기타(직접 입력)' 상시** — Claude Code
  AskUserQuestion 패턴. 추천 답변이 핵심 UX: 사용자는 Enter만 쳐도 진행된다.
- **새 상태 `AwaitingInput`**: Pending 앞단(비실행, `next_runnable` 제외, 스케줄 점유 없음).
  전이: `AwaitingInput→Pending(답변/타임아웃)|Discarded`. 타임아웃(기본 10분, config) 시
  `assumptions_if_silent`를 기록하고 자동 진행 — **백그라운드 자율성 유지**.
- API 표면: Heavy 202 응답에 `"status":"needs_input","questions":[…]` / `POST
  /queue/{id}/answers {"answers":[…]}` / 대시보드·TUI 질문 카드(번호 선택+직접입력).
- 답변은 `tasks.answers`에 저장되어 계획(B) 입력과 실행 프롬프트에 주입.
- 게이트: `ALPHRED_CLARIFY`(기본 off). 비대화형 소스(cron/api)는 질문 생략+가정 기록.

**C2. 가정 원장(assumptions ledger).** "Never ask back"은 유지하되, 실행이 채택한 가정을
구조화 기록(`tasks.assumptions`) → 상세뷰/완료보고에 "이 작업은 X를 가정했음" 표면화.
고위험 가정(critical인데 미답변)이 있으면 완료 시 ⚠ 배지(기존 needs_attention 채널 재사용).

**C3. 선호 기억.** 사용자의 반복 답변(예: "PDF는 항상 A4/국문")을 `ALPHRED_HOME/
preferences.md`에 축적, 인테이크 프롬프트에 동봉 → 같은 질문을 두 번 안 하게 됨.
(수동 편집 가능한 평문 파일 — system_prompt.md 외부화와 동일 철학.)

### 34.5 Track D — 가용 능력 레지스트리 (CapabilityRegistry)

**D1. `alphred/capabilities.py` 신설.** 스냅샷 수집(전부 무LLM·저비용):

| 소스 | 방법 | 산출 |
|---|---|---|
| 스킬 | Hermes `GET /v1/skills`(기존 프록시 재사용) | 설치 스킬 이름/설명/카테고리 |
| 툴셋 | Hermes `GET /v1/toolsets`(§20.7 실측 존재) | 활성 도구 목록(write_file 등) |
| MCP 서버 | `config.yaml` mcp 블록 파싱(읽기 전용) | 등록 서버/도구 |
| 코딩 에이전트 CLI | `agy/claude/codex/opencode --version` 프로브(타임아웃 2s) | 실행 가능 여부+버전 |
| 파이썬 라이브러리 | Hermes venv `python -c "import …"` 배치 프로브(reportlab/weasyprint/python-docx/openpyxl/matplotlib/pandas…) | 산출물 형식별 생성 능력 판정 |

- 캐시: `ALPHRED_HOME/capabilities.json` + 해시. 갱신 트리거 = 데몬 시작 / TTL(기본 1h) /
  설치류 작업 Completed 직후. 수집 실패 항목은 unknown으로(fail-open).
- **형식별 능력 매트릭스 파생**: "pdf 생성=weasyprint|reportlab 중 하나 필요" 같은 규칙표 →
  §20.8의 "PDF 수단 부재를 실패 후에야 발견" 문제를 **착수 전** 판정으로 전환.

**D2. 동적 하네스.** `assets/system_prompt.md`의 하드코딩 스킬 목록을 `{{CAPABILITIES}}`
마커로 교체 — 디스패치 시점에 레지스트리에서 생성한 실물 목록(스킬/CLI/라이브러리 가용
여부 포함)을 치환 주입. 사용자 편집본에 마커가 없으면 기존 그대로(하위호환).

**D3. 접지 소비자.** 플래너(B1)·failure_suggestion(결정적 "weasyprint 미설치 — 설치 후
weasyprint 경로 사용" 제안)·doctor(`능력` 섹션)·게이트웨이 `GET /capabilities`.

**D4. 갭 처리.** 계획이 요구하는 능력이 없으면: 대화형 → C-track 질문("설치하고 진행할까요?
[추천] / 다른 형식으로 / 취소"), 비대화형 → 설치 서브스텝을 계획 앞에 자동 삽입(설치류는
이미 Heavy 분류 존재) 후 가정 기록.

### 34.6 Track E — 단계 실행·검증·자가수정 (StepRunner)

**E1. 오케스트레이션 실행(depth=high 한정, `ALPHRED_ORCHESTRATE` 게이트).**
`queue_manager._start`가 high+plan v2 작업을 StepRunner 경로로 분기:

```
for step in plan.steps(topological):
    input = 스텝 하네스(경량) + step.goal + 이전 스텝 산출 요약 + 능력 힌트 + 사용자 답변/가정
    run   = client.start_run(input, session_id=task.session_id)   # 세션 연속성(기존 §21.12)
    wait  → verify_step(step.accept)                              # ↓E2
    실패  → 스텝 피드백 재시도(≤2) → 그래도 실패 → replan 1회 → NeedsReview
    성공  → steps 테이블 갱신 + plan_progress = 실제 스텝 진행
```

- mid/low는 기존 단발 경로 유지(콜 수 보호). high가 실패 재시도로 태우는 비용(현재: 통째
  재실행)과 비교하면 스텝 분할이 오히려 싸질 수 있음 — F-track에서 실측.
- DB: `steps` 테이블 신설(task_id, idx, goal, state, run_id, output_summary, verify_report,
  attempts) or `tasks.plan` 내 상태 인라인(초기엔 후자로 단순하게 — 마이그레이션 최소화).

**E2. 스텝 단위 수용검사.** verify.py 체커 확장(스키마 동일 `{check,target,ok,detail}`):
`exit_code`(terminal 결과), `content`(파일 내 정규식/필수 문자열), `url`(HEAD 200),
`judge_lite`(스텝 goal 대비 1문장 판정 — high에서만, 기존 judge 재사용 축소판). 기존
파일/형식 체커는 그대로 스텝에 적용. **작업 전체 judge(Tier2)는 최종 1회 유지**(스텝
검증이 통과한 작업만 도달 → judge 실패율 하락 = 재시도 비용 하락).

**E3. 실행 중 감시(watchdog).** eventbus 구독자로 상주: 동일 도구 연속 실패 ≥3 / N분
무이벤트 / 오류 문자열 루프 감지 → `stop_run`(기존 프리미티브) → 교정 피드백을 붙여
스텝 재개(conversation_history 재개 경로 재사용). — "잘못 가고 있는 실행을 중간에 세우고
고치는" 소양 ⑤의 핵심. 게이트: `ALPHRED_WATCHDOG`(기본 off→검증 후 on).

**E4. 스텝 경계 선점.** 오케스트레이션 작업의 선점/재개를 스텝 경계로 정렬 — 재개 시
전체 대화 재전송 대신 "완료 스텝 요약 + 현재 스텝부터" (재개 비용·컨텍스트 길이 절감,
소형 모델의 긴 컨텍스트 취약성 회피).

**E5. 예산 가드.** 작업당 LLM 콜/재시도 상한(`ALPHRED_TASK_BUDGET`, 기본 25콜), 초과 시
부분 성공 보고+NeedsReview(§21.4 서킷브레이커 항목의 구체화). doctor에 소모 통계.

### 34.7 Track F — 평가·회귀 체계

- **골든셋 3종**: ①의도 분류 50+(A3) ②질문 필요성 판정 30+(질문해야 할 입력 vs 하지 말아야
  할 입력 — 과잉 질문은 UX 죽임) ③계획 품질 루브릭 20+(스텝 수·수용기준 유무·능력 접지).
- **fake-upstream e2e**: 기존 `tests/test_integration.py` 패턴으로 인테이크→계획→스텝→검증
  →보고 전체 파이프라인을 LLM 0콜로 회귀(구조 검증). 라이브 평가는 소량·수동 트리거
  (무료 쿼터 보호 — 기존 관례 유지).
- **지표 정의**: 의도 정확도(오버라이드율 역산), NeedsReview율, 작업당 평균 콜 수,
  스텝 1회 통과율, 질문 정답률(사용자가 추천답변을 그대로 고른 비율). doctor `--json`에 노출.

### 34.8 Track G — 상용화 요건 (소양 외 필수 조건)

에이전트 소양과 별개로 "상용 가능"에 필요한 것들 — M6 이후 별도 트랙으로만 명시:

1. **멀티유저/인증**: 현재 단일 Bearer 키 → 사용자별 API 키·작업 소유권(tasks.owner)·큐 격리.
2. **관측성**: 구조화 로그(현재 산재된 logger → JSON 라인), 작업별 비용/토큰 계측, /metrics.
3. **보안 재검토**: `ALPHRED_AUTONOMOUS_EXEC`(YOLO) 기본 on은 개인용 가정 — 상용은 도구
   화이트리스트/샌드박스 정책 필요. safety.py 확장.
4. **배포**: 버전드 릴리스(PyPI), 업데이트 데몬화(기존 백로그), Hermes 버전 호환성 매트릭스.
5. **디바이스 연동**(기존 백로그): ESP32/안드로이드 샘플 클라이언트.

### 34.9 설계 원칙 — "약한 모델 전제" (전 트랙 공통)

Claude Code류는 프런티어 모델이 소양의 절반을 공짜로 준다. gemma-4급 전제의 Alphred는
**구조가 모델을 보완**해야 한다:

1. 파이프라인의 모든 LLM 콜은 **좁은 단일 임무 + 구조화 출력 + few-shot** (긴 만능 프롬프트 금지).
2. 판정·검증·게이팅은 **코드가 결정**(LLM은 재료만 제공) — plan_to_weight/Tier0 패턴 유지.
3. 모든 보조 콜 fail-open + 결정적 폴백(현행 관례 유지).
4. JSON 파싱은 `parse_json_object` + 필드 정규화 + 1회 재질의(repair) 표준화.
5. 검증은 싼 것부터: 결정적(무료) → judge_lite(1문장) → judge full(high 최종 1회).

### 34.10 로드맵 (의존관계 순)

| 마일스톤 | 내용 | 트랙 | 산출 게이트 | 예상 |
|---|---|---|---|---|
| **M1 기반** | CapabilityRegistry+동적 하네스+`/capabilities`+doctor 연동; IntentCard(플래그 off)+intent_log+골든셋 시드 | D1-D3, A1, A3 | 능력 스냅샷 실측 일치; 의도 골든셋 기준선 측정 | ~1주 |
| **M2 인테이크** | AwaitingInput 상태+질문 생성+추천답변 UI(TUI/대시보드/API)+타임아웃 가정 진행+가정 원장; IntentCard 대화맥락(A2) | C1-C2, A2 | QA-34.3~5 통과; 과잉질문율 골든셋 기준 이하 | ~1.5주 |
| **M3 계획 v2** | 스키마 승격+모든 Heavy 계획+능력 접지+계획 수리+/plan v2 | B1-B2, D4 | 계획이 실물 능력만 참조; v1 하위호환 | ~1주 |
| **M4 오케스트레이션** | StepRunner(high 한정)+스텝 수용검사+스텝 경계 선점/재개+예산 가드 | E1-E2, E4-E5 | QA-34.6~8; fake-upstream e2e 통과 | ~2주 |
| **M5 자가수정 심화** | watchdog 중간 개입+replan+선호 기억+평가 지표 완성 | E3, C3, F | 도구 오류 루프 실측 차단; 지표 doctor 노출 | ~1주 |
| **M6+ 상용화** | 멀티유저/관측성/보안/배포 | G | 별도 기획 | TBD |

원칙: 마일스톤마다 플래그 off로 머지 → QA 통과 후 개별 on 전환. M1·M2는 기존 경로에
영향 0(추가만). M4가 유일하게 실행 경로를 바꾸며 high+플래그로 이중 게이팅.

### 34.11 QA 인수 기준 (QA-34)

- **34.1** IntentCard: 골든셋 의도 정확도 ≥ 기존 prefilter 대비 +15%p, JSON 파싱 실패 시 100% prefilter 폴백.
- **34.2** 대화 맥락: "방금 그거 PDF로도 만들어줘"가 직전 작업 참조로 해석(Heavy+형식 인지).
- **34.3** 질문 UX: 결정적 정보 부재 시 ≤3질문·각 2~4선택지·추천 1개 표기·기타 입력 가능. TUI에서 Enter=추천 선택.
- **34.4** AwaitingInput: 스케줄러가 실행하지 않음, 타임아웃 시 가정 기록 후 자동 Pending, 답변은 실행 입력에 주입됨.
- **34.5** 과잉질문 방지: 골든셋 "질문 불필요" 케이스에서 질문율 ≤10%; cron/api 소스는 질문 0.
- **34.6** 능력 접지: 미설치 라이브러리를 참조하는 계획이 생성되지 않음(수리 or 갭 표면화); 하네스의 스킬 목록이 실제 `/v1/skills`와 일치.
- **34.7** StepRunner: 스텝 실패 시 해당 스텝만 재시도(전체 재실행 0회); 선점 후 재개가 완료 스텝을 다시 실행하지 않음.
- **34.8** watchdog: 인위적 도구 오류 루프(fake)에서 3회 내 stop+교정 재개 발동.
- **34.9** 예산: 상한 도달 시 부분 성공 보고+NeedsReview(무한 루프 불가).
- **34.10** 회귀: 전체 pytest 통과 유지, 플래그 전부 off 시 기존 222 테스트 무변경 통과.

### 34.12 리스크 & 대응

| 리스크 | 대응 |
|---|---|
| 소형 모델 JSON 불안정 → 파이프라인 곳곳 파싱 실패 | repair 1회 재질의 표준화 + 전 콜 fail-open + 결정적 폴백(34.9-③④). 파싱 실패율을 F-track 지표로 상시 계측 |
| LLM 콜 인플레이션(무료 쿼터) | 인테이크1+계획1+스텝N+최종judge1 = high 기준 ~(N+3)콜. mid/low는 기존 경로(콜 증가 0). 예산 가드(E5)+fast-path 정규식 유지. §32처럼 실측으로 조정 |
| 과잉 질문 → "간단한 입력" UX 파괴 | critical만 질문+3개 상한+비대화형 생략+과잉질문 골든셋(34.5). 추천답변 기본값으로 마찰 최소화 |
| 오케스트레이션 복잡도(§11 훅 retrofit 전철) | §11 실패 교훈 = *남의 루프에 끼어들기*였음. StepRunner는 자기 루프에서 기존 프리미티브(run/stop/resume/verify)만 조합 — 신규 결합점 없음. 단일코어 불변식 유지 |
| 스텝 분할이 오히려 품질 저하(맥락 단절) | 스텝 입력에 이전 산출 요약+세션 연속성(session_id) 유지. high 한정+플래그로 A/B 비교 후 확대 |
| Hermes 업데이트로 API 형태 변화 | 레지스트리/프로브는 전부 fail-open(unknown 처리). doctor가 호환성 즉시 표면화 |

### 34.13 M1 As-Built — CapabilityRegistry + IntentCard 기반 (2026-07-03 구현·검증)

**D1 — `alphred/capabilities.py` `CapabilityRegistry`.** 수집원 5종(전부 무LLM·섹션별 fail-open):
스킬(`GET /v1/skills`)·툴셋(`GET /v1/toolsets`, `HermesClient.toolsets()` 신설)·MCP(config.yaml
최상위 `mcp:`/`mcp_servers:` 라인 파싱, 인라인 `{}`·부재 graceful)·코딩 CLI(agy/claude/codex/
opencode `--version` 프로브 2s)·파이썬 라이브러리(Hermes venv `find_spec` 배치 프로브, 무임포트).
파생 `derive_formats()` = 형식별 생성능력 매트릭스(pdf→weasyprint|reportlab|fpdf, pptx→lib 또는
`skill:powerpoint` 등, 불가 시 `install` pip 이름 제시). 캐시 `ALPHRED_HOME/capabilities.json`
+TTL(`ALPHRED_CAPS_TTL` 기본 1h), `invalidate()`. **수집 실패 섹션은 직전 캐시 유지+`stale` 표시**
(:8642 일시 다운이 능력 정보를 지우지 않음). Config `caps`(`ALPHRED_CAPS` 기본 **ON**).

**D2 — 동적 하네스.** `assets/system_prompt.md` 의 하드코딩 스킬 목록 → `{{CAPABILITIES}}` 마커.
`prompt.render_capabilities()`: 마커를 실물 인벤토리로 치환, 인벤토리 없으면 정적 폴백(구 목록
상당+"미검증" 표시), **마커 없는 사용자 편집본은 무변경**(하위호환). `queue_manager._start` 가
`capabilities.harness_section()` 을 fail-open 으로 주입. **설치류 작업 Completed 직후
`invalidate()`** (classify_reason "install" 매칭) → 다음 스냅샷이 새 능력 반영.

**D3 — 소비자 배선.** 게이트웨이 `GET /capabilities`·`POST /capabilities/refresh`(routes_admin,
인증), lifespan 백그라운드 워밍업 스레드(첫 디스패치가 프로브 비용 안 물게). doctor "능력
레지스트리(§34.5)" 행(스킬/도구/CLI/라이브러리/MCP 카운트+생성가능/불가 형식).
`verify.failure_suggestion(report, verdict, formats=)` — 형식 불량 시 매트릭스 기반 결정적 힌트
("이 런타임에 pdf 생성 수단 없음 → `uv pip install weasyprint` 후 생성" / "설치된 reportlab 사용").

**A1 — IntentCard(`ALPHRED_INTENT` 기본 OFF).** `classifier.INTENT_INSTRUCTION`(few-shot 2,
영어)+`build_intent_prompt`(context 슬롯=M2 A2 예비)+`parse_intent`(kind 필수, priority/depth/
confidence clamp, missing_info≤5 정규화)+`intent_to_classification`(실시간 Light 하한 9).
`llm_calls.make_hermes_intent`. `_classify` 파이프라인 재편: **fast-path**(명시/상태조회/설치류
+아주 짧은 대화체) → **IntentCard**(그 외 전부 — 확신-Heavy 포함, §34.0 ① "확신-Heavy 무계획
진입 역설"의 판정측 해소) → 폴백(기존 플래너→LLM분류→보수 Heavy 그대로). `is_fastpath()`:
길이 기반 즉결(realtime ≤80/greeting ≤25)은 **≤12자 또는 Light 패턴만 신뢰** — 25자 한국어
문장("서버 로그 분석해서 장애 원인 보고서 만들어줘")이 Light 로 삼켜지던 실측 함정을 IntentCard
로 넘김. `Task.intent` 컬럼(자동 마이그레이션)+`submit(intent=)`+depth 우선순위 = 명시 오버라이드
> IntentCard depth > plan_to_depth. `classify_full` 5-tuple 로 확장(게이트웨이 3개 호출부 갱신),
`/plan` 드라이런·task_view 에 intent 노출. intent 캐시(재질의 방지).

**A3 — 텔레메트리+골든셋.** DB `intent_log` 테이블+`Store.log_intent`(모든 분류 판정: 엔진 라벨
explicit/fastpath/intent/planner/llm/prefilter·근거·confidence·프롬프트 200자)+`intent_stats()`.
골든셋 `tests/eval/intents.jsonl` **51케이스**(한/영, 정규식 함정 포함). `tests/test_intent_eval.py`:
**기준선(플래그 전부 off) = 94%**(미스 3: 긴 채팅 즉답 2, 25자 산출물 요청 1) — 회귀 하한 70%;
완벽 IntentCard 가정 시 배선 정확도 **100%**(fast-path 가 개선을 안 막음 검증, LLM 0콜).

**검증.** pytest **248종 전체 통과**(기존 222 무변경 + 신규 26: test_capabilities 12·test_intent 11·
test_intent_eval 3), pyflakes 클린(alphred/). **실환경 doctor 스모크**: 레지스트리가 실머신에서
CLI 2종·데이터 라이브러리 검출 + **pdf/docx/pptx/xlsx 생성 수단 부재를 착수 전 검출**(§20.8 에서
실패 후에야 알았던 갭의 사전 판정 실증). README EN/KO 갱신(설정표 3항목·API 표·분류 0단계·§26
하네스 동적화). **M1 게이트 충족**: 능력 스냅샷 실측 일치 ✓ / 의도 골든셋 기준선 측정(94%) ✓ /
플래그 off 시 기존 경로 무변경 ✓. **잔여 주의**: IntentCard 는 라이브 LLM 미검증(무료 쿼터 보호
— 활성화는 `ALPHRED_INTENT=1` 후 intent_log 로 실측 권장). 다음 = M2(인테이크 질문+AwaitingInput,
A2 대화맥락).

### 34.14 M2 As-Built — 인테이크 질문+추천답변 + AwaitingInput + 대화맥락 (2026-07-03 구현·검증)

**C2a — AwaitingInput 상태.** `TaskState.AWAITING_INPUT`(비최종, 전이 `→Pending|Discarded`),
`next_runnable` 은 상태 필터라 자연 제외(스케줄 비대상). Task 필드 4종
`questions/answers/assumptions/input_deadline`(dataclass 파생 자동 마이그레이션).
`tick()` 최상단 `_promote_awaiting()`: 마감 경과 시 `Pending` 승격(reason="input timeout —
proceeding on assumptions"). QUEUE.MD ❓ 아이콘+Active 분류.

**C1 — 질문 생성+게이트.** `classifier.CLARIFY_INSTRUCTION`/`build_clarify_prompt`(부족정보+
맥락 동봉)/`parse_clarify`(질문≤3·선택지 2~4·**추천 정확히 1개 강제**(무표기→첫 선택지,
중복→첫 표기만)·선택지<2 질문 폐기·가정 미제공 시 추천 선택지로 합성 — 무응답 진행 보장).
**게이트 `needs_clarification`(전부 결정적, QA-34.5)**: Heavy ∧ 대화형(tui/chat) ∧ IntentCard
critical 부족정보. 그 외(api/cron/subservice·Light·비critical·명시 오버라이드) 질문 0.
`llm_calls.make_hermes_clarify`. Config `clarify`(`ALPHRED_CLARIFY` 기본 OFF, **intent 필요** —
runtime 이 `cfg.clarify and cfg.intent` 일 때만 배선)·`clarify_timeout`(기본 600s).
`submit()`: 게이트 통과+질문 생성 성공 시 `AwaitingInput` 상태로 생성(질문/가정/마감 저장),
실패는 fail-open(질문 없이 Pending). **랭커는 AwaitingInput 제출 시 스킵** → 승격 시점
(answer/타임아웃 후 첫 재정렬 기회)에 동작. `answer(task_id, answers)`: 검증(404/409)→답변
저장→Pending 승격→랭커.

**C2b — API 표면+실행 주입.** `STATE_TO_RUN[AwaitingInput]="needs_input"`.
`/v1/chat/completions`(route_realtime)·`/v1/runs`·`/chat/stream` 3표면 모두 AwaitingInput 이면
`status:"needs_input"+questions+input_deadline+answer_endpoint`(SSE 는 `needs_input` 이벤트).
`POST /queue/{id}/answers`(400/404/409). task_view 에 questions/answers/assumptions/
input_deadline 노출. **실행 주입**: `prompt.intake_block()`(답변 있으면 "[사용자 확인 답변]"
Q→A 블록, 무응답이면 "[채택한 가정… 보고에 명시]" 블록; answers 는 문자열 리스트/
[{q,answer}]/dict 관용 수용)→`autonomous_input(intake=)`→`_start`. **가정 표면화**:
`_finalize_done` 이 무응답 작업의 가정을 `verify_report.assumptions` 로 기록.

**C2c — TUI 질문 카드+대시보드.** `/chat/stream` `needs_input` 수신 → **답변 모드**: 질문
1개씩 표시(헤더·선택지 번호·`✦ 추천` 표기), 입력창 border 가 "답변 i/n · 빈 Enter=추천 ·
번호/직접 입력 · Esc=가정 진행" 으로 전환. `submit_current` 가 답변 모드면 입력을
`answer_submit` 으로 소비(**빈 Enter=추천 채택**, 숫자=선택지, 텍스트=직접, `/`명령은 통과),
완료 시 `POST answers`. Esc(`PromptInput.on_key`)=보류(타임아웃 가정 진행 안내). 큐 표
`입력대기` 라벨+Active 포함, 상세뷰(Enter)에 질문/답변/가정 표시, 검증 패널에 `· 가정:` 행.
대시보드: AwaitingInput ACTIVE 포함+색상+`💬 답변` 버튼(질문별 prompt, 빈 값=추천/번호/직접).

**A2 — 대화맥락.** TUI `send()` 가 세션 최근 6턴(현재 메시지 제외·800자컷)을 `context` 로
동봉 → `/chat/stream`→`classify_full(context=)`→`_get_intent(prompt, context)`(캐시 키에 맥락
포함)→`build_intent_prompt` CONVERSATION CONTEXT 블록. `route_realtime` 은 body messages
이전 턴에서(`context_of`), `/v1/runs` 는 conversation_history 에서 도출. clarify 프롬프트에도
동일 맥락 전달.

**검증.** pytest **265종 전체 통과**(M1 248 + 신규 17: test_intake 14·test_clarify_eval 3),
pyflakes 클린. 과잉질문 골든셋 `tests/eval/clarify.jsonl` **34케이스**(질문 필요 13/불필요 21,
비대화형·Light·비critical 포함) — 결정적 게이트 전 케이스 일치, **should_ask=false 과잉질문율
0%·cron/api 질문 0**(QA-34.5 게이트 충족). QA-34.3(질문≤3·선택지 2~4·추천 1·직접 입력·TUI
Enter=추천) 파서·UI 구현, QA-34.4(비실행·타임아웃 가정 진행·답변 주입) 테스트 통과.
README EN/KO(설정 2항목·answers 엔드포인트·상태 다이어그램·분류 0단계) 갱신.
**잔여 주의**: clarify 라이브 LLM 미검증(critical 판정 정확도는 `ALPHRED_INTENT=1
ALPHRED_CLARIFY=1` 활성화 후 intent_log+골든셋 prompt 열로 실측); TUI 답변 모드는 헤드리스
스모크 미작성(수동 검증 권장); C3(선호 기억)은 M5 로 이월. 다음 = **M3**(Plan v2 스키마
승격+모든 Heavy 계획+능력 접지).

### 34.15 M3 As-Built — Plan v2(실행 가능한 계획) + 능력 접지 + 갭 처리 (2026-07-03 구현·검증)

**B1a — 스키마/파서/파생.** `PLANNER_V2_INSTRUCTION`(dod + steps{id,goal,tool_hint,needs,
expected{type,format,path_hint},accept[{check,arg}]}; tool_hint 어휘 = execute_code|terminal|
write_file|web_search|skill:<name>|cli:<agy…>|none; "없는 능력은 첫 스텝에서 설치" 규칙 내장).
`build_planner_v2_prompt` 가 CAPABILITY INVENTORY(콤팩트)+USER GOAL(IntentCard)+USER DECISIONS
(인테이크 답변/가정)+DRAFT(§19 v1 분해 재활용)를 동봉. `parse_plan_v2` 정규화: id 자동부여·
goal 필수·type/check 화이트리스트·실존 않는 needs 제거·**accept 미제공 시 expected 에서 파생**
(file→file 체크). `plan_to_depth`/`estimate_cost` v1/v2 겸용(`_plan_steps` 헬퍼),
`prompt._plan_hint` v2 렌더 = "[EXECUTION PLAN …]"+DoD+스텝별 produces/done-when/after
(v1 은 기존 '제안 힌트' 유지 — 하위호환).

**B1b+D4 — 능력 접지(전부 결정적, 무LLM).** `capabilities.planner_context(snapshot)`(스킬/
CLI/라이브러리/생성가능·불가 형식 콤팩트 텍스트), `plan_gaps(plan, snapshot)`(skill:X 미설치·
cli:X 부재·expected.format 생성불가 검출 — 계획 안에 해당 설치 스텝이 이미 있으면 갭 아님),
`apply_gap_fixes`(format 갭→**설치 스텝을 맨 앞 삽입**(terminal, exit_code=0 검증, 기존 무의존
스텝들이 설치를 needs 로 연결)·skill/cli 갭→해당 스텝 tool_hint 를 execute_code 로 강등·수리
내역 `plan.gaps` 표면화). §34.3 의 "LLM 수리 1회" 대신 **결정적 수리 채택**(§34.9 원칙 —
약한 모델 재질의보다 코드 수리가 신뢰·비용 우위; 의도적 설계 변경으로 기록).
**D4 대화형** = 인테이크에 통합: `classifier.format_gap_question(intent, formats)` — IntentCard
deliverable.format 이 생성불가면 **무LLM 결정적 질문**("[환경 설치] {lib} 설치 후 진행[추천]/
가능한 형식으로 대체/그대로 시도") + 무응답 가정 "설치 후 진행"(계획 접지의 자동 설치 스텝과
정합). `submit` 인테이크 분기 재구성: 형식갭 질문(무LLM)+critical clarify 질문(LLM)을 결합,
총 ≤3 상한. 비대화형은 종전대로 질문 0 + 계획 접지가 설치 스텝 처리.

**B1c — 디스패치 시점 계획.** `make_hermes_planner_v2`, `QueueManager(planner2=)`(runtime 이
`ALPHRED_PLANNER` 로 v1 과 함께 배선 — 새 플래그 없음). `_start`: Heavy ∧ planner2 ∧ 기존
계획이 v2 아님 → `_get_plan_v2`(스냅샷→planner_context→LLM 1콜→plan_gaps→apply_gap_fixes→
캐시(prompt+intake 키)) → task.plan 갱신 → `_plan_hint` 주입. **재개(Paused)/검증 재시도는
저장된 v2 재사용(재계획 0)**, 실패는 fail-open(계획 없이 실행). §19 v1 분해가 있으면 초안으로
전달(모호+intent-off 경로의 콜 재활용). `preview_plan()` + `/plan` 드라이런이 **실 디스패치와
동일 경로**(접지 포함)의 v2 를 반환. 알려진 한계: 계획 LLM 콜이 tick `_lock` 내 동기 실행
(judge V2 와 동일 클래스, §21 V3 한계 기록과 정합 — 비동기화는 후속).

**B2 — TUI 계획 뷰.** 상세뷰 체크리스트를 모듈 레벨 순수 함수 `plan_checklist_lines(plan,
prog, state)` 로 분리(v1 subtasks/v2 steps 겸용; v2 는 "실행 계획(v2)"+DoD+스텝별 도구/산출물/
확인 표시+`⚙ 접지` 수리 내역; 진행 마크는 §19.7 도구 카운트 휴리스틱 유지 — M4 StepRunner 가
실측으로 대체 예정).

**검증.** pytest **282종 전체 통과**(M2 265 + 신규 17 test_plan_v2: 파서 3·파생 3·접지 5·
디스패치 4·/plan 1·체크리스트 1), pyflakes 클린. **M3 게이트**: 계획이 실물 능력만 참조
(갭 검출→결정적 수리, 설치 스텝 존재 시 미중복) ✓ / v1 하위호환(v1 렌더·분류 경로·파생 함수
무변경 동작, 플래그 off 시 기존 265종 무변경) ✓. README EN/KO(ALPHRED_PLANNER 의미 확장·분류
§3 갱신). **잔여 주의**: Plan v2 라이브 LLM 미검증(소형 모델의 스키마 준수율은 활성화 후
실측 — parse 실패 시 fail-open 으로 안전); 계획 수리의 LLM 재질의 버전은 미구현(결정적 수리로
대체, 필요성 확인 시 후속); tick 내 동기 LLM 콜 누적(계획+judge — M4 에서 비동기화 검토).
다음 = **M4**(StepRunner: high 한정 스텝 실행+스텝 수용검사+스텝 경계 선점/재개+예산 가드).

### 34.16 M4 As-Built — StepRunner(스텝 실행·검증·재개·예산) (2026-07-03 구현·검증)

**E2 — 스텝 수용검사(`verify.verify_step`, 전부 결정적).** accept 체크 3종: `file`(절대/홈
경로면 존재·비어있지않음·형식 시그니처, 상대/부재면 출력이 주장한 파일들 검사(§21 Tier0
재사용), 주장도 없으면 "경로 미보고" 실패), `content`(요구 문구 포함), `exit_code`(출력 꼬리
실패 마커 휴리스틱 — **종료코드 직접 관측 불가 한계를 detail 에 명시**). 반환에 `feedback`
(실패 항목별 보완 지시 — 스텝 재시도 입력에 주입). accept 없으면 통과("검사 생략" 명시).

**E1 — StepRunner.** `prompt.step_input` = 경량 스텝 프리앰블(§26 전체 하네스 대신 하드 규칙
압축 — 되묻기 금지·실물 산출·검증·경로 보고) + CAPABILITIES + 전체 요청/DoD + 인테이크 +
**완료 스텝 요약**(output 400자컷 — 스텝 간 맥락) + 현재 스텝(goal/tool_hint/expected/
Done-when) + 실패 피드백. `QueueManager(orchestrate/task_budget/step_retries)`:
`_is_orchestrated` = orchestrate ∧ heavy ∧ **depth=high** ∧ Plan v2. `_start` 가 스텝 경로 분기
→ `_start_step`(예산 가드→`_next_step`(needs 충족 첫 미완료, 교착 시 첫 미완료 폴백)→좁은
입력→run, 스텝 상태/runs_used 는 **plan JSON 인라인**(§34.6 결정 — 별도 테이블 없음),
plan_progress=실측 done 수, 세션 연속(session_id 공유)). `_finalize_active`/`recover` 분기 →
`_finalize_step`: verify_step 통과 → done+output 저장 → 다음 스텝 **같은 틱 연속 시작**;
전체 완료 → 기존 `_finalize_done`(Tier0/judge/MoA 그대로); 실패 → `attempts+1`+feedback 저장
→ **그 스텝만 재시도**(≤step_retries) → 소진 시 부분성공 NeedsReview(`steps_done/steps_total`
보고). 진행 트래커는 `update_progress=False` 로 기동(§19.7 도구 카운트가 실측 스텝 진행을
덮지 않음 — 라이브 팬아웃·활동 표시는 유지).

**E4 — 스텝 경계 재개.** 선점(`_preempt` 무수정)·transient 재큐 후 재개 → `_start`→orchestrated
→`_next_step` 이 done 스텝을 건너뛰고 중단된 스텝부터 재시작. **conversation_history 재전송
불필요**(완료 스텝 요약+Hermes 세션이 맥락 계승) — T4-B 대비 재개 비용·컨텍스트 길이 절감.

**E5 — 예산 가드.** `plan.runs_used` 가 run 시작마다 증가, `task_budget`(기본 25) 도달 시
`_budget_exhausted` → 부분성공 NeedsReview(마지막 완료 스텝 출력 + steps_done/total). §21.4
서킷브레이커 항목의 오케스트레이션 구현. **fix 스텝**: 전체 judge/Tier0 실패로 Tier3 재큐된
오케스트레이션 작업은 재개 시 전체 재실행 대신 `fix{n}` 스텝(피드백 내장)을 추가 실행,
verify_feedback 은 소진 처리(무한 fix 방지 — 전체 judge 예산 judge_max_retries 는 그대로).

**표면.** TUI 체크리스트가 스텝 `state` 실측 마크(✓/▶/○) 사용(휴리스틱은 state 없을 때 폴백),
run 사용량·스텝 재시도 횟수 표시. doctor `StepRunner(ALPHRED_ORCHESTRATE)` 행. Config 3종
`ALPHRED_ORCHESTRATE`(기본 OFF)/`ALPHRED_TASK_BUDGET`(25)/`ALPHRED_STEP_RETRIES`(2)+runtime 배선.

**검증.** pytest **294종 전체 통과**(M3 282 + 신규 12 test_steprunner: verify_step 4·step_input
1·e2e 7 — 2스텝 완주(세션 연속·실측 진행)·스텝만 재시도(QA-34.7 전체 재실행 0)·재시도 소진
부분성공·예산 초과(QA-34.9)·선점 후 완료 스텝 미재실행(QA-34.7)·fix 스텝 정합·플래그 off/mid
단발 경로 무변경), pyflakes 클린. **잔여 주의**: exit_code 검사는 휴리스틱(Hermes run 출력
텍스트만 관측 가능 — 도구 종료코드 직접 수신은 이벤트 스트림 확장 필요, 후속); watchdog
(E3 실행 중 개입)·replan·선호기억(C3)은 M5; 스텝 run 도 tick lock 내 동기 시작(기존 한계
클래스, 스텝 자체는 비동기 폴링이라 lock 점유 짧음); 라이브 LLM 오케스트레이션 미실측
(`ALPHRED_ORCHESTRATE=1 ALPHRED_PLANNER=1` + high 작업으로 실측 권장 — 스텝 분할 품질/비용
A/B 는 §34.12 리스크 항목). 다음 = **M5**(watchdog 중간 개입 E3, replan, 선호 기억 C3, 평가
지표 완성 F).

### 34.17 M5 As-Built — watchdog·replan·선호 기억·평가 지표 (2026-07-03 구현·검증)

**E3 — watchdog(`ALPHRED_WATCHDOG` 기본 OFF).** 신호 수집은 트래커 스레드, **개입은 tick
스케줄러**(스레드 경계 분리 — 상태 전이는 스케줄러만): ①연속 도구 실패 — `_track_run` 이
`tool.failed` 연속 카운트(성공 시 리셋), `tool_fail_limit`(기본 3) 도달 시 `_flag_runaway`
②무진전 — 매 이벤트 시각(`_last_event`, 트래커 가동 시) 또는 DB `updated_at`(미가동 근사)이
`stall_seconds`(기본 600) 경과. `_watchdog_check`(tick)→`_watchdog_intervene`: stop_run →
Paused(백오프)+`retries+1`+**교정 힌트**("같은 접근 반복 금지 — 원인 진단 후 다른 도구/
라이브러리로"; 단발=verify_feedback 슬롯, 오케스트레이션=현재 스텝 feedback) → 재개 시 주입.
반복 개입은 `max_retries` 상한 → NeedsReview. 종료 작업의 잔여 신호는 체크 시 정리.

**Replan(1회).** `_finalize_step` 스텝 재시도 소진 분기 → `_replan`: 완료 스텝 요약+실패
스텝/검증 피드백을 REPLAN 컨텍스트로 planner2 에 전달(`build_planner_v2_prompt(replan=)`
신설), "남은 작업만·실패 부분은 다른 접근" 지시, 접지(갭 검출/수리) 재적용,
**runs_used 예산 승계**(E5 상한 유지), `replanned` 플래그로 1회 한정, 구 계획은
`previous_steps` 이력 보존(§34.3). 실패/불가 시 기존 부분성공 NeedsReview 경로.

**C3 — 선호 기억.** `ALPHRED_HOME/preferences.md`(수동 편집 가능 평문 —
`prompt.append_preference/load_preferences`). `answer()` 가 인테이크 답변을 날짜+Q→A 로 축적.
주입 2곳: ①clarify 프롬프트 "KNOWN USER PREFERENCES (do NOT ask again)"(구형 3-인자 콜러블은
TypeError 폴백 하위호환) ②실행 입력 "[USER PREFERENCES — apply when relevant]"(`_run_context`
에 통합 — _start 의 인라인 caps/intake 조립도 이 헬퍼로 단일화). 한계 명시: 작업 특정적
답변도 섞일 수 있어 "관련 있을 때만 적용" 문구+사용자 파일 정리 전제.

**F — 평가 지표(`cli._collect_metrics`, 무LLM 순수 함수 → doctor "지표(§34.7)" 행 + `--json`
`metrics`).** intent_log 기반 분류 건수·명시 오버라이드 비율(암묵 정답 신호), NeedsReview율,
질문율·**추천 답변 채택률**(answers ↔ recommended 라벨 대조), 오케스트레이션 작업 수·평균
run·**스텝 1회 통과율**(attempts=0 비율). doctor 에 watchdog 행 추가.

**검증.** pytest **306종 전체 통과**(M4 294 + 신규 12 test_watchdog_m5: watchdog 6(폭주 개입/
교정 힌트 재주입/무진전 감지/off 무개입/반복 상한 NeedsReview(QA-34.8)/오케스트레이션 스텝
피드백)·replan 2(1회 재계획+이력·예산 승계, 재계획 후에도 실패 시 종료)·prefs 3·지표 1),
pyflakes 클린. 기존 test_steprunner 1건은 replan 도입 반영해 조정(재계획 불가 조건 명시).
**M5 게이트**: 도구 오류 루프 차단(fake 실측) ✓ / 지표 doctor 노출 ✓. **잔여 주의**: 연속
도구 실패 감지는 실 Hermes 이벤트 스트림에서만 동작(트래커 필요 — fake/테스트는 flag 직접
호출로 검증); 무진전 임계 600s 는 느린 무료 모델의 장문 생성과 겹칠 수 있어 보수적으로 유지
(§32 스트림 타임아웃 600s 와 정합); watchdog·replan 라이브 LLM 미실측. **§34 로드맵 M1~M5
완료** — 남은 트랙 = M6+(상용화 G: 멀티유저/관측성/보안/배포 — 별도 기획), 라이브 실측
과제(IntentCard/clarify/Plan v2/오케스트레이션 A/B — intent_log·지표로 측정 가능).

### 34.18 라이브 실측 — M1~M5 전체 파이프라인 (2026-07-03, 실 LLM)

**환경.** `ALPHRED_INTENT=1 CLARIFY=1 PLANNER=1 ORCHESTRATE=1 WATCHDOG=1` + `alphred serve`
(Hermes :8642 자동 기동). **모델 사건**: 기본 `meta/llama-3.3-70b-instruct`(NIM)가 제공자측
포화로 완전 불능 — `ResourceExhausted: Worker local total request limit reached (55/16)` +
"Stream stale for 180s(첫 청크 없음)" 반복, 인사 1건이 180s 타임아웃. →
`POST /models/default` 로 **`google/gemma-4-31b-it`**(NIM, 추론 모델, §33.6 💭) 영구 전환 후
전부 정상. 사용자 비전("gemma-4급 소형 모델")과 정합.

**실측 결과(전부 통과).**
| 단계 | 결과 |
|---|---|
| Light fast-path | "안녕!" → 10.8s 즉답(LLM 보조콜 0 — fastpath 엔진 로그 확인) |
| 인테이크(모호 Heavy) | "보고서 하나 만들어서 파일로 저장해줘"(tui) → 31.4s에 `202 needs_input`. IntentCard: heavy·mid·critical("주제")+비critical("형식") 정확 판정(confidence 90). clarify: 질문 2개("주제" ✦최근 작업 요약 / "형식" ✦Markdown) — **스키마 완벽 준수, 파싱 실패 0** |
| 답변→실행 | `POST answers` → Pending 승격 → Plan v2 3스텝 힌트 주입(단발/mid 경로) → web_search→web_extract→write_file→**read_file(자가 검증)** → 2분 완주. `python_3_13_features.md` 실존(2.8KB, 내용 정확 — free-threading/JIT), Tier0 "산출물 1/1 통과" |
| 선호 축적(C3) | preferences.md 에 주제/형식 답변 2건 자동 기록 확인 |
| 과잉질문 방지 | 구체적 요청(경로·형식 명시, depth=high 헤더) → **질문 없이** 11.3s 큐 등록(QA-34.5) |
| **오케스트레이션(high)** | Plan v2 **2스텝**(내용 구성→파일 저장) 생성 → 스텝별 run(runs 1→2, 실측 진행 ▶○→✓▶→✓✓, plan_activity="스텝: …") → 90초 완주. `pathlib_vs_ospath.md` 실존(표 포함, 요구 반영), **스텝 1회 통과율 100%** |
| 지표(F) | doctor: classified 4(fastpath light 2·intent heavy 2), NeedsReview율 0%, 질문율 14%, 추천 채택률 50%(형식=추천 채택·주제=직접 입력 — 정확), orchestrated 1·평균 2.0run·스텝 1회 통과 100% |

**관찰/교훈.** ①gemma-4-31b-it 가 IntentCard/clarify/Plan v2 JSON 스키마를 전부 준수 —
"약한 모델 전제" 설계(좁은 임무+정규화+fail-open)가 라이브에서 성립. ②NIM 70B 포화는
제공자측 문제(§32 와 동일 클래스) — 모델 전환이 즉효, doctor 가 원인 표면화. ③clarify 가
"직접 입력"을 선택지 label 로 내기도 함(무해 — 자유 입력은 어차피 지원). ④모호한 보고서
요청을 IntentCard 가 mid 로 판정 → 오케스트레이션은 high 판정/오버라이드 시 동작(설계 의도).
⑤Hermes auxiliary(openrouter/nous) 결제 경고 소음은 무관(메인 경로 영향 없음). **미실측
잔여**: watchdog 실개입(폭주 상황 미발생 — fake 검증만), replan 라이브, 인테이크 타임아웃
자동 진행 라이브(120s 설정했으나 답변으로 소진), NeedsReview/부분성공 라이브 케이스.

---

## 35. M6 — 상용화(Track G) 상세 기획 (2026-07-03, 미구현)

> §34 로 "에이전트 소양"은 갖췄다(라이브 검증 §34.18). M6 는 그것을 **남에게 줄 수 있는
> 제품**으로 만드는 트랙이다: 안전한 기본값, 관측 가능성, 설치·업데이트, 다중 디바이스.
> 기능 추가가 아니라 **신뢰·운영성** 트랙이므로, 각 항목의 인수 기준은 "동작한다"가 아니라
> "남이 문서만 보고 15분 안에 깔고, 1주일 무인 가동하고, 사고 시 원인을 스스로 찾는다"이다.

### 35.0 G0 — 상용 형태 결정 (선행, 모든 범위를 좌우)

| 형태 | 내용 | 현 구조와의 정합 |
|---|---|---|
| **A. 개인용 셀프호스트 (권고 1차)** | 사용자 1명이 자기 머신/서버에 설치, 여러 디바이스(TUI·웹·모바일·ESP32)로 접속 | **현 구조 그대로 상품화 가능.** 단일슬롯 스케줄러·config.yaml 모델 스왑(§29.1)·Hermes home 공유·SQLite 전부 1-사용자 전제와 정합. 원 비전("클라우드·다중 디바이스 가정")과 일치 |
| B. 팀 서버 (2차, 수요 확인 후) | 하나의 배포가 여러 사용자를 서빙 | **재설계 필요 3종**(§35.1-B): 단일슬롯→공정 스케줄링, 전역 config 모델 스왑→요청별 모델(Hermes per-run model 지원 여부 조사 필요), Hermes 세션 네임스페이스 격리 |
| C. 관리형 SaaS (3차) | 호스팅+과금 | B 완성 후 별도 기획(과금·테넌트·컴플라이언스) |

**결정 제안: A 를 M6 범위로 확정**, B 는 M6d(조건부)로 명시만. **라이선스 실측**: Hermes =
MIT(Nous Research) — 상용 래핑·재배포 법적 문제 없음(저작권 고지 포함 의무만; NOTICE 파일로
처리). Alphred 자체 라이선스 선택(MIT 권고)과 상표("Alphred") 확인은 비개발 항목으로 기록.

**"상용 가능"의 정의(인수 기준의 기준):** ①문서만으로 15분 내 설치·온보딩 ②기본값이 안전
(외부 노출·파괴적 도구·무인증 없음) ③1주 무인 가동(크래시·누수·무한루프 없음 — 안전망 실측)
④사고 시 doctor/로그만으로 원인 특정 ⑤업데이트가 데이터를 깨지 않음(마이그레이션 보장).

### 35.1 G1 — 접근 제어 (A단계: 1사용자·다중 디바이스)

- **클라이언트 키 관리**: 현재 단일 `ALPHRED_API_KEY` 전부 아니면 전무 → `alphred keys
  issue <이름> [--scope read|control]` / `keys list` / `keys revoke`. 저장:
  `alphred_home/clients.json`(키 해시만 저장 — 평문 미보관). `read` 스코프 = GET /queue·
  /metrics·대시보드(모니터링 디바이스용), `control` = 전부. 인증 미들웨어가 스코프 검사
  (`make_auth` 확장). 마이그레이션: 기존 단일 키는 control 스코프로 자동 인정(하위호환).
- **대시보드 인증 마감**: 현재 페이지 자체 무인증(§Phase4) — 키 입력 화면+세션 스토리지
  유지하되, **키 없이는 데이터 API 가 전부 401**임을 보장(현행 유지)+ 로그인 UX 정리.
- **바인딩 기본값 변경(파괴적 변경 — 주의 문서화)**: `serve --host` 기본 `0.0.0.0` →
  **`127.0.0.1`**. 외부 노출은 명시 옵트인(`--host 0.0.0.0` + 키 필수 강제: 외부 바인딩인데
  키 미설정이면 기동 거부). LAN/원격은 리버스 프록시(TLS) 가이드 문서 제공(내장 TLS 는
  비범위 — 인증서 관리 복잡도 대비 가치 낮음).
- **B단계(팀 서버, M6d 조건부)**: `tasks.owner`(키→사용자 매핑), 큐 조회/조작 소유자 격리,
  사용자별 우선순위 밴드·rate limit·run 예산, 관리자 스코프. **선행 조사 3건**: Hermes
  per-run model 파라미터(전역 config 스왑 대체 가능?), 세션 id 네임스페이스 충돌, 다중 슬롯
  시 선점 의미론(사용자 간 선점 금지 정책).

### 35.2 G2 — 관측성

- **구조화 로그**: `ALPHRED_LOG_JSON=1` 시 JSON lines(ts/level/logger/task_id/msg) —
  logging.Formatter 교체만(의존성 0). `task_id` 상관관계: 큐 경로 로그에 이미 id[:8] 관례 →
  extra 필드로 승격. 파일 회전: `alphred_home/logs/alphred.log`(RotatingFileHandler, 10MB×5)
  — serve.log/hermes.log 산재 현황 정리(위치 통일 + doctor 가 경로 안내).
- **토큰/비용 계측**: **조사 선행** — Hermes chat/completions 응답 `usage` 필드·runs 결과의
  토큰 정보 유무 실측. 있으면 `tasks.tokens_in/out` 컬럼(자동 마이그레이션)+보조콜(intent/
  clarify/plan/judge) 사용량 합산 → doctor 지표·작업 상세뷰에 "이 작업 비용" 표시, E5 예산을
  run 수 → 토큰 기반으로 승격 옵션. 없으면 run 수 근사 유지(솔직 표기).
- **`GET /metrics`(Prometheus 텍스트 포맷, 수동 직렬화 — 의존성 0)**: 큐 깊이(상태별),
  완료/실패/NeedsReview 카운터, 작업 처리시간 버킷, watchdog 개입 수, 인테이크 질문/채택
  카운터, LLM transient 오류 카운터. §34.7 지표와 동일 소스(doctor 와 이원화 방지 — 공용
  수집 함수 재사용).
- **알림(webhook)**: 미소비 상태인 `Task.delivery` JSON 활용 — `{"webhook": "https://…"}`
  이면 종결 전이(Completed/NeedsReview/Discarded) 시 task_view POST(재시도 1회, fail-open).
  submit API body `delivery` 로 지정, TUI/대시보드는 전역 기본 webhook 설정(config).
  텔레그램/디스코드 등 채널은 webhook 뒤에 두는 것으로 비범위(Hermes gateway 의 telegram
  플랫폼 재활용은 조사 항목으로만).
- **보존 정책**: task_events·intent_log 무한 증가 → `alphred queue clear` 확장(N일 초과
  종결 작업 자동 정리 옵션 `ALPHRED_RETENTION_DAYS`, 기본 무제한=현행).

### 35.3 G3 — 보안 기본값

- **도구 정책 3단계**(`ALPHRED_EXEC_POLICY=safe|standard|yolo`): 현재
  `ALPHRED_AUTONOMOUS_EXEC`(YOLO 주입)는 개인 로컬 전제. `safe`=파일 생성/읽기·웹 검색만
  (셸/설치 차단), `standard`(신규 기본)=+패키지 설치·일반 셸(파괴 패턴 차단), `yolo`=현행.
  **구현 경로 조사 2안**: ①Hermes `pre_tool_call` 훅(§11.12 에서 block 동작 실측 완료 —
  Alphred 가 스폰하는 :8642 에만 훅 설치, 코어 무수정·순정 `hermes` 무영향) ②Hermes
  toolsets 구성으로 도구 자체 제한. ①이 세밀(명령 인자 검사 가능), ②가 단순 — 착수 시 실측
  후 택1. `safety.scan_payload` 확장: 파괴 패턴(rm -rf /, format, reg delete HKLM 등)·자격
  증명 유출 패턴(.env/.ssh 읽어 외부 전송) 사전 차단 목록.
- **시크릿 위생**: `upstream.key`/`clients.json` 파일 권한(Windows icacls·POSIX 600),
  로그 마스킹(Authorization 헤더·키 문자열), `ALPHRED_LOG_PROMPTS=0` 옵션(프롬프트 본문
  로그 제외 — PII 배려).
- **입력 상한**: 프롬프트 최대 길이(기본 100KB)·answers 항목 수 상한 — 폭주 방지.
- **의존성**: 버전 하한만 있는 현 pyproject → lock 파일(uv lock) 동봉 + CI 에서 검증.

### 35.4 G4 — 배포·설치·업데이트

- **패키징**: PyPI 배포(이름 `alphred` 가용성 확인 — 선점 시 `alphred-agent` 폴백), pipx
  권장 설치 경로 문서화(현 install.ps1/sh 는 유지하되 pipx 우선). 시맨틱 버저닝(0.x →
  1.0 은 M6 완료 시), CHANGELOG.md, GitHub Release+태그.
- **Hermes 호환성 매트릭스**: 현재 v0.16.0 실측 고정 → `hermes version` 파싱해 doctor 에
  표시+지원 범위 밖이면 경고. **`alphred doctor --deep`**: poc/verify_primitives 를 재활용한
  라이브 스모크(runs/stop/재개/세션 5종, 옵트인·LLM 소량) — 새 Hermes 버전 나올 때 호환성
  15분 검증 절차로 문서화.
- **서비스화**: `alphred service install|uninstall|status` — Windows=작업 스케줄러(로그온 시
  기동, schtasks) / Linux=systemd 유닛 생성 / macOS=launchd plist. 데몬 헬스는 기존 안전망
  (RestartGuard) 재사용.
- **업데이트**: `alphred update`(pip 자기 업그레이드+재기동 안내+DB 자동 마이그레이션은
  기존 dataclass 파생 구조로 보장— 다운그레이드 비지원 명시). Hermes 업데이트는 안내만
  (기존 #1 보류 결정 유지 — 코어 무수정 원칙).
- **프로파일(플래그 UX)**: env 5~10개 조합은 제품 UX 로 부적합 → `ALPHRED_PROFILE=
  basic|smart|full` 프리셋(basic=현 기본값 / smart=+intent+planner / full=+clarify+
  orchestrate+watchdog). 개별 env 는 프리셋을 덮어쓰는 오버라이드로 유지(하위호환).
  §34.18 라이브 결과에 근거해 **1.0 의 권장 기본 = smart** 제안(질문 UX 는 옵트인 유지).
- **온보딩 마법사**: `alphred setup` 확장 — Hermes 온보딩 후 ①모델 선택(provider 카탈로그
  +§34.18 교훈 "포화 시 대체 모델" 안내) ②프로파일 선택 ③(외부 노출 시) 키 발급 ④doctor
  자동 실행. 목표: 문서 없이 15분.

### 35.5 G5 — 클라이언트/디바이스

- **웹 대시보드 v2**: 질문 카드 정식 UI(현 window.prompt 임시 → 인라인 폼+추천 하이라이트),
  Plan v2 체크리스트(스텝 상태 ✓/▶/○)·가정·검증 패널 표시, §33 SSE 라이브 뷰(실행 중 작업
  클릭 시 스트림) — 단일 HTML 원칙 유지(의존성 0).
- **모바일/서드파티**: OpenAI 호환이라 기존 클라이언트 앱 연결 가능 — "지원 클라이언트"
  문서(베이스 URL+키 설정 스크린샷), needs_input 은 OpenAI 비표준이므로 대시보드/TUI 로
  답하는 흐름 안내(또는 클라이언트가 202 를 그대로 표시).
- **ESP32/음성(기존 백로그)**: 샘플 펌웨어 1종(푸시투토크→/v1/chat/completions→TTS는
  디바이스측) — M6 범위에선 문서+회로 없는 최소 예제만, 실기기 e2e 는 후속.

### 35.6 QA-35 인수 기준

- **35.1** 신규 머신에서 문서만으로 pipx 설치→setup→첫 작업 완주까지 15분 이내(실측 1회).
- **35.2** 기본 기동이 127.0.0.1 바인딩·외부 바인딩+무키 조합은 기동 거부.
- **35.3** read 스코프 키로 제출/폐기 API 호출 시 403; revoke 즉시 401.
- **35.4** `ALPHRED_EXEC_POLICY=safe` 에서 셸 파괴 명령이 실행 전 차단되고 작업은
  NeedsReview 로 사유 표면화(침묵 실패 금지).
- **35.5** /metrics 가 Prometheus 로 스크레이프 가능(형식 검증), doctor 지표와 수치 일치.
- **35.6** webhook 지정 작업 종결 시 3초 내 POST 1회(실패해도 작업 상태 무영향).
- **35.7** 1주 연속 가동 soak(cron 작업 일 3건): 크래시 0, 메모리 증가 상한 내,
  intent_log/이벤트 보존 정책 동작.
- **35.8** `alphred update` 후 기존 DB 의 과거 작업·설정이 그대로 조회됨(마이그레이션).
- **35.9** doctor --deep 이 Hermes v0.16.0 에서 5종 스모크 통과를 보고.

### 35.7 로드맵

| 단계 | 내용 | 산출 게이트 | 예상 |
|---|---|---|---|
| **M6a 하드닝** | 바인딩 기본값·키 스코프(G1-A)·도구 정책(G3, 훅/toolsets 실측 포함)·시크릿 위생·입력 상한 | QA-35.2/3/4 | ~1.5주 |
| **M6b 관측** | JSON 로그·회전·토큰 계측(조사→구현)·/metrics·webhook(delivery 소비)·보존 정책 | QA-35.5/6 | ~1주 |
| **M6c 배포** | PyPI/pipx·프로파일·온보딩 마법사·서비스화·doctor --deep·호환성 매트릭스·CHANGELOG | QA-35.1/8/9 | ~1.5주 |
| **M6d 클라이언트** | 대시보드 v2(질문 카드·체크리스트·라이브)·지원 클라이언트 문서·soak 테스트 | QA-35.7 + 대시보드 수동 QA | ~1주 |
| (조건부) 팀 서버 | G1-B — 수요 확인 후 별도 기획(선행 조사 3건 먼저) | — | TBD |

순서 근거: 보안 기본값(M6a)이 외부에 보여줄 수 있는 전제 → 관측(M6b)이 soak/실측의 눈 →
배포(M6c)가 "남이 깔 수 있음" → 클라이언트(M6d)는 그 위의 UX. 각 단계 플래그/기본값 변경은
CHANGELOG 에 파괴적 변경 명시.

### 35.8 리스크 & 대응

| 리스크 | 대응 |
|---|---|
| 바인딩/YOLO 기본값 변경이 기존 사용자(본인) 워크플로 파괴 | CHANGELOG+doctor 가 구 기본값 감지 시 1회 안내; env 로 즉시 복원 가능(`--host 0.0.0.0`, `ALPHRED_EXEC_POLICY=yolo`) |
| 도구 정책이 Hermes 훅/toolsets 로 깔끔히 안 걸릴 수 있음 | 착수 첫 작업 = 실측 스파이크(§11.12 훅 block 실측 자산 재활용). 안 되면 정책을 "차단" 대신 "감사 로그+사후 NeedsReview"로 강등하고 솔직히 문서화 |
| 토큰 usage 를 Hermes 가 노출 안 함 | run 수 근사 유지+"추정치" 라벨. Hermes 이슈 제안은 별도 |
| PyPI 이름 선점 | `alphred-agent` 폴백, console_script 는 `alphred` 유지 |
| 팀 서버(B) 기대와 A 범위의 괴리 | G0 에서 형태를 명시적으로 결정·문서 첫 줄에 "1-사용자 셀프호스트" 포지셔닝 명기 |
| soak 중 미실측 경로(watchdog 실개입·replan·타임아웃 가정 진행) 발현 | §34.18 미실측 목록을 soak 관찰 항목으로 편입 — 발현 시 intent_log/메트릭으로 즉시 원인 특정 가능(M6b 선행 이유) |

### 35.9 범위 확정 — 사용자 결정 반영 (2026-07-03)

**결정(사용자):** 멀티유저(한 배포를 여러 사람이 사용)는 **상정하지 않는다** — 조건부 M6d
팀서버 항목 폐기. 목표는 **1인 사용자 × 다양한 기기 접속**의 확인이며, 주력은 **배포(G4)와
클라이언트(G5)**. 이에 따라 §35.1~35.8 을 아래로 대체·재편한다(G2 관측성 대부분·G3 도구
정책 3단계는 백로그로 이동, 접속에 필요한 최소 보안만 유지).

**중심 산출물 — 접속 매트릭스 5종(최상위 인수 기준):**

| 모드 | 현재 상태(실측 근거) | 필요 작업 |
|---|---|---|
| a. 서버 머신 TUI | ✅ 동작(`alphred`, §34.18 라이브) | — |
| b. **외부 장치 TUI** | △ 거의 동작 — `ALPHRED_GATEWAY_URL` 지정 시 원격에 붙고 로컬 데몬 미기동(cli.py `_ensure_daemon` 실측). **갭**: 원격 불달 시 로컬 데몬을 띄워버림(씬클라이언트 의미론 위반) | `alphred connect <url>` 신설: 명시적 씬클라이언트 모드 — 데몬 자동기동 금지·연결 실패는 에러·키 인자/프롬프트·세션은 로컬 보관(큐는 서버 단일코어 ✓) |
| c. **웹 챗봇 UI** | ✖ 대시보드는 큐 관리만(챗 없음) | **`/chat` 페이지 신설**(단일 HTML 원칙): `/chat/stream` SSE 소비(도구 회색/답변 흰색), **needs_input 질문 카드**(추천 하이라이트·번호/직접 입력), 세션 유지(localStorage), 큐 미니 패널 |
| d. 외부 서비스 API | ✅ OpenAI 호환 동작(§34.18 라이브: SDK/curl) | 키+노출 가이드 문서, `needs_input`(202) 처리 안내 — api 소스는 질문 0·가정 진행이 이미 설계라 표준 클라이언트도 안전 |
| e. **ESP32/Arduino** | ✖ | 최소 샘플 스케치(`examples/esp32/`): WiFi+HTTPClient 로 `/v1/chat/completions` POST(키 헤더), Light=응답 표시·Heavy 202=task id 보관 후 `GET /v1/runs/{id}` 폴링(또는 webhook 수신은 서버측 예제). 회로 불필요(시리얼 모니터 데모). Arduino(C++) 단일 파일 |

**개정 로드맵:**

| 단계 | 내용 | 게이트 | 예상 |
|---|---|---|---|
| **M6-R1 원격 접속 기반** | 클라이언트 키 발급/회수·스코프(§35.1-A 유지), 바인딩 기본 127.0.0.1+외부 바인딩 무키 기동 거부, `alphred connect`(모드 b), LAN 노출/역프록시 TLS 가이드 | 모드 b 실측: 두 번째 머신(또는 같은 머신 별도 홈)에서 원격 TUI 로 제출→서버 큐 반영 | ~1주 |
| **M6-R2 배포** | PyPI/pipx(이름 폴백 alphred-agent), `ALPHRED_PROFILE=basic\|smart\|full`, `alphred setup` 마법사(모델 선택+프로파일+키 발급+doctor), 서비스화(schtasks/systemd/launchd), `doctor --deep`, CHANGELOG/버저닝 | QA-35.1(15분 설치)·35.8(업데이트 무손상)·35.9(--deep) | ~1.5주 |
| **M6-R3 클라이언트** | **웹 챗봇 UI(모드 c — 핵심 신규)**, webhook 알림(`Task.delivery` 소비 — 임베디드/외부 서비스에 실용), ESP32/Arduino 샘플(모드 e)+지원 클라이언트 문서(모드 d), **접속 매트릭스 5종 e2e 실측** | 매트릭스 5/5 통과 | ~1.5주 |

**백로그로 이동(폐기 아님):** /metrics·JSON 구조화 로그·보존 정책(G2 — soak 필요 시 소환),
도구 정책 3단계(G3 — 현행 YOLO+하드라인 차단+scan_payload 유지가 1인 로컬 전제에선 수용),
ESP32 실기기 음성 e2e. **폐기:** 팀서버(G1-B) 전체.

**설계 노트(모드별):** ①웹 챗은 대시보드와 별 페이지(`GET /chat`)로 — 큐 관리(운영)와 대화
(사용)의 관심사 분리, 둘 다 무의존 단일 HTML. ②모드 e 는 needs_input 을 만나지 않는다
(X-Alphred-Source 기본 api → 질문 0 — 임베디드가 질문에 답할 수 없다는 제약이 §34.4 설계와
정확히 맞물림; 문서에 명시). ③모드 b 세션: TUI 세션 파일은 기기 로컬(기기별 대화 이력),
작업 큐·선점·검증은 전부 서버 — 단일코어 불변식이 다중 기기의 정합성을 공짜로 보장.
④키는 기기당 1개 발급 권장(회수 단위 = 기기).

### 35.10 M6-R1~R3 As-Built — 다기기 접속·배포·클라이언트 (2026-07-03 구현·검증)

**R1 — 원격 접속 기반.** ①`alphred/clientkeys.py`: 기기별 키 발급/회수(`clients.json` 에
**sha256 해시만** 저장, 평문은 발급 시 1회 표시), 스코프 `read`(GET 만)/`control`(전부),
last_seen 분단위 갱신. CLI `alphred keys issue|list|revoke`. ②`deps.make_auth` 스코프 인식:
레거시 단일 키=control(하위호환), 클라이언트 키=자기 스코프, **read 의 변경류(POST/DELETE)
=403**, 키 전무=개발 모드 개방(현행 유지). ③**바인딩 기본 `0.0.0.0`→`127.0.0.1`(파괴적
변경, CHANGELOG 명시)** + `gateway.check_bind_safety`: 비루프백+무인증 → **기동 거부**.
④`alphred connect <url> [--key]`(모드 b): TUI 진입 전 프리플라이트(불달=연결 안내 exit 2·
401=키 발급 안내 exit 2·**로컬 데몬 폴백 없음**), 세션=기기 로컬·큐=서버.

**R2 — 배포.** ①`ALPHRED_PROFILE=basic|smart|full`(env>`ALPHRED_HOME/profile` 파일>basic;
smart=intent+planner, full=+clarify+orchestrate+watchdog; **개별 env 상시 우선** —
`config._flag`). `alphred setup --profile <이름>`(+TTY 미설정 시 1회 대화형 선택, 권장 2=
smart). ②pyproject 메타데이터(0.9.0, readme/keywords/classifiers/urls)+`CHANGELOG.md`
(0.9.0: §34+§35, 파괴적 변경 2건 명시). ③`alphred service install|uninstall|status`:
Windows=schtasks 직접(ONLOGON), Linux/macOS=systemd 유닛/launchd plist 생성+설치 안내.
④`alphred doctor --deep`: Hermes 프리미티브 라이브 스모크(인증/runs 생성/완주/stop —
PoC 축약판, 버전 호환성 검증 절차). doctor 에 프로파일·watchdog 행.

**R3 — 클라이언트.** ①**웹 챗 `GET /chat`**(`alphred/webchat.py`, 단일 HTML·의존성 0):
`/chat/stream` fetch-SSE 소비(도구 회색·최종 흰색), **needs_input 질문 카드**(선택지 버튼
+✦추천 강조+자유 입력, `/queue/{id}/answers` 제출), localStorage 세션·키, 최근 6턴 context
동봉(§34.2 A2), 미니 큐 스트립+완료/검토 알림(5s 폴링). ②**webhook(§35.2)**: 미소비였던
`Task.delivery` 소비 — `_notify_delivery` 가 종결 전이(Completed/NeedsReview/Discarded) 시
`{"webhook": url}` 로 결과 POST(백그라운드 스레드·재시도 1회·fail-open), `/v1/runs` body
`delivery` 수용. ③`examples/esp32/`(모드 e): Arduino 스케치(chat/completions POST → 200
즉답/202 폴링, webhook 대안 주석)+README — **컴파일/실기기 미실측 명시**(툴체인 부재).
④README EN/KO "다기기 접속" 섹션(매트릭스 표+서버 준비 4줄)+`/chat`·`ALPHRED_PROFILE` 표.

**검증.** pytest **323종 전체 통과**(M5 306 + R1 7(test_remote_access)+R2 5(test_profile)+
R3 5(test_webclient)), pyflakes 클린. **라이브 실측(임시 홈+키 서버 :8663)**: 무키
/queue=401 · control=200 · read GET=200 · **read POST=403** · `/chat` 페이지 서빙 ·
`connect` 잘못된 키=exit 2(발급 안내)/불달=exit 2(**로컬 데몬 스폰 없음**) · **키 revoke
즉시 401** · `serve --host 0.0.0.0` 무키=**기동 거부(exit 1, 포트 미바인딩 확인)**.
**접속 매트릭스 상태**: a ✅(§34.18) · b ✅(프리플라이트 실측+run_tui 계약; 실제 별도 머신
TUI 조작은 수동 QA 항목) · c ✅(페이지 서빙+API 계약; 브라우저 JS 플로우는 수동 QA) ·
d ✅(§34.18 라이브) · e △(코드 제공, 컴파일 미실측). **잔여**: QA-35.1(신규 머신 15분 설치
실측)·PyPI 실제 업로드(계정 필요)·soak 1주는 사용자 액션 필요 항목으로 이월.

---

## 36. 전용 TUI 대개편 — "Mission Deck" UI/UX (2026-07-03, 기획)

> 목표: 상용 에이전트 TUI(Claude Code, Gemini CLI/agy, Codex CLI)의 검증된 UX 패턴을 흡수하되,
> **그들에게 없는 Alphred 의 큐 시스템을 TUI 의 중심 경험으로 승격**시킨다.
> 참조 원칙: 유출본이 아니라 **공개 관찰 가능한 UX + 오픈소스 구현**(gemini-cli Apache-2.0,
> codex CLI, opencode)을 기준으로 한다 — 라이선스/출처 리스크 없이 같은 결론에 도달 가능.

### 36.0 설계 원칙

1. **콘텐츠 우선(Content-first)** — 화면의 90%는 대화·결과물. 크롬(보더/패널/배너)은 최소화.
   현재 TUI 는 크롬(상시 큐 패널 54칸 + 4중 보더 + 거대 배너)이 콘텐츠를 압도한다.
2. **큐는 벽지가 아니라 스토리** — 큐 상태를 옆에 "붙여두는" 것이 아니라, 작업의 탄생(제출)→
   질문→계획→스텝 진행→선점→완료/검증까지를 **대화 흐름 속 라이브 카드**로 서사화한다.
   상용 에이전트의 백그라운드 태스크 UI보다 한 단계 위 — 이것이 Alphred 의 차별화다.
3. **in-place 갱신** — append-only 로그(RichLog) 대신 위젯 단위 상태 갱신. 도구 줄·체크리스트·
   태스크 카드가 제자리에서 업데이트되어야 "요즘 에이전트" 감각이 난다.
4. **터미널에 순응하는 테마** — 배경색 강제(#140707) 제거. 사용자의 터미널 팔레트 위에
   Alph-RED 는 **액센트로만** 존재한다(브랜드 마크·추천 표시·포커스 보더).
5. **발견 가능성(discoverability)** — 숨은 단축키(v/c/d/p/r/+/-) 금지. 모든 조작은 화면 어딘가
   (푸터/상태줄/카드 힌트)에 항상 보이거나 팔레트에서 fuzzy 검색된다.
6. **약한 모델 전제 유지(§34.9)** — TUI 는 표현 계층. 새 LLM 호출을 추가하지 않고 게이트웨이가
   이미 주는 데이터(IntentCard/plan v2 step.state/verify_report/estimate)를 전부 표면화한다.

### 36.1 상용 에이전트 TUI 벤치마킹 — 채택할 패턴 12

| # | 패턴 | Claude Code | Gemini CLI(agy 계열) | Alphred 현재 |
|---|------|------------|---------------------|--------------|
| B1 | 도구 호출 블록: `● Tool(인자요약)` + `⎿ 결과 미리보기 n줄`(접힘, 실행 중 스피너→✓ in-place) | ✅ 핵심 문법 | ✅ 유사(박스) | ✗ dim 텍스트 2줄 append, 64자 |
| B2 | Todo/계획 체크리스트가 제자리에서 갱신(☐→▶→✓) | ✅ TodoWrite 렌더 | ✅ | △ 상세보기 때만 재출력 |
| B3 | 상태줄 스피너: `✻ 작업 중… (12s · esc 중단)` 경과시간+중단 힌트 | ✅ | ✅ | ✗ sub_title "처리 중…" 뿐 |
| B4 | Esc = 즉시 중단, 응답 중에도 입력창 활성(후속 메시지 큐잉) | ✅ | ✅ | ✗ 중단 불가·busy 정책 없음 |
| B5 | 질문/승인 UI: 화살표+Enter 인터랙티브 선택(숫자 타이핑 아님) | ✅ AskUserQuestion | ✅ | ✗ 번호를 타이핑 |
| B6 | 슬래시 fuzzy 자동완성 + 인자 힌트/2차 완성(@파일, 모델명…) | ✅ | ✅ | △ prefix-only, 인자 완성 없음 |
| B7 | 마크다운 렌더(헤딩/굵게/코드블록 하이라이트), diff 색상 | ✅ | ✅ | ✗ 평문 |
| B8 | 컴팩트 웰컴: 버전·모델·cwd·팁 몇 줄 박스(거대 아트 지양) | ✅ 작은 박스 | △ 배너+팁박스 | ✗ 114칸 배너+로고 전면 |
| B9 | 세션 재개: `/resume` 인터랙티브 피커(목록·미리보기·화살표 선택) | ✅ | ✅ | ✗ 로그에 목록 출력 후 번호 입력 |
| B10 | 모드 순환 단축키(Shift+Tab: 기본→auto-accept→plan) | ✅ | ✅ (approval mode) | ✗ /depth 타이핑 |
| B11 | 상세도 토글(ctrl+o 트랜스크립트/verbose) — 기본은 컴팩트 | ✅ | ✅ (ctrl+o 디버그) | ✗ 항상 모든 사고·도구 노출 |
| B12 | 유휴/완료 알림(터미널 벨·데스크톱), 컨텍스트 잔량 표시 | ✅ | ✅ | ✗ 로그 한 줄 |
| — | **백그라운드 작업 다중 관리** | △ bash 셸 수준(ctrl+b) | ✗ | ✅ **큐가 본체 — 유일한 우위, 그러나 표현이 못 따라감** |

결론: B1~B12 는 "따라잡기"(테이블 스테이크), 마지막 줄이 "앞서가기". 이 개편의 절반은 수렴,
절반은 큐 표현력의 독주다.

### 36.2 현 TUI 문제 진단

**[디자인] D 계열**
- **D1 강제 다크레드 배경** — `Screen { background: #140707 }` + 패널마다 배경/보더 4중
  (tui.py CSS). 사용자 터미널 테마 무시, 라이트 터미널에서 이질적. → 배경 강제 제거.
- **D2 거대 스플래시** — 114칸 배너+로고가 첫 화면 전부(splash.py). 상용 대비 구식 인상.
  정보(버전/모델/프로파일/서버/팁)가 0. → 컴팩트 웰컴 패널로 교체, 아트는 축소 보존.
- **D3 마크다운 미렌더** — 최종 답변이 평문 흰색 덩어리(tui_chat._flush_proc). 보고서형
  산출물(Alphred 주력)이 가장 손해. → Textual `Markdown` 위젯.
- **D4 append-only RichLog** — 쓴 시점 폭으로 고정되어 리사이즈 시 재래핑 불가(짤림),
  in-place 갱신 불가, 위젯(버튼/선택지) 삽입 불가. **모든 D/I/Q 개선의 병목** → 구조 전환.
- **D5 도구/사고 표현** — `┊ 🔧 tool…` 두 줄 append, 미리보기 64자 고정, 접기/펼치기 없음.
  사고(💭)가 항상 본문에 섞여 김. → B1 도구 블록 + B11 상세 토글.

**[상호작용] I 계열 문제**
- **I-P1 중단 불가** — Light 스트리밍 중 Esc 없음(send 워커 취소 경로 부재). busy 중 재전송
  시 동시 스트림 경합(정책 부재).
- **I-P2 질문 답변 UX** — §34.4 답변 모드가 "번호를 타이핑"(tui_chat._show_question).
  선택지가 로그로 흘러가 스크롤되면 유실.
- **I-P3 목록형 명령이 전부 로그 덤프** — /sessions·/model·/skills 가 수십 줄 출력 후 번호
  재입력 요구. 스크롤 오염 + 재입력 왕복.
- **I-P4 팔레트 빈약** — prefix 매칭만, 인자 완성 없음(/model 이름·/queue id·/sessions 번호).
- **I-P5 숨은 단축키** — 큐 테이블 v/c/d/p/r/+/- 가 어디에도 표시 안 됨(tui_base.QueueTable).
- **I-P6 마우스 포기** — mouse=False 로 휠 스크롤 상실(run_tui 주석의 의도적 트레이드오프).
  네이티브 복사는 얻었으나 풀스크린이라 큐 패널까지 같이 긁힘 — 실효 반감.

**[큐 활용] Q 계열 문제 — 핵심 자산의 표현 부족**
- **Q-P1 상시 고정폭 패널** — 54칸을 항상 점유, 요청문 22자 컷. 큐가 비어도 화면 1/3 낭비,
  많아도 정보 부족(진행바·스텝·견적 없음). 어중간한 상주.
- **Q-P2 상세보기가 대화 오염** — Enter 상세가 채팅 로그에 20줄 덤프(tui_queue._do_queue_action).
  대화와 큐 관리가 한 스트림에서 충돌.
- **Q-P3 스텝 실측 진행의 사장** — M4 StepRunner 가 step.state(done/running)를 실시간 기록하는데
  TUI 는 상세보기를 열 때만 스냅숏 표시. 진행바·현재 스텝 goal 상시 노출 없음.
- **Q-P4 선점(preemption) 비가시** — Alphred 의 존재 이유(단일 슬롯 선점 스케줄러)인데
  "보류" 라벨 한 단어뿐. 누가 누구를 밀어냈고 언제 재개되는지 서사가 없다.
- **Q-P5 AwaitingInput 발견성** — 다른 세션/다른 기기에서 생긴 질문 대기는 큐 표의 "입력대기"
  한 줄. 놓치면 타임아웃 가정 진행 — 기능은 안전하지만 UX 는 기회 손실.
- **Q-P6 견적·DoD 미표면화** — IntentCard/estimate/plan.dod 가 API 에 있는데 제출 순간
  사용자에게 안 보임(‘⏳ 큐에 등록됨’ 한 줄).

### 36.3 새 디자인 시스템

**레이아웃(기본 상태 — 큐 패널 상주 폐지):**
```
┌──────────────────────────────────────────────────────────┐
│  (웰컴 패널: ◆ Alphred v0.9 · 모델 · 프로파일 · 서버 · 팁 3줄) │
│                                                          │
│  › 사용자 메시지                                            │
│  ● execute_code(report.py)                    ✓ 1.2s     │ ← ToolBlock(접힘, in-place)
│    ⎿ 파일 생성: out/report.md                              │
│  ◆ Alphred                                               │
│  (마크다운 렌더된 답변)                                       │
│  ▛ 작업 3f9a12 · 보고서 생성      ▶ 실행 중 2/5 ▰▰▱▱▱      │ ← TaskCard(라이브)
│  ▙ 현재: 데이터 수집 · 견적 ~9콜 · high                       │
├──────────────────────────────────────────────────────────┤
│ ✻ 실행 중… 14s   큐 ▶1 ⏳2 ❓1   depth:auto   esc 중단      │ ← StatusBar
├──────────────────────────────────────────────────────────┤
│ › 메시지 입력…                                              │ ← Prompt
└──────────────────────────────────────────────────────────┘
  ctrl+t 큐 덱 · shift+tab 심화도 · ctrl+o 상세 · / 명령        ← Footer(콘텍스트 키)
```

- **채팅 영역 구조 전환(D4 해소, 최대 공사)**: `RichLog` → `VerticalScroll` + 메시지 위젯.
  위젯 종류: `UserMsg`(1줄 prefix `›`) · `AssistantMd`(Markdown, 스트리밍 중 dim→확정 시 본색) ·
  `ThinkBlock`(접힘 기본, ctrl+o 로 전개) · `ToolBlock`(●/⎿, 실행 중 스피너→✓/✗ in-place,
  Enter 로 전문 펼침) · `TaskCard`(36.5) · `QuestionCard`(36.4 I3) · `WelcomePanel`.
  리사이즈 재래핑·위젯 갱신·마우스 클릭이 전부 여기서 풀린다.
- **테마**: Screen 배경 지정 제거(터미널 팔레트 승계). 시맨틱 5색(_OK/_INFO/_WARN/_ERR/_SOFT)
  유지, Alph-RED(#E63946)는 브랜드 마크(◆)·추천(✦)·포커스 보더에만. `textual` 다크/라이트
  자동 대응 확인. 보더는 프롬프트(포커스 시 액센트)와 모달에만 round, 나머지는 무보더+여백.
- **웰컴 패널**: mini 로고(8칸 'A') + 우측 정보 스택(버전·모델·프로파일·서버 URL·큐 요약·팁 2개).
  풀 배너는 `/banner` 명령으로 보존(브랜딩 애착 존중, 기본 화면에서는 제거).
- **기호 문법 통일**: `›` 사용자 · `◆` Alphred · `●` 도구 실행 · `⎿` 결과 · `✦` 추천 ·
  `▶/⏳/❓/⚠/✓/✗` 상태. 이 문법을 README 스크린샷·웹챗에도 역수출(§35 웹챗과 통일).

### 36.4 상호작용 개편 (I1~I8)

- **I1 Esc = 중단**: Light 스트림 중 Esc → httpx 스트림 close + 워커 cancel + "중단됨" 확정.
  Heavy 라이브 뷰 중 Esc 는 기존대로 뷰 이탈(작업은 계속 — 큐라서 가능, 카드에 힌트 표기).
- **I2 busy 중 입력 = 큐잉**: 스트리밍 중에도 입력창 활성. 제출 시 즉시 새 요청으로 전송 —
  Light 응답 중이면 "대기 메시지" 칩으로 잡아뒀다 완료 후 자동 전송, Heavy 는 어차피 큐가
  받아준다. 상용의 "메시지 큐잉"이 Alphred 에선 구조적으로 자연스럽다는 점을 문서화.
- **I3 QuestionCard(§34.4 재작성)**: 질문을 로그가 아닌 **카드 위젯**으로. OptionList 에
  선택지(✦ 추천이 기본 하이라이트), ↑↓+Enter 선택, `직접 입력…` 항목 선택 시 인라인 필드,
  Esc=보류(가정 진행 안내). 여러 질문이면 카드 내 1/3→2/3 페이지 전환. 답변 후 카드가
  "→ 채택: …" 요약으로 접힘. 웹챗 질문 카드(§35.3)와 시각 문법 공유.
- **I4 팔레트 강화**: fuzzy 매칭(부분열), 선택 항목 아래 인자 힌트 줄, **2차 완성** —
  `/model ` 뒤 모델명 목록, `/sessions ` 뒤 세션 목록, `/queue cancel ` 뒤 활성 작업 id.
  Enter 즉시실행/Tab 채우기 규칙은 유지.
- **I5 세션 피커 모달**: `/sessions`(및 시작 시 복원 안내) → 모달 리스트(제목·메시지 수·시각,
  ↑↓ 선택, Enter 전환, d 삭제, Esc 닫기). 로그 덤프 제거.
- **I6 Shift+Tab = 심화도 순환**: auto→low→mid→high→auto. 상태줄에 `depth:high` 상시 표시.
  상용의 모드 순환을 Alphred 의 실질 모드(검증 강도)에 매핑.
- **I7 ctrl+o 상세 토글**: 기본 컴팩트(사고 접힘·도구 미리보기 1줄). 토글 시 ThinkBlock 전개
  + 도구 결과 전문. 세션 단위 유지.
- **I8 복사/내보내기**: 위젯 전환으로 네이티브 긁기가 어려워지는 대신 — `ctrl+y` 마지막 답변
  클립보드 복사, ToolBlock/AssistantMd 포커스 후 `y` 로 해당 블록 복사, `/export` 로 세션
  전체를 md 파일 저장. **mouse=True 복귀**(휠 스크롤·클릭 펼침 회복) — 트레이드오프 재결정을
  §36.8 에 명시.

### 36.5 큐 100% — "Mission Deck" (Q1~Q6)

큐 표현을 3 계층으로 재구성: **배지(항상) → 인라인 카드(대화 속) → 덱(전체 관리)**.

- **Q1 상태줄 배지(항상)**: `큐 ▶1 ⏳2 ❓1 ⚠1` — 실행/대기/입력대기/검토필요 실시간 카운트
  (기존 2s 폴링 재사용). ❓·⚠ 는 색 강조. 클릭/ctrl+t 로 덱 오픈. 54칸 상주 패널은 **폐지**.
- **Q2 인라인 TaskCard(대화 속 라이브 카드)**: 내 메시지가 Heavy 로 큐잉되는 순간 로그 한 줄
  대신 카드 삽입 → 이후 **제자리 갱신**:
  - 접수: 제목(프롬프트 요약) · id · 우선순위 · depth · **견적(~n콜)** · **DoD**(Q-P6 해소)
  - AwaitingInput: 카드가 QuestionCard 를 품음(I3)
  - 실행: **스텝 진행바 ▰▰▱▱▱ + 현재 스텝 goal**(plan v2 step.state 실측, Q-P3 해소) ·
    재시도 카운트 · Enter=라이브 스트림 확장(카드 내부에 회색 과정 표시, Esc 접기)
  - **선점**: `⏸ 보류 — 'DB 마이그레이션'(prio 9) 에 선점됨 · 스텝 경계에서 재개` (Q-P4 해소)
  - 종결: ✓완료(결과 요약 3줄+검증 뱃지 `Tier0 ✓ · judge 82`) / ⚠검토필요(미충족 항목) /
    ✗폐기. Enter=전체 결과 마크다운 펼침.
  - 다른 세션/기기에서 제출된 작업의 종결·질문은 카드가 아닌 **토스트+배지**로(Q5) — 내
    대화에는 내 작업만 카드로.
- **Q3 AwaitingInput 부상**: 어떤 경로(웹챗/API/타 세션)로 생겼든 ❓ 배지 + 토스트
  "작업 3f9a 가 답변 대기 중 (남은 시간 4:32)" + `/answer` (또는 덱에서 a) 로 QuestionCard 소환.
  남은 타임아웃을 카운트다운으로 표시 — "가만두면 추천값 진행" 계약(§34.4)을 시각화.
- **Q4 큐 덱(ctrl+t / /queue)**: 전체화면 모달. 좌측 리스트(우선순위 정렬, 탭 필터:
  활성/입력대기/종결), 우측 상세 패널(계획 체크리스트 실측 마크 · 검증 증거 패널 · 가정/답변 ·
  이력 타임라인 · 라이브 탭). **모든 조작 키를 하단에 상시 표시**(Enter 상세 · a 답변 ·
  p 보류 · r 재개 · +/- 우선순위 · d 폐기 · R 재시도 · L 라이브)(I-P5 해소). 단일 슬롯
  시각화: 리스트 최상단에 "실행 슬롯" 행 고정 — 지금 누가 점유 중이고 대기 1순위가 누구인지.
  상세 20줄 로그 덤프(Q-P2)는 덱 상세 패널로 이관, 채팅은 오염되지 않는다.
- **Q5 알림**: Textual `notify()` 토스트 + 터미널 벨(설정 `ALPHRED_TUI_BELL`, 기본 on) —
  완료/검토필요/입력대기 전이 시. 토스트에 "ctrl+t 로 확인" 힌트.
- **Q6 제출 전 견적 인라인**: `/plan` 드라이런 결과도 카드 프리뷰 형태로(동일 컴포넌트 재사용),
  "그대로 제출" 버튼(Enter) 제공 — 견적 확인 → 원클릭 제출 흐름.

**서버 보강(소폭, 새 LLM 호출 없음)**: 배지 카운트·카드 갱신은 기존 `GET /queue` 폴링과
`GET /queue/{id}/stream`(§33) 재사용으로 충분. 확인 필요 2건 — ① `/queue` 응답에 plan
step.state 요약(현재 스텝 index/goal)이 없으면 `task_view` 에 `step_progress` 필드 추가,
② Light 중단을 위한 클라이언트 disconnect 시 게이트웨이의 업스트림 run 정리 여부 점검.

### 36.6 마일스톤

| 단계 | 내용 | 핵심 리스크 |
|------|------|------------|
| **T1 기반 전환** | RichLog→위젯 채팅(D4), 테마/배경 제거(D1), 웰컴 패널(D2), 마크다운 렌더(D3), 도구 블록(D5, B1), 상태줄 스피너+배지 골격(B3·Q1), 세션 복원 재작성 | 최대 공사 — 기존 test_tui/세션 replay 회귀. 이 단계 전까지 다른 단계 착수 불가 |
| **T2 상호작용** | I1 Esc 중단 · I2 busy 큐잉 · I3 QuestionCard · I4 fuzzy+2차 완성 · I5 세션 피커 · I6 shift+tab · I7 ctrl+o | 답변 모드 재작성 — §34.4 계약(빈 Enter=추천, Esc=가정) 보존 필수 |
| **T3 미션 덱** | Q2 TaskCard 라이브 · Q3 AwaitingInput 부상 · Q4 큐 덱 모달 · Q5 알림 · Q6 /plan 카드 · 상주 큐 패널 제거 | step_progress 서버 필드 여부, 폴링→카드 갱신 일관성 |
| **T4 마감** | I8 복사/내보내기 · 저폭 반응형(배지 축약·카드 1줄 모드) · 성능(위젯 상한·과거 메시지 접기) · README 스크린샷 · §36 as-built | mouse=True 트레이드오프 실사용 검증 |

**테스트 전략**: 기존 방식 계승 — ① 렌더 로직은 순수 함수로 분리(plan 진행바·배지 카운트·
카드 라벨 등, `plan_checklist_lines` 전례) 단위 테스트, ② Textual `run_test()` Pilot 헤드리스로
카드 갱신/키 바인딩/모달 스모크, ③ fake gateway(httpx mock) 로 SSE 이벤트 시퀀스 재생 →
카드 상태 전이 검증. 인수 기준(발췌): Esc 1초 내 중단 · 질문 카드 화살표만으로 답변 완료 ·
Heavy 제출→완료까지 채팅 이탈 없이 카드에서 관찰 가능 · 선점 발생 시 카드에 사유 표기 ·
리사이즈 후 과거 메시지 재래핑 · 라이트 테마 터미널에서 가독성.

### 36.7 하지 않는 것

- 새 LLM 호출 추가(원칙 6), Hermes 코어 수정(불변 invariant), 웹챗 리라이트(문법 통일만),
  큐 패널의 "개선 유지"(상주 패널은 개선이 아니라 폐지가 답), GUI/이미지 렌더.

### 36.8 리스크 & 트레이드오프

1. **Textual 최소 버전 상향** — Markdown 스트리밍·notify·테마 대응을 위해 `textual>=0.60` 을
   최신 안정선으로 올려 고정(착수 시 실측 결정). CHANGELOG 에 명시.
2. **mouse=True 복귀 ↔ 터미널 네이티브 복사 상실**(§13 시절 결정 뒤집기) — 위젯 채팅에서는
   화면 긁기 복사가 어차피 레이아웃 파편을 긁는다. `ctrl+y`/블록 `y`/`/export` 로 대체하고,
   그래도 필요하면 대부분 터미널의 Shift+드래그(마우스 캡처 우회)가 동작함을 도움말에 기재.
3. **T1 회귀 폭** — 세션 replay·라이브 뷰·답변 모드가 전부 RichLog 의존. T1 을 "동작 동등
   교체"로 좁히고 신기능은 T2+ 로 격리, 마일스톤당 전체 pytest 그린 유지.
4. **폴링 기반 카드 갱신의 지연**(2s) — 스텝 전이는 2s 지연 허용, 실행 과정은 기존 SSE 라이브
   로 즉시성 확보. 폴링 주기 단축은 하지 않는다(서버 부하·배터리).

### 36.9 T1 as-built — 기반 전환 (2026-07-03 구현 완료)

**구현 내역** (계획 §36.6 T1 대비):

- **D4 위젯 채팅**: `#chat` 이 `RichLog` → `ChatView(VerticalScroll)` (tui_base). 바닥에
  있을 때만 자동 스크롤(위로 읽는 중이면 방해 안 함), 위젯 상한 600(초과 시 오래된 것부터
  제거). 믹스인 호환을 위해 `_log(markup)` 은 유지하되 `Static` 마운트로 구현 — 기존
  호출부(검증 패널·큐 상세 등) 무수정 동작.
- **D5/B1 ToolBlock**: `● 도구명 실행 중…` → 완료 시 같은 위젯이 `● 도구명 ✓` +
  `⎿ 미리보기` 로 제자리 갱신, 실패 시 `✗`. 시작 이벤트를 못 본 채 완료가 오면(라이브
  중간 진입) 종결 상태 블록을 새로 추가. `_open_tools` 는 send/라이브 시작 시 리셋.
- **D3 마크다운**: 최종 답변은 `AssistantMd(Markdown)` — 원문을 `source_text` 로 보존
  (세션 기록·복사·테스트용). 세션 replay 도 동일 위젯.
- **D1 테마**: `Screen`/패널 배경색 지정 전부 제거(터미널 팔레트 승계). Alph-RED 는
  프롬프트/팔레트 보더와 브랜드 마크에만.
- **D2 웰컴 패널**: mini 'A' 그라데이션 아트 + 버전(importlib.metadata)·서버·모델·세션·
  팁 2줄. 모델 로드/세션 전환 시 제자리 갱신. 전체 배너+로고 아트는 신설 `/banner` 로 보존.
  주의: `remove_children()` 의 제거가 비동기라 위젯 id 재사용 시 DuplicateIds — id 대신
  참조(`_welcome_widget`)로 관리(실측 후 수정).
- **B3/Q1 상태줄**: `#statusbar` 1줄 — busy 시 `✻ 처리 중… {n}s`(1s 틱), 큐 배지
  `queue_badges()`(순수 함수: ▶실행 ⏳대기+보류 ❓입력대기 ⚠검토필요, 활성 없으면 '큐 —'),
  `depth:auto|low|mid|high`, 모델 단축명(`model_short`), 세션 단축 ID. 기존 "모델·세션은
  채팅 보더 제목" 계약을 상태줄로 이관(`_set_titlebar` 이름은 유지).
- **기타**: PgUp/PgDn 로 입력 포커스 유지한 채 채팅 스크롤. 리사이즈 시 배너 재렌더
  로직 삭제(위젯이 스스로 재래핑). `pyproject` 의존성 `textual>=0.60` → `>=1.0`.

**실측 노트**: Textual 8.2.7 기준 — `Static.renderable` 속성이 제거되고 `.content` 로
바뀜(테스트 작성 시 확인). 계획의 "동작 동등 교체" 범위 준수 — Esc 중단·질문 카드·fuzzy
팔레트·큐 덱은 T2/T3 그대로 남김. 큐 상시 패널도 T3 까지 유지.

**검증**: 전체 pytest 324 통과(TUI 29 — 상태줄/웰컴/ToolBlock in-place/배지 신규 포함),
pyflakes 클린. README 양 언어 + CHANGELOG(Unreleased) 갱신. 수동 QA(실터미널 라이트
테마·실서버 스트리밍 외관)는 T4 마감에서 일괄.

### 36.10 T2 as-built — 상호작용 (2026-07-04 구현 완료)

**구현 내역** (계획 §36.4 I1~I7; I8 은 T4):

- **I1 Esc 중단**: `PromptInput` 의 Esc 우선순위 = 팔레트 닫기 → 답변 보류 → **send 워커
  취소(`cancel_send`)** → 라이브 뷰 이탈. 취소 시 부분 출력/사고는 회색으로 확정하고
  "⏹ 응답을 중단했습니다" 표시. 워커 참조는 `_send_worker` 에 보관(제출·드레인 공용).
- **I2 busy 중 입력 큐잉**: 응답 중 제출된 메시지는 화면·세션에 즉시 기록하고
  `_pending_msgs` 에 보관 → send 의 finally 에서 `_drain_pending()` 이 순서대로 자동 전송.
  Heavy 는 어차피 서버 큐가 받으므로 이 경로는 Light 연속 질문용.
- **I3 QuestionCard**: §34.4 답변 모드를 카드 위젯으로 재작성 — OptionList 에 선택지
  (+ "✏ 직접 입력…"), **✦ 추천이 기본 하이라이트**(Enter=추천), 선택 시 같은 카드가 다음
  질문(1/3→2/3)으로 갱신, 완료/보류 시 카드 제거. 입력창 텍스트 경로(빈 Enter=추천 ·
  번호 · 자유 입력 · Esc=가정 진행)는 **병행 유지** — 기존 §34.4 계약 보존.
  공용 확정 로직은 `_apply_answer` 로 추출(카드 `answer_pick`/입력창 `answer_submit` 공용).
- **I4 팔레트**: 명령 매칭 = prefix 우선 + **fuzzy(부분열, `fuzzy_match`)**. **인자 2차
  완성** 신설 — `/depth`(auto/low/mid/high), `/model`(모델 목록 캐시 — `/models/available`
  응답에서 확보), `/sessions`(저장 세션 → 번호 완성), `/queue`(서브명령 + 활성 작업 id).
  Option id `arg:<완성줄>` 인코딩, Enter=즉시 실행 · Tab=채우기 규칙 유지.
- **I5 세션 피커**: `/sessions` 무인자 → `SessionPicker(ModalScreen)` — ↑↓ 선택 ·
  Enter 전환 · d 삭제(세션발 큐 작업 연쇄 삭제 포함, `_delete_session` 공용 추출) ·
  Esc 닫기. 텍스트 경로(`/sessions 2`, `delete <id>`)는 유지.
- **I6 심화도 순환**: Shift+Tab = auto→low→mid→high→auto (`action_cycle_depth`,
  `DEPTH_CYCLE`). 상태줄 `depth:` 에 즉시 반영. PromptInput 이 가로채고(TextArea 의
  포커스 이동 대체) App 바인딩도 등록(다른 포커스 커버 + 푸터 표시).
- **I7 상세 토글**: ctrl+o — `ThinkBlock`(사고: 기본 48자 요약+"ctrl+o 전체" 힌트 ↔ 전문),
  `ToolBlock`(결과: 기본 1줄 64자 ↔ 전문 최대 8줄) 을 **마운트된 것까지 소급** 재렌더.
  도구 preview 는 원문을 위젯에 보관(표시 컷은 위젯 책임). 새 위젯은 rich markup escape
  적용(임의 결과 텍스트의 `[` 파싱 오류 방지 — 기존 _log 경로 대비 개선).

**구조 보강(실측에서 발견)**:
- `App.query_one` 은 **최상단 스크린**을 검색 → 모달이 떠 있는 동안 2s 큐 폴링·라이브
  스트림 워커가 `#chat`/`#queue` NoMatches 로 죽는 문제. 배경 워커가 쓰는 위젯
  (`_chat_view`/`_queue_table`/`_statusbar_w`/`_streaming_w`)을 on_mount 에서 참조로
  고정해 해결.
- `Screen.on_mount` 가 compose 자식 마운트보다 먼저 불릴 수 있음(SessionPicker 실측)
  → 옵션 구성을 생성자로 이동.

**검증**: 전체 pytest 332(+8: Esc 취소·큐잉/드레인·카드 플로·카드 Esc 보류·fuzzy+인자
후보·심화도 순환·상세 토글·피커 모달), pyflakes 클린.

### 36.11 T3 as-built — 큐 미션 덱 (2026-07-04 구현 완료)

**구현 내역** (계획 §36.5 Q1~Q6). 큐 표현을 **3계층**으로 재구성하고 상주 큐 패널을 폐지:

- **상주 큐 패널 폐지**: `#queuepanel`(54칸 DataTable)과 `QueueTable` 위젯 제거. compose 는
  이제 채팅이 화면 전폭. 큐 키보드 단축키(v/c/d/p/r/+/-)의 "숨은 조작" 문제(Q-P1/I-P5)도
  함께 해소 — 조작은 덱에서 키를 상시 표시.
- **Q1 상태줄 배지**: (T1 에 이미 골격) `queue_badges()` 가 2s 폴링마다 `▶⏳❓⚠` 카운트 갱신.
- **Q2 인라인 TaskCard**: 내 대화에서 Heavy 로 큐잉되는 순간(`queued` 이벤트) 로그 대신
  `TaskCard` 위젯을 심고(`_spawn_task_card`, id→위젯 dict `_task_cards`), 2s 폴링이
  `_update_task_cards` 로 **제자리 갱신**. 상태별 표현은 순수 함수 `task_card_markup`:
  - Pending: 견적(~n콜)·우선·심화도 + DoD (Q-P6)
  - In-Progress: **스텝 진행바 ▰▰▱▱ + 완료/전체 + 현재 스텝 goal** — `step_progress(plan)`
    가 StepRunner 의 실측 step.state 를 읽고(없으면 도구 카운트 `plan_progress` 폴백, Q-P3)
  - Paused: **"우선순위 높은 작업에 선점됨 · 경계에서 자동 재개"** 서사 (Q-P4)
  - 종결: ✓완료(결과 3줄 요약 + 검증 뱃지 `검증 ✓ · judge NN`) / ⚠검토필요(미통과 요약) /
    ✗폐기. 종결 후엔 `_task_cards` 에서 빼 폴링 갱신 중단(최종 렌더 고정).
  - **markup escape**: 프롬프트/결과의 `[` 를 escape 해 rich 파싱 오류 방지.
- **Q3 AwaitingInput 부상**: `/answer [id]`(`cmd_answer`) — 출처(웹챗/API/타 세션) 무관하게
  대기 작업의 질문 카드(§36 I3)를 소환. 카드 자체가 남은 시간 카운트다운(`_remaining_mmss`)과
  "그대로 두면 추천값 진행" 계약을 표시. 타 세션에서 새로 뜬 대기는 폴링이 토스트로 알림.
- **Q4 큐 덱(`QueueDeck` ModalScreen, ctrl+t / `/queue`)**: 좌측 OptionList(활성 우선순위
  정렬 + 종결 12개) + 우측 상세(VerticalScroll). 상단에 **단일 실행 슬롯 시각화**
  (`deck_slot_line` — 지금 점유 중 + 대기 1순위). 하단에 **조작 키 상시 표시**. 조작
  (a 답변·p 보류·r 재개·R 재시도·d 폐기·+/- 우선·L 라이브)은 공용 `_queue_op` 경유. 하이라이트
  이동 시 상세를 exclusive 워커로 로드(스테일 가드). 덱이 떠 있으면 2s 폴링이 리스트도 갱신.
  상세 20줄 덤프가 채팅을 오염시키던 문제(Q-P2)를 덱으로 이관.
- **Q5 알림**: `_notify_transitions` — 완료/검토/폐기/대기부상 전이에 Textual `notify()` 토스트
  + 터미널 벨(`ALPHRED_TUI_BELL`, 기본 on). **채팅 로그 알림은 인라인 카드가 없는 작업
  (=타 세션/기기)만** — 내 카드가 이미 표시 중인 작업은 중복 로그 안 냄.
- **Q6**: `/plan` 드라이런은 기존 유지(카드형 프리뷰+원클릭 제출은 T4 여력 시 — 현재는 견적
  텍스트로 충분히 표면화됨).

**서버 보강 판단**: `task_view` 가 이미 `plan`(step.state 포함)·`estimate`·`verify_report`·
`input_deadline`·`plan_progress`/`plan_activity`·`session_key` 를 모두 노출 →
**서버 변경 불필요**. TaskCard/덱은 순수하게 표현 계층에서 조립. (계획에서 검토 대상이던
`step_progress` 서버 필드 추가는 불요로 결론.)

**리팩터**: 구 `_do_queue_action`/`_render_verify_report`/`queue_action` 의 상세·검증 렌더를
순수 함수 `task_detail_lines`/`verify_report_lines` 로 추출(덱·채팅 공용). `_ACTIVE_STATES`/
`_TERMINAL_STATES` 를 tui_base 상수로 통일.

**검증**: 전체 pytest 335(+5: 덱 열림/슬롯·카드 상태별 마크업·카드 폴링 갱신+종결·/answer
소환·전이 토스트/벨), pyflakes 클린. 상주 패널 폐지로 깨진 3개 테스트(마운트 위젯 목록·
큐 열·큐 키)를 덱/카드 테스트로 교체.

### 36.12 T4 as-built — 마감 (2026-07-04 구현 완료)

**구현 내역** (계획 §36.6 T4):

- **I8 복사/내보내기**:
  - `ctrl+y`(`action_copy_last`) — 마지막 어시스턴트 답변 원문을 클립보드로
    (`App.copy_to_clipboard`, 세션 기록에서 원문 추출). 실패 시 /export 안내.
  - `/export [경로]`(`cmd_export`) — 현재 세션 대화를 Markdown 으로 저장(제목·모델·시각
    헤더 + 역할별 섹션). 경로 생략 시 `alphred-<sid>-<타임스탬프>.md` 자동 이름.
  - **`mouse=True` 복귀**(§13 결정 뒤집기): 위젯 채팅에선 휠 스크롤·클릭(도구/카드 펼침)이
    더 값짐. 네이티브 긁기 복사 상실은 ctrl+y//export + 대부분 터미널의 Shift+드래그로 보전.
- **저폭 반응형**: `_refresh_statusbar` 가 폭 <80 이면 모델·세션을 접고 상태·배지·depth 를
  보존(좁은 터미널 오버플로 방지). 카드/도구 블록은 위젯이라 자동 재래핑.
- **성능**: `ChatView.MAX_CHILDREN=600` 상한(초과 시 오래된 위젯부터 제거). 종결 작업의
  TaskCard 는 `_task_cards` 에서 빠져 폴링 갱신 대상에서 이탈(누수 방지). 상세 로드는
  exclusive 워커로 중복 억제.
- **도움말**: `/help` 키 힌트에 Ctrl+T/Ctrl+Y 추가, `/export` 명령 등록.

**남은 사용자-액션(코드 밖, 대신 못 하는 것)**: 실제 터미널에서의 수동 QA — 라이트 테마
가독성, mouse=True 실사용감(휠/클릭/Shift+드래그 복사), 클립보드 연동(OS/터미널별), README
스크린샷 캡처(실행 화면 필요). 회귀는 헤드리스 Pilot 로 커버되나 "외관/손맛"은 실기기 확인 몫.

### 36.13 §36 종합 — 4개 마일스톤 완료 (2026-07-04)

T1(기반 전환)·T2(상호작용)·T3(미션 덱)·T4(마감) 전부 구현. 전체 pytest **338 통과**
(TUI 43), pyflakes 클린. 상용 TUI 패턴 B1~B12 수렴 + 큐 3계층으로 독주.

**설계 원칙 준수 결산**:
1. 콘텐츠 우선 ✅ 상주 크롬(54칸 패널·거대 배너·4중 보더) 제거, 채팅 전폭.
2. 큐=스토리 ✅ 인라인 TaskCard 가 접수→질문→진행→선점→종결을 대화 속 서사로.
3. in-place ✅ ToolBlock/ThinkBlock/TaskCard/상태줄이 제자리 갱신.
4. 터미널 순응 ✅ 배경 강제 제거, Alph-RED 는 액센트로만.
5. 발견성 ✅ 조작 키를 덱 하단·/help·상태줄에 상시 노출(숨은 단축키 제거).
6. 약한 모델 전제 ✅ 새 LLM 호출 0, Hermes 코어 무수정, 서버는 기존 필드만 노출(불요 필드
   추가 안 함).

**핵심 구조 변경 1줄 요약**: RichLog(append-only) → 위젯 채팅(ChatView) 이 모든 개선의
잠금 해제였고, 그 위에 큐를 배지→카드→덱 3계층으로 올린 것이 Alphred 만의 차별화.

**미착수(백로그, 명시적 보류)**: Q6 /plan 카드형 원클릭 제출(현재 견적 텍스트로 충분),
블록 단위 `y` 복사(ctrl+y 마지막 답변으로 대체), 데스크톱 알림(터미널 벨+토스트로 충분).
