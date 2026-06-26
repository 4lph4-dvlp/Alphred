"""Phase 1 핵심 로직 테스트 (LLM 불필요 — FakeClient 사용)."""
from __future__ import annotations

import pytest

from alphred import classifier
from alphred.db import Store, new_id
from alphred.models import Task, TaskKind, TaskSource, TaskState
from alphred.queue_manager import QueueManager
from alphred.state_machine import InvalidTransition, can_transition


# ---------- 상태머신 ----------
def test_state_machine_allowed():
    assert can_transition(TaskState.PENDING, TaskState.IN_PROGRESS)
    assert can_transition(TaskState.IN_PROGRESS, TaskState.PAUSED)
    assert can_transition(TaskState.PAUSED, TaskState.IN_PROGRESS)
    assert can_transition(TaskState.IN_PROGRESS, TaskState.COMPLETED)


def test_state_machine_forbidden():
    assert not can_transition(TaskState.COMPLETED, TaskState.IN_PROGRESS)
    assert not can_transition(TaskState.PENDING, TaskState.PAUSED)
    assert not can_transition(TaskState.DISCARDED, TaskState.PENDING)


def test_state_machine_needs_review():
    """§21: In-Progress → NeedsReview 허용, NeedsReview 에서 재큐/폐기 가능."""
    assert can_transition(TaskState.IN_PROGRESS, TaskState.NEEDS_REVIEW)
    assert can_transition(TaskState.NEEDS_REVIEW, TaskState.PENDING)
    assert can_transition(TaskState.NEEDS_REVIEW, TaskState.DISCARDED)
    assert not can_transition(TaskState.PENDING, TaskState.NEEDS_REVIEW)


# ---------- DB ----------
def make_store(tmp_path) -> Store:
    return Store(tmp_path / "t.db")


def test_db_create_and_transition(tmp_path):
    s = make_store(tmp_path)
    t = Task(id=new_id(), prompt="hi", priority=5)
    s.create(t)
    assert s.get(t.id).state == TaskState.PENDING.value
    s.transition(t.id, TaskState.IN_PROGRESS, reason="go")
    got = s.get(t.id)
    assert got.state == TaskState.IN_PROGRESS.value
    assert got.started_at
    evs = s.events(t.id)
    assert evs[-1]["to_state"] == TaskState.IN_PROGRESS.value


def test_db_invalid_transition_blocked(tmp_path):
    s = make_store(tmp_path)
    t = Task(id=new_id(), prompt="x")
    s.create(t)
    s.transition(t.id, TaskState.IN_PROGRESS)
    s.transition(t.id, TaskState.COMPLETED)
    with pytest.raises(InvalidTransition):
        s.transition(t.id, TaskState.IN_PROGRESS)  # 최종 상태에서 재개 불가


def test_db_priority_ordering(tmp_path):
    s = make_store(tmp_path)
    for p in (3, 9, 1, 7):
        s.create(Task(id=new_id(), prompt=f"p{p}", priority=p))
    assert s.next_pending().priority == 9
    s.set_priority(s.next_pending().id, 2)  # 9짜리를 2로 낮춤
    assert s.next_pending().priority == 7


def test_db_persistence_across_reopen(tmp_path):
    path = tmp_path / "t.db"
    s = Store(path)
    t = Task(id=new_id(), prompt="persist", priority=8)
    s.create(t)
    s.close()
    s2 = Store(path)
    assert s2.get(t.id).prompt == "persist"


# ---------- 분류기 ----------
def test_classifier_explicit_override():
    k, p, _ = classifier.classify("뭐든", explicit_priority=10)
    assert p == 10 and k == TaskKind.LIGHT.value


def test_classifier_heavy_keyword():
    k, p, _ = classifier.classify("전체 코드베이스를 리팩토링 해줘")
    assert k == TaskKind.HEAVY.value


