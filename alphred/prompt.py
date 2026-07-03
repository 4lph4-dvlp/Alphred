"""시스템 프롬프트(하네스) 로딩 — 백그라운드 Heavy 작업의 실행 품질을 끌어올린다.

우선순위: 사용자 편집본(`ALPHRED_HOME/system_prompt.md`) → 패키지 기본값
(`alphred/assets/system_prompt.md`) → 최소 폴백 문자열.

사용자가 자기 입맛대로 품질 기준을 조정할 수 있도록 외부 파일로 분리한다. 편집본이 있으면
업데이트와 무관하게 그것을 사용하므로, Hermes/Alphred 업데이트가 사용자 튜닝을 덮어쓰지 않는다.
"""
from __future__ import annotations

from pathlib import Path

PROMPT_FILENAME = "system_prompt.md"
_ASSET = Path(__file__).resolve().parent / "assets" / PROMPT_FILENAME

# §29.2 Light(즉답) 하네스 — 동기 응답 앞에 system 메시지로 주입(콜드스타트 해소).
LIGHT_PROMPT_FILENAME = "light_prompt.md"
_LIGHT_ASSET = Path(__file__).resolve().parent / "assets" / LIGHT_PROMPT_FILENAME

# Minimal fallback when neither the user override nor the packaged asset is readable.
_FALLBACK = (
    "This is an autonomous background task. No interactive user is present. Do not ask "
    "back; make reasonable assumptions and finish end-to-end. Do not merely claim to have "
    "produced artifacts — actually create them with tools, verify they exist, and report "
    "the full path(s). Produce deep, specific, evidence-based results.\n\n## REQUEST\n"
)
_LIGHT_FALLBACK = (
    "You are Alphred, a sharp, capable assistant. Answer directly, accurately, and "
    "concisely. Don't invent facts; if unsure, say so. Don't over-refuse or pad. Match "
    "the user's language."
)


def default_prompt_text() -> str:
    """패키지 기본 하네스 텍스트(자산 없으면 폴백)."""
    try:
        return _ASSET.read_text(encoding="utf-8")
    except Exception:
        return _FALLBACK


def user_prompt_path(alphred_home) -> Path:
    return Path(alphred_home) / PROMPT_FILENAME


def load_harness(alphred_home=None) -> str:
    """실행에 prepend 할 하네스 문자열을 해석한다.

    alphred_home 의 사용자 편집본이 있으면 우선, 없으면 패키지 기본값.
    """
    if alphred_home is not None:
        p = user_prompt_path(alphred_home)
        try:
            if p.exists():
                txt = p.read_text(encoding="utf-8").strip()
                if txt:
                    return txt + "\n"
        except Exception:
            pass
    return default_prompt_text()


def init_user_prompt(alphred_home, *, overwrite: bool = False) -> tuple[Path, bool]:
    """패키지 기본 하네스를 사용자 편집본 위치로 복사한다(편집 시작점 제공).

    반환: (경로, 새로_썼는지). overwrite=False 면 이미 있을 때 건드리지 않는다.
    """
    p = user_prompt_path(alphred_home)
    if p.exists() and not overwrite:
        return p, False
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(default_prompt_text(), encoding="utf-8")
    return p, True


# ---- §29.2 Light(즉답) 하네스 — system_prompt 와 동일 구조의 경량판 ----
def default_light_prompt_text() -> str:
    try:
        return _LIGHT_ASSET.read_text(encoding="utf-8")
    except Exception:
        return _LIGHT_FALLBACK


def user_light_prompt_path(alphred_home) -> Path:
    return Path(alphred_home) / LIGHT_PROMPT_FILENAME


def _strip_html_comment(text: str) -> str:
    """편집 안내용 HTML 주석(<!-- ... -->)을 앞에서 제거해 토큰을 아낀다(주입 전용)."""
    import re
    return re.sub(r"^\s*<!--.*?-->\s*", "", text, count=1, flags=re.DOTALL).strip()


