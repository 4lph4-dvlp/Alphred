"""§34.6 StepRunner 테스트 — 스텝 수용검사·스텝 입력·오케스트레이션 e2e·재개·예산."""
from __future__ import annotations

import json

from alphred.db import Store, new_id
from alphred.models import TaskState
from alphred.prompt import step_input
from alphred.queue_manager import QueueManager
from alphred.verify import verify_step


# ---- verify_step (E2) ----
def test_verify_step_file_abs_path(tmp_path):
    p = tmp_path / "out.json"
    p.write_text('{"ok": true}', encoding="utf-8")
    step = {"accept": [{"check": "file", "arg": str(p)}]}
    rep = verify_step(step, "저장 완료")
    assert rep["passed"]
    rep2 = verify_step({"accept": [{"check": "file", "arg": str(tmp_path / "no.pdf")}]}, "x")
    assert not rep2["passed"] and "존재하지 않음" in rep2["feedback"]


def test_verify_step_file_falls_back_to_claimed(tmp_path):
    p = tmp_path / "r.json"
    p.write_text("{}", encoding="utf-8")
    step = {"accept": [{"check": "file", "arg": ""}]}
    rep = verify_step(step, f"결과를 저장했습니다: {p}")
    assert rep["passed"]
    rep2 = verify_step(step, "그냥 답변만 했습니다")   # 파일 주장 없음 → 실패
    assert not rep2["passed"] and "보고되지 않음" in rep2["feedback"]


def test_verify_step_content_and_exit_code():
    step = {"accept": [{"check": "content", "arg": "매출 요약"}]}
    assert verify_step(step, "…매출 요약 표를 포함했습니다")["passed"]
    assert not verify_step(step, "다른 내용")["passed"]
    ec = {"accept": [{"check": "exit_code", "arg": "0"}]}
    assert verify_step(ec, "package installed successfully")["passed"]
    assert not verify_step(ec, "ERROR: command not found: uv")["passed"]


def test_verify_step_no_accept_passes():
    rep = verify_step({"accept": []}, "아무 출력")
    assert rep["passed"] and "검사 생략" in rep["summary"]


# ---- step_input (E1) ----
def _plan(steps):
    return {"version": 2, "dod": "전체 완성", "steps": steps}


def test_step_input_contains_context_and_criteria():
    steps = [
        {"id": "s1", "goal": "자료 수집", "state": "done", "output": "수집 결과 요약",
         "tool_hint": "web_search", "needs": [], "expected": {"type": "text"}, "accept": []},
        {"id": "s2", "goal": "보고서 작성", "tool_hint": "execute_code", "needs": ["s1"],
         "expected": {"type": "file", "format": "md", "path_hint": "report.md"},
         "accept": [{"check": "file", "arg": ""}]},
    ]
    out = step_input("시장 조사 보고서", _plan(steps), steps[1],
                     capabilities="skills: powerpoint", intake="[사용자 확인 답변]\n- 형식 → MD",
                     feedback="- [file] r.md: 존재하지 않음")
    assert "ONE step" in out and "YOUR STEP (2/2): 보고서 작성" in out
    assert "COMPLETED STEPS" in out and "수집 결과 요약" in out    # 완료 스텝 요약(E4 맥락)
    assert "OVERALL DoD: 전체 완성" in out
    assert "Done-when [file]" in out and "PREVIOUS ATTEMPT FAILED" in out
    assert "skills: powerpoint" in out and "형식 → MD" in out


# ---- 오케스트레이션 e2e (fake upstream) ----
class _Client:
    """스텝 run 마다 지정된 출력을 차례로 반환하는 fake Hermes."""

    def __init__(self, outputs=None):
        self.inputs: list[str] = []
        self.sessions: list = []
        self.outputs = list(outputs or [])
        self.status: dict = {}

    def start_run(self, prompt, **kw):
        self.inputs.append(prompt)
        self.sessions.append(kw.get("session_id"))
        rid = "run_" + new_id()[:8]
        out = self.outputs.pop(0) if self.outputs else "DONE"
        self.status[rid] = {"status": "completed", "output": out}
        return rid

    def get_run(self, rid):
        return self.status.get(rid, {"status": "running"})

    def stop_run(self, rid):
        self.status[rid] = {"status": "cancelled"}
        return {}

    def close(self):
        pass


