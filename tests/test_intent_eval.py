"""§34.7 의도 분류 골든셋 평가 — 정규식 기준선 측정 + IntentCard 배선 개선 검증.

골든셋(tests/eval/intents.jsonl)은 한/영 51케이스로, 정규식이 잘 잡는 케이스와
"짧은 채팅이지만 산출물 요청" 같은 알려진 함정을 섞었다. 이 테스트는:
  1) 기준선 — 사전필터(플래그 전부 off 의 실제 폴백 경로)의 정확도를 측정·출력하고
     회귀 하한을 지킨다(정확도 수치 자체는 M1 as-built 기록 참조).
  2) 배선 — '완벽한' fake IntentCard 를 붙였을 때 fast-path 가 개선을 막지 않고
     파이프라인 정확도가 기준선 이상으로 오르는지 검증한다(LLM 0콜).
"""
from __future__ import annotations

import json
from pathlib import Path

from alphred import classifier
from alphred.db import Store
from alphred.queue_manager import QueueManager

GOLDEN = Path(__file__).parent / "eval" / "intents.jsonl"


def _cases() -> list[dict]:
    return [json.loads(ln) for ln in GOLDEN.read_text(encoding="utf-8").splitlines()
            if ln.strip()]


def _prefilter_accuracy() -> tuple[float, list[tuple[str, str, str]]]:
    cases = _cases()
    hit, misses = 0, []
    for c in cases:
        k, _p, _r, _amb = classifier.prefilter(c["prompt"], source=c.get("source", "api"))
        if k == c["expect_kind"]:
            hit += 1
        else:
            misses.append((c["prompt"][:44], k, c["expect_kind"]))
    return hit / len(cases), misses


def test_golden_set_shape():
    cases = _cases()
    assert len(cases) >= 50
    for c in cases:
        assert c["expect_kind"] in ("light", "heavy")
        assert c["prompt"].strip()


def test_prefilter_baseline_floor():
    acc, misses = _prefilter_accuracy()
    print(f"\n[intent-eval] 사전필터 기준선 정확도 = {acc:.0%} (미스 {len(misses)}건)")
    for p, got, want in misses:
        print(f"  miss: {p!r} → {got} (기대 {want})")
    # 회귀 하한 — 사전필터 튜닝이 골든셋에서 크게 후퇴하면 실패
    assert acc >= 0.70


class _Client:
    def close(self):
        pass


def test_intentcard_wiring_improves_over_baseline(tmp_path):
    """완벽한 IntentCard(정답 반환 fake)를 붙이면 파이프라인 정확도가 기준선 이상.

    fast-path(상태조회/설치류/아주 짧은 인사)는 정규식이 즉결하고, 나머지 전부가
    IntentCard 로 위임되는 배선을 end-to-end 로 확인한다(§34.2 A1).
    """
    cases = _cases()
    by_prompt = {c["prompt"]: c for c in cases}

    def perfect_intent(prompt, context=None):
        c = by_prompt[prompt]
        return {"goal": "eval", "domain": "other",
                "deliverable": {"type": None, "format": None},
                "kind": c["expect_kind"], "priority": 5,
                "depth": "low" if c["expect_kind"] == "light" else "mid",
                "missing_info": [], "confidence": 95}

    mgr = QueueManager(Store(tmp_path / "q.db"), _Client(), tmp_path / "Q.MD",
                       intent=perfect_intent)
    hit, misses = 0, []
    for c in cases:
        k, _p, _r = mgr.classify_only(c["prompt"], source=c.get("source", "api"))
        if k == c["expect_kind"]:
            hit += 1
        else:
            misses.append((c["prompt"][:44], k, c["expect_kind"]))
    acc = hit / len(cases)
    base, _ = _prefilter_accuracy()
    print(f"\n[intent-eval] IntentCard(완벽 가정) 정확도 = {acc:.0%} vs 기준선 {base:.0%}")
    for p, got, want in misses:
        print(f"  miss(fast-path 판정): {p!r} → {got} (기대 {want})")
    assert acc >= base            # IntentCard 가 기준선을 깎지 않음
    assert acc >= 0.95            # fast-path 가 개선을 막지 않음(잔여 미스는 즉결 케이스뿐)
