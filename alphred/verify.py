"""산출물 검증 + 환각/되물음 휴리스틱 (§21 Tier0 + P4).

queue_manager 에서 분리한 **순수 함수 모음**(Task/QueueManager 의존 없음). 완료 결과가
실제 산출물을 만들었는지 결정적으로 검사하거나(파일 존재·형식 시그니처), 산출물 없이
되묻/실패했는지 판정한다. 모두 라이브 LLM 호출 없이 무비용으로 동작한다.
"""
from __future__ import annotations

import json
import logging
import os
import re

logger = logging.getLogger("alphred.verify")

# ── 완료 결과가 산출물 없이 "되물음/실패" 인지 판정(기획 P4) — 표면화에 사용 ──────────
_ATTENTION_PAT = re.compile(
    r"되묻|어떤.{0,6}(정보|파일|형식|내용).{0,6}(원하|알려|드릴|필요)|구체적으로\s*(알려|말씀)|"
    r"죄송|실패|할\s*수\s*없|못\s*(만들|생성|찾|했)|불가능|제한|429|quota|rate.?limit|"
    r"명확(히|하게)\s*(알려|말씀)|\?\s*$",
    re.IGNORECASE,
)

# 결과가 "파일을 저장/생성했다"고 주장하지만 실제 파일이 없는 환각을 잡는다(기획 P4 보강).
# 작은 모델이 write_file 을 실제 호출하지 않고 산문으로만 "저장 완료 + 경로"를 지어내는
# 사례를 파일시스템 존재 검증으로 적발한다(Hermes·Alphred 동일 호스트 전제).
_SAVE_VERB = re.compile(
    r"저장|생성|만들었|작성|기록|saved|created|wrote|written|export|generated|output",
    re.IGNORECASE)
# 따옴표 밖 경로: ~ 홈 / Windows 절대 / POSIX 절대.
_PATH_PAT = re.compile(
    r"~[\\/][^\s\"'<>|()]+"                    # ~ 홈 경로 (~/a/b)
    r"|[A-Za-z]:[\\/][^\s\"'<>|()]+"           # Windows 절대 (C:\... / C:/...)
    r"|/(?:[^\s\"'<>|()/]+/)+[^\s\"'<>|()]+"   # POSIX 절대 (/a/b/c)
)
# 따옴표/백틱 안 경로(공백 포함 허용) — 구분자 있을 때만 경로로 간주.
_QUOTED_PAT = re.compile(r"[\"'`]([^\"'`\n]{2,260})[\"'`]")
# 상대경로는 작업 cwd 가 서버측이라 신뢰성 있게 해석할 수 없어 의도적으로 다루지 않는다
# (오탐 방지). 절대/홈/따옴표 경로만 검증 대상으로 삼는다.


def _claimed_file_paths(text: str) -> list[str]:
    """결과 텍스트에서 '파일'로 보이는 경로(확장자 보유, URL 제외)를 추출·정규화."""
    cands: list[str] = []
    for m in _PATH_PAT.finditer(text or ""):
        cands.append(m.group(0).rstrip(".,);:!?'\""))
    for m in _QUOTED_PAT.finditer(text or ""):
        s = m.group(1).strip()
        if "/" in s or "\\" in s:              # 따옴표 안이지만 경로 모양일 때만
            cands.append(s)
    out: list[str] = []
    for p in cands:
        if "://" in p:                         # URL(http/https/ftp/…) 제외 — 경로엔 '://' 없음
            continue
        last = re.split(r"[\\/]", p)[-1]
        if "." not in last:                    # 디렉터리·확장자 없는 것 제외
            continue
        out.append(os.path.expanduser(p))      # ~ 전개
    return out


def claimed_missing_files(result: str | None) -> list[str]:
    """결과가 '저장/생성했다'고 주장한 절대경로 중 실제로 존재하지 않는 파일 목록.

    저장/생성 동사가 없으면(단순 답변·조사 보고) 빈 목록 → 오탐 방지.
    """
    if not result or not _SAVE_VERB.search(result):
        return []
    seen: set[str] = set()
    missing: list[str] = []
    for p in _claimed_file_paths(result):
        if p in seen:
            continue
        seen.add(p)
        try:
            if not os.path.exists(p):
                missing.append(p)
        except OSError:
            continue
    return missing