def test_classifier_light_short():
    k, p, _ = classifier.classify("안녕?", source=TaskSource.CHAT.value)
    assert k == TaskKind.LIGHT.value and p >= 8


# ---------- 스케줄러 (FakeClient) ----------
class FakeClient:
    """run_id 발급 후, finalize 폴링 시 즉시 completed 를 반환하는 가짜 Hermes."""
    def __init__(self):
        self.started = []
        self._status = {}

    def start_run(self, prompt, **kw):
        rid = "run_" + new_id()[:8]
        self.started.append((rid, prompt, kw))
        self._status[rid] = {"status": "completed", "output": f"DONE:{prompt[:10]}"}
        return rid

    def get_run(self, run_id):
        return self._status[run_id]

    def stop_run(self, run_id):
        self._status[run_id] = {"status": "cancelled"}
        return {"status": "stopping"}

    def close(self):
        pass


def make_mgr(tmp_path, slots=1):
    s = Store(tmp_path / "q.db")
    c = FakeClient()
    return QueueManager(s, c, tmp_path / "QUEUE.MD", max_slots=slots), s, c


def test_scheduler_runs_highest_priority_first(tmp_path):
    mgr, store, client = make_mgr(tmp_path)
    mgr.submit("low background crawl 작업", priority=2)
    mgr.submit("urgent", priority=10)
    # 단일 슬롯: tick 마다 (이전 마감 → 다음 시작). quiescent 까지 반복.
    for _ in range(5):
        mgr.tick()
    states = {t.prompt: t.state for t in store.list()}
    assert all(v == TaskState.COMPLETED.value for v in states.values())
    # 높은 우선순위가 먼저 시작됐는지 (P3: 자율 프리앰블이 앞에 붙으므로 포함 검사)
    assert "urgent" in client.started[0][1]


def test_autonomous_preamble_on_start(tmp_path):
    """백그라운드 실행 프롬프트에 자율 완수 지시가 붙는다(P3)."""
    mgr, store, client = make_mgr(tmp_path)
    mgr.submit("리포트 작성", priority=5, kind="heavy")
    mgr.tick()
    sent = client.started[0][1]
    assert "자율 백그라운드 작업" in sent and "리포트 작성" in sent


def test_context_handoff_stored_and_sent(tmp_path):
    """제출 시 동봉한 직전 대화가 저장되고 백그라운드 실행에 전달된다(P3)."""
    import json as _j
    mgr, store, client = make_mgr(tmp_path)
    t = mgr.submit("이어서 정리해줘", priority=5, kind="heavy",
                   conversation_history=[{"role": "user", "content": "앞선 맥락"}])
    assert _j.loads(mgr.get(t.id).conversation_history)[0]["content"] == "앞선 맥락"
    mgr.tick()
    assert client.started[0][2].get("conversation_history") == [{"role": "user", "content": "앞선 맥락"}]


def test_result_needs_attention(tmp_path):
    """완료됐지만 되물음/실패한 결과를 사람 확인 필요로 판정(P4)."""
    from alphred.queue_manager import result_needs_attention
    assert result_needs_attention("어떤 정보를 원하시는지 알려주세요") is True
    assert result_needs_attention("죄송합니다. 파일을 생성할 수 없습니다.") is True
    assert result_needs_attention("") is True
    # 실제로 존재하는 파일을 저장했다고 보고 → 정상 완료.
    real = tmp_path / "report.pdf"
    real.write_text("ok", encoding="utf-8")
    assert result_needs_attention(f"보고서를 {real} 에 저장했습니다.") is False
    # 산출물 언급 없는 단순 요약 답변 → 정상 완료(오탐 없음).
    assert result_needs_attention("미국 증시는 전반적으로 상승했습니다.") is False


