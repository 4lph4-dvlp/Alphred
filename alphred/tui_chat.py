"""ChatMixin — 게이트웨이 `/chat/stream`(SSE) 소비 + 작업 과정/답변 렌더.

`AlphredTUI` 에 믹스인. tool.started/completed·assistant.delta/completed·queued 이벤트를
채팅 로그/스트리밍 위젯/상태바에 반영한다.
"""
from __future__ import annotations

import json
from rich.text import Text
from textual import work
from textual.widgets import Static

from .tui_base import QuestionCard, ThinkBlock, ToolBlock, _ACCENT, _AMBER, _ERR, _WARN


class ChatMixin:
    def _chat_context(self) -> str | None:
        """§34.2 A2 — 최근 대화(현재 메시지 제외 6턴, 800자컷)를 의도 판정 맥락으로 동봉.

        submit_current 가 현재 메시지를 세션에 먼저 기록하므로 마지막 항목은 제외한다.
        """
        msgs = ((self._session or {}).get("messages") or [])[:-1]
        lines = [f"{m.get('role')}: {(m.get('text') or '').strip()[:200]}"
                 for m in msgs[-6:] if (m.get("text") or "").strip()]
        return "\n".join(lines)[:800] or None

    @work(exclusive=False)
    async def send(self, msg: str) -> None:
        import time
        self._busy = True
        self._busy_since = time.monotonic()
        self._open_tools = []
        self._status("처리 중…")
        try:
            async with self.http.stream("POST", "/chat/stream",
                                        json={"message": msg, "session_id": self.session_id,
                                              "model": self.model,
                                              "depth": self.depth_override,
                                              "context": self._chat_context()}) as resp:
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
            self._send_worker = None
            self._status("준비됨")
            self._drain_pending()          # §36 I2 — busy 중 쌓인 대기 메시지 자동 전송

    # ---- §36 I1/I2 — Esc 중단 · busy 중 입력 큐잉 ----
    def cancel_send(self) -> None:
        """Esc — 진행 중 응답 스트림을 즉시 중단(부분 출력은 회색으로 확정)."""
        w = self._send_worker
        if w is None:
            return
        try:
            w.cancel()                     # 워커 취소 → 스트림 정리 → finally 에서 상태 복구
        except Exception:
            pass
        self._flush_proc(record=False)     # 부분 텍스트/사고 = 회색 확정
        self._log(f"[{_WARN}]⏹ 응답을 중단했습니다.[/]")

    def _drain_pending(self) -> None:
        """대기 메시지(제출 시점에 이미 화면·세션 기록됨)를 순서대로 전송."""
        if self._pending_msgs and not self._busy:
            self._send_worker = self.send(self._pending_msgs.pop(0))

    def _stream_widget(self) -> Static:
        return self._streaming_w

    def _clear_stream(self) -> None:
        self._stream_buf = ""
        self._proc_buf = ""
        self._think_buf = ""
        w = self._stream_widget()
        w.update("")
        w.display = False

    def _handle_event(self, event: str, d: dict) -> None:
        """Light(/chat/stream) 이벤트 — queued/needs_input/프레이밍만 특수 처리, 나머지는 공용 렌더러로."""
        if event == "queued":
            self._clear_stream()
            tid = d.get("id") or ""
            self._log(f"[{_AMBER}]⏳ 무거운 작업으로 백그라운드 큐에 등록됨 "
                      f"(id={tid[:8]}). 아래 카드에서 진행 상황을 추적합니다.[/]")
            self._spawn_task_card(tid)                # §36 Q2 인라인 라이브 카드
            self.call_after_refresh(self.refresh_queue)
            return
        if event == "needs_input":                   # §34.4 착수 전 질문(추천답변 포함)
            self._clear_stream()
            self._begin_answer_mode(d)
            self.call_after_refresh(self.refresh_queue)
            return
        if event in ("run.started", "message.started"):
            return                                   # 프레이밍 이벤트 — 무시
        self._render_run_event(event, d, record=True)

    # ---- §34.4 인테이크 답변 모드 — Enter=추천 · 숫자=선택 · 텍스트=직접 · Esc=가정 진행 ----
    _PROMPT_TITLE = "메시지  ·  Enter 전송  ·  Shift+Enter 줄바꿈  ·  ↑↓ 기록  ·  / 명령"

    def _begin_answer_mode(self, d: dict) -> None:
        qs = d.get("questions") or []
        if not qs:
            return
        card = QuestionCard()
        self._pending_input = {"task_id": d.get("id") or "", "questions": qs,
                               "answers": [], "idx": 0, "card": card}
        self._chat_add(card)
        self.call_after_refresh(self._show_question)   # 카드 마운트 후 첫 질문 렌더

    def _show_question(self) -> None:
        st = self._pending_input
        if not st:
            return
        i, qs = st["idx"], st["questions"]
        try:
            st["card"].show_question(qs[i], i, len(qs))
        except Exception:
            pass
        try:
            from .tui_base import PromptInput
            self.query_one("#prompt", PromptInput).border_title = (
                f"답변 {i + 1}/{len(qs)}  ·  카드에서 ↑↓+Enter  ·  "
                f"입력창: 빈 Enter=추천 · 번호/직접 입력  ·  Esc=가정 진행")
        except Exception:
            pass

    def answer_pick(self, n: int) -> None:
        """카드 OptionList 에서 n번 선택지 채택(§36 I3)."""
        st = self._pending_input
        if not st:
            return
        opts = st["questions"][st["idx"]].get("options") or []
        if 0 <= n < len(opts):
            self._apply_answer(opts[n].get("label", ""))

    def answer_free_input(self) -> None:
        """카드에서 '직접 입력…' 선택 — 입력창으로 포커스를 넘긴다(제출은 기존 경로)."""
        try:
            from .tui_base import PromptInput
            inp = self.query_one("#prompt", PromptInput)
            inp.border_title = "직접 답변 입력  ·  Enter 제출  ·  Esc=가정 진행"
            inp.focus()
        except Exception:
            pass

    def answer_submit(self, text: str) -> None:
        """답변 모드에서의 입력창 텍스트 1건 처리(빈 문자열=추천 채택)."""
        st = self._pending_input
        if not st:
            return
        opts = st["questions"][st["idx"]].get("options") or []
        text = (text or "").strip()
        if not text:
            ans = next((o.get("label", "") for o in opts if o.get("recommended")),
                       opts[0].get("label", "") if opts else "")
        elif text.isdigit() and 1 <= int(text) <= len(opts):
            ans = opts[int(text) - 1].get("label", "")
        else:
            ans = text
        self._apply_answer(ans)

    def _apply_answer(self, ans: str) -> None:
        """답변 1건 확정(카드/입력창 공용) — 다음 질문 또는 전송."""
        st = self._pending_input
        if not st:
            return
        q = st["questions"][st["idx"]]
        st["answers"].append({"q": q.get("q", ""), "answer": ans})
        self._log(f"[{_ACCENT}]  → {ans}[/]")
        st["idx"] += 1
        if st["idx"] < len(st["questions"]):
            self._show_question()
        else:
            self.run_worker(self._post_answers(dict(st)))
            self._end_answer_mode()

    def answer_mode_cancel(self) -> None:
        """Esc — 답변을 보류하고 넘어간다(타임아웃 시 추천 가정으로 자동 진행)."""
        if self._pending_input is None:
            return
        self._end_answer_mode()
        self._log("[dim]질문을 건너뜀 — 답변 대기시간이 지나면 추천값을 가정하고 "
                  "자동으로 진행합니다.[/]")

    def _end_answer_mode(self) -> None:
        st, self._pending_input = self._pending_input, None
        card = (st or {}).get("card")
        if card is not None:
            try:
                card.remove()
            except Exception:
                pass
        try:
            from .tui_base import PromptInput
            inp = self.query_one("#prompt", PromptInput)
            inp.border_title = self._PROMPT_TITLE
            inp.focus()
        except Exception:
            pass

    async def _post_answers(self, st: dict) -> None:
        try:
            r = await self.http.post(f"/queue/{st['task_id']}/answers",
                                     json={"answers": st["answers"]})
            if r.status_code == 200:
                self._log(f"[b #7BC96F]✓ 답변 반영 — 작업이 실행 대기열에 등록되었습니다 "
                          f"(id={st['task_id'][:8]}).[/]")
            else:
                self._log(f"[{_ERR}]답변 전송 실패 HTTP {r.status_code} — 작업은 대기시간 후 "
                          f"추천 가정으로 진행됩니다.[/]")
        except Exception as e:
            self._log(f"[{_ERR}]답변 전송 오류: {e}[/]")
        self.call_after_refresh(self.refresh_queue)

    # ---- 공용 실행 이벤트 렌더러(§33/2B) — 사고·중간텍스트=회색, 최종 답변=흰색 ----
    @staticmethod
    def _ev_text(d: dict) -> str:
        return d.get("delta") or d.get("content") or d.get("text") or d.get("preview") or ""

    @staticmethod
    def _ev_tool(d: dict) -> str:
        return d.get("tool_name") or d.get("tool") or "tool"

    def _live_update(self) -> None:
        """진행 중 사고(_think_buf)+중간텍스트(_proc_buf)를 스트리밍 위젯에 회색으로 표시."""
        w = self._stream_widget()
        w.display = True
        combined = (("💭 " + self._think_buf + "\n") if self._think_buf else "") + self._proc_buf
        w.update(Text(combined, style="dim"))

    def _think_live(self, delta: str) -> None:
        if delta:
            self._think_buf += delta
            self._live_update()

    def _proc_live(self, delta: str) -> None:
        if delta:
            self._proc_buf += delta
            self._live_update()

    def _flush_proc(self, *, final: bool = False, text: str | None = None,
                    record: bool = True) -> None:
        """누적 확정 — 사고(_think_buf)는 항상 회색, 어시스턴트 텍스트는 final 이면 흰색(최종답변).

        추론(thinking)과 답변을 분리 보관하므로, 도구 없이 바로 답해도 사고=회색·답변=흰색.
        """
        think = self._think_buf.strip()
        ans = (text if text is not None else self._proc_buf).strip()
        self._clear_stream()
        if think:
            # 모델 사고 = 회색, 기본 접힘(§36 I7 — ctrl+o 로 전문)
            self._chat_add(ThinkBlock(think, expanded=self.verbose))
        if not ans:
            return
        if final:
            self.add_assistant(ans)                   # 최종 답변 = 마크다운(§36 D3)
            if record:
                self._record("assistant", ans)
        else:
            self._log(f"[dim]  ┊ {ans}[/]")           # 중간 서술 = 회색

    def _tool_block_done(self, name: str, *, ok: bool, preview: str = "") -> None:
        """실행 중 ToolBlock 을 제자리에서 완료/실패로 갱신(§36 B1).

        시작 이벤트를 못 본 경우(라이브 중간 진입 등)엔 종결 상태 블록을 새로 추가한다.
        """
        for tb in reversed(self._open_tools):
            if tb.tool == name and tb.running:
                tb.complete(preview, expanded=self.verbose) if ok else tb.fail()
                self._open_tools.remove(tb)
                return
        tb = ToolBlock(name)
        tb.complete(preview, expanded=self.verbose) if ok else tb.fail()
        self._chat_add(tb)

    def _render_run_event(self, name: str, d: dict, *, record: bool = True) -> None:
        """모델의 사고 과정(추론·중간 서술)은 회색으로 누적, 도구는 in-place 블록,
        최종 답변만 마크다운으로 확정한다.

        핵심: 어시스턴트 텍스트는 일단 누적하고 — 도구가 시작되면 그건 '근거/사고'였으므로
        회색으로 확정하고, 턴이 완료되면(assistant.completed) 그게 '최종 답변'이므로 본색으로.
        추론 모델의 thinking(`_thinking`/`reasoning.available`)도 회색으로 표시한다.
        """
        if name in ("assistant.delta", "message.delta"):
            self._proc_live(self._ev_text(d))
        elif (name == "tool.progress" and self._ev_tool(d) == "_thinking") \
                or name in ("reasoning.available", "reasoning", "message.reasoning", "thinking"):
            self._think_live(self._ev_text(d))        # 모델 사고(thinking) = 회색(별도 버퍼)
        elif name == "tool.progress":
            tn = self._ev_tool(d)
            if tn and tn != "tool":
                self._status(f"🧠 {tn}")
        elif name == "tool.started":
            self._flush_proc(record=record)           # 도구 직전 텍스트 = 사고 → 회색 확정
            tb = ToolBlock(self._ev_tool(d))
            self._open_tools.append(tb)
            self._chat_add(tb)
        elif name == "tool.completed":
            # 원문을 그대로 넘긴다 — 표시 컷(1줄 64자/상세 전문)은 ToolBlock 이 담당(§36 I7)
            self._tool_block_done(self._ev_tool(d), ok=True, preview=d.get("preview") or "")
        elif name == "tool.failed":
            self._tool_block_done(self._ev_tool(d), ok=False)
        elif name in ("assistant.completed", "message.completed"):
            self._flush_proc(final=True, text=(d.get("content") or None), record=record)
        elif name == "error":
            self._flush_proc(record=record)
            self._log(f"[{_ERR}]오류: {(d.get('message') or '')[:200]}[/]")
