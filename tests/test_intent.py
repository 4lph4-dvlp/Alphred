"""§34.2 IntentCard 단위 테스트 — 파서 정규화 · fast-path · 폴백 · 저장 · 텔레메트리."""
from __future__ import annotations

import json

from alphred import classifier
from alphred.db import Store
from alphred.models import TaskKind
from alphred.queue_manager import QueueManager


# ---- parse_intent 정규화 ----
def test_parse_intent_valid_normalizes():
    txt = ('{"goal":"PDF 보고서 생성","domain":"document",'
           '"deliverable":{"type":"file","format":"PDF"},"kind":"heavy","priority":15,'
           '"depth":"HIGH","missing_info":[{"what":"분량","critical":false},'
           '{"what":"","critical":true}],"confidence":250}')
    card = classifier.parse_intent(txt)
    assert card["kind"] == "heavy"
    assert card["priority"] == 10          # clamp 1..10
    assert card["depth"] == "high"         # 소문자 정규화
    assert card["deliverable"]["format"] == "pdf"
    assert card["confidence"] == 100       # clamp 0..100
    assert len(card["missing_info"]) == 1  # what 없는 항목 제거


def test_parse_intent_bad_kind_or_json_is_none():
    assert classifier.parse_intent('{"kind":"medium"}') is None
    assert classifier.parse_intent("no json here") is None
    assert classifier.parse_intent("") is None


def test_parse_intent_defaults():
    card = classifier.parse_intent('{"kind":"light"}')
    assert card["priority"] == 8 and card["depth"] == "low"
    assert card["domain"] == "other" and card["missing_info"] == []
    assert card["confidence"] == 50


def test_intent_to_classification_realtime_floor():
    card = {"kind": "light", "priority": 5, "goal": "g", "confidence": 80}
    k, p, r = classifier.intent_to_classification(card, source="chat")
    assert k == "light" and p == 9 and r.startswith("intent(80)")
    k2, p2, _ = classifier.intent_to_classification(card, source="api")
    assert p2 == 5


# ---- fast-path 판정 ----
def test_is_fastpath_policy_reasons():
    assert classifier.is_fastpath("prefilter: queue/status query")
    assert classifier.is_fastpath("prefilter: skill/package install (slow admin op)")
    assert classifier.is_fastpath("explicit override")
    assert not classifier.is_fastpath("prefilter: explicit large-scope")
    assert not classifier.is_fastpath("prefilter: ambiguous (needs planning)")


def test_is_fastpath_length_based_only_very_short():
    for r in ("prefilter: realtime chat", "prefilter: greeting/short"):
        assert classifier.is_fastpath(r, "안녕!")                  # ≤12자 즉결
        assert classifier.is_fastpath(r, "검색해줘 오늘 금값")        # Light 패턴
        # 25자 안팎이어도 산출물 요청이면 IntentCard 로 넘어가야 함
        assert not classifier.is_fastpath(r, "서버 로그 분석해서 장애 원인 보고서 만들어줘")
        assert not classifier.is_fastpath(r, "우리 회사 소개 자료를 파워포인트로 하나 만들어줘")


# ---- QueueManager 배선 ----
class _Client:
    def close(self):
        pass


def _mgr(tmp_path, intent):
    return QueueManager(Store(tmp_path / "q.db"), _Client(), tmp_path / "Q.MD", intent=intent)


def test_fastpath_skips_intent_llm(tmp_path):
    calls = []

    def intent(prompt, context=None):
        calls.append(prompt)
        return {"kind": "heavy", "priority": 4, "depth": "mid", "goal": "g",
                "missing_info": [], "confidence": 90}

    mgr = _mgr(tmp_path, intent)
    k, _p, _r = mgr.classify_only("큐 상태 보여줘", source="tui")
    assert k == TaskKind.LIGHT.value and not calls      # 상태조회 즉결 — LLM 미호출
    k, _p, _r = mgr.classify_only("안녕!", source="tui")
    assert k == TaskKind.LIGHT.value and not calls      # 짧은 인사 즉결


