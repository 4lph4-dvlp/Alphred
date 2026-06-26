"""Light/Heavy 분류기.

정책(기획 2.3):
  1) 명시 우선   — priority/kind 가 직접 주어지면 그대로 사용(QA-2.2)
  2) 휴리스틱     — 채널/키워드/길이 기반 빠른 판정
  3) LLM 폴백     — 모호 시 1회 질의 (Phase 2; 여기선 훅만 제공)

반환: (kind, priority, reason)
"""
from __future__ import annotations

import re

from .models import TaskKind, TaskSource

# Heavy 신호: 시간이 걸리고 즉답이 불필요한 작업
_HEAVY_PAT = re.compile(
    r"리팩토|refactor|크롤|crawl|scrape|분석|analyz|리포트|report|마이그레이|migrat|"
    r"전체\s|모든\s|대규모|batch|일괄|백그라운드|background|정리해|요약본\s*작성|"
    r"빌드|build|테스트\s*전체|train|학습|수집|aggregate|벤치|benchmark",
    re.IGNORECASE,
)
# Light 신호: 즉각적인 대화/조회
_LIGHT_PAT = re.compile(
    r"^(안녕|hi|hello|뭐야|what|who|when|where|얼마|몇|있어\?|알려줘|간단|빠르게|지금|즉시|"
    r"검색해줘|번역|계산)",
    re.IGNORECASE,
)

# 큐/작업 상태 조회는 항상 Light(즉답) — Heavy 키워드보다 우선. 백그라운드로 빠지면 안 됨.
# (예: "큐에 등록된 작업들과 상태를 보여줘", "아까 작업 어떻게 됐어?", "보고서 결과 알려줘")
_QUEUE_STATUS_PAT = re.compile(
    r"(큐|queue|작업|task|보고서|report).{0,12}"
    r"(목록|리스트|상태|현황|결과|보고|보여\s*줘|알려\s*줘|확인|조회|어때|list|status)"
    r"|진행\s*상황|어떻게\s*(됐|돼|되었)|완료\s*(됐|된|되었|여부|했)",
    re.IGNORECASE,
)

# 확신-Heavy: 계획 없이도 명백히 무거운 대규모 마커(단일 약한 키워드와 구분)
_CONFIDENT_HEAVY_PAT = re.compile(
    r"전체\s*(코드|코드베이스|프로젝트|시스템|파일|디렉|레포|repo)|모든\s*(파일|코드|테스트|문서)|"
    r"코드베이스|codebase|마이그레이션|migrat|대규모|일괄|batch|백그라운드|background|"
    r"크롤(링)?|crawl|스크래?핑|scrap|벤치마크|benchmark|학습\s*(시켜|해|돌)|train",
    re.IGNORECASE,
)

# 즉답이 기대되는 소스
_REALTIME_SOURCES = {TaskSource.CHAT.value}


def classify(
    prompt: str,
    *,
    source: str = TaskSource.API.value,
    explicit_priority: int | None = None,
    explicit_kind: str | None = None,
) -> tuple[str, int, str]:
    # 1) 명시 우선
    if explicit_kind or explicit_priority is not None:
        kind = explicit_kind or (
            TaskKind.LIGHT.value if (explicit_priority or 0) >= 7 else TaskKind.HEAVY.value
        )
        prio = explicit_priority if explicit_priority is not None else (
            9 if kind == TaskKind.LIGHT.value else 4
        )
        return kind, _clamp(prio), "explicit override"

    text = (prompt or "").strip()

    # 2) 휴리스틱
    # 2a) 큐/작업 상태 조회는 Heavy 키워드보다 우선 — 즉답 처리(큐에 안 넣음).
    if _QUEUE_STATUS_PAT.search(text):
        return TaskKind.LIGHT.value, 9, "heuristic: queue/status query"
    if _HEAVY_PAT.search(text):
        return TaskKind.HEAVY.value, 3, "heuristic: heavy keyword"
    if _LIGHT_PAT.search(text) or len(text) <= 60 or source in _REALTIME_SOURCES:
        prio = 9 if source in _REALTIME_SOURCES else 8
        return TaskKind.LIGHT.value, prio, "heuristic: short/realtime/light keyword"

    # 3) 모호 → 기본 Heavy(보수적). Phase 2 에서 LLM 폴백으로 대체.
    return TaskKind.HEAVY.value, 5, "default: ambiguous -> heavy"


