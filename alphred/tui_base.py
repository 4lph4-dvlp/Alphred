"""전용 TUI 공용 기반 — 상수·명령 레지스트리·헬퍼·입력/큐/채팅 위젯.

`AlphredTUI`(tui.py)와 그 Mixin(tui_commands/queue/chat)이 공유한다. App/Mixin 을 import 하지
않으므로 순환이 생기지 않는다(위젯은 `self.app.<메서드>` 를 런타임에 참조).

§36 T1: 채팅 영역이 RichLog(append-only)에서 위젯 스크롤(ChatView)로 전환됐다.
메시지·도구 블록이 개별 위젯이라 in-place 갱신·리사이즈 재래핑이 가능하다.
"""
from __future__ import annotations

import uuid
from collections import Counter

from rich.markup import escape as _esc
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Markdown, OptionList, Static, TextArea
from textual.widgets.option_list import Option

_BORDER = "#B22232"
_ACCENT = "#E63946"    # 브랜드 강조(Alph-RED)
_AMBER = "#FF9F45"     # 제목/헤딩

# 의미론적 상태 팔레트(§30.9) — markup 색상을 한 곳에서 관리한다.
_OK = "#7BC96F"        # 성공/완료/OK
_INFO = "#8FD3FF"      # 정보/사용자/진행
_WARN = "#FFB454"      # 경고/검토필요/보류
_ERR = "#FF6B4A"       # 오류/실패/폐기
_SOFT = "#FFE2D6"      # 옅은 본문/폴백

_SID_PREFIX = "alphred-tui-"


def _new_sid() -> str:
    return _SID_PREFIX + uuid.uuid4().hex[:8]


def _short_sid(sid: str) -> str:
    """표시·참조용 단축 세션 ID — 'alphred-tui-' 접두사를 떼어 고유 부분만 남긴다."""
    sid = sid or ""
    return sid[len(_SID_PREFIX):] if sid.startswith(_SID_PREFIX) else sid[:8]


# (이름, 설명, 인자필요, 핸들러). 팝업·/help·디스패치가 공유.
_COMMANDS = [
    ("help", "사용 가능한 명령 목록", False, "cmd_help"),
    ("model", "모델 영구 전환/보기  (예: /model meta/llama-3.3-70b-instruct · 깊이별: /model high|mid|low <이름>)", True, "cmd_model"),
    ("plan", "드라이런 — 실행 전 심화도/계획/비용 견적 미리보기  (예: /plan 보고서 만들어줘)", True, "cmd_plan"),
    ("depth", "작업 심화도 고정/해제  (예: /depth high, /depth auto)", True, "cmd_depth"),
    ("sessions", "저장된 세션 목록 / 전환 / 삭제  (번호 또는 ID; 예: /sessions, /sessions 2, /sessions delete a1b2c3d4)", True, "cmd_sessions"),
    ("clear", "화면 출력만 지우기 (현재 세션·대화 맥락 유지)", False, "cmd_clear"),
    ("new", "새 세션 시작 (대화 비우기)", False, "cmd_new"),
    ("queue", "큐 덱 열기 / 관리  (예: /queue, /queue ask \"우선순위 올려줘\", /queue cancel <id>)", True, "cmd_queue"),
    ("answer", "답변 대기(❓) 작업에 답하기  (예: /answer, /answer <id>)", True, "cmd_answer"),
    ("skills", "설치된 스킬 목록", False, "cmd_skills"),
    ("export", "현재 세션 대화를 Markdown 파일로 저장  (예: /export, /export report.md)", True, "cmd_export"),
    ("banner", "Alph-RED 전체 배너/로고 표시", False, "cmd_banner"),
    ("quit", "종료", False, "cmd_quit"),
]


def _state_label(s: str) -> str:
    return {"AwaitingInput": f"[{_AMBER}]입력대기[/]",
            "Pending": f"[{_WARN}]대기[/]", "In-Progress": f"[{_INFO}]진행[/]",
            "Paused": f"[{_WARN}]보류[/]", "Completed": f"[{_OK}]완료[/]",
            "NeedsReview": f"[{_WARN}]검토필요[/]",
            "Discarded": f"[{_ERR}]폐기[/]"}.get(s, s)


_ACTIVE_STATES = ("AwaitingInput", "Pending", "In-Progress", "Paused")
_TERMINAL_STATES = ("Completed", "NeedsReview", "Discarded")


