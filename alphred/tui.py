"""전용 Alphred TUI (Textual) — Alphred 게이트웨이(:8643)의 터미널 클라이언트.

설계(기획 §13/§16/§17/§13.4, §36 T1 개편): web/ESP32 와 동일하게 게이트웨이에 붙는
단일-코어 클라이언트.
- 채팅 영역: 위젯 스크롤(ChatView) — 메시지·도구 블록이 개별 위젯(in-place 갱신·재래핑)
- 시작 화면: 컴팩트 웰컴 패널(버전·서버·모델·팁). 전체 배너 아트는 `/banner` 로 보존
- 입력 → 게이트웨이 `/chat/stream`(SSE) → 도구 블록(●/⎿) 실시간 갱신 + 마크다운 답변
- 멀티라인 입력(TextArea): Enter 전송 · Shift+Enter 줄바꿈 · ↑↓ 히스토리 · PgUp/PgDn 스크롤
- 상태줄: 스피너+경과시간 · 큐 배지(▶⏳❓⚠) · depth · 모델 · 세션
- 세션 복원/관리: 대화 화면 기록을 alphred_home 에 저장, 재시작 시 복원 + `/sessions`
- 큐 패널 → `/queue` 폴링(표) + 유휴 시 완료/폐기 알림
- 슬래시 명령: `/` 입력 시 팝업 목록(설명 포함), ↑/↓·Enter/Tab·Esc 조작

이 파일은 App 코어(수명주기·웰컴/상태줄·세션 상태)만 담고, 나머지 관심사는 믹스인으로 분리:
`CommandsMixin`(tui_commands) · `QueueMixin`(tui_queue) · `ChatMixin`(tui_chat).
공용 위젯/상수/헬퍼는 `tui_base`.
"""
from __future__ import annotations

import time

import httpx
from rich.text import Text
from textual.app import App, ComposeResult
from textual.widgets import Footer, Header, OptionList, Static

from .splash import banner_lines
from .tui_base import (AssistantMd, ChatView, PromptInput, TaskCard, _ACCENT, _BORDER,
                       _COMMANDS, _INFO, _new_sid, _short_sid, model_short, queue_badges)
from .tui_chat import ChatMixin
from .tui_commands import CommandsMixin
from .tui_queue import QueueMixin
from .tui_sessions import SessionStore


def _pkg_version() -> str:
    try:
        from importlib.metadata import version
        return "v" + version("alphred")
    except Exception:
        return ""