def prefilter(
    prompt: str,
    *,
    source: str = TaskSource.API.value,
    explicit_priority: int | None = None,
    explicit_kind: str | None = None,
) -> tuple[str, int, str, bool]:
    """3-tier 사전필터(LLM 없음). 반환: (kind, priority, reason, ambiguous).

    ambiguous=True 면 호출측이 LLM 플래너로 판정하도록 위임한다(미가동 시 reason 의
    기본값=보수적 Heavy 로 폴백). 확신 케이스는 0비용 즉결.
    """
    # 1) 명시 우선 — 기존 규칙 재사용
    if explicit_kind or explicit_priority is not None:
        k, p, r = classify(prompt, source=source,
                           explicit_priority=explicit_priority, explicit_kind=explicit_kind)
        return k, p, r, False

    text = (prompt or "").strip()

    # 2) 상태/큐 조회는 항상 즉답(heavy 키워드 포함해도 Light) — 백그라운드로 빠지면 안 됨
    if _QUEUE_STATUS_PAT.search(text):
        return TaskKind.LIGHT.value, 9, "prefilter: queue/status query", False

    # 3) 확신-Heavy: 명시적 대규모 마커 또는 무거운 키워드 2개 이상 (단축-Light 보다 우선)
    if _CONFIDENT_HEAVY_PAT.search(text) or len(_HEAVY_PAT.findall(text)) >= 2:
        return TaskKind.HEAVY.value, 3, "prefilter: explicit large-scope", False

    # 4) 확신-Light: 실시간 짧은 채팅 / 인사·아주 짧음
    if source in _REALTIME_SOURCES and len(text) <= 80:
        return TaskKind.LIGHT.value, 9, "prefilter: realtime chat", False
    if _LIGHT_PAT.search(text) or len(text) <= 25:
        return TaskKind.LIGHT.value, 8, "prefilter: greeting/short", False

    # 5) 모호 → 플래너 위임(미가동 시 보수적 Heavy 폴백)
    return TaskKind.HEAVY.value, 5, "prefilter: ambiguous (needs planning)", True


def _clamp(p: int) -> int:
    return max(1, min(10, int(p)))


def is_ambiguous(reason: str) -> bool:
    """휴리스틱이 확신 없이 기본값으로 떨어졌는지(=LLM 폴백 대상)."""
    return reason.startswith("default")


# ---- LLM 폴백 (모호한 입력에 한해 1회 질의, QA-2.3) ----
LLM_INSTRUCTION = (
    "You are a task router. Classify the user's request for a background agent.\n"
    "Decide if it is LIGHT (needs an immediate, quick answer — chat, lookup, short Q&A) "
    "or HEAVY (time-consuming background work — analysis, refactor, crawl, report).\n"
    "Also assign a priority 1..10 (10=urgent immediate reply, 1=low background).\n"
    'Respond with ONLY a compact JSON object: {"kind":"light|heavy","priority":<1-10>,"reason":"<short>"}\n\n'
    "Request:\n"
)


def build_llm_prompt(prompt: str) -> str:
    return LLM_INSTRUCTION + (prompt or "")[:2000]


def parse_classification(text: str) -> tuple[str, int, str] | None:
    """LLM 응답에서 {kind, priority, reason} 를 추출. 실패 시 None."""
    import json
    import re
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except Exception:
        return None
    kind = str(d.get("kind", "")).lower()
    if kind not in (TaskKind.LIGHT.value, TaskKind.HEAVY.value):
        return None
    try:
        prio = _clamp(int(d.get("priority", 5)))
    except (TypeError, ValueError):
        prio = 9 if kind == TaskKind.LIGHT.value else 4
    return kind, prio, str(d.get("reason", ""))[:120]


