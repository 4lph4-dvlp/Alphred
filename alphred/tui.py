"""전용 Alphred TUI (Textual) — Alphred 게이트웨이(:8643)의 터미널 클라이언트.

설계(기획 §13/§16/§17/§13.4): web/ESP32 와 동일하게 게이트웨이에 붙는 단일-코어 클라이언트.
- 시작 화면: Alph-RED ASCII 배너(§13.4, 과거 브랜딩 아트 재활용)
- 입력 → 게이트웨이 `/chat/stream`(SSE) → 작업 과정(tool.started/completed) 실시간 표시 + 답변
- 멀티라인 입력(TextArea): Enter 전송 · Ctrl+J 줄바꿈
- 세션 복원/관리: 대화 화면 기록을 alphred_home 에 저장, 재시작 시 복원 + `/sessions`
- 큐 패널 → `/queue` 폴링(표) + 유휴 시 완료/폐기 알림
- 슬래시 명령: `/` 입력 시 팝업 목록(설명 포함), ↑/↓·Enter/Tab·Esc 조작
"""
from __future__ import annotations

import inspect

import httpx
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Footer, Header, OptionList, RichLog, Static, TextArea
from textual.widgets.option_list import Option

from .splash import banner_lines, logo_lines, pick_banner
from .tui_sessions import SessionStore
from .runtime import resolve_task_id_from_tasks

_BORDER = "#B22232"
_ACCENT = "#E63946"
_AMBER = "#FF9F45"

# (이름, 설명, 인자필요, 핸들러). 팝업·/help·디스패치가 공유.
_COMMANDS = [
    ("help", "사용 가능한 명령 목록", False, "cmd_help"),
    ("model", "모델 보기/전환  (예: /model gemini-2.5-flash [--global])", True, "cmd_model"),
    ("plan", "드라이런 — 실행 전 심화도/계획/비용 견적 미리보기  (예: /plan 보고서 만들어줘)", True, "cmd_plan"),
    ("sessions", "저장된 세션 목록 / 전환  (예: /sessions, /sessions 2)", True, "cmd_sessions"),
    ("clear", "대화 지우기 + 새 세션", False, "cmd_clear"),
    ("new", "대화 지우기 + 새 세션", False, "cmd_clear"),
    ("queue", "큐 조회/관리  (예: /queue cancel <id>, /queue prio <id> 8)", True, "cmd_queue"),
    ("skills", "설치된 스킬 목록", False, "cmd_skills"),
    ("quit", "종료", False, "cmd_quit"),
]


class PromptInput(TextArea):
    """멀티라인 입력 — Enter 전송 / Shift+Enter 줄바꿈(Ctrl+J 는 호환 폴백).

    슬래시 팝업이 떠 있으면 ↑/↓·Tab·Esc·Enter 를 팝업 조작으로 가로챈다.
    """
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
        if event.key in ("shift+enter", "ctrl+j"):       # 줄바꿈(주: Shift+Enter)
            event.stop()
            event.prevent_default()
            self.insert("\n")
        elif event.key == "enter":                       # 전송
            event.stop()
            event.prevent_default()
            app.submit_current()


class QueueTable(DataTable):
    """큐 패널 — 포커스 시 키로 선택 작업 조작(↑/↓ 이동은 기본, 아래 키는 액션)."""
    _ACTIONS = {"enter": "view", "v": "view", "c": "cancel", "d": "cancel", "delete": "cancel",
                "p": "pause", "r": "resume", "plus": "prio_up", "+": "prio_up",
                "minus": "prio_down", "-": "prio_down"}

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.app.set_focus(self.app.query_one("#prompt", PromptInput))
            event.stop()
            return
        action = self._ACTIONS.get(event.key)
        if action:
            self.app.queue_action(action)
            event.stop()


