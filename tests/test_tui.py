"""전용 Alphred TUI 스모크 — Textual 헤드리스 마운트(컴포즈/CSS/on_mount 검증)."""
from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("textual")
from alphred.tui import AlphredTUI  # noqa: E402


def test_tui_mounts_without_gateway():
    """게이트웨이가 없어도(연결 실패) 크래시 없이 마운트되고 위젯이 구성된다."""
    async def go():
        # 도달 불가 포트 → refresh_queue 가 예외를 흡수해야 함
        app = AlphredTUI("http://localhost:59999", None)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.query_one("#chat") is not None
            assert app.query_one("#statusbar") is not None   # §36 T3: 상주 큐 패널 폐지
            assert app.query_one("#prompt") is not None
        await app.http.aclose()

    asyncio.run(go())


def test_tui_state_label():
    from alphred.tui_base import _state_label
    assert "완료" in _state_label("Completed")
    assert "폐기" in _state_label("Discarded")
    assert "검토" in _state_label("NeedsReview")   # §21
    assert _state_label("Weird") == "Weird"


def test_tui_evidence_panel_renders(monkeypatch):
    """검증 증거 패널(§21): verify_report 의 체크 결과를 표시한다."""
    async def go():
        app = AlphredTUI("http://localhost:59999", None)
        async with app.run_test() as pilot:
            await pilot.pause()
            logged = []
            monkeypatch.setattr(app, "_log", lambda m: logged.append(str(m)))
            app._render_verify_report({
                "passed": False, "checked": 1, "summary": "산출물 1개 중 1개 미통과",
                "checks": [{"check": "file", "target": "C:/x/out.pdf", "ok": False,
                            "detail": "pdf 형식 시그니처 불일치", "exists": True, "nonempty": True}],
            })
            blob = "\n".join(logged)
            assert "검증" in blob and "out.pdf" in blob and "불일치" in blob
            # Tier2 judge 결과도 증거 패널에 표시
            logged.clear()
            app._render_verify_report({
                "passed": True, "checked": 0, "checks": [], "summary": "검증 안 함",
                "judge": {"passed": False, "score": 40, "summary": "근거 부족",
                          "criteria": [{"name": "출처 포함", "met": False, "note": "없음"}],
                          "unmet": ["출처 누락"]},
            })
            jblob = "\n".join(logged)
            assert "judge" in jblob and "40" in jblob and "출처" in jblob
        await app.http.aclose()

    asyncio.run(go())


def test_tui_queue_deck_opens_and_lists(monkeypatch):
    """§36 Q4: ctrl+t → 큐 덱 모달, /queue 응답을 리스트+실행 슬롯으로 표시, Esc 닫기."""
    from alphred.tui_queue import QueueDeck

    tasks = {"tasks": [
        {"id": "run1aaaa", "state": "In-Progress", "priority": 5, "prompt": "리포트 생성",
         "session_key": "s1"},
        {"id": "run2bbbb", "state": "Pending", "priority": 9, "prompt": "DB 마이그레이션",
         "session_key": "s1"}]}

    async def go():
        app = AlphredTUI("http://localhost:59999", None)
        async with app.run_test() as pilot:
            await pilot.pause()

            async def fake_get(path, *a, **k):
                class _R:
                    def json(self):
                        return tasks if path == "/queue" else {}
                return _R()
            monkeypatch.setattr(app.http, "get", fake_get)
            app.action_queue_deck()
            await pilot.pause()
            assert isinstance(app.screen, QueueDeck)
            await pilot.pause()
            from textual.widgets import OptionList, Static
            ol = app.screen.query_one("#deck-list", OptionList)
            assert ol.option_count == 2
            slot = str(app.screen.query_one("#deck-slot", Static).content)
            assert "실행 슬롯" in slot and "run1aaaa" in slot   # 점유 중
            assert "run2bbbb" in slot                           # 대기 1순위(우선 9)
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, QueueDeck)
        await app.http.aclose()

    asyncio.run(go())


def test_task_card_markup_states():
    """§36 Q2 TaskCard 마크업(순수 함수) — 접수/진행/선점/완료 상태별 표현."""
    from alphred.tui_base import progress_bar, step_progress, task_card_markup
    # 진행바·스텝 진행 헬퍼
    assert progress_bar(2, 4, 8) == "▰▰▰▰▱▱▱▱" and progress_bar(0, 0) == ""
    plan = {"steps": [{"state": "done"}, {"state": "running", "goal": "데이터 수집"},
                      {"state": "pending"}]}
    assert step_progress(plan) == (1, 3, "데이터 수집")
    assert step_progress({"steps": [{"goal": "x"}]}) == (0, 0, None)   # state 없으면 폴백
    # 상태별 카드
    pend = task_card_markup({"id": "abcd1234", "state": "Pending", "prompt": "보고서",
                             "priority": 4, "depth": "high",
                             "estimate": {"est_llm_calls": 9}, "plan": {"dod": "PDF 산출"}})
    assert "대기" in pend and "~9콜" in pend and "DoD" in pend
    run = task_card_markup({"id": "abcd1234", "state": "In-Progress", "prompt": "보고서",
                            "plan": plan})
    assert "▰" in run and "2/3" not in run and "1/3" in run and "데이터 수집" in run
    paused = task_card_markup({"id": "abcd1234", "state": "Paused", "prompt": "보고서"})
    assert "보류" in paused and "선점" in paused             # Q-P4 선점 서사
    done = task_card_markup({"id": "abcd1234", "state": "Completed", "prompt": "보고서",
                             "result": "완료된 보고서 내용",
                             "verify_report": {"passed": True, "judge": {"score": 82}}})
    assert "완료" in done and "검증 ✓" in done and "82" in done