# ---- 계획 기반 분류 (모호한 입력에 한해 1회 분해, §19) ----
PLANNER_INSTRUCTION = (
    "You are a task planner for a background agent. Break the user's request into the "
    "concrete sub-tasks needed to fulfil it (coarse, 1-7 steps). For each sub-task give a "
    "short title, a kind (chat|search|io|compute|edit) and effort (trivial|moderate|heavy), "
    "plus any tools it needs. The plan will decide whether the whole request is a quick "
    "answer or multi-step background work. Reply in the user's language for titles.\n"
    'Respond with ONLY compact JSON: {"subtasks":[{"title":"...","kind":"chat",'
    '"effort":"trivial","tools":[]}],"urgent":false}\n\nRequest:\n'
)

_PLAN_KINDS = {"chat", "search", "io", "compute", "edit"}
_PLAN_EFFORTS = {"trivial", "moderate", "heavy"}
_HEAVY_KINDS = {"compute", "edit"}        # 장연산/파일변경 = 무거움 신호


def build_planner_prompt(prompt: str) -> str:
    return PLANNER_INSTRUCTION + (prompt or "")[:2000]


def parse_plan(text: str) -> dict | None:
    """LLM 응답에서 {subtasks:[{title,kind,effort,tools}], urgent} 를 추출·정규화. 실패 시 None."""
    import json
    import re
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except Exception:
        return None
    raw = d.get("subtasks")
    if not isinstance(raw, list) or not raw:
        return None
    subs = []
    for s in raw[:7]:
        if not isinstance(s, dict):
            continue
        kind = str(s.get("kind", "")).lower()
        effort = str(s.get("effort", "")).lower()
        tools = s.get("tools") if isinstance(s.get("tools"), list) else []
        subs.append({
            "title": str(s.get("title") or "").strip()[:120],
            "kind": kind if kind in _PLAN_KINDS else "chat",
            "effort": effort if effort in _PLAN_EFFORTS else "moderate",
            "tools": [str(t)[:40] for t in tools][:6],
        })
    if not subs:
        return None
    return {"subtasks": subs, "urgent": bool(d.get("urgent"))}


def plan_to_weight(plan: dict, *, source: str = TaskSource.API.value) -> tuple[str, int, str]:
    """분해된 계획 → Heavy/Light + priority (결정적 규칙, 튜닝 가능)."""
    subs = plan.get("subtasks") or []
    n = len(subs)
    heavy_effort = any(s.get("effort") == "heavy" for s in subs)
    heavy_kind = any(s.get("kind") in _HEAVY_KINDS for s in subs)
    tool_steps = sum(1 for s in subs if s.get("tools"))
    is_heavy = n >= 3 or heavy_effort or heavy_kind or tool_steps >= 2
    if is_heavy:
        bits = [f"{n} steps"]
        if heavy_effort:
            bits.append("heavy effort")
        if heavy_kind:
            bits.append("mutating/compute")
        if tool_steps >= 2:
            bits.append(f"{tool_steps} tool steps")
        return TaskKind.HEAVY.value, 4, "plan: " + ", ".join(bits)
    prio = 9 if plan.get("urgent") else 8
    return TaskKind.LIGHT.value, prio, f"plan: {n} light step(s)"


# 산출물/되돌리기 어려움 신호(io=파일입출력, compute=장연산, edit=파일변경)
_DEPTH_MUTATING = {"io", "compute", "edit"}


