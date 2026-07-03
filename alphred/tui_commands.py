"""CommandsMixin — 슬래시 팝업 · 입력 히스토리 · 명령 디스패치 · 단순 명령 핸들러.

`AlphredTUI` 에 믹스인된다(자체 __init__ 없음; 상태는 AlphredTUI.__init__ 에서 초기화).
교차 참조(self.send·self.refresh_queue·self._new_session 등)는 결합 클래스에서 런타임 해결.
"""
from __future__ import annotations

import inspect

from rich.text import Text
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from .tui_base import (PromptInput, _ACCENT, _AMBER, _COMMANDS, _ERR, _INFO, _OK,
                       _SOFT, _WARN, _short_sid, fuzzy_match)


class SessionPicker(ModalScreen):
    """세션 피커 모달(§36 I5) — ↑↓ 선택 · Enter 전환 · d 삭제 · Esc 닫기.

    결과는 dismiss(("switch"|"delete", session)) 또는 dismiss(None).
    """
    BINDINGS = [("escape", "close", "닫기"), ("d", "delete", "삭제")]
    DEFAULT_CSS = f"""
    SessionPicker {{ align: center middle; }}
    #sp-box {{ width: 80; max-width: 90%; height: auto; max-height: 80%;
              border: round {_ACCENT}; padding: 1 2; }}
    #sp-box OptionList {{ height: auto; max-height: 20; }}
    """

    def __init__(self, items: list[dict], current_id: str):
        super().__init__()
        self._items = items
        opts = []
        for i, s in enumerate(items):
            t = Text(f"{_short_sid(s.get('id', '')):<10} {s.get('title') or '(제목 없음)'}")
            meta = (f"  {s.get('model') or ''} · {len(s.get('messages') or [])}개 메시지 · "
                    f"{(s.get('updated') or '')[:16]}")
            if s.get("id") == current_id:
                meta += " · 현재"
            t.append(meta, style="dim")
            opts.append(Option(t, id=str(i)))
        self._ol = OptionList(*opts)   # 마운트 타이밍과 무관하게 생성 시점에 채운다

    def compose(self):
        with Vertical(id="sp-box"):
            yield Static(f"[b {_AMBER}]저장된 세션 {len(self._items)}개[/]  "
                         f"[dim]Enter 전환 · d 삭제 · Esc 닫기[/]")
            yield self._ol

    def on_mount(self) -> None:
        if self._items:
            self._ol.highlighted = 0
        self._ol.focus()

    def on_option_list_option_selected(self, event) -> None:
        event.stop()
        self.dismiss(("switch", self._items[int(event.option.id)]))

    def action_delete(self) -> None:
        if self._ol.highlighted is not None and self._items:
            self.dismiss(("delete", self._items[self._ol.highlighted]))

    def action_close(self) -> None:
        self.dismiss(None)


