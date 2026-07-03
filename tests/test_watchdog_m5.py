"""§34 M5 테스트 — watchdog(E3) · replan · 선호 기억(C3) · 평가 지표(F)."""
from __future__ import annotations

import json

from alphred.cli import _collect_metrics
from alphred.db import Store, new_id
from alphred.models import TaskState
from alphred.prompt import append_preference, load_preferences
from alphred.queue_manager import QueueManager


class _Client:
    def __init__(self, outputs=None):
        self.inputs: list[str] = []
        self.outputs = list(outputs or [])
        self.status: dict = {}
        self.stopped: list[str] = []

    def start_run(self, prompt, **kw):
        self.inputs.append(prompt)
        rid = "run_" + new_id()[:8]
        out = self.outputs.pop(0) if self.outputs else "DONE"
        self.status[rid] = {"status": "completed", "output": out}
        return rid

    def get_run(self, rid):
        return self.status.get(rid, {"status": "running"})

    def stop_run(self, rid):
        self.stopped.append(rid)
        self.status[rid] = {"status": "cancelled"}
        return {}

    def close(self):
        pass


def _mgr(tmp_path, client, **kw):
    return QueueManager(Store(tmp_path / "q.db"), client, tmp_path / "Q.MD", **kw)


def _running(mgr, client, prompt="전체 코드베이스 리팩토링 해줘", **submit_kw):
    """작업을 실행 중(run 미종료) 상태로 만든다."""
    t = mgr.submit(prompt, **submit_kw)
    mgr.tick()
    cur = mgr.store.get(t.id)
    client.status[cur.hermes_run_id] = {"status": "running"}
    return cur


# ---- E3 watchdog ----
def test_watchdog_runaway_flag_intervenes(tmp_path):
    client = _Client()
    mgr = _mgr(tmp_path, client, watchdog=True)
    t = _running(mgr, client)
    mgr._flag_runaway(t.id, "연속 도구 실패 3회(terminal)")
    mgr.tick()                                    # 개입: stop → Paused + 교정 피드백
    cur = mgr.store.get(t.id)
    assert client.stopped                          # run 중단됨(QA-34.8)
    assert cur.state == TaskState.PAUSED.value
    assert cur.paused_reason.startswith("watchdog")
    assert cur.retries == 1
    assert "watchdog" in (cur.verify_feedback or "")   # 단발 → 교정 피드백 주입
    assert t.id not in mgr._runaway                # 신호 소진


def test_watchdog_resume_injects_corrective_hint(tmp_path):
    client = _Client(outputs=["첫 run", "재시도 완료"])
    mgr = _mgr(tmp_path, client, watchdog=True, retry_base_seconds=0.0)
    t = _running(mgr, client)
    mgr._flag_runaway(t.id, "연속 도구 실패")
    mgr.tick()                                    # 개입 + (백오프 0) 같은 틱 재시작
    assert len(client.inputs) == 2
    assert "같은 접근을 그대로 반복하지" in client.inputs[1]   # 교정 힌트 주입


def test_watchdog_stall_detection_via_updated_at(tmp_path):
    client = _Client()
    mgr = _mgr(tmp_path, client, watchdog=True, stall_seconds=60.0)
    t = _running(mgr, client)
    # 트래커 미가동(fake) → DB 갱신 시각 근사 경로. 갱신 시각을 과거로 조작.
    mgr.store._conn.execute("UPDATE tasks SET updated_at=? WHERE id=?",
                            ("2020-01-01T00:00:00+00:00", t.id))
    mgr.tick()
    cur = mgr.store.get(t.id)
    assert client.stopped                          # 무진전 감지 → 중단
    assert cur.retries == 1


def test_watchdog_off_no_intervention(tmp_path):
    client = _Client()
    mgr = _mgr(tmp_path, client, watchdog=False)
    t = _running(mgr, client)
    mgr._flag_runaway(t.id, "x")                  # 신호가 있어도 watchdog off → 무개입
    mgr.tick()
    assert mgr.store.get(t.id).state == TaskState.IN_PROGRESS.value
    assert not client.stopped


def test_watchdog_repeated_interventions_needs_review(tmp_path):
    client = _Client()
    mgr = _mgr(tmp_path, client, watchdog=True, max_retries=1)
    t = _running(mgr, client)
    mgr.store.update_fields(t.id, retries=1)      # 이미 상한 도달
    mgr._flag_runaway(t.id, "여전히 폭주")
    mgr.tick()
    cur = mgr.store.get(t.id)
    assert cur.state == TaskState.NEEDS_REVIEW.value
    assert "watchdog" in json.loads(cur.verify_report)["summary"]


