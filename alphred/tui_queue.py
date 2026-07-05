"""QueueMixin + QueueDeck — 큐 미션 덱(§36 T3).

큐 표현 3계층: 상태줄 배지(Q1, tui.py) → 인라인 TaskCard(Q2, 2s 폴링 제자리 갱신) →
큐 덱 모달(Q4, ctrl+t: 리스트+상세+조작 키 상시 표시). 상시 큐 패널은 폐지됐다.
슬래시 `/queue <op>` 와 덱 키 조작이 공용 `_queue_op` 으로 게이트웨이를 호출한다.
"""
from __future__ import annotations

import os

from rich.markup import escape as _esc
from textual import work
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from .runtime import resolve_task_id_from_tasks
from .tui_base import (_ACTIVE_STATES, _AMBER, _ERR, _INFO, _OK, _TERMINAL_STATES,
                       _WARN, _short_sid, _state_label)
from .verify import result_needs_attention


def plan_checklist_lines(plan, prog: int, state: str) -> list[str]:
    """계획 체크리스트 마크업 — v1(subtasks)/v2(steps) 겸용 순수 함수(상세뷰·테스트 공용).

    진행 마크는 §19.7 도구 카운트 휴리스틱(도구 힌트가 있는 스텝 순서 대비 완료 도구 수)
    — 스텝 단위 실측 진행은 M4 StepRunner 에서 대체된다.
    """
    if not isinstance(plan, dict):
        return []
    completed = state == "Completed"
    lines: list[str] = []

    def mark_of(has_tool: bool, seen: int) -> tuple[str, int]:
        if completed:
            return f"[{_OK}]✓[/]", seen + (1 if has_tool else 0)
        if has_tool and seen < prog:
            return f"[{_OK}]✓[/]", seen + 1
        if has_tool and seen == prog:
            return f"[{_INFO}]▶[/]", seen + 1
        return "[dim]○[/]", seen + (1 if has_tool else 0)

    steps = plan.get("steps") if isinstance(plan.get("steps"), list) else None
    if steps is not None:                                    # ---- v2 ----
        head = "[b]실행 계획(v2):[/]"
        if plan.get("dod"):
            head += f"  [dim]DoD: {plan['dod']}[/]"
        if plan.get("runs_used"):
            head += f"  [dim](run {plan['runs_used']}회 사용)[/]"
        lines.append(head)
        # §34.6 오케스트레이션 — 스텝에 실측 상태(state)가 있으면 그것을 쓴다(휴리스틱 대체).
        has_state = any("state" in s for s in steps)
        seen = 0
        for i, s in enumerate(steps, 1):
            hint = s.get("tool_hint")
            has_tool = bool(hint) and hint != "none"
            if has_state:
                st = s.get("state")
                mark = (f"[{_OK}]✓[/]" if st == "done"
                        else f"[{_INFO}]▶[/]" if st == "running" else "[dim]○[/]")
                if st != "done" and completed:
                    mark = f"[{_OK}]✓[/]"
            else:
                mark, seen = mark_of(has_tool, seen)
            exp = s.get("expected") or {}
            extra = []
            if hint:
                extra.append(str(hint))
            if exp.get("type") == "file":
                extra.append("→ " + (exp.get("format") or "file")
                             + (f" {exp['path_hint']}" if exp.get("path_hint") else ""))
            elif exp.get("type") == "action":
                extra.append("→ 상태 변경")
            acc = ",".join(a.get("check", "") for a in (s.get("accept") or []) if a.get("check"))
            if acc:
                extra.append(f"확인:{acc}")
            if s.get("attempts"):
                extra.append(f"재시도 {s['attempts']}회")
            lines.append(f"  {mark} [dim]{i}.[/] {s.get('goal', '')}"
                         + (f"  [dim]({' · '.join(extra)})[/]" if extra else ""))
        for g in plan.get("gaps") or []:                     # 접지 수리 내역(§34.5 D4)
            lines.append(f"  [{_WARN}]⚙ 접지:[/] [dim]{g}[/]")
        return lines

    subs = plan.get("subtasks") if isinstance(plan.get("subtasks"), list) else None
    if not subs:                                             # ---- v1 ----
        return []
    lines.append("[b]하위작업 계획:[/]")
    seen = 0
    for i, s in enumerate(subs, 1):
        has_tool = bool(s.get("tools"))
        mark, seen = mark_of(has_tool, seen)
        tools = ",".join(s.get("tools") or [])
        lines.append(f"  {mark} [dim]{i}.[/] {s.get('title', '')} "
                     f"[dim]({s.get('kind', '')}/{s.get('effort', '')}"
                     f"{(' · ' + tools) if tools else ''})[/]")
    return lines