class AlphredTUI(App):
    TITLE = "Alphred"
    SUB_TITLE = "준비됨"
    CSS = f"""
    Screen {{ background: #140707; }}
    #main {{ height: 1fr; }}
    #chat {{ width: 1fr; border: round {_BORDER}; padding: 0 1; background: #1a0a0a; }}
    #queuepanel {{ width: 40; border: round {_BORDER}; }}
    #qtitle {{ color: {_AMBER}; text-style: bold; padding: 0 1; }}
    #queue {{ height: 1fr; }}
    #streaming {{ display: none; height: auto; max-height: 12; padding: 0 1; color: #FFE2D6; }}
    #palette {{ display: none; height: auto; max-height: 10; border: round {_ACCENT};
               background: #1f0d0d; }}
    #prompt {{ border: round {_ACCENT}; height: auto; min-height: 3; max-height: 10;
              background: #1a0a0a; }}
    """
    # ctrl+c 는 Textual 기본(선택 텍스트 복사 / 선택 없으면 종료)에 맡긴다 → 화면 긁어 복사 가능.
    BINDINGS = [("ctrl+q", "quit", "종료"), ("ctrl+l", "clear_chat", "지우기")]

    def __init__(self, base_url: str, api_key: str | None, sessions_dir=None):
        super().__init__()
        import uuid
        self._base = base_url.rstrip("/")
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self.http = httpx.AsyncClient(base_url=self._base, headers=headers, timeout=300.0)
        self.session_id = "alphred-tui-" + uuid.uuid4().hex[:8]
        self.model: str | None = None      # /model 로 전환한 세션 모델(None=config 기본)
        self._busy = False
        self._stream_buf = ""              # assistant.delta 라이브 누적
        self._rows: list[tuple] = []       # 큐 표 행 → (id, priority, state) 매핑
        self._states: dict[str, str] = {}
        self._cmd_map = {n: {"desc": d, "needs_args": a, "handler": h}
                         for (n, d, a, h) in _COMMANDS}
        self._sessions = SessionStore(sessions_dir) if sessions_dir else None
        self._session: dict | None = None  # 현재 세션(영속화 활성 시)
        self._has_convo = False            # 스플래시(빈 화면) 여부 — 리사이즈 재렌더 판단
        self._model_label = "(불러오는 중)"  # 타이틀바용 현재 모델 라벨

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="main"):
            yield RichLog(id="chat", wrap=True, markup=True, highlight=False, auto_scroll=True)
            with Vertical(id="queuepanel"):
                yield Static("◆ 큐", id="qtitle")
                yield QueueTable(id="queue", zebra_stripes=True, cursor_type="row")
        yield Static("", id="streaming")
        yield OptionList(id="palette")
        yield PromptInput(id="prompt", soft_wrap=True)
        yield Footer()

    def on_mount(self) -> None:
        # 모든 큐 작업은 heavy 라 '종류' 열은 생략하고 요청문에 폭을 양보.
        self.query_one("#queue", DataTable).add_columns("ID", "우선", "상태", "요청")
        self.query_one("#chat", RichLog).border_title = "◆ Alphred"
        inp = self.query_one("#prompt", PromptInput)
        inp.border_title = "메시지  ·  Enter 전송  ·  Shift+Enter 줄바꿈  ·  / 명령"
        # 레이아웃이 정착한 뒤 렌더해야 배너가 올바른 폭으로 그려진다(짤림 방지).
        self.call_after_refresh(self._initial_render)
        inp.focus()
        self.set_interval(2.0, self.refresh_queue)
        self.run_worker(self.refresh_queue())
        self.run_worker(self._update_model_display())

    def _initial_render(self) -> None:
        """레이아웃 정착 후 1회: 배너/로고 + (있으면) 직전 세션 복원."""
        self._render_banner()
        if self._sessions:
            latest = self._sessions.latest()
            if latest and latest.get("messages"):
                self._load_session(latest)
                self._log(f"[dim]직전 세션 복원됨: {latest.get('title') or latest['id'][:8]} "
                          f"({len(latest['messages'])}개 메시지) · /sessions 로 전환[/]")
            else:
                self._new_session(announce=False)

    # ---- helpers ----
    def _log(self, markup: str) -> None:
        self.query_one("#chat", RichLog).write(markup)

    def _render_banner(self) -> None:
        """메인화면 출력 패널: 배너 + 로고만 표시(§13.4, 반응형).

        가용 폭에 맞춰 full(ALPHRED-AGENT)/half(ALPHRED)/mini(A) 배너를 고르고,
        세로 여유가 있을 때만 로고를 함께 그린다(짤림 방지).
        """
        chat = self.query_one("#chat", RichLog)
        # 실제 쓰기 가능한 영역 폭(보더/패딩 제외)을 기준으로 변형을 고른다. RichLog 는
        # 쓰기 시점 폭으로 내용을 잘라 저장하므로, 이 메서드는 레이아웃 정착 후에 호출한다.
        region = chat.scrollable_content_region
        w = region.width or max(0, self.size.width - 44)
        h = region.height or max(0, self.size.height - 7)
        _raw, bw = pick_banner(w)
        chat.write("")
        # expand=True: RichLog 기본 min_width(78)로 잘리지 않도록 영역 폭까지 확장.
        for line in banner_lines(w):
            chat.write(line, expand=True)
        if h >= 18:                       # 배너(약8줄)+로고(약10줄)가 들어갈 세로 여유일 때만
            chat.write("")
            for line in logo_lines(bw):
                chat.write(line, expand=True)
        chat.write("")

    def on_resize(self, event=None) -> None:
        # 스플래시(빈 대화)일 때만 현재 크기에 맞춰 배너/로고를 다시 그린다.
        # 대화 중이면 기록이 지워지면 안 되므로 재렌더하지 않는다.
        # 레이아웃 정착 후 영역 폭으로 그려야 하므로 refresh 뒤로 미룬다.
        if self._has_convo or getattr(self, "_busy", False):
            return
        self.call_after_refresh(self._rerender_splash)

    def _rerender_splash(self) -> None:
        if self._has_convo or getattr(self, "_busy", False):
            return
        try:
            chat = self.query_one("#chat", RichLog)
        except Exception:
            return
        chat.clear()
        self._render_banner()

    def _status(self, s: str) -> None:
        self.sub_title = s

    def _session_label(self) -> str:
        """현재 세션의 표시 이름(제목 우선, 없으면 id 앞 8자)."""
        if self._session:
            return self._session.get("title") or self._session.get("id", "")[:8]
        return (self.session_id or "")[:14]

    def _set_titlebar(self) -> None:
        """출력 패널 테두리에 모델 + 현재 세션을 상시 표시."""
        try:
            self.query_one("#chat", RichLog).border_title = (
                f"◆ Alphred  ·  모델: {self._model_label}  ·  세션: {self._session_label()}")
        except Exception:
            pass

    async def _update_model_display(self) -> None:
        """현재 사용 모델 라벨을 갱신하고 타이틀바 재구성(세션 전환 모델 우선)."""
        model, prov = self.model, None
        try:
            d = (await self.http.get("/models/available")).json()
            model = self.model or d.get("current")
            prov = d.get("provider")
        except Exception:
            pass
        label = model or "(config 기본값)"
        if prov:
            label += f"  [{prov}]"
        self._model_label = label
        self._set_titlebar()

    def action_clear_chat(self) -> None:
        self.query_one("#chat", RichLog).clear()

    # ---- 세션 ----
    def _new_session(self, *, announce: bool = True) -> None:
        import uuid
        self.session_id = "alphred-tui-" + uuid.uuid4().hex[:8]
        self._session = self._sessions.new(self.session_id, self.model) if self._sessions else None
        self._has_convo = False
        self.query_one("#chat", RichLog).clear()
        self._render_banner()
        self._set_titlebar()
        if announce:
            self._log("[dim]대화를 지우고 새 세션을 시작했습니다.[/]")

    def _load_session(self, session: dict) -> None:
        self._session = session
        self.session_id = session["id"]
        self.model = session.get("model")
        self._has_convo = bool(session.get("messages"))
        chat = self.query_one("#chat", RichLog)
        chat.clear()
        self._render_banner()
        self._set_titlebar()
        for m in session.get("messages", []):
            if m.get("role") == "user":
                self._log(f"[b #8FD3FF]›[/] {m.get('text', '')}")
            else:
                self._log(f"[b {_ACCENT}]◆ Alphred[/]\n{m.get('text', '')}")

    def _record(self, role: str, text: str) -> None:
        if self._session is None or not text:
            return
        self._session.setdefault("messages", []).append({"role": role, "text": text})
        self._session["model"] = self.model
        if self._sessions:
            self._sessions.save(self._session)

    # ---- 슬래시 팝업 ----
    def _palette(self) -> OptionList:
        return self.query_one("#palette", OptionList)

    def palette_visible(self) -> bool:
        p = self._palette()
        return p.display and p.option_count > 0

    def _refresh_palette(self, prefix: str) -> None:
        p = self._palette()
        p.clear_options()
        seen, uniq = set(), []
        for name, desc, _a, _h in _COMMANDS:
            if name.startswith(prefix) and name not in seen:
                seen.add(name)
                uniq.append((name, desc))
        if not uniq:
            p.display = False
            return
        for name, desc in uniq:
            t = Text()
            t.append(f"/{name}", style=f"bold {_ACCENT}")
            t.append("  ")
            t.append(desc, style="dim")
            p.add_option(Option(t, id=name))
        p.display = True
        p.highlighted = 0

    def palette_hide(self) -> None:
        self._palette().display = False

    def palette_move(self, delta: int) -> None:
        p = self._palette()
        if p.option_count == 0:
            return
        cur = p.highlighted or 0
        p.highlighted = max(0, min(p.option_count - 1, cur + delta))

    def _palette_current(self) -> str | None:
        p = self._palette()
        if p.highlighted is None:
            return None
        try:
            return p.get_option_at_index(p.highlighted).id
        except Exception:
            return None

    def palette_accept(self, *, execute: bool = True) -> None:
        """Enter(execute=True) = 명령 즉시 실행(무인자) · Tab(execute=False) = 인자 입력용 채우기."""
        name = self._palette_current()
        self.palette_hide()
        if not name:
            return
        inp = self.query_one("#prompt", PromptInput)
        if execute:
            inp.text = ""
            self.run_worker(self._dispatch_command(f"/{name}"))
        else:
            inp.text = f"/{name} "
            try:
                inp.move_cursor((0, len(inp.text)))
            except Exception:
                pass

    def on_text_area_changed(self, event) -> None:
        try:
            v = self.query_one("#prompt", PromptInput).text
        except Exception:
            return
        if v.startswith("/") and " " not in v and "\n" not in v:
            self._refresh_palette(v[1:])
        else:
            self.palette_hide()

    def submit_current(self) -> None:
        """입력창 내용을 전송(명령이면 디스패치, 아니면 대화)."""
        inp = self.query_one("#prompt", PromptInput)
        msg = inp.text.strip()
        inp.text = ""
        self.palette_hide()
        if not msg:
            return
        if msg.startswith("/") and "\n" not in msg:
            self.run_worker(self._dispatch_command(msg))
            return
        self._has_convo = True
        self._log(f"[b #8FD3FF]›[/] {msg}")
        self._record("user", msg)
        self._set_titlebar()        # 첫 메시지로 세션 제목이 생기면 타이틀바에 반영
        self.send(msg)

    async def _dispatch_command(self, raw: str) -> None:
        parts = raw[1:].split(maxsplit=1)
        name = parts[0] if parts else ""
        args = parts[1] if len(parts) > 1 else ""
        cmd = self._cmd_map.get(name)
        if not cmd:
            self._log(f"[red]알 수 없는 명령: /{name}[/]  ([dim]/help 로 목록 확인[/])")
            return
        res = getattr(self, cmd["handler"])(args)
        if inspect.iscoroutine(res):
            await res

    # ---- 명령 핸들러 ----
    def cmd_help(self, args: str) -> None:
        self._log("[b #FF9F45]사용 가능한 명령[/]")
        for name, desc, _a, _h in _COMMANDS:
            self._log(f"  [{_ACCENT}]/{name}[/]  [dim]{desc}[/]")
        self._log("  [dim]전체 Hermes 명령(/browser, /skills install, /mcp …)은 `alphred chat` 에서.[/]")

    def cmd_clear(self, args: str) -> None:
        self._new_session()

    async def cmd_sessions(self, args: str) -> None:
        if not self._sessions:
            self._log("[dim]세션 영속화가 비활성입니다(임시 모드).[/]")
            return
        args = args.strip()
        items = self._sessions.list()
        if not args:
            if not items:
                self._log("[dim]저장된 세션이 없습니다.[/]")
                return
            self._log(f"[b #FF9F45]저장된 세션 {len(items)}개[/]  ([dim]/sessions <번호> 로 전환[/])")
            for i, s in enumerate(items[:20], 1):
                cur = " [#7BC96F](현재)[/]" if s.get("id") == self.session_id else ""
                title = s.get("title") or "(제목 없음)"
                self._log(f"  [{_ACCENT}]{i:>2}[/]. {title}  "
                          f"[dim]{s.get('model') or ''} · {len(s.get('messages', []))}개 메시지 · "
                          f"{(s.get('updated') or '')[:16]}[/]{cur}")
            return
        if args.isdigit():
            idx = int(args) - 1
            if 0 <= idx < len(items):
                self._load_session(items[idx])
                self._log(f"[#7BC96F]세션 전환: {items[idx].get('title') or items[idx]['id'][:8]}[/]")
            else:
                self._log("[red]범위를 벗어난 번호입니다.[/]")
            return
        self._log("[dim]사용법: /sessions  또는  /sessions <번호>[/]")

    async def cmd_queue(self, args: str) -> None:
        args = args.strip()
        parts = args.split()
        sub = parts[0] if parts else "list"
        if sub in ("", "list"):
            await self.refresh_queue()
            self._log("[dim]큐 새로고침됨[/]")
            return
        if sub in ("cancel", "discard", "pause", "resume", "retry", "prio") and len(parts) >= 2:
            full = await self._resolve_task_id(parts[1])
            if not full:
                self._log(f"[red]작업을 찾을 수 없음: {parts[1]}[/]")
                return
            try:
                if sub in ("cancel", "discard"):
                    await self.http.request("DELETE", f"/queue/{full}")
                    self._log(f"[#FF6B4A]폐기됨: {full[:8]}[/]")
                elif sub == "pause":
                    await self.http.post(f"/queue/{full}/pause")
                    self._log(f"[#FFB454]일시중지: {full[:8]}[/]")
                elif sub == "resume":
                    await self.http.post(f"/queue/{full}/resume")
                    self._log(f"[#7BC96F]재개 허용: {full[:8]}[/]")
                elif sub == "retry":
                    await self.http.post(f"/queue/{full}/retry")
                    self._log(f"[#7BC96F]재시도 대기열 등록: {full[:8]}[/]")
                elif sub == "prio":
                    if len(parts) < 3 or not parts[2].lstrip("-").isdigit():
                        self._log("[red]사용법: /queue prio <id> <1-10>[/]")
                        return
                    await self.http.post(f"/queue/{full}/prio", json={"priority": int(parts[2])})
                    self._log(f"[#7BC96F]우선순위 변경: {full[:8]} → {parts[2]}[/]")
            except Exception as e:
                self._log(f"[red]실패: {e}[/]")
            await self.refresh_queue()
            return
        self._log("[dim]사용법: /queue [list | cancel <id> | pause <id> | resume <id> | retry <id> | prio <id> <1-10>][/]")

    # ---- 큐 키보드 조작 ----
    def queue_action(self, action: str) -> None:
        table = self.query_one("#queue", DataTable)
        idx = table.cursor_row
        if idx is None or idx < 0 or idx >= len(self._rows):
            return
        tid, prio, _state = self._rows[idx]
        self.run_worker(self._do_queue_action(action, tid, prio))

    async def _do_queue_action(self, action: str, tid: str, prio: int) -> None:
        try:
            if action == "view":
                d = (await self.http.get(f"/queue/{tid}")).json()
                est = d.get("estimate") or {}
                self._log(f"[b #FF9F45]작업 {tid[:8]}[/]  상태={d.get('state')} "
                          f"우선={d.get('priority')} 종류={d.get('kind')}"
                          + (f" 심화도={d['depth']}" if d.get("depth") else "")
                          + (f" · 견적 ~{est['est_llm_calls']}콜" if est.get("est_llm_calls") else "")
                          + (f" · 재시도 {d['verify_attempts']}회" if d.get("verify_attempts") else ""))
                self._log(f"[dim]분류근거:[/] {d.get('classify_reason') or '-'}")
                self._log(f"[dim]요청:[/] {(d.get('prompt') or '')[:300]}")
                prog = d.get("plan_progress") or 0
                act = d.get("plan_activity")
                if d.get("state") == "In-Progress":
                    self._log(f"[#8FD3FF]진행: 완료 도구 {prog}개"
                              + (f" · 현재 🔧 {act}" if act else "") + "[/]")
                plan = d.get("plan") or {}
                subs = plan.get("subtasks") if isinstance(plan, dict) else None
                if subs:
                    self._log("[b]하위작업 계획:[/]")
                    seen_tools = 0
                    completed = d.get("state") == "Completed"
                    for i, s in enumerate(subs, 1):
                        has_tool = bool(s.get("tools"))
                        if completed:
                            mark = "[#7BC96F]✓[/]"
                        elif has_tool and seen_tools < prog:
                            mark = "[#7BC96F]✓[/]"
                        elif has_tool and seen_tools == prog:
                            mark = "[#8FD3FF]▶[/]"
                        else:
                            mark = "[dim]○[/]"
                        if has_tool:
                            seen_tools += 1
                        tools = ",".join(s.get("tools") or [])
                        self._log(f"  {mark} [dim]{i}.[/] {s.get('title', '')} "
                                  f"[dim]({s.get('kind', '')}/{s.get('effort', '')}"
                                  f"{(' · ' + tools) if tools else ''})[/]")
                if d.get("result"):
                    self._log(f"[b]결과:[/]\n{d['result'][:2000]}")
                if d.get("error"):
                    self._log(f"[red]에러:[/] {d['error'][:400]}")
                self._render_verify_report(d.get("verify_report"))
                evs = d.get("events") or []
                if evs:
                    self._log("[dim]이력: " + " → ".join(e.get("to_state", "") for e in evs[-6:]) + "[/]")
                return
            if action == "cancel":
                await self.http.request("DELETE", f"/queue/{tid}")
                self._log(f"[#FF6B4A]폐기됨: {tid[:8]}[/]")
            elif action == "pause":
                await self.http.post(f"/queue/{tid}/pause")
                self._log(f"[#FFB454]일시중지: {tid[:8]}[/]")
            elif action == "resume":
                await self.http.post(f"/queue/{tid}/resume")
                self._log(f"[#7BC96F]재개 허용: {tid[:8]}[/]")
            elif action in ("prio_up", "prio_down"):
                newp = max(1, min(10, (prio or 5) + (1 if action == "prio_up" else -1)))
                await self.http.post(f"/queue/{tid}/prio", json={"priority": newp})
                self._log(f"[#7BC96F]우선순위: {tid[:8]} → {newp}[/]")
            await self.refresh_queue()
        except Exception as e:
            self._log(f"[red]큐 조작 실패: {e}[/]")

    def _render_verify_report(self, rep) -> None:
        """검증 증거 패널(§21) — 무엇을 어떻게 검증했는지 표시(Tier0 결정적 + Tier2 judge)."""
        if not isinstance(rep, dict):
            return
        icon = "[#7BC96F]✓[/]" if rep.get("passed") else "[#FFB454]⚠[/]"
        self._log(f"[b]검증(Tier0):[/] {icon} {rep.get('summary', '')}")
        for c in (rep.get("checks") or []):
            ok = "[#7BC96F]OK[/]" if c.get("ok") else "[#FF6B4A]실패[/]"
            kind = c.get("check", "")
            target = c.get("target") or c.get("path") or ""   # 신/구 스키마 모두 허용
            detail = c.get("detail") or c.get("reason") or ""
            self._log(f"  {ok} [dim]{kind}:[/] [dim]{target}[/]"
                      + (f"  [dim]— {detail}[/]" if detail else ""))
        j = rep.get("judge")
        if isinstance(j, dict):
            jicon = "[#7BC96F]✓[/]" if j.get("passed") else "[#FF6B4A]✗[/]"
            self._log(f"[b]수용 judge(Tier2):[/] {jicon} 점수 {j.get('score', '-')} "
                      f"[dim]{j.get('summary', '')}[/]")
            for c in (j.get("criteria") or []):
                m = "[#7BC96F]✓[/]" if c.get("met") else "[#FF6B4A]✗[/]"
                self._log(f"  {m} [dim]{c.get('name', '')}"
                          + (f" — {c.get('note')}" if c.get("note") else "") + "[/]")
            for u in (j.get("unmet") or []):
                self._log(f"  [#FFB454]· 미흡:[/] [dim]{u}[/]")
        if rep.get("suggestion"):
            self._log(f"[#8FD3FF]제안:[/] {rep['suggestion']}")

    async def _resolve_task_id(self, prefix: str) -> str | None:
        try:
            tasks = (await self.http.get("/queue")).json().get("tasks", [])
        except Exception:
            return None
        return resolve_task_id_from_tasks(tasks, prefix)

    def cmd_quit(self, args: str) -> None:
        self.run_worker(self._quit())

    async def _quit(self) -> None:
        try:
            await self.http.aclose()
        except Exception:
            pass
        self.exit()

    async def cmd_model(self, args: str) -> None:
        args = args.strip()
        if not args:
            try:
                d = (await self.http.get("/models/available")).json()
            except Exception as e:
                self._log(f"[red]모델 목록 조회 실패: {e}[/]")
                return
            self._log(f"[b #FF9F45]현재 모델[/]: {self.model or d.get('current') or '(config 기본값)'}")
            models = d.get("models") or []
            if models:
                prov = d.get("provider") or ""
                self._log(f"[dim]{prov} 사용 가능 ({len(models)}개):[/]")
                self._log("  " + ", ".join(f"[#E63946]{m}[/]" for m in models[:60]))
                if len(models) > 60:
                    self._log(f"  [dim]… 외 {len(models) - 60}개[/]")
            else:
                self._log("[dim]목록을 가져오지 못했습니다(provider 미설정?). /model <이름> 으로 직접 전환하세요.[/]")
            self._log("[dim]전환: /model <이름> [--global][/]")
            return
        glob = "--global" in args
        name = args.replace("--global", "").strip()
        if not name:
            self._log("[red]모델 이름이 필요합니다: /model <이름>[/]")
            return
        self.model = name
        self._new_session(announce=False)  # 새 세션에 새 모델 적용(세션 모델은 생성 시 고정)
        await self._update_model_display()
        self._log(f"[#7BC96F]모델 전환: {name}[/] (이번 대화). 다음 메시지부터 적용됩니다.")
        if glob:
            ok = self._set_global_model(name)
            self._log(f"[dim]config 기본값 {'변경됨' if ok else '변경 실패(수동 편집 필요)'}[/]")

    async def cmd_plan(self, args: str) -> None:
        """드라이런(§21 V3) — 실행 전 심화도/계획/비용 견적 미리보기."""
        msg = args.strip()
        if not msg:
            self._log("[dim]사용법: /plan <요청>  — 실행하지 않고 분류·계획·견적만 봅니다.[/]")
            return
        try:
            d = (await self.http.post("/plan", json={"message": msg})).json()
        except Exception as e:
            self._log(f"[red]드라이런 실패: {e}[/]")
            return
        depth = d.get("depth", "?")
        dcolor = {"low": "#7BC96F", "mid": "#8FD3FF", "high": "#FFB454"}.get(depth, "#FFE2D6")
        est = d.get("estimate") or {}
        self._log(f"[b #FF9F45]드라이런:[/] 종류={d.get('kind')} · "
                  f"심화도=[{dcolor}]{depth}[/] · 우선={d.get('priority')}")
        self._log(f"[dim]근거:[/] {d.get('classify_reason') or '-'}")
        self._log(f"[dim]비용 견적(러프):[/] 단계 {est.get('steps', '?')}개 · "
                  f"도구 {est.get('tool_steps', 0)}개 · 추정 LLM 호출 ~{est.get('est_llm_calls', '?')}회 · "
                  f"부담 {est.get('band', '?')}")
        subs = (d.get("plan") or {}).get("subtasks") if isinstance(d.get("plan"), dict) else None
        if subs:
            self._log("[b]예상 하위작업:[/]")
            for i, s in enumerate(subs, 1):
                tools = ",".join(s.get("tools") or [])
                self._log(f"  [dim]{i}.[/] {s.get('title', '')} "
                          f"[dim]({s.get('kind', '')}/{s.get('effort', '')}"
                          f"{(' · ' + tools) if tools else ''})[/]")
        self._log("[dim]그대로 보내려면 메시지를 입력하세요.[/]")

    async def cmd_skills(self, args: str) -> None:
        try:
            data = (await self.http.get("/v1/skills")).json()
        except Exception as e:
            self._log(f"[red]스킬 목록 조회 실패: {e}[/]")
            return
        if isinstance(data, dict) and data.get("error") and not data.get("data"):
            self._log(f"[red]스킬 목록 조회 실패(Hermes 미가동?): {data['error']}[/]")
            return
        items = data.get("data") or data.get("skills") or (data if isinstance(data, list) else [])
        self._log(f"[b #FF9F45]설치된 스킬 {len(items)}개[/]  "
                  f"[dim](에이전트가 작업 중 자동으로 활용 — skill_view 로 로드)[/]")
        for s in items[:60]:
            if not isinstance(s, dict):
                self._log(f"  [{_ACCENT}]{s}[/]")
                continue
            nm = s.get("name") or s.get("id") or "?"
            cat = s.get("category") or ""
            desc = (s.get("description") or "").replace("\n", " ")[:60]
            self._log(f"  [{_ACCENT}]{nm}[/]"
                      + (f" [dim #8FD3FF]{cat}[/]" if cat else "")
                      + (f"  [dim]{desc}[/]" if desc else ""))
        if len(items) > 60:
            self._log(f"  [dim]… 외 {len(items) - 60}개[/]")

    def _set_global_model(self, name: str) -> bool:
        """config.yaml 의 model.default 라인을 교체(--global). best-effort."""
        try:
            import re
            from .config import Config
            p = Config.load().hermes_home / "config.yaml"
            lines = p.read_text(encoding="utf-8").splitlines(keepends=True)
            in_model, done, out = False, False, []
            for ln in lines:
                if ln[:1] not in (" ", "\t") and ":" in ln:
                    in_model = ln.startswith("model:")
                if in_model and not done:
                    m = re.match(r"^(\s+)default:\s*.*$", ln)
                    if m:
                        out.append(f"{m.group(1)}default: {name}\n")
                        done = True
                        continue
                out.append(ln)
            if done:
                p.write_text("".join(out), encoding="utf-8")
            return done
        except Exception:
            return False

    # ---- 채팅(SSE) ----
    @work(exclusive=False)
    async def send(self, msg: str) -> None:
        import json
        self._busy = True
        self._status("처리 중…")
        try:
            async with self.http.stream("POST", "/chat/stream",
                                        json={"message": msg, "session_id": self.session_id,
                                              "model": self.model}) as resp:
                if resp.status_code != 200:
                    self._log(f"[red]오류 HTTP {resp.status_code}[/]")
                    return
                event, data_lines = None, []
                async for line in resp.aiter_lines():
                    if line.startswith(":"):
                        continue
                    if line.startswith("event:"):
                        event = line[6:].strip()
                    elif line.startswith("data:"):
                        data_lines.append(line[5:].strip())
                    elif line == "" and event:
                        try:
                            payload = json.loads("".join(data_lines)) if data_lines else {}
                        except Exception:
                            payload = {}
                        self._handle_event(event, payload)
                        event, data_lines = None, []
        except Exception as e:
            self._log(f"[red]연결 오류: {e}[/]  (alphred 서비스가 준비 중일 수 있습니다)")
        finally:
            self._busy = False
            self._status("준비됨")

    def _stream_widget(self) -> Static:
        return self.query_one("#streaming", Static)

    def _clear_stream(self) -> None:
        self._stream_buf = ""
        w = self._stream_widget()
        w.update("")
        w.display = False

    def _handle_event(self, event: str, d: dict) -> None:
        if event == "queued":
            self._clear_stream()
            self._log(f"[{_AMBER}]⏳ 무거운 작업으로 백그라운드 큐에 등록됨 "
                      f"(id={(d.get('id') or '')[:8]}). 완료되면 알려드립니다.[/]")
            self.call_after_refresh(self.refresh_queue)
        elif event == "tool.started":
            self._log(f"[dim]  ┊ 🔧 {d.get('tool_name', 'tool')}…[/]")
        elif event == "tool.completed":
            pv = (d.get("preview") or "").replace("\n", " ")[:64]
            self._log(f"[dim]  ┊ ✓ {d.get('tool_name', 'tool')}{(' — ' + pv) if pv else ''}[/]")
        elif event == "tool.failed":
            self._log(f"[#FF6B4A]  ┊ ✗ {d.get('tool_name', 'tool')} 실패[/]")
        elif event == "tool.progress":
            tn = d.get("tool_name", "")
            if tn:
                self._status(f"🧠 {tn}")
        elif event == "assistant.delta":            # 라이브 토큰 누적 표시
            self._stream_buf += d.get("delta", "")
            w = self._stream_widget()
            w.display = True
            w.update(Text.assemble(("◆ Alphred\n", f"bold {_ACCENT}"), (self._stream_buf, "")))
        elif event == "assistant.completed":
            txt = d.get("content") or self._stream_buf
            self._clear_stream()
            if txt:
                self._log(f"[b {_ACCENT}]◆ Alphred[/]\n{txt}")
                self._record("assistant", txt)
        elif event == "error":
            self._clear_stream()
            self._log(f"[red]오류: {(d.get('message') or '')[:200]}[/]")

    # ---- 큐 패널 ----
    async def refresh_queue(self) -> None:
        try:
            tasks = (await self.http.get("/queue")).json().get("tasks", [])
        except Exception:
            self._status("서비스 연결 대기 중…")
            return
        from .queue_manager import result_needs_attention
        table = self.query_one("#queue", DataTable)
        prev_cursor = table.cursor_row
        table.clear()
        self._rows = []
        active = [t for t in tasks if t["state"] in ("Pending", "In-Progress", "Paused")]
        active.sort(key=lambda t: -int(t.get("priority") or 0))   # 우선순위 높은 순
        done = [t for t in tasks if t["state"] in ("Completed", "NeedsReview", "Discarded")]
        done.sort(key=lambda t: t.get("created_at") or "", reverse=True)
        for t in active + done[:8]:
            prompt = (t.get("prompt") or "").replace("\n", " ").strip()[:18]
            label = _state_label(t["state"])
            if t["state"] == "In-Progress" and (t.get("plan_progress") or 0):
                label = f"[#8FD3FF]진행 {t['plan_progress']}⚙[/]"  # 완료 도구 수(라이브)
            elif t["state"] == "Completed" and result_needs_attention(t.get("result")):
                label = "[#FFB454]완료⚠[/]"   # 산출물 없이 되묻거나 실패한 완료
            table.add_row(t["id"][:8], str(t.get("priority", "")), label, prompt)
            self._rows.append((t["id"], int(t.get("priority") or 0), t["state"]))
        if self._rows and prev_cursor is not None:        # 네비 중 커서 위치 유지
            table.move_cursor(row=min(prev_cursor, len(self._rows) - 1))
        for t in tasks:
            prev = self._states.get(t["id"])
            cur = t["state"]
            if (prev and prev != cur and not self._busy
                    and cur in ("Completed", "NeedsReview", "Discarded")):
                if cur == "Completed":
                    res = (t.get("result") or "").replace("\n", " ")[:140]
                    if result_needs_attention(t.get("result")):
                        self._log(f"[b #FFB454]⚠ 작업 {t['id'][:8]} 완료(확인 필요)[/] {res}")
                    else:
                        self._log(f"[b #7BC96F]✓ 작업 {t['id'][:8]} 완료[/] {res}")
                elif cur == "NeedsReview":
                    self._log(
                        f"[b #FFB454]⚠ 작업 {t['id'][:8]} 검토 필요 — 검증 미통과:[/] "
                        f"{self._verify_summary(t)}  [dim](Enter 로 상세 확인)[/]")
                else:
                    self._log(f"[b #FF6B4A]✗ 작업 {t['id'][:8]} 폐기[/] {(t.get('error') or '')[:100]}")
            self._states[t["id"]] = cur

    @staticmethod
    def _verify_summary(t: dict) -> str:
        rep = t.get("verify_report") or {}
        return rep.get("summary") if isinstance(rep, dict) else ""


def _state_label(s: str) -> str:
    return {"Pending": "[#FFB454]대기[/]", "In-Progress": "[#8FD3FF]진행[/]",
            "Paused": "[#FFB454]보류[/]", "Completed": "[#7BC96F]완료[/]",
            "NeedsReview": "[#FFB454]검토필요[/]",
            "Discarded": "[#FF6B4A]폐기[/]"}.get(s, s)


def run_tui(base_url: str, api_key: str | None, sessions_dir=None) -> None:
    # mouse=False: Textual 가 마우스를 캡처하지 않아 터미널 네이티브 텍스트 선택(긁기)·
    # 복사가 그대로 동작한다(큐 패널은 키보드 Tab/↑↓ 로 조작). 마우스 휠 스크롤은 포기.
    AlphredTUI(base_url, api_key, sessions_dir).run(mouse=False)
