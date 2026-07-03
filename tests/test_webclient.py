"""§35.9 M6-R3 테스트 — 웹 챗 페이지 · webhook 알림 · /v1/runs delivery."""
from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from alphred.db import Store, new_id
from alphred.gateway import create_app
from alphred.models import TaskState
from alphred.queue_manager import QueueManager


class _Client:
    def __init__(self, output="DONE"):
        self.output = output
        self.status = {}

    def start_run(self, prompt, **kw):
        rid = "run_" + new_id()[:8]
        self.status[rid] = {"status": "completed", "output": self.output}
        return rid

    def get_run(self, rid):
        return self.status.get(rid, {"status": "running"})

    def stop_run(self, rid):
        self.status[rid] = {"status": "cancelled"}
        return {}

    def close(self):
        pass


@pytest.fixture
def app(tmp_path):
    store = Store(tmp_path / "g.db")
    mgr = QueueManager(store, _Client(), tmp_path / "QUEUE.MD")
    app = create_app(mgr=mgr, scheduler_interval=3600)
    with TestClient(app) as tc:
        yield tc, mgr


# ---- 웹 챗 페이지(모드 c) ----
def test_chat_page_served(app):
    tc, _ = app
    r = tc.get("/chat")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    # 페이지가 실제 사용하는 API 계약 문자열 존재(회귀 가드)
    for needle in ("/chat/stream", "needs_input", "/answers", "Alphred Chat"):
        assert needle in r.text


# ---- webhook(§35.2) ----
def _wait_for(cond, timeout=5.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_webhook_fired_on_completion(tmp_path, monkeypatch):
    import alphred.queue_manager as qm
    posts = []

    class _FakeHttpx:
        @staticmethod
        def post(url, json=None, timeout=None):
            posts.append((url, json))

    monkeypatch.setattr(qm.httpx, "post", _FakeHttpx.post)
    mgr = QueueManager(Store(tmp_path / "q.db"), _Client(output="결과물"),
                       tmp_path / "Q.MD")
    t = mgr.submit("전체 코드베이스 리팩토링 해줘",
                   delivery={"webhook": "http://device.local/hook"})
    mgr.tick()   # 시작
    mgr.tick()   # 완료 → webhook 스레드 발사
    assert mgr.store.get(t.id).state == TaskState.COMPLETED.value
    assert _wait_for(lambda: posts)
    url, payload = posts[0]
    assert url == "http://device.local/hook"
    assert payload["id"] == t.id and payload["state"] == "Completed"
    assert payload["result"] == "결과물" and payload["verify"]["passed"] is True


def test_webhook_fired_on_discard_and_invalid_url_ignored(tmp_path, monkeypatch):
    import alphred.queue_manager as qm
    posts = []
    monkeypatch.setattr(qm.httpx, "post",
                        lambda url, json=None, timeout=None: posts.append(url))
    mgr = QueueManager(Store(tmp_path / "q.db"), _Client(), tmp_path / "Q.MD")
    t = mgr.submit("전체 코드베이스 리팩토링 해줘",
                   delivery={"webhook": "http://x/hook"})
    mgr.discard(t.id)
    assert _wait_for(lambda: posts)
    # 잘못된 URL/webhook 없음 → 무시(발사 없음)
    t2 = mgr.submit("모든 테스트 파일 점검해줘", delivery={"webhook": "not-a-url"})
    mgr.discard(t2.id)
    t3 = mgr.submit("전체 프로젝트 분석해줘")
    mgr.discard(t3.id)
    time.sleep(0.3)
    assert len(posts) == 1


def test_webhook_failure_does_not_affect_task(tmp_path, monkeypatch):
    import alphred.queue_manager as qm

    def boom(url, json=None, timeout=None):
        raise RuntimeError("network down")

    monkeypatch.setattr(qm.httpx, "post", boom)
    monkeypatch.setattr(qm.time, "sleep", lambda s: None)   # 재시도 대기 생략
    mgr = QueueManager(Store(tmp_path / "q.db"), _Client(), tmp_path / "Q.MD")
    t = mgr.submit("전체 코드베이스 리팩토링 해줘",
                   delivery={"webhook": "http://x/hook"})
    mgr.tick()
    mgr.tick()
    assert mgr.store.get(t.id).state == TaskState.COMPLETED.value   # 상태 무영향


# ---- /v1/runs delivery 수용 ----
def test_runs_accepts_delivery(app):
    tc, mgr = app
    r = tc.post("/v1/runs", json={"input": "전체 코드베이스 분석 보고서",
                                  "delivery": {"webhook": "http://dev.local/hook"}})
    assert r.status_code == 202
    t = mgr.store.get(r.json()["run_id"])
    assert json.loads(t.delivery)["webhook"] == "http://dev.local/hook"