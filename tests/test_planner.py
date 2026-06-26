"""계획 기반 분류 테스트 (§19) — 3-tier 사전필터 · 플래너 파서 · 결정적 매핑 · 통합."""
from __future__ import annotations

import json

from alphred import classifier
from alphred.db import Store
from alphred.models import TaskKind
from alphred.queue_manager import QueueManager, _plan_hint


# ---- 3-tier 사전필터 ----
def test_prefilter_status_query_is_light():
    k, _p, _r, amb = classifier.prefilter("큐에 등록된 작업들 상태 보여줘")
    assert k == TaskKind.LIGHT.value and amb is False


def test_prefilter_confident_heavy():
    k, _p, _r, amb = classifier.prefilter("전체 코드베이스 리팩토링 분석")
    assert k == TaskKind.HEAVY.value and amb is False


def test_prefilter_two_heavy_keywords_confident():
    k, _p, _r, amb = classifier.prefilter("리팩토링하고 분석해줘")
    assert k == TaskKind.HEAVY.value and amb is False


def test_prefilter_greeting_light():
    k, _p, _r, amb = classifier.prefilter("안녕?")
    assert k == TaskKind.LIGHT.value and amb is False


def test_prefilter_single_weak_keyword_is_ambiguous():
    # 무거운 키워드 1개 + 중간 길이 → 확신 못 함 → 플래너 위임
    _k, _p, _r, amb = classifier.prefilter(
        "이 자료를 살펴보고 알맞게 다뤄주면 좋겠어 천천히 진행해도 괜찮아")
    assert amb is True


# ---- 플래너 파서 ----
def test_parse_plan_valid_and_normalizes():
    txt = ('{"subtasks":[{"title":"수집","kind":"search","effort":"moderate","tools":["web_search"]},'
           '{"title":"정리","kind":"weird","effort":"???"}],"urgent":true}')
    plan = classifier.parse_plan(txt)
    assert plan["urgent"] is True
    assert len(plan["subtasks"]) == 2
    assert plan["subtasks"][1]["kind"] == "chat"        # 잘못된 값 → 기본 chat
    assert plan["subtasks"][1]["effort"] == "moderate"  # 잘못된 값 → 기본 moderate


def test_parse_plan_invalid():
    assert classifier.parse_plan("no json") is None
    assert classifier.parse_plan('{"subtasks":[]}') is None
    assert classifier.parse_plan("") is None


# ---- 결정적 매핑 ----
def test_plan_to_weight_heavy_by_steps():
    plan = {"subtasks": [{"kind": "chat", "effort": "trivial"}] * 3}
    assert classifier.plan_to_weight(plan)[0] == TaskKind.HEAVY.value


def test_plan_to_weight_heavy_by_edit_kind():
    plan = {"subtasks": [{"kind": "edit", "effort": "moderate"}]}
    assert classifier.plan_to_weight(plan)[0] == TaskKind.HEAVY.value


def test_plan_to_weight_light_single_trivial():
    plan = {"subtasks": [{"kind": "chat", "effort": "trivial"}], "urgent": False}
    k, prio, _r = classifier.plan_to_weight(plan)
    assert k == TaskKind.LIGHT.value and prio == 8


# ---- QueueManager 통합 ----
class _Client:
    def close(self):
        pass


def _mgr(tmp_path, planner):
    return QueueManager(Store(tmp_path / "q.db"), _Client(), tmp_path / "Q.MD", planner=planner)


def test_planner_called_only_when_ambiguous_and_plan_stored(tmp_path):
    calls = []
    plan = {"subtasks": [{"title": "분석", "kind": "compute", "effort": "heavy", "tools": []}],
            "urgent": False}

    def planner(prompt):
        calls.append(prompt)
        return plan

    mgr = _mgr(tmp_path, planner)

    # 확신-Heavy → 플래너 미호출, plan 없음
    t = mgr.submit("전체 코드베이스 리팩토링 분석")
    assert t.kind == TaskKind.HEAVY.value and not calls and t.plan is None

    # 모호 → 플래너 호출 → 결정적 규칙(Heavy: edit/compute) + plan 저장
    amb = "이 자료를 살펴보고 알맞게 다뤄주면 좋겠어 천천히 진행해도 괜찮아"
    t2 = mgr.submit(amb)
    assert calls and t2.kind == TaskKind.HEAVY.value
    assert json.loads(t2.plan)["subtasks"][0]["kind"] == "compute"


def test_planner_result_cached(tmp_path):
    calls = []

    def planner(prompt):
        calls.append(prompt)
        return {"subtasks": [{"title": "x", "kind": "chat", "effort": "trivial"}], "urgent": False}

    mgr = _mgr(tmp_path, planner)
    amb = "이 자료를 살펴보고 알맞게 다뤄주면 좋겠어 천천히 진행해도 괜찮아"
    mgr.classify_only(amb)
    mgr.classify_only(amb)
    assert len(calls) == 1  # 동일 프롬프트 재분해 안 함(캐시)


def test_set_progress_only_while_in_progress(tmp_path):
    from alphred.models import TaskState
    mgr = _mgr(tmp_path, None)
    t = mgr.submit("전체 코드베이스 리팩토링 분석")          # Heavy → Pending
    mgr.store.transition(t.id, TaskState.IN_PROGRESS)
    mgr._set_progress(t.id, progress=2, activity="web_search")
    got = mgr.store.get(t.id)
    assert got.plan_progress == 2 and got.plan_activity == "web_search"
    # 종료 후에는 더 갱신하지 않음(추적 스레드 잔류 방지)
    mgr.store.transition(t.id, TaskState.COMPLETED)
    mgr._set_progress(t.id, progress=9, activity="late")
    assert mgr.store.get(t.id).plan_progress == 2


def test_submit_with_precomputed_classification_stores_plan(tmp_path):
    """게이트웨이 경로: classify_full 로 분해한 plan 을 submit 에 넘기면 그대로 저장(재분류 X)."""
    calls = []

    def planner(prompt):
        calls.append(prompt)
        return {"subtasks": [{"title": "x", "kind": "compute", "effort": "heavy", "tools": []}],
                "urgent": False}

    mgr = _mgr(tmp_path, planner)
    k, p, reason, plan = mgr.classify_full(
        "이 자료를 살펴보고 알맞게 다뤄주면 좋겠어 천천히 진행해도 괜찮아")
    assert k == TaskKind.HEAVY.value and plan is not None
    t = mgr.submit("...", source="tui", kind=k, priority=p, plan=plan, classify_reason=reason)
    # 사전계산 분류를 그대로 사용(재분류 안 함 → 플래너 추가 호출 없음) + plan 저장됨
    assert len(calls) == 1
    assert json.loads(t.plan)["subtasks"][0]["kind"] == "compute"
    assert t.classify_reason == reason


def test_progress_tracker_skipped_for_fake_client(tmp_path):
    mgr = _mgr(tmp_path, None)               # _Client 는 _http/base_url 없음
    mgr._spawn_progress_tracker("anytask", "anyrun")   # 예외 없이 no-op


def test_plan_hint_injected_into_input():
    plan = {"subtasks": [{"title": "데이터 수집", "kind": "search", "effort": "moderate",
                          "tools": ["web_search"]}]}
    hint = _plan_hint(plan)
    assert "데이터 수집" in hint and "web_search" in hint
    assert _plan_hint(None) == ""