def test_intent_decides_nonfastpath_and_cached(tmp_path):
    calls = []

    def intent(prompt, context=None):
        calls.append(prompt)
        return {"kind": "heavy", "priority": 6, "depth": "high", "goal": "산출물 생성",
                "missing_info": [], "confidence": 88}

    mgr = _mgr(tmp_path, intent)
    msg = "우리 회사 소개 자료를 파워포인트로 하나 만들어줘"
    k, p, r = mgr.classify_only(msg, source="tui")
    assert k == TaskKind.HEAVY.value and p == 6 and r.startswith("intent(88)")
    mgr.classify_only(msg, source="tui")
    assert len(calls) == 1                              # 캐시 — 재질의 없음


def test_intent_failure_falls_back_to_prefilter(tmp_path):
    def bad_intent(prompt, context=None):
        raise RuntimeError("llm down")

    mgr = _mgr(tmp_path, bad_intent)
    # 확신-Heavy 는 intent 실패 시 사전필터 판정 유지
    k, _p, r = mgr.classify_only("전체 코드베이스 리팩토링 해줘")
    assert k == TaskKind.HEAVY.value and r.startswith("prefilter")

    def none_intent(prompt, context=None):
        return None

    mgr2 = _mgr(tmp_path, none_intent)
    k2, _p2, r2 = mgr2.classify_only("전체 코드베이스 리팩토링 해줘")
    assert k2 == TaskKind.HEAVY.value and r2.startswith("prefilter")


def test_submit_stores_intent_and_depth(tmp_path):
    def intent(prompt, context=None):
        return {"kind": "heavy", "priority": 5, "depth": "high", "goal": "보고서",
                "deliverable": {"type": "file", "format": "pdf"},
                "missing_info": [{"what": "분량", "critical": False}], "confidence": 85}

    mgr = _mgr(tmp_path, intent)
    t = mgr.submit("서버 로그 분석해서 장애 원인 보고서 만들어줘", source="tui")
    assert t.kind == TaskKind.HEAVY.value
    assert t.depth == "high"                            # IntentCard depth 채택
    card = json.loads(t.intent)
    assert card["deliverable"]["format"] == "pdf"
    assert card["missing_info"][0]["what"] == "분량"


def test_explicit_depth_overrides_intent(tmp_path):
    def intent(prompt, context=None):
        return {"kind": "heavy", "priority": 5, "depth": "high", "goal": "g",
                "missing_info": [], "confidence": 85}

    mgr = _mgr(tmp_path, intent)
    t = mgr.submit("서버 로그 분석해서 장애 원인 보고서 만들어줘", source="tui", depth="low")
    assert t.depth == "low"                             # 사용자 명시 오버라이드 우선


def test_intent_log_records_engine(tmp_path):
    def intent(prompt, context=None):
        return {"kind": "heavy", "priority": 6, "depth": "mid", "goal": "g",
                "missing_info": [], "confidence": 90}

    mgr = _mgr(tmp_path, intent)
    mgr.classify_only("안녕!", source="tui")                                   # fastpath
    mgr.classify_only("우리 회사 소개 자료를 파워포인트로 하나 만들어줘", source="tui")  # intent
    mgr.classify_only("아무거나", source="api", explicit_kind="heavy")          # explicit
    stats = mgr.store.intent_stats()
    assert "fastpath" in stats and "intent" in stats and "explicit" in stats
    assert stats["intent"].get("heavy") == 1


def test_intent_off_pipeline_unchanged(tmp_path):
    """intent=None(기본 off)이면 기존 사전필터/플래너 경로 그대로 — 회귀 없음."""
    mgr = QueueManager(Store(tmp_path / "q.db"), _Client(), tmp_path / "Q.MD")
    k, _p, r = mgr.classify_only("전체 코드베이스 리팩토링 해줘")
    assert k == TaskKind.HEAVY.value and r == "prefilter: explicit large-scope"
    # chat 소스 ≤80자 = 실시간 즉결(기존 동작 유지)
    k2, _p2, r2 = mgr.classify_only("우리 회사 소개 자료를 파워포인트로 하나 만들어줘",
                                    source="chat")
    assert k2 == TaskKind.LIGHT.value and r2 == "prefilter: realtime chat"
    # tui 소스는 실시간 소스가 아님 → 모호 → 보수적 Heavy(기존 동작 유지)
    k3, _p3, r3 = mgr.classify_only("우리 회사 소개 자료를 파워포인트로 하나 만들어줘",
                                    source="tui")
    assert k3 == TaskKind.HEAVY.value and r3 == "prefilter: ambiguous (needs planning)"