def queue_badges(tasks: list[dict]) -> str:
    """상태줄 큐 배지(§36 Q1) 마크업 — ▶실행 ⏳대기(보류 포함) ❓입력대기 ⚠검토필요.

    순수 함수(테스트 용이). 활성이 하나도 없으면 옅은 '큐 —' 를 반환한다.
    """
    c = Counter((t.get("state") or "") for t in tasks)
    parts: list[str] = []
    if c["In-Progress"]:
        parts.append(f"[{_INFO}]▶{c['In-Progress']}[/]")
    waiting = c["Pending"] + c["Paused"]
    if waiting:
        parts.append(f"[{_WARN}]⏳{waiting}[/]")
    if c["AwaitingInput"]:
        parts.append(f"[b {_AMBER}]❓{c['AwaitingInput']}[/]")
    if c["NeedsReview"]:
        parts.append(f"[b {_WARN}]⚠{c['NeedsReview']}[/]")
    return ("큐 " + " ".join(parts)) if parts else "[dim]큐 —[/]"


def model_short(label: str | None) -> str:
    """상태줄용 모델 단축명 — provider 접두어([nvidia] 등)와 네임스페이스를 걷어낸다."""
    label = (label or "").split("[")[0].strip()
    return label.rsplit("/", 1)[-1] if label else ""


def fuzzy_match(query: str, name: str) -> bool:
    """부분열(subsequence) fuzzy 매칭(§36 I4) — 'mdl' ⊂ 'model'. 대소문자 무시."""
    it = iter(name.lower())
    return all(ch in it for ch in query.lower())


DEPTH_CYCLE = [None, "low", "mid", "high"]   # §36 I6 shift+tab 순환(None=auto)


# ---- §36 Q2 TaskCard 순수 렌더 헬퍼 ----
def step_progress(plan) -> tuple[int, int, str | None]:
    """plan v2 스텝 실측 상태 → (완료 수, 전체 수, 현재 실행 스텝 goal).

    StepRunner(§34.6)가 기록한 step.state 가 없으면 (0, 0, None) — 호출자가 도구
    카운트(plan_progress)로 폴백한다.
    """
    steps = (plan or {}).get("steps") if isinstance(plan, dict) else None
    if not isinstance(steps, list) or not any(isinstance(s, dict) and "state" in s
                                              for s in steps):
        return 0, 0, None
    done = sum(1 for s in steps if s.get("state") == "done")
    cur = next((s.get("goal") for s in steps if s.get("state") == "running"), None)
    return done, len(steps), cur


def progress_bar(done: int, total: int, width: int = 8) -> str:
    """▰▰▱▱ 진행바 — total 0 이면 빈 문자열."""
    if total <= 0:
        return ""
    filled = max(0, min(width, round(done / total * width)))
    return "▰" * filled + "▱" * (width - filled)


def _remaining_mmss(deadline: str | None) -> str | None:
    """입력 마감(ISO)까지 남은 m:ss — 없거나 지났으면 None."""
    if not deadline:
        return None
    try:
        from datetime import datetime, timezone
        dl = datetime.fromisoformat(deadline)
        if dl.tzinfo is None:
            dl = dl.replace(tzinfo=timezone.utc)
        left = int((dl - datetime.now(timezone.utc)).total_seconds())
    except Exception:
        return None
    if left <= 0:
        return None
    return f"{left // 60}:{left % 60:02d}"