def test_claimed_missing_files_detects_hallucinated_save(tmp_path):
    """write_file 을 실제 호출하지 않고 '저장했다'고 거짓 보고한 환각을 적발(P4 보강)."""
    from alphred.queue_manager import claimed_missing_files, result_needs_attention
    ghost = tmp_path / "US_Market_Summary.pdf"   # 만들지 않음
    msg = f"조사를 마쳤습니다. 보고서를 {ghost} 에 저장했습니다."
    assert claimed_missing_files(msg) == [str(ghost)]
    assert result_needs_attention(msg) is True
    # 존재하는 파일이면 환각 아님.
    ghost.write_text("x", encoding="utf-8")
    assert claimed_missing_files(msg) == []
    # 저장 동사가 없으면 경로가 없어도 검사하지 않음(오탐 방지).
    assert claimed_missing_files("참고: /nonexistent/path/foo.txt 형식을 따릅니다.") == []


# ---------- §21 작업 심화도 + 검증 ----------
def test_plan_to_depth():
    """심화도: Light=low, Heavy(분해없음)=mid, 복합/heavy/다단계=high."""
    assert classifier.plan_to_depth(None, TaskKind.LIGHT.value) == "low"
    assert classifier.plan_to_depth(None, TaskKind.HEAVY.value) == "mid"
    small = {"subtasks": [{"kind": "search", "effort": "moderate", "tools": ["web_search"]}]}
    assert classifier.plan_to_depth(small, TaskKind.HEAVY.value) == "mid"
    big = {"subtasks": [
        {"kind": "search", "effort": "moderate", "tools": ["web_search"]},
        {"kind": "io", "effort": "moderate", "tools": ["write_file"]},
        {"kind": "compute", "effort": "heavy", "tools": ["execute_code"]},
    ]}
    assert classifier.plan_to_depth(big, TaskKind.HEAVY.value) == "high"
    # 파일변경 다단계도 high
    mut = {"subtasks": [
        {"kind": "io", "effort": "moderate", "tools": ["write_file"]},
        {"kind": "edit", "effort": "moderate", "tools": ["patch"]},
    ]}
    assert classifier.plan_to_depth(mut, TaskKind.HEAVY.value) == "high"


def test_verify_artifacts(tmp_path):
    """Tier0 결정적 검증: 누락/빈/형식불일치 적발, 유효 파일·산출물無는 통과."""
    from alphred.queue_manager import verify_artifacts
    # 산출물 주장 없음 → 통과(단, checked=0 으로 '검증 안 함' 구분)
    rep = verify_artifacts("증시는 상승했습니다.")
    assert rep["passed"] is True and rep["checked"] == 0
    # 누락 파일 → 실패
    ghost = tmp_path / "r.pdf"
    rep = verify_artifacts(f"보고서를 {ghost} 에 저장했습니다.")
    assert rep["passed"] is False and rep["checks"][0]["exists"] is False
    # 텍스트를 .pdf 로 저장(형식 불일치) → 실패
    fake_pdf = tmp_path / "fake.pdf"
    fake_pdf.write_text("이건 그냥 텍스트", encoding="utf-8")
    rep = verify_artifacts(f"보고서를 {fake_pdf} 에 저장했습니다.")
    assert rep["passed"] is False and rep["checks"][0]["ok"] is False
    # 유효 PDF 시그니처 → 통과
    real_pdf = tmp_path / "real.pdf"
    real_pdf.write_bytes(b"%PDF-1.4\n stuff")
    assert verify_artifacts(f"보고서를 {real_pdf} 에 저장했습니다.")["passed"] is True


