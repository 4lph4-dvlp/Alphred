"""Cron 인터셉트 테스트 (기획 5) — 매처 + 큐 편입 + 중복 방지."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from alphred.cron_intercept import CronIntercept, matches
from alphred.db import Store, new_id
from alphred.models import TaskSource, TaskState
from alphred.queue_manager import QueueManager


# ---- cron 매처 ----
def dt(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


def test_matches_basic():
    assert matches("0 9 * * *", dt(2026, 6, 20, 9, 0))
    assert not matches("0 9 * * *", dt(2026, 6, 20, 9, 1))
    assert not matches("0 9 * * *", dt(2026, 6, 20, 10, 0))


def test_matches_weekday_range():
    # 2026-06-22 는 월요일 → 평일(1-5) 매치
    assert matches("0 9 * * 1-5", dt(2026, 6, 22, 9, 0))
    # 2026-06-21 은 일요일 → 평일 아님
    assert not matches("0 9 * * 1-5", dt(2026, 6, 21, 9, 0))


def test_matches_step_and_list():
    assert matches("*/15 * * * *", dt(2026, 6, 20, 12, 30))
    assert not matches("*/15 * * * *", dt(2026, 6, 20, 12, 31))
    assert matches("0 9,18 * * *", dt(2026, 6, 20, 18, 0))


def test_matches_sunday_both_7_and_0():
    # 일요일: cron 0 또는 7
    assert matches("0 0 * * 0", dt(2026, 6, 21, 0, 0))
    assert matches("0 0 * * 7", dt(2026, 6, 21, 0, 0))


# ---- 인터셉트 ----
class FakeClient:
    def start_run(self, p, **k): return "run_" + new_id()[:6]
    def get_run(self, r): return {"status": "running"}
    def stop_run(self, r): return {"status": "stopping"}
    def close(self): pass


def setup(tmp_path, jobs):
    jobs_path = tmp_path / "jobs.json"
    jobs_path.write_text(json.dumps({"jobs": jobs}), encoding="utf-8")
    mgr = QueueManager(Store(tmp_path / "q.db"), FakeClient(), tmp_path / "Q.MD")
    cron = CronIntercept(mgr, jobs_path, tmp_path / "cron_state.json")
    return mgr, cron


def test_intercept_enqueues_due_job(tmp_path):
    mgr, cron = setup(tmp_path, [
        {"id": "daily", "schedule": "0 9 * * *", "prompt": "아침 브리핑", "priority": 4},
    ])
    ids = cron.tick(dt(2026, 6, 20, 9, 0))
    assert len(ids) == 1
    t = mgr.get(ids[0])
    assert t.source == TaskSource.CRON.value
    assert t.state == TaskState.PENDING.value
    assert t.priority == 4


def test_intercept_not_due(tmp_path):
    mgr, cron = setup(tmp_path, [{"id": "daily", "schedule": "0 9 * * *", "prompt": "x"}])
    assert cron.tick(dt(2026, 6, 20, 8, 0)) == []


def test_intercept_dedup_same_minute(tmp_path):
    mgr, cron = setup(tmp_path, [{"id": "daily", "schedule": "0 9 * * *", "prompt": "x"}])
    assert len(cron.tick(dt(2026, 6, 20, 9, 0))) == 1
    # 같은 분에 다시 tick → 중복 등록 안 함
    assert cron.tick(dt(2026, 6, 20, 9, 0)) == []
    # 다음 날 같은 시각 → 다시 등록
    assert len(cron.tick(dt(2026, 6, 21, 9, 0))) == 1


def test_intercept_skips_disabled(tmp_path):
    mgr, cron = setup(tmp_path, [
        {"id": "on", "schedule": "0 9 * * *", "prompt": "a"},
        {"id": "off", "schedule": "0 9 * * *", "prompt": "b", "enabled": False},
        {"id": "paused", "schedule": "0 9 * * *", "prompt": "c", "state": "paused"},
    ])
    ids = cron.tick(dt(2026, 6, 20, 9, 0))
    assert len(ids) == 1


def test_intercept_blocks_lifecycle_job(tmp_path):
    """cron 작업이 라이프사이클 명령이면 안전망이 큐 진입을 막는다(#30719 시너지)."""
    mgr, cron = setup(tmp_path, [
        {"id": "bad", "schedule": "* * * * *", "prompt": "hermes gateway restart"},
    ])
    ids = cron.tick(dt(2026, 6, 20, 9, 0))
    assert ids == []            # 차단되어 큐에 안 들어감
    assert mgr.list() == []


def test_intercept_does_not_record_failed_job_when_other_job_succeeds(tmp_path):
    mgr, cron = setup(tmp_path, [
        {"id": "bad", "schedule": "* * * * *", "prompt": "hermes gateway restart"},
        {"id": "ok", "schedule": "* * * * *", "prompt": "정상 작업"},
    ])
    ids = cron.tick(dt(2026, 6, 20, 9, 0))
    assert len(ids) == 1
    state = json.loads((tmp_path / "cron_state.json").read_text(encoding="utf-8"))
    assert "ok" in state
    assert "bad" not in state


def test_intercept_dict_schedule(tmp_path):
    mgr, cron = setup(tmp_path, [
        {"id": "d", "schedule": {"minute": "0", "hour": "9", "day": "*", "month": "*", "weekday": "*"},
         "prompt": "dict sched"},
    ])
    assert len(cron.tick(dt(2026, 6, 20, 9, 0))) == 1


def test_intercept_ignores_once_schedule(tmp_path):
    """Hermes 'once'(run_at) 작업은 5필드 cron 이 아니므로 매분 편입되면 안 된다(폭주 버그 회귀)."""
    mgr, cron = setup(tmp_path, [
        {"id": "5f79", "name": "once job",
         "schedule": {"kind": "once", "run_at": "2026-06-21T09:05:00+09:00",
                      "display": "once at 2026-06-21 09:05"},
         "prompt": "한 번만 실행", "state": "scheduled"},
    ])
    # 어떤 분에 tick 해도 편입 0건이어야 함
    assert cron.tick(dt(2026, 6, 21, 0, 5)) == []
    assert cron.tick(dt(2026, 6, 21, 0, 6)) == []
    assert mgr.list() == []


def test_intercept_ignores_every_schedule(tmp_path):
    mgr, cron = setup(tmp_path, [
        {"id": "ev", "schedule": {"kind": "every", "interval_seconds": 60}, "prompt": "x"},
    ])
    assert cron.tick(dt(2026, 6, 20, 9, 0)) == []
