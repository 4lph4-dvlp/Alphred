"""도메인 모델 — 작업(Task)과 상태(State)."""
from __future__ import annotations

import enum
from dataclasses import asdict, dataclass


class TaskState(str, enum.Enum):
    AWAITING_INPUT = "AwaitingInput"  # 착수 전 사용자 답변 대기 (비실행, §34.4 — 타임아웃 시 가정 진행)
    PENDING = "Pending"          # 시작 전
    IN_PROGRESS = "In-Progress"  # 진행 중
    PAUSED = "Paused"            # 일시중지
    COMPLETED = "Completed"      # 완료 (최종)
    NEEDS_REVIEW = "NeedsReview" # 실행은 끝났으나 검증 미통과 → 사람 확인 필요 (최종, §21)
    DISCARDED = "Discarded"      # 폐기 (최종)

    @property
    def is_terminal(self) -> bool:
        return self in (TaskState.COMPLETED, TaskState.NEEDS_REVIEW, TaskState.DISCARDED)


class TaskKind(str, enum.Enum):
    LIGHT = "light"
    HEAVY = "heavy"


class TaskSource(str, enum.Enum):
    CHAT = "chat"
    API = "api"
    CRON = "cron"
    SUBSERVICE = "subservice"
    TUI = "tui"


@dataclass
class Task:
    id: str
    source: str = TaskSource.API.value
    kind: str = TaskKind.HEAVY.value
    priority: int = 5                     # 1(최하) .. 10(최상)
    state: str = TaskState.PENDING.value
    prompt: str = ""
    resolved_prompt: str | None = None    # §40 지시어 해소본("동일하게" → 자기완결형) — 계획/실행/검증 기준
    hermes_run_id: str | None = None      # /v1/runs id
    response_id: str | None = None        # 정상 멀티턴 재개용
    conversation_history: str | None = None  # JSON; 중단된 Heavy 재개용 SSOT
    attachments: str | None = None        # JSON; 요청 멀티모달 파트(이미지) 보존(§37)
    session_key: str | None = None
    delivery: str | None = None           # JSON
    result: str | None = None
    artifacts: str | None = None          # JSON; 결과가 실제 생성한 파일 경로들(§40 원장·후속 참조용)
    classify_reason: str | None = None    # 분류 근거(감사)
    depth: str | None = None              # 작업 심화도 low/mid/high (§21)
    category: str | None = None           # §39 작업 카테고리 (coding/research/etc.)
    verify_report: str | None = None      # JSON; 검증·수용 결과(Tier0/Tier2 §21)
    verify_attempts: int = 0              # 검증 폐루프 재시도 횟수(§21 Tier3)
    verify_feedback: str | None = None    # 직전 검증 미흡 항목(재시도 입력에 주입)
    intent: str | None = None             # JSON; §34.2 IntentCard(goal/depth/missing_info 등)
    questions: str | None = None          # JSON; §34.4 인테이크 질문(선택지+추천 포함)
    answers: str | None = None            # JSON; §34.4 사용자 답변(실행 입력에 주입)
    assumptions: str | None = None        # JSON; §34.4 가정 원장(무답변 진행 시 채택·표면화)
    input_deadline: str | None = None     # ISO8601; §34.4 답변 대기 타임아웃(경과 시 가정 진행)
    plan: str | None = None               # JSON; 계획기반 분류의 하위작업 분해(실행 힌트로 재활용)
    plan_progress: int = 0                # 실행 중 완료한 도구 호출 수(단계 진행 추적, §19 P3)
    plan_activity: str | None = None      # 현재/최근 도구 활동(예: web_search)
    paused_reason: str | None = None
    error: str | None = None
    retries: int = 0                      # transient 실패 재시도 횟수
    retry_not_before: str | None = None   # 백오프: 이 시각 이전에는 재개하지 않음(ISO8601)
    created_at: str = ""
    updated_at: str = ""
    started_at: str | None = None
    finished_at: str | None = None

    def to_row(self) -> dict:
        return asdict(self)