def test_verify_artifacts_flexible_detection(tmp_path):
    """유연성: 따옴표 경로·다양한 확장자·미등록 형식 graceful·비파일 작업 구분."""
    from alphred.queue_manager import verify_artifacts, register_format
    # 따옴표로 감싼(공백 포함 가능) 경로도 탐지
    miss = tmp_path / "my report.csv"
    rep = verify_artifacts(f'결과를 "{miss}" 에 생성했습니다.')
    assert rep["passed"] is False and rep["checked"] == 1
    # 미등록 확장자(.csv)는 존재+비어있지않음만 → 통과(graceful)
    csv = tmp_path / "data.csv"
    csv.write_text("a,b\n1,2\n", encoding="utf-8")
    assert verify_artifacts(f"데이터를 {csv} 에 저장했습니다.")["passed"] is True
    # register_format 로 새 형식 추가가 즉시 반영(확장성)
    register_format(".csv", lambda p: (open(p, encoding="utf-8").readline().count(",") >= 1,
                                       "CSV 헤더 확인"))
    assert verify_artifacts(f"데이터를 {csv} 에 저장했습니다.")["passed"] is True
    bad_csv = tmp_path / "bad.csv"
    bad_csv.write_text("no-comma-here\n", encoding="utf-8")
    assert verify_artifacts(f"데이터를 {bad_csv} 에 저장했습니다.")["passed"] is False
    register_format(".csv", None)  # 정리(다른 테스트 영향 방지) — None 이면 graceful 분기


def test_claimed_paths_excludes_urls(tmp_path):
    """URL(https://...)을 Windows 드라이브(s:/)로 오인하지 않는다(회귀)."""
    from alphred.queue_manager import _claimed_file_paths, verify_artifacts
    assert _claimed_file_paths("자료 https://example.com/report.pdf 참고") == []
    keep = tmp_path / "r.pdf"
    got = _claimed_file_paths(f"보고서 {keep} 와 https://site/x.pdf 참조")
    assert got == [str(keep)]
    # URL 만 있는 결과는 검사 대상 없음 → 통과(거짓 실패 방지)
    assert verify_artifacts("결과는 https://x.com/a.pdf 에서 확인하세요.")["checked"] == 0


def test_finalize_needs_review_on_bad_artifact(tmp_path):
    """완료 run 이 없는/형식불량 파일을 주장하면 NeedsReview 로 마감(§21 Tier0)."""
    mgr, s, c = make_mgr(tmp_path)
    ghost = tmp_path / "out.pdf"   # 만들지 않음
    t = mgr.submit("보고서를 PDF로 만들어줘", kind="heavy")
    mgr.tick()                                     # Pending → In-Progress (start_run)
    rid = s.get(t.id).hermes_run_id
    c._status[rid] = {"status": "completed",
                      "output": f"완료했습니다. 보고서를 {ghost} 에 저장했습니다."}
    mgr.tick()                                     # finalize → 검증 실패
    got = s.get(t.id)
    assert got.state == TaskState.NEEDS_REVIEW.value
    assert got.verify_report and "out.pdf" in got.verify_report
    assert got.depth == "mid"


def test_finalize_completed_on_valid_artifact(tmp_path):
    """유효 산출물(또는 산출물 주장 없음)은 정상 Completed."""
    mgr, s, c = make_mgr(tmp_path)
    good = tmp_path / "ok.pdf"
    good.write_bytes(b"%PDF-1.5\n...")
    t = mgr.submit("보고서를 PDF로 만들어줘", kind="heavy")
    mgr.tick()
    rid = s.get(t.id).hermes_run_id
    c._status[rid] = {"status": "completed", "output": f"보고서를 {good} 에 저장했습니다."}
    mgr.tick()
    assert s.get(t.id).state == TaskState.COMPLETED.value


def test_verify_disabled_skips_gate(tmp_path):
    """verify=False 면 검증을 건너뛰고 항상 Completed(레거시 동작)."""
    s = Store(tmp_path / "q.db")
    c = FakeClient()
    mgr = QueueManager(s, c, tmp_path / "Q.MD", verify=False)
    ghost = tmp_path / "x.pdf"
    t = mgr.submit("보고서 만들어줘", kind="heavy")
    mgr.tick()
    rid = s.get(t.id).hermes_run_id
    c._status[rid] = {"status": "completed", "output": f"{ghost} 에 저장했습니다."}
    mgr.tick()
    assert s.get(t.id).state == TaskState.COMPLETED.value


