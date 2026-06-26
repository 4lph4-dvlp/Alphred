"""Phase 2 선점(Preemption) 테스트 — 제어 가능한 FakeClient 사용 (QA-4)."""
from __future__ import annotations

from alphred.db import Store, new_id
from alphred.models import TaskState
from alphred.queue_manager import QueueManager


class ControlledClient:
    """run 별 상태를 테스트가 직접 제어한다. 기본은 'running'(완료되지 않음)."""
    def __init__(self):
        self.status: dict[str, dict] = {}
        self.started: list[tuple[str, str]] = []   # (run_id, prompt)
        self.stopped: list[str] = []

    def start_run(self, prompt, **kw):
        rid = "run_" + new_id()[:8]
        self.status[rid] = {"status": "running"}
        self.started.append((rid, prompt))
        return rid

    def get_run(self, run_id):
        return self.status.get(run_id, {"status": "running"})

    def stop_run(self, run_id):
        self.stopped.append(run_id)
        self.status[run_id] = {"status": "cancelled"}
        return {"status": "stopping"}

    def complete(self, run_id, output="DONE"):
        self.status[run_id] = {"status": "completed", "output": output}

    def close(self):
        pass


def make(tmp_path, slots=1):
    s = Store(tmp_path / "q.db")
    c = ControlledClient()
    return QueueManager(s, c, tmp_path / "QUEUE.MD", max_slots=slots), s, c


def _state(store, tid):
    return store.get(tid).state


def test_preempt_pause_light_resume(tmp_path):
    """핵심 시나리오: Heavy 진행 중 Light 유입 → Heavy 일시중지 → Light 완료 → Heavy 재개."""
    mgr, store, client = make(tmp_path)

    heavy = mgr.submit("대규모 리팩토링", priority=3)
    mgr.tick()  # Heavy In-Progress
    assert _state(store, heavy.id) == TaskState.IN_PROGRESS.value
    heavy_run = store.get(heavy.id).hermes_run_id

    # Light 유입
    light = mgr.submit("즉시 답변", priority=10)
    mgr.tick()  # 선점: Heavy → Paused, Light In-Progress
    assert _state(store, heavy.id) == TaskState.PAUSED.value
    assert _state(store, light.id) == TaskState.IN_PROGRESS.value
    assert heavy_run in client.stopped            # Heavy run 이 실제로 중단됨
    assert store.get(heavy.id).paused_reason.startswith("preempted")

    # Light 완료
    light_run = store.get(light.id).hermes_run_id
    client.complete(light_run, "LIGHT_DONE")
    mgr.tick()  # Light Completed → Heavy 재개(In-Progress)
    assert _state(store, light.id) == TaskState.COMPLETED.value
    assert _state(store, heavy.id) == TaskState.IN_PROGRESS.value

    # Heavy 완료
    heavy_run2 = store.get(heavy.id).hermes_run_id
    assert heavy_run2 != heavy_run                # 재개 시 새 run 으로 이어감
    client.complete(heavy_run2, "HEAVY_DONE")
    mgr.tick()
    assert _state(store, heavy.id) == TaskState.COMPLETED.value
    assert store.get(heavy.id).result == "HEAVY_DONE"


def test_equal_or_lower_does_not_preempt(tmp_path):
    """동급/저순위 유입은 선점하지 않는다 (QA-4.4)."""
    mgr, store, client = make(tmp_path)
    h = mgr.submit("진행 작업", priority=5)
    mgr.tick()
    assert _state(store, h.id) == TaskState.IN_PROGRESS.value
    same = mgr.submit("동급 작업", priority=5)
    low = mgr.submit("저순위", priority=2)
    mgr.tick()
    assert _state(store, h.id) == TaskState.IN_PROGRESS.value   # 선점 안 됨
    assert _state(store, same.id) == TaskState.PENDING.value
    assert _state(store, low.id) == TaskState.PENDING.value


def test_chained_preemption(tmp_path):
    """연쇄 선점: H(3) 실행 중 L1(7) 선점 → 다시 L2(10) 선점. 이후 우선순위순 재개 (QA-4.3)."""
    mgr, store, client = make(tmp_path)
    h = mgr.submit("H", priority=3); mgr.tick()
    l1 = mgr.submit("L1", priority=7); mgr.tick()
    assert _state(store, h.id) == TaskState.PAUSED.value
    assert _state(store, l1.id) == TaskState.IN_PROGRESS.value
    l2 = mgr.submit("L2", priority=10); mgr.tick()
    assert _state(store, l1.id) == TaskState.PAUSED.value
    assert _state(store, l2.id) == TaskState.IN_PROGRESS.value

    # L2 완료 → 다음은 더 높은 L1(7) 재개, 그 다음 H(3)
    client.complete(store.get(l2.id).hermes_run_id); mgr.tick()
    assert _state(store, l1.id) == TaskState.IN_PROGRESS.value
    client.complete(store.get(l1.id).hermes_run_id); mgr.tick()
    assert _state(store, h.id) == TaskState.IN_PROGRESS.value
    client.complete(store.get(h.id).hermes_run_id); mgr.tick()
    assert all(store.get(t.id).state == TaskState.COMPLETED.value for t in (h, l1, l2))


def test_manual_pause_not_auto_resumed(tmp_path):
    """사용자 명시 일시중지는 자동 재개되지 않는다(user hold)."""
    mgr, store, client = make(tmp_path)
    a = mgr.submit("A", priority=5); mgr.tick()
    mgr.pause(a.id)
    assert _state(store, a.id) == TaskState.PAUSED.value
    # 다른 대기 작업이 없어도 자동 재개되면 안 됨
    mgr.tick()
    assert _state(store, a.id) == TaskState.PAUSED.value
    # resume 허용 후에는 재개
    mgr.resume(a.id); mgr.tick()
    assert _state(store, a.id) == TaskState.IN_PROGRESS.value