def load_light_harness(alphred_home=None) -> str:
    """Light 응답에 system 메시지로 주입할 하네스(사용자 편집본 우선). 주석은 제거."""
    raw = None
    if alphred_home is not None:
        p = user_light_prompt_path(alphred_home)
        try:
            if p.exists():
                t = p.read_text(encoding="utf-8").strip()
                if t:
                    raw = t
        except Exception:
            raw = None
    if raw is None:
        raw = default_light_prompt_text()
    out = _strip_html_comment(raw)
    return out or _LIGHT_FALLBACK


def init_user_light_prompt(alphred_home, *, overwrite: bool = False) -> tuple[Path, bool]:
    p = user_light_prompt_path(alphred_home)
    if p.exists() and not overwrite:
        return p, False
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(default_light_prompt_text(), encoding="utf-8")
    return p, True


# ---- §34.5 D2: 동적 능력 인벤토리 마커 ----
# 하네스의 {{CAPABILITIES}} 자리에 디스패치 시점의 실물 인벤토리(CapabilityRegistry)를
# 치환한다. 레지스트리 미가용/off 면 정적 폴백(기존 하드코딩 목록 상당)으로 강등,
# 사용자 편집본에 마커가 없으면 아무것도 바꾸지 않는다(하위호환).
CAPS_MARKER = "{{CAPABILITIES}}"

_CAPS_FALLBACK = (
    "- **Skills (load and follow their workflow if available):**\n"
    "  - `nano-pdf` — *edit* existing PDFs via natural language (typos/titles/text).\n"
    "  - `powerpoint` — create / read / edit `.pptx` decks (use for ANY pptx work).\n"
    "  - `ocr-and-documents` — parse/extract from PDFs, scans, and office docs\n"
    "    (e.g., `python -m markitdown file.ext` for text extraction).\n"
    "  - `excel-author` — author `.xlsx` workbooks/models (if installed).\n"
    "  - `antigravity-cli` (`agy`) / `claude-code` / `codex` / `opencode` —\n"
    "    coding-agent CLIs; **prefer one of these for substantial coding/dev tasks**\n"
    "    (see \"CODE & COMMAND EXECUTION\" below). Verify availability first\n"
    "    (e.g. `agy --version` via `terminal`); fall back to `execute_code` if none.\n"
    "  (Static list — actual availability was not verified; check before relying on it.)"
)


def render_capabilities(harness: str, capabilities: str | None) -> str:
    """하네스의 {{CAPABILITIES}} 마커를 실물 인벤토리(또는 정적 폴백)로 치환."""
    if CAPS_MARKER not in harness:
        return harness
    return harness.replace(CAPS_MARKER, (capabilities or "").strip() or _CAPS_FALLBACK)


# ---- §34.4 C3: 선호 기억 — 인테이크 답변을 축적해 같은 질문을 반복하지 않는다 ----
# system_prompt.md 외부화와 동일 철학: 수동 편집 가능한 평문 파일(ALPHRED_HOME/preferences.md).
# 주의: 작업 특정적 답변(파일 경로 등)도 섞일 수 있어 주입 문구가 "관련 있을 때만 적용"을
# 명시하고, 사용자가 파일을 직접 정리할 수 있다.
PREFS_FILENAME = "preferences.md"
_PREFS_HEADER = "# Alphred 사용자 선호 (인테이크 답변 자동 축적 — 자유롭게 편집/정리하세요)\n\n"


def append_preference(path, q: str, answer: str) -> None:
    """인테이크 답변 1건을 선호 파일에 축적(날짜 + Q→A). 실패는 무시(fail-open)."""
    try:
        p = Path(path)
        q, answer = (q or "").strip(), (answer or "").strip()
        if not answer:
            return
        p.parent.mkdir(parents=True, exist_ok=True)
        fresh = not p.exists() or p.stat().st_size == 0
        from datetime import date
        line = f"- [{date.today().isoformat()}] {q[:120]}{' → ' if q else ''}{answer[:160]}\n"
        with open(p, "a", encoding="utf-8") as f:
            if fresh:
                f.write(_PREFS_HEADER)
            f.write(line)
    except Exception:
        pass


def load_preferences(path) -> str | None:
    """선호 파일 꼬리(최대 2000자)를 주입용으로 읽는다. 없으면/실패 시 None."""
    try:
        txt = Path(path).read_text(encoding="utf-8").strip()
        return txt[-2000:] or None
    except Exception:
        return None