def verify_report_lines(rep) -> list[str]:
    """검증 증거 패널(§21) 마크업 — Tier0 결정적 + Tier2 judge (순수 함수, 채팅·덱 공용)."""
    if not isinstance(rep, dict):
        return []
    lines: list[str] = []
    icon = f"[{_OK}]✓[/]" if rep.get("passed") else f"[{_WARN}]⚠[/]"
    lines.append(f"[b]검증(Tier0):[/] {icon} {rep.get('summary', '')}")
    for c in (rep.get("checks") or []):
        ok = f"[{_OK}]OK[/]" if c.get("ok") else f"[{_ERR}]실패[/]"
        kind = c.get("check", "")
        target = c.get("target") or c.get("path") or ""   # 신/구 스키마 모두 허용
        detail = c.get("detail") or c.get("reason") or ""
        lines.append(f"  {ok} [dim]{kind}:[/] [dim]{target}[/]"
                     + (f"  [dim]— {detail}[/]" if detail else ""))
    j = rep.get("judge")
    if isinstance(j, dict):
        jicon = f"[{_OK}]✓[/]" if j.get("passed") else f"[{_ERR}]✗[/]"
        lines.append(f"[b]수용 judge(Tier2):[/] {jicon} 점수 {j.get('score', '-')} "
                     f"[dim]{j.get('summary', '')}[/]")
        for c in (j.get("criteria") or []):
            m = f"[{_OK}]✓[/]" if c.get("met") else f"[{_ERR}]✗[/]"
            lines.append(f"  {m} [dim]{c.get('name', '')}"
                         + (f" — {c.get('note')}" if c.get("note") else "") + "[/]")
        for u in (j.get("unmet") or []):
            lines.append(f"  [{_WARN}]· 미흡:[/] [dim]{u}[/]")
    if rep.get("suggestion"):
        lines.append(f"[{_INFO}]제안:[/] {rep['suggestion']}")
    for a in (rep.get("assumptions") or []):     # §34.4 무응답 채택 가정 표면화
        lines.append(f"  [{_WARN}]· 가정:[/] [dim]{a}[/]")
    return lines