def test_tui_multiline_input_and_submit():
    """Shift+Enter 는 줄바꿈, Enter 는 전송(입력창 비워짐). Ctrl+J 는 호환 폴백."""
    from alphred.tui import PromptInput

    async def go():
        app = AlphredTUI("http://localhost:59999", None)
        async with app.run_test() as pilot:
            await pilot.pause()
            inp = app.query_one("#prompt", PromptInput)
            inp.focus()
            await pilot.pause()
            await pilot.press("a")
            await pilot.press("shift+enter")   # 줄바꿈(전송 아님)
            await pilot.press("b")
            await pilot.press("ctrl+j")         # 폴백 줄바꿈
            await pilot.press("c")
            await pilot.pause()
            assert inp.text == "a\nb\nc"        # 세 줄 유지
            await pilot.press("enter")          # 전송 → 입력창 비워짐
            await pilot.pause()
            assert inp.text == ""
        await app.http.aclose()

    asyncio.run(go())


def test_tui_splash_banner_rendered():
    """시작 시 ASCII 배너가 채팅 로그에 그려진다(크래시 없음)."""
    from alphred.splash import banner_lines

    async def go():
        app = AlphredTUI("http://localhost:59999", None)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert len(banner_lines()) >= 5
        await app.http.aclose()

    asyncio.run(go())


def test_tui_welcome_panel_and_banner_command():
    """§36 D2: 시작 화면은 컴팩트 웰컴 패널, 전체 배너 아트는 /banner 로 보존."""
    from textual.widgets import Static
    from alphred.splash import logo_lines

    assert len(logo_lines()) >= 4        # 아트 자산 자체는 보존

    async def go():
        app = AlphredTUI("http://localhost:59999", None)
        async with app.run_test(size=(180, 40)) as pilot:
            await pilot.pause()
            assert isinstance(app._welcome_widget, Static)   # 웰컴 패널 마운트됨
            assert app._welcome_widget.is_attached
            plain = app._welcome_renderable().plain
            assert "Alphred" in plain and "서버" in plain and "/banner" in plain
            n_before = len(list(app._chat().children))
            app.cmd_banner("")             # 배너 아트 위젯 1개 추가(크래시 없음)
            await pilot.pause()
            assert len(list(app._chat().children)) == n_before + 1
        await app.http.aclose()

    asyncio.run(go())


def test_tui_quit_binding_is_ctrl_q():
    """종료는 ctrl+q (ctrl+c 는 터미널 네이티브 복사/인터럽트에 양보)."""
    keys = {k for (k, *_rest) in AlphredTUI.BINDINGS}
    assert "ctrl+q" in keys
    assert "ctrl+c" not in keys


def test_responsive_banner_picks_variant_by_width():
    """폭에 따라 full(ALPHRED-AGENT)/half(ALPHRED)/mini(A) 배너를 고른다."""
    from alphred.splash import pick_banner, _W_FULL, _W_HALF, _W_MINI
    assert pick_banner(200)[1] == _W_FULL      # 넓으면 full
    assert pick_banner(_W_FULL)[1] == _W_FULL
    assert pick_banner(_W_FULL - 1)[1] == _W_HALF   # 조금 좁으면 half
    assert pick_banner(_W_HALF)[1] == _W_HALF
    assert pick_banner(_W_HALF - 1)[1] == _W_MINI   # 많이 좁으면 mini
    assert pick_banner(3)[1] == _W_MINI


def test_responsive_logo_picks_variant_by_height():
    """남은 세로 공간에 따라 100%/75%/50% 로고 변형을 고르고, 너무 낮으면 생략한다."""
    from alphred.splash import pick_logo, _LOGO_VARIANTS
    (fw, fh), mh = _LOGO_VARIANTS[0][1:], _LOGO_VARIANTS[-1][2]
    assert pick_logo(200, 999)[2] == fh           # 충분히 높으면 100%
    assert pick_logo(200, fh)[2] == fh
    assert pick_logo(200, fh - 1)[2] < fh          # 조금 낮으면 더 작은 변형
    assert pick_logo(200, mh)[2] == mh             # 최소 변형은 들어감
    assert pick_logo(200, mh - 1)[0] == []         # 그보다 낮으면 생략
    assert pick_logo(fw - 1, 999)[2] < fh          # 폭이 좁아도 더 작은 변형으로


def test_tui_narrow_screen_no_crash():
    """좁은 화면에서도 스플래시 렌더가 깨지지 않는다(반응형)."""
    async def go():
        app = AlphredTUI("http://localhost:59999", None)
        async with app.run_test(size=(70, 18)) as pilot:   # 좁고 낮음 → half/ logo 생략
            await pilot.pause()
        await app.http.aclose()

    asyncio.run(go())