# ---- §34.6 E1: StepRunner 스텝 입력 조립 ----
# 스텝 run 은 좁은 단일 임무라 §26 전체 하네스 대신 경량 프리앰블을 쓴다(토큰 절약 +
# 약한 모델 집중). 하드 규칙(되묻기 금지·실물 산출·검증·경로 보고)은 압축해 유지한다.
_STEP_PREAMBLE = (
    "You are executing ONE step of a larger autonomous background task. No interactive "
    "user is present — never ask back; state assumptions and proceed.\n"
    "Do ONLY this step's goal (other steps handle the rest), but do it COMPLETELY and "
    "for real:\n"
    "- Create real artifacts with tools (execute_code / write_file / terminal); verify "
    "they exist and are valid BEFORE reporting. Never claim work you did not perform.\n"
    "- Report concretely: what you did, full artifact path(s), how you verified. If you "
    "truly cannot finish, state exactly what failed and why."
)


def step_input(request: str, plan: dict, step: dict, *,
               capabilities: str | None = None, intake: str | None = None,
               feedback: str | None = None) -> str:
    """스텝 실행 run 에 보낼 입력 — 프리앰블+전체 맥락+완료 스텝 요약+현재 스텝+피드백.

    완료 스텝 요약(output 400자컷)이 스텝 간 맥락을 잇는다(세션 연속성의 보강 —
    스텝 경계 재개 시 전체 대화 재전송이 필요 없어진다, §34.6 E4).
    """
    steps = plan.get("steps") or []
    idx = next((i for i, s in enumerate(steps, 1) if s.get("id") == step.get("id")), 0)
    parts = [_STEP_PREAMBLE]
    if capabilities:
        parts.append("CAPABILITIES (verified on this machine):\n" + capabilities[:1200])
    parts.append("OVERALL REQUEST:\n" + (request or "")[:1500])
    if plan.get("dod"):
        parts.append("OVERALL DoD: " + plan["dod"])
    if intake:
        parts.append(intake)
    done = [s for s in steps if s.get("state") == "done"]
    if done:
        lines = [f"- ({s.get('id')}) {s.get('goal', '')} → "
                 f"{(s.get('output') or '완료').strip()[:200]}" for s in done]
        parts.append("COMPLETED STEPS (context — do not redo):\n" + "\n".join(lines))
    exp = step.get("expected") or {}
    desc = [f"YOUR STEP ({idx}/{len(steps)}): {step.get('goal', '')}"]
    if step.get("tool_hint") and step["tool_hint"] != "none":
        desc.append(f"Suggested tool: {step['tool_hint']}")
    if exp.get("type") == "file":
        desc.append("Must produce: a real, valid file"
                    + (f" ({exp['format']})" if exp.get("format") else "")
                    + (f" at {exp['path_hint']}" if exp.get("path_hint") else "")
                    + " — report its absolute path.")
    elif exp.get("type") == "action":
        desc.append("Must produce: an actual state change (run it for real).")
    for a in (step.get("accept") or []):
        desc.append(f"Done-when [{a.get('check')}]"
                    + (f": {a['arg']}" if a.get("arg") else ""))
    parts.append("\n".join(desc))
    if feedback:
        parts.append("[PREVIOUS ATTEMPT FAILED VERIFICATION — fix these before "
                     "reporting done]\n" + feedback)
    return "\n\n".join(parts)


# ---- 백그라운드 Heavy 실행 입력 조립 ----
# 하네스(§26) + 능력 인벤토리(§34.5) + 사용자 요청 + (있으면) 계획 힌트(§19)
# + 심화도 지시(§21) + 검증 피드백(§21 V3).
# Depth-specific directive appended after the request to scale rigor/verification.
_DEPTH_DIRECTIVE = {
    "high": "\n\n[TASK DEPTH: HIGH] Apply maximum rigor — thorough research and evidence "
            "gathering, multi-angle analysis, include methodology/assumptions in the "
            "deliverable, and strict self-verification + remediation after producing it.",
    "mid": "\n\n[TASK DEPTH: MID] Cover the essentials well; actually create and verify the "
           "artifact before reporting.",
    "low": "",
}