class AlphredTUI(CommandsMixin, QueueMixin, ChatMixin, App):
    TITLE = "Alphred"
    SUB_TITLE = "준비됨"
    # §36 D1: 배경색 강제를 걷어내 사용자의 터미널 팔레트를 승계한다.
    # Alph-RED 는 액센트(프롬프트 보더·브랜드 마크·추천 표시)로만 존재.
    CSS = f"""
    #chat {{ height: 1fr; padding: 0 1; scrollbar-size-vertical: 1; }}
    #chat Static {{ height: auto; }}
    #chat Markdown {{ margin: 0 0 1 0; }}
    #chat .alph-card {{ border: round {_BORDER}; padding: 0 1; margin: 0 0 1 0; }}
    #streaming {{ display: none; height: auto; max-height: 12; padding: 0 1; }}
    #statusbar {{ height: 1; padding: 0 1; }}
    #palette {{ display: none; height: auto; max-height: 10; border: round {_ACCENT}; }}
    #prompt {{ border: round {_ACCENT}; height: auto; min-height: 3; max-height: 10; }}
    """
    # ctrl+c 는 Textual 기본(선택 텍스트 복사 / 선택 없으면 종료)에 맡긴다.
    BINDINGS = [("ctrl+q", "quit", "종료"), ("ctrl+l", "clear_chat", "지우기"),
                ("ctrl+t", "queue_deck", "큐 덱"),
                ("ctrl+o", "toggle_verbose", "상세"),
                ("ctrl+y", "copy_last", "답변 복사"),
                ("shift+tab", "cycle_depth", "심화도")]

    def __init__(self, base_url: str, api_key: str | None, sessions_dir=None):
        super().__init__()
        self._base = base_url.rstrip("/")
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self.http = httpx.AsyncClient(base_url=self._base, headers=headers, timeout=300.0)
        self.session_id = _new_sid()
        self.model: str | None = None      # /model 로 전환한 세션 모델(None=config 기본)
        self.depth_override: str | None = None  # /depth 로 고정한 작업 심화도(None=자동 판정)
        self._busy = False
        self._busy_since = 0.0             # 상태줄 경과시간(§36 B3)
        self._status_text = "준비됨"
        self._badges = "[dim]큐 —[/]"      # 상태줄 큐 배지(§36 Q1)
        self.verbose = False               # §36 I7 상세 토글(사고 전문·도구 결과 전문)
        self._pending_msgs: list[str] = []  # §36 I2 busy 중 제출된 대기 메시지
        self._send_worker = None           # §36 I1 진행 중 send 워커(Esc 취소 대상)
        self._model_names: list[str] = []  # §36 I4 /model 인자 완성용 캐시
        self._stream_buf = ""              # (레거시) 스트리밍 위젯 버퍼
        self._proc_buf = ""                # 어시스턴트 중간/최종 텍스트 누적
        self._think_buf = ""               # 모델 사고(thinking/reasoning) 누적(항상 회색)
        self._open_tools: list = []        # 실행 중 ToolBlock (in-place 갱신 대상)
        self._rows: list[tuple] = []       # 큐 표 행 → (id, priority, state) 매핑
        self._states: dict[str, str] = {}
        self._cmd_map = {n: {"desc": d, "needs_args": a, "handler": h}
                         for (n, d, a, h) in _COMMANDS}
        self._sessions = SessionStore(sessions_dir) if sessions_dir else None
        self._session: dict | None = None  # 현재 세션(영속화 활성 시)
        self._welcome_widget: Static | None = None  # 웰컴 패널(§36 D2, 참조로 갱신)
        self._task_cards: dict[str, TaskCard] = {}  # §36 Q2 활성 인라인 카드(id→위젯)
        # 배경 워커용 위젯 참조(on_mount 에서 고정 — 모달 위에서도 안전)
        self._chat_view = self._statusbar_w = self._streaming_w = None
        self._has_convo = False            # 대화 시작 여부
        self._model_label = "(불러오는 중)"  # 상태줄/웰컴용 현재 모델 라벨
        self._history: list[str] = []      # 입력 히스토리(↑/↓) — 제출한 명령·메시지
        self._hist_idx: int | None = None  # 히스토리 탐색 위치(None=편집 중인 새 입력)
        self._hist_draft = ""              # 히스토리 진입 전 작성 중이던 입력 보존
        self._live_tid: str | None = None  # §33 라이브 뷰 중인 작업 id
        self._live_worker = None           # 라이브 스트림 워커(Esc 로 취소)
        self._pending_input: dict | None = None  # §34.4 답변 모드 상태(task_id/questions/idx)
        self.slots: int = 1
        self.slots_config: str = "1"
        self.slots_max: int = 4
        self.active_slots: int = 0
        self.budgets: dict = {}

    def compose(self) -> ComposeResult:
        # §36 T3: 상시 큐 패널 폐지 — 큐는 상태줄 배지 + 인라인 TaskCard + 큐 덱(ctrl+t).
        yield Header(show_clock=True)
        yield ChatView(id="chat")
        yield Static("", id="streaming")
        yield Static("", id="statusbar")
        yield OptionList(id="palette")
        yield PromptInput(id="prompt", soft_wrap=True)
        yield Footer()

    def on_mount(self) -> None:
        # 배경 워커(큐 폴링·라이브 스트림·상태줄 틱)가 건드리는 위젯은 참조로 고정한다 —
        # App.query_one 은 최상단 스크린을 검색하므로 모달(세션 피커·큐 덱)이 떠 있는 동안
        # NoMatches 로 워커가 죽는 것을 막는다(§36 I5).
        self._chat_view = self.query_one("#chat", ChatView)
        self._statusbar_w = self.query_one("#statusbar", Static)
        self._streaming_w = self.query_one("#streaming", Static)
        inp = self.query_one("#prompt", PromptInput)
        inp.border_title = self._PROMPT_TITLE
        self._initial_render()
        inp.focus()
        self.set_interval(2.0, self.refresh_queue)
        self.set_interval(1.0, self._tick_status)
        self.run_worker(self.refresh_queue())
        self.run_worker(self._update_model_display())

    def _initial_render(self) -> None:
        """시작 1회: 웰컴 패널 + (있으면) 직전 세션 복원."""
        if self._sessions:
            latest = self._sessions.latest()
            if latest and latest.get("messages"):
                self._load_session(latest)
                self._log(f"[dim]직전 세션 복원됨: {latest.get('title') or latest['id'][:8]} "
                          f"({len(latest['messages'])}개 메시지) · /sessions 로 전환[/]")
                return
        self._new_session(announce=False)

    # ---- 채팅 영역(§36 D4: 위젯 스크롤) ----
    def _chat(self) -> ChatView:
        return self._chat_view

    def _chat_add(self, widget) -> None:
        self._chat().add(widget)

    def _log(self, markup: str) -> None:
        """마크업 한 덩어리를 채팅 영역에 추가(믹스인 공용 — RichLog.write 대체)."""
        self._chat_add(Static(markup))

    def add_assistant(self, text: str) -> None:
        """최종 답변(§36 D3) — ◆ 헤더 + 마크다운 렌더."""
        self._chat_add(Static(f"[b {_ACCENT}]◆ Alphred[/]"))
        self._chat_add(AssistantMd(text))

    # ---- 웰컴 패널(§36 D2) ----
    def _welcome_renderable(self) -> Text:
        """가용 폭/높이에 맞는 배너 및 로고 + 정보 스택."""
        from .splash import banner_lines, logo_lines
        w = self.size.width if self.size.width > 0 else 80
        h = self.size.height if self.size.height > 0 else 24

        # Determine banner (responsive)
        art = banner_lines(w)

        # Determine logo (if height is reasonably large, e.g. >= 22)
        logo = []
        if h >= 22:
            logo = logo_lines(w, avail_height=h - 12)

        ver = _pkg_version()
        info = [
            Text.from_markup(f"[b {_ACCENT}]◆ Alphred[/] [b]{ver}[/]"
                             f"  [dim]— Hermes 위 선점형 큐 에이전트[/]"),
            Text.from_markup(f"[dim]서버[/] {self._base}"
                             f"  [dim]모델[/] {self._model_label}"
                             f"  [dim]세션[/] {self._session_label()}"),
            Text(""),
            Text.from_markup("[dim]Enter 전송 · Shift+Enter 줄바꿈 · / 명령 · "
                             "↑↓ 입력 기록 · PgUp/PgDn 스크롤[/]"),
            Text.from_markup("[dim]/plan 드라이런 · /queue 큐 관리 · /sessions 세션 · "
                             "/banner 전체 로고[/]"),
        ]

        out = Text()
        out.append("\n")
        # Banner
        for line in art:
            out.append_text(line)
            out.append("\n")
        out.append("\n")

        # Logo
        if logo:
            for line in logo:
                out.append_text(line)
                out.append("\n")
            out.append("\n")

        # Info
        for line in info:
            out.append_text(line)
            out.append("\n")

        return out

    def _mount_welcome(self) -> None:
        # remove_children() 의 제거는 비동기라 id 를 쓰면 재마운트 시 중복 — 참조로 관리한다.
        self._welcome_widget = Static(self._welcome_renderable())
        self._chat_add(self._welcome_widget)

    def _refresh_welcome(self) -> None:
        try:
            if self._welcome_widget is not None:
                self._welcome_widget.update(self._welcome_renderable())
        except Exception:
            pass

    # ---- 상태줄(§36 B3/Q1) ----
    def _status(self, s: str) -> None:
        self._status_text = s
        self.sub_title = s
        self._refresh_statusbar()

    def _tick_status(self) -> None:
        if self._busy:
            self._refresh_statusbar()

    def _refresh_statusbar(self) -> None:
        if self._busy:
            elapsed = int(time.monotonic() - self._busy_since)
            left = f"[{_ACCENT}]✻[/] {self._status_text} [dim]{elapsed}s[/]"
        else:
            left = f"[dim]{self._status_text}[/]"
        sep = "  [dim]│[/]  "
        is_auto = str(self.slots_config).lower() == "auto"
        slot_label = f"[dim]slots:[/]{self.active_slots}/{self.slots}"
        if is_auto:
            slot_label += "⚡"
        parts = [left, self._badges, f"[dim]depth:[/]{self.depth_override or 'auto'}", slot_label]
        # §36 T4 저폭 반응형: 좁은 터미널에선 모델·세션을 접어 배지/상태를 보존.
        if self.size.width >= 80:
            m = model_short(self._model_label)
            if m:
                parts.append(f"[dim]모델[/] {m}")
            parts.append(f"[dim]세션[/] {self._session_label()}")
            or_budget = self.budgets.get("openrouter", {})
            nv_budget = self.budgets.get("nvidia", {})
            or_rem = or_budget.get("remaining", 50)
            nv_rem = nv_budget.get("remaining", 100)
            parts.append(f"[dim]budget:[/] OR:{or_rem} NV:{nv_rem}")
        try:
            self._statusbar_w.update(sep.join(parts))
        except Exception:
            pass

    def _update_badges(self, tasks: list[dict]) -> None:
        self._badges = queue_badges(tasks)
        self._refresh_statusbar()

    def _session_label(self) -> str:
        """현재 세션의 단축 ID(상태줄·식별용). 제목이 아니라 ID 를 표시한다."""
        if self._session:
            return _short_sid(self._session.get("id", "")) or _short_sid(self.session_id)
        return _short_sid(self.session_id)

    def _set_titlebar(self) -> None:
        """모델·세션 표시 갱신(상태줄+웰컴) — 이름은 RichLog 보더 시절의 잔재지만 계약 유지."""
        self._refresh_statusbar()
        self._refresh_welcome()

    async def _update_model_display(self) -> None:
        """현재 사용 모델 라벨을 갱신하고 상태줄/웰컴 재구성(세션 전환 모델 우선)."""
        model, prov = self.model, None
        has_tiers = False
        tiers = {}
        try:
            d = (await self.http.get("/models/available")).json()
            model = self.model or d.get("current")
            prov = d.get("provider")
            models_list = d.get("models") or []
            if models_list and isinstance(models_list[0], dict):
                self._model_names = [m["id"] for m in models_list]
            else:
                self._model_names = models_list
            has_tiers = d.get("has_tiers", False)
            tiers = d.get("tiers") or {}
        except Exception:
            pass

        # 세션별 강제 전환 모델이 없고, depth별 모델 라우팅이 설정되어 있다면 라우팅 정보 표시
        if not self.model and has_tiers:
            t_models = []
            for t in ("high", "mid", "low"):
                t_spec = tiers.get(t)
                if t_spec and t_spec.get("model"):
                    t_models.append(t_spec["model"])
            if t_models:
                if all(m == "auto" for m in t_models):
                    label = "auto (카테고리 자동 라우팅)"
                else:
                    label = f"depth 라우팅 ({'/'.join(t_models)})"
            else:
                label = model or "(config 기본값)"
        else:
            label = model or "(config 기본값)"

        if prov and not label.startswith("auto") and not label.startswith("depth"):
            label += f"  [{prov}]"
        self._model_label = label
        self._set_titlebar()

    def action_clear_chat(self) -> None:
        self._chat().remove_children()

    def action_queue_deck(self) -> None:
        """§36 Q4 — 큐 덱 모달(ctrl+t): 리스트+상세+조작."""
        from .tui_queue import QueueDeck
        if isinstance(self.screen, QueueDeck):
            return
        self.push_screen(QueueDeck())

    def _spawn_task_card(self, task_id: str) -> None:
        """§36 Q2 — 내 대화에서 생긴 작업의 인라인 라이브 카드를 심는다(중복 방지)."""
        if not task_id or task_id in self._task_cards:
            return
        card = TaskCard(task_id)
        self._task_cards[task_id] = card
        self._chat_add(card)

    def action_cycle_depth(self) -> None:
        """§36 I6 — shift+tab 으로 작업 심화도 순환: auto→low→mid→high→auto."""
        from .tui_base import DEPTH_CYCLE
        i = DEPTH_CYCLE.index(self.depth_override) if self.depth_override in DEPTH_CYCLE else 0
        self.depth_override = DEPTH_CYCLE[(i + 1) % len(DEPTH_CYCLE)]
        self._refresh_statusbar()

    def _last_assistant_text(self) -> str | None:
        """세션 기록에서 마지막 어시스턴트 답변 원문(§36 I8 복사·내보내기용)."""
        for m in reversed((self._session or {}).get("messages") or []):
            if m.get("role") == "assistant" and (m.get("text") or "").strip():
                return m["text"]
        return None

    def action_copy_last(self) -> None:
        """ctrl+y — 마지막 답변을 클립보드로(§36 I8)."""
        text = self._last_assistant_text()
        if not text:
            self._log("[dim]복사할 답변이 아직 없습니다.[/]")
            return
        try:
            self.copy_to_clipboard(text)
            self._log(f"[dim]📋 마지막 답변을 클립보드에 복사했습니다 ({len(text)}자).[/]")
        except Exception as e:
            self._log(f"[{_INFO}]클립보드 복사 실패({e}) — /export 로 파일 저장을 이용하세요.[/]")

    def action_toggle_verbose(self) -> None:
        """§36 I7 — 상세 토글: 사고(ThinkBlock) 전문 + 도구 결과 전문을 펴고 접는다."""
        from .tui_base import ThinkBlock, ToolBlock
        self.verbose = not self.verbose
        for w in self.query(ThinkBlock):
            w.set_expanded(self.verbose)
        for w in self.query(ToolBlock):
            w.set_expanded(self.verbose)
        self._log(f"[dim]상세 표시 {'켬 — 사고·도구 결과 전문' if self.verbose else '끔 — 컴팩트'}"
                  f" (ctrl+o)[/]")

    # ---- 세션 상태 ----
    def _new_session(self, *, announce: bool = True) -> None:
        self.session_id = _new_sid()
        self._session = self._sessions.new(self.session_id, self.model) if self._sessions else None
        self._has_convo = False
        self._chat().remove_children()
        self._mount_welcome()
        self._set_titlebar()
        if announce:
            self._log("[dim]대화를 지우고 새 세션을 시작했습니다.[/]")

    def _load_session(self, session: dict) -> None:
        self._session = session
        self.session_id = session["id"]
        self.model = session.get("model")
        self._has_convo = bool(session.get("messages"))
        self._chat().remove_children()
        self._mount_welcome()
        self._set_titlebar()
        for m in session.get("messages", []):
            if m.get("role") == "user":
                self._log(f"[b {_INFO}]›[/] {m.get('text', '')}")
            else:
                self.add_assistant(m.get("text", ""))

    def _record(self, role: str, text: str) -> None:
        if self._session is None or not text:
            return
        self._session.setdefault("messages", []).append({"role": role, "text": text})
        self._session["model"] = self.model
        if self._sessions:
            self._sessions.save(self._session)


def run_tui(base_url: str, api_key: str | None, sessions_dir=None) -> None:
    # §36 I8: mouse=True 복귀 — 위젯 채팅에서는 마우스 휠 스크롤·클릭(도구/카드 펼침)이
    # 더 값지다. 터미널 네이티브 긁기 복사를 잃는 대신 ctrl+y(마지막 답변)·/export(세션 md)
    # 로 대체하고, 대부분 터미널의 Shift+드래그(마우스 캡처 우회)는 여전히 동작한다.
    AlphredTUI(base_url, api_key, sessions_dir).run()