def result_needs_attention(result: str | None) -> bool:
    """완료됐지만 산출물 없이 되묻거나 실패한 결과인가(사람 확인 필요)."""
    if not result:
        return True  # 결과 자체가 비면 확인 필요
    if claimed_missing_files(result):
        return True  # "저장했다"는데 실제 파일이 없음 → 환각 의심
    tail = result.strip()[-400:]
    return bool(_ATTENTION_PAT.search(tail))


# ── §21 Tier0: 결정적 검증(무비용, 플러그형) ────────────────────────────────
# 설계: ① 형식 검증은 확장자→검증기 레지스트리(데이터 주도, register_format 로 확장).
#       ② 검증 자체는 "체커" 리스트(_CHECKERS)로 — 지금은 파일 체커뿐이나,
#          나중에 url/exit-code/no-op 등 다른 산출물 종류를 코드 골격 변경 없이 추가.
#       체크 결과 스키마(통일): {check, target, ok, detail, exists?, nonempty?}.

# 확장자 → 검증기. 값은 매직바이트 튜플(bytes) 또는 콜러블(path)->(ok, reason).
_FORMAT_VALIDATORS: dict[str, object] = {}


def register_format(ext: str, validator) -> None:
    """확장자별 형식 검증기 등록(확장 지점). validator=매직바이트 튜플|(path)->(ok,reason)."""
    _FORMAT_VALIDATORS[ext.lower()] = validator


def _validate_json(path: str) -> tuple[bool, str]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            json.load(f)
        return True, "JSON 파싱 성공"
    except Exception as e:
        return False, f"JSON 파싱 실패: {e}"


# 기본 형식(매직바이트). 목록에 없는 확장자는 존재+비어있지않음만 확인(graceful).
for _ext, _sig in {
    ".pdf": (b"%PDF",), ".png": (b"\x89PNG",),
    ".jpg": (b"\xff\xd8\xff",), ".jpeg": (b"\xff\xd8\xff",), ".gif": (b"GIF8",),
    ".docx": (b"PK\x03\x04",), ".xlsx": (b"PK\x03\x04",), ".pptx": (b"PK\x03\x04",),
    ".zip": (b"PK\x03\x04",), ".gz": (b"\x1f\x8b",),
}.items():
    register_format(_ext, _sig)
register_format(".json", _validate_json)


def _check_format(path: str) -> tuple[bool, str]:
    """파일이 확장자에 맞는 유효 형식인지 결정적 확인. (ok, reason)."""
    ext = os.path.splitext(path)[1].lower()
    v = _FORMAT_VALIDATORS.get(ext)
    if v is None:
        return True, "형식 검사 없음(존재·비어있지 않음만 확인)"
    if callable(v):
        try:
            return v(path)
        except Exception as e:
            return False, f"형식 검사 오류: {e}"
    try:
        with open(path, "rb") as f:
            head = f.read(8)
    except OSError as e:
        return False, f"열기 실패: {e}"
    if any(head.startswith(sig) for sig in v):
        return True, "형식 시그니처 일치"
    return False, f"{ext} 형식 시그니처 불일치"


def _check_files(result: str | None) -> list[dict]:
    """체커: 결과가 '저장/생성했다'고 주장한 파일을 검증(존재·비어있지않음·형식)."""
    if not (result and _SAVE_VERB.search(result)):
        return []
    seen: set[str] = set()
    checks: list[dict] = []
    for p in _claimed_file_paths(result):
        if p in seen:
            continue
        seen.add(p)
        exists = nonempty = ok = False
        detail = ""
        try:
            exists = os.path.exists(p)
            if not exists:
                detail = "파일이 존재하지 않음"
            elif os.path.getsize(p) <= 0:
                detail = "파일이 비어 있음(0바이트)"
            else:
                nonempty = True
                ok, detail = _check_format(p)
        except OSError as e:
            detail = f"검사 오류: {e}"
        checks.append({"check": "file", "target": p, "ok": ok,
                       "detail": detail, "exists": exists, "nonempty": nonempty})
    return checks