def test_tui_plan_command_dryrun(monkeypatch):
    """/plan 드라이런: 심화도·견적·계획을 표시(§21 V3, 크래시 없음)."""
    class _Resp:
        def json(self):
            return {"kind": "heavy", "priority": 4, "depth": "high",
                    "classify_reason": "plan: 3 steps",
                    "estimate": {"steps": 3, "tool_steps": 2, "est_llm_calls": 7, "band": "높음"},
                    "plan": {"subtasks": [{"title": "조사", "kind": "search",
                                           "effort": "moderate", "tools": ["web_search"]}]}}

    async def go():
        app = AlphredTUI("http://localhost:59999", None)
        async with app.run_test() as pilot:
            await pilot.pause()
            logged = []
            monkeypatch.setattr(app, "_log", lambda m: logged.append(str(m)))

            async def fake_post(path, *a, **k):
                assert path == "/plan"
                return _Resp()
            monkeypatch.setattr(app.http, "post", fake_post)
            await app.cmd_plan("보고서 만들어줘")
            blob = "\n".join(logged)
            assert "드라이런" in blob and "high" in blob and "~7회" in blob and "조사" in blob
        await app.http.aclose()

    asyncio.run(go())


def test_tui_skills_command_lists(monkeypatch):
    """/skills 가 게이트웨이 /v1/skills 응답을 받아 이름/카테고리를 표시(크래시 없음)."""
    class _Resp:
        def json(self):
            return {"object": "list", "data": [
                {"name": "research", "description": "리서치 워크플로", "category": "research"},
                {"name": "nano-pdf", "description": "PDF 편집", "category": "productivity"},
            ]}

    async def go():
        app = AlphredTUI("http://localhost:59999", None)
        async with app.run_test() as pilot:
            await pilot.pause()
            logged = []
            monkeypatch.setattr(app, "_log", lambda m: logged.append(m))

            async def fake_get(path, *a, **k):
                assert path == "/v1/skills"
                return _Resp()
            monkeypatch.setattr(app.http, "get", fake_get)
            await app.cmd_skills("")
            blob = "\n".join(logged)
            assert "설치된 스킬 2개" in blob and "research" in blob and "nano-pdf" in blob
        await app.http.aclose()

    asyncio.run(go())