def task_detail_lines(d: dict) -> list[str]:
    """작업 상세 마크업(순수 함수) — 큐 덱 상세 패널용(구 상세뷰 이관, §36 Q4)."""
    lines: list[str] = []
    est = d.get("estimate") or {}
    tid = (d.get("id") or "")[:8]
    lines.append(f"[b {_AMBER}]작업 {tid}[/]  상태={_state_label(d.get('state') or '')} "
                 f"우선={d.get('priority')} 종류={d.get('kind')}"
                 + (f" 심화도={d['depth']}" if d.get("depth") else "")
                 + (f" · 견적 ~{est['est_llm_calls']}콜" if est.get("est_llm_calls") else "")
                 + (f" · 재시도 {d['verify_attempts']}회" if d.get("verify_attempts") else ""))
    lines.append(f"[dim]분류근거:[/] {d.get('classify_reason') or '-'}")
    lines.append(f"[dim]요청:[/] {(d.get('prompt') or '')[:300]}")
    prog = d.get("plan_progress") or 0
    act = d.get("plan_activity")
    if d.get("state") == "In-Progress":
        lines.append(f"[{_INFO}]진행: 완료 도구 {prog}개"
                     + (f" · 현재 🔧 {act}" if act else "") + "[/]")
    lines += plan_checklist_lines(d.get("plan") or {}, prog, d.get("state") or "")
    # §34.4 인테이크 — 질문/답변/채택 가정 표시
    for i, q in enumerate(d.get("questions") or [], 1):
        opts = " · ".join(o.get("label", "") + ("✦" if o.get("recommended") else "")
                          for o in (q.get("options") or []))
        lines.append(f"[dim]질문{i}: {q.get('q', '')}  ({opts})[/]")
    if d.get("state") == "AwaitingInput":
        lines.append(f"[{_AMBER}]❓ 답변 대기 중 — a 키(또는 /answer {tid})로 답하거나 "
                     f"그대로 두면 추천값(✦)을 가정하고 진행합니다.[/]")
    if d.get("answers"):
        for a in d["answers"] if isinstance(d["answers"], list) else [d["answers"]]:
            txt = (a.get("answer") if isinstance(a, dict) else str(a)) or ""
            lines.append(f"[{_INFO}]답변: {txt[:120]}[/]")
    elif d.get("assumptions"):
        for a in d["assumptions"]:
            lines.append(f"[{_WARN}]가정: {str(a)[:120]}[/]")
    if d.get("result"):
        lines.append(f"[b]결과:[/]\n{d['result'][:2000]}")
    if d.get("error"):
        lines.append(f"[red]에러:[/] {d['error'][:400]}")
    lines += verify_report_lines(d.get("verify_report"))
    evs = d.get("events") or []
    if evs:
        lines.append("[dim]이력: " + " → ".join(e.get("to_state", "") for e in evs[-6:]) + "[/]")
    return lines


def deck_slot_line(tasks: list[dict], slots: int = 1, active_slots: int = 0) -> str:
    """실행 슬롯 시각화(§36 Q4, §38 P4) — 누가 점유 중이고 대기 1순위가 누구인지."""
    def head(t):
        return f"{t['id'][:8]} {(t.get('prompt') or '').replace(chr(10), ' ')[:32]}"
    running = [t for t in tasks if t.get("state") == "In-Progress"]
    pend = [t for t in tasks if t.get("state") == "Pending"]
    pend.sort(key=lambda t: -int(t.get("priority") or 0))

    if running:
        heads = ", ".join(f"[{t['id'][:8]}]" for t in running)
        line = f"[{_INFO}]▶ 실행 슬롯 ({active_slots}/{slots}):[/] {heads}"
    else:
        line = f"[dim]▶ 실행 슬롯 (0/{slots}): (비어 있음)[/]"

    if pend:
        line += f"   [dim]│ 대기 1순위: {_esc(head(pend[0]))} (우선 {pend[0].get('priority')})[/]"
    return line


