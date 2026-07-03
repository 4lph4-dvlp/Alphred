"""상태 제어 머신 — 허용된 전이만 강제한다 (QA-7.4).

전이 규칙(기획 2.1, §21, §34.4):
  AwaitingInput → Pending, Discarded   (답변 수신/타임아웃 → 실행 대기열 진입)
  Pending     → In-Progress, Discarded
  In-Progress → Paused, Completed, NeedsReview, Discarded
  Paused      → In-Progress, Discarded
  Completed   → (최종)
  NeedsReview → Pending, Discarded   (사람이 재큐/폐기 가능)
  Discarded   → (최종)
"""
from __future__ import annotations

from .models import TaskState

ALLOWED: dict[TaskState, set[TaskState]] = {
    # 착수 전 질문 대기(§34.4) — 스케줄 비대상. 답변/타임아웃으로 Pending, 취소로 Discarded.
    TaskState.AWAITING_INPUT: {TaskState.PENDING, TaskState.DISCARDED},
    TaskState.PENDING: {TaskState.IN_PROGRESS, TaskState.DISCARDED},
    TaskState.IN_PROGRESS: {TaskState.PAUSED, TaskState.COMPLETED,
                            TaskState.NEEDS_REVIEW, TaskState.DISCARDED},
    TaskState.PAUSED: {TaskState.IN_PROGRESS, TaskState.DISCARDED},
    TaskState.COMPLETED: set(),
    # NeedsReview 는 최종에 가깝지만, 사람이 보완 재시도(재큐)하거나 폐기할 수 있게 연다.
    TaskState.NEEDS_REVIEW: {TaskState.PENDING, TaskState.DISCARDED},
    TaskState.DISCARDED: set(),
}


class InvalidTransition(Exception):
    def __init__(self, frm: TaskState, to: TaskState):
        super().__init__(f"허용되지 않는 상태 전이: {frm.value} → {to.value}")
        self.frm = frm
        self.to = to


def can_transition(frm: TaskState, to: TaskState) -> bool:
    return to in ALLOWED.get(frm, set())


def assert_transition(frm: TaskState, to: TaskState) -> None:
    if not can_transition(frm, to):
        raise InvalidTransition(frm, to)
