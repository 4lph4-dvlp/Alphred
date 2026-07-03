"""QUEUE.MD 투영 — DB(SSOT)를 사람이 읽는 마크다운으로 렌더링한다.

DB 가 단일 진실원천이고 QUEUE.MD 는 항상 그 투영이다(QA-3.4).
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .models import Task, TaskState

_STATE_EMOJI = {
    TaskState.AWAITING_INPUT.value: "❓",
    TaskState.PENDING.value: "⏳",
    TaskState.IN_PROGRESS.value: "▶️",
    TaskState.PAUSED.value: "⏸️",
    TaskState.COMPLETED.value: "✅",
    TaskState.NEEDS_REVIEW.value: "⚠️",
    TaskState.DISCARDED.value: "🗑️",
}

_ACTIVE = [TaskState.IN_PROGRESS.value, TaskState.PAUSED.value, TaskState.PENDING.value,
           TaskState.AWAITING_INPUT.value]
_DONE = [TaskState.COMPLETED.value, TaskState.NEEDS_REVIEW.value, TaskState.DISCARDED.value]


def render(tasks: list[Task]) -> str:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    lines = ["# QUEUE.MD", "", f"_자동 생성됨 (SSOT=alphred.db) · {now}_", ""]

    def table(title: str, rows: list[Task]) -> None:
        lines.append(f"## {title}")
        if not rows:
            lines.append("")
            lines.append("_(없음)_")
            lines.append("")
            return
        lines.append("")
        lines.append("| 우선 | 상태 | Kind | 작업 | ID |")
        lines.append("|---:|:--:|:--:|---|---|")
        for t in rows:
            emoji = _STATE_EMOJI.get(t.state, "")
            prompt = (t.prompt or "").replace("\n", " ").strip()
            if len(prompt) > 60:
                prompt = prompt[:57] + "…"
            lines.append(
                f"| {t.priority} | {emoji} {t.state} | {t.kind} | {prompt} | `{t.id[:8]}` |"
            )
        lines.append("")

    active = [t for t in tasks if t.state in _ACTIVE]
    done = [t for t in tasks if t.state in _DONE]
    table("진행/대기 (Active)", active)
    table("종료 (History)", done[:50])
    return "\n".join(lines) + "\n"


def write(path: Path, tasks: list[Task]) -> None:
    Path(path).write_text(render(tasks), encoding="utf-8")
