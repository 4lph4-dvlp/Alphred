# Changelog

Alphred 의 눈에 띄는 변경 사항. 형식은 [Keep a Changelog](https://keepachangelog.com/ko/1.1.0/),
버전은 시맨틱 버저닝(1.0 전 0.x — 마이너 올림이 기능, 패치가 수정)을 따른다.

## [Unreleased]

### 추가 (Added)

- **§29.1 확장 — 추론 깊이(reasoning_effort) 래핑**: Hermes `agent.reasoning_effort`
  (none/minimal/low/medium/high/xhigh)를 Alphred 에서 설정 가능 — 전역
  (`POST /models/reasoning`, TUI `/reasoning <레벨>`) + depth tier별
  (`/reasoning high xhigh`, env `ALPHRED_REASONING_HIGH|MID|LOW`, models.json
  `{tier: {reasoning}}`). 디스패치 직전 멱등 라인편집으로 적용, 미설정 depth 는
  base 스냅샷 복원. `POST /models/tiers` 는 부분 갱신(model/reasoning 독립)으로 확장.
- **§37 Heavy 멀티모달 보존**: 이미지 동봉 요청이 Heavy 큐로 분류되어도 이미지
  파트(image_url/input_image)를 표준형으로 정규화해 Task 에 저장(`attachments`)하고,
  run 시작 시(단일·스텝 실행 모두) 사용자 메시지에 재동봉 — 기존에는 텍스트만 남아
  이미지가 유실됐다. Light 경로는 종전대로 원본 패스스루.
- **§29.3 확장 — 임의 설정 get/set**: `alphred tune --get <path>` ·
  `--set <path> <value>` 로 KNOBS 밖 Hermes 스칼라(agent.max_turns,
  auxiliary.\*.model 등)를 백업·멱등 규칙 그대로 조회·조정(신규 키 삽입은 거부,
  `--revert` 로 원복).

### 수정 (Fixed)

- **§29.1 크로스 프로바이더 tier 복원 버그**: provider/base_url 을 지정한 tier 실행 후
  미설정 depth 로 복귀할 때 이전 tier 의 provider 가 config.yaml 에 잔류하던 문제 —
  첫 tier 설정 시 base 를 model+provider+base_url 로 스냅샷하고 복귀 시 전체 복원.
- **§29.1 종료 시 tier 잔류**: 게이트웨이 종료 시 config.yaml 을 base 모델·추론 깊이로
  복원 — Alphred 종료 후 독립 실행한 Hermes 가 마지막 tier 의 (비싼) 모델/추론으로
  동작하던 문제 해소.

- **§36 TUI 대개편 T1(기반 전환)**: 위젯 채팅(도구 블록 `●/⎿` 제자리 갱신) · 최종 답변
  마크다운 렌더 · 컴팩트 웰컴 패널(전체 아트는 `/banner`) · 상태줄(스피너+경과시간,
  큐 배지 `▶⏳❓⚠`, depth/모델/세션) · PgUp/PgDn 채팅 스크롤
- **§36 TUI 대개편 T2(상호작용)**: Esc = 응답 즉시 중단 · 응답 중 제출 = 대기 후 자동
  전송 · 인테이크 질문 카드(↑↓+Enter, ✦추천 기본 선택, 직접 입력) · 슬래시 fuzzy 매칭
  + 인자 자동완성(`/model`·`/depth`·`/sessions`·`/queue`) · `/sessions` 인터랙티브
  피커 모달 · Shift+Tab 심화도 순환 · Ctrl+O 상세 토글(사고/도구 결과 전문)
- **§36 TUI 대개편 T3(큐 미션 덱)**: 상주 큐 패널 폐지 → 큐 3계층 — 상태줄 배지 +
  **대화 속 인라인 작업 카드**(견적/DoD·스텝 진행바·현재 스텝·선점 사유·검증 뱃지가
  제자리 갱신) + **큐 덱 모달**(`Ctrl+T` / `/queue`: 리스트+상세+실행 슬롯 시각화, 조작 키
  상시 표시) · `/answer` 로 답변 대기 작업 소환 · 완료/검토/폐기/대기 전이 토스트+벨
  (`ALPHRED_TUI_BELL`)
- **§36 TUI 대개편 T4(마감)**: `Ctrl+Y` 마지막 답변 클립보드 복사 · `/export` 세션 대화
  Markdown 저장 · 저폭 터미널 상태줄 축약 · 채팅 위젯 상한(성능)

### 변경 (Changed)

- TUI 마우스 활성(`mouse=True`) 복귀 — 휠 스크롤·클릭(도구/카드 펼침) 회복. 터미널 네이티브
  긁기 복사는 `Ctrl+Y`·`/export`·Shift+드래그로 대체.

### 변경 (Changed)

- TUI 배경색 강제(#140707) 제거 — 사용자 터미널 팔레트를 승계(라이트 테마 호환)
- 의존성 하한 상향: `textual>=1.0` (실측 8.2.7)

## [0.9.0] — 2026-07-03

§34 "Conductor"(에이전트 소양) 전체 + §35 M6 다기기 접속. 1.0 직전 베타.

### ⚠ 파괴적 변경 (Breaking)

- **`alphred serve` 기본 바인딩이 `0.0.0.0` → `127.0.0.1`** (로컬 전용). 다른 기기에서
  접속하려면 `alphred serve --host 0.0.0.0` 을 명시하고, **접속 키가 필수**가 됩니다
  (키 없이 외부 바인딩 시 기동 거부). 키 발급: `alphred keys issue <기기이름>`.
- 클라이언트 키가 하나라도 발급되면 API 인증이 필수가 됩니다(키 전무 시에만 기존
  개발 모드 유지).

### 추가 (Added)

- **§34 Conductor 파이프라인** *(프로파일로 게이팅 — 기본 `basic` 은 기존 동작 유지)*:
  - IntentCard — LLM-first 의도 판정(종류·우선순위·심화도·부족정보, `ALPHRED_INTENT`)
  - 인테이크 질문 — 착수 전 확인 질문+추천답변, `AwaitingInput` 상태, 무응답 시 가정
    진행(`ALPHRED_CLARIFY`, `POST /queue/{id}/answers`)
  - Plan v2 — 디스패치 직전 실행 가능한 계획(스텝별 목표/도구/산출물/완료 기준) +
    실물 능력 접지(없는 라이브러리 → 설치 스텝 자동 삽입)
  - StepRunner — high 심화도 스텝 단위 실행·스텝별 검증·실패 스텝만 재시도·재계획 1회·
    예산 가드(`ALPHRED_ORCHESTRATE`)
  - watchdog — 실행 중 도구 오류 루프/무진전 감지 → 중단 후 교정 재개(`ALPHRED_WATCHDOG`)
  - CapabilityRegistry — 스킬/도구/MCP/CLI/라이브러리 실물 스냅샷을 하네스·계획에 주입,
    `GET /capabilities`
  - 선호 기억(`preferences.md`) · 분류 텔레메트리(intent_log) · doctor 품질 지표
- **§35 다기기 접속(1인 사용자 × 여러 기기)**:
  - `alphred keys issue|list|revoke` — 기기별 접속 키(서버엔 해시만 저장,
    `read`=모니터링 전용 / `control`=전부)
  - `alphred connect <서버URL>` — 외부 기기에서 서버로 붙는 씬클라이언트 TUI
    (로컬 데몬을 띄우지 않음)
  - `alphred service install|uninstall|status` — 로그온 시 자동 기동(Windows 작업
    스케줄러 / systemd·launchd 파일 생성)
  - `alphred setup --profile basic|smart|full` — 프리셋(§35.4): smart=+의도·계획(권장),
    full=+질문·스텝 실행·감시. 개별 환경변수가 항상 우선.
  - `alphred doctor --deep` — Hermes 프리미티브 라이브 스모크(버전 호환성 검증 절차)

### 변경 (Changed)

- 기본 모델 운영 노트: NVIDIA NIM `meta/llama-3.3-70b-instruct` 포화 실측에 따라
  `google/gemma-4-31b-it` 로 전환·검증(§34.18). 모델은 `/model` 또는
  `POST /models/default` 로 언제든 변경.

## [0.1.0] — 2026-06-20 ~ 2026-07-01

최초 공개 이전 개발 라인 — 우선순위 큐+상태머신(Phase 1), 선점 스케줄링(Phase 2),
OpenAI 호환 게이트웨이(Phase 3), 운영 안정성+웹 대시보드(Phase 4), 전용 TUI(§13),
실시간 스트리밍(§16/§33), 계획 기반 분류(§19), 검증·수용 루프(§21), 큐 랭커(§22),
실행 하네스 외부화(§26), 모델 라우팅·튜닝(§29).