# ---------- §21 V2: LLM-judge(Tier2) + 폐루프(Tier3) ----------
def test_estimate_cost():
    from alphred.classifier import estimate_cost
    e = estimate_cost(None, "low")
    assert e["band"] == "낮음" and e["est_llm_calls"] >= 1
    plan = {"subtasks": [{"tools": ["web_search"]}, {"tools": []}, {"tools": ["x"]}]}
    hi = estimate_cost(plan, "high", judge_enabled=True)
    assert hi["steps"] == 3 and hi["tool_steps"] == 2 and hi["band"] == "높음"
    # high+judge 가 더 비싸게 추정
    assert hi["est_llm_calls"] > estimate_cost(plan, "mid")["est_llm_calls"]


def test_suggestion_recorded_on_needs_review(tmp_path):
    """검증 실패 시 실행 가능한 제안이 verify_report 에 기록(§21 V3)."""
    import json as _j
    mgr, s, c = make_mgr(tmp_path)
    fake_pdf = tmp_path / "x.pdf"
    fake_pdf.write_text("그냥 텍스트", encoding="utf-8")   # 형식 불일치
    tid = _drive(mgr, s, c, f"보고서를 {fake_pdf} 에 저장했습니다.", depth="mid")  # mid → NeedsReview
    got = s.get(tid)
    assert got.state == TaskState.NEEDS_REVIEW.value
    rep = _j.loads(got.verify_report)
    assert rep.get("suggestion") and ".pdf" in rep["suggestion"]


def test_parse_verdict():
    from alphred.classifier import parse_verdict
    v = parse_verdict('{"verdict":"pass","score":88,"criteria":[{"name":"a","met":true}],'
                      '"unmet":[],"summary":"good"}')
    assert v["passed"] is True and v["score"] == 88 and v["criteria"][0]["met"] is True
    v = parse_verdict('{"verdict":"fail","unmet":["X 누락","Y 부족"],"summary":"미흡"}')
    assert v["passed"] is False and v["unmet"] == ["X 누락", "Y 부족"]
    # verdict 누락 시 score 로 보정
    assert parse_verdict('{"score":80}')["passed"] is True
    assert parse_verdict('{"score":40}')["passed"] is False
    assert parse_verdict("쓰레기") is None


def test_autonomous_input_injects_feedback():
    from alphred.queue_manager import _autonomous_input
    s = _autonomous_input("원요청", None, "- X 누락\n- Y 부족")
    assert "X 누락" in s and "보완" in s and "원요청" in s


def _drive(mgr, s, c, output, depth="high"):
    """heavy 작업을 제출→시작→커스텀 output 으로 마감(finalize)까지 구동."""
    t = mgr.submit("복합 보고서를 만들어줘", kind="heavy")
    s.update_fields(t.id, depth=depth)
    mgr.tick()                                  # start
    rid = s.get(t.id).hermes_run_id
    c._status[rid] = {"status": "completed", "output": output}
    mgr.tick()                                  # finalize
    return t.id


def test_judge_pass_completes(tmp_path):
    mgr, s, c = make_mgr(tmp_path)
    mgr.judge = lambda req, res: {"passed": True, "score": 90, "criteria": [],
                                  "unmet": [], "summary": "ok"}
    tid = _drive(mgr, s, c, "분석을 마쳤습니다.")
    got = s.get(tid)
    assert got.state == TaskState.COMPLETED.value
    assert '"judge"' in (got.verify_report or "")


