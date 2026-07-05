"""§40 세션 컨텍스트 연속성 — 작업 원장(session ledger)·산출물 추출·지시어 감지.

한 세션에서 여러 작업이 큐를 오가며 실행될 때, 이전 작업의 요청·결과·산출물을 압축한
원장 블록을 분류(IntentCard)·계획(Plan v2)·실행 입력에 주입해 "이번에는 X도 동일하게" 류의
후속 요청이 맥락을 잃지 않게 한다. 이 모듈은 전부 무LLM·결정적이고 실패는 fail-open —
지시어 해소 재작성(LLM 1콜)은 llm_calls.make_hermes_rewrite 가 담당한다.
"""
from __future__ import annotations

import json
import os
import re

from .verify import _claimed_file_paths

LEDGER_LIMIT = 5          # 원장에 담는 최근 종결 작업 수
LEDGER_MAX_CHARS = 1200   # 원장 블록 총 상한(실행 입력 토큰 보호)
_REQ_CUT = 120            # 항목당 요청 요약 길이
_RES_CUT = 300            # 항목당 결과 요약 길이
ARTIFACTS_LIMIT = 5       # 작업당 저장하는 산출물 경로 수

# 이전 작업을 가리키는 지시어 휴리스틱 — IntentCard(refers_to_previous) 누락을 보완하는
# 보조 트리거. 정밀도 우선: "같은" 단독 같은 범용어는 제외하고 명시적 조합만 잡는다.
_REFER_PAT = re.compile(
    r"동일하게|동일한\s*(분석|작업|방식|형식|보고서)|같은\s*(방식|형식|포맷|분석|작업)으?로?"
    r"|똑같이|마찬가지로|마저\s*(해|진행)|그것도|그거\s*(도|처럼)|아까\s*(처럼|같이|한)"
    r"|방금\s*(처럼|같이|한)|이전\s*(작업|결과)|이번에는|이번엔"
    r"|(do|the)\s+same|as\s+before|likewise|same\s+(way|format|analysis)",
    re.IGNORECASE)


def looks_referential(prompt: str | None) -> bool:
    """요청이 이전 작업을 참조하는 것으로 보이는가(결정적 휴리스틱)."""
    return bool(_REFER_PAT.search(prompt or ""))


def extract_artifacts(result: str | None) -> list[str] | None:
    """결과 텍스트가 언급한 경로 중 **실제 존재하는 파일**만 추출(§40 산출물 레지스트리).

    verify 의 경로 추출기를 재활용하되 존재 검사로 좁혀 오탐(예시 경로·실패 주장)을 걸러낸다.
    하네스가 이미 "산출물 절대경로 보고"를 지시하므로 별도 형식 요구가 없다(무 LLM 의존).
    """
    if not result:
        return None
    seen: set[str] = set()
    out: list[str] = []
    for p in _claimed_file_paths(result):
        if p in seen:
            continue
        seen.add(p)
        try:
            if os.path.isfile(p):
                out.append(p)
        except OSError:
            continue
        if len(out) >= ARTIFACTS_LIMIT:
            break
    return out or None


def _one_line(text: str | None, cut: int) -> str:
    return " ".join((text or "").split())[:cut]


def ledger_block(tasks) -> str | None:
    """최근 종결 Task 목록(최신 우선) → 실행/분류 입력용 원장 블록. 비면 None.

    결과 요약은 결정적 절단(§34.9 약모델 전제 — 요약 LLM 콜 없음). 산출물 경로를
    우선 노출해 "같은 형식으로" 류 후속 요청이 이전 산출물을 참조할 수 있게 한다.
    """
    items: list[str] = []
    for i, t in enumerate(tasks or [], 1):
        state = "done" if getattr(t, "state", "") == "Completed" else "needs-review"
        lines = [f"{i}. REQUEST: {_one_line(t.prompt, _REQ_CUT)} ({state})"]
        res = _one_line(getattr(t, "result", None), _RES_CUT)
        if res:
            lines.append(f"   RESULT: {res}")
        arts = getattr(t, "artifacts", None)
        if arts:
            try:
                paths = json.loads(arts)
                if paths:
                    lines.append("   ARTIFACTS: " + "; ".join(str(p) for p in paths[:3]))
            except Exception:
                pass
        items.append("\n".join(lines))
    if not items:
        return None
    head = ("[RECENT SESSION WORK — earlier tasks in this conversation, most recent "
            "first. Use to resolve references like \"the same as before\"; reuse the "
            "listed artifact formats/paths when the request asks for consistency]\n")
    body = ""
    for it in items:
        if len(head) + len(body) + len(it) + 1 > LEDGER_MAX_CHARS:
            break
        body += (it + "\n")
    return (head + body).rstrip() if body else None
