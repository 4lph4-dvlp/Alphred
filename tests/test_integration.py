"""D5 통합 테스트 (§12.4) — 데몬 조립부(업스트림 게이팅→스케줄러→완주) 회귀 방지.

§12.1~§12.3 에서 데몬 런타임 조립부가 멈추는 버그(pause_scheduling 갇힘)가 있었다.
D1 수정으로 `tick()` 이 매 틱 `ensure_upstream()` 을 직접 호출하는 단일 경로가 됐다.
이 테스트는 실제 네트워크/Hermes 없이 그 조립부를 끝까지 구동해 회귀를 막는다:
  - 업스트림 가동 시 heavy 제출 → tick → 완주(Completed)
  - 업스트림 미가동 시 보류(Pending 유지, 폐기 안 함) → 복구 시 완주
  - ensure_upstream 이 매 틱 호출되는지(단일 경로 보장)
  - 크래시 복구(고아 In-Progress 정리)
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from alphred.db import Store
from alphred.gateway import create_app
from alphred.models import TaskState
from alphred.queue_manager import QueueManager

from test_gateway import FakeClient


def _mgr(tmp_path, ensure_upstream=None):
    store = Store(tmp_path / "i.db")
    fake = FakeClient()
    mgr = QueueManager(store, fake, tmp_path / "Q.MD", ensure_upstream=ensure_upstream)
    return store, fake, mgr


def test_daemon_assembly_heavy_runs_to_completion(tmp_path):
    """게이트웨이로 heavy 제출 → 수동 tick → 완주. 조립부 정상 작동."""
    calls = {"n": 0}

    def ensure_upstream():
        calls["n"] += 1
        return True

    store, fake, mgr = _mgr(tmp_path, ensure_upstream)
    app = create_app(mgr=mgr, scheduler_interval=3600)  # 자동 스케줄 끄고 수동 구동
    with TestClient(app) as tc:
        rid = tc.post("/v1/runs", json={"input": "전체 코드베이스를 대규모로 리팩토링 분석"}).json()["run_id"]
        assert mgr.get(rid).state == TaskState.PENDING.value
        mgr.tick()  # 단일 경로: ensure_upstream→finalize→start
        assert mgr.get(rid).state in (TaskState.IN_PROGRESS.value, TaskState.COMPLETED.value)
        mgr.tick()  # FakeClient.start_run 은 즉시 completed → 다음 틱에 마감
        assert mgr.get(rid).state == TaskState.COMPLETED.value
        assert mgr.get(rid).result.startswith("DONE:")
    assert calls["n"] >= 2  # 매 틱마다 업스트림 게이트가 호출됨(단일 경로)


def test_upstream_down_holds_then_recovers(tmp_path):
    """업스트림 미가동이면 작업을 폐기하지 않고 Pending 유지 → 복구되면 완주."""
    state = {"up": False}
    store, fake, mgr = _mgr(tmp_path, lambda: state["up"])
    t = mgr.submit("대규모 데이터셋 일괄 마이그레이션", priority=5, kind="heavy")
    mgr.tick()  # 업스트림 down → 보류
    assert mgr.get(t.id).state == TaskState.PENDING.value  # 폐기 아님
    state["up"] = True
    mgr.tick()  # 복구 → 시작
    mgr.tick()  # 마감
    assert mgr.get(t.id).state == TaskState.COMPLETED.value
    store.close()


def test_crash_recovery_finalizes_orphan(tmp_path):
    """재기동 시 In-Progress 로 남은 고아 작업을, 끝났으면 마감한다(QA-7.7)."""
    store, fake, mgr = _mgr(tmp_path, lambda: True)
    t = mgr.submit("대규모 분석 작업 백그라운드", priority=4, kind="heavy")
    mgr.tick()
    assert mgr.get(t.id).state == TaskState.IN_PROGRESS.value
    # Hermes run 은 사실 완료 상태(FakeClient) → recover 가 마감해야 함
    recovered = mgr.recover()
    assert recovered == 1
    assert mgr.get(t.id).state == TaskState.COMPLETED.value
    store.close()


def test_crash_recovery_requeues_dead_run(tmp_path):
    """고아 작업의 run 이 죽어있으면(취소/실패) 재큐(Paused 자동재개)한다."""
    store, fake, mgr = _mgr(tmp_path, lambda: True)
    t = mgr.submit("대규모 분석 작업 백그라운드2", priority=4, kind="heavy")
    mgr.tick()
    assert mgr.get(t.id).state == TaskState.IN_PROGRESS.value
    fake.get_run = lambda rid: {"status": "cancelled"}  # run 사망 시뮬
    recovered = mgr.recover()
    assert recovered == 1
    assert mgr.get(t.id).state == TaskState.PAUSED.value  # 재큐 대기
    store.close()