def _plan_hint(plan: dict | None) -> str:
    """계획을 실행 에이전트에 주입 — v2(steps)는 완료 기준까지, v1(subtasks)은 제안 힌트로."""
    if plan and (plan.get("version") == 2 or isinstance(plan.get("steps"), list)):
        steps = plan.get("steps") or []
        if not steps:
            return ""
        lines = []
        for i, s in enumerate(steps, 1):
            exp = s.get("expected") or {}
            bits = []
            if s.get("tool_hint"):
                bits.append(f"tool: {s['tool_hint']}")
            if exp.get("type") == "file":
                bits.append("produces: file"
                            + (f"({exp['format']})" if exp.get("format") else "")
                            + (f" {exp['path_hint']}" if exp.get("path_hint") else ""))
            elif exp.get("type") == "action":
                bits.append("produces: action/state-change")
            for a in (s.get("accept") or []):
                bits.append(f"done-when[{a.get('check')}]"
                            + (f": {a['arg']}" if a.get("arg") else ""))
            if s.get("needs"):
                bits.append("after: " + ",".join(s["needs"]))
            lines.append(f"  {i}. ({s.get('id')}) {s.get('goal', '')}"
                         + (f"  [{'; '.join(bits)}]" if bits else ""))
        head = ("\n\n[EXECUTION PLAN — follow these steps (adapt if needed) and self-check "
                "each step's done-when criteria before moving on]\n")
        dod = plan.get("dod")
        return head + (f"DoD: {dod}\n" if dod else "") + "\n".join(lines)
    subs = (plan or {}).get("subtasks") or []
    if not subs:
        return ""
    lines = [f"  {i}. {s.get('title', '')}"
             f"  [{s.get('kind', '')}/{s.get('effort', '')}]"
             + (f"  tools={','.join(s.get('tools') or [])}" if s.get("tools") else "")
             for i, s in enumerate(subs, 1)]
    return ("\n\n[제안된 하위작업 분해 — 참고/조정해 수행하세요(필요시 변경 가능)]\n"
            + "\n".join(lines))


def intake_block(questions: list | None = None, answers: list | dict | None = None,
                 assumptions: list | None = None) -> str:
    """§34.4 인테이크 결과 → 실행 입력 블록. 답변이 있으면 답변, 없으면 채택 가정.

    answers 관용 수용: 문자열 리스트(질문 순서 정렬) 또는 [{"q","answer"}] 또는 dict.
    """
    if answers:
        qs = questions or []
        if isinstance(answers, dict):
            answers = [answers]
        lines = []
        for i, a in enumerate(answers):
            if isinstance(a, dict):
                q = str(a.get("q") or (qs[i].get("q") if i < len(qs) else "") or "")
                ans = str(a.get("answer") or a.get("a") or "").strip()
            else:
                q = str(qs[i].get("q")) if i < len(qs) else ""
                ans = str(a).strip()
            if ans:
                lines.append(f"- {(q + ' → ') if q else ''}{ans}")
        if lines:
            return ("[사용자 확인 답변 — 아래 결정을 반드시 반영해 수행하세요]\n"
                    + "\n".join(lines))
    if assumptions:
        return ("[채택한 가정 — 사용자 무응답으로 아래 가정 하에 진행합니다. "
                "결과 보고에 이 가정을 명시하세요]\n"
                + "\n".join(f"- {a}" for a in assumptions))
    return ""


def autonomous_input(prompt: str, plan: dict | None = None,
                     feedback: str | None = None, *,
                     harness: str | None = None, depth: str | None = None,
                     capabilities: str | None = None,
                     intake: str | None = None) -> str:
    """백그라운드 실행 입력 조립(하네스+능력+요청+계획힌트+인테이크(답변/가정)+심화도+검증피드백)."""
    base = (render_capabilities(harness or default_prompt_text(), capabilities)
            + (prompt or "") + _plan_hint(plan))
    if intake:
        base += "\n\n" + intake
    base += _DEPTH_DIRECTIVE.get(depth or "", "")
    if feedback:
        base += ("\n\n[이전 시도가 검증을 통과하지 못했습니다 — 아래 미흡 항목을 "
                 "반드시 보완해 완수하세요]\n" + feedback)
    return base