def test_watchdog_orchestrated_puts_feedback_on_step(tmp_path):
    plan = {"version": 2, "dod": "d", "steps": [
        {"id": "s1", "goal": "작업", "tool_hint": None, "needs": [],
         "expected": {"type": "text", "format": None, "path_hint": None}, "accept": []}]}
    client = _Client()
    mgr = _mgr(tmp_path, client, watchdog=True, orchestrate=True,
               planner2=lambda p, **k: json.loads(json.dumps(plan)))
    t = _running(mgr, client, depth="high")
    mgr._flag_runaway(t.id, "무한 도구 루프")
    mgr.tick()
    cur = mgr.store.get(t.id)
    stored = json.loads(cur.plan)
    assert "watchdog" in (stored["steps"][0].get("feedback") or "")   # 스텝 피드백
    assert not cur.verify_feedback                # 오케스트레이션은 verify_feedback 미사용


# ---- replan ----
def test_replan_once_after_step_retries_exhausted(tmp_path):
    plan_a = {"version": 2, "dod": "d", "steps": [
        {"id": "s1", "goal": "생성", "tool_hint": None, "needs": [],
         "expected": {"type": "text", "format": None, "path_hint": None},
         "accept": [{"check": "content", "arg": "성공표식"}]}]}
    plan_b = {"version": 2, "dod": "d2", "steps": [
        {"id": "n1", "goal": "다른 접근으로 생성", "tool_hint": None, "needs": [],
         "expected": {"type": "text", "format": None, "path_hint": None}, "accept": []}]}
    calls = []

    def planner2(prompt, **kw):
        calls.append(kw)
        return json.loads(json.dumps(plan_b if kw.get("replan") else plan_a))

    client = _Client(outputs=["미달1", "미달2", "다른 접근 결과"])
    mgr = _mgr(tmp_path, client, orchestrate=True, planner2=planner2, step_retries=1)
    t = mgr.submit("전체 코드베이스 리팩토링 해줘", depth="high")
    mgr.tick()          # 계획A + 스텝 시작
    mgr.tick()          # 실패1 → 재시도
    mgr.tick()          # 실패2(소진) → replan → 새 계획으로 실행
    replans = [k for k in calls if k.get("replan")]
    assert len(replans) == 1
    assert "FAILED (s1)" in replans[0]["replan"]   # 실패 맥락 동봉
    cur = mgr.store.get(t.id)
    stored = json.loads(cur.plan)
    assert stored["replanned"] is True
    assert stored["steps"][0]["id"] == "n1"
    assert stored["previous_steps"][0]["id"] == "s1"   # 이력 보존
    assert stored["runs_used"] >= 2                    # 예산 승계
    mgr.tick()          # 새 계획 완주
    assert mgr.store.get(t.id).state == TaskState.COMPLETED.value


def test_replan_only_once_then_needs_review(tmp_path):
    bad = {"version": 2, "dod": "d", "steps": [
        {"id": "s1", "goal": "생성", "tool_hint": None, "needs": [],
         "expected": {"type": "text", "format": None, "path_hint": None},
         "accept": [{"check": "content", "arg": "불가능한표식"}]}]}

    def planner2(prompt, **kw):
        return json.loads(json.dumps(bad))

    client = _Client()
    mgr = _mgr(tmp_path, client, orchestrate=True, planner2=planner2, step_retries=0)
    t = mgr.submit("전체 코드베이스 리팩토링 해줘", depth="high")
    for _ in range(6):
        mgr.tick()
    cur = mgr.store.get(t.id)
    assert cur.state == TaskState.NEEDS_REVIEW.value   # replan 1회 후에도 실패 → 종료
    assert json.loads(cur.plan)["replanned"] is True


# ---- C3 선호 기억 ----
def test_preferences_append_and_load(tmp_path):
    p = tmp_path / "preferences.md"
    append_preference(p, "보고서 형식은?", "PDF")
    append_preference(p, "", "한국어로 답변")
    txt = load_preferences(p)
    assert "보고서 형식은? → PDF" in txt and "한국어로 답변" in txt
    assert load_preferences(tmp_path / "none.md") is None


