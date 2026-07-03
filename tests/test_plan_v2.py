"""§34.3 Plan v2 테스트 — 파서·접지(갭 검출/수리)·디스패치 통합·드라이런·파생함수."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from alphred import classifier
from alphred.capabilities import apply_gap_fixes, plan_gaps, planner_context
from alphred.db import Store, new_id
from alphred.gateway import create_app
from alphred.models import TaskKind, TaskState
from alphred.prompt import _plan_hint
from alphred.queue_manager import QueueManager
from alphred.tui_queue import plan_checklist_lines


# ---- parse_plan_v2 ----
def test_parse_plan_v2_normalizes():
    txt = json.dumps({"dod": "PDF 보고서 완성", "steps": [
        {"goal": "자료 조사", "tool_hint": "web_search", "needs": ["sX"],
         "expected": {"type": "TEXT"}},
        {"id": "make", "goal": "PDF 생성", "tool_hint": "execute_code",
         "expected": {"type": "file", "format": ".PDF", "path_hint": "out.pdf"},
         "accept": [{"check": "file", "arg": "out.pdf"}, {"check": "weird"}]},
        {"no_goal": True},
    ]})
    p = classifier.parse_plan_v2(txt)
    assert p["version"] == 2 and p["dod"] == "PDF 보고서 완성"
    assert len(p["steps"]) == 2                      # goal 없는 스텝 제거
    s1, s2 = p["steps"]
    assert s1["id"] == "s1" and s1["expected"]["type"] == "text"
    assert s1["needs"] == []                         # 실존하지 않는 선행 id 제거
    assert s2["id"] == "make" and s2["expected"]["format"] == "pdf"
    assert s2["accept"] == [{"check": "file", "arg": "out.pdf"}]  # 잘못된 check 제거


def test_parse_plan_v2_derives_accept_from_expected():
    txt = json.dumps({"steps": [
        {"goal": "docx 생성", "expected": {"type": "file", "format": "docx"}}]})
    p = classifier.parse_plan_v2(txt)
    assert p["steps"][0]["accept"] == [{"check": "file", "arg": ""}]


def test_parse_plan_v2_invalid_is_none():
    assert classifier.parse_plan_v2("no json") is None
    assert classifier.parse_plan_v2('{"steps":[]}') is None
    assert classifier.parse_plan_v2('{"steps":[{"no_goal":1}]}') is None


# ---- 파생 함수 v2 호환 ----
def _v2(steps):
    return {"version": 2, "steps": steps}


def test_plan_to_depth_v2():
    hi = _v2([{"goal": "a", "tool_hint": "execute_code",
               "expected": {"type": "file"}, "accept": []},
              {"goal": "b", "tool_hint": "terminal",
               "expected": {"type": "action"}, "accept": []}])
    assert classifier.plan_to_depth(hi, TaskKind.HEAVY.value) == "high"   # mutating+tool 2
    lo = _v2([{"goal": "a", "tool_hint": None, "expected": {"type": "text"}, "accept": []}])
    assert classifier.plan_to_depth(lo, TaskKind.HEAVY.value) == "mid"
    assert classifier.plan_to_depth(hi, TaskKind.LIGHT.value) == "low"


def test_estimate_cost_v2():
    p = _v2([{"goal": "a", "tool_hint": "execute_code"},
             {"goal": "b", "tool_hint": "none"},
             {"goal": "c", "tool_hint": "web_search"}])
    est = classifier.estimate_cost(p, "mid")
    assert est["steps"] == 3 and est["tool_steps"] == 2


def test_plan_hint_v2_renders_dod_and_accept():
    p = {"version": 2, "dod": "보고서 완성",
         "steps": [{"id": "s1", "goal": "PDF 생성", "tool_hint": "execute_code",
                    "needs": [], "expected": {"type": "file", "format": "pdf",
                                              "path_hint": "out.pdf"},
                    "accept": [{"check": "file", "arg": "out.pdf"}]}]}
    out = _plan_hint(p)
    assert "EXECUTION PLAN" in out and "DoD: 보고서 완성" in out
    assert "done-when[file]: out.pdf" in out and "produces: file(pdf)" in out


# ---- 능력 접지 ----
_SNAP = {
    "skills": {"ok": True, "items": [{"name": "powerpoint", "description": "pptx"}]},
    "cli_agents": {"ok": True, "items": {"agy": {"found": False},
                                         "claude": {"found": True, "version": "1.0"}}},
    "pylibs": {"ok": True, "available": ["reportlab"], "missing": ["docx"]},
    "formats": {"pdf": {"capable": True, "via": "reportlab"},
                "docx": {"capable": False, "via": None, "install": "python-docx"},
                "pptx": {"capable": True, "via": "skill:powerpoint"}},
    "mcp": {"ok": True, "servers": []},
    "toolsets": {"ok": True, "tools": ["write_file"]},
}


def test_planner_context_compact():
    ctx = planner_context(_SNAP)
    assert "powerpoint" in ctx and "claude" in ctx and "reportlab" in ctx
    assert "docx(install python-docx)" in ctx


def test_plan_gaps_detects_skill_cli_format():
    plan = _v2([
        {"id": "s1", "goal": "덱 작성", "tool_hint": "skill:excel-author",
         "needs": [], "expected": {"type": "file", "format": "docx"}, "accept": []},
        {"id": "s2", "goal": "코드 작업", "tool_hint": "cli:agy",
         "needs": [], "expected": {"type": "text"}, "accept": []},
    ])
    gaps = plan_gaps(plan, _SNAP)
    kinds = {(g["kind"], g.get("name") or g.get("fmt")) for g in gaps}
    assert ("skill", "excel-author") in kinds
    assert ("cli", "agy") in kinds
    assert ("format", "docx") in kinds


def test_plan_gaps_none_when_grounded():
    plan = _v2([{"id": "s1", "goal": "pptx 작성", "tool_hint": "skill:powerpoint",
                 "needs": [], "expected": {"type": "file", "format": "pptx"},
                 "accept": []}])
    assert plan_gaps(plan, _SNAP) == []


def test_apply_gap_fixes_inserts_install_and_downgrades():
    plan = _v2([
        {"id": "s1", "goal": "docx 작성", "tool_hint": "skill:excel-author",
         "needs": [], "expected": {"type": "file", "format": "docx"}, "accept": []}])
    gaps = plan_gaps(plan, _SNAP)
    fixed = apply_gap_fixes(plan, gaps)
    assert "python-docx" in fixed["steps"][0]["goal"]           # 설치 스텝이 맨 앞
    assert fixed["steps"][0]["accept"] == [{"check": "exit_code", "arg": "0"}]
    body = [s for s in fixed["steps"] if s["id"] == "s1"][0]
    assert body["tool_hint"] == "execute_code"                  # 없는 스킬 → 강등
    assert body["needs"] == [fixed["steps"][0]["id"]]           # 설치 후 실행 의존성
    assert fixed["gaps"]                                        # 수리 내역 표면화


def test_install_step_in_plan_suppresses_format_gap():
    plan = _v2([
        {"id": "s0", "goal": "Install python-docx first via uv pip install python-docx",
         "tool_hint": "terminal", "needs": [], "expected": {"type": "action"},
         "accept": [{"check": "exit_code", "arg": "0"}]},
        {"id": "s1", "goal": "docx 작성", "tool_hint": "execute_code",
         "needs": ["s0"], "expected": {"type": "file", "format": "docx"}, "accept": []}])
    assert not [g for g in plan_gaps(plan, _SNAP) if g["kind"] == "format"]


# ---- 디스패치 통합 ----
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


_PLAN2 = {"version": 2, "dod": "완성",
          "steps": [{"id": "s1", "goal": "조사", "tool_hint": "web_search", "needs": [],
                     "expected": {"type": "text", "format": None, "path_hint": None},
                     "accept": []},
                    {"id": "s2", "goal": "작성", "tool_hint": "execute_code", "needs": ["s1"],
                     "expected": {"type": "file", "format": "md", "path_hint": None},
                     "accept": [{"check": "file", "arg": ""}]}]}


def _mgr(tmp_path, planner2, **kw):
    return QueueManager(Store(tmp_path / "q.db"), _Client(), tmp_path / "Q.MD",
                        planner2=planner2, **kw)


def test_dispatch_generates_and_stores_plan_v2(tmp_path):
    calls = []

    def planner2(prompt, **kw):
        calls.append((prompt, kw))
        return dict(_PLAN2)

    mgr = _mgr(tmp_path, planner2)
    t = mgr.submit("전체 코드베이스 리팩토링 해줘")          # 확신-Heavy, 계획 없음
    assert t.plan is None
    mgr.tick()                                             # 디스패치 → 계획 생성·저장·주입
    assert len(calls) == 1
    stored = json.loads(mgr.store.get(t.id).plan)
    assert stored["version"] == 2 and len(stored["steps"]) == 2
    assert "EXECUTION PLAN" in mgr.client.inputs[0] and "DoD: 완성" in mgr.client.inputs[0]


def test_dispatch_skips_when_v2_exists_and_on_resume(tmp_path):
    calls = []

    def planner2(prompt, **kw):
        calls.append(prompt)
        return dict(_PLAN2)

    mgr = _mgr(tmp_path, planner2)
    t = mgr.submit("전체 코드베이스 리팩토링 해줘")
    mgr.tick()
    assert len(calls) == 1
    # 재개 경로: Paused 로 되돌린 뒤 다시 시작해도 재계획 없음(저장된 v2 재사용)
    mgr.store.transition(t.id, TaskState.PAUSED, reason="test", hermes_run_id=None)
    mgr.tick()
    assert len(calls) == 1


def test_dispatch_planner_failure_fail_open(tmp_path):
    def bad(prompt, **kw):
        raise RuntimeError("llm down")

    mgr = _mgr(tmp_path, bad)
    t = mgr.submit("전체 코드베이스 리팩토링 해줘")
    mgr.tick()                                             # 계획 실패해도 실행은 진행
    assert mgr.client.inputs
    assert mgr.store.get(t.id).state in (TaskState.IN_PROGRESS.value,
                                         TaskState.COMPLETED.value)


def test_dispatch_passes_intake_and_draft(tmp_path):
    seen = {}

    def planner2(prompt, **kw):
        seen.update(kw)
        return dict(_PLAN2)

    def planner_v1(prompt):                                # §19 모호 분류용 v1
        return {"subtasks": [{"title": "초안", "kind": "compute", "effort": "heavy",
                              "tools": []}], "urgent": False}

    mgr = _mgr(tmp_path, planner2, planner=planner_v1)
    amb = "이 자료를 살펴보고 알맞게 다뤄주면 좋겠어 천천히 진행해도 괜찮아"
    t = mgr.submit(amb)                                    # 모호 → v1 계획으로 분류
    assert json.loads(t.plan)["subtasks"]
    mgr.store.update_fields(t.id, answers=json.dumps(["MD 로"], ensure_ascii=False),
                            questions=json.dumps([{"q": "형식은?"}], ensure_ascii=False))
    mgr.tick()
    assert (seen.get("draft") or {}).get("subtasks")       # v1 초안 전달
    assert "MD 로" in (seen.get("intake") or "")           # 인테이크 답변 전달


# ---- 게이트웨이 /plan v2 ----
def test_plan_endpoint_shows_v2(tmp_path):
    store = Store(tmp_path / "g.db")
    fake = _Client()
    mgr = QueueManager(store, fake, tmp_path / "QUEUE.MD",
                       planner2=lambda prompt, **kw: dict(_PLAN2))
    app = create_app(mgr=mgr, scheduler_interval=3600)
    with TestClient(app) as tc:
        r = tc.post("/plan", json={"message": "전체 코드베이스 리팩토링 해줘"})
        d = r.json()
        assert d["kind"] == "heavy"
        assert d["plan"]["version"] == 2
        assert d["plan"]["steps"][1]["accept"] == [{"check": "file", "arg": ""}]
        assert d["estimate"]["steps"] == 2


# ---- TUI 체크리스트 v2 ----
def test_plan_checklist_lines_v2_and_gaps():
    plan = {**_PLAN2, "gaps": ["docx 생성 수단 부재 → 설치 스텝 자동 삽입(python-docx)"]}
    lines = plan_checklist_lines(plan, 1, "In-Progress")
    joined = "\n".join(lines)
    assert "실행 계획(v2)" in joined and "DoD: 완성" in joined
    assert "✓" in joined and "▶" in joined                 # 진행 휴리스틱 동작
    assert "⚙ 접지" in joined
    # v1 도 여전히 렌더
    v1 = {"subtasks": [{"title": "x", "kind": "chat", "effort": "trivial", "tools": []}]}
    assert "하위작업 계획" in "\n".join(plan_checklist_lines(v1, 0, "Pending"))