"""자연어 큐 관리(nlq) 테스트 — 주입식 LLM 으로 네트워크 없이 검증."""
from __future__ import annotations

from alphred import nlq
from alphred.db import Store
from alphred.models import TaskState
from alphred.queue_manager import QueueManager


class FakeClient:
    """nlq 에 필요한 최소 인터페이스(분류·실행은 직접 호출하지 않음)."""
    def start_run(self, prompt, **kw):
        return "run_x"

    def get_run(self, run_id):
        return {"status": "completed", "output": "ok"}

    def close(self):
        pass


def make_mgr(tmp_path):
    s = Store(tmp_path / "q.db")
    return QueueManager(s, FakeClient(), tmp_path / "QUEUE.MD"), s


# ---------- parse ----------
def test_parse_plain_json():
    d = nlq.parse('{"reply":"ok","actions":[{"action":"discard","id":"abc12345"}]}')
    assert d["reply"] == "ok"
    assert d["actions"] == [{"action": "discard", "id": "abc12345"}]


def test_parse_with_code_fence_and_prose():
    text = 'Sure!\n```json\n{"reply":"done","actions":[]}\n```'
    d = nlq.parse(text)
    assert d["reply"] == "done"
    assert d["actions"] == []


def test_parse_filters_unknown_actions():
    d = nlq.parse('{"reply":"x","actions":[{"action":"delete_all"},{"action":"pause","id":"z"}]}')
    assert d["actions"] == [{"action": "pause", "id": "z"}]


def test_parse_invalid_returns_none():
    assert nlq.parse("not json at all") is None
    assert nlq.parse("") is None


# ---------- execute ----------
def test_execute_reprioritize(tmp_path):
    mgr, store = make_mgr(tmp_path)
    t = mgr.submit("background crawl 작업", priority=3, kind="heavy")
    res = nlq.execute(mgr, store, [{"action": "reprioritize", "id": t.id[:8], "priority": 9}])
    assert mgr.get(t.id).priority == 9
    assert any("9" in r for r in res)


def test_execute_discard(tmp_path):
    mgr, store = make_mgr(tmp_path)
    t = mgr.submit("작업", priority=4, kind="heavy")
    nlq.execute(mgr, store, [{"action": "discard", "id": t.id[:8]}])
    assert mgr.get(t.id).state == TaskState.DISCARDED.value


def test_execute_unknown_id_reports_failure(tmp_path):
    mgr, store = make_mgr(tmp_path)
    res = nlq.execute(mgr, store, [{"action": "discard", "id": "deadbeef"}])
    assert res and "실패" in res[0]


# ---------- ask (end-to-end with injected llm) ----------
def test_ask_status_only(tmp_path):
    mgr, store = make_mgr(tmp_path)
    mgr.submit("작업1", priority=5, kind="heavy")
    llm = lambda prompt: '{"reply":"대기 1건 있습니다","actions":[]}'
    out = nlq.ask(mgr, store, "지금 큐 어때?", llm)
    assert out["reply"] == "대기 1건 있습니다"
    assert out["results"] == []


def test_ask_reprioritizes(tmp_path):
    mgr, store = make_mgr(tmp_path)
    t = mgr.submit("리포트 작성", priority=3, kind="heavy")
    llm = lambda prompt: (
        '{"reply":"우선순위를 올렸습니다",'
        f'"actions":[{{"action":"reprioritize","id":"{t.id[:8]}","priority":8}}]}}'
    )
    out = nlq.ask(mgr, store, "리포트 작업 우선순위 올려줘", llm)
    assert mgr.get(t.id).priority == 8
    assert out["results"]


def test_ask_llm_failure_is_graceful(tmp_path):
    mgr, store = make_mgr(tmp_path)
    def boom(prompt):
        raise RuntimeError("network down")
    out = nlq.ask(mgr, store, "뭐든", boom)
    assert "실패" in out["reply"]
    assert out["results"] == []