class CommandsMixin:
    _TIERS = ("high", "mid", "low")

    # ---- 슬래시 팝업 ----
    def _palette(self) -> OptionList:
        return self.query_one("#palette", OptionList)

    def palette_visible(self) -> bool:
        p = self._palette()
        return p.display and p.option_count > 0

    def _refresh_palette(self, prefix: str) -> None:
        """명령 매칭(§36 I4) — prefix 일치 우선, 그 뒤 fuzzy(부분열) 일치."""
        p = self._palette()
        p.clear_options()
        seen, pri, fz = set(), [], []
        for name, desc, _a, _h in _COMMANDS:
            if name in seen:
                continue
            seen.add(name)
            if name.startswith(prefix):
                pri.append((name, desc))
            elif prefix and fuzzy_match(prefix, name):
                fz.append((name, desc))
        uniq = pri + fz
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

    # ---- 인자 2차 완성(§36 I4) — 동기 캐시만 사용(모델 목록·세션·큐 행) ----
    _DEPTHS = ("auto", "low", "mid", "high")
    _QUEUE_SUBS = ("list", "ask", "cancel", "purge", "clear", "pause", "resume",
                   "retry", "prio")
    _QUEUE_ID_SUBS = ("cancel", "purge", "pause", "resume", "retry", "prio")

    def _arg_candidates(self, cmd: str, arg: str) -> list[tuple[str, str]]:
        """(표시 라벨, 완성된 입력줄) 후보 — 없는 명령/후보면 빈 목록."""
        out: list[tuple[str, str]] = []
        if cmd == "depth":
            out = [(d, f"/depth {d}") for d in self._DEPTHS if d.startswith(arg.lower())]
        elif cmd == "model":
            names = [m for m in self._model_names if not arg or fuzzy_match(arg, m)]
            out = [(m, f"/model {m}") for m in names]
        elif cmd == "sessions" and self._sessions:
            for i, s in enumerate(self._sessions.list()[:12], 1):
                label = f"{i}. {_short_sid(s.get('id', ''))}  {s.get('title') or '(제목 없음)'}"
                if not arg or fuzzy_match(arg, label):
                    out.append((label, f"/sessions {i}"))
        elif cmd == "queue":
            parts = arg.split()
            if not parts or (len(parts) == 1 and not arg.endswith(" ")):
                q = parts[0] if parts else ""
                out = [(s, f"/queue {s}") for s in self._QUEUE_SUBS if s.startswith(q)]
            elif parts[0] in self._QUEUE_ID_SUBS:
                idq = parts[1] if len(parts) > 1 else ""
                for tid, _prio, state in self._rows:
                    if tid[:8].startswith(idq):
                        out.append((f"{tid[:8]}  ({state})", f"/queue {parts[0]} {tid[:8]}"))
        return out[:8]

    def _refresh_arg_palette(self, cmd: str, arg: str) -> None:
        p = self._palette()
        p.clear_options()
        cands = self._arg_candidates(cmd, arg) if cmd in self._cmd_map else []
        if not cands:
            p.display = False
            return
        for label, insert in cands:
            t = Text()
            t.append(label, style=_SOFT)
            p.add_option(Option(t, id="arg:" + insert))
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
        """Enter(execute=True) = 즉시 실행 · Tab(execute=False) = 입력줄 채우기.

        명령 완성(id=이름)과 인자 완성(id="arg:<입력줄>", §36 I4)을 모두 처리한다.
        """
        oid = self._palette_current()
        self.palette_hide()
        if not oid:
            return
        inp = self.query_one("#prompt", PromptInput)
        if oid.startswith("arg:"):                   # 인자 완성 — 완성된 입력줄
            line = oid[4:]
            if execute:
                inp.text = ""
                self.run_worker(self._dispatch_command(line))
            else:
                self._set_prompt_text(line + (" " if not line.endswith(" ") else ""))
            return
        if execute:
            inp.text = ""
            self.run_worker(self._dispatch_command(f"/{oid}"))
        else:
            self._set_prompt_text(f"/{oid} ")

    def on_text_area_changed(self, event) -> None:
        try:
            v = self.query_one("#prompt", PromptInput).text
        except Exception:
            return
        if v.startswith("/") and "\n" not in v:
            if " " not in v:
                self._refresh_palette(v[1:])
            else:                                    # "/cmd arg…" → 인자 완성(§36 I4)
                cmd, _, arg = v[1:].partition(" ")
                self._refresh_arg_palette(cmd, arg)
        else:
            self.palette_hide()

    # ---- 입력 히스토리(↑/↓) ----
    def _set_prompt_text(self, text: str) -> None:
        inp = self.query_one("#prompt", PromptInput)
        inp.text = text
        try:
            inp.move_cursor(inp.document.end)
        except Exception:
            pass
        self.palette_hide()

    def history_prev(self) -> bool:
        """이전 입력으로 — 탐색했으면 True(키 소비), 히스토리 없으면 False(기본 커서이동)."""
        if not self._history:
            return False
        if self._hist_idx is None:
            self._hist_draft = self.query_one("#prompt", PromptInput).text
            self._hist_idx = len(self._history)
        if self._hist_idx > 0:
            self._hist_idx -= 1
            self._set_prompt_text(self._history[self._hist_idx])
        return True

    def history_next(self) -> bool:
        """다음 입력으로 — 끝을 넘으면 작성 중이던 초안 복원. 탐색 중이 아니면 False."""
        if self._hist_idx is None:
            return False
        self._hist_idx += 1
        if self._hist_idx >= len(self._history):
            self._hist_idx = None
            self._set_prompt_text(self._hist_draft)
        else:
            self._set_prompt_text(self._history[self._hist_idx])
        return True

    def submit_current(self) -> None:
        """입력창 내용을 전송(명령이면 디스패치, 아니면 대화)."""
        inp = self.query_one("#prompt", PromptInput)
        msg = inp.text.strip()
        inp.text = ""
        self.palette_hide()
        # §34.4 답변 모드 — 입력을 인테이크 답변으로 소비(빈 Enter=추천 채택). 명령은 통과.
        if self._pending_input is not None and not msg.startswith("/"):
            self.answer_submit(msg)
            return
        if not msg:
            return
        if msg != (self._history[-1] if self._history else None):  # 연속 중복 제외
            self._history.append(msg)
        self._hist_idx = None
        if msg.startswith("/") and "\n" not in msg:
            self.run_worker(self._dispatch_command(msg))
            return
        self._has_convo = True
        self._log(f"[b {_INFO}]›[/] {msg}")
        self._record("user", msg)
        self._set_titlebar()        # 첫 메시지로 세션 제목이 생기면 타이틀바에 반영
        if self._busy:              # §36 I2 — 응답 중 제출은 대기열로(완료 후 자동 전송)
            self._pending_msgs.append(msg)
            self._log("[dim]  ⧗ 현재 응답이 끝나면 자동 전송됩니다. (Esc=현재 응답 중단)[/]")
            return
        self._send_worker = self.send(msg)

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
        self._log(f"[b {_AMBER}]사용 가능한 명령[/]")
        for name, desc, _a, _h in _COMMANDS:
            self._log(f"  [{_ACCENT}]/{name}[/]  [dim]{desc}[/]")
        self._log("  [dim]키: Esc 응답 중단 · Ctrl+T 큐 덱 · Shift+Tab 심화도 · Ctrl+O 상세 · "
                  "Ctrl+Y 답변 복사 · PgUp/PgDn 스크롤 · 응답 중 제출 = 대기 후 자동 전송[/]")
        self._log("  [dim]전체 Hermes 명령(/browser, /skills install, /mcp …)은 `hermes` 를 직접 실행.[/]")

    def cmd_clear(self, args: str) -> None:
        """화면 출력만 지운다 — 현재 세션과 대화 맥락은 그대로 유지(깔끔하게 보고 싶을 때)."""
        self.action_clear_chat()
        self._log("[dim]화면을 비웠습니다 (세션 유지 — 대화 맥락은 그대로). 새 세션은 /new[/]")

    def cmd_export(self, args: str) -> None:
        """현재 세션 대화를 Markdown 파일로 저장(§36 I8) — 인자=경로(생략 시 자동 이름)."""
        from datetime import datetime
        from pathlib import Path
        msgs = (self._session or {}).get("messages") or []
        if not msgs:
            self._log("[dim]내보낼 대화가 없습니다.[/]")
            return
        title = (self._session or {}).get("title") or "Alphred 세션"
        lines = [f"# {title}", "",
                 f"- 세션: `{_short_sid(self.session_id)}`",
                 f"- 모델: {self.model or '(config 기본값)'}",
                 f"- 내보낸 시각: {datetime.now().isoformat(timespec='seconds')}", ""]
        for m in msgs:
            who = "🧑 You" if m.get("role") == "user" else "◆ Alphred"
            lines.append(f"## {who}\n\n{(m.get('text') or '').rstrip()}\n")
        target = (args or "").strip()
        if not target:
            target = f"alphred-{_short_sid(self.session_id)}-{datetime.now():%Y%m%d-%H%M%S}.md"
        try:
            p = Path(target).expanduser().resolve()
            p.write_text("\n".join(lines), encoding="utf-8")
            self._log(f"[{_OK}]💾 세션을 저장했습니다: {p}  ({len(msgs)}개 메시지)[/]")
        except Exception as e:
            self._log(f"[red]내보내기 실패: {e}[/]")

    def cmd_banner(self, args: str) -> None:
        """Alph-RED 전체 배너/로고 아트(§13.4)를 표시 — 웰컴 패널에서 분리 보존(§36 D2)."""
        from .splash import banner_lines, logo_lines, pick_banner
        chat = self._chat()
        w = chat.scrollable_content_region.width or max(0, self.size.width - 58)
        _raw, bw = pick_banner(w)
        group = Text("\n")
        for line in banner_lines(w):
            group.append_text(line)
            group.append("\n")
        logo = logo_lines(bw, avail_height=max(0, chat.scrollable_content_region.height - 10))
        if logo:
            group.append("\n")
            for line in logo:
                group.append_text(line)
                group.append("\n")
        self._chat_add(Static(group))

    def cmd_new(self, args: str) -> None:
        """새 세션을 시작한다(대화 비우기)."""
        self._new_session()

    def cmd_depth(self, args: str) -> None:
        """작업 심화도(low/mid/high) 고정 또는 자동 판정으로 해제.

        고정 시 이후 제출되는 Heavy 작업의 검증/재시도 강도를 강제한다(자동 분류를 덮어씀).
        """
        arg = (args or "").strip().lower()
        if not arg:
            cur = self.depth_override or "auto (자동 판정)"
            self._log(f"[{_INFO}]현재 작업 심화도: {cur}[/]  "
                      "([dim]/depth low|mid|high 고정 · /depth auto 해제[/])")
            return
        if arg in ("auto", "off", "reset"):
            self.depth_override = None
            self._log(f"[{_OK}]작업 심화도: 자동 판정으로 전환[/]")
            return
        if arg in ("low", "mid", "high"):
            self.depth_override = arg
            self._log(f"[{_OK}]작업 심화도 고정: [b]{arg}[/] (이후 Heavy 작업에 적용)[/]")
            return
        self._log("[red]사용법: /depth low|mid|high  또는  /depth auto[/]")

    @staticmethod
    def _resolve_session(items: list[dict], token: str) -> dict | None:
        """세션을 번호(1-base) 또는 ID(단축/전체 prefix)로 찾는다. 큐 작업 ID 해석과 동일 규칙."""
        token = (token or "").strip()
        if not token:
            return None
        if token.isdigit():
            idx = int(token) - 1
            return items[idx] if 0 <= idx < len(items) else None
        for s in items:
            sid = s.get("id", "")
            if _short_sid(sid).startswith(token) or sid.startswith(token):
                return s
        return None

    async def cmd_sessions(self, args: str) -> None:
        if not self._sessions:
            self._log("[dim]세션 영속화가 비활성입니다(임시 모드).[/]")
            return
        args = args.strip()
        items = self._sessions.list()
        if not args:                                   # §36 I5 — 인터랙티브 피커 모달
            if not items:
                self._log("[dim]저장된 세션이 없습니다.[/]")
                return
            self.push_screen(SessionPicker(items, self.session_id), self._session_picked)
            return
        parts = args.split()
        if parts[0] in ("delete", "rm", "del"):
            target = self._resolve_session(items, parts[1]) if len(parts) >= 2 else None
            if target is None:
                self._log("[red]세션을 찾을 수 없습니다(번호 또는 ID 확인).[/]")
                return
            await self._delete_session(target)
            return
        target = self._resolve_session(items, args)
        if target is not None:
            self._load_session(target)
            self._log(f"[{_OK}]세션 전환: {target.get('title') or _short_sid(target['id'])}[/]")
            return
        self._log("[dim]사용법: /sessions(피커) · /sessions <번호|ID>(전환) · "
                  "/sessions delete <번호|ID>(삭제)[/]")

    def _session_picked(self, result) -> None:
        """SessionPicker 모달 결과 처리(§36 I5)."""
        if not result:
            return
        action, session = result
        if action == "switch":
            self._load_session(session)
            self._log(f"[{_OK}]세션 전환: {session.get('title') or _short_sid(session['id'])}[/]")
        elif action == "delete":
            self.run_worker(self._delete_session(session))

    async def _delete_session(self, target: dict) -> None:
        """세션 삭제(피커·텍스트 명령 공용) — 세션발 큐 작업 연쇄 삭제 포함."""
        if not (self._sessions and self._sessions.delete(target["id"])):
            self._log("[red]세션 삭제 실패.[/]")
            return
        n = 0
        try:
            r = await self.http.request("DELETE", f"/queue/by-session/{target['id']}")
            n = r.json().get("purged", 0)
        except Exception:
            pass
        label = target.get("title") or _short_sid(target["id"])
        self._log(f"[{_ERR}]세션 삭제됨: {label}  (연결된 작업 {n}건 삭제)[/]")
        if target.get("id") == self.session_id:
            self._new_session()  # 현재 세션을 지웠으면 새 세션 시작
        await self.refresh_queue()

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
            rset = set(d.get("reasoning") or [])          # §33 추론(사고 표시 가능) 모델
            cur = self.model or d.get("current") or "(config 기본값)"
            badge = " [{}]💭 추론[/]".format(_INFO) if d.get("current_reasoning") else ""
            self._log(f"[b {_AMBER}]현재 모델[/]: {cur}{badge}")
            await self._show_tiers()
            models = d.get("models") or []
            if models:
                prov = d.get("provider") or ""
                self._log(f"[dim]{prov} 사용 가능 ({len(models)}개):[/]  [dim]💭=추론(사고 과정 표시 가능)[/]")
                self._log("  " + ", ".join(
                    f"[{_ACCENT}]{m}[/]" + (" 💭" if m in rset else "") for m in models[:60]))
                if len(models) > 60:
                    self._log(f"  [dim]… 외 {len(models) - 60}개[/]")
            else:
                self._log("[dim]목록을 가져오지 못했습니다(provider 미설정?). /model <이름> 으로 직접 전환하세요.[/]")
            self._log("[dim]전환: /model <이름>  ·  깊이별: /model high|mid|low <이름> (또는 auto 해제)[/]")
            return
        # depth별 모델 라우팅(§29.1): /model high|mid|low <이름>|auto
        parts = args.split()
        if parts[0].lower() in self._TIERS:
            tier = parts[0].lower()
            val = (parts[1].strip() if len(parts) > 1 else "")
            if not val:
                self._log(f"[red]사용법: /model {tier} <모델이름>  (또는 auto 로 해제)[/]")
                return
            model = None if val.lower() in ("auto", "none", "-") else val
            try:
                r = await self.http.post("/models/tiers", json={"tier": tier, "model": model})
                if r.status_code >= 400:
                    self._log(f"[red]설정 실패: {r.text[:200]}[/]")
                    return
            except Exception as e:
                self._log(f"[red]설정 실패: {e}[/]")
                return
            if model:
                self._log(f"[{_OK}]{tier} 작업 모델 = {model}[/] (다음 해당 작업부터 적용)")
            else:
                self._log(f"[dim]{tier} 작업 모델 해제 → base 모델 사용[/]")
            await self._show_tiers()
            return
        name = args.replace("--global", "").strip()   # --global 은 이제 기본 동작(하위호환 무시)
        if not name:
            self._log("[red]모델 이름이 필요합니다: /model <이름>[/]")
            return
        # /model <이름> = 영구 기본값 설정 — config.default + base 저장 + 깊이별 tier 해제.
        # 사용자가 다시 바꾸기 전까지(재시작 후에도) 유지되고 라우팅이 덮어쓰지 않는다.
        try:
            r = await self.http.post("/models/default", json={"model": name})
            d = r.json() if r.status_code < 400 else {}
            if r.status_code >= 400:
                self._log(f"[red]모델 설정 실패: {r.text[:200]}[/]")
                return
        except Exception as e:
            self._log(f"[red]모델 설정 실패(서비스 준비 중?): {e}[/]")
            return
        self.model = name
        self._new_session(announce=False)  # 새 대화에 즉시 반영
        await self._update_model_display()
        self._log(f"[{_OK}]모델을 {name} 으로 영구 설정했습니다[/] "
                  f"[dim](모든 작업 · 재시작 후에도 유지 · 다시 /model 로 바꾸기 전까지)[/]")
        if d.get("known") is False:
            self._log(f"[{_WARN}]⚠ 이 모델명이 provider 목록에 없습니다 — 오타/접두어(예: "
                      f"meta/, google/) 확인. 설정은 적용했으나 실행 시 404 가 날 수 있습니다. "
                      f"/model 로 목록 확인.[/]")

    async def _show_tiers(self) -> None:
        """현재 depth별 모델 매핑을 표시(§29.1)."""
        try:
            d = (await self.http.get("/models/tiers")).json()
        except Exception:
            return
        tiers = d.get("tiers") or {}
        if not d.get("enabled"):
            self._log("[dim]깊이별 모델: 미설정(단일 모델). 설정 예) /model high llama-3.3-70b-instruct[/]")
            return

        def _lbl(t):
            v = tiers.get(t)
            if isinstance(v, dict):
                return f"{v.get('model')}[dim]({v.get('source', '')})[/]"
            return "[dim]base[/]"
        self._log(f"[b {_AMBER}]깊이별 모델[/]: high={_lbl('high')} · mid={_lbl('mid')} · "
                  f"low={_lbl('low')} · [dim]base={tiers.get('base') or '?'}[/]")

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
        dcolor = {"low": _OK, "mid": _INFO, "high": _WARN}.get(depth, _SOFT)
        est = d.get("estimate") or {}
        self._log(f"[b {_AMBER}]드라이런:[/] 종류={d.get('kind')} · "
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
        self._log(f"[b {_AMBER}]설치된 스킬 {len(items)}개[/]  "
                  f"[dim](에이전트가 작업 중 자동으로 활용 — skill_view 로 로드)[/]")
        for s in items[:60]:
            if not isinstance(s, dict):
                self._log(f"  [{_ACCENT}]{s}[/]")
                continue
            nm = s.get("name") or s.get("id") or "?"
            cat = s.get("category") or ""
            desc = (s.get("description") or "").replace("\n", " ")[:60]
            self._log(f"  [{_ACCENT}]{nm}[/]"
                      + (f" [dim {_INFO}]{cat}[/]" if cat else "")
                      + (f"  [dim]{desc}[/]" if desc else ""))
        if len(items) > 60:
            self._log(f"  [dim]… 외 {len(items) - 60}개[/]")