def test_judge_fail_requeues_then_needs_review(tmp_path):
    """judge 미통과 → 피드백과 함께 재큐(Paused), 예산 소진 시 NeedsReview."""
    mgr, s, c = make_mgr(tmp_path)
    mgr.judge_max_retries = 1
    mgr.judge = lambda req, res: {"passed": False, "score": 20, "criteria": [],
                                  "unmet": ["근거 부족"], "summary": "미흡"}
    tid = _drive(mgr, s, c, "대충 했습니다.")
    got = s.get(tid)
    assert got.state == TaskState.PAUSED.value           # 재큐됨(백오프 중)
    assert got.verify_attempts == 1 and "근거 부족" in (got.verify_feedback or "")
    assert got.hermes_run_id is None                     # 새 run 으로 재시작 예정
    # 백오프 해제 후 재개 → 입력에 피드백이 주입되는지
    s.update_fields(tid, retry_not_before=None)
    mgr.tick()                                           # resume(start)
    assert "근거 부족" in c.started[-1][1]
    rid = s.get(tid).hermes_run_id
    c._status[rid] = {"status": "completed", "output": "여전히 대충"}
    mgr.tick()                                           # finalize → 예산(1) 소진
    assert s.get(tid).state == TaskState.NEEDS_REVIEW.value


def test_judge_only_runs_for_high_depth(tmp_path):
    mgr, s, c = make_mgr(tmp_path)
    calls = []
    mgr.judge = lambda req, res: (calls.append(1), {"passed": False, "unmet": ["x"],
                                                    "summary": "n"})[1]
    tid = _drive(mgr, s, c, "결과", depth="mid")          # mid → judge 미적용
    assert s.get(tid).state == TaskState.COMPLETED.value
    assert calls == []


def test_judge_fail_open_on_error(tmp_path):
    """judge 호출이 예외면 통과로 처리(좋은 작업을 막지 않음)."""
    mgr, s, c = make_mgr(tmp_path)
    def boom(req, res):
        raise RuntimeError("judge down")
    mgr.judge = boom
    tid = _drive(mgr, s, c, "결과입니다.")
    assert s.get(tid).state == TaskState.COMPLETED.value


def test_tier0_fail_skips_judge_and_self_heals(tmp_path):
    """Tier0 실패는 judge 없이도 high 면 역량 힌트와 함께 자가치유 재시도(§21 V3)."""
    mgr, s, c = make_mgr(tmp_path)
    calls = []
    mgr.judge = lambda req, res: (calls.append(1), {"passed": True})[1]
    ghost = tmp_path / "g.pdf"
    tid = _drive(mgr, s, c, f"보고서를 {ghost} 에 저장했습니다.")   # depth=high(_drive 기본)
    got = s.get(tid)
    assert got.state == TaskState.PAUSED.value          # NeedsReview 가 아니라 재시도
    assert got.verify_attempts == 1
    assert calls == []                                  # Tier0 실패 → judge 호출 안 함
    assert ".pdf" in (got.verify_feedback or "")        # 역량 힌트 주입


def test_tier0_fail_mid_no_retry(tmp_path):
    """mid 심화도는 자가치유 재시도 없이 바로 NeedsReview(쿼터 보호)."""
    mgr, s, c = make_mgr(tmp_path)
    ghost = tmp_path / "g.pdf"
    tid = _drive(mgr, s, c, f"보고서를 {ghost} 에 저장했습니다.", depth="mid")
    assert s.get(tid).state == TaskState.NEEDS_REVIEW.value


def test_classify_queue_status_is_light():
    """큐/작업 상태 조회는 Heavy 키워드가 섞여도 Light 즉답(백그라운드로 안 빠짐) — P2."""
    for q in ["큐에 등록된 작업들과 상태를 보여줘", "아까 작업 어떻게 됐어?",
              "보고서 결과 알려줘", "모든 작업 목록 보여줘", "작업 진행 상황 알려줘",
              "그거 완료됐어?"]:
        kind, prio, reason = classifier.classify(q, source="tui")
        assert kind == TaskKind.LIGHT.value, (q, kind, reason)


def test_classify_real_heavy_still_heavy():
    for q in ["전체 코드베이스를 리팩토링해줘", "이 사이트들 크롤링해서 분석해줘",
              "분석 작업 진행해줘"]:
        kind, _p, _r = classifier.classify(q, source="tui")
        assert kind == TaskKind.HEAVY.value, (q, kind)