# ---- 수용 검증(LLM-judge, §21 Tier1+Tier2) ----
JUDGE_INSTRUCTION = (
    "You are a strict acceptance reviewer for a background agent's completed work. "
    "Given the ORIGINAL REQUEST and the agent's RESULT, first infer the concrete "
    "acceptance criteria the request implies (a short Definition-of-Done), then judge "
    "whether the RESULT actually satisfies each. Be skeptical of claims not backed by "
    "evidence in the result. Do not reward vague or partial answers. "
    "Reply in the user's language for notes.\n"
    'Respond with ONLY compact JSON: {"verdict":"pass|fail","score":<0-100>,'
    '"criteria":[{"name":"...","met":true,"note":"..."}],'
    '"unmet":["..."],"summary":"..."}\n\n'
)


def build_judge_prompt(request: str, result: str) -> str:
    return (JUDGE_INSTRUCTION
            + "ORIGINAL REQUEST:\n" + (request or "")[:2000]
            + "\n\nRESULT:\n" + (result or "")[:6000])


def parse_verdict(text: str) -> dict | None:
    """judge 응답에서 {passed, score, criteria, unmet, summary} 추출·정규화. 실패 시 None."""
    import json
    import re
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except Exception:
        return None
    verdict = str(d.get("verdict", "")).strip().lower()
    if verdict not in ("pass", "fail"):
        # score 로 보정(없으면 판정 불가 → None)
        sc = d.get("score")
        if not isinstance(sc, (int, float)):
            return None
        verdict = "pass" if sc >= 70 else "fail"
    crit = []
    for c in (d.get("criteria") or [])[:12]:
        if isinstance(c, dict):
            crit.append({"name": str(c.get("name") or "")[:120],
                         "met": bool(c.get("met")),
                         "note": str(c.get("note") or "")[:200]})
    unmet = [str(u)[:200] for u in (d.get("unmet") or [])][:12]
    try:
        score = int(d.get("score")) if d.get("score") is not None else (100 if verdict == "pass" else 0)
    except (TypeError, ValueError):
        score = 100 if verdict == "pass" else 0
    return {"passed": verdict == "pass", "score": score, "criteria": crit,
            "unmet": unmet, "summary": str(d.get("summary") or "")[:300]}


def plan_to_depth(plan: dict | None, kind: str) -> str:
    """작업 심화도 결정(§21) → "low" | "mid" | "high".

    low  = Light(직답).  mid = 일반 Heavy.  high = 복합·고위험·산출물 다단계.
    심화도는 검증/재시도 강도를 게이팅한다(토큰 낭비 방지).
    """
    if kind != TaskKind.HEAVY.value:
        return "low"
    subs = (plan or {}).get("subtasks") or []
    if not subs:
        return "mid"          # 무겁지만 분해 없음(플래너 off/실패) → 중간
    n = len(subs)
    heavy_effort = any((s.get("effort") or "") == "heavy" for s in subs)
    tool_steps = sum(1 for s in subs if s.get("tools"))
    mutating = any((s.get("kind") or "") in _DEPTH_MUTATING for s in subs)
    if n >= 3 or heavy_effort or (mutating and tool_steps >= 2):
        return "high"
    return "mid"


def estimate_cost(plan: dict | None, depth: str, *, judge_enabled: bool = False) -> dict:
    """실행 전 러프 비용 견적(§21 V3) — 결정적, LLM 호출 없음.

    에이전트 루프는 단계·도구 왕복마다 LLM 을 호출하므로 보수적 하한만 제시한다
    (정밀 토큰 추정은 시스템 프롬프트 크기 등으로 신뢰도 낮아 의도적으로 생략).
    """
    subs = (plan or {}).get("subtasks") or []
    steps = len(subs) or 1
    tool_steps = sum(1 for s in subs if s.get("tools"))
    est_calls = max(1, steps + tool_steps)
    if depth == "high":
        est_calls += 2                         # 조사/정리 여유
        if judge_enabled:
            est_calls += 1                     # 수용 judge
    band = {"low": "낮음", "mid": "보통", "high": "높음"}.get(depth, "보통")
    return {"steps": steps, "tool_steps": tool_steps,
            "est_llm_calls": est_calls, "band": band}
