"""§34.5 능력 레지스트리 테스트 — 수집기 · 형식 매트릭스 · 캐시/TTL · 하네스 주입."""
from __future__ import annotations

from types import SimpleNamespace

from alphred import capabilities as caps_mod
from alphred.capabilities import (
    CapabilityRegistry,
    collect_mcp,
    collect_skills,
    collect_toolsets,
    derive_formats,
)
from alphred.prompt import CAPS_MARKER, autonomous_input, render_capabilities


class _FakeClient:
    def __init__(self, skills=None, toolsets=None, fail=False):
        self._skills = skills or {"object": "list", "data": [
            {"name": "powerpoint", "description": "pptx 생성/편집", "category": "docs"},
            {"name": "nano-pdf", "description": "PDF 편집", "category": "docs"},
        ]}
        self._toolsets = toolsets or {"data": [
            {"name": "hermes-api-server",
             "tools": ["write_file", "read_file", "execute_code", "terminal"]}]}
        self._fail = fail

    def skills(self):
        if self._fail:
            raise RuntimeError(":8642 down")
        return self._skills

    def toolsets(self):
        if self._fail:
            raise RuntimeError(":8642 down")
        return self._toolsets


def _cfg(tmp_path):
    home = tmp_path / "hermes"
    home.mkdir(exist_ok=True)
    alph = tmp_path / "alphred"
    alph.mkdir(exist_ok=True)
    return SimpleNamespace(hermes_home=home, alphred_home=alph, hermes_bin=None,
                           caps_ttl=3600.0)


# ---- 수집기 ----
def test_collect_skills_and_toolsets_normalize():
    c = _FakeClient()
    s = collect_skills(c)
    assert s["ok"] and s["items"][0]["name"] == "powerpoint"
    t = collect_toolsets(c)
    assert "write_file" in t["tools"] and "hermes-api-server" in t["tools"]


def test_collect_mcp_block_and_absent(tmp_path):
    home = tmp_path / "h1"
    home.mkdir()
    (home / "config.yaml").write_text(
        "model:\n  default: x\nmcp:\n  servers:\n    filesystem:\n      cmd: npx\n"
        "    github:\n      cmd: npx\nother: {}\n", encoding="utf-8")
    r = collect_mcp(home)
    assert r["ok"] and set(r["servers"]) == {"filesystem", "github"}

    home2 = tmp_path / "h2"
    home2.mkdir()
    (home2 / "config.yaml").write_text("model:\n  default: x\nmcp: {}\n", encoding="utf-8")
    assert collect_mcp(home2)["servers"] == []          # 인라인 빈 매핑

    home3 = tmp_path / "h3"
    home3.mkdir()
    (home3 / "config.yaml").write_text("model:\n  default: x\n", encoding="utf-8")
    assert collect_mcp(home3)["servers"] == []          # 블록 자체 없음(정상)


def test_derive_formats_lib_skill_and_missing():
    pylibs = {"ok": True, "available": ["reportlab", "openpyxl"], "missing": ["weasyprint"]}
    skills = {"ok": True, "items": [{"name": "powerpoint"}]}
    f = derive_formats(pylibs, skills)
    assert f["pdf"]["capable"] and f["pdf"]["via"] == "reportlab"
    assert f["xlsx"]["capable"] and f["xlsx"]["via"] == "openpyxl"
    assert f["pptx"]["capable"] and f["pptx"]["via"] == "skill:powerpoint"  # 스킬로 커버
    assert not f["docx"]["capable"] and f["docx"]["install"] == "python-docx"


# ---- 레지스트리 캐시/TTL/폴백 ----
def _patch_local_probes(monkeypatch):
    monkeypatch.setattr(caps_mod, "collect_cli_agents",
                        lambda: {"ok": True, "items": {"agy": {"found": False}}})
    monkeypatch.setattr(caps_mod, "collect_pylibs",
                        lambda b, h: {"ok": True, "available": ["reportlab"],
                                      "missing": ["docx"]})


def test_snapshot_ttl_cache_and_invalidate(tmp_path, monkeypatch):
    _patch_local_probes(monkeypatch)
    calls = []
    orig = caps_mod.collect_skills

    def counting(client):
        calls.append(1)
        return orig(client)

    monkeypatch.setattr(caps_mod, "collect_skills", counting)
    reg = CapabilityRegistry(_cfg(tmp_path), _FakeClient())
    reg.snapshot()
    reg.snapshot()
    assert len(calls) == 1                     # TTL 내 재수집 없음
    reg.invalidate()
    reg.snapshot()
    assert len(calls) == 2                     # 무효화 → 재수집
    assert reg.cache_path.exists()             # 파일 캐시 기록


def test_failed_section_keeps_previous(tmp_path, monkeypatch):
    _patch_local_probes(monkeypatch)
    cfg = _cfg(tmp_path)
    reg = CapabilityRegistry(cfg, _FakeClient())
    first = reg.snapshot()
    assert first["skills"]["ok"]
    # 두 번째 수집에서 :8642 다운 → 스킬 섹션은 직전 값 유지(stale 표시)
    reg.client = _FakeClient(fail=True)
    reg.invalidate()
    second = reg.snapshot()
    assert second["skills"]["ok"] and second["skills"].get("stale")
    assert second["skills"]["items"] == first["skills"]["items"]


def test_no_client_fail_open(tmp_path, monkeypatch):
    _patch_local_probes(monkeypatch)
    reg = CapabilityRegistry(_cfg(tmp_path), client=None)
    d = reg.snapshot()
    assert d["skills"]["ok"] is False          # 오류 기록만, 예외 없음
    assert d["formats"]["pdf"]["capable"]      # 로컬 프로브는 정상 동작


# ---- 하네스 주입 ----
def test_harness_section_lists_capabilities(tmp_path, monkeypatch):
    _patch_local_probes(monkeypatch)
    reg = CapabilityRegistry(_cfg(tmp_path), _FakeClient())
    text = reg.harness_section()
    assert "`powerpoint`" in text
    assert "pdf (via reportlab)" in text
    assert "docx" in text and "python-docx" in text    # 불가 형식 + 설치 제안


def test_render_capabilities_marker_and_passthrough():
    harness = f"HEAD\n{CAPS_MARKER}\nTAIL"
    out = render_capabilities(harness, "- inventory line")
    assert "- inventory line" in out and CAPS_MARKER not in out
    # 인벤토리 없음 → 정적 폴백으로 강등(기존 동작 상당)
    out2 = render_capabilities(harness, None)
    assert "nano-pdf" in out2 and CAPS_MARKER not in out2
    # 마커 없는 사용자 편집본 → 무변경
    legacy = "my custom harness, no marker"
    assert render_capabilities(legacy, "x") == legacy


def test_autonomous_input_injects_capabilities():
    harness = f"H {CAPS_MARKER} T\n\n## REQUEST\n"
    out = autonomous_input("작업해줘", harness=harness, capabilities="- CAP LINE")
    assert "- CAP LINE" in out and "작업해줘" in out


def test_default_harness_asset_has_marker():
    from alphred.prompt import default_prompt_text
    assert CAPS_MARKER in default_prompt_text()