_PLAN2 = {"version": 2, "dod": "완성", "steps": [
    {"id": "s1", "goal": "조사", "tool_hint": "web_search", "needs": [],
     "expected": {"type": "text", "format": None, "path_hint": None}, "accept": []},
    {"id": "s2", "goal": "작성", "tool_hint": "execute_code", "needs": ["s1"],
     "expected": {"type": "text", "format": None, "path_hint": None},
     "accept": [{"check": "content", "arg": "최종 보고"}]},
]}


def _mgr(tmp_path, client, **kw):
    kw.setdefault("orchestrate", True)
    kw.setdefault("planner2", lambda prompt, **k: json.loads(json.dumps(_PLAN2)))
    return QueueManager(Store(tmp_path / "q.db"), client, tmp_path / "Q.MD", **kw)


def _submit_high(mgr):
    return mgr.submit("전체 코드베이스 분석 보고서 만들어줘", depth="high")


def test_orchestrated_two_steps_complete(tmp_path):
    client = _Client(outputs=["조사 결과입니다", "최종 보고 완성"])
    mgr = _mgr(tmp_path, client)
    t = _submit_high(mgr)
    mgr.tick()          # 디스패치: 계획 생성 + 스텝1 시작
    assert len(client.inputs) == 1 and "YOUR STEP (1/2)" in client.inputs[0]
    mgr.tick()          # 스텝1 완료 → 검사 통과 → 스텝2 즉시 시작
    assert len(client.inputs) == 2 and "YOUR STEP (2/2)" in client.inputs[1]
    assert "COMPLETED STEPS" in client.inputs[1] and "조사 결과" in client.inputs[1]
    mgr.tick()          # 스텝2 완료 → 전체 마감
    cur = mgr.store.get(t.id)
    assert cur.state == TaskState.COMPLETED.value
    assert cur.result == "최종 보고 완성"
    plan = json.loads(cur.plan)
    assert all(s["state"] == "done" for s in plan["steps"])
    assert cur.plan_progress == 2                       # 실측 스텝 진행
    assert plan["runs_used"] == 2
    # 세션 연속성 — 모든 스텝이 같은 세션
    assert len(set(client.sessions)) == 1


def test_step_failure_retries_only_that_step(tmp_path):
    # 스텝2 검사(content '최종 보고')를 첫 시도에 불통과 → 그 스텝만 재시도(QA-34.7)
    client = _Client(outputs=["조사 결과입니다", "아직 미완성", "최종 보고 완성"])
    mgr = _mgr(tmp_path, client)
    t = _submit_high(mgr)
    mgr.tick()          # 스텝1 시작
    mgr.tick()          # 스텝1 done → 스텝2 시작
    mgr.tick()          # 스텝2 실패 → 같은 스텝 재시도(피드백 주입)
    assert len(client.inputs) == 3
    assert "YOUR STEP (2/2)" in client.inputs[2]        # 스텝1 재실행 없음
    assert "PREVIOUS ATTEMPT FAILED" in client.inputs[2]
    mgr.tick()          # 재시도 성공 → 완료
    cur = mgr.store.get(t.id)
    assert cur.state == TaskState.COMPLETED.value
    assert json.loads(cur.plan)["steps"][1]["attempts"] == 1


def test_step_retries_exhausted_partial_needs_review(tmp_path):
    client = _Client(outputs=["조사 결과", "미완성1", "미완성2", "미완성3"])

    # M5 replan 이 끼어들지 않는 경우(재계획 실패/불가) → 부분성공 NeedsReview 경로
    def planner2(prompt, **kw):
        return None if kw.get("replan") else json.loads(json.dumps(_PLAN2))

    mgr = _mgr(tmp_path, client, step_retries=2, planner2=planner2)
    t = _submit_high(mgr)
    for _ in range(5):
        mgr.tick()
    cur = mgr.store.get(t.id)
    assert cur.state == TaskState.NEEDS_REVIEW.value
    rep = json.loads(cur.verify_report)
    assert rep["steps_done"] == 1 and rep["steps_total"] == 2   # 부분 성공 표면화
    assert "수용검사" in rep["summary"]


