"""가용 능력 레지스트리 (§34.5 D1) — 에이전트가 "지금 실제로" 쓸 수 있는 것의 스냅샷.

수집원(전부 무LLM·저비용, 항목별 fail-open):
  · 스킬        — Hermes :8642 `GET /v1/skills`
  · 툴셋/도구   — Hermes :8642 `GET /v1/toolsets`
  · MCP 서버    — config.yaml 최상위 `mcp:`/`mcp_servers:` 블록(읽기 전용 라인 파싱)
  · 코딩 CLI    — agy/claude/codex/opencode `--version` 프로브(타임아웃 2s)
  · 파이썬 라이브러리 — Hermes venv 파이썬으로 `find_spec` 배치 프로브(임포트 부작용 없음)

파생: 형식별 생성 능력 매트릭스(pdf/docx/pptx/xlsx/…) — "텍스트를 .pdf 로 저장" 류의
환각을 착수 전에 차단할 근거. §20.8 의 "PDF 생성 수단 부재를 실패 후에야 발견" 문제를
사전 판정으로 전환한다.

캐시: ALPHRED_HOME/capabilities.json + TTL(기본 1h). 갱신 트리거 = 데몬 시작 워밍업 /
TTL 경과 / 설치류 작업 완료 직후 invalidate(). 수집 실패 섹션은 직전 캐시를 유지해
일시 장애(:8642 다운 등)가 능력 정보를 통째로 지우지 않게 한다.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

logger = logging.getLogger("alphred.capabilities")

# 코딩 에이전트 CLI (하네스 §26 이 안내하는 것과 동일 집합)
CLI_AGENTS = ("agy", "claude", "codex", "opencode")

# 산출물 형식 → 그 형식을 "진짜로" 만들 수 있게 하는 파이썬 임포트 이름 후보(우선순위순).
# 키는 임포트 이름(pip 이름과 다를 수 있음: docx=python-docx, pptx=python-pptx).
FORMAT_LIBS: dict[str, tuple[str, ...]] = {
    "pdf": ("weasyprint", "reportlab", "fpdf"),
    "docx": ("docx",),
    "pptx": ("pptx",),
    "xlsx": ("openpyxl", "xlsxwriter"),
    "charts": ("matplotlib", "plotly"),
    "data": ("pandas", "numpy"),
}
# 라이브러리 없이도 해당 형식을 커버하는 스킬(설치돼 있으면 capable 로 인정).
FORMAT_SKILLS: dict[str, tuple[str, ...]] = {
    "pptx": ("powerpoint",),
    "xlsx": ("excel-author",),
}
# 임포트 이름 → pip 설치 이름(제안 문구용).
PIP_NAMES = {"docx": "python-docx", "pptx": "python-pptx"}

_PROBE_LIBS = sorted({m for libs in FORMAT_LIBS.values() for m in libs})

CACHE_FILENAME = "capabilities.json"


def _venv_python(hermes_bin: str | None, hermes_home: Path) -> Path | None:
    """Hermes venv 의 파이썬 실행 파일(라이브러리 프로브 대상 런타임)."""
    cands = []
    if hermes_bin:
        b = Path(hermes_bin)
        cands += [b.with_name("python.exe"), b.with_name("python")]
    venv = Path(hermes_home) / "hermes-agent" / "venv"
    cands += [venv / "Scripts" / "python.exe", venv / "bin" / "python"]
    for c in cands:
        if c.exists():
            return c
    return None


def _no_window_kwargs() -> dict:
    return {"creationflags": 0x08000000} if os.name == "nt" else {}  # CREATE_NO_WINDOW


# ---- 수집기(각각 예외를 던질 수 있음 — 레지스트리가 섹션 단위 fail-open) ----
def collect_skills(client) -> dict:
    """Hermes /v1/skills → 설치 스킬 목록(에이전트가 보는 것과 동일)."""
    data = client.skills()
    items = []
    for s in (data.get("data") or [])[:80]:
        if isinstance(s, dict) and s.get("name"):
            items.append({"name": str(s["name"])[:60],
                          "description": str(s.get("description") or "")[:160],
                          "category": str(s.get("category") or "")[:40]})
    return {"ok": True, "items": items}


def _tool_names(payload) -> list[str]:
    """/v1/toolsets 응답에서 도구 이름을 방어적으로 추출(형태 변화에 관대)."""
    names: list[str] = []

    def from_entry(e) -> None:
        if isinstance(e, str):
            names.append(e)
        elif isinstance(e, dict):
            n = e.get("name") or e.get("id") or e.get("tool")
            if isinstance(n, str):
                names.append(n)
            if isinstance(e.get("tools"), list):
                for t in e["tools"]:
                    from_entry(t)

    if isinstance(payload, dict):
        for key in ("data", "toolsets", "tools"):
            v = payload.get(key)
            if isinstance(v, list):
                for e in v:
                    from_entry(e)
    elif isinstance(payload, list):
        for e in payload:
            from_entry(e)
    seen: set[str] = set()
    out = []
    for n in names:
        if n not in seen:
            seen.add(n)
            out.append(n[:60])
    return out[:120]


def collect_toolsets(client) -> dict:
    return {"ok": True, "tools": _tool_names(client.toolsets())}


def collect_mcp(hermes_home: Path) -> dict:
    """config.yaml 최상위 `mcp:`/`mcp_servers:` 블록의 서버 이름(읽기 전용).

    이 Hermes 버전 config 에 블록이 없으면 빈 목록(정상). 자식이 `servers:` 하나뿐이면
    그 아래 키들이 실제 서버 이름이다.
    """
    p = Path(hermes_home) / "config.yaml"
    if not p.exists():
        return {"ok": True, "servers": []}
    lines = p.read_text(encoding="utf-8").splitlines()
    servers: list[str] = []
    in_block = False
    block_child_indent: int | None = None
    in_servers = False
    servers_child_indent: int | None = None
    for ln in lines:
        stripped = ln.lstrip(" ")
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(ln) - len(stripped)
        m = re.match(r"([\w\-]+):(.*)$", stripped)
        if not m:
            continue
        key, rest = m.group(1), m.group(2).strip()
        if indent == 0:
            in_block = key in ("mcp", "mcp_servers")
            block_child_indent = None
            in_servers = False
            # 인라인 값(`mcp: {}` 등)이면 자식 없음
            if in_block and rest and rest not in ("", "|", ">"):
                in_block = False
            continue
        if not in_block:
            continue
        if block_child_indent is None:
            block_child_indent = indent
        if indent == block_child_indent:
            in_servers = key == "servers" and (not rest or rest in ("", "|", ">"))
            servers_child_indent = None
            if not in_servers and key != "servers":
                servers.append(key[:60])
            continue
        if in_servers:
            if servers_child_indent is None:
                servers_child_indent = indent
            if indent == servers_child_indent:
                servers.append(key[:60])
    # 자식이 servers: 하나뿐이던 경우 위 로직이 이미 그 아래만 담는다.
    return {"ok": True, "servers": servers[:40]}


def collect_cli_agents() -> dict:
    """코딩 에이전트 CLI 실행 가능 여부 + 버전(각 2s 타임아웃, 개별 fail-open)."""
    items: dict[str, dict] = {}
    for name in CLI_AGENTS:
        path = shutil.which(name)
        if not path:
            items[name] = {"found": False}
            continue
        entry: dict = {"found": True, "path": path}
        try:
            out = subprocess.run([path, "--version"], capture_output=True, text=True,
                                 timeout=2.0, **_no_window_kwargs())
            first = (out.stdout or out.stderr or "").strip().splitlines()
            if first:
                entry["version"] = first[0][:60]
        except Exception:
            pass  # 버전 확인 실패해도 found 는 유지
        items[name] = entry
    return {"ok": True, "items": items}


def collect_pylibs(hermes_bin: str | None, hermes_home: Path) -> dict:
    """Hermes venv 파이썬에서 형식 생성 라이브러리 가용성 배치 프로브(find_spec, 무임포트)."""
    py = _venv_python(hermes_bin, hermes_home)
    if py is None:
        return {"ok": False, "error": "hermes venv python not found",
                "available": [], "missing": list(_PROBE_LIBS)}
    code = ("import json,importlib.util as u;"
            "print(json.dumps({m: u.find_spec(m) is not None for m in %r}))" % _PROBE_LIBS)
    env = {**os.environ, "PYTHONUTF8": "1"}
    out = subprocess.run([str(py), "-c", code], capture_output=True, text=True,
                         timeout=20, env=env, **_no_window_kwargs())
    if out.returncode != 0 or not out.stdout.strip():
        raise RuntimeError(f"probe failed: {out.stderr.strip()[:120]}")
    res = json.loads(out.stdout.strip())
    avail = sorted(m for m, ok in res.items() if ok)
    missing = sorted(m for m, ok in res.items() if not ok)
    return {"ok": True, "available": avail, "missing": missing, "python": str(py)}


def derive_formats(pylibs: dict, skills: dict) -> dict:
    """형식별 생성 능력 매트릭스 — 라이브러리 또는 대응 스킬이 있으면 capable."""
    avail = set(pylibs.get("available") or [])
    skill_names = {s.get("name", "").lower() for s in (skills.get("items") or [])}
    out: dict[str, dict] = {}
    for fmt, libs in FORMAT_LIBS.items():
        via = next((m for m in libs if m in avail), None)
        if via is None:
            skill = next((s for s in FORMAT_SKILLS.get(fmt, ()) if s in skill_names), None)
            if skill:
                out[fmt] = {"capable": True, "via": f"skill:{skill}"}
                continue
            missing = [PIP_NAMES.get(m, m) for m in libs]
            out[fmt] = {"capable": False, "via": None, "install": missing[0],
                        "alternatives": missing[1:]}
        else:
            out[fmt] = {"capable": True, "via": via}
    return out


# ---- §34.3 B1b/D4: 플래너 접지(grounding) — 계획 ↔ 실물 능력 대조 ----
def planner_context(snapshot: dict) -> str:
    """플래너 v2 프롬프트에 동봉할 콤팩트 인벤토리(하네스 섹션의 축약판, ~수백 자)."""
    lines: list[str] = []
    skills = [s.get("name") for s in (snapshot.get("skills", {}).get("items") or [])]
    if skills:
        lines.append("skills: " + ", ".join(skills[:30]))
    cli = snapshot.get("cli_agents", {}).get("items") or {}
    found = [n for n, v in cli.items() if v.get("found")]
    lines.append("coding CLIs on PATH: " + (", ".join(found) or "none"))
    py = snapshot.get("pylibs", {})
    if py.get("available"):
        lines.append("python libs: " + ", ".join(py["available"]))
    fmts = snapshot.get("formats") or {}
    ok = [f for f, v in fmts.items() if v.get("capable")]
    no = [f"{f}(install {v.get('install')})" for f, v in fmts.items() if not v.get("capable")]
    if ok:
        lines.append("producible formats: " + ", ".join(sorted(ok)))
    if no:
        lines.append("NOT producible without install: " + ", ".join(sorted(no)))
    return "\n".join(lines)


def plan_gaps(plan: dict, snapshot: dict) -> list[dict]:
    """Plan v2 가 참조하는 능력 중 실물에 없는 것 검출(결정적, 무LLM).

    반환: [{kind: skill|cli|format, name/fmt, step, install?}] — 없으면 빈 리스트.
    """
    gaps: list[dict] = []
    skills = {s.get("name", "").lower()
              for s in (snapshot.get("skills", {}).get("items") or [])}
    cli = snapshot.get("cli_agents", {}).get("items") or {}
    fmts = snapshot.get("formats") or {}
    seen: set[tuple] = set()
    for s in plan.get("steps") or []:
        hint = (s.get("tool_hint") or "").strip().lower()
        goal = (s.get("goal") or "").lower()
        is_install_step = "install" in goal or "설치" in goal or hint == "terminal"
        if hint.startswith("skill:"):
            name = hint[6:].strip()
            if name and name not in skills and ("skill", name) not in seen:
                seen.add(("skill", name))
                gaps.append({"kind": "skill", "name": name, "step": s.get("id")})
        elif hint.startswith("cli:"):
            name = hint[4:].strip()
            if name and not (cli.get(name) or {}).get("found") and ("cli", name) not in seen:
                seen.add(("cli", name))
                gaps.append({"kind": "cli", "name": name, "step": s.get("id")})
        fmt = (s.get("expected") or {}).get("format")
        if fmt and fmt in fmts and not fmts[fmt].get("capable") and not is_install_step:
            # 계획 안에 해당 라이브러리 설치 스텝이 이미 있으면 갭 아님
            has_install = any(
                ("install" in (t.get("goal") or "").lower() or "설치" in (t.get("goal") or ""))
                and (fmts[fmt].get("install") or "") in (t.get("goal") or "")
                for t in plan.get("steps") or [])
            if not has_install and ("format", fmt) not in seen:
                seen.add(("format", fmt))
                gaps.append({"kind": "format", "fmt": fmt, "step": s.get("id"),
                             "install": fmts[fmt].get("install")})
    return gaps


def apply_gap_fixes(plan: dict, gaps: list[dict]) -> dict:
    """갭을 결정적으로 수리(§34.9 — LLM 재질의 대신 코드가 고침).

    · format 갭 → 계획 맨 앞에 설치 스텝 삽입(uv pip install <lib>, exit_code 검증)
    · skill/cli 갭 → 해당 스텝의 tool_hint 를 execute_code 로 강등(스킬 없이 수행)
    수리 내역은 plan["gaps"] 에 남겨 상세뷰/드라이런에 표면화한다.
    """
    if not gaps:
        return plan
    steps = list(plan.get("steps") or [])
    notes: list[str] = []
    installs: list[dict] = []
    for g in gaps:
        if g["kind"] == "format" and g.get("install"):
            sid = f"s0install{len(installs)}"
            installs.append({
                "id": sid,
                "goal": f"Install the missing {g['fmt']} library first: "
                        f"`uv pip install {g['install']}` (fall back to pip).",
                "tool_hint": "terminal", "needs": [],
                "expected": {"type": "action", "format": None, "path_hint": None},
                "accept": [{"check": "exit_code", "arg": "0"}],
            })
            notes.append(f"{g['fmt']} 생성 수단 부재 → 설치 스텝 자동 삽입({g['install']})")
        elif g["kind"] in ("skill", "cli"):
            for s in steps:
                if s.get("id") == g.get("step"):
                    s["tool_hint"] = "execute_code"
            notes.append(f"{g['kind']} '{g.get('name')}' 미설치 → execute_code 로 강등")
    if installs:
        first_ids = [i["id"] for i in installs]
        for s in steps:                      # 기존 첫 스텝이 설치 이후에 오도록 의존성 연결
            if not s.get("needs"):
                s["needs"] = list(first_ids)
        steps = installs + steps
    out = dict(plan)
    out["steps"] = steps[:9]                 # 삽입으로 늘어도 상한 유지
    out["gaps"] = notes
    return out


class CapabilityRegistry:
    """능력 스냅샷의 수집·캐시·표현(하네스 섹션/API 요약)을 담당하는 단일 객체."""

    def __init__(self, cfg, client=None, ttl_seconds: float | None = None):
        self.cfg = cfg
        self.client = client
        self.ttl = float(ttl_seconds if ttl_seconds is not None
                         else getattr(cfg, "caps_ttl", 3600.0))
        self._lock = threading.Lock()
        self._data: dict | None = None
        self._collected_mono: float = 0.0

    @property
    def cache_path(self) -> Path:
        return Path(self.cfg.alphred_home) / CACHE_FILENAME

    # ---- 수명주기 ----
    def invalidate(self) -> None:
        """다음 조회 때 재수집을 강제한다(설치류 작업 완료 직후 등). 데이터는 폴백용으로 유지."""
        with self._lock:
            self._collected_mono = 0.0

    def snapshot(self, force: bool = False) -> dict:
        """현재 능력 스냅샷(TTL 캐시). 수집 실패 섹션은 직전 값을 유지(fail-open)."""
        with self._lock:
            now = time.monotonic()
            if (not force and self._data is not None
                    and self._collected_mono > 0.0
                    and now - self._collected_mono < self.ttl):
                return self._data
            prev = self._data or self._load_file() or {}
            data = self._collect(prev)
            self._data = data
            self._collected_mono = now
            self._save_file(data)
            return data

    def _collect(self, prev: dict) -> dict:
        def section(name: str, fn) -> dict:
            try:
                return fn()
            except Exception as e:
                logger.debug("capabilities %s 수집 실패: %s", name, e)
                old = prev.get(name)
                if isinstance(old, dict) and old.get("ok"):
                    return {**old, "stale": True}
                return {"ok": False, "error": f"{type(e).__name__}: {e}"[:160]}

        skills = section("skills", lambda: collect_skills(self.client)
                         if self.client is not None else {"ok": False, "error": "no client"})
        toolsets = section("toolsets", lambda: collect_toolsets(self.client)
                           if self.client is not None else {"ok": False, "error": "no client"})
        mcp = section("mcp", lambda: collect_mcp(self.cfg.hermes_home))
        cli = section("cli_agents", collect_cli_agents)
        pylibs = section("pylibs", lambda: collect_pylibs(
            getattr(self.cfg, "hermes_bin", None), self.cfg.hermes_home))
        data = {
            "collected_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "skills": skills, "toolsets": toolsets, "mcp": mcp,
            "cli_agents": cli, "pylibs": pylibs,
            "formats": derive_formats(pylibs, skills),
        }
        return data

    def _load_file(self) -> dict | None:
        try:
            return json.loads(self.cache_path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def _save_file(self, data: dict) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                                       encoding="utf-8")
        except Exception:
            logger.debug("capabilities 캐시 저장 실패", exc_info=True)

    # ---- 표현 ----
    def summary(self) -> dict:
        """API(/capabilities)·doctor 용 요약 — 스냅샷 + 파생 카운트."""
        d = self.snapshot()
        cli = d.get("cli_agents", {}).get("items") or {}
        return {
            **d,
            "counts": {
                "skills": len(d.get("skills", {}).get("items") or []),
                "tools": len(d.get("toolsets", {}).get("tools") or []),
                "mcp_servers": len(d.get("mcp", {}).get("servers") or []),
                "cli_agents": sum(1 for v in cli.values() if v.get("found")),
                "pylibs": len(d.get("pylibs", {}).get("available") or []),
            },
        }

    def harness_section(self) -> str:
        """{{CAPABILITIES}} 마커에 주입할 실물 인벤토리(영어 — 하네스와 동일 언어)."""
        d = self.snapshot()
        lines: list[str] = [
            "LIVE CAPABILITY INVENTORY (verified on this machine — trust this over "
            "assumptions; anything not listed must be checked or installed first):",
        ]
        items = d.get("skills", {}).get("items") or []
        if items:
            lines.append("")
            lines.append("- **Installed skills** (load with `skill_view` and follow their "
                         "workflow before doing the task manually):")
            for s in items[:40]:
                desc = s.get("description") or ""
                lines.append(f"  - `{s['name']}`" + (f" — {desc[:100]}" if desc else ""))
            if len(items) > 40:
                lines.append(f"  - …and {len(items) - 40} more (list via skills tools)")
        cli = d.get("cli_agents", {}).get("items") or {}
        found = [f"`{n}`" + (f" ({v.get('version')})" if v.get("version") else "")
                 for n, v in cli.items() if v.get("found")]
        lines.append("")
        if found:
            lines.append("- **Coding-agent CLIs available on PATH**: " + ", ".join(found)
                         + " — prefer one of these for substantial coding/dev tasks "
                           "(drive via the `terminal` tool).")
        else:
            lines.append("- **Coding-agent CLIs**: none found on PATH — use `execute_code` "
                         "and `terminal` directly for coding work.")
        py = d.get("pylibs", {})
        if py.get("available") or py.get("missing"):
            lines.append("- **Python libraries present in the runtime**: "
                         + (", ".join(f"`{m}`" for m in py.get("available") or []) or "none"))
        fmts = d.get("formats") or {}
        cap = [f"{f} (via {v['via']})" for f, v in fmts.items() if v.get("capable")]
        nocap = [f"{f} — install `{v.get('install')}` first"
                 for f, v in fmts.items() if not v.get("capable")]
        if cap:
            lines.append("- **File formats you can genuinely produce right now**: "
                         + "; ".join(cap))
        if nocap:
            lines.append("- **Formats NOT currently producible** (do NOT fake them by "
                         "renaming a text file — install the library first, e.g. "
                         "`uv pip install <name>` via terminal): " + "; ".join(nocap))
        servers = d.get("mcp", {}).get("servers") or []
        if servers:
            lines.append("- **MCP servers registered**: "
                         + ", ".join(f"`{s}`" for s in servers))
        return "\n".join(lines)
