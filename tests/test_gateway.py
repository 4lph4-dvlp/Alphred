"""Phase 3 게이트웨이 테스트 — FastAPI TestClient + FakeClient (네트워크 불필요)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from alphred.db import Store, new_id
from alphred.gateway import Scheduler, create_app
from alphred.models import TaskState
from alphred.queue_manager import QueueManager


class FakeClient:
    def __init__(self):
        self.status = {}
        self.chat_calls = 0
        self.respond_calls = 0
        self.stopped = []

    def start_run(self, prompt, **kw):
        rid = "run_" + new_id()[:8]
        self.status[rid] = {"status": "completed", "output": f"DONE:{prompt[:8]}"}
        return rid

    def get_run(self, run_id):
        return self.status.get(run_id, {"status": "running"})

    def stop_run(self, run_id):
        self.stopped.append(run_id)
        self.status[run_id] = {"status": "cancelled"}
        return {"status": "stopping"}

    def chat_completion(self, body):
        self.chat_calls += 1
        return {"id": "chatcmpl-x", "object": "chat.completion",
                "choices": [{"message": {"role": "assistant", "content": "LIGHT_ANSWER"}}]}

    def respond(self, prompt, **kw):
        self.respond_calls += 1
        return {"id": "resp_x", "object": "response", "output_text": "RESP_ANSWER"}

    def respond_passthrough(self, body):
        self.respond_calls += 1
        self.last_respond_body = body
        return {"id": "resp_x", "object": "response", "output_text": "RESP_ANSWER"}

    def models(self):
        return {"object": "list", "data": [{"id": "hermes-agent"}]}

    def skills(self):
        return {"object": "list", "data": [
            {"name": "research", "description": "리서치", "category": "research"},
            {"name": "nano-pdf", "description": "PDF 편집", "category": "productivity"},
        ]}

    def close(self):
        pass


@pytest.fixture
def app_client(tmp_path):
    store = Store(tmp_path / "g.db")
    fake = FakeClient()
    mgr = QueueManager(store, fake, tmp_path / "QUEUE.MD")
    # 스케줄러가 테스트 중 끼어들지 않도록 간격 크게
    app = create_app(mgr=mgr, scheduler_interval=3600)
    with TestClient(app) as tc:
        yield tc, mgr, fake


def test_dashboard_served(app_client):
    tc, mgr, fake = app_client
    r = tc.get("/")
    assert r.status_code == 200
    assert "Alphred Queue" in r.text
    assert "text/html" in r.headers["content-type"]


def test_models_proxy(app_client):
    tc, mgr, fake = app_client
    r = tc.get("/v1/models")
    assert r.status_code == 200
    assert r.json()["data"][0]["id"] == "hermes-agent"


def test_runs_view_includes_depth(app_client):
    """heavy 작업 제출 시 task_view 에 §21 심화도(depth)가 노출된다."""
    tc, mgr, fake = app_client
    r = tc.post("/v1/runs", json={"input": "보고서를 만들어줘"},
                headers={"X-Alphred-Kind": "heavy"})
    rid = r.json()["run_id"]
    d = tc.get(f"/queue/{rid}").json()
    assert d["depth"] in ("low", "mid", "high")
    assert "verify_report" in d


def test_runs_accepts_session_id(app_client):
    """API /v1/runs 가 session_id 를 받아 작업 세션으로 보존(멀티턴 연속성)."""
    tc, mgr, fake = app_client
    r = tc.post("/v1/runs", json={"input": "조사해줘", "session_id": "sess-abc"},
                headers={"X-Alphred-Kind": "heavy"})
    assert r.status_code == 202 and r.json()["session_id"] == "sess-abc"
    t = mgr.get(r.json()["run_id"])
    assert t.session_key == "sess-abc"


def test_run_status_exposes_verification(app_client):
    """GET /v1/runs/{id} 가 검증 결과(needs_review/verify_report/depth)를 노출."""
    import json as _j

    from alphred.models import TaskState
    tc, mgr, fake = app_client
    rid = tc.post("/v1/runs", json={"input": "보고서"},
                  headers={"X-Alphred-Kind": "heavy"}).json()["run_id"]
    # In-Progress 를 거쳐 NeedsReview 로 (상태머신 준수)
    mgr.store.transition(rid, TaskState.IN_PROGRESS, reason="t")
    rep = {"passed": False, "summary": "산출물 누락", "suggestion": "실제로 생성하세요",
           "checks": [{"check": "file", "target": "x.pdf", "ok": False, "detail": "없음"}]}
    mgr.store.transition(rid, TaskState.NEEDS_REVIEW, reason="t",
                         verify_report=_j.dumps(rep, ensure_ascii=False))
    d = tc.get(f"/v1/runs/{rid}").json()
    assert d["needs_review"] is True and d["state"] == "NeedsReview"
    assert d["verify_report"]["suggestion"] == "실제로 생성하세요"


def test_plan_preview_dryrun(app_client):
    """드라이런 /plan 은 분류·심화도·견적을 반환하되 큐에 등록하지 않는다(§21 V3)."""
    tc, mgr, fake = app_client
    before = len(mgr.list())
    r = tc.post("/plan", json={"message": "보고서를 만들어줘"},
                headers={"X-Alphred-Kind": "heavy"})
    assert r.status_code == 200
    d = r.json()
    assert d["depth"] in ("low", "mid", "high")
    assert d["estimate"]["est_llm_calls"] >= 1
    assert len(mgr.list()) == before          # 실행/등록 없음


def test_skills_proxy(app_client):
    tc, mgr, fake = app_client
    r = tc.get("/v1/skills")
    assert r.status_code == 200
    names = [s["name"] for s in r.json()["data"]]
    assert "research" in names and "nano-pdf" in names


def test_skills_proxy_graceful_when_upstream_down(app_client):
    """업스트림(:8642) 조회 실패 시 500 대신 빈 목록+에러로 graceful 응답."""
    tc, mgr, fake = app_client

    def boom():
        raise RuntimeError("connection refused")
    fake.skills = boom
    r = tc.get("/v1/skills")
    assert r.status_code == 200
    body = r.json()
    assert body["data"] == [] and "error" in body


def test_chat_light_passthrough(app_client):
    tc, mgr, fake = app_client
    r = tc.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "안녕?"}]})
    assert r.status_code == 200
    assert "LIGHT_ANSWER" in r.text
    assert fake.chat_calls == 1


def test_chat_heavy_goes_to_queue(app_client):
    tc, mgr, fake = app_client
    r = tc.post("/v1/chat/completions",
                json={"messages": [{"role": "user", "content": "전체 코드베이스를 리팩토링 분석"}]})
    assert r.status_code == 202
    assert r.json()["status"] == "queued"
    assert fake.chat_calls == 0  # 동기 처리되지 않음
    assert len(mgr.list()) == 1


def test_chat_stream_heavy_queues(app_client):
    """전용 TUI SSE: heavy 요청은 queued 이벤트 1개 + 큐 등록(:8642 불필요)."""
    tc, mgr, fake = app_client
    r = tc.post("/chat/stream", json={"message": "전체 코드베이스 리팩토링 분석", "session_id": "t"})
    assert r.status_code == 200
    assert "event: queued" in r.text
    assert len(mgr.list()) == 1


def test_chat_stream_light_upstream_down(app_client):
    """Light 인데 :8642 미가동이면 error 이벤트로 graceful 처리(크래시 없음)."""
    tc, mgr, fake = app_client
    r = tc.post("/chat/stream", json={"message": "안녕?", "session_id": "t"})
    assert r.status_code == 200
    assert "event: error" in r.text
    assert len(mgr.list()) == 0


def test_queue_ask_smoke(app_client):
    tc, mgr, fake = app_client
    mgr.submit("백그라운드 분석", priority=3, kind="heavy")
    r = tc.post("/queue/ask", json={"q": "지금 큐 어때?"})
    assert r.status_code == 200
    body = r.json()
    assert "reply" in body and "results" in body
    # FakeClient 는 평문을 반환 → 액션 없음(조회만)
    assert body["results"] == []


def test_queue_ask_missing_q(app_client):
    tc, mgr, fake = app_client
    r = tc.post("/queue/ask", json={})
    assert r.status_code == 400


def test_runs_enqueue_and_status(app_client):
    tc, mgr, fake = app_client
    r = tc.post("/v1/runs", json={"input": "백그라운드 분석 작업"})
    assert r.status_code == 202
    rid = r.json()["run_id"]
    s = tc.get(f"/v1/runs/{rid}")
    assert s.status_code == 200
    assert s.json()["status"] in ("queued", "running", "completed")


def test_priority_override_header_forces_heavy(app_client):
    tc, mgr, fake = app_client
    # 짧은 입력이라 평소 Light 지만, 헤더로 heavy 강제
    r = tc.post("/v1/chat/completions",
                json={"messages": [{"role": "user", "content": "hi"}]},
                headers={"X-Alphred-Kind": "heavy", "X-Alphred-Priority": "2"})
    assert r.status_code == 202
    t = mgr.list()[0]
    assert t.kind == "heavy" and t.priority == 2


def test_light_preempts_running_heavy(app_client):
    tc, mgr, fake = app_client
    # Heavy 를 직접 등록하고 In-Progress 로 만든다(완료 막기)
    fake.get_run = lambda rid: {"status": "running"}
    h = mgr.submit("대규모 분석 작업", priority=3)
    mgr.tick()
    assert mgr.get(h.id).state == TaskState.IN_PROGRESS.value
    # Light 채팅 유입 → Heavy 선점되어야 함
    r = tc.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "지금 뭐해?"}]})
    assert r.status_code == 200
    assert mgr.get(h.id).state == TaskState.PAUSED.value
    assert fake.stopped  # Heavy run 이 중단됨


def test_queue_management_endpoints(app_client):
    tc, mgr, fake = app_client
    rid = tc.post("/v1/runs", json={"input": "작업"}).json()["run_id"]
    # 우선순위 변경
    assert tc.post(f"/queue/{rid}/prio", json={"priority": 9}).json()["priority"] == 9
    # 목록
    assert any(t["id"] == rid for t in tc.get("/queue").json()["tasks"])
    # 폐기
    assert tc.request("DELETE", f"/queue/{rid}").json()["state"] == TaskState.DISCARDED.value


def test_queue_retry_endpoint(app_client):
    tc, mgr, fake = app_client
    t = mgr.submit("검토 후 재시도", kind="heavy")
    mgr.store.transition(t.id, TaskState.IN_PROGRESS)
    mgr.store.transition(t.id, TaskState.NEEDS_REVIEW, reason="verify failed")
    r = tc.post(f"/queue/{t.id}/retry")
    assert r.status_code == 200
    assert r.json()["state"] == TaskState.PENDING.value


def test_scheduler_stop_join_does_not_shadow_thread_stop():
    class Mgr:
        def tick(self):
            pass

    scheduler = Scheduler(Mgr(), interval=60)
    scheduler.start()
    scheduler.stop()
    scheduler.join(timeout=2)
    assert not scheduler.is_alive()


def test_multimodal_text_plus_image_light(app_client):
    tc, mgr, fake = app_client
    body = {"messages": [{"role": "user", "content": [
        {"type": "text", "text": "이게 뭐야?"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]}]}
    r = tc.post("/v1/chat/completions", json=body)
    assert r.status_code == 200          # 짧은 텍스트 → Light → 프록시
    assert fake.chat_calls == 1


def test_multimodal_audio_only_routes_light(app_client):
    tc, mgr, fake = app_client
    body = {"messages": [{"role": "user", "content": [
        {"type": "input_audio", "input_audio": {"data": "xxxx", "format": "wav"}},
    ]}]}
    r = tc.post("/v1/chat/completions", json=body)
    assert r.status_code == 200          # 텍스트 없는 음성 → Light 라우팅
    assert fake.chat_calls == 1


def test_multimodal_heavy_text_with_image_queues(app_client):
    tc, mgr, fake = app_client
    body = {"messages": [{"role": "user", "content": [
        {"type": "text", "text": "이 이미지를 포함해 전체 데이터셋을 대규모로 분석해줘"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]}]}
    r = tc.post("/v1/chat/completions", json=body)
    assert r.status_code == 202          # heavy → 큐
    assert fake.chat_calls == 0
    assert len(mgr.list()) == 1


def test_responses_light_passthrough_preserves_multimodal(app_client):
    tc, mgr, fake = app_client
    body = {"input": [{"role": "user", "content": [
        {"type": "input_text", "text": "이게 뭐야?"},
        {"type": "input_image", "image_url": "data:image/png;base64,AAAA"},
    ]}], "previous_response_id": "resp_prev"}
    r = tc.post("/v1/responses", json=body)
    assert r.status_code == 200                       # 짧은 텍스트 → Light → 프록시
    assert fake.respond_calls == 1
    # 원본 body 가 그대로 전달돼 멀티모달 input·previous_response_id 가 보존된다
    assert fake.last_respond_body["input"] == body["input"]
    assert fake.last_respond_body["previous_response_id"] == "resp_prev"


def test_subservice_source_tag(app_client):
    tc, mgr, fake = app_client
    r = tc.post("/v1/runs", json={"input": "데이터 동기화"},
                headers={"X-Alphred-Source": "subservice"})
    assert r.status_code == 202
    assert mgr.get(r.json()["run_id"]).source == "subservice"


def test_gateway_blocks_lifecycle_payload(app_client):
    tc, mgr, fake = app_client
    r = tc.post("/v1/runs", json={"input": "hermes gateway restart"})
    assert r.status_code == 403
    assert r.json()["detail"]["error"] == "blocked_payload"
    assert len(mgr.list()) == 0


def test_safety_endpoints(tmp_path):
    from alphred.safety import RestartGuard
    store = Store(tmp_path / "s.db")
    mgr = QueueManager(store, FakeClient(), tmp_path / "Q.MD")
    guard = RestartGuard(tmp_path / "r.json", window_seconds=60, threshold=3)
    app = create_app(mgr=mgr, guard=guard, scheduler_interval=3600)
    with TestClient(app) as tc:
        s = tc.get("/safety").json()
        assert s["halted"] is False and s["restart_count"] == 1  # startup 이 1회 기록
        assert tc.post("/safety/reset").json()["halted"] is False


def test_restart_storm_halts_on_startup(tmp_path):
    from alphred.safety import RestartGuard
    store = Store(tmp_path / "s.db")
    mgr = QueueManager(store, FakeClient(), tmp_path / "Q.MD")
    guard = RestartGuard(tmp_path / "r.json", window_seconds=60, threshold=3)
    guard.record_restart(); guard.record_restart()  # 이미 2회 → startup 의 1회로 3회 트립
    app = create_app(mgr=mgr, guard=guard, scheduler_interval=3600)
    with TestClient(app) as tc:
        assert mgr.halted is True
        assert tc.get("/safety").json()["halted"] is True
        # 리셋하면 해제
        tc.post("/safety/reset")
        assert mgr.halted is False


def test_auth_required_when_key_set(tmp_path, monkeypatch):
    monkeypatch.setenv("ALPHRED_API_KEY", "secret")
    from alphred.config import Config
    store = Store(tmp_path / "a.db")
    mgr = QueueManager(store, FakeClient(), tmp_path / "Q.MD")
    cfg = Config.load()
    app = create_app(cfg, mgr=mgr, scheduler_interval=3600)
    with TestClient(app) as tc:
        assert tc.get("/v1/models").status_code == 401
        assert tc.get("/v1/models", headers={"Authorization": "Bearer secret"}).status_code == 200
