"""§34.7 질문 필요성 골든셋 평가 — 과잉질문 방지 게이트(QA-34.5) 검증.

골든셋(tests/eval/clarify.jsonl)의 card 는 IntentCard 출력(부족정보+critical)을 시뮬레이션한
것이다. 이 테스트는 **결정적 게이트**(classifier.needs_clarification)가 정책을 정확히
지키는지 검증한다: ①비대화형(cron/api/subservice) 질문 0 ②Light 질문 0 ③critical 아닌
부족정보 질문 0 ④critical+대화형+Heavy 만 질문. (critical 판정 자체의 라이브 정확도는
IntentCard 활성화 후 intent_log 로 측정 — prompt 열은 그 재사용을 위해 보존.)
"""
from __future__ import annotations

import json
from pathlib import Path

from alphred import classifier

GOLDEN = Path(__file__).parent / "eval" / "clarify.jsonl"


def _cases() -> list[dict]:
    return [json.loads(ln) for ln in GOLDEN.read_text(encoding="utf-8").splitlines()
            if ln.strip()]


def test_golden_set_shape():
    cases = _cases()
    assert len(cases) >= 30
    for c in cases:
        assert isinstance(c["should_ask"], bool)
        assert c["source"] in ("tui", "chat", "api", "cron", "subservice")


def test_clarify_gate_matches_golden():
    cases = _cases()
    miss = []
    for c in cases:
        got = classifier.needs_clarification(c.get("card"), source=c["source"],
                                             kind=c.get("kind", "heavy"))
        if got != c["should_ask"]:
            miss.append((c["prompt"][:40], got, c["should_ask"], c.get("note", "")))
    for m in miss:
        print("miss:", m)
    assert not miss                       # 게이트는 결정적 — 전 케이스 일치해야 함


def test_overask_rate_zero_on_should_not_ask():
    """QA-34.5 — '질문 불필요' 케이스에서 과잉질문율 0 (특히 cron/api 는 무조건 0)."""
    cases = [c for c in _cases() if not c["should_ask"]]
    overask = [c for c in cases
               if classifier.needs_clarification(c.get("card"), source=c["source"],
                                                 kind=c.get("kind", "heavy"))]
    assert not overask
    # 비대화형 소스는 critical 이어도 질문 0
    for src in ("cron", "api", "subservice"):
        card = {"missing_info": [{"what": "x", "critical": True}]}
        assert not classifier.needs_clarification(card, source=src, kind="heavy")
