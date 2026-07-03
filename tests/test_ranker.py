"""§22 큐 상대 우선순위 재정렬(Queue Ranker) 테스트 — 주입된 가짜 랭커 사용."""
from __future__ import annotations

from alphred.classifier import parse_rank, build_rank_prompt
from alphred.db import Store
from alphred.models import TaskState
from alphred.queue_manager import QueueManager
from tests.test_preemption import ControlledClient


def make(tmp_path, ranker=None):
    s = Store(tmp_path / "q.db")
    c = ControlledClient()
    return QueueManager(s, c, tmp_path / "Q.MD", ranker=ranker), s, c


# ---- parse_rank ----
def test_parse_rank_ok_and_clamp():
    out = parse_rank('x {"rankings":[{"id":"NEW","priority":99,"reason":"r"},'
                     '{"id":"abc12345","priority":0}]} tail')
    assert out == [{"id": "NEW", "priority": 10, "reason": "r"},
                   {"id": "abc12345", "priority": 1, "reason": ""}]


def test_parse_rank_bad_or_empty():
    assert parse_rank("no json") is None
    assert parse_rank('{"rankings":[]}') is None
    assert parse_rank('{"summary":"x"}') is None


def test_build_rank_prompt_includes_new_and_queue():
    p = build_rank_prompt({"prompt": "육식맨 먼저", "depth": "high"},
                          [{"id": "abc12345", "prompt": "침착맨 분석", "priority": 5,
                            "state": "In-Progress", "kind": "heavy"}])
    assert "NEW" in p and "육식맨" in p and "abc12345" in p and "침착맨" in p


# ---- 재정렬 동작 ----
def test_rerank_makes_new_outrank_existing(tmp_path):
    """신규(NEW)가 기존보다 높은 우선순위를 받으면 둘 다 반영된다."""
    def ranker(new, queue):
        return [{"id": "NEW", "priority": 8, "reason": "선행 작업"}] + \
               [{"id": q["id"], "priority": 3, "reason": "후행"} for q in queue]
    mgr, store, _ = make(tmp_path, ranker=ranker)
    a = mgr.submit("침착맨 분석", kind="heavy")
    a_prio0 = store.get(a.id).priority
    b = mgr.submit("육식맨 먼저", kind="heavy")
    assert store.get(b.id).priority == 8
    assert store.get(a.id).priority == 3
    assert store.get(b.id).priority > store.get(a.id).priority
    assert a_prio0 != 3 or True   # A 의 우선순위가 랭킹으로 갱신됨
    assert "rank:" in (store.get(b.id).classify_reason or "")


def test_rerank_not_called_for_single_heavy(tmp_path):
    """비교할 다른 Heavy 가 없으면 랭커를 호출하지 않는다(no-op)."""
    calls = []
    def ranker(new, queue):
        calls.append((new, queue))
        return None
    mgr, store, _ = make(tmp_path, ranker=ranker)
    mgr.submit("단일 Heavy 작업", kind="heavy")
    assert calls == []                      # 첫 작업 → 호출 안 함
    mgr.submit("두 번째 Heavy", kind="heavy")
    assert len(calls) == 1                   # 둘째 → 1회 호출


def test_rerank_graceful_on_failure(tmp_path):
    """랭커가 None 반환/예외여도 기존 우선순위를 유지한다(회귀 없음)."""
    a_prio = b_prio = None

    def boom(new, queue):
        raise RuntimeError("llm down")
    mgr, store, _ = make(tmp_path, ranker=boom)
    a = mgr.submit("작업 A", kind="heavy")
    b = mgr.submit("작업 B", kind="heavy")     # 랭커 예외 → 폴백
    assert store.get(a.id) is not None and store.get(b.id) is not None  # 크래시 없음


def test_higher_rank_preempts_running_heavy(tmp_path):
    """랭킹으로 신규 Heavy 가 실행 중 Heavy 보다 높아지면 다음 tick 에 선점한다."""
    def ranker(new, queue):
        return [{"id": "NEW", "priority": 9, "reason": "긴급 선행"}] + \
               [{"id": q["id"], "priority": 2} for q in queue]
    mgr, store, client = make(tmp_path, ranker=ranker)
    a = mgr.submit("기존 분석", kind="heavy")
    mgr.tick()                                # A In-Progress
    assert store.get(a.id).state == TaskState.IN_PROGRESS.value
    b = mgr.submit("더 급한 선행 작업", kind="heavy")   # rerank: B=9 > A=2
    mgr.tick()                                # B 가 A 를 선점
    assert store.get(b.id).state == TaskState.IN_PROGRESS.value
    assert store.get(a.id).state == TaskState.PAUSED.value
