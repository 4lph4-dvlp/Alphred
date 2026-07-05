"""Light/Heavy 분류기.

정책(기획 2.3):
  1) 명시 우선   — priority/kind 가 직접 주어지면 그대로 사용(QA-2.2)
  2) 휴리스틱     — 채널/키워드/길이 기반 빠른 판정
  3) LLM 폴백     — 모호 시 1회 질의 (Phase 2; 여기선 훅만 제공)

반환: (kind, priority, reason)
"""
from __future__ import annotations

import re

from .jsonutil import parse_json_object
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

# 관리/설치류 — 보안 스캔·복사·인덱싱·빌드로 오래 걸리므로 동기 즉답이 아니라 백그라운드 Heavy 로.
# (조회/목록은 _QUEUE_STATUS_PAT 또는 짧은 입력으로 Light 유지 — 여기선 변경 동사만 잡음)
_ADMIN_HEAVY_PAT = re.compile(
    r"(스킬|skill|플러그인|plugin|확장|익스텐션|extension|패키지|package|모듈|module|"
    r"라이브러리|library|의존성|dependenc|툴|tool).{0,40}?"
    r"(설치|install|활성화|enable|셋업|set\s*up|setup|구성|configure|업데이트|update|업그레이드|upgrade)"
    r"|(설치|install|활성화|enable|셋업|set\s*up|setup|구성|configure|업그레이드|upgrade).{0,40}?"
    r"(스킬|skill|플러그인|plugin|확장|익스텐션|extension|패키지|package|모듈|module|라이브러리|library|툴|tool)",
    re.IGNORECASE,
)

