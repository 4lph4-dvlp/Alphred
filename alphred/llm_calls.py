"""Hermes(:8642) chat/completions 를 쓰는 보조 LLM 콜러블 팩토리.

분류·계획분해·수용judge·큐랭킹·MoA 는 모두 같은 모양이다:
요청 프롬프트를 만들어 chat_completion 으로 보내고, 응답 텍스트를 파서로 구조화한다.
공통 골격은 `_chat()` 하나로 모으고, 각 팩토리는 (프롬프트 빌더, 파서)만 다르다.

보조 작업도 **메인 모델**(model="hermes-agent" → 게이트웨이 default)을 쓴다 — 약한 보조모델로의
하향 라우팅(보고서 #2 '인지 오염')을 피하기 위함이다.
"""
from __future__ import annotations

from . import classifier
from .hermes_client import HermesClient


def _chat(client: HermesClient, model: str, content: str) -> str | None:
    """단일 user 메시지를 보내고 assistant 텍스트를 추출. 형식 이상 시 None(fail-open)."""
    resp = client.chat_completion({"model": model,
                                   "messages": [{"role": "user", "content": content}]})
    try:
        return resp["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return None


def make_hermes_classifier(client: HermesClient, model: str = "hermes-agent"):
    """모호한 입력을 분류하는 콜러블. 반환: prompt -> (kind, priority, reason) | None."""
    def _classify(prompt: str):
        text = _chat(client, model, classifier.build_llm_prompt(prompt))
        return classifier.parse_classification(text) if text is not None else None
    return _classify


def make_hermes_intent(client: HermesClient, model: str = "hermes-agent"):
    """§34.2 IntentCard — 의도(kind/priority/depth/missing_info)를 콜 1회로 통합 판정.

    반환: (prompt, context=None) -> intent dict | None. None 이면 호출측이 사전필터로 폴백.
    """
    def _intent(prompt: str, context: str | None = None):
        text = _chat(client, model, classifier.build_intent_prompt(prompt, context))
        return classifier.parse_intent(text) if text is not None else None
    return _intent


def make_hermes_clarify(client: HermesClient, model: str = "hermes-agent"):
    """§34.4 인테이크 질문 생성 — 부족정보를 선택지+추천 답변이 있는 질문 ≤3개로.

    반환: (prompt, missing_info, context=None) -> {questions, assumptions_if_silent} | None.
    None 이면 호출측이 질문 없이 진행(fail-open — 인테이크가 작업을 막지 않음).
    """
    def _clarify(prompt: str, missing_info: list, context: str | None = None,
                 preferences: str | None = None):
        text = _chat(client, model,
                     classifier.build_clarify_prompt(prompt, missing_info, context,
                                                     preferences=preferences))
        return classifier.parse_clarify(text) if text is not None else None
    return _clarify


def make_hermes_rewrite(client: HermesClient, model: str = "hermes-agent"):
    """§40 지시어 해소 — 이전 작업을 참조하는 요청을 원장에 접지해 자기완결형으로 재작성.

    반환: (prompt, ledger) -> resolved str | None. None/저신뢰면 호출측이 원문 유지
    (fail-open — 원장 블록 주입만으로 진행).
    """
    def _rewrite(prompt: str, ledger: str):
        text = _chat(client, model, classifier.build_rewrite_prompt(prompt, ledger))
        r = classifier.parse_rewrite(text) if text is not None else None
        if not r or r["confidence"] < classifier.REWRITE_MIN_CONFIDENCE:
            return None
        return r["resolved"]
    return _rewrite


def make_hermes_planner(client: HermesClient, model: str = "hermes-agent"):
    """요청을 하위작업으로 분해하는 콜러블(§19). 반환: prompt -> plan dict | None."""
    def _plan(prompt: str):
        text = _chat(client, model, classifier.build_planner_prompt(prompt))
        return classifier.parse_plan(text) if text is not None else None
    return _plan


def make_hermes_planner_v2(client: HermesClient, model: str = "hermes-agent"):
    """§34.3 Plan v2 — 실행·검증 가능한 계획(dod + steps{goal,tool_hint,expected,accept}).

    v1(분류용 coarse 분해)과 달리 디스패치 직전에 능력 인벤토리·인테이크 답변까지 접지해
    생성한다. 반환: (prompt, *, capabilities, intent, intake, draft) -> plan dict | None.
    """
    def _plan2(prompt: str, *, capabilities: str | None = None, intent: dict | None = None,
               intake: str | None = None, draft: dict | None = None,
               replan: str | None = None, context: str | None = None):
        text = _chat(client, model, classifier.build_planner_v2_prompt(
            prompt, capabilities=capabilities, intent=intent, intake=intake, draft=draft,
            replan=replan, context=context))
        return classifier.parse_plan_v2(text) if text is not None else None
    return _plan2


def make_hermes_judge(client: HermesClient, model: str = "hermes-agent"):
    """완료 결과를 수용 기준으로 채점하는 LLM-judge(§21 Tier1+2).

    반환: (request, result) -> verdict dict | None. None 이면 호출측이 fail-open(통과).
    """
    def _judge(request: str, result: str):
        text = _chat(client, model, classifier.build_judge_prompt(request, result))
        return classifier.parse_verdict(text) if text is not None else None
    return _judge


def make_hermes_ranker(client: HermesClient, model: str = "hermes-agent"):
    """큐의 Heavy 작업들을 상대 우선순위로 재정렬하는 콜러블(§22).

    반환: (new_task dict, queue list) -> [{id, priority, reason}] | None(기존 유지).
    """
    def _rank(new_task: dict, queue: list[dict]):
        text = _chat(client, model, classifier.build_rank_prompt(new_task, queue))
        return classifier.parse_rank(text) if text is not None else None
    return _rank


def make_hermes_moa(client: HermesClient, model: str = "hermes-agent"):
    """§29.4 Alphred-side MoA(Mode A) — 결과를 비평·종합해 개선본을 만드는 콜러블.

    반환: (request, result) -> 개선 텍스트 | None(원본 유지, fail-open).
    """
    def _moa(request: str, result: str):
        text = _chat(client, model, classifier.build_moa_prompt(request, result))
        return classifier.parse_moa(text, original=result) if text is not None else None
    return _moa
