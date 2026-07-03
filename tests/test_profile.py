"""§35.4 프로파일 + keys CLI 테스트."""
from __future__ import annotations

from alphred.cli import _cmd_keys
from alphred.config import Config, read_profile, set_profile


def _load(monkeypatch, tmp_path, **env):
    monkeypatch.setenv("ALPHRED_HOME", str(tmp_path))
    for k in ("ALPHRED_PROFILE", "ALPHRED_INTENT", "ALPHRED_PLANNER", "ALPHRED_CLARIFY",
              "ALPHRED_ORCHESTRATE", "ALPHRED_WATCHDOG"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return Config.load()


def test_profile_default_basic_keeps_flags_off(monkeypatch, tmp_path):
    cfg = _load(monkeypatch, tmp_path)
    assert cfg.profile == "basic"
    assert not (cfg.intent or cfg.planner or cfg.clarify or cfg.orchestrate or cfg.watchdog)


def test_profile_smart_and_full_defaults(monkeypatch, tmp_path):
    cfg = _load(monkeypatch, tmp_path, ALPHRED_PROFILE="smart")
    assert cfg.intent and cfg.planner
    assert not (cfg.clarify or cfg.orchestrate or cfg.watchdog)
    cfg2 = _load(monkeypatch, tmp_path, ALPHRED_PROFILE="full")
    assert cfg2.intent and cfg2.planner and cfg2.clarify
    assert cfg2.orchestrate and cfg2.watchdog


def test_profile_file_and_env_precedence(monkeypatch, tmp_path):
    set_profile(tmp_path, "full")
    assert read_profile(tmp_path) == "full"
    cfg = _load(monkeypatch, tmp_path)                       # 파일 반영
    assert cfg.profile == "full" and cfg.orchestrate
    cfg2 = _load(monkeypatch, tmp_path, ALPHRED_PROFILE="basic")   # env > 파일
    assert cfg2.profile == "basic" and not cfg2.intent


def test_individual_env_overrides_profile(monkeypatch, tmp_path):
    # full 인데 개별 env 로 orchestrate 만 끔 / basic 인데 intent 만 켬
    cfg = _load(monkeypatch, tmp_path, ALPHRED_PROFILE="full", ALPHRED_ORCHESTRATE="0")
    assert cfg.clarify and not cfg.orchestrate
    cfg2 = _load(monkeypatch, tmp_path, ALPHRED_INTENT="1")
    assert cfg2.profile == "basic" and cfg2.intent and not cfg2.planner


def test_keys_cli_roundtrip(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("ALPHRED_HOME", str(tmp_path))
    assert _cmd_keys(["issue", "노트북", "--scope", "read"]) == 0
    out = capsys.readouterr().out
    assert "alph_" in out and "한 번만 표시" in out
    assert _cmd_keys(["list"]) == 0
    assert "노트북" in capsys.readouterr().out
    assert _cmd_keys(["issue", "노트북"]) == 2               # 중복 이름
    capsys.readouterr()
    assert _cmd_keys(["revoke", "노트북"]) == 0
    assert _cmd_keys(["revoke", "노트북"]) == 1              # 이미 없음