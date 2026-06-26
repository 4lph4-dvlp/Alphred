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
            assert app.query_one("#queue") is not None
            assert app.query_one("#prompt") is not None
        await app.http.aclose()

    asyncio.run(go())


def test_tui_state_label():
    from alphred.tui import _state_label
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


def test_tui_queue_columns():
    """큐 테이블은 종류 열을 빼고 ID/우선/상태/요청 = 4열(모든 작업이 heavy)."""
    from textual.widgets import DataTable

    async def go():
        app = AlphredTUI("http://localhost:59999", None)
        async with app.run_test() as pilot:
            await pilot.pause()
            cols = app.query_one("#queue", DataTable).columns
            assert len(cols) == 4
            labels = [str(c.label) for c in cols.values()]
            assert "종류" not in labels and "요청" in labels
        await app.http.aclose()

    asyncio.run(go())


def test_tui_queue_keys_and_focus():
    """큐 패널 포커스 후 액션 키(빈 큐) 무crash + Esc 로 입력 복귀."""
    from alphred.tui import PromptInput, QueueTable

    async def go():
        app = AlphredTUI("http://localhost:59999", None)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.set_focus(app.query_one("#queue", QueueTable))
            await pilot.pause()
            await pilot.press("c")        # cancel on empty → no-op, no crash
            await pilot.press("v")        # view on empty → no-op
            await pilot.press("escape")   # 입력으로 복귀
            await pilot.pause()
            assert isinstance(app.focused, PromptInput)
        await app.http.aclose()

    asyncio.run(go())


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


def test_tui_logo_rendered_and_fits_panel():
    """메인화면에 로고가 그려지고, 배너가 출력 패널 폭 안에 들어간다(짤림 방지)."""
    from textual.widgets import RichLog
    from alphred.splash import banner_lines, logo_lines, _BANNER_W

    assert len(logo_lines()) >= 4

    async def go():
        app = AlphredTUI("http://localhost:59999", None)
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            chat = app.query_one("#chat", RichLog)
            # 출력 패널의 콘텐츠 가용 폭 ≥ 배너 폭 → 줄바꿈/짤림 없음.
            assert chat.size.width >= _BANNER_W
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


def test_tui_session_shown_in_panel_title(tmp_path):
    """현재 세션이 출력 패널 테두리 제목에 표시된다(모델과 함께)."""
    from textual.widgets import RichLog

    async def go():
        app = AlphredTUI("http://localhost:59999", None, sessions_dir=tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.submit_current  # noqa — 그냥 세션 제목 생성 경로 확인
            app._session["title"] = "주식 보고서"
            app._set_titlebar()
            title = str(app.query_one("#chat", RichLog).border_title)
            assert "세션" in title and "주식 보고서" in title
        await app.http.aclose()

    asyncio.run(go())


def test_tui_model_shown_in_panel_title():
    """현재 모델이 출력 패널 테두리 제목에 표시된다."""
    from textual.widgets import RichLog

    async def go():
        app = AlphredTUI("http://localhost:59999", None)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.model = "meta/llama-3.3-70b-instruct"
            await app._update_model_display()
            title = str(app.query_one("#chat", RichLog).border_title)
            assert "모델" in title and "llama-3.3-70b" in title
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