def test_budget_guard_partial_needs_review(tmp_path):
    client = _Client(outputs=["조사 결과", "최종 보고"])
    mgr = _mgr(tmp_path, client, task_budget=1)         # run 1개만 허용
    t = _submit_high(mgr)
    mgr.tick()          # 스텝1 시작(run 1/1 소모)
    mgr.tick()          # 스텝1 done → 스텝2 시작 시도 → 예산 초과(QA-34.9)
    cur = mgr.store.get(t.id)
    assert cur.state == TaskState.NEEDS_REVIEW.value
    rep = json.loads(cur.verify_report)
    assert "예산 초과" in rep["summary"] and rep["steps_done"] == 1


def test_preempt_resume_does_not_redo_done_steps(tmp_path):
    # 스텝1 완료 후 스텝2 실행 중 선점 → 재개 시 스텝2부터(스텝1 재실행 없음, QA-34.7)
    client = _Client(outputs=["조사 결과", "중단됨", "최종 보고 완성"])
    mgr = _mgr(tmp_path, client)
    t = _submit_high(mgr)
    mgr.tick()          # 스텝1 시작
    mgr.tick()          # 스텝1 done → 스텝2 시작
    # 스텝2 run 을 아직 완료 전으로 만들고 선점 상황 재현
    cur = mgr.store.get(t.id)
    client.status[cur.hermes_run_id] = {"status": "running"}
    mgr.store.transition(t.id, TaskState.PAUSED, reason="preempted by test",
                         paused_reason="preempted:prio9")
    mgr.tick()          # 재개 → 스텝2 재시작(스텝 경계 재개)
    assert len(client.inputs) == 3
    assert "YOUR STEP (2/2)" in client.inputs[2]
    assert "COMPLETED STEPS" in client.inputs[2]        # 스텝1 요약으로 맥락 계승
    mgr.tick()
    assert mgr.store.get(t.id).state == TaskState.COMPLETED.value


def test_verify_retry_appends_fix_step(tmp_path):
    """전체 Tier0 실패 → §21 Tier3 재큐 → 재개 시 fix 스텝이 추가돼 오케스트레이션과 정합."""
    missing = tmp_path / "no" / "report.pdf"            # 존재하지 않는 파일 주장 → Tier0 실패
    client = _Client(outputs=[f"보고서를 저장했습니다: {missing}", "보완 완료했습니다"])
    plan_one = {"version": 2, "dod": "완성", "steps": [
        {"id": "s1", "goal": "작성", "tool_hint": None, "needs": [],
         "expected": {"type": "text", "format": None, "path_hint": None}, "accept": []}]}
    mgr = _mgr(tmp_path, client, retry_base_seconds=0.0,
               planner2=lambda p, **k: json.loads(json.dumps(plan_one)))
    t = _submit_high(mgr)
    mgr.tick()          # 스텝1 시작
    # 스텝 done → 전체 마감 시도 → Tier0 실패(없는 파일) → Tier3 재큐 → 백오프 0 이라
    # 같은 틱의 슬롯 채우기에서 즉시 재개 → 모든 스텝 done + 피드백 → fix 스텝 추가·실행
    mgr.tick()
    assert "Address the verification feedback" in client.inputs[-1]
    cur = mgr.store.get(t.id)
    assert cur.state == TaskState.IN_PROGRESS.value     # fix run 진행 중
    assert any(s["id"].startswith("fix") for s in json.loads(cur.plan)["steps"])
    assert cur.verify_feedback is None                  # 소진(무한 fix 방지)
    assert (cur.verify_attempts or 0) == 1              # Tier3 재시도 1회 기록
    mgr.tick()          # fix 완료 → 전체 Tier0 통과 → Completed
    assert mgr.store.get(t.id).state == TaskState.COMPLETED.value


def test_orchestrate_off_or_mid_depth_single_run(tmp_path):
    client = _Client(outputs=["DONE"])
    mgr = _mgr(tmp_path, client, orchestrate=False)     # 플래그 off → 단발 경로
    _submit_high(mgr)
    mgr.tick()
    assert "YOUR STEP" not in client.inputs[0]          # 스텝 프레임 없음
    sub = tmp_path / "b"
    sub.mkdir()
    client2 = _Client(outputs=["DONE"])
    mgr2 = _mgr(sub, client2)
    mgr2.submit("전체 코드베이스 분석 보고서 만들어줘", depth="mid")   # mid → 단발
    mgr2.tick()
    assert "YOUR STEP" not in client2.inputs[0]