def task_card_markup(t: dict) -> str:
    """인라인 TaskCard(§36 Q2) 마크업 — 상태별로 접수/질문/진행/선점/종결을 표현.

    순수 함수(테스트 용이). 2s 폴링의 task_view dict 를 그대로 받는다.
    """
    tid = (t.get("id") or "")[:8]
    prompt = (t.get("prompt") or "").replace("\n", " ").strip()
    head_txt = _esc(prompt[:48] + ("…" if len(prompt) > 48 else ""))
    state = t.get("state") or ""
    plan = t.get("plan") if isinstance(t.get("plan"), dict) else {}
    est = t.get("estimate") or {}
    lines = [f"[b]▛ 작업 {tid}[/] · {head_txt}  {_state_label(state)}"]
    meta = []
    if t.get("priority"):
        meta.append(f"우선 {t['priority']}")
    if t.get("depth"):
        meta.append(f"심화도 {t['depth']}")
    if est.get("est_llm_calls"):
        meta.append(f"견적 ~{est['est_llm_calls']}콜")
    if state == "AwaitingInput":
        left = _remaining_mmss(t.get("input_deadline"))
        wait = f"남은 {left} · " if left else ""
        lines.append(f"[{_AMBER}]❓ 답변 대기[/] — [dim]{wait}그대로 두면 추천값(✦)을 "
                     f"가정하고 진행 · /answer {tid}[/]")
    elif state == "Pending":
        lines.append(f"[{_WARN}]⏳ 대기[/]  [dim]{' · '.join(meta)}[/]")
        if plan.get("dod"):
            lines.append(f"[dim]  DoD: {_esc(str(plan['dod'])[:80])}[/]")
    elif state == "In-Progress":
        done, total, cur = step_progress(plan)
        if total:
            bar = progress_bar(done, total)
            row = f"[{_INFO}]▶ {bar} {done}/{total}[/]"
            if cur:
                row += f"  [dim]현재: {_esc(str(cur)[:60])}[/]"
            lines.append(row)
        else:
            prog = t.get("plan_progress") or 0
            act = t.get("plan_activity")
            row = f"[{_INFO}]▶ 실행 중[/]" + (f" [dim]도구 {prog}⚙[/]" if prog else "")
            if act:
                row += f"  [dim]🔧 {_esc(str(act))}[/]"
            lines.append(row)
        if t.get("verify_attempts"):
            lines.append(f"[dim]  재시도 {t['verify_attempts']}회[/]")
    elif state == "Paused":
        lines.append(f"[{_WARN}]⏸ 보류[/] — [dim]우선순위 높은 작업에 선점됨 · "
                     f"실행/스텝 경계에서 자동 재개[/]")
    elif state == "Completed":
        rep = t.get("verify_report") or {}
        badge = f"[{_OK}]검증 ✓[/]" if rep.get("passed") else \
            (f"[{_WARN}]검증 ⚠[/]" if rep else "")
        j = rep.get("judge") if isinstance(rep, dict) else None
        if isinstance(j, dict) and j.get("score") is not None:
            badge += f" [dim]judge {j['score']}[/]"
        lines.append(f"[b {_OK}]✓ 완료[/]  {badge}")
        res = (t.get("result") or "").strip().replace("\n", " ")
        if res:
            lines.append(f"[dim]  {_esc(res[:160])}{'…' if len(res) > 160 else ''}[/]")
        lines.append(f"[dim]  전체 결과: ctrl+t 덱에서 {tid} 선택[/]")
    elif state == "NeedsReview":
        rep = t.get("verify_report") or {}
        lines.append(f"[b {_WARN}]⚠ 검토 필요[/] — [dim]{_esc(str(rep.get('summary') or '검증 미통과'))}"
                     f" · ctrl+t 상세 · /queue retry {tid}[/]")
    elif state == "Discarded":
        err = (t.get("error") or "").replace("\n", " ")[:100]
        lines.append(f"[{_ERR}]✗ 폐기[/]" + (f"  [dim]{_esc(err)}[/]" if err else ""))
    return "\n".join(lines)


class TaskCard(Static):
    """대화 속 라이브 작업 카드(§36 Q2) — 2s 폴링으로 제자리 갱신, 종결 시 최종 확정."""

    def __init__(self, task_id: str):
        self.task_id = task_id
        super().__init__(f"[b]▛ 작업 {task_id[:8]}[/] [dim]접수 중…[/]",
                         classes="alph-card")

    def update_from(self, t: dict) -> None:
        try:
            self.update(task_card_markup(t))
        except Exception:
            pass


class ChatView(VerticalScroll):
    """채팅 영역(§36 D4) — 메시지/도구/카드 위젯의 세로 스크롤 컨테이너.

    RichLog 대체: 위젯 단위라 in-place 갱신과 리사이즈 재래핑이 된다.
    바닥에 있을 때만 자동 스크롤(위로 긁어 읽는 중이면 방해하지 않음).
    """
    MAX_CHILDREN = 600     # 성능 상한 — 넘치면 오래된 위젯부터 제거

    def add(self, widget) -> None:
        at_bottom = self.scroll_offset.y >= self.max_scroll_y - 1
        self.mount(widget)
        kids = list(self.children)
        if len(kids) > self.MAX_CHILDREN:
            for w in kids[:len(kids) - self.MAX_CHILDREN]:
                w.remove()
        if at_bottom:
            self.call_after_refresh(lambda: self.scroll_end(animate=False))