class QueueDeck(ModalScreen):
    """큐 덱(§36 Q4) — 좌측 작업 리스트 + 우측 상세, 조작 키 상시 표시. ctrl+t / /queue."""
    BINDINGS = [("escape", "close", "닫기"), ("a", "answer", "답변"),
                ("p", "pause", "보류"), ("r", "resume", "재개"),
                ("R", "retry", "재시도"), ("d", "discard", "폐기"),
                ("plus", "prio_up", "우선↑"), ("minus", "prio_down", "우선↓")]
    DEFAULT_CSS = f"""
    QueueDeck {{ align: center middle; }}
    #deck-box {{ width: 95%; height: 85%; border: round {_AMBER}; padding: 0 1; }}
    #deck-main {{ height: 1fr; }}
    #deck-list {{ width: 44; height: 1fr; }}
    #deck-detail {{ width: 1fr; height: 1fr; padding: 0 1; }}
    #deck-detail Static {{ height: auto; }}
    #deck-keys {{ height: 1; color: $text-muted; }}
    """

    def __init__(self):
        super().__init__()
        self._tasks: list[dict] = []
        self._detail_tid: str | None = None

    def compose(self):
        with Vertical(id="deck-box"):
            yield Static("[b]◆ 큐 덱[/]", id="deck-slot")
            with Horizontal(id="deck-main"):
                yield OptionList(id="deck-list")
                with VerticalScroll(id="deck-detail"):
                    yield Static("[dim]작업을 선택하세요.[/]", id="deck-detail-body")
            yield Static("↑↓ 이동 · Enter 상세/라이브 · a 답변 · p 보류 · r 재개 · "
                         "+/- 우선순위 · R 재시도 · d 폐기 · Esc 닫기", id="deck-keys")

    def on_mount(self) -> None:
        self.query_one("#deck-list", OptionList).focus()
        self.run_worker(self._load())

    async def _load(self) -> None:
        try:
            res = (await self.app.http.get("/queue")).json()
            tasks = res.get("tasks", [])
            slots = res.get("slots", 1)
            active_slots = res.get("active_slots", 0)
        except Exception as e:
            self.query_one("#deck-slot", Static).update(f"[{_ERR}]큐 조회 실패: {e}[/]")
            return
        self.set_tasks(tasks, slots=slots, active_slots=active_slots)

    def set_tasks(self, tasks: list[dict], slots: int = 1, active_slots: int = 0) -> None:
        """리스트/슬롯 갱신(열 때 + 2s 폴링 훅에서 호출) — 하이라이트는 작업 id 로 유지."""
        active = [t for t in tasks if t["state"] in _ACTIVE_STATES]
        active.sort(key=lambda t: -int(t.get("priority") or 0))
        done = [t for t in tasks if t["state"] in _TERMINAL_STATES]
        done.sort(key=lambda t: t.get("created_at") or "", reverse=True)
        self._tasks = active + done[:12]
        try:
            ol = self.query_one("#deck-list", OptionList)
            slot = self.query_one("#deck-slot", Static)
        except Exception:
            return
        slot.update(deck_slot_line(tasks, slots=slots, active_slots=active_slots))
        prev_tid = None
        if ol.highlighted is not None and 0 <= ol.highlighted < ol.option_count:
            prev_tid = ol.get_option_at_index(ol.highlighted).id
        ol.clear_options()
        for t in self._tasks:
            prompt = _esc((t.get("prompt") or "").replace("\n", " ").strip()[:24])
            sid = _short_sid(t.get("session_key") or "") or "-"
            label = (f"{t['id'][:8]}  {_state_label(t['state'])} "
                     f"[dim]{t.get('priority', '')}[/] {prompt} [dim]{sid}[/]")
            ol.add_option(Option(label, id=t["id"]))
        if not self._tasks:
            return
        idx = next((i for i, t in enumerate(self._tasks) if t["id"] == prev_tid), 0)
        ol.highlighted = idx

    def _current(self) -> dict | None:
        ol = self.query_one("#deck-list", OptionList)
        if ol.highlighted is None or not self._tasks:
            return None
        if 0 <= ol.highlighted < len(self._tasks):
            return self._tasks[ol.highlighted]
        return None

    def on_option_list_option_highlighted(self, event) -> None:
        tid = getattr(event.option, "id", None)
        if tid and tid != self._detail_tid:
            self._detail_tid = tid
            self.run_worker(self._load_detail(tid), exclusive=True)

    def on_option_list_option_selected(self, event) -> None:
        event.stop()
        t = self._current()
        if not t:
            return
        state = t.get("state", "")
        tid = t["id"]
        if state == "In-Progress":
            self.dismiss(None)
            self.app._start_history_live(tid)
        elif state in ("Completed", "NeedsReview", "Discarded"):
            self.dismiss(None)
            self.app._start_history_view(tid)
        else:
            self.run_worker(self._load_detail(tid), exclusive=True)

    async def _load_detail(self, tid: str) -> None:
        body = self.query_one("#deck-detail-body", Static)
        try:
            d = (await self.app.http.get(f"/queue/{tid}")).json()
        except Exception as e:
            body.update(f"[{_ERR}]상세 조회 실패: {e}[/]")
            return
        if self._detail_tid not in (None, tid):
            return                          # 이동이 앞질렀으면 무시(스테일)
        text = "\n".join(task_detail_lines(d))
        try:
            body.update(text)
        except Exception:                   # 결과물에 마크업 충돌 문자가 있으면 원문으로
            body.update(_esc(text))

    # ---- 조작(공용 _queue_op 경유) ----
    def _op(self, op: str, *, delta: int = 0) -> None:
        t = self._current()
        if not t:
            return
        prio = None
        if delta:
            prio = max(1, min(10, int(t.get("priority") or 5) + delta))
            op = "prio"
        self.run_worker(self._do_op(op, t["id"], prio))

    async def _do_op(self, op: str, tid: str, prio: int | None) -> None:
        try:
            await self.app._queue_op(op, tid, priority=prio)
        except Exception as e:
            self.query_one("#deck-slot", Static).update(f"[{_ERR}]실패: {e}[/]")
            return
        await self._load()
        if self._detail_tid:
            await self._load_detail(self._detail_tid)

    def action_pause(self) -> None:
        self._op("pause")

    def action_resume(self) -> None:
        self._op("resume")

    def action_retry(self) -> None:
        self._op("retry")

    def action_discard(self) -> None:
        self._op("cancel")

    def action_prio_up(self) -> None:
        self._op("prio", delta=+1)

    def action_prio_down(self) -> None:
        self._op("prio", delta=-1)

    def action_answer(self) -> None:
        t = self._current()
        if t and t.get("state") == "AwaitingInput":
            self.dismiss(None)
            self.app.run_worker(self.app.cmd_answer(t["id"][:8]))



    def action_close(self) -> None:
        self.dismiss(None)


