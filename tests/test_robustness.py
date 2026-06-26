"""Phase 4 (1) 운영 안정성 — transient 재큐(QA-4.6) + 크래시 복구(QA-7.7)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from alphred.db import Store, new_id
from alphred.models import Task, TaskState
from alphred.queue_manager import QueueManager, is_transient_error


def test_is_transient():
    assert is_transient_error("HTTP 429: RESOURCE_EXHAUSTED quota")
    assert is_transient_error("connection reset by peer")
    assert not is_transient_error("invalid argument: bad prompt")
    assert not is_transient_error(None)


class FailClient:
    """항상 실패 상태를 반환. error 문자열을 주입해 transient 여부를 제어."""
    def __init__(self, error="HTTP 429 RESOURCE_EXHAUSTED"):
        self.error = error
        self.started = 0

    def start_run(self, prompt, **kw):
        self.started += 1
        return "run_" + new_id()[:6]

    def get_run(self, run_id):
        return {"status": "failed", "error": self.error}

    def stop_run(self, run_id):
        return {"status": "stopping"}

    def close(self):
        pass


def make(tmp_path, client, **kw):
    s = Store(tmp_path / "q.db")
    return QueueManager(s, client, tmp_path / "Q.MD", retry_base_seconds=0.0, **kw), s


def test_transient_failure_requeues_then_discards(tmp_path):
    client = FailClient("HTTP 429 RESOURCE_EXHAUSTED")
    mgr, store = make(tmp_path, client, max_retries=2)
    t = mgr.submit("작업", priority=5)

    # tick1: 시작 → 실패 감지는 다음 tick. retry_base=0 이라 즉시 재선택 가능.
    mgr.tick()                                  # start (In-Progress)
    assert store.get(t.id).state == TaskState.IN_PROGRESS.value
    mgr.tick()                                  # finalize: 실패→retry1(Paused), 그리고 재시작
    assert store.get(t.id).retries >= 1
    # 재시도 한도까지 돌리면 결국 Discarded
    for _ in range(8):
        mgr.tick()
    final = store.get(t.id)
    assert final.state == TaskState.DISCARDED.value
    assert final.retries == 2                   # max_retries 만큼만 재시도
    assert client.started >= 3                  # 최초 + 재시도 2회


def test_permanent_failure_discards_immediately(tmp_path):
    client = FailClient("invalid argument: malformed request")
    mgr, store = make(tmp_path, client, max_retries=3)
    t = mgr.submit("작업", priority=5)
    mgr.tick(); mgr.tick()
    f = store.get(t.id)
    assert f.state == TaskState.DISCARDED.value
    assert f.retries == 0                        # 비-transient 는 재시도 없음


def test_backoff_blocks_immediate_resume(tmp_path):
    client = FailClient("429 quota")
    mgr, store = make(tmp_path, client, max_retries=5)
    mgr.retry_base_seconds = 60.0               # 큰 백오프
    t = mgr.submit("작업", priority=5)
    mgr.tick()  # start
    mgr.tick()  # fail → retry1 with not_before = +60s
    assert store.get(t.id).state == TaskState.PAUSED.value
    # 백오프 중에는 재선택되지 않아야 함
    assert store.next_runnable() is None


def test_recover_orphan_in_progress(tmp_path):
    """프로세스 사망으로 In-Progress 로 남은 작업을 재큐한다."""
    class LostClient(FailClient):
        def get_run(self, run_id):
            raise RuntimeError("run unknown")   # Hermes 가 모름
    client = LostClient()
    mgr, store = make(tmp_path, client)
    # In-Progress 고아 상태를 직접 만든다
    t = Task(id=new_id(), prompt="고아", priority=5, state=TaskState.IN_PROGRESS.value,
             hermes_run_id="run_dead")
    store.create(t)
    n = mgr.recover()
    assert n == 1
    rec = store.get(t.id)
    assert rec.state == TaskState.PAUSED.value
    assert rec.paused_reason == "recovered"
    # 복구된 작업은 다시 실행 대상이 된다
    assert store.next_runnable().id == t.id


def test_recover_completed_run(tmp_path):
    """사망 사이에 Hermes 가 완료한 경우 결과로 마감한다."""
    class DoneClient(FailClient):
        def get_run(self, run_id):
            return {"status": "completed", "output": "RECOVERED_RESULT"}
    mgr, store = make(tmp_path, DoneClient())
    t = Task(id=new_id(), prompt="x", priority=5, state=TaskState.IN_PROGRESS.value,
             hermes_run_id="run_x")
    store.create(t)
    mgr.recover()
    assert store.get(t.id).state == TaskState.COMPLETED.value
    assert store.get(t.id).result == "RECOVERED_RESULT"
