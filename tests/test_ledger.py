"""§40 세션 컨텍스트 연속성 테스트 — 작업 원장·산출물 추출·지시어 해소(재작성)."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from alphred import classifier, ledger
from alphred.db import Store, new_id
from alphred.gateway import create_app
from alphred.llm_calls import make_hermes_rewrite
from alphred.models import Task, TaskState
from alphred.queue_manager import QueueManager
from alphred.server.deps import task_view

from test_gateway import FakeClient


# ---- 단위: ledger 모듈 ----
def test_extract_artifacts_only_existing_files(tmp_path):
    real = tmp_path / "report.pdf"
    real.write_bytes(b"%PDF-1.4 x")
    text = (f"보고서를 {real} 에 저장했습니다. "
            f"실패한 예시 경로는 {tmp_path / 'ghost.pdf'} 입니다.")
    arts = ledger.extract_artifacts(text)
    assert arts == [str(real)]
    assert ledger.extract_artifacts("경로 없는 일반 답변") is None
    assert ledger.extract_artifacts(None) is None


def test_ledger_block_format_and_cap():
    tasks = [Task(id=new_id(), state=TaskState.COMPLETED.value,
                  prompt="침착맨 최신 영상 요약 PDF", result="요약 완료. 저장함.",
                  artifacts=json.dumps(["C:/out/침착맨.pdf"]))]
    block = ledger.ledger_block(tasks)
    assert block and "[RECENT SESSION WORK" in block
    assert "침착맨 최신 영상 요약 PDF" in block and "(done)" in block
    assert "ARTIFACTS: C:/out/침착맨.pdf" in block
    assert ledger.ledger_block([]) is None
    # 총량 상한 — 긴 작업 여러 개여도 LEDGER_MAX_CHARS 근방에서 잘린다
    many = [Task(id=new_id(), state=TaskState.COMPLETED.value,
                 prompt="요청 " + "가" * 200, result="결과 " + "나" * 500)
            for _ in range(10)]
    big = ledger.ledger_block(many)
    assert big is not None and len(big) <= ledger.LEDGER_MAX_CHARS + 200


def test_looks_referential():
    for s in ("이번에는 육식맨 채널에 대한 동일한 분석을 해줘", "같은 형식으로 하나 더",
              "do the same for the other channel", "아까처럼 정리해줘"):
        assert ledger.looks_referential(s), s
    for s in ("cat 명령어 설명해줘", "같은 회사의 주가를 조사해줘", "미국 주식 요약", ""):
        assert not ledger.looks_referential(s), s


# ---- 단위: Store.recent_finished ----
def test_recent_finished_filters_and_orders(tmp_path):
    store = Store(tmp_path / "l.db")
    for i, (state, sess, fin) in enumerate([
            (TaskState.COMPLETED.value, "s1", "2026-07-01T00:00:00"),
            (TaskState.COMPLETED.value, "s1", "2026-07-03T00:00:00"),
            (TaskState.NEEDS_REVIEW.value, "s1", "2026-07-02T00:00:00"),
            (TaskState.DISCARDED.value, "s1", "2026-07-04T00:00:00"),   # 제외
            (TaskState.COMPLETED.value, "s2", "2026-07-05T00:00:00"),   # 타 세션
            (TaskState.PENDING.value, "s1", None)]):                    # 미종결
        t = Task(id=f"t{i}", state=state, session_key=sess, prompt=f"p{i}")
        store.create(t)
        if fin:
            store.update_fields(t.id, finished_at=fin)
    got = store.recent_finished("s1", limit=5)
    assert [t.id for t in got] == ["t1", "t2", "t0"]   # 최신 우선, 종결만
    assert store.recent_finished("", limit=5) == []


# ---- 단위: 재작성 파서/팩토리 ----
def test_parse_rewrite_and_intent_flag():
    r = classifier.parse_rewrite('{"resolved":"육식맨 영상 분석 후 PDF 생성","confidence":85}')
    assert r == {"resolved": "육식맨 영상 분석 후 PDF 생성", "confidence": 85}
    assert classifier.parse_rewrite('{"resolved":""}') is None
    assert classifier.parse_rewrite("json 아님") is None
    # IntentCard 에 refers_to_previous 가 정규화되어 실린다
    card = classifier.parse_intent(
        '{"kind":"heavy","priority":5,"depth":"mid","refers_to_previous":true}')
    assert card and card["refers_to_previous"] is True
    card2 = classifier.parse_intent('{"kind":"light","priority":9}')
    assert card2 and card2["refers_to_previous"] is False


def test_make_hermes_rewrite_confidence_gate():
    class StubClient:
        def __init__(self, payload):
            self.payload = payload

        def chat_completion(self, body):
            return {"choices": [{"message": {"content": self.payload}}]}

    hi = make_hermes_rewrite(StubClient('{"resolved":"자기완결 요청","confidence":90}'))
    assert hi("원 요청", "원장") == "자기완결 요청"
    lo = make_hermes_rewrite(StubClient('{"resolved":"자기완결 요청","confidence":30}'))
    assert lo("원 요청", "원장") is None                 # 저신뢰 → 원문 유지


# ---- 매니저: 원장 주입·재작성·산출물 저장 ----
def _mk_mgr(tmp_path, **kw):
    store = Store(tmp_path / "m.db")
    fake = FakeClient()
    mgr = QueueManager(store, fake, tmp_path / "QUEUE.MD", **kw)
    return mgr, store, fake


def _add_done(store, sess, prompt, result, artifacts=None):
    t = Task(id=new_id(), state=TaskState.COMPLETED.value, session_key=sess,
             prompt=prompt, result=result,
             artifacts=json.dumps(artifacts, ensure_ascii=False) if artifacts else None)
    store.create(t)
    return t


def test_session_context_merges_ledger(tmp_path):
    mgr, store, _ = _mk_mgr(tmp_path)
    _add_done(store, "s", "침착맨 요약 PDF", "완료했습니다")
    ctx = mgr.session_context("s", "user: 안녕")
    assert ctx.startswith("user: 안녕") and "[RECENT SESSION WORK" in ctx
    assert mgr.session_context(None, "base") == "base"          # 세션 없음 → 원본
    mgr.ledger_enabled = False
    assert mgr.session_context("s", "base") == "base"           # off → 원본


def test_submit_rewrite_triggers_and_fail_open(tmp_path):
    calls = []

    def rewrite(prompt, lb):
        calls.append((prompt, lb))
        return "육식맨 채널 최신 영상을 분석하고 침착맨 작업과 동일하게 요약 PDF 생성"

    mgr, store, _ = _mk_mgr(tmp_path, rewrite=rewrite)
    _add_done(store, "s", "침착맨 최신 영상 요약 PDF", "PDF 저장 완료")
    # ① 지시어 요청 + 세션 + 원장 → 재작성 채택(휴리스틱 트리거, intent 없이도)
    t = mgr.submit("이번에는 육식맨 채널에 대한 동일한 분석을 해줘",
                   kind="heavy", session_key="s")
    assert t.resolved_prompt and "PDF" in t.resolved_prompt
    assert len(calls) == 1 and "[RECENT SESSION WORK" in calls[0][1]
    # ② 독립 요청 → 재작성 콜 없음(QA-40.5 비용 0)
    t2 = mgr.submit("미국 주식 시장을 조사해줘", kind="heavy", session_key="s")
    assert t2.resolved_prompt is None and len(calls) == 1
    # ③ 세션 없음 → 콜 없음
    mgr.submit("이번에는 동일한 분석을 해줘", kind="heavy")
    assert len(calls) == 1


def test_submit_rewrite_exception_is_fail_open(tmp_path):
    def boom(prompt, lb):
        raise RuntimeError("LLM down")

    mgr, store, _ = _mk_mgr(tmp_path, rewrite=boom)
    _add_done(store, "s", "이전 작업", "완료")
    t = mgr.submit("이번에는 동일한 분석을 해줘", kind="heavy", session_key="s")
    assert t.state == TaskState.PENDING.value and t.resolved_prompt is None


def test_dispatch_uses_resolved_prompt_and_ledger(tmp_path):
    """QA-40.1/40.2 — 실행 입력이 해소본 + [RECENT SESSION WORK] 원장을 담는다."""
    resolved = "육식맨 채널 최신 영상을 분석하고 침착맨 작업과 동일하게 요약 PDF를 생성해줘"
    mgr, store, fake = _mk_mgr(tmp_path, rewrite=lambda p, lb: resolved)
    _add_done(store, "s", "침착맨 최신 영상 요약 PDF", "요약 PDF 저장 완료",
              artifacts=["C:/out/침착맨_요약.pdf"])
    mgr.submit("이번에는 육식맨 채널에 대한 동일한 분석을 해줘",
               kind="heavy", session_key="s")
    mgr.tick()
    assert fake.started, "run 이 시작되어야 함"
    inp = fake.started[0][0]
    assert "육식맨" in inp and "PDF" in inp                     # 해소본이 실행 기준
    assert "[RECENT SESSION WORK" in inp                        # 원장 주입(④)
    assert "침착맨_요약.pdf" in inp                             # 이전 산출물 참조 가능


def test_finalize_stores_existing_artifacts(tmp_path):
    mgr, store, fake = _mk_mgr(tmp_path)
    real = tmp_path / "out.md"
    real.write_text("# done", encoding="utf-8")
    t = mgr.submit("보고서 만들어줘", kind="heavy", session_key="s")
    store.transition(t.id, TaskState.IN_PROGRESS, reason="t")
    mgr._finalize_done(store.get(t.id), f"보고서를 {real} 에 저장했습니다.")
    done = store.get(t.id)
    assert done.state == TaskState.COMPLETED.value
    assert json.loads(done.artifacts) == [str(real)]
    # 이후 같은 세션 원장에 산출물이 실린다
    assert str(real) in (mgr._session_ledger("s") or "")


def test_task_view_exposes_resolved_and_artifacts():
    t = Task(id="x", prompt="원문", resolved_prompt="해소본",
             artifacts=json.dumps(["C:/a.pdf"]))
    v = task_view(t)
    assert v["resolved_prompt"] == "해소본" and v["artifacts"] == ["C:/a.pdf"]


# ---- 게이트웨이: chat 경로 세션 식별 + 원장 맥락 ----
@pytest.fixture
def app_client(tmp_path):
    store = Store(tmp_path / "g.db")
    fake = FakeClient()
    mgr = QueueManager(store, fake, tmp_path / "QUEUE.MD")
    app = create_app(mgr=mgr, scheduler_interval=3600)
    with TestClient(app) as tc:
        yield tc, mgr, fake


def test_chat_route_carries_session_key(app_client):
    """§40 — chat 경로도 session_id(body)/X-Alphred-Session(헤더)로 세션을 보존한다."""
    tc, mgr, fake = app_client
    r = tc.post("/v1/chat/completions",
                json={"messages": [{"role": "user", "content": "보고서 만들어줘"}],
                      "session_id": "web-1"},
                headers={"X-Alphred-Kind": "heavy"})
    assert r.status_code == 202
    assert mgr.get(r.json()["id"]).session_key == "web-1"
    r2 = tc.post("/v1/chat/completions",
                 json={"messages": [{"role": "user", "content": "조사해줘"}]},
                 headers={"X-Alphred-Kind": "heavy", "X-Alphred-Session": "hdr-2"})
    assert mgr.get(r2.json()["id"]).session_key == "hdr-2"
