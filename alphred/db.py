"""영속화 계층 — SQLite 기반 작업 저장소(SSOT).

상태 전이는 트랜잭션으로 원자적이며, 상태머신 규칙을 강제하고 감사 로그를 남긴다.
추후 Postgres 이관을 위해 Store 인터페이스로 캡슐화한다.
"""
from __future__ import annotations

import dataclasses
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .models import Task, TaskState
from .state_machine import assert_transition

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
  id            TEXT PRIMARY KEY,
  source        TEXT,
  kind          TEXT,
  priority      INTEGER NOT NULL DEFAULT 5,
  state         TEXT NOT NULL,
  prompt        TEXT,
  hermes_run_id TEXT,
  response_id   TEXT,
  conversation_history TEXT,
  session_key   TEXT,
  delivery      TEXT,
  result        TEXT,
  classify_reason TEXT,
  depth         TEXT,
  verify_report TEXT,
  verify_attempts INTEGER NOT NULL DEFAULT 0,
  verify_feedback TEXT,
  plan          TEXT,
  plan_progress INTEGER NOT NULL DEFAULT 0,
  plan_activity TEXT,
  paused_reason TEXT,
  error         TEXT,
  retries       INTEGER NOT NULL DEFAULT 0,
  retry_not_before TEXT,
  created_at    TEXT,
  updated_at    TEXT,
  started_at    TEXT,
  finished_at   TEXT
);
CREATE TABLE IF NOT EXISTS task_events (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id   TEXT NOT NULL,
  from_state TEXT,
  to_state  TEXT,
  reason    TEXT,
  at        TEXT
);
CREATE TABLE IF NOT EXISTS intent_log (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  at         TEXT,
  source     TEXT,
  engine     TEXT,
  kind       TEXT,
  priority   INTEGER,
  depth      TEXT,
  confidence INTEGER,
  reason     TEXT,
  prompt     TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_state_priority ON tasks(state, priority DESC, created_at ASC);
"""

_TASK_COLS = list(Task.__dataclass_fields__.keys())


def _col_ddl(field: dataclasses.Field) -> str:
    """dataclass 필드 → SQLite 컬럼 DDL(타입/기본값). int 만 NOT NULL DEFAULT, 그 외 TEXT."""
    if field.type in ("int", int):
        default = field.default if field.default is not dataclasses.MISSING else 0
        return f"INTEGER NOT NULL DEFAULT {default}"
    return "TEXT"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id() -> str:
    return uuid.uuid4().hex


class Store:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: 게이트웨이 요청 핸들러(스레드풀)와 스케줄러 스레드가
        # 같은 연결을 공유한다. 쓰기는 QueueManager 의 락으로 직렬화된다.
        self._conn = sqlite3.connect(str(self.db_path), isolation_level=None,
                                     check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Task dataclass 를 기준으로 기존 DB 에 누락된 컬럼을 자동 추가한다.

        삽입 컬럼(_TASK_COLS)·스키마·마이그레이션이 모두 dataclass 한 곳에서
        파생되므로, Task 에 필드만 추가하면 스키마가 자동으로 따라온다(drift 방지).
        """
        have = {r["name"] for r in self._conn.execute("PRAGMA table_info(tasks)")}
        for name, field in Task.__dataclass_fields__.items():
            if name not in have:
                self._conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} {_col_ddl(field)}")

    def close(self) -> None:
        self._conn.close()

    # ---- 생성/조회 ----
    def create(self, task: Task) -> Task:
        task.created_at = task.created_at or _now()
        task.updated_at = _now()
        row = task.to_row()
        cols = ", ".join(_TASK_COLS)
        ph = ", ".join("?" for _ in _TASK_COLS)
        self._conn.execute(
            f"INSERT INTO tasks ({cols}) VALUES ({ph})",
            [row[c] for c in _TASK_COLS],
        )
        self._log(task.id, None, task.state, "created")
        return task

    def get(self, task_id: str) -> Task | None:
        cur = self._conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,))
        r = cur.fetchone()
        return self._to_task(r) if r else None

    def list(self, states: list[str] | None = None) -> list[Task]:
        q = "SELECT * FROM tasks"
        args: list = []
        if states:
            q += " WHERE state IN (%s)" % ", ".join("?" for _ in states)
            args = list(states)
        q += " ORDER BY (state='In-Progress') DESC, priority DESC, created_at ASC"
        return [self._to_task(r) for r in self._conn.execute(q, args).fetchall()]

    def next_pending(self) -> Task | None:
        """선점 도전자(새 작업) 후보 — Pending 중 최고 우선순위."""
        cur = self._conn.execute(
            "SELECT * FROM tasks WHERE state=? ORDER BY priority DESC, created_at ASC LIMIT 1",
            (TaskState.PENDING.value,),
        )
        r = cur.fetchone()
        return self._to_task(r) if r else None

    # 사용자가 명시적으로 보류한 작업은 자동 재개 대상에서 제외한다.
    USER_HOLD = "user hold"

    def next_runnable(self, now: str | None = None) -> Task | None:
        """슬롯에 올릴 다음 작업 — Pending + 자동재개 가능한 Paused 중 최고 우선순위.

        백오프 중(retry_not_before > now)인 작업은 제외한다.
        """
        now = now or _now()
        cur = self._conn.execute(
            "SELECT * FROM tasks "
            "WHERE (state=? OR (state=? AND COALESCE(paused_reason,'')<>?)) "
            "  AND (retry_not_before IS NULL OR retry_not_before<=?) "
            "ORDER BY priority DESC, created_at ASC LIMIT 1",
            (TaskState.PENDING.value, TaskState.PAUSED.value, self.USER_HOLD, now),
        )
        r = cur.fetchone()
        return self._to_task(r) if r else None

    def in_progress(self) -> list[Task]:
        return self.list(states=[TaskState.IN_PROGRESS.value])

    def recent_finished(self, session_key: str, limit: int = 5) -> list[Task]:
        """세션의 최근 종결(Completed/NeedsReview) 작업 — §40 원장용, 최신 우선.

        Discarded 는 제외(사용자가 버린 작업은 후속 참조 가치가 낮고 오염 위험).
        """
        if not session_key:
            return []
        cur = self._conn.execute(
            "SELECT * FROM tasks WHERE session_key=? AND state IN (?, ?) "
            "ORDER BY COALESCE(finished_at, updated_at) DESC LIMIT ?",
            (session_key, TaskState.COMPLETED.value, TaskState.NEEDS_REVIEW.value,
             int(limit)),
        )
        return [self._to_task(r) for r in cur.fetchall()]

    def events(self, task_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT from_state,to_state,reason,at FROM task_events WHERE task_id=? ORDER BY id",
            (task_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ---- 변경 ----
    def update_fields(self, task_id: str, **fields) -> None:
        if not fields:
            return
        fields["updated_at"] = _now()
        sets = ", ".join(f"{k}=?" for k in fields)
        self._conn.execute(
            f"UPDATE tasks SET {sets} WHERE id=?", [*fields.values(), task_id]
        )

    def set_priority(self, task_id: str, priority: int) -> None:
        if not 1 <= priority <= 10:
            raise ValueError("priority 는 1..10 범위여야 합니다")
        self.update_fields(task_id, priority=priority)

    def transition(self, task_id: str, to: TaskState, reason: str = "", **extra) -> Task:
        """상태머신을 강제하는 원자적 전이 + 감사 로그."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            r = self._conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if r is None:
                raise KeyError(task_id)
            frm = TaskState(r["state"])
            assert_transition(frm, to)
            stamp: dict = {"state": to.value, "updated_at": _now()}
            if to == TaskState.IN_PROGRESS and not r["started_at"]:
                stamp["started_at"] = _now()
            if to.is_terminal:
                stamp["finished_at"] = _now()
            stamp.update(extra)
            sets = ", ".join(f"{k}=?" for k in stamp)
            self._conn.execute(
                f"UPDATE tasks SET {sets} WHERE id=?", [*stamp.values(), task_id]
            )
            self._conn.execute(
                "INSERT INTO task_events (task_id,from_state,to_state,reason,at) VALUES (?,?,?,?,?)",
                (task_id, frm.value, to.value, reason, _now()),
            )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        return self.get(task_id)  # type: ignore[return-value]

    # ---- 영구 삭제 ----
    def delete(self, task_id: str) -> bool:
        """작업 행과 그 이벤트를 영구 삭제한다(원자적). 존재했으면 True."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            cur = self._conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
            self._conn.execute("DELETE FROM task_events WHERE task_id=?", (task_id,))
            self._conn.execute("COMMIT")
            return cur.rowcount > 0
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def delete_by_states(self, states: list[str]) -> int:
        """주어진 상태의 작업들을 (이벤트 포함) 영구 삭제하고 삭제 건수를 반환한다."""
        if not states:
            return 0
        ph = ", ".join("?" for _ in states)
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            ids = [r["id"] for r in self._conn.execute(
                f"SELECT id FROM tasks WHERE state IN ({ph})", states).fetchall()]
            if ids:
                iph = ", ".join("?" for _ in ids)
                self._conn.execute(f"DELETE FROM task_events WHERE task_id IN ({iph})", ids)
                self._conn.execute(f"DELETE FROM tasks WHERE id IN ({iph})", ids)
            self._conn.execute("COMMIT")
            return len(ids)
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    # ---- §34.7 의도 판정 텔레메트리 ----
    def log_intent(self, *, source: str, engine: str, kind: str, priority: int,
                   depth: str | None = None, confidence: int | None = None,
                   reason: str = "", prompt: str = "") -> None:
        """분류 판정 1건 기록 — 의도 정확도의 사후 측정 근거(엔진 라벨·근거·입력 요약)."""
        self._conn.execute(
            "INSERT INTO intent_log (at,source,engine,kind,priority,depth,confidence,"
            "reason,prompt) VALUES (?,?,?,?,?,?,?,?,?)",
            (_now(), source, engine, kind, int(priority) if priority is not None else None,
             depth, confidence, (reason or "")[:200], (prompt or "")[:200]),
        )

    def intent_stats(self) -> dict:
        """엔진별 판정 건수 집계(doctor/평가용, 무비용)."""
        rows = self._conn.execute(
            "SELECT engine, kind, COUNT(*) AS n FROM intent_log GROUP BY engine, kind"
        ).fetchall()
        out: dict = {}
        for r in rows:
            e = out.setdefault(r["engine"] or "?", {})
            e[r["kind"] or "?"] = r["n"]
        return out

    # ---- 내부 ----
    def _log(self, task_id: str, frm, to, reason: str) -> None:
        self._conn.execute(
            "INSERT INTO task_events (task_id,from_state,to_state,reason,at) VALUES (?,?,?,?,?)",
            (task_id, getattr(frm, "value", frm), getattr(to, "value", to), reason, _now()),
        )

    @staticmethod
    def _to_task(r: sqlite3.Row) -> Task:
        return Task(**{k: r[k] for k in r.keys() if k in Task.__dataclass_fields__})