# 플러그형 체커 레지스트리 — 산출물 종류별 검증기를 여기에 추가(현재: 파일).
_CHECKERS = [_check_files]


def verify_artifacts(result: str | None) -> dict:
    """결과가 주장한 산출물을 결정적으로 검증(§21 Tier0, 무비용).

    반환: {passed, checked, checks:[{check,target,ok,detail,...}], summary}.
    검사 대상이 없으면(파일 주장 없는 단순 답변·분석) checked=0, passed=True 이되
    summary 로 '검증 안 함'을 명시한다(거짓 안심 방지). 의미적 정확성은 V2(Tier1/2) 몫.
    """
    checks: list[dict] = []
    for chk in _CHECKERS:
        try:
            checks.extend(chk(result))
        except Exception as e:
            logger.debug("checker %s 실패: %s", getattr(chk, "__name__", chk), e)
    if not checks:
        return {"passed": True, "checked": 0, "checks": [],
                "summary": "검증 대상 산출물 없음(검증 안 함 — 의미 검증은 미적용)"}
    ok = sum(1 for c in checks if c.get("ok"))
    passed = ok == len(checks)
    summary = (f"산출물 {len(checks)}개 중 {ok}개 검증 통과" if passed
               else f"산출물 {len(checks)}개 중 {len(checks) - ok}개 미통과")
    return {"passed": passed, "checked": len(checks), "checks": checks, "summary": summary}


# ── §34.6 E2: 스텝 단위 수용검사 (Plan v2 accept) ────────────────────────────
# 실행 러너가 스텝마다 호출한다. 전부 결정적(무LLM)·fail-open 지향.
# exit_code 는 외부에서 도구 종료코드를 직접 볼 수 없어(출력 텍스트만 수신) 실패 마커
# 휴리스틱으로 근사한다 — 한계를 detail 에 명시(거짓 확신 방지).
_STEP_FAIL_PAT = re.compile(
    r"traceback|exception|command not found|no such file|permission denied|"
    r"could not|cannot |failed to|error[:\s]|실패했|오류[:\s가]|설치.{0,8}(실패|불가)",
    re.IGNORECASE,
)


def _step_check_file(arg: str, output: str) -> list[dict]:
    """file 체크 — arg 가 절대/홈 경로면 그 파일을, 아니면 출력이 주장한 파일들을 검사."""
    arg = (arg or "").strip()
    p = os.path.expanduser(arg) if arg else ""
    if p and (os.path.isabs(p) or arg.startswith("~")):
        exists = nonempty = ok = False
        detail = ""
        try:
            exists = os.path.exists(p)
            if not exists:
                detail = "파일이 존재하지 않음"
            elif os.path.getsize(p) <= 0:
                detail = "파일이 비어 있음(0바이트)"
            else:
                nonempty = True
                ok, detail = _check_format(p)
        except OSError as e:
            detail = f"검사 오류: {e}"
        return [{"check": "file", "target": p, "ok": ok, "detail": detail,
                 "exists": exists, "nonempty": nonempty}]
    # 경로 힌트가 상대/부재 → 출력이 보고한 파일 주장으로 검사(§21 Tier0 재사용)
    claimed = _check_files(output)
    if claimed:
        return claimed
    return [{"check": "file", "target": arg or "(미지정)", "ok": False,
             "detail": "산출 파일 경로가 결과에 보고되지 않음 — 실제 생성/보고 필요"}]


