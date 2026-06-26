"""Classifier LLM 폴백 테스트 (QA-2.3) — 모호할 때만 LLM 질의, 주입식."""
from __future__ import annotations

from alphred import classifier
from alphred.classifier import parse_classification
from alphred.db import Store, new_id
from alphred.models import TaskKind
from alphred.queue_manager import QueueManager, make_hermes_classifier


# ---- 파서 ----
def test_parse_valid_json():
    r = parse_classification('{"kind":"heavy","priority":3,"reason":"big analysis"}')
    assert r == ("heavy", 3, "big analysis")


def test_parse_json_in_markdown():
    txt = "Sure!\n```json\n{\"kind\":\"light\",\"priority\":9,\"reason\":\"quick\"}\n```"
    assert parse_classification(txt)[0] == "light"


def test_parse_invalid():
    assert parse_classification("no json here") is None
    assert parse_classification('{"kind":"weird"}') is None
    assert parse_classification("") is None


def test_parse_clamps_priority():
    assert parse_classification('{"kind":"light","priority":99}')[1] == 10


# ---- classify_only 통합 ----
class _Client:
    def close(self): pass


def mgr_with_llm(tmp_path, llm):
    return QueueManager(Store(tmp_path / "q.db"), _Client(), tmp_path / "Q.MD", llm_classify=llm)


def test_llm_called_only_when_ambiguous(tmp_path):
    calls = []
    def llm(prompt):
        calls.append(prompt)
        return ("heavy", 2, "llm decided")
    mgr = mgr_with_llm(tmp_path, llm)

    # 명확한 heavy 키워드 → 휴리스틱이 처리, LLM 미호출
    k, p, r = mgr.classify_only("전체 리팩토링 분석")
    assert k == "heavy" and not calls

    # 명확한 light(짧음) → LLM 미호출
    mgr.classify_only("hi")
    assert not calls

    # 모호한 입력(길고 키워드 없음) → LLM 호출
    ambiguous = "please take a look at this whenever you get a chance and handle it however you think is best, no rush"
    k, p, r = mgr.classify_only(ambiguous)
    assert calls and k == "heavy" and p == 2 and r.startswith("llm:")


def test_llm_failure_falls_back_to_heuristic(tmp_path):
    def llm(prompt):
        raise RuntimeError("LLM down")
    mgr = mgr_with_llm(tmp_path, llm)
    # 모호 입력이라 LLM 시도 → 실패 → 휴리스틱 기본값 유지(heavy)
    ambiguous = "could you go over the thing from before and continue where we left off when you have some free time later"
    k, p, r = mgr.classify_only(ambiguous)
    assert k == TaskKind.HEAVY.value


def test_explicit_override_skips_llm(tmp_path):
    calls = []
    mgr = mgr_with_llm(tmp_path, lambda p: calls.append(p) or ("light", 9, "x"))
    mgr.classify_only("아무 모호한 말", explicit_priority=1, explicit_kind="heavy")
    assert not calls   # 명시 오버라이드는 LLM 건너뜀


def test_make_hermes_classifier(tmp_path):
    class FakeHermes:
        def chat_completion(self, body):
            return {"choices": [{"message": {"content":
                    '{"kind":"heavy","priority":4,"reason":"from hermes"}'}}]}
    fn = make_hermes_classifier(FakeHermes())
    assert fn("뭔가 모호") == ("heavy", 4, "from hermes")