# 조회/목록 신호 — 있으면 설치류 라우팅을 적용하지 않는다(읽기 요청은 Light).
_ADMIN_QUERY_PAT = re.compile(
    r"목록|리스트|list\b|보여|알려|조회|확인|상태|설치\s*(됐|된|되었|돼|여부)|installed|"
    r"뭐\s*(있|가|를)|어떤.{0,8}(있|가능)|가능한|available|show\b|status",
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

    # 2.5) 스킬/패키지 설치·활성화류 = 느린 관리 작업 → 백그라운드 Heavy(동기 타임아웃 회피).
    #      단, 조회/목록("설치된 스킬 알려줘")은 Light 유지(설치 '동작'만 잡음).
    if _ADMIN_HEAVY_PAT.search(text) and not _ADMIN_QUERY_PAT.search(text):
        return TaskKind.HEAVY.value, 6, "prefilter: skill/package install (slow admin op)", False

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
    d = parse_json_object(text)
    if d is None:
        return None
    kind = str(d.get("kind", "")).lower()
    if kind not in (TaskKind.LIGHT.value, TaskKind.HEAVY.value):
        return None
    try:
        prio = _clamp(int(d.get("priority", 5)))
    except (TypeError, ValueError):
        prio = 9 if kind == TaskKind.LIGHT.value else 4
    return kind, prio, str(d.get("reason", ""))[:120]


# ---- §34.2 IntentCard — LLM-first 통합 의도 판정 ----
# 분류(kind)+우선순위+심화도(depth)+모호성(missing_info)을 **콜 1회**로 통합 판정한다.
# 정규식 사전필터는 fast-path(정책/즉결 케이스)와 LLM 불가 시 폴백으로 강등(§34.2 A1).
INTENT_INSTRUCTION = (
    "You are the intake analyst for a personal AI agent. Read the user's REQUEST and "
    "output ONE JSON object describing the true intent, so the system can route it.\n"
    "Fields:\n"
    '- "goal": one-sentence summary of what the user actually wants (user\'s language).\n'
    '- "domain": one of coding|research|document|data|admin|chat|other.\n'
    '- "deliverable": {"type":"file|answer|action","format":"pdf|docx|pptx|xlsx|md|code|null"}.\n'
    '- "kind": "light" if a quick interactive answer is expected (chat, lookup, short '
    'Q&A, translation, small single edit); "heavy" if it needs minutes of background '
    "work (research report, multi-file coding, crawling, batch processing, generating "
    "files).\n"
    '- "priority": 1-10 (10 = urgent immediate reply, 1 = low background).\n'
    '- "depth": "low" (trivial), "mid" (normal background work), "high" (multi-step, '
    "high-stakes, or artifact-producing work that needs verification).\n"
    '- "category": one of coding|research|analysis|writing|translation|creative|math|agentic|general.\n'
    '- "missing_info": up to 3 things worth asking the user, each '
    '{"what":"...","critical":true|false}. critical=true ONLY if a wrong guess would '
    "make the result useless. Usually an empty list.\n"
    '- "refers_to_previous": true ONLY if the request depends on earlier work/results '
    'in this conversation to be understood (e.g. "do the same for X", "that one too", '
    '"같은 방식으로", "이번에는 ..."); false for self-contained requests.\n'
    '- "confidence": 0-100 that this routing is right.\n'
    "Respond with ONLY the compact JSON object.\n\n"
    "Examples:\n"
    "REQUEST: 미국 주식 시장 요약해서 PDF로 만들어줘\n"
    '{"goal":"미국 주식 시장 요약 PDF 보고서 생성","domain":"document",'
    '"deliverable":{"type":"file","format":"pdf"},"kind":"heavy","priority":5,'
    '"depth":"high","category":"research","missing_info":[{"what":"보고서 분량/대상 독자","critical":false}],'
    '"confidence":88}\n'
    "REQUEST: what's 2+2?\n"
    '{"goal":"simple arithmetic answer","domain":"chat",'
    '"deliverable":{"type":"answer","format":null},"kind":"light","priority":9,'
    '"depth":"low","category":"math","missing_info":[],"confidence":99}\n\n'
    "REQUEST:\n"
)

_INTENT_CATEGORIES = {"coding", "research", "analysis", "writing", "translation", "creative", "math", "agentic", "general"}
_INTENT_DOMAINS = {"coding", "research", "document", "data", "admin", "chat", "other"}
_INTENT_DEPTHS = {"low", "mid", "high"}

# IntentCard 가 켜져 있어도 그대로 신뢰하는 사전필터 판정(정책/즉결 케이스 — LLM 불필요).
# 확신-Heavy(대규모 마커)·모호 케이스는 LLM 의도 판정으로 넘긴다(우선순위/심화도 정밀화).
_FASTPATH_REASONS = {
    "explicit override",
    "prefilter: queue/status query",
    "prefilter: skill/package install (slow admin op)",
}

# 길이 기반 즉결(realtime ≤80 / greeting ≤25)을 IntentCard 대신 신뢰하는 상한.
# 한국어 13~25자에도 산출물 요청("…보고서 만들어줘")이 들어가는 실측 사례가 있어
# (§P2·§34.0 ①) 아주 짧은 대화체(인사/단답)만 즉결로 남긴다.
_SHORT_TRUST = 12
_LENGTH_BASED_REASONS = ("prefilter: realtime chat", "prefilter: greeting/short")


def is_fastpath(reason: str, prompt: str = "") -> bool:
    """사전필터 판정을 (IntentCard 켜져 있어도) 그대로 신뢰할지.

    정책 케이스(명시/상태조회/설치류)는 항상 신뢰. 길이 기반 즉결(짧은 채팅/인사)은
    아주 짧거나(≤12자) Light 패턴(인사·번역·계산 등)일 때만 신뢰하고, 나머지는
    IntentCard 로 넘겨 "짧지만 무거운 요청" 오분류를 잡는다.
    """
    if reason in _LENGTH_BASED_REASONS:
        text = (prompt or "").strip()
        return len(text) <= _SHORT_TRUST or bool(_LIGHT_PAT.search(text))
    return reason in _FASTPATH_REASONS


def build_intent_prompt(prompt: str, context: str | None = None) -> str:
    ctx = f"CONVERSATION CONTEXT (recent turns):\n{context[:1200]}\n\n" if context else ""
    return ctx + INTENT_INSTRUCTION + (prompt or "")[:2000]


def parse_intent(text: str) -> dict | None:
    """LLM 응답 → IntentCard 정규화 dict. 필수 필드(kind) 불량이면 None(폴백)."""
    d = parse_json_object(text)
    if d is None:
        return None
    kind = str(d.get("kind", "")).lower()
    if kind not in (TaskKind.LIGHT.value, TaskKind.HEAVY.value):
        return None
    try:
        prio = _clamp(int(d.get("priority")))
    except (TypeError, ValueError):
        prio = 8 if kind == TaskKind.LIGHT.value else 4
    depth = str(d.get("depth", "")).lower()
    if depth not in _INTENT_DEPTHS:
        depth = "low" if kind == TaskKind.LIGHT.value else "mid"
    domain = str(d.get("domain", "")).lower()
    deliv = d.get("deliverable") if isinstance(d.get("deliverable"), dict) else {}
    missing = []
    for m in (d.get("missing_info") or [])[:5]:
        if isinstance(m, dict) and m.get("what"):
            missing.append({"what": str(m["what"])[:160], "critical": bool(m.get("critical"))})
    try:
        conf = max(0, min(100, int(d.get("confidence"))))
    except (TypeError, ValueError):
        conf = 50

    category = str(d.get("category", "")).lower()
    if category not in _INTENT_CATEGORIES:
        category = "general"

    return {
        "goal": str(d.get("goal") or "")[:200],
        "domain": domain if domain in _INTENT_DOMAINS else "other",
        "deliverable": {"type": str(deliv.get("type") or "")[:20] or None,
                        "format": (str(deliv.get("format"))[:12].lower()
                                   if deliv.get("format") else None)},
        "kind": kind, "priority": prio, "depth": depth,
        "category": category,
        "missing_info": missing, "confidence": conf,
        "refers_to_previous": bool(d.get("refers_to_previous")),  # §40 지시어 해소 트리거
    }


def heuristic_category(prompt: str) -> str:
    """IntentCard 가 없거나 실패했을 때 프롬프트 텍스트에서 휴리스틱하게 카테고리를 추정한다."""
    p = (prompt or "").lower()
    if any(kw in p for kw in ("python", "code", "def ", "class ", "git", "테스트", "test", "함수", "버그", "에러", "script", "스크립트", "코드")):
        return "coding"
    if any(kw in p for kw in ("수학", "계산", "더하기", "빼기", "곱하기", "나누기", "방정식", "통계", "math", "calculator", "statistics", "1+1")):
        return "math"
    if any(kw in p for kw in ("번역", "translate", "영어", "한글", "국어", "일어", "중어", "spanish", "japanese")):
        return "translation"
    if any(kw in p for kw in ("자동화", "매크로", "cron", "스케줄", "install", "설치", "시스템")):
        return "agentic"
    if any(kw in p for kw in ("보고서", "문서", "글쓰기", "이메일", "장문", "편지", "draft", "report", "doc")):
        return "writing"
    if any(kw in p for kw in ("디자인", "그림", "소설", "시", "창작", "카피라이팅", "creative", "design", "paint", "draw")):
        return "creative"
    if any(kw in p for kw in ("조사", "검색", "찾아", "리서치", "news", "뉴스", "search", "구글링", "crawling", "크롤링")):
        return "research"
    if any(kw in p for kw in ("분석", "도표", "차트", "통계", "excel", "csv", "xlsx", "데이터", "data", "summary", "요약")):
        return "analysis"
    return "general"


def intent_to_classification(card: dict, *, source: str = TaskSource.API.value
                             ) -> tuple[str, int, str]:
    """IntentCard → (kind, priority, reason). 실시간 소스의 Light 는 우선순위 하한 9."""
    kind = card["kind"]
    prio = card["priority"]
    if kind == TaskKind.LIGHT.value and source in _REALTIME_SOURCES:
        prio = max(prio, 9)
    reason = f"intent({card.get('confidence', '?')}): {card.get('goal') or kind}"[:160]
    return kind, _clamp(prio), reason


# ---- §40 지시어 해소(reference resolution) — 후속 요청을 자기완결형으로 재작성 ----
# 대화형 검색/RAG 의 표준인 query rewriting: "이번에는 육식맨도 동일하게" 를 원장(이전
# 작업 요약)에 접지해 "육식맨 채널 최신 영상을 분석해 …요약 PDF 생성" 으로 풀어쓴다.
# 재작성본은 계획·실행·검증(§21 DoD)의 기준이 되므로, 새 요구를 지어내면 안 된다.
REWRITE_INSTRUCTION = (
    "The user's new REQUEST refers to earlier work in this session (listed under RECENT "
    "SESSION WORK). Rewrite the REQUEST as ONE self-contained task statement that needs "
    "no conversation context.\n"
    "Rules:\n"
    "- Keep the user's language and exact intent. Do NOT invent new requirements.\n"
    "- Resolve every reference (\"the same\", \"that one\", \"동일하게\") into concrete "
    "details taken from the referenced earlier task: deliverable type/format, scope, "
    "style, structure.\n"
    "- If an earlier artifact is a natural template (e.g. \"same report format\"), "
    "mention its path so the worker can mirror it.\n"
    "- If the REQUEST is actually self-contained, return it unchanged with low "
    "confidence.\n"
    'Respond with ONLY compact JSON: {"resolved":"...","confidence":0-100}\n\n'
)

# 재작성 채택 임계 — 이보다 낮으면 원문 유지(원장 블록 주입만으로 진행).
REWRITE_MIN_CONFIDENCE = 60


def build_rewrite_prompt(prompt: str, ledger: str) -> str:
    return (REWRITE_INSTRUCTION + (ledger or "")[:1600]
            + "\n\nREQUEST:\n" + (prompt or "")[:2000])


def parse_rewrite(text: str) -> dict | None:
    """LLM 응답 → {"resolved", "confidence"}. 형식 불량/빈 재작성이면 None(원문 유지)."""
    d = parse_json_object(text)
    if d is None:
        return None
    resolved = str(d.get("resolved") or "").strip()
    if not resolved:
        return None
    try:
        conf = max(0, min(100, int(d.get("confidence"))))
    except (TypeError, ValueError):
        conf = 50
    return {"resolved": resolved[:4000], "confidence": conf}


# ---- §34.4 인테이크 질문 생성 (Clarify) — 추천 답변 포함 ----
# IntentCard 가 critical 부족정보를 표시한 대화형 Heavy 요청에 한해, 착수 전 질문 ≤3개를
# 만든다. 각 질문은 선택지 2~4개 + 추천 1개(Claude Code AskUserQuestion 패턴) — 사용자는
# Enter 만 쳐도 진행된다. 무응답 타임아웃에 대비한 가정(assumptions_if_silent)도 함께 받는다.
CLARIFY_INSTRUCTION = (
    "You are the intake assistant for a personal AI agent. The user's REQUEST will run as "
    "an autonomous background task, and the listed MISSING INFO could change the outcome. "
    "Write the fewest clarifying questions (1-3) that would most improve the result.\n"
    "Rules:\n"
    "- Only ask what materially changes the deliverable. Never ask what you can safely "
    "assume or discover with tools.\n"
    "- Each question: a short 'header' (≤12 chars), the question text, and 2-4 concrete "
    "answer OPTIONS the user can pick instantly. Mark exactly ONE option "
    '"recommended":true (your best default).\n'
    "- Also give assumptions_if_silent: for each question, the assumption to proceed on "
    "if the user never answers (match the recommended options).\n"
    "- Use the user's language.\n"
    'Respond with ONLY compact JSON: {"questions":[{"header":"...","q":"...",'
    '"options":[{"label":"...","recommended":true},{"label":"..."}],"why":"..."}],'
    '"assumptions_if_silent":["..."]}\n\n'
)


def build_clarify_prompt(prompt: str, missing_info: list[dict],
                         context: str | None = None,
                         preferences: str | None = None) -> str:
    miss = "\n".join(f"- {m.get('what')}" + (" (critical)" if m.get("critical") else "")
                     for m in (missing_info or [])[:5])
    ctx = f"CONVERSATION CONTEXT (recent turns):\n{context[:1200]}\n\n" if context else ""
    prefs = (f"KNOWN USER PREFERENCES (do NOT ask about these again — assume them):\n"
             f"{preferences[:1200]}\n\n" if preferences else "")
    return (prefs + ctx + CLARIFY_INSTRUCTION
            + "REQUEST:\n" + (prompt or "")[:2000]
            + "\n\nMISSING INFO:\n" + (miss or "- (unspecified)"))


def parse_clarify(text: str) -> dict | None:
    """clarify 응답 → {questions:[{header,q,options:[{label,recommended}],why}],
    assumptions_if_silent:[...]} 정규화. 질문이 없으면 None(질문 없이 진행)."""
    d = parse_json_object(text)
    if d is None:
        return None
    out_q = []
    for q in (d.get("questions") or [])[:3]:
        if not isinstance(q, dict) or not q.get("q"):
            continue
        opts = []
        for o in (q.get("options") or [])[:4]:
            if isinstance(o, dict) and o.get("label"):
                opts.append({"label": str(o["label"])[:80],
                             "recommended": bool(o.get("recommended"))})
            elif isinstance(o, str) and o.strip():
                opts.append({"label": o.strip()[:80], "recommended": False})
        if len(opts) < 2:
            continue                      # 선택지 2개 미만이면 질문으로 성립 안 함
        # 추천은 정확히 1개 — 없으면 첫 선택지, 여럿이면 첫 표기만 유지
        rec_seen = False
        for o in opts:
            if o["recommended"] and not rec_seen:
                rec_seen = True
            else:
                o["recommended"] = False
        if not rec_seen:
            opts[0]["recommended"] = True
        out_q.append({"header": str(q.get("header") or "")[:24],
                      "q": str(q["q"])[:200], "options": opts,
                      "why": str(q.get("why") or "")[:160]})
    if not out_q:
        return None
    assumptions = [str(a)[:200] for a in (d.get("assumptions_if_silent") or [])][:5]
    if not assumptions:                   # 가정 미제공 → 추천 선택지로 합성(무응답 진행 보장)
        assumptions = [f"{q['q']} → {next(o['label'] for o in q['options'] if o['recommended'])}"
                       for q in out_q]
    return {"questions": out_q, "assumptions_if_silent": assumptions}


# 대화형 소스 — 질문을 받아 답할 사용자가 있는 곳(cron/api/subservice 는 질문 없이 가정 진행).
_INTERACTIVE_SOURCES = {TaskSource.TUI.value, TaskSource.CHAT.value}


def is_interactive_source(source: str) -> bool:
    return source in _INTERACTIVE_SOURCES


def format_gap_question(card: dict | None, formats: dict | None
                        ) -> tuple[dict, str] | None:
    """§34.5 D4 대화형 — 요청한 산출물 형식의 생성 수단이 없으면 **무LLM 결정적 질문**.

    IntentCard 의 deliverable.format 과 능력 매트릭스를 대조해, 착수 전에 "설치하고
    진행할까요?"를 추천 답변과 함께 묻는다. 무응답 시 가정 = 설치 후 진행(계획 접지가
    설치 스텝을 자동 삽입하므로 가정과 실행이 일치한다).
    반환: (질문 dict, 무응답 가정 문자열) | None(갭 없음/판단 불가).
    """
    fmt = ((card or {}).get("deliverable") or {}).get("format")
    if not fmt or not isinstance(formats, dict):
        return None
    info = formats.get(fmt)
    if not isinstance(info, dict) or info.get("capable"):
        return None
    lib = info.get("install") or "필요 라이브러리"
    q = {"header": "환경 설치",
         "q": f"{fmt} 파일을 만들 도구가 이 환경에 없습니다. 어떻게 진행할까요?",
         "options": [
             {"label": f"{lib} 설치 후 {fmt} 로 진행", "recommended": True},
             {"label": "markdown 등 지금 가능한 형식으로 대체", "recommended": False},
             {"label": f"설치 없이 그대로 시도 ({fmt} 실패 가능)", "recommended": False},
         ],
         "why": "산출물 형식 도구 부재 — 실패를 착수 전에 방지"}
    return q, f"{lib} 설치 후 {fmt} 로 진행한다고 가정"


def needs_clarification(card: dict | None, *, source: str, kind: str) -> bool:
    """인테이크 질문 게이트(§34.4 QA-34.5 과잉질문 방지) — 전부 결정적.

    조건: Heavy(동기 Light 는 즉답이라 무의미) ∧ 대화형 소스 ∧ IntentCard 가
    critical=true 부족정보를 표시. 그 외(비대화형/비critical/카드 없음)는 질문 0.
    """
    if kind != TaskKind.HEAVY.value or source not in _INTERACTIVE_SOURCES:
        return False
    missing = (card or {}).get("missing_info") or []
    return any(m.get("critical") for m in missing)


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
    d = parse_json_object(text)
    if d is None:
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


# ---- §34.3 Plan v2 — 실행·검증 가능한 계획 (M3) ----
# v1(subtasks: 분류용 coarse 분해)과 달리 v2 는 **실행 단위 step** 으로: 각 step 이
# 목표(goal)·도구 힌트(tool_hint)·기대 산출물(expected)·완료 기준(accept)을 가진다.
# expected+accept 가 §21 검증과 계획을 한 몸으로 만든다(수용기준을 계획 시점에 확정).
PLANNER_V2_INSTRUCTION = (
    "You are the execution planner for an autonomous background agent. Break the REQUEST "
    "into concrete, executable steps (1-7). The plan will guide the run and each step's "
    "acceptance will be verified.\n"
    "Rules:\n"
    "- Use ONLY capabilities from the CAPABILITY INVENTORY (skills/CLIs/libraries listed "
    "there). If something needed is missing, make the FIRST step install it via terminal "
    "(e.g. `uv pip install <lib>`).\n"
    "- Each step: \"goal\" (imperative, user's language), \"tool_hint\" (one of: "
    "execute_code | terminal | write_file | web_search | skill:<name> | cli:<agy|claude|"
    "codex|opencode> | none), \"needs\" (ids of prerequisite steps), \"expected\" "
    "(what the step produces: type=file|text|action, format like pdf/docx/md if a file, "
    "path_hint if plausible), \"accept\" (how to check it is done: check=file|content|"
    "exit_code|none with an optional arg — e.g. content arg = a phrase the artifact must "
    "contain).\n"
    "- \"dod\": one sentence — when is the WHOLE request done.\n"
    "- Honor the USER DECISIONS block if present (answers/assumptions from intake).\n"
    'Respond with ONLY compact JSON: {"dod":"...","steps":[{"id":"s1","goal":"...",'
    '"tool_hint":"execute_code","needs":[],"expected":{"type":"file","format":"pdf",'
    '"path_hint":""},"accept":[{"check":"file","arg":""}]}]}\n\n'
)

_V2_TYPES = {"file", "text", "action"}
_V2_CHECKS = {"file", "content", "exit_code", "none"}


def build_planner_v2_prompt(prompt: str, *, capabilities: str | None = None,
                            intent: dict | None = None, intake: str | None = None,
                            draft: dict | None = None,
                            replan: str | None = None,
                            context: str | None = None) -> str:
    parts = [PLANNER_V2_INSTRUCTION]
    if capabilities:
        parts.append("CAPABILITY INVENTORY:\n" + capabilities[:1600] + "\n")
    if context:                               # §40 세션 원장 — "동일 형식" 등 참조 접지
        parts.append(context[:1400] + "\n")
    goal = (intent or {}).get("goal")
    if goal:
        parts.append(f"USER GOAL (pre-analyzed): {goal}\n")
    if intake:
        parts.append("USER DECISIONS (from intake):\n" + intake[:800] + "\n")
    if replan:                                # §34.6 재계획 — 완료 작업/실패 맥락 동봉
        parts.append(replan[:1400] + "\n")
    subs = (draft or {}).get("subtasks") or []
    if subs:                                  # §19 v1 분해가 있으면 초안으로 재활용(콜 낭비 방지)
        titles = "; ".join(str(s.get("title") or "")[:60] for s in subs[:7])
        parts.append(f"DRAFT DECOMPOSITION (refine, don't repeat verbatim): {titles}\n")
    parts.append("REQUEST:\n" + (prompt or "")[:2000])
    return "\n".join(parts)


def parse_plan_v2(text: str) -> dict | None:
    """LLM 응답 → Plan v2 정규화. steps 없으면 None(계획 없이 진행, fail-open).

    정규화: id 자동 부여(s1..sN), 잘못된 type/check 는 안전값으로, needs 는 실존 id 만,
    accept 미제공 시 expected 에서 파생(file→file 체크, 그 외 none).
    """
    d = parse_json_object(text)
    if d is None:
        return None
    raw = d.get("steps")
    if not isinstance(raw, list) or not raw:
        return None
    steps: list[dict] = []
    for i, s in enumerate(raw[:7], 1):
        if not isinstance(s, dict):
            continue
        goal = str(s.get("goal") or "").strip()
        if not goal:
            continue
        exp = s.get("expected") if isinstance(s.get("expected"), dict) else {}
        etype = str(exp.get("type") or "").lower()
        expected = {
            "type": etype if etype in _V2_TYPES else "text",
            "format": (str(exp.get("format"))[:12].lower().lstrip(".")
                       if exp.get("format") else None),
            "path_hint": str(exp.get("path_hint") or "")[:200] or None,
        }
        accept: list[dict] = []
        for a in (s.get("accept") or [])[:4]:
            if not isinstance(a, dict):
                continue
            chk = str(a.get("check") or "").lower()
            if chk in _V2_CHECKS and chk != "none":
                accept.append({"check": chk, "arg": str(a.get("arg") or "")[:200]})
        if not accept and expected["type"] == "file":
            accept = [{"check": "file", "arg": expected["path_hint"] or ""}]
        hint = str(s.get("tool_hint") or "").strip()[:60]
        steps.append({
            "id": str(s.get("id") or f"s{i}")[:12],
            "goal": goal[:240],
            "tool_hint": hint or None,
            "needs": [str(n)[:12] for n in (s.get("needs") or []) if n][:6],
            "expected": expected,
            "accept": accept,
        })
    if not steps:
        return None
    ids = {s["id"] for s in steps}
    for s in steps:                            # 실존하지 않는 선행 id 제거
        s["needs"] = [n for n in s["needs"] if n in ids and n != s["id"]]
    return {"version": 2, "dod": str(d.get("dod") or "")[:300], "steps": steps}


def _plan_steps(plan: dict | None) -> tuple[list[dict], bool]:
    """(스텝 목록, v2 여부) — v1 subtasks/v2 steps 를 공통으로 다루는 헬퍼."""
    if not plan:
        return [], False
    if plan.get("version") == 2 or isinstance(plan.get("steps"), list):
        return plan.get("steps") or [], True
    return plan.get("subtasks") or [], False


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
    d = parse_json_object(text)
    if d is None:
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
    """작업 심화도 결정(§21) → "low" | "mid" | "high". v1(subtasks)/v2(steps) 겸용.

    low  = Light(직답).  mid = 일반 Heavy.  high = 복합·고위험·산출물 다단계.
    심화도는 검증/재시도 강도를 게이팅한다(토큰 낭비 방지).
    """
    if kind != TaskKind.HEAVY.value:
        return "low"
    subs, is_v2 = _plan_steps(plan)
    if not subs:
        return "mid"          # 무겁지만 분해 없음(플래너 off/실패) → 중간
    n = len(subs)
    if is_v2:
        mutating = any((s.get("expected") or {}).get("type") in ("file", "action")
                       for s in subs)
        tool_steps = sum(1 for s in subs if s.get("tool_hint") not in (None, "", "none"))
        if n >= 3 or (mutating and tool_steps >= 2):
            return "high"
        return "mid"
    heavy_effort = any((s.get("effort") or "") == "heavy" for s in subs)
    tool_steps = sum(1 for s in subs if s.get("tools"))
    mutating = any((s.get("kind") or "") in _DEPTH_MUTATING for s in subs)
    if n >= 3 or heavy_effort or (mutating and tool_steps >= 2):
        return "high"
    return "mid"


def estimate_cost(plan: dict | None, depth: str, *, judge_enabled: bool = False) -> dict:
    """실행 전 러프 비용 견적(§21 V3) — 결정적, LLM 호출 없음. v1/v2 겸용.

    에이전트 루프는 단계·도구 왕복마다 LLM 을 호출하므로 보수적 하한만 제시한다
    (정밀 토큰 추정은 시스템 프롬프트 크기 등으로 신뢰도 낮아 의도적으로 생략).
    """
    subs, is_v2 = _plan_steps(plan)
    steps = len(subs) or 1
    if is_v2:
        tool_steps = sum(1 for s in subs if s.get("tool_hint") not in (None, "", "none"))
    else:
        tool_steps = sum(1 for s in subs if s.get("tools"))
    est_calls = max(1, steps + tool_steps)
    if depth == "high":
        est_calls += 2                         # 조사/정리 여유
        if judge_enabled:
            est_calls += 1                     # 수용 judge
    band = {"low": "낮음", "mid": "보통", "high": "높음"}.get(depth, "보통")
    return {"steps": steps, "tool_steps": tool_steps,
            "est_llm_calls": est_calls, "band": band}


# ---- Alphred-side MoA (§29.4) — 초안 비평·종합으로 결과 품질 상향 ----
MOA_INSTRUCTION = (
    "You are a senior reviewer-synthesizer improving a background agent's DRAFT result "
    "before it is delivered to the client. Given the ORIGINAL REQUEST and the DRAFT, do this "
    "internally: (1) critique the draft for gaps, shallow spots, missing evidence, weak "
    "structure, and unmet parts of the request; (2) produce a SINGLE improved final "
    "deliverable that fixes those issues — deeper, more specific, better organized.\n"
    "RULES: Do NOT fabricate facts, numbers, citations, or claim files/artifacts that the "
    "draft did not actually produce — preserve real file paths and factual claims as-is. Keep "
    "the user's language. If the draft is already strong, return it essentially unchanged.\n"
    "Output ONLY the final improved deliverable text — no preamble, no critique notes, no "
    "meta commentary.\n\n"
)


def build_moa_prompt(request: str, result: str) -> str:
    return (MOA_INSTRUCTION
            + "ORIGINAL REQUEST:\n" + (request or "")[:2000]
            + "\n\nDRAFT:\n" + (result or "")[:12000])


def parse_moa(text: str, *, original: str | None = None) -> str | None:
    """MoA 응답에서 개선 산출물 텍스트를 추출. 빈약하면 None(원본 유지, fail-open).

    모델이 메타설명을 덧붙였을 수 있어 흔한 머리말은 제거한다. 원본보다 너무 짧아지면
    (정보 손실 의심) 채택하지 않는다.
    """
    if not text:
        return None
    out = text.strip()
    # 흔한 머리말 제거(있을 때만)
    out = re.sub(r"^(?:```[a-zA-Z]*\n)?(?:final\s+deliverable\s*[:\-]?\s*)?", "", out,
                 flags=re.IGNORECASE).strip()
    if out.endswith("```"):
        out = out[:-3].rstrip()
    if len(out) < 40:
        return None
    if original and len(out) < 0.5 * len(original.strip()):
        return None  # 원본 대비 과도 축약 → 정보 손실 의심, 채택 안 함
    return out


# ---- 큐 상대 우선순위 재정렬 (Queue Ranker) ----
RANK_INSTRUCTION = (
    "You are the scheduler for a single-slot background task queue. New heavy work just "
    "arrived. Re-rank the heavy tasks by assigning each a priority 1..10 (10 = run first, "
    "1 = run last). Decide RELATIVELY by comparing ALL tasks, considering:\n"
    "  - urgency / user intent (explicit 'do X first' must win),\n"
    "  - DEPENDENCIES & efficient ordering: if task B must finish before A is useful, give B "
    "a higher priority than A (even if A was requested earlier),\n"
    "  - effort and number of sub-steps.\n"
    "Give DISTINCT priorities to tasks that should run in a clear order (ties mean 'no "
    "preference'). The currently running task may be lowered so a more urgent/prerequisite "
    "task preempts it. Use the user's language for reasons.\n"
    'Respond with ONLY compact JSON: {"rankings":[{"id":"<id|NEW>","priority":<1-10>,'
    '"reason":"<short>"}],"summary":"<short>"}\n\n'
)


def build_rank_prompt(new_task: dict, queue: list[dict]) -> str:
    """신규 Heavy 작업 + 현재 큐 스냅샷 → 상대 재정렬 프롬프트.

    new_task: {prompt, depth, kind, plan?} (id 는 "NEW" 로 지칭).
    queue:    [{id, prompt, priority, state, kind}, ...] (신규 작업 포함, 활성 Heavy 만).
    """
    lines = [RANK_INSTRUCTION]
    nt = (new_task.get("prompt") or "").replace("\n", " ")[:300]
    lines.append(f'NEW task (id="NEW"): {nt}')
    plan = new_task.get("plan") or {}
    subs = plan.get("subtasks") or []
    if subs:
        titles = ", ".join(str(s.get("title") or "")[:40] for s in subs[:7])
        lines.append(f"  sub-tasks: {titles}")
    if new_task.get("depth"):
        lines.append(f"  depth: {new_task['depth']}")
    lines.append("\nCurrent queue (heavy tasks):")
    for t in queue:
        p = (t.get("prompt") or "").replace("\n", " ")[:140]
        lines.append(f'  id={t.get("id")} prio={t.get("priority")} state={t.get("state")}: {p}')
    lines.append("\nReturn priorities for EVERY task above (use its id; the new one is \"NEW\").")
    return "\n".join(lines)


def parse_rank(text: str) -> list[dict] | None:
    """랭커 응답 → [{id, priority, reason}] 정규화. 실패/빈 결과 시 None."""
    d = parse_json_object(text)
    if d is None:
        return None
    raw = d.get("rankings")
    if not isinstance(raw, list) or not raw:
        return None
    out: list[dict] = []
    for r in raw:
        if not isinstance(r, dict):
            continue
        rid = str(r.get("id") or "").strip()
        if not rid:
            continue
        try:
            prio = _clamp(int(r.get("priority")))
        except (TypeError, ValueError):
            continue
        out.append({"id": rid, "priority": prio, "reason": str(r.get("reason") or "")[:120]})
    return out or None