class AssistantMd(Markdown):
    """최종 답변 마크다운(§36 D3) — 원문을 보존한다(source_text: 세션 기록·테스트용)."""

    def __init__(self, text: str):
        self.source_text = text
        super().__init__(text)


class ToolBlock(Static):
    """도구 호출 블록(§36 B1) — `● 도구명 실행 중…` → 완료 시 제자리에서 ✓/✗ + 결과 줄로 갱신.

    §36 I7: 기본은 결과 미리보기 1줄(64자), 상세 모드(ctrl+o)면 원문 여러 줄을 편다.
    """
    _MAX_LINES = 8         # 상세 모드 결과 줄 상한

    def __init__(self, tool: str):
        self.tool = tool
        self.running = True
        self.ok: bool | None = None
        self.preview = ""            # 결과 원문(컷 없이 보관 — 상세 토글 재렌더용)
        self.expanded = False
        super().__init__(f"[{_ACCENT}]●[/] [b]{_esc(tool)}[/] [dim]실행 중…[/]")

    def _markup(self) -> str:
        if self.ok is False:
            return f"[dim]●[/] {_esc(self.tool)} [{_ERR}]✗ 실패[/]"
        head = f"[dim]●[/] {_esc(self.tool)} [{_OK}]✓[/]"
        if not self.preview:
            return head
        if self.expanded:
            lines = self.preview.strip().splitlines()
            body = "".join(f"\n[dim]  ⎿ {_esc(ln)}[/]" for ln in lines[:self._MAX_LINES])
            if len(lines) > self._MAX_LINES:
                body += f"\n[dim]  ⎿ … 외 {len(lines) - self._MAX_LINES}줄[/]"
            return head + body
        pv = self.preview.replace("\n", " ").strip()[:64]
        return head + f"\n[dim]  ⎿ {_esc(pv)}[/]"

    def complete(self, preview: str = "", *, expanded: bool = False) -> None:
        self.running = False
        self.ok = True
        self.preview = preview or ""
        self.expanded = expanded
        self.update(self._markup())

    def fail(self) -> None:
        self.running = False
        self.ok = False
        self.update(self._markup())

    def set_expanded(self, expanded: bool) -> None:
        self.expanded = expanded
        if not self.running:
            self.update(self._markup())


class ThinkBlock(Static):
    """모델 사고 블록(§36 I7) — 기본 접힘(요약 1줄), 상세 모드(ctrl+o)면 전문."""
    _HEAD = 48

    def __init__(self, text: str, *, expanded: bool = False):
        self.full = text
        self.expanded = expanded
        super().__init__("")
        self.update(self._markup())

    def _markup(self) -> str:
        flat = self.full.replace("\n", " ").strip()
        if self.expanded or len(flat) <= self._HEAD:
            return f"[dim]  ┊ 💭 {_esc(self.full.strip())}[/]"
        return (f"[dim]  ┊ 💭 {_esc(flat[:self._HEAD])}… "
                f"({len(flat)}자 · ctrl+o 전체)[/]")

    def set_expanded(self, expanded: bool) -> None:
        self.expanded = expanded
        self.update(self._markup())


