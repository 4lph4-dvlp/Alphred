"""§34.4 인테이크(질문+추천답변+AwaitingInput) 테스트 — 파서·게이트·흐름·API·주입."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from alphred import classifier
from alphred.db import Store, new_id
from alphred.gateway import create_app
from alphred.models import TaskKind, TaskState
from alphred.prompt import intake_block
from alphred.queue_manager import QueueManager


# ---- parse_clarify 정규화 ----
def test_parse_clarify_normalizes_recommended():
    txt = ('{"questions":[{"header":"독자","q":"대상 독자는?",'
           '"options":[{"label":"경영진","recommended":true},{"label":"실무자","recommended":true},'
           '"외부 공개"],"why":"톤 결정"}],"assumptions_if_silent":["경영진용 가정"]}')
    pack = classifier.parse_clarify(txt)
    q = pack["questions"][0]
    assert len(q["options"]) == 3
    recs = [o for o in q["options"] if o["recommended"]]
    assert len(recs) == 1 and recs[0]["label"] == "경영진"   # 추천 정확히 1개(첫 표기 유지)
    assert pack["assumptions_if_silent"] == ["경영진용 가정"]


def test_parse_clarify_drops_bad_questions_and_synthesizes_assumptions():
    txt = ('{"questions":[{"q":"선택지 하나뿐","options":[{"label":"A"}]},'
           '{"q":"정상 질문","options":[{"label":"X"},{"label":"Y","recommended":true}]}]}')
    pack = classifier.parse_clarify(txt)
    assert len(pack["questions"]) == 1                       # 선택지<2 질문 제거
    assert pack["questions"][0]["q"] == "정상 질문"
    assert pack["assumptions_if_silent"] == ["정상 질문 → Y"]  # 가정 미제공 → 추천으로 합성


def test_parse_clarify_empty_is_none():
    assert classifier.parse_clarify('{"questions":[]}') is None
    assert classifier.parse_clarify("no json") is None


# ---- needs_clarification 게이트(QA-34.5) ----
def _card(critical: bool | None):
    if critical is None:
        return {"kind": "heavy", "missing_info": []}
    return {"kind": "heavy", "missing_info": [{"what": "형식", "critical": critical}]}


def test_needs_clarification_gate():
    # 대화형 + heavy + critical → 질문
    assert classifier.needs_clarification(_card(True), source="tui", kind="heavy")
    assert classifier.needs_clarification(_card(True), source="chat", kind="heavy")
    # 비대화형(cron/api/subservice) → 질문 0
    for src in ("api", "cron", "subservice"):
        assert not classifier.needs_clarification(_card(True), source=src, kind="heavy")
    # critical 아님/부족정보 없음/카드 없음 → 질문 0
    assert not classifier.needs_clarification(_card(False), source="tui", kind="heavy")
    assert not classifier.needs_clarification(_card(None), source="tui", kind="heavy")
    assert not classifier.needs_clarification(None, source="tui", kind="heavy")
    # Light 는 즉답이라 질문 없음
    assert not classifier.needs_clarification(_card(True), source="tui", kind="light")


# ---- intake_block(실행 입력 주입) ----
def test_intake_block_answers_and_assumptions():
    qs = [{"q": "대상 독자는?", "options": []}]
    out = intake_block(qs, ["경영진"], ["가정1"])
    assert "사용자 확인 답변" in out and "대상 독자는? → 경영진" in out
    out2 = intake_block(qs, None, ["경영진용으로 가정"])
    assert "채택한 가정" in out2 and "경영진용으로 가정" in out2
    assert intake_block(None, None, None) == ""


# ---- QueueManager 흐름 ----
class _Client:
    def __init__(self):
        self.inputs = []
        self.status = {}

    def start_run(self, prompt, **kw):
        self.inputs.append(prompt)
        rid = "run_" + new_id()[:8]
        self.status[rid] = {"status": "completed", "output": "DONE"}
        return rid

    def get_run(self, rid):
        return self.status.get(rid, {"status": "running"})

    def close(self):
        pass


_QPACK = {"questions": [
    {"header": "형식", "q": "어떤 형식으로?", "why": "",
     "options": [{"label": "PDF", "recommended": True}, {"label": "MD", "recommended": False}]}],
    "assumptions_if_silent": ["PDF 로 가정"]}


def _intent_critical(prompt, context=None):
    return {"kind": "heavy", "priority": 5, "depth": "high", "goal": "보고서",
            "missing_info": [{"what": "형식", "critical": True}], "confidence": 85}


def _mgr(tmp_path, *, intent=_intent_critical, clarify=lambda p, m, c=None: _QPACK,
         timeout=600.0):
    return QueueManager(Store(tmp_path / "q.db"), _Client(), tmp_path / "Q.MD",
                        intent=intent, clarify=clarify, clarify_timeout=timeout)


def test_submit_enters_awaiting_input_and_not_scheduled(tmp_path):
    mgr = _mgr(tmp_path)
    t = mgr.submit("서버 로그 분석해서 장애 원인 보고서 만들어줘", source="tui")
    assert t.state == TaskState.AWAITING_INPUT.value
    assert json.loads(t.questions)[0]["q"] == "어떤 형식으로?"
    assert json.loads(t.assumptions) == ["PDF 로 가정"]
    assert t.input_deadline is not None
    mgr.tick()                                   # 스케줄러가 실행하면 안 됨(QA-34.4)
    assert mgr.store.get(t.id).state == TaskState.AWAITING_INPUT.value
    assert not mgr.client.inputs


def test_answer_promotes_and_injects(tmp_path):
    mgr = _mgr(tmp_path)
    t = mgr.submit("보고서 하나 만들어줘 형식은 알아서", source="tui")
    task = mgr.answer(t.id, [{"q": "어떤 형식으로?", "answer": "MD"}])
    assert task.state == TaskState.PENDING.value and task.input_deadline is None
    mgr.tick()                                   # 이제 실행됨 + 답변이 입력에 주입됨
    sent = mgr.client.inputs[0]
    assert "사용자 확인 답변" in sent and "MD" in sent


def test_answer_wrong_state_raises(tmp_path):
    mgr = _mgr(tmp_path, intent=None, clarify=None)
    t = mgr.submit("전체 코드베이스 리팩토링 해줘")     # 질문 없이 Pending
    with pytest.raises(ValueError):
        mgr.answer(t.id, ["x"])
    with pytest.raises(KeyError):
        mgr.answer("no-such-id", ["x"])


def test_timeout_promotes_with_assumptions(tmp_path):
    mgr = _mgr(tmp_path)
    t = mgr.submit("보고서 하나 만들어줘 형식은 알아서", source="tui")
    past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    mgr.store.update_fields(t.id, input_deadline=past)
    mgr.tick()                                   # 타임아웃 승격 + 같은 틱에서 실행 시작
    cur = mgr.store.get(t.id)
    assert cur.state in (TaskState.PENDING.value, TaskState.IN_PROGRESS.value,
                         TaskState.COMPLETED.value)
    assert mgr.client.inputs and "채택한 가정" in mgr.client.inputs[0]
    mgr.tick()                                   # 완주 → 보고서에 가정 표면화
    done = mgr.store.get(t.id)
    assert done.state == TaskState.COMPLETED.value
    assert json.loads(done.verify_report).get("assumptions") == ["PDF 로 가정"]


def test_explicit_override_skips_questions(tmp_path):
    mgr = _mgr(tmp_path)
    t = mgr.submit("보고서 하나 만들어줘", source="tui", kind="heavy", priority=8)
    assert t.state == TaskState.PENDING.value and t.questions is None


def test_clarify_failure_fail_open(tmp_path):
    def bad_clarify(p, m, c=None):
        raise RuntimeError("llm down")

    mgr = _mgr(tmp_path, clarify=bad_clarify)
    t = mgr.submit("보고서 하나 만들어줘 형식은 알아서", source="tui")
    assert t.state == TaskState.PENDING.value    # 질문 생성 실패가 작업을 막지 않음


def test_noncritical_or_api_source_no_questions(tmp_path):
    calls = []

    def clarify(p, m, c=None):
        calls.append(p)
        return _QPACK

    mgr = _mgr(tmp_path, clarify=clarify)
    t = mgr.submit("보고서 하나 만들어줘 형식은 알아서", source="api")   # 비대화형
    assert t.state == TaskState.PENDING.value and not calls              # 질문 0(LLM 미호출)


# ---- 게이트웨이 표면 ----
@pytest.fixture
def intake_app(tmp_path):
    store = Store(tmp_path / "g.db")
    fake = _Client()
    mgr = QueueManager(store, fake, tmp_path / "QUEUE.MD",
                       intent=_intent_critical, clarify=lambda p, m, c=None: _QPACK)
    app = create_app(mgr=mgr, scheduler_interval=3600)
    with TestClient(app) as tc:
        yield tc, mgr


def test_gateway_needs_input_then_answers(intake_app):
    tc, mgr = intake_app
    r = tc.post("/v1/chat/completions",
                json={"messages": [{"role": "user",
                                    "content": "서버 로그 분석해서 장애 원인 보고서 만들어줘 형식은 알아서"}]},
                headers={"X-Alphred-Source": "tui"})
    assert r.status_code == 202
    d = r.json()
    assert d["status"] == "needs_input"
    assert d["questions"][0]["options"][0]["recommended"] is True
    tid = d["id"]
    # 잘못된 본문 → 400
    assert tc.post(f"/queue/{tid}/answers", json={}).status_code == 400
    # 답변 제출 → Pending 승격 + task_view 노출
    r2 = tc.post(f"/queue/{tid}/answers", json={"answers": ["PDF"]})
    assert r2.status_code == 200
    assert r2.json()["state"] == TaskState.PENDING.value
    assert r2.json()["answers"] == ["PDF"]
    # 중복 답변 → 409
    assert tc.post(f"/queue/{tid}/answers", json={"answers": ["PDF"]}).status_code == 409


def test_gateway_runs_needs_input_status(intake_app):
    tc, mgr = intake_app
    r = tc.post("/v1/runs", json={"input": "보고서 하나 만들어줘 형식은 알아서"},
                headers={"X-Alphred-Source": "tui"})
    assert r.status_code == 202
    d = r.json()
    assert d["status"] == "needs_input" and d["questions"]
    # 상태 폴링에도 needs_input 노출
    r2 = tc.get(f"/v1/runs/{d['run_id']}")
    assert r2.json()["status"] == "needs_input"