def verify_step(step: dict, output: str) -> dict:
    """Plan v2 스텝의 accept 기준을 결정적으로 검사(§34.6 E2).

    반환: {passed, checks:[{check,target,ok,detail}], summary, feedback}.
    accept 가 없으면 통과(검사 없음 명시). feedback = 실패 항목의 보완 지시(재시도 입력용).
    """
    accept = step.get("accept") or []
    checks: list[dict] = []
    out = output or ""
    for a in accept:
        chk = (a.get("check") or "").lower()
        arg = a.get("arg") or ""
        if chk == "file":
            checks.extend(_step_check_file(arg, out))
        elif chk == "content":
            ok = bool(arg) and arg.lower() in out.lower()
            checks.append({"check": "content", "target": arg[:80], "ok": ok,
                           "detail": "출력에 포함됨" if ok else
                           "요구 문구가 결과에 없음 — 해당 내용을 실제로 산출하세요"})
        elif chk == "exit_code":
            tail = out[-600:]
            bad = _STEP_FAIL_PAT.search(tail)
            checks.append({"check": "exit_code", "target": arg or "0",
                           "ok": not bad,
                           "detail": ("실패 신호 없음(휴리스틱 — 종료코드 직접 관측 불가)"
                                      if not bad else
                                      f"실패 신호 감지: …{bad.group(0)!r}")})
    if not checks:
        return {"passed": True, "checks": [],
                "summary": "스텝 수용 기준 없음(검사 생략)", "feedback": ""}
    ok_n = sum(1 for c in checks if c.get("ok"))
    passed = ok_n == len(checks)
    fb_lines = [f"- [{c['check']}] {c.get('target', '')}: {c.get('detail', '')}"
                for c in checks if not c.get("ok")]
    return {"passed": passed, "checks": checks,
            "summary": (f"스텝 검사 {len(checks)}개 통과" if passed
                        else f"스텝 검사 {len(checks) - ok_n}개 미통과"),
            "feedback": "\n".join(fb_lines)}


def failure_suggestion(report: dict, verdict: dict | None,
                       formats: dict | None = None) -> str:
    """검증 실패에서 '다음 시도에 무엇을 고쳐야 하는지' 실행 가능한 힌트를 도출(§21 V3).

    Hermes 환경을 자동 변경하지 않는다 — 에이전트가 스스로 보완(필요시 도구 설치)하도록
    안내만 한다. 이 힌트는 Tier3 재시도 입력에 주입되고, 성공 시 Hermes background_review/
    Curator 가 스킬로 보존해 자가개선 루프가 닫힌다.

    formats — §34.5 능력 레지스트리의 형식 매트릭스({ext: {capable, via, install}}).
    있으면 "그 형식을 만들 수단이 실제로 없다/있다"를 결정적으로 짚어 준다(추측 제거).
    """
    for c in (report.get("checks") or []):
        if c.get("ok"):
            continue
        tgt = c.get("target", "")
        ext = os.path.splitext(tgt)[1].lower()
        if not c.get("exists"):
            return (f"보고만 하지 말고 적절한 도구(write_file 등)로 '{tgt}' 를 실제로 생성한 뒤 "
                    f"read_file 로 존재를 확인하세요.")
        if c.get("nonempty") and ext:
            fmt = (formats or {}).get(ext.lstrip("."))
            if fmt is not None and not fmt.get("capable"):
                lib = fmt.get("install") or "적절한 생성 라이브러리"
                return (f"'{tgt}' 가 유효한 {ext} 형식이 아닙니다. 이 런타임에는 {ext} 생성 수단이 "
                        f"없습니다 — terminal 로 `uv pip install {lib}` 를 먼저 실행해 설치한 뒤 "
                        f"그 라이브러리로 정식 {ext} 파일을 생성하세요.")
            if fmt is not None and fmt.get("capable") and fmt.get("via"):
                return (f"'{tgt}' 가 유효한 {ext} 형식이 아닙니다. 텍스트를 확장자만 바꿔 저장하지 "
                        f"말고, 이미 설치된 `{fmt['via']}` 로 정식 {ext} 파일을 생성하세요.")
            return (f"'{tgt}' 가 유효한 {ext} 형식이 아닙니다. 텍스트를 확장자만 바꿔 저장하지 말고 "
                    f"정식 {ext} 생성 도구/라이브러리를 사용하세요(없으면 terminal 로 설치 후 생성).")
        return f"'{tgt}' 산출물을 점검하세요: {c.get('detail')}"
    if verdict and verdict.get("unmet"):
        return "다음 미흡 항목을 보완하세요: " + "; ".join(verdict["unmet"][:5])
    if verdict and verdict.get("summary"):
        return verdict["summary"]
    return ""