class QuestionCard(Vertical):
    """인테이크 질문 카드(§36 I3, §34.4) — ↑↓+Enter 선택, ✦ 추천 기본 하이라이트.

    선택지는 OptionList 로 인터랙티브하게, '직접 입력…' 선택 시 입력창으로 포커스를
    넘긴다(기존 텍스트 답변 경로와 병행 — 입력창 타이핑도 여전히 동작).
    """
    FREE_ID = "__free__"
    DEFAULT_CSS = f"""
    QuestionCard {{ height: auto; border: round {_ACCENT}; padding: 0 1; margin: 0 0 1 0; }}
    QuestionCard > Static {{ height: auto; }}
    QuestionCard > OptionList {{ height: auto; max-height: 8; }}
    """

    def __init__(self):
        super().__init__()
        self._title = Static("")
        self._opts = OptionList()

    def compose(self):
        yield self._title
        yield self._opts

    def show_question(self, q: dict, idx: int, total: int) -> None:
        from rich.text import Text
        head = f"[{q.get('header')}] " if q.get("header") else ""
        self._title.update(
            f"[b {_AMBER}]❓ {_esc(head)}{_esc(q.get('q', ''))}[/]  "
            f"[dim]({idx + 1}/{total} · ↑↓+Enter 선택 · 입력창 직접 답변 · Esc 보류)[/]")
        self._opts.clear_options()
        rec = 0
        for n, o in enumerate(q.get("options") or []):
            t = Text(o.get("label", ""))
            if o.get("recommended"):
                rec = n
                t.append("  ✦ 추천", style=f"bold {_ACCENT}")
            self._opts.add_option(Option(t, id=f"opt:{n}"))
        self._opts.add_option(Option(Text("✏ 직접 입력…", style="dim"),
                                     id=QuestionCard.FREE_ID))
        self._opts.highlighted = rec        # ✦ 추천이 기본 하이라이트(Enter=추천)
        self._opts.focus()

    def on_option_list_option_selected(self, event) -> None:
        event.stop()
        oid = event.option.id or ""
        if oid == QuestionCard.FREE_ID:
            self.app.answer_free_input()
        else:
            self.app.answer_pick(int(oid.split(":", 1)[1]))

    def on_key(self, event) -> None:
        if event.key == "escape":           # 보류 — 타임아웃 시 추천값 가정 진행(§34.4)
            event.stop()
            self.app.answer_mode_cancel()


class PromptInput(TextArea):
    """멀티라인 입력 — Enter 전송 / Shift+Enter 줄바꿈(Ctrl+J 는 호환 폴백).

    슬래시 팝업이 떠 있으면 ↑/↓·Tab·Esc·Enter 를 팝업 조작으로 가로챈다.
    팝업이 없을 때 ↑/↓ 는 현재 줄이 맨 위/맨 아래일 때만 입력 히스토리(이전 명령·메시지)로
    쓰고, 그 외에는 평소처럼 커서 줄이동(멀티라인 편집 보존).
    """
    def _cursor_row(self) -> int:
        try:
            return self.cursor_location[0]
        except Exception:
            return 0

    def _last_row(self) -> int:
        try:
            return self.document.line_count - 1
        except Exception:
            return 0

    def on_key(self, event) -> None:
        app = self.app
        if app.palette_visible():
            if event.key == "down":
                app.palette_move(1)
            elif event.key == "up":
                app.palette_move(-1)
            elif event.key == "enter":
                app.palette_accept(execute=True)        # 팝업 떠 있으면 Enter = 선택 실행
            elif event.key == "tab":
                app.palette_accept(execute=False)       # Tab = 인자 입력용으로 채우기
            elif event.key == "escape":
                app.palette_hide()
            else:
                return
            event.stop()
            event.prevent_default()
            return
        if event.key == "escape":
            event.stop()
            event.prevent_default()
            if getattr(app, "_pending_input", None) is not None:
                app.answer_mode_cancel()             # §34.4 질문 보류(타임아웃 시 가정 진행)
            elif getattr(app, "_busy", False):
                app.cancel_send()                    # §36 I1 — 응답 스트림 즉시 중단
            elif getattr(app, "_live_tid", None):
                app.stop_live()                      # §33 라이브 뷰 이탈(작업은 계속)
            return
        if event.key == "shift+tab":                     # §36 I6 — 심화도 순환
            event.stop()
            event.prevent_default()
            app.action_cycle_depth()
            return
        if event.key in ("pageup", "pagedown"):          # 채팅 스크롤(입력 포커스 유지)
            try:
                chat = app.query_one("#chat", ChatView)
                (chat.scroll_page_up if event.key == "pageup" else chat.scroll_page_down)()
                event.stop()
                event.prevent_default()
            except Exception:
                pass
            return
        if event.key in ("shift+enter", "ctrl+j"):       # 줄바꿈(주: Shift+Enter)
            event.stop()
            event.prevent_default()
            self.insert("\n")
        elif event.key == "enter":                       # 전송
            event.stop()
            event.prevent_default()
            app.submit_current()
        elif event.key == "up" and self._cursor_row() == 0:        # 히스토리 이전(맨 윗줄에서만)
            if app.history_prev():
                event.stop()
                event.prevent_default()
        elif event.key == "down" and self._cursor_row() == self._last_row():  # 히스토리 다음
            if app.history_next():
                event.stop()
                event.prevent_default()


# (구) QueueTable 상주 패널은 §36 T3 에서 폐지 — 큐 조작은 QueueDeck 모달(ctrl+t)로 이관.
