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


def test_env_file_loading(monkeypatch, tmp_path):
    hermes_dir = tmp_path / "hermes_home"
    alphred_dir = tmp_path / "alphred_home"
    hermes_dir.mkdir()
    alphred_dir.mkdir()

    monkeypatch.setenv("HERMES_HOME", str(hermes_dir))
    monkeypatch.setenv("ALPHRED_HOME", str(alphred_dir))

    monkeypatch.delenv("ALPHRED_GATEWAY_URL", raising=False)
    monkeypatch.delenv("ALPHRED_MAX_RETRIES", raising=False)
    monkeypatch.delenv("ALPHRED_PROFILE", raising=False)

    (hermes_dir / ".env").write_text("ALPHRED_GATEWAY_URL=http://100.99.88.77:9999\n# Comment\nALPHRED_MAX_RETRIES=5", encoding="utf-8")
    (alphred_dir / ".env").write_text("ALPHRED_PROFILE=smart\nALPHRED_MAX_RETRIES=99", encoding="utf-8")

    cfg = Config.load()
    assert cfg.gateway_url == "http://100.99.88.77:9999"
    assert cfg.max_retries == 5
    assert cfg.profile == "smart"


def test_setup_creates_env_template(monkeypatch, tmp_path):
    hermes_dir = tmp_path / "hermes_home"
    alphred_dir = tmp_path / "alphred_home"
    hermes_dir.mkdir()
    alphred_dir.mkdir()

    monkeypatch.setenv("HERMES_HOME", str(hermes_dir))
    monkeypatch.setenv("ALPHRED_HOME", str(alphred_dir))

    (hermes_dir / "bin").mkdir()
    hermes_bin = hermes_dir / "bin" / "hermes"
    hermes_bin.write_text("#!/bin/sh\nexit 0", encoding="utf-8")
    hermes_bin.chmod(0o755)
    monkeypatch.setenv("ALPHRED_HERMES_BIN", str(hermes_bin))

    from alphred.cli import _cmd_setup
    res = _cmd_setup(["--profile", "basic"])
    assert res == 0

    env_path = alphred_dir / ".env"
    assert env_path.exists()
    text = env_path.read_text(encoding="utf-8")
    assert "ALPHRED_GATEWAY_URL" in text
    assert "ALPHRED_HERMES_API" in text