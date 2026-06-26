"""전용 TUI 대화 세션 영속화(§13.4) — alphred_home/tui_sessions/<id>.json.

각 세션 = {id, model, title, created, updated, messages:[{role, text}]}.
게이트웨이(:8643)는 Hermes 세션을 서버측에 보관하지만, **TUI 화면 로그**(사용자에게
보이는 대화 기록)는 재시작 시 사라진다. 이 저장소가 그 화면 기록을 복원/관리한다.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SessionStore:
    def __init__(self, base_dir: str | Path):
        self.dir = Path(base_dir) / "tui_sessions"
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, sid: str) -> Path:
        return self.dir / f"{sid}.json"

    def new(self, sid: str, model: str | None = None) -> dict:
        return {"id": sid, "model": model, "title": "", "created": _now(),
                "updated": _now(), "messages": []}

    def save(self, session: dict) -> None:
        # 전달된 세션 dict 를 직접 갱신(메모리 상태와 디스크 일관 — 타이틀바 등에서 즉시 반영).
        session["updated"] = _now()
        if not session.get("title"):
            for m in session.get("messages", []):
                if m.get("role") == "user" and m.get("text"):
                    session["title"] = m["text"].replace("\n", " ")[:48]
                    break
        try:
            self._path(session["id"]).write_text(
                json.dumps(session, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    def load(self, sid: str) -> dict | None:
        p = self._path(sid)
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None

    def list(self) -> list[dict]:
        """메타(updated 내림차순). messages 포함된 전체 dict 를 반환."""
        out: list[dict] = []
        for p in self.dir.glob("*.json"):
            try:
                out.append(json.loads(p.read_text(encoding="utf-8")))
            except Exception:
                continue
        out.sort(key=lambda d: d.get("updated") or "", reverse=True)
        return out

    def latest(self) -> dict | None:
        items = self.list()
        return items[0] if items else None

    def delete(self, sid: str) -> bool:
        p = self._path(sid)
        if p.exists():
            try:
                p.unlink()
                return True
            except Exception:
                return False
        return False
