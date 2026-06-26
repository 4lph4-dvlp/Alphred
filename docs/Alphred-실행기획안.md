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
