"""Cron 인터셉트 (기획 5) — 주기 작업을 즉시 실행하지 않고 큐의 Pending 으로 편입.

기존 Hermes cron 은 tick 시점에 작업을 *강제 실행*한다. Alphred 는 같은 jobs.json
정의를 읽되, 만료된 작업을 우선순위 큐에 등록만 하여 실시간 대화/선점 규칙의
통제를 받게 한다(Phase 0 결정: Hermes cron 은 끄고 Alphred 가 소유).

의존성 없이 표준 5필드 cron(분 시 일 월 요일)을 직접 해석한다.
- 지원: `*`, `a`, `a-b`, `a-b/s`, `*/s`, 목록 `a,b,c`
- 요일: 0=일 .. 6=토 (7=일도 허용)
실시간 데몬이 매 분 검사하는 표준 cron 의미를 따른다(데몬이 꺼져 있던 구간은
따라잡지 않음 — 일반 cron 과 동일).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from .models import TaskKind, TaskSource

logger = logging.getLogger("alphred.cron")


def _parse_field(field: str, lo: int, hi: int) -> set[int]:
    out: set[int] = set()
    for part in field.split(","):
        part = part.strip()
        step = 1
        if "/" in part:
            part, s = part.split("/", 1)
            step = int(s)
        if part in ("*", ""):
            start, end = lo, hi
        elif "-" in part:
            a, b = part.split("-", 1)
            start, end = int(a), int(b)
        else:
            start = end = int(part)
        for v in range(start, end + 1):
            if (v - start) % step == 0:
                out.add(v)
    return out


def matches(expr: str, dt: datetime) -> bool:
    """주어진 시각이 cron 표현식과 일치하는가(분 단위)."""
    fields = expr.split()
    if len(fields) != 5:
        raise ValueError(f"5필드 cron 표현식이 아님: {expr!r}")
    mins = _parse_field(fields[0], 0, 59)
    hours = _parse_field(fields[1], 0, 23)
    doms = _parse_field(fields[2], 1, 31)
    months = _parse_field(fields[3], 1, 12)
    dows = _parse_field(fields[4], 0, 7)
    if 7 in dows:
        dows.add(0)
    cron_dow = dt.isoweekday() % 7  # Mon=1..Sun=7 → Sun=0..Sat=6
    if dt.minute not in mins or dt.hour not in hours or dt.month not in months:
        return False
    # 표준 cron: dom 과 dow 가 둘 다 제한되면 OR, 하나만 제한되면 그 조건
    dom_restricted = fields[2] != "*"
    dow_restricted = fields[4] != "*"
    dom_ok = dt.day in doms
    dow_ok = cron_dow in dows
    if dom_restricted and dow_restricted:
        return dom_ok or dow_ok
    return dom_ok and dow_ok


def _minute_key(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M")


_CRON_KEYS = ("minute", "hour", "day", "month", "weekday")


def _job_schedule_expr(job: dict) -> str | None:
    """Hermes job 의 schedule 에서 **진짜 5필드 cron** 표현식만 뽑는다.

    주의: Hermes 의 schedule 은 `{kind:"once", run_at:...}` / `{kind:"every", ...}` 같은
    비-cron 형식도 쓴다. 과거엔 누락 cron 필드를 전부 `*` 로 채워 `"* * * * *"`(매분)으로
    오인 → "한 번" 작업이 매분 큐에 편입되는 폭주 버그가 있었다. 이제 진짜 cron 필드(또는
    cron 문자열)가 있을 때만 반환하고, once/every/run_at 형식은 None(cron_intercept 미처리)으로 둔다.
    """
    sched = job.get("schedule")
    if isinstance(sched, str) and len(sched.split()) == 5:
        return sched
    if isinstance(sched, dict):
        kind = str(sched.get("kind", "")).strip().lower()
        if kind and kind not in ("cron", "crontab"):
            return None  # once/every/interval 등은 5필드 cron 이 아님 → 미처리
        c = sched.get("cron") or sched.get("expr")
        if isinstance(c, str) and len(c.split()) == 5:
            return c
        if any(k in sched for k in _CRON_KEYS):  # 명시적 cron 필드가 있을 때만
            return " ".join(str(sched.get(k, "*")) for k in _CRON_KEYS)
    return None


class CronIntercept:
    def __init__(self, mgr, jobs_path: Path, state_path: Path, default_priority: int = 4):
        self.mgr = mgr
        self.jobs_path = Path(jobs_path)
        self.state_path = Path(state_path)
        self.default_priority = default_priority

    def _load_jobs(self) -> list[dict]:
        try:
            data = json.loads(self.jobs_path.read_text(encoding="utf-8"))
        except Exception:
            return []
        jobs = data.get("jobs", data) if isinstance(data, dict) else data
        return [j for j in jobs if isinstance(j, dict)]

    def _load_state(self) -> dict:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_state(self, state: dict) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps(state), encoding="utf-8")
        except Exception:
            logger.warning("cron state 저장 실패")

    @staticmethod
    def _enabled(job: dict) -> bool:
        if job.get("enabled") is False:
            return False
        state = str(job.get("state", "")).strip().lower()
        return state in ("", "scheduled", "active", "enabled")

    def tick(self, now: datetime | None = None) -> list[str]:
        """만료된 cron 작업을 큐에 편입한다. 등록된 task id 목록 반환."""
        now = (now or datetime.now(timezone.utc)).replace(second=0, microsecond=0)
        key = _minute_key(now)
        state = self._load_state()
        enqueued: list[str] = []
        for job in self._load_jobs():
            jid = str(job.get("id") or job.get("name") or "")
            if not jid or not self._enabled(job):
                continue
            expr = _job_schedule_expr(job)
            if not expr:
                continue
            try:
                due = matches(expr, now)
            except ValueError:
                logger.warning("cron 표현식 해석 실패 job=%s expr=%r", jid, expr)
                continue
            if not due or state.get(jid) == key:
                continue  # 미도래 또는 이미 이번 분에 등록함(중복 방지)
            prompt = job.get("prompt") or job.get("name") or ""
            prio = int(job.get("priority", self.default_priority))
            try:
                task = self.mgr.submit(prompt, source=TaskSource.CRON.value,
                                       priority=prio, kind=TaskKind.HEAVY.value)
                enqueued.append(task.id)
                logger.info("cron 편입: job=%s → task=%s prio=%s", jid, task.id[:8], prio)
                state[jid] = key
            except Exception as e:
                logger.warning("cron 편입 실패 job=%s: %s", jid, e)
        if enqueued:
            self._save_state(state)
        return enqueued