def test_start_failure_transient_requeues(tmp_path):
    """시작 실패가 transient(연결거부 등)면 즉시 폐기하지 않고 재큐(Paused)해야 한다."""
    from alphred.db import Store
    s = Store(tmp_path / "q.db")

    class FailClient:
        def start_run(self, *a, **k):
            raise OSError("[WinError 10061] 대상 컴퓨터에서 연결을 거부했으므로 연결하지 못했습니다")
        def get_run(self, r): return {"status": "running"}
        def stop_run(self, r): return {}
        def close(self): pass

    mgr = QueueManager(s, FailClient(), tmp_path / "Q.MD")
    t = mgr.submit("heavy bg 작업", priority=5, kind="heavy")
    mgr.tick()
    got = mgr.get(t.id)
    assert got.state == TaskState.PAUSED.value   # 폐기 아님
    assert got.retries == 1


def test_ensure_upstream_gates_scheduling(tmp_path):
    """D1: 업스트림 미가동이면 보류(폐기 아님), 가동되면 매 틱 재평가해 처리."""
    mgr, store, client = make_mgr(tmp_path)
    mgr.submit("low background crawl 작업", priority=5, kind="heavy")
    up = {"ok": False}
    mgr.ensure_upstream = lambda: up["ok"]
    mgr.tick()
    assert all(t.state == TaskState.PENDING.value for t in store.list())  # 미가동 → 보류
    assert client.started == []
    up["ok"] = True
    for _ in range(3):
        mgr.tick()
    assert all(t.state == TaskState.COMPLETED.value for t in store.list())  # 가동 → 처리


def test_queue_md_written(tmp_path):
    mgr, store, client = make_mgr(tmp_path)
    mgr.submit("작업 A", priority=5)
    assert (tmp_path / "QUEUE.MD").exists()
    content = (tmp_path / "QUEUE.MD").read_text(encoding="utf-8")
    assert "작업 A" in content


def test_queue_md_includes_needs_review(tmp_path):
    mgr, store, client = make_mgr(tmp_path)
    t = mgr.submit("검토 대상", priority=5, kind="heavy")
    store.transition(t.id, TaskState.IN_PROGRESS)
    store.transition(t.id, TaskState.NEEDS_REVIEW, reason="verify failed")
    mgr.sync_md()
    content = (tmp_path / "QUEUE.MD").read_text(encoding="utf-8")
    assert "NeedsReview" in content
    assert "검토 대상" in content


def test_requeue_needs_review(tmp_path):
    mgr, store, client = make_mgr(tmp_path)
    t = mgr.submit("다시 시도", priority=5, kind="heavy")
    store.transition(t.id, TaskState.IN_PROGRESS)
    store.transition(t.id, TaskState.NEEDS_REVIEW, reason="verify failed")
    got = mgr.requeue(t.id)
    assert got.state == TaskState.PENDING.value
    assert store.events(t.id)[-1]["to_state"] == TaskState.PENDING.value


def test_single_slot_no_double_run(tmp_path):
    # 완료를 막는 클라이언트로 슬롯 점유 확인
    mgr, store, client = make_mgr(tmp_path)

    def never_complete(run_id):
        return {"status": "running"}
    client.get_run = never_complete

    mgr.submit("a", priority=5)
    mgr.submit("b", priority=5)
    mgr.tick()
    in_prog = store.in_progress()
    assert len(in_prog) == 1  # 단일 슬롯
    mgr.tick()
    assert len(store.in_progress()) == 1  # 여전히 1개만


def test_discard(tmp_path):
    mgr, store, client = make_mgr(tmp_path)
    t = mgr.submit("폐기 대상", priority=4)
    mgr.discard(t.id)
    assert store.get(t.id).state == TaskState.DISCARDED.value
