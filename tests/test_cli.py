"""CLI 동작 — 인터셉트/제거된 명령의 라우팅(네트워크·Hermes 불필요)."""
from __future__ import annotations

from alphred.cli import main


def test_alphred_chat_removed(capsys):
    """`alphred chat` 은 제거됨 — Hermes 로 위임하지 않고 hermes 안내 후 비정상 종료."""
    rc = main(["chat"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "alphred chat" in err and "hermes" in err
