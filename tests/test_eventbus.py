"""§33 실행 이벤트 팬아웃 버스 + 라이브 스트림 엔드포인트."""
from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from alphred.db import Store, new_id
from alphred.eventbus import RunEventBus
from alphred.gateway import create_app
from alphred.queue_manager import QueueManager


def test_event_bus_fanout_and_isolation():
    """한 run 의 여러 구독자에게 팬아웃, 다른 run 에는 안 감, unsubscribe 정리."""
    async def go():
        bus = RunEventBus()
        q1 = bus.subscribe("t1")
        q2 = bus.subscribe("t1")
        bus.publish("t1", {"event": "tool.started", "tool": "x"})
        bus.publish("t2", {"event": "other"})            # 다른 run
        e1 = await asyncio.wait_for(q1.get(), 1)
        e2 = await asyncio.wait_for(q2.get(), 1)
        assert e1["tool"] == "x" and e2["tool"] == "x"   # 둘 다 받음(팬아웃)
        assert q1.empty() and q2.empty()                 # t2 이벤트는 안 옴
        bus.unsubscribe("t1", q1)
        assert bus.has_subscribers("t1") is True         # q2 남음
        bus.unsubscribe("t1", q2)
        assert bus.has_subscribers("t1") is False
        bus.close("t3")                                  # 구독자 없어도 무해

    asyncio.run(go())


class _Fake:
    def start_run(self, prompt, **kw):
        return "run_" + new_id()[:8]

    def get_run(self, run_id):
        return {"status": "running"}

    def close(self):
        pass


def test_queue_stream_static_when_not_running(tmp_path):
    """실행 중이 아닌 작업의 /queue/{id}/stream 은 상태만 주고 즉시 닫힘(무한 대기 X)."""
    store = Store(tmp_path / "q.db")
    mgr = QueueManager(store, _Fake(), tmp_path / "QUEUE.MD", event_bus=RunEventBus())
    t = mgr.submit("전체 코드베이스 리팩터링 대규모 작업", kind="heavy", priority=5)  # Pending
    mgr.set_halted(True, "test")   # 스케줄러가 작업을 시작하지 않게 → Pending 유지(정적 경로)
    with TestClient(create_app(mgr=mgr, scheduler_interval=3600)) as tc:
        r = tc.get(f"/queue/{t.id}/stream")
        assert r.status_code == 200
        assert "done" in r.text and "Pending" in r.text
        assert tc.get("/queue/nonexistent/stream").status_code == 404