def test_tui_session_shown_in_statusbar(tmp_path):
    """§36 B3: 현재 세션 ID(이름이 아니라)가 상태줄에 표시된다."""
    from textual.widgets import Static
    from alphred.tui import _short_sid

    async def go():
        app = AlphredTUI("http://localhost:59999", None, sessions_dir=tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._session["title"] = "주식 보고서"      # 제목이 있어도
            app._set_titlebar()
            bar = str(app.query_one("#statusbar", Static).content)
            assert "세션" in bar
            assert _short_sid(app.session_id) in bar     # 이름이 아니라 단축 ID 표시
            assert "주식 보고서" not in bar
        await app.http.aclose()

    asyncio.run(go())


def test_tui_model_shown_in_statusbar():
    """§36 B3: 현재 모델(단축명)이 상태줄에 표시된다."""
    from textual.widgets import Static

    async def go():
        app = AlphredTUI("http://localhost:59999", None)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.model = "meta/llama-3.3-70b-instruct"
            await app._update_model_display()
            bar = str(app.query_one("#statusbar", Static).content)
            assert "모델" in bar and "llama-3.3-70b" in bar
            assert "depth:" in bar                       # 심화도 오버라이드 상시 표시
        await app.http.aclose()

    asyncio.run(go())


def test_tui_session_persist_and_restore(tmp_path):
    """세션 영속화: 대화 기록을 저장하고, 새 인스턴스가 직전 세션을 복원한다."""
    from alphred.tui_sessions import SessionStore

    async def first():
        app = AlphredTUI("http://localhost:59999", None, sessions_dir=tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._record("user", "첫 메시지")
            app._record("assistant", "응답입니다")
            sid = app.session_id
        await app.http.aclose()
        return sid

    sid = asyncio.run(first())
    # 디스크에 저장됐는지
    store = SessionStore(tmp_path)
    saved = store.load(sid)
    assert saved and len(saved["messages"]) == 2
    assert saved["title"] == "첫 메시지"

    async def second():
        app = AlphredTUI("http://localhost:59999", None, sessions_dir=tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            # 직전 세션이 복원되어 같은 session_id 를 잇는다
            assert app.session_id == sid
            assert app._session and len(app._session["messages"]) == 2
        await app.http.aclose()

    asyncio.run(second())


def test_tui_sessions_command_lists(tmp_path):
    """/sessions 가 저장된 세션을 나열한다(크래시 없음)."""
    async def go():
        app = AlphredTUI("http://localhost:59999", None, sessions_dir=tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._record("user", "세션A 메시지")
            await app.cmd_sessions("")          # 목록
            await app.cmd_sessions("1")         # 1번으로 전환
        await app.http.aclose()

    asyncio.run(go())


def test_session_short_id_and_resolution():
    """세션 단축 ID(접두사 제거) + 번호/ID 양쪽으로 세션을 찾는다."""
    from alphred.tui import _short_sid, _new_sid, AlphredTUI
    assert _short_sid("alphred-tui-a1b2c3d4") == "a1b2c3d4"   # 고유 부분만 (기존 id[:8]='alphred-' 버그 수정)
    assert _new_sid().startswith("alphred-tui-")
    items = [{"id": "alphred-tui-aaaa1111", "title": "A"},
             {"id": "alphred-tui-bbbb2222", "title": "B"}]
    r = AlphredTUI._resolve_session
    assert r(items, "1")["title"] == "A"            # 번호
    assert r(items, "bbbb")["title"] == "B"          # 단축 ID prefix
    assert r(items, "alphred-tui-aaaa")["title"] == "A"  # 전체 ID prefix
    assert r(items, "9") is None and r(items, "zzz") is None


def test_tui_sessions_delete(tmp_path):
    """/sessions delete <n> 가 세션을 삭제하고, 현재 세션 삭제 시 새 세션을 시작한다."""
    from alphred.tui_sessions import SessionStore

    async def go():
        app = AlphredTUI("http://localhost:59999", None, sessions_dir=tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._record("user", "삭제될 세션")   # 현재 세션 저장
            cur = app.session_id
            await app.cmd_sessions("delete 1")   # 현재(유일) 세션 삭제
            assert SessionStore(tmp_path).load(cur) is None   # 디스크에서 제거됨
            assert app.session_id != cur                       # 새 세션으로 전환됨
        await app.http.aclose()

    asyncio.run(go())


def test_tui_clear_keeps_session_new_resets(tmp_path):
    """/clear 는 화면만 비우고 현재 세션·대화 맥락 유지, /new 는 새 세션을 연다."""
    async def go():
        app = AlphredTUI("http://localhost:59999", None, sessions_dir=tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._record("user", "이전 맥락")
            sid = app.session_id
            app.cmd_clear("")
            assert app.session_id == sid                       # 세션 유지
            assert app._session and len(app._session["messages"]) == 1  # 맥락 보존
            app.cmd_new("")
            assert app.session_id != sid                       # 새 세션
            assert not app._session.get("messages")            # 대화 비워짐
        await app.http.aclose()

    asyncio.run(go())


def test_tui_input_history_navigation():
    """↑/↓ 입력 히스토리: 제출한 입력을 거슬러 호출하고 작성 중이던 초안을 복원한다."""
    from alphred.tui import PromptInput

    async def go():
        app = AlphredTUI("http://localhost:59999", None)
        async with app.run_test() as pilot:
            await pilot.pause()
            inp = app.query_one("#prompt", PromptInput)
            inp.text = "/help"; app.submit_current()           # 명령(네트워크 없음)
            inp.text = "/clear"; app.submit_current()
            await pilot.pause()
            assert app._history == ["/help", "/clear"]
            inp.text = "작성중"                                # 진입 전 초안
            assert app.history_prev() and inp.text == "/clear"
            assert app.history_prev() and inp.text == "/help"
            assert app.history_prev() and inp.text == "/help"  # 더 위 없음(유지)
            assert app.history_next() and inp.text == "/clear"
            assert app.history_next() and inp.text == "작성중"  # 끝을 넘으면 초안 복원
            assert app.history_next() is False                 # 탐색 종료 상태
        await app.http.aclose()

    asyncio.run(go())


def test_tui_queue_op_shared_helper(monkeypatch):
    """_queue_op 공용 헬퍼: 각 op 가 올바른 게이트웨이 엔드포인트를 호출한다(슬래시·키보드 공용)."""
    async def go():
        app = AlphredTUI("http://localhost:59999", None)
        async with app.run_test() as pilot:
            await pilot.pause()
            calls = []

            class _R:
                def json(self):
                    return {}

            async def fake_post(path, *a, **k):
                calls.append(("POST", path, k.get("json")))
                return _R()

            async def fake_req(method, path, *a, **k):
                calls.append((method, path))
                return _R()

            monkeypatch.setattr(app.http, "post", fake_post)
            monkeypatch.setattr(app.http, "request", fake_req)
            assert "일시중지" in await app._queue_op("pause", "abcd1234")
            assert "우선순위" in await app._queue_op("prio", "abcd1234", priority=7)
            assert "폐기" in await app._queue_op("cancel", "abcd1234")
            assert ("POST", "/queue/abcd1234/pause", None) in calls
            assert ("POST", "/queue/abcd1234/prio", {"priority": 7}) in calls
            assert ("DELETE", "/queue/abcd1234") in calls
        await app.http.aclose()

    asyncio.run(go())


def test_tui_queue_ask_natural_language(monkeypatch):
    """/queue ask 가 POST /queue/ask 로 자연어 요청을 보내고 reply/results 를 표시한다(③)."""
    async def go():
        app = AlphredTUI("http://localhost:59999", None)
        async with app.run_test() as pilot:
            await pilot.pause()
            logged, posted = [], {}
            monkeypatch.setattr(app, "_log", lambda m: logged.append(str(m)))

            async def fake_post(path, json=None, **k):
                posted["path"], posted["json"] = path, json

                class _R:
                    def json(self):
                        return {"reply": "우선순위를 올렸습니다",
                                "results": ["우선순위 변경: abcd1234 → 9"]}
                return _R()

            async def fake_get(path, *a, **k):
                class _R:
                    def json(self):
                        return {"tasks": []}
                return _R()

            monkeypatch.setattr(app.http, "post", fake_post)
            monkeypatch.setattr(app.http, "get", fake_get)
            await app.cmd_queue('ask 리포트 우선순위 올려줘')
            assert posted["path"] == "/queue/ask"
            assert posted["json"] == {"q": "리포트 우선순위 올려줘"}
            blob = "\n".join(logged)
            assert "우선순위를 올렸습니다" in blob and "abcd1234" in blob
        await app.http.aclose()

    asyncio.run(go())


def test_tui_render_run_event_toolblock_and_markdown():
    """§36 B1/D3 렌더러: 도구=ToolBlock(제자리 갱신), 생각=ThinkBlock, 최종 답변=마크다운."""
    from alphred.tui_base import AssistantMd, ThinkBlock, ToolBlock

    async def go():
        app = AlphredTUI("http://localhost:59999", None)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._render_run_event("tool.started", {"tool_name": "terminal"})
            await pilot.pause()
            blocks = list(app.query(ToolBlock))
            assert blocks and blocks[-1].tool == "terminal" and blocks[-1].running
            app._render_run_event("tool.completed", {"tool_name": "terminal", "preview": "ok"})
            assert not blocks[-1].running                       # 같은 위젯이 제자리 갱신됨
            app._render_run_event("reasoning", {"text": "먼저 파일을 만든다"})
            app._render_run_event("assistant.completed", {"content": "최종 **결과**입니다"},
                                  record=False)
            await pilot.pause()
            mds = list(app.query(AssistantMd))
            assert mds and "최종 **결과**입니다" in mds[-1].source_text   # 최종=마크다운
            ths = list(app.query(ThinkBlock))
            assert ths and "먼저 파일을 만든다" in ths[-1].full           # 생각=ThinkBlock
            assert "💭" in ths[-1]._markup() and "[dim]" in ths[-1]._markup()  # 회색
        await app.http.aclose()

    asyncio.run(go())


def test_tui_thinking_rendered_gray():
    """모델 사고(_thinking / reasoning) 델타가 ThinkBlock(회색)으로, 최종 답변과 구분된다."""
    from alphred.tui_base import AssistantMd, ThinkBlock

    async def go():
        app = AlphredTUI("http://localhost:59999", None)
        async with app.run_test() as pilot:
            await pilot.pause()
            # 추론 모델식 흐름: _thinking 델타 → 도구 → 최종 답변
            app._render_run_event("tool.progress",
                                  {"tool_name": "_thinking", "delta": "cd ~ 가 홈으로 가는 명령"})
            app._render_run_event("tool.started", {"tool_name": "terminal"})
            app._render_run_event("assistant.completed", {"content": "cd ~ 입니다"}, record=False)
            await pilot.pause()
            ths = list(app.query(ThinkBlock))
            assert ths and "cd ~ 가 홈으로" in ths[-1].full                # 사고=ThinkBlock(회색)
            mds = list(app.query(AssistantMd))
            assert mds and "cd ~ 입니다" in mds[-1].source_text            # 최종=마크다운
        await app.http.aclose()

    asyncio.run(go())


def test_queue_badges_and_model_short():
    """§36 Q1 상태줄 배지(순수 함수) + 모델 단축명."""
    from alphred.tui_base import model_short, queue_badges
    tasks = [{"state": "In-Progress"}, {"state": "Pending"}, {"state": "Paused"},
             {"state": "AwaitingInput"}, {"state": "NeedsReview"}, {"state": "Completed"}]
    b = queue_badges(tasks)
    assert "▶1" in b and "⏳2" in b and "❓1" in b and "⚠1" in b
    assert queue_badges([]) == "[dim]큐 —[/]"                    # 활성 없음
    assert queue_badges([{"state": "Completed"}]) == "[dim]큐 —[/]"
    assert model_short("google/gemma-4-31b-it  [nvidia]") == "gemma-4-31b-it"
    assert model_short(None) == ""


def test_tui_live_view_start_stop_no_crash():
    """라이브 뷰: 도달 불가 스트림에도 크래시 없이 시작/중단(Esc)."""
    async def go():
        app = AlphredTUI("http://localhost:59999", None)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._start_live("abcd1234")           # 스트림 실패는 워커가 흡수
            await pilot.pause()
            assert app._live_tid == "abcd1234"
            app.stop_live()
            assert app._live_tid is None and app._live_worker is None
        await app.http.aclose()

    asyncio.run(go())


def test_tui_esc_cancels_send(monkeypatch):
    """§36 I1: 응답 중 Esc → 워커 취소 + 부분 출력 회색 확정 + 중단 안내."""
    async def go():
        app = AlphredTUI("http://localhost:59999", None)
        async with app.run_test() as pilot:
            await pilot.pause()
            logged = []
            monkeypatch.setattr(app, "_log", lambda m: logged.append(str(m)))

            class _W:
                cancelled = False

                def cancel(self):
                    self.cancelled = True

            w = _W()
            app._busy = True
            app._send_worker = w
            app._proc_buf = "부분 출력"
            app.query_one("#prompt").focus()
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            assert w.cancelled
            blob = "\n".join(logged)
            assert "중단" in blob and "부분 출력" in blob   # 부분 출력은 회색 확정
        await app.http.aclose()

    asyncio.run(go())


def test_tui_busy_input_queued_and_drained():
    """§36 I2: 응답 중 제출 → 대기열 보관, 응답 종료 시 자동 전송."""
    async def go():
        app = AlphredTUI("http://localhost:59999", None)
        async with app.run_test() as pilot:
            await pilot.pause()
            sent = []
            app.send = lambda m: sent.append(m) or "worker"   # 전송 기록용 스텁
            app._busy = True
            inp = app.query_one("#prompt")
            inp.text = "다음 질문"
            app.submit_current()
            assert app._pending_msgs == ["다음 질문"] and not sent   # 즉시 전송 안 함
            app._busy = False
            app._drain_pending()
            assert sent == ["다음 질문"] and not app._pending_msgs   # 종료 후 자동 전송
        await app.http.aclose()

    asyncio.run(go())


def test_tui_question_card_flow(monkeypatch):
    """§36 I3 QuestionCard: ✦추천 기본 하이라이트, 선택→다음 질문, 완료 시 카드 제거."""
    from alphred.tui_base import QuestionCard

    qs = [{"q": "형식은?", "options": [{"label": "md"}, {"label": "pdf", "recommended": True}]},
          {"q": "분량은?", "options": [{"label": "짧게", "recommended": True}, {"label": "길게"}]}]

    async def go():
        app = AlphredTUI("http://localhost:59999", None)
        async with app.run_test() as pilot:
            await pilot.pause()
            captured = {}

            async def fake_post_answers(st):
                captured.update(st)
            monkeypatch.setattr(app, "_post_answers", fake_post_answers)
            app._begin_answer_mode({"id": "t1", "questions": qs})
            await pilot.pause()
            card = app.query_one(QuestionCard)
            assert card._opts.highlighted == 1            # ✦ 추천(pdf)이 기본 하이라이트
            app.answer_pick(0)                            # 1번 질문: md 선택
            await pilot.pause()
            assert app._pending_input["idx"] == 1         # 같은 카드가 2번 질문으로
            app.answer_submit("")                          # 빈 입력 = 추천(짧게) 채택
            await pilot.pause()
            assert app._pending_input is None              # 답변 모드 종료
            assert not list(app.query(QuestionCard))       # 카드 제거됨
            assert [a["answer"] for a in captured["answers"]] == ["md", "짧게"]
        await app.http.aclose()

    asyncio.run(go())


def test_tui_question_card_esc_defers():
    """§36 I3: 카드에서 Esc → 보류(카드 제거, 타임아웃 시 추천 가정은 서버 몫)."""
    from alphred.tui_base import QuestionCard

    async def go():
        app = AlphredTUI("http://localhost:59999", None)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._begin_answer_mode({"id": "t1", "questions": [
                {"q": "형식은?", "options": [{"label": "md", "recommended": True}]}]})
            await pilot.pause()
            assert app.query_one(QuestionCard) is not None
            await pilot.press("escape")                    # 카드 OptionList 포커스 상태
            await pilot.pause()
            assert app._pending_input is None
            assert not list(app.query(QuestionCard))
        await app.http.aclose()

    asyncio.run(go())


def test_tui_fuzzy_palette_and_arg_candidates(tmp_path):
    """§36 I4: fuzzy 명령 매칭 + /model·/depth·/sessions·/queue 인자 후보."""
    from alphred.tui_base import fuzzy_match
    assert fuzzy_match("mdl", "model") and not fuzzy_match("xz", "model")

    async def go():
        app = AlphredTUI("http://localhost:59999", None, sessions_dir=tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            # fuzzy: "/dt" → depth (prefix 아님, 부분열 일치)
            inp = app.query_one("#prompt")
            inp.text = "/dt"
            await pilot.pause()
            assert app.palette_visible()
            ids = [app._palette().get_option_at_index(i).id
                   for i in range(app._palette().option_count)]
            assert ids == ["depth"]
            # 인자 후보
            app._model_names = ["google/gemma-4-31b-it", "meta/llama-3.3-70b-instruct"]
            assert [c[1] for c in app._arg_candidates("depth", "l")] == ["/depth low"]
            assert app._arg_candidates("model", "gemma") == \
                [("google/gemma-4-31b-it", "/model google/gemma-4-31b-it")]
            app._rows = [("abcd1234efgh5678", 5, "Pending")]
            assert app._arg_candidates("queue", "cancel ab") == \
                [("abcd1234  (Pending)", "/queue cancel abcd1234")]
            subs = [c[0] for c in app._arg_candidates("queue", "")]
            assert "cancel" in subs and "ask" in subs
            app._record("user", "세션 후보 확인")            # 세션 1개 저장
            sess = app._arg_candidates("sessions", "")
            assert sess and sess[0][1] == "/sessions 1"
            # 인자 팔레트가 입력창 타이핑으로 뜨는지("/depth l")
            inp.text = "/depth l"
            await pilot.pause()
            assert app.palette_visible()
            assert app._palette().get_option_at_index(0).id == "arg:/depth low"
        await app.http.aclose()

    asyncio.run(go())


def test_tui_cycle_depth_and_statusbar():
    """§36 I6: shift+tab 순환 auto→low→mid→high→auto + 상태줄 반영."""
    from textual.widgets import Static

    async def go():
        app = AlphredTUI("http://localhost:59999", None)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.depth_override is None
            app.query_one("#prompt").focus()
            await pilot.press("shift+tab")
            assert app.depth_override == "low"
            bar = str(app.query_one("#statusbar", Static).content)
            assert "depth:" in bar and "low" in bar
            for expect in ("mid", "high", None):
                app.action_cycle_depth()
                assert app.depth_override == expect
        await app.http.aclose()

    asyncio.run(go())


def test_tui_verbose_toggle_expands_blocks():
    """§36 I7: ctrl+o — ThinkBlock/ToolBlock 이 접힘(요약)↔전문으로 제자리 갱신."""
    from alphred.tui_base import ThinkBlock, ToolBlock

    async def go():
        app = AlphredTUI("http://localhost:59999", None)
        async with app.run_test() as pilot:
            await pilot.pause()
            long_think = "긴 사고 과정 " * 20
            app._render_run_event("reasoning", {"text": long_think})
            app._render_run_event("tool.started", {"tool_name": "terminal"})
            app._render_run_event("tool.completed",
                                  {"tool_name": "terminal", "preview": "줄1\n줄2\n줄3"})
            app._render_run_event("assistant.completed", {"content": "끝"}, record=False)
            await pilot.pause()
            tb = list(app.query(ToolBlock))[-1]
            th = list(app.query(ThinkBlock))[-1]
            assert not tb.expanded and not th.expanded
            assert "ctrl+o" in th._markup()               # 접힘 요약에 힌트
            assert "줄2" not in tb._markup() or "줄1 줄2" in tb._markup()  # 1줄 압축
            app.action_toggle_verbose()
            assert app.verbose and tb.expanded and th.expanded
            assert "줄2" in tb._markup() and long_think.strip()[:30] in th._markup()
        await app.http.aclose()

    asyncio.run(go())


def test_tui_session_picker_modal(tmp_path):
    """§36 I5: /sessions → 피커 모달 표시, Esc 닫기, switch 콜백으로 세션 전환."""
    from alphred.tui_commands import SessionPicker

    async def go():
        app = AlphredTUI("http://localhost:59999", None, sessions_dir=tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._record("user", "피커 대상 세션")
            await app.cmd_sessions("")
            await pilot.pause()
            assert isinstance(app.screen, SessionPicker)   # 모달이 떠 있음
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, SessionPicker)
            # switch 콜백 경로
            target = app._sessions.list()[0]
            app._session_picked(("switch", target))
            assert app.session_id == target["id"]
        await app.http.aclose()

    asyncio.run(go())


def test_tui_task_card_polling_updates_and_finalizes(monkeypatch):
    """§36 Q2: queued → 인라인 카드 생성, 폴링으로 제자리 갱신, 종결 시 갱신 중단."""
    from alphred.tui_base import TaskCard

    seq = [
        {"tasks": [{"id": "runx1234", "state": "In-Progress", "prompt": "보고서",
                    "plan": {"steps": [{"state": "done"}, {"state": "running", "goal": "집계"}]}}]},
        {"tasks": [{"id": "runx1234", "state": "Completed", "prompt": "보고서",
                    "result": "끝", "verify_report": {"passed": True}}]},
    ]

    async def go():
        app = AlphredTUI("http://localhost:59999", None)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._handle_event("queued", {"id": "runx1234"})   # 카드 생성
            await pilot.pause()
            cards = list(app.query(TaskCard))
            assert cards and "runx1234" in app._task_cards

            calls = {"i": 0}

            async def fake_get(path, *a, **k):
                class _R:
                    def json(self):
                        return seq[min(calls["i"], len(seq) - 1)]
                return _R()
            monkeypatch.setattr(app.http, "get", fake_get)
            await app.refresh_queue()                          # In-Progress → 카드 유지
            assert "runx1234" in app._task_cards
            assert "집계" in str(cards[0].content)
            calls["i"] = 1
            await app.refresh_queue()                          # Completed → 카드 종결
            assert "runx1234" not in app._task_cards           # 폴링 갱신 중단
            assert "완료" in str(cards[0].content)              # 마지막 렌더는 완료
        await app.http.aclose()

    asyncio.run(go())


def test_tui_answer_command_summons_card(monkeypatch):
    """§36 Q3: /answer → 답변 대기(❓) 작업의 질문 카드를 소환한다."""
    from alphred.tui_base import QuestionCard

    tasks = {"tasks": [{"id": "awaitzz1", "state": "AwaitingInput", "created_at": "2026-01-01",
                        "questions": [{"q": "형식은?",
                                       "options": [{"label": "md", "recommended": True}]}]}]}

    async def go():
        app = AlphredTUI("http://localhost:59999", None)
        async with app.run_test() as pilot:
            await pilot.pause()

            async def fake_get(path, *a, **k):
                class _R:
                    def json(self):
                        return tasks
                return _R()
            monkeypatch.setattr(app.http, "get", fake_get)
            await app.cmd_answer("")
            await pilot.pause()
            assert app._pending_input and app._pending_input["task_id"] == "awaitzz1"
            assert list(app.query(QuestionCard))
        await app.http.aclose()

    asyncio.run(go())


def test_tui_transition_toast_and_bell(monkeypatch):
    """§36 Q5: 타 세션 작업의 완료/검토/폐기 전이에 토스트+벨."""
    async def go():
        app = AlphredTUI("http://localhost:59999", None)
        async with app.run_test() as pilot:
            await pilot.pause()
            toasts, bells = [], []
            monkeypatch.setattr(app, "notify",
                                lambda m, **k: toasts.append((m, k.get("severity"))))
            monkeypatch.setattr(app, "bell", lambda: bells.append(1))
            app._states = {"t1": "In-Progress"}
            app._notify_transitions(
                [{"id": "t1", "state": "Completed", "result": "결과물 있음",
                  "session_key": "other"}], cards_before=set())
            assert bells and any("완료" in m for m, _ in toasts)
            # AwaitingInput(타 세션) 부상
            toasts.clear()
            app._states = {"t2": "Pending"}
            app._notify_transitions(
                [{"id": "t2", "state": "AwaitingInput", "prompt": "질문작업",
                  "session_key": "other"}], cards_before=set())
            assert any("답변 대기" in m for m, _ in toasts)
        await app.http.aclose()

    asyncio.run(go())


def test_tui_copy_last_answer(monkeypatch, tmp_path):
    """§36 I8: ctrl+y — 마지막 어시스턴트 답변을 클립보드로."""
    async def go():
        app = AlphredTUI("http://localhost:59999", None, sessions_dir=tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            copied = []
            monkeypatch.setattr(app, "copy_to_clipboard", lambda t: copied.append(t))
            app.action_copy_last()                       # 답변 없음 → no-op
            assert not copied
            app._record("user", "질문")
            app._record("assistant", "# 답변\n본문입니다")
            assert app._last_assistant_text() == "# 답변\n본문입니다"
            app.action_copy_last()
            assert copied == ["# 답변\n본문입니다"]
        await app.http.aclose()

    asyncio.run(go())


def test_tui_export_session_markdown(tmp_path):
    """§36 I8: /export — 세션 대화를 Markdown 파일로 저장."""
    async def go():
        app = AlphredTUI("http://localhost:59999", None, sessions_dir=tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._record("user", "보고서 만들어줘")
            app._record("assistant", "## 결과\n- 항목1")
            out = tmp_path / "export.md"
            app.cmd_export(str(out))
            text = out.read_text(encoding="utf-8")
            assert "보고서 만들어줘" in text and "## 결과" in text
            assert "◆ Alphred" in text and "🧑 You" in text
        await app.http.aclose()

    asyncio.run(go())


def test_tui_statusbar_responsive_narrow():
    """§36 T4: 좁은 터미널(<80칸)에선 상태줄이 모델·세션을 접고 배지/depth 는 보존."""
    from textual.widgets import Static

    async def go():
        app = AlphredTUI("http://localhost:59999", None)
        async with app.run_test(size=(70, 20)) as pilot:
            await pilot.pause()
            app.model = "meta/llama-3.3-70b-instruct"
            app._model_label = "meta/llama-3.3-70b-instruct"
            app._refresh_statusbar()
            bar = str(app.query_one("#statusbar", Static).content)
            assert "depth:" in bar and "모델" not in bar   # 좁으면 모델/세션 접힘
        await app.http.aclose()

    asyncio.run(go())


def test_tui_slash_palette_filters_and_navigates():
    """`/` 입력 시 명령 팝업 표시, 타이핑 필터, Esc 닫힘."""
    async def go():
        app = AlphredTUI("http://localhost:59999", None)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("slash")          # "/"
            await pilot.pause()
            assert app.palette_visible()
            assert app._palette().option_count >= 5   # 전체 명령 노출
            await pilot.press("m")              # "/m" → model 만
            await pilot.pause()
            ids = [app._palette().get_option_at_index(i).id
                   for i in range(app._palette().option_count)]
            assert "model" in ids and "help" not in ids
            await pilot.press("escape")
            await pilot.pause()
            assert not app.palette_visible()
        await app.http.aclose()

    asyncio.run(go())


def test_tui_budget_and_slots():
    """§38 P4: TUI 상태줄에 남은 RPD 예산이 노출되고 큐 덱에서 멀티 실행 슬롯 현황이 시각화되는지 테스트."""
    from textual.widgets import Static
    from alphred.tui_queue import deck_slot_line

    async def go():
        app = AlphredTUI("http://localhost:59999", None)
        async with app.run_test(size=(100, 20)) as pilot:
            await pilot.pause()
            
            # 1) 상태줄 예산 표시 확인
            app.budgets = {
                "openrouter": {"limit": 50, "used": 5, "remaining": 45},
                "nvidia": {"limit": 100, "used": 10, "remaining": 90}
            }
            app._refresh_statusbar()
            bar = str(app.query_one("#statusbar", Static).content)
            assert "budget:" in bar
            assert "OR:45" in bar
            assert "NV:90" in bar

            # 2) 큐 덱 멀티 슬롯 라인 시각화 테스트
            tasks = [
                {"id": "t1", "prompt": "task 1", "state": "In-Progress"},
                {"id": "t2", "prompt": "task 2", "state": "In-Progress"},
                {"id": "t3", "prompt": "task 3", "state": "Pending", "priority": 5}
            ]
            line = deck_slot_line(tasks, slots=4, active_slots=2)
            assert "실행 슬롯 (2/4)" in line
            assert "[t1]" in line
            assert "[t2]" in line
            assert "대기 1순위" in line
            assert "t3" in line

        await app.http.aclose()

    asyncio.run(go())