class QueueMixin:
    async def _queue_op(self, op: str, tid: str, *, priority: int | None = None) -> str:
        """단일 큐 작업에 대한 게이트웨이 호출(슬래시 명령·덱 조작 공용).

        반환: 사람이 읽을 결과 메시지(마크업). 호출자가 try/except + refresh_queue 를 감싼다.
        """
        if op in ("cancel", "discard"):
            await self.http.request("DELETE", f"/queue/{tid}")
            return f"[{_ERR}]폐기됨: {tid[:8]}[/]"
        if op == "purge":
            await self.http.request("DELETE", f"/queue/{tid}/purge")
            return f"[{_ERR}]영구 삭제됨: {tid[:8]}[/]"
        if op == "pause":
            await self.http.post(f"/queue/{tid}/pause")
            return f"[{_WARN}]일시중지: {tid[:8]}[/]"
        if op == "resume":
            await self.http.post(f"/queue/{tid}/resume")
            return f"[{_OK}]재개 허용: {tid[:8]}[/]"
        if op == "retry":
            await self.http.post(f"/queue/{tid}/retry")
            return f"[{_OK}]재시도 대기열 등록: {tid[:8]}[/]"
        if op == "prio":
            await self.http.post(f"/queue/{tid}/prio", json={"priority": int(priority)})
            return f"[{_OK}]우선순위: {tid[:8]} → {priority}[/]"
        raise ValueError(f"unknown queue op: {op}")

    async def cmd_queue(self, args: str) -> None:
        args = args.strip()
        parts = args.split()
        sub = parts[0] if parts else "list"
        if sub in ("", "list"):
            self.action_queue_deck()                      # §36 Q4 — 무인자 = 큐 덱
            return
        if sub == "ask":                                  # 자연어 큐 제어(POST /queue/ask)
            q = args[len("ask"):].strip()
            if not q:
                self._log('[dim]사용법: /queue ask "<자연어 요청>"  '
                          '(예: /queue ask "리포트 작업 우선순위 올려줘")[/]')
                return
            try:
                d = (await self.http.post("/queue/ask", json={"q": q})).json()
            except Exception as e:
                self._log(f"[red]실패: {e}[/]")
                return
            if d.get("reply"):
                self._log(f"[{_INFO}]{d['reply']}[/]")
            for r in d.get("results", []):
                self._log(f"  • {r}")
            await self.refresh_queue()
            return
        if sub == "clear":
            try:
                r = (await self.http.post("/queue/clear")).json()
                self._log(f"[{_ERR}]종료된 작업 {r.get('cleared', 0)}건 영구 삭제됨[/]")
            except Exception as e:
                self._log(f"[red]실패: {e}[/]")
            await self.refresh_queue()
            return
        if sub in ("cancel", "discard", "purge", "pause", "resume", "retry", "prio") and len(parts) >= 2:
            full = await self._resolve_task_id(parts[1])
            if not full:
                self._log(f"[red]작업을 찾을 수 없음: {parts[1]}[/]")
                return
            if sub == "prio" and (len(parts) < 3 or not parts[2].lstrip("-").isdigit()):
                self._log("[red]사용법: /queue prio <id> <1-10>[/]")
                return
            try:
                prio = int(parts[2]) if sub == "prio" else None
                self._log(await self._queue_op(sub, full, priority=prio))
            except Exception as e:
                self._log(f"[red]실패: {e}[/]")
            await self.refresh_queue()
            return
        self._log('[dim]사용법: /queue(덱) | ask "<요청>" | cancel <id> | purge <id> | clear | '
                  'pause <id> | resume <id> | retry <id> | prio <id> <1-10>[/]')

    async def cmd_answer(self, args: str) -> None:
        """§36 Q3 — 답변 대기(❓) 작업에 어디서 생겼든 답한다(질문 카드 소환)."""
        args = (args or "").strip()
        if self._pending_input is not None:
            self._log("[dim]이미 답변 모드입니다 — Esc 로 보류 후 다시 시도하세요.[/]")
            return
        try:
            tasks = (await self.http.get("/queue")).json().get("tasks", [])
        except Exception as e:
            self._log(f"[red]큐 조회 실패: {e}[/]")
            return
        waiting = [t for t in tasks if t.get("state") == "AwaitingInput"]
        if args:
            waiting = [t for t in waiting if t["id"].startswith(args)]
        if not waiting:
            self._log("[dim]답변 대기(❓) 작업이 없습니다.[/]")
            return
        waiting.sort(key=lambda t: t.get("created_at") or "")   # 가장 오래 기다린 것부터
        t = waiting[0]
        qs = t.get("questions") or []
        if not qs:
            self._log(f"[dim]작업 {t['id'][:8]} 에 질문 정보가 없습니다.[/]")
            return
        self._begin_answer_mode({"id": t["id"], "questions": qs})

    # ---- Heavy 실행 라이브 뷰(§33/2A) — 진행 과정(회색) 실시간, 최종 결과(흰색) ----
    # ---- Heavy 실행 라이브 뷰(§33/2A) — 진행 과정(회색) 실시간, 최종 결과(흰색) ----
    def _start_history_live(self, tid: str) -> None:
        """과거 히스토리 로드 + 이어서 라이브 스트리밍."""
        self.stop_live()
        self._live_tid = tid
        self._live_worker = self._history_live_run(tid)

    def _start_live(self, tid: str) -> None:
        """이전 버전과의 호환성을 위한 프록시."""
        self._start_history_live(tid)

    def _start_history_view(self, tid: str) -> None:
        """완료된 작업의 전체 히스토리 표시."""
        self.stop_live()
        self._live_tid = tid
        self._live_worker = self._history_view_run(tid)

    def stop_live(self) -> None:
        w = getattr(self, "_live_worker", None)
        if w is not None:
            try:
                w.cancel()
            except Exception:
                pass
        self._live_worker = None
        self._live_tid = None
        self._clear_stream()

    @work(exclusive=False)
    async def _history_live_run(self, tid: str) -> None:
        """과거 이벤트 렌더링 후 라이브 스트리밍 연결."""
        import json
        self._log(f"[dim]▶ 작업 {tid[:8]} — 진행 기록 로드 중...[/]")
        self._clear_stream()
        self._open_tools = []
        
        # 1단계: 과거 히스토리 로드 및 렌더링
        try:
            res = (await self.http.get(f"/queue/{tid}/history")).json()
            events = res.get("events", [])
        except Exception as e:
            self._log(f"[dim]히스토리 로드 실패: {e}[/]")
            events = []
        
        if events:
            self._log(f"[dim]── 진행 기록 ({len(events)}건) ──────────────────────[/]")
            for ev in events:
                name = ev.get("event")
                if name in ("tool.started", "tool.completed", "tool.failed",
                            "assistant.delta", "assistant.completed", "reasoning"):
                    self._render_run_event(name, ev, record=False)
            self._clear_stream()  # 히스토리 렌더 후 스트림 버퍼 초기화
            self._open_tools = []
        
        # 2단계: 실시간 스트리밍 연결
        self._log(f"[dim]── 실시간 ──────────────────────────────────[/]")
        try:
            async with self.http.stream("GET", f"/queue/{tid}/stream") as resp:
                if resp.status_code != 200:
                    self._log(f"[{_ERR}]라이브 스트림 실패 HTTP {resp.status_code}[/]")
                    return
                event, data = None, []
                async for line in resp.aiter_lines():
                    if line.startswith(":"):
                        continue
                    if line.startswith("event:"):
                        event = line[6:].strip()
                    elif line.startswith("data:"):
                        data.append(line[5:].strip())
                    elif line == "" and event:
                        try:
                            d = json.loads("".join(data)) if data else {}
                        except Exception:
                            d = {}
                        if event == "done":
                            if self._proc_buf.strip():
                                self._flush_proc(final=True, text=(d.get("result") or None),
                                                 record=False)
                            else:
                                self._clear_stream()
                            self._log(f"[dim]— 라이브 종료 ({d.get('state', '')})[/]")
                            await self.refresh_queue()
                            break
                        if event == "state":
                            self._log(f"[dim]상태: {d.get('state')}[/]")
                        else:
                            self._render_run_event(event, d, record=False)
                        event, data = None, []
        except Exception as e:
            self._log(f"[{_ERR}]라이브 오류: {e}[/]")

    @work(exclusive=False)
    async def _history_view_run(self, tid: str) -> None:
        """완료된 작업의 전체 히스토리 + 결과 표시."""
        import json
        self._clear_stream()
        self._open_tools = []
        
        # 작업 상세 조회
        try:
            detail = (await self.http.get(f"/queue/{tid}")).json()
        except Exception as e:
            self._log(f"[{_ERR}]작업 조회 실패: {e}[/]")
            return
        
        state = detail.get("state", "")
        state_label = "✓" if state == "Completed" else "⚠" if state == "NeedsReview" else "✗"
        self._log(f"[dim]▶ 작업 {tid[:8]} — {state} {state_label}[/]")
        
        # 히스토리 로드
        try:
            res = (await self.http.get(f"/queue/{tid}/history")).json()
            events = res.get("events", [])
        except Exception:
            events = []
        
        if events:
            self._log(f"[dim]── 진행 기록 ({len(events)}건) ──────────────────────[/]")
            for ev in events:
                name = ev.get("event")
                if name in ("tool.started", "tool.completed", "tool.failed",
                            "assistant.delta", "assistant.completed", "reasoning"):
                    self._render_run_event(name, ev, record=False)
            self._clear_stream()
            self._open_tools = []
        
        # 결과 표시
        result = detail.get("result") or ""
        error = detail.get("error") or ""
        if result:
            self._log(f"[dim]── 결과 ──────────────────────────────────[/]")
            self._log(result)
        if error:
            self._log(f"[{_ERR}]── 오류 ──────────────────────────────────[/]")
            self._log(f"[{_ERR}]{error}[/]")
        
        verify = detail.get("verify_report")
        if verify and isinstance(verify, dict):
            self._render_verify_report(verify)
        
        self._log(f"[dim]— Esc 로 복귀[/]")

    def _render_verify_report(self, rep) -> None:
        """검증 증거 패널(§21)을 채팅에 출력 — 마크업 생성은 verify_report_lines 공용."""
        for ln in verify_report_lines(rep):
            self._log(ln)

    async def _resolve_task_id(self, prefix: str) -> str | None:
        try:
            tasks = (await self.http.get("/queue")).json().get("tasks", [])
        except Exception:
            return None
        return resolve_task_id_from_tasks(tasks, prefix)

    # ---- 2s 폴링 파이프라인(§36 Q1/Q2/Q5) ----
    async def refresh_queue(self) -> None:
        try:
            res = (await self.http.get("/queue")).json()
            tasks = res.get("tasks", [])
            self.slots = res.get("slots", 1)
            self.slots_config = res.get("slots_config", "1")
            self.slots_max = res.get("slots_max", 4)
            self.active_slots = res.get("active_slots", 0)
            self.budgets = res.get("budgets", {})
        except Exception:
            self._status("서비스 연결 대기 중…")
            return
        if self._status_text == "서비스 연결 대기 중…":
            self._status("준비됨")
        self._update_badges(tasks)                    # Q1 상태줄 배지
        active = [t for t in tasks if t["state"] in _ACTIVE_STATES]
        active.sort(key=lambda t: -int(t.get("priority") or 0))
        self._rows = [(t["id"], int(t.get("priority") or 0), t["state"])
                      for t in active]                # 인자 완성(/queue <op> <id>) 후보
        cards_before = set(self._task_cards)
        self._update_task_cards(tasks)                # Q2 인라인 카드 제자리 갱신
        self._notify_transitions(tasks, cards_before)  # Q5 토스트/벨/채팅 알림
        deck = self.screen if isinstance(self.screen, QueueDeck) else None
        if deck is not None:
            deck.set_tasks(tasks, slots=self.slots, active_slots=self.active_slots)

    def _update_task_cards(self, tasks: list[dict]) -> None:
        by_id = {t["id"]: t for t in tasks}
        for tid, card in list(self._task_cards.items()):
            t = by_id.get(tid)
            if t is None:                             # purge 등으로 사라짐
                self._task_cards.pop(tid, None)
                continue
            card.update_from(t)
            if t["state"] in _TERMINAL_STATES:        # 최종 렌더 후 갱신 중단
                self._task_cards.pop(tid, None)

    def _toast(self, msg: str, severity: str) -> None:
        try:
            self.notify(msg, severity=severity, timeout=6)
        except Exception:
            pass
        if os.environ.get("ALPHRED_TUI_BELL", "1") != "0":
            try:
                self.bell()
            except Exception:
                pass

    def _notify_transitions(self, tasks: list[dict], cards_before: set) -> None:
        """상태 전이 알림 — 토스트+벨은 항상, 채팅 로그는 카드 없는 작업(타 세션)만."""
        for t in tasks:
            tid, cur = t["id"], t["state"]
            prev = self._states.get(tid)
            # Q3: 어디서 생겼든 답변 대기 부상(내 세션은 질문 카드가 이미 떠 있으므로 제외)
            if (cur == "AwaitingInput" and prev != cur
                    and t.get("session_key") != self.session_id):
                self._toast(f"❓ 작업 {tid[:8]} 답변 대기 — /answer 또는 ctrl+t", "warning")
                if not self._busy:
                    self._log(f"[b {_AMBER}]❓ 작업 {tid[:8]} 답변 대기[/] "
                              f"[dim]{(t.get('prompt') or '')[:60]} · /answer {tid[:8]}[/]")
            if prev and prev != cur and cur in _TERMINAL_STATES:
                had_card = tid in cards_before
                if cur == "Completed":
                    attn = result_needs_attention(t.get("result"))
                    self._toast(f"{'⚠' if attn else '✓'} 작업 {tid[:8]} 완료",
                                "warning" if attn else "information")
                    if not had_card and not self._busy:
                        res = (t.get("result") or "").replace("\n", " ")[:140]
                        mark = f"[b {_WARN}]⚠ 작업 {tid[:8]} 완료(확인 필요)[/]" if attn \
                            else f"[b {_OK}]✓ 작업 {tid[:8]} 완료[/]"
                        self._log(f"{mark} {res}")
                elif cur == "NeedsReview":
                    self._toast(f"⚠ 작업 {tid[:8]} 검토 필요", "warning")
                    if not had_card and not self._busy:
                        rep = t.get("verify_report") or {}
                        self._log(f"[b {_WARN}]⚠ 작업 {tid[:8]} 검토 필요 — 검증 미통과:[/] "
                                  f"{rep.get('summary', '') if isinstance(rep, dict) else ''}"
                                  f"  [dim](ctrl+t 상세)[/]")
                else:
                    self._toast(f"✗ 작업 {tid[:8]} 폐기", "error")
                    if not had_card and not self._busy:
                        self._log(f"[b {_ERR}]✗ 작업 {tid[:8]} 폐기[/] "
                                  f"{(t.get('error') or '')[:100]}")
            self._states[tid] = cur