def test_answer_accumulates_preferences_and_injects(tmp_path):
    prefs = tmp_path / "preferences.md"
    client = _Client()

    def intent(prompt, context=None):
        return {"kind": "heavy", "priority": 5, "depth": "mid", "goal": "g",
                "missing_info": [{"what": "형식", "critical": True}], "confidence": 90}

    qpack = {"questions": [{"header": "형식", "q": "형식은?", "why": "",
                            "options": [{"label": "PDF", "recommended": True},
                                        {"label": "MD", "recommended": False}]}],
             "assumptions_if_silent": ["PDF 가정"]}
    seen_prefs = []

    def clarify(p, m, c=None, preferences=None):
        seen_prefs.append(preferences)
        return json.loads(json.dumps(qpack))

    mgr = _mgr(tmp_path, client, intent=intent, clarify=clarify, prefs_path=prefs)
    t = mgr.submit("보고서 하나 만들어줘 형식은 알아서", source="tui")
    mgr.answer(t.id, [{"q": "형식은?", "answer": "MD"}])
    assert "형식은? → MD" in load_preferences(prefs)   # 축적
    mgr.tick()                                          # 실행 입력에 선호 블록 주입
    assert "USER PREFERENCES" in client.inputs[0] and "MD" in client.inputs[0]
    # 두 번째 인테이크에는 선호가 동봉됨(같은 질문 재발 방지 신호)
    mgr.submit("다른 보고서도 만들어줘 형식은 알아서", source="tui")
    assert seen_prefs[-1] and "형식은? → MD" in seen_prefs[-1]


def test_clarify_legacy_signature_still_works(tmp_path):
    """preferences 미지원 구형 clarify 콜러블도 TypeError 폴백으로 동작(하위호환)."""
    client = _Client()

    def intent(prompt, context=None):
        return {"kind": "heavy", "priority": 5, "depth": "mid", "goal": "g",
                "missing_info": [{"what": "형식", "critical": True}], "confidence": 90}

    qpack = {"questions": [{"q": "형식은?", "options": [{"label": "PDF", "recommended": True},
                                                        {"label": "MD"}]}],
             "assumptions_if_silent": ["PDF 가정"]}
    mgr = _mgr(tmp_path, client, intent=intent,
               clarify=lambda p, m, c=None: json.loads(json.dumps(qpack)),
               prefs_path=tmp_path / "preferences.md")
    t = mgr.submit("보고서 하나 만들어줘 형식은 알아서", source="tui")
    assert t.state == TaskState.AWAITING_INPUT.value


# ---- F 지표 ----
def test_collect_metrics(tmp_path):
    store = Store(tmp_path / "m.db")
    mgr = QueueManager(store, _Client(), tmp_path / "Q.MD")
    # 분류 텔레메트리
    mgr.classify_only("안녕!", source="tui")                            # fastpath
    mgr.classify_only("아무거나", source="api", explicit_kind="heavy")   # explicit
    # 오케스트레이션 이력 + 인테이크 답변(추천 채택) 가진 작업을 직접 구성
    t = mgr.submit("전체 코드베이스 리팩토링 해줘")
    plan = {"version": 2, "runs_used": 3, "steps": [
        {"id": "s1", "state": "done", "attempts": 0, "goal": "a"},
        {"id": "s2", "state": "done", "attempts": 1, "goal": "b"}]}
    qs = [{"q": "형식은?", "options": [{"label": "PDF", "recommended": True},
                                       {"label": "MD"}]}]
    store.update_fields(t.id, plan=json.dumps(plan),
                        questions=json.dumps(qs, ensure_ascii=False),
                        answers=json.dumps([{"q": "형식은?", "answer": "PDF"}],
                                           ensure_ascii=False))
    store.transition(t.id, TaskState.IN_PROGRESS)
    store.transition(t.id, TaskState.NEEDS_REVIEW, reason="test")
    m = _collect_metrics(store.list(), store.intent_stats())
    assert m["classified"] >= 3                    # submit 분류 포함
    assert m["explicit_ratio"] is not None and m["explicit_ratio"] > 0
    assert m["needs_review_rate"] == 1.0
    assert m["asked"] == 1 and m["recommend_adopt_rate"] == 1.0
    assert m["orchestrated"] == 1 and m["avg_runs"] == 3.0
    assert m["step_first_pass_rate"] == 0.5
    store.close()