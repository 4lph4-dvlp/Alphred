"""우선순위 큐 매니저 + 단일 슬롯 스케줄러 (Phase 1).

책임:
  - submit(): 분류 → Pending 등록 → QUEUE.MD 동기화
  - 큐 조회/우선순위 변경/폐기
  - tick(): In-Progress 작업을 폴링해 마감하고, 슬롯이 비면 최고 우선순위 Pending 을 실행
선점(Heavy 중 Light 유입 시 일시중지/재개)은 Phase 2 에서 이 위에 얹는다.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import classifier, queue_md, safety
from .db import Store, new_id
from .hermes_client import HermesClient, run_outcome
from .models import Task, TaskKind, TaskSource, TaskState
from .safety import BlockedPayloadError

logger = logging.getLogger("alphred.queue")

# 일시적(재시도 가능) 실패 신호 — 쿼터/레이트리밋/혼잡/타임아웃/일시 장애/연결 실패.
# Windows 소켓 오류(WinError 10061 연결거부 등)와 한국어 연결오류 메시지도 포함한다.
_TRANSIENT = re.compile(
    r"429|503|RESOURCE_EXHAUSTED|rate.?limit|quota|overloaded|temporarily|timeout|timed out|"
    r"unavailable|connection|ECONN|reset by peer|refused|all connection attempts failed|"
    r"WinError\s*(10061|10060|10054|10053|10065)|연결|거부",
    re.IGNORECASE,
)


def is_transient_error(text: str | None) -> bool:
    return bool(text and _TRANSIENT.search(text))


# 백그라운드(큐) 작업은 대화형 사용자가 없다 → 되묻지 말고 자율 완수하도록 지시(기획 P3).
_AUTONOMOUS_PREAMBLE = (
    "[자율 백그라운드 작업] 이 작업은 대화형 사용자 없이 백그라운드에서 자동 실행됩니다. "
    "사용자에게 되묻지 마세요. 모호하면 합리적으로 가정해 **끝까지 완수**하세요.\n"
    "파일·문서 등 산출물을 만들어야 하면 말로만 끝내지 말고 반드시 적절한 도구(write_file 등)로 "
    "**실제로 생성**하세요. 생성 직후 read_file 또는 search_files 로 그 파일이 실제로 존재하는지 "
    "**검증**한 뒤에만 '완료'로 보고하세요. 실행하지 않은 작업을 했다고 말하지 마세요. "
    "PDF·xlsx·docx 처럼 특정 형식을 요청받으면 그 형식으로 실제로 열리는 **유효한 파일**을 만드세요 "
    "(텍스트를 확장자만 .pdf 로 바꿔 저장하지 말고, 코드 실행 등으로 정식 형식을 생성). "
    "완료 시 생성한 결과물의 전체 경로를 명확히 보고하고, 완수가 불가능하면 무엇을 왜 못 했는지 "
    "구체적으로 적으세요(막연한 질문/되물음 금지).\n\n요청:\n"
)


def _plan_hint(plan: dict | None) -> str:
    """분해된 계획을 실행 에이전트에 '제안' 힌트로 주입(강제 아님 → Hermes 자체 계획과 충돌 회피)."""
    subs = (plan or {}).get("subtasks") or []
    if not subs:
        return ""
    lines = [f"  {i}. {s.get('title', '')}"
             f"  [{s.get('kind', '')}/{s.get('effort', '')}]"
             + (f"  tools={','.join(s.get('tools') or [])}" if s.get("tools") else "")
             for i, s in enumerate(subs, 1)]
    return ("\n\n[제안된 하위작업 분해 — 참고/조정해 수행하세요(필요시 변경 가능)]\n"
            + "\n".join(lines))


def _autonomous_input(prompt: str, plan: dict | None = None,
                      feedback: str | None = None) -> str:
    base = _AUTONOMOUS_PREAMBLE + (prompt or "") + _plan_hint(plan)
    if feedback:
        base += ("\n\n[이전 시도가 검증을 통과하지 못했습니다 — 아래 미흡 항목을 "
                 "반드시 보완해 완수하세요]\n" + feedback)
    return base


# 완료 결과가 실제 산출물이 아니라 "되물음/실패"인지 판정(기획 P4) — 표면화에 사용.
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


def _failure_suggestion(report: dict, verdict: dict | None) -> str:
    """검증 실패에서 '다음 시도에 무엇을 고쳐야 하는지' 실행 가능한 힌트를 도출(§21 V3).

    Hermes 환경을 자동 변경하지 않는다 — 에이전트가 스스로 보완(필요시 도구 설치)하도록
    안내만 한다. 이 힌트는 Tier3 재시도 입력에 주입되고, 성공 시 Hermes background_review/
    Curator 가 스킬로 보존해 자가개선 루프가 닫힌다.
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
            return (f"'{tgt}' 가 유효한 {ext} 형식이 아닙니다. 텍스트를 확장자만 바꿔 저장하지 말고 "
                    f"정식 {ext} 생성 도구/라이브러리를 사용하세요(없으면 terminal 로 설치 후 생성).")
        return f"'{tgt}' 산출물을 점검하세요: {c.get('detail')}"
    if verdict and verdict.get("unmet"):
        return "다음 미흡 항목을 보완하세요: " + "; ".join(verdict["unmet"][:5])
    if verdict and verdict.get("summary"):
        return verdict["summary"]
    return ""


class _LightMarker:
    """실시간 Light 요청이 Heavy 를 선점할 때 사용하는 가상 도전자."""
    id = "light____"
    priority = 10


def make_hermes_classifier(client: HermesClient, model: str = "hermes-agent"):
    """Hermes chat/completions 로 모호한 입력을 분류하는 콜러블을 만든다.

    반환: prompt -> (kind, priority, reason) | None
    """
    def _classify(prompt: str):
        body = {"model": model,
                "messages": [{"role": "user", "content": classifier.build_llm_prompt(prompt)}]}
        resp = client.chat_completion(body)
        try:
            text = resp["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            return None
        return classifier.parse_classification(text)
    return _classify


def make_hermes_planner(client: HermesClient, model: str = "hermes-agent"):
    """Hermes chat/completions 로 요청을 하위작업으로 분해하는 콜러블(§19).

    반환: prompt -> plan dict {subtasks, urgent} | None
    """
    def _plan(prompt: str):
        body = {"model": model,
                "messages": [{"role": "user", "content": classifier.build_planner_prompt(prompt)}]}
        resp = client.chat_completion(body)
        try:
            text = resp["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            return None
        return classifier.parse_plan(text)
    return _plan


def make_hermes_judge(client: HermesClient, model: str = "hermes-agent"):
    """완료 결과를 수용 기준으로 채점하는 LLM-judge 콜러블(§21 Tier1+2).

    반환: (request, result) -> verdict dict {passed,score,criteria,unmet,summary} | None.
    None 이면 호출측이 fail-open(통과)으로 처리한다.
    """
    def _judge(request: str, result: str):
        body = {"model": model, "messages": [
            {"role": "user", "content": classifier.build_judge_prompt(request, result)}]}
        resp = client.chat_completion(body)
        try:
            text = resp["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            return None
        return classifier.parse_verdict(text)
    return _judge


class QueueManager:
    def __init__(self, store: Store, client: HermesClient, queue_md_path: Path,
                 max_slots: int = 1, max_retries: int = 3, retry_base_seconds: float = 5.0,
                 llm_classify=None, ensure_upstream=None, planner=None, verify: bool = True,
                 judge=None, judge_max_retries: int = 2):
        self.store = store
        self.client = client
        self.queue_md_path = Path(queue_md_path)
        self.max_slots = max_slots
        self.max_retries = max_retries
        self.retry_base_seconds = retry_base_seconds
        # 모호한 입력을 분류하는 선택적 LLM 콜러블: prompt -> (kind, prio, reason)|None
        self.llm_classify = llm_classify
        # 계획 기반 분류(§19): prompt -> plan dict|None. 모호 케이스만 호출, 결과는 캐시.
        self.planner = planner
        self._plan_cache: dict[str, dict] = {}
        # §21 검증·수용: 완료 run 의 산출물을 Tier0(결정적, 무비용) 검사. 실패 시 NeedsReview.
        self.verify = verify
        # §21 Tier2: LLM-judge 콜러블(opt-in, high 한정). 미흡 시 Tier3 폐루프 재시도.
        self.judge = judge
        self.judge_max_retries = judge_max_retries
        # 업스트림(Hermes :8642) 준비 게이트(D1). 매 틱 직접 호출 → 별도 watcher 스레드/플래그
        # 없이 단일 경로로 health 를 평가(갇힘 상태 제거). None 이면 게이트 없음(항상 가동 가정).
        # 반환: True=가동(처리 진행) / False=미가동(이번 틱 시작 보류, 폐기 아님; 다음 틱 재평가).
        self.ensure_upstream = ensure_upstream
        # 게이트웨이 요청 핸들러와 스케줄러 스레드의 상태 변경을 직렬화(QA-7.5/7.6)
        self._lock = threading.RLock()
        # 실시간 Light 작업이 진행 중인 동안에는 Heavy 슬롯을 비워둔다.
        self._active_lights = 0
        # 재시작 폭주 안전망: True 면 자동 시작/재개를 멈춘다(#30719).
        self.halted = False
        self.halt_reason = ""

    # ---- 제출/분류 ----
    def submit(self, prompt: str, *, source: str = TaskSource.API.value,
               priority: int | None = None, kind: str | None = None,
               session_key: str | None = None, delivery: dict | None = None,
               conversation_history: list | None = None,
               plan: dict | None = None, classify_reason: str | None = None) -> Task:
        # 안전망: 자기 수명주기를 건드리는 명령은 큐 진입 차단(#30719)
        matched = safety.scan_payload(prompt)
        if matched:
            logger.warning("blocked payload (lifecycle command): %r", matched)
            raise BlockedPayloadError(
                f"라이프사이클 제어 명령이 감지되어 차단됨: {matched!r}", matched)
        if classify_reason is not None:
            # 호출측(게이트웨이)이 이미 classify_full 로 분류·분해함 → 재분류 없이 그대로 사용
            # (안 그러면 explicit override 분기로 빠져 플래너 계획이 유실됨)
            k, prio, reason = kind, priority, classify_reason
        else:
            k, prio, reason, plan = self._classify(
                prompt, source=source, explicit_priority=priority, explicit_kind=kind
            )
        depth = classifier.plan_to_depth(plan, k)   # §21 작업 심화도
        with self._lock:
            task = Task(
                id=new_id(), source=source, kind=k, priority=prio,
                state=TaskState.PENDING.value, prompt=prompt,
                session_key=session_key,
                # 맥락 핸드오프(P3): TUI 직전 대화를 백그라운드 실행에 동봉
                conversation_history=(json.dumps(conversation_history, ensure_ascii=False)
                                      if conversation_history else None),
                delivery=json.dumps(delivery, ensure_ascii=False) if delivery else None,
                classify_reason=reason, depth=depth,
                plan=json.dumps(plan, ensure_ascii=False) if plan else None,
            )
            self.store.create(task)
            self.sync_md()
        logger.info("submit %s kind=%s prio=%s (%s)", task.id[:8], k, prio, reason)
        return task

    def classify_only(self, prompt: str, **kw) -> tuple[str, int, str]:
        k, prio, reason, _plan = self._classify(prompt, **kw)
        return k, prio, reason

    def classify_full(self, prompt: str, **kw) -> tuple[str, int, str, dict | None]:
        """분류 + (모호 시) 계획까지 반환. 게이트웨이가 plan 을 submit 으로 넘겨 재활용한다."""
        return self._classify(prompt, **kw)

    def _classify(self, prompt: str, **kw) -> tuple[str, int, str, dict | None]:
        """3-tier 사전필터 → 모호 시 계획기반(없으면 LLM 폴백) → (kind, prio, reason, plan)."""
        k, prio, reason, ambiguous = classifier.prefilter(prompt, **kw)
        if not ambiguous:
            return k, prio, reason, None
        # 모호 → 계획 기반 분류(§19): 분해 후 결정적 규칙으로 판정 + 계획을 실행에 재활용
        if self.planner:
            plan = self._get_plan(prompt)
            if plan:
                pk, pprio, preason = classifier.plan_to_weight(
                    plan, source=kw.get("source", TaskSource.API.value))
                return pk, pprio, preason, plan
        # 계획기가 없거나 실패 → 레거시 단순 LLM 분류 폴백(있으면)
        if self.llm_classify:
            try:
                r = self.llm_classify(prompt)
                if r:
                    return r[0], r[1], "llm: " + (r[2] or ""), None
            except Exception as e:
                logger.warning("LLM 분류 실패, 휴리스틱 유지: %s", e)
        # 최종 폴백 = 사전필터의 보수적 기본값(Heavy)
        return k, prio, reason, None

    def _get_plan(self, prompt: str) -> dict | None:
        """프롬프트별 계획 1회 생성·캐시(동일 입력 재분해 방지)."""
        key = (prompt or "").strip()
        if key in self._plan_cache:
            return self._plan_cache[key]
        try:
            plan = self.planner(prompt)
        except Exception as e:
            logger.warning("계획 분해 실패, 휴리스틱 유지: %s", e)
            return None
        if plan:
            if len(self._plan_cache) > 256:
                self._plan_cache.clear()
            self._plan_cache[key] = plan
        return plan

    # ---- Light 즉시 처리(선점 동반) ----
    def light_begin(self) -> None:
        """실시간 Light 시작 — 진행 중 Heavy 를 선점(Paused)하고 Heavy 슬롯을 잠근다."""
        with self._lock:
            self._active_lights += 1
            for t in self.store.in_progress():
                if t.kind == TaskKind.HEAVY.value:
                    self._preempt(t, _LightMarker())

    def light_end(self) -> None:
        with self._lock:
            self._active_lights = max(0, self._active_lights - 1)

    def run_light(self, prompt: str, *, previous_response_id: str | None = None) -> dict:
        """Light 작업을 Hermes 로 동기 처리하고 응답 객체를 반환한다(선점 보장)."""
        with self.light_scope():
            return self.client.respond(prompt, previous_response_id=previous_response_id)

    @contextmanager
    def light_scope(self):
        """게이트웨이 핸들러용: 진입 시 Heavy 선점, 종료 시 슬롯 해제."""
        self.light_begin()
        try:
            yield
        finally:
            self.light_end()

    # ---- 큐 조회/조작 ----
    def list(self, states: list[str] | None = None) -> list[Task]:
        return self.store.list(states)

    def get(self, task_id: str) -> Task | None:
        return self.store.get(task_id)

    def reprioritize(self, task_id: str, priority: int) -> Task:
        with self._lock:
            self.store.set_priority(task_id, priority)
            self.sync_md()
            return self.store.get(task_id)  # type: ignore[return-value]

    def discard(self, task_id: str, reason: str = "user cancel") -> Task:
        with self._lock:
            task = self.store.get(task_id)
            if task is None:
                raise KeyError(task_id)
            # 진행 중이면 Hermes run 도 중단
            if task.state == TaskState.IN_PROGRESS.value and task.hermes_run_id:
                try:
                    self.client.stop_run(task.hermes_run_id)
                except Exception:
                    logger.warning("discard: stop_run 실패 %s", task.hermes_run_id)
            t = self.store.transition(task_id, TaskState.DISCARDED, reason=reason, error=reason)
            self.sync_md()
            return t

    def pause(self, task_id: str, reason: str | None = None) -> Task:
        """사용자 명시 일시중지 — 자동 재개 대상에서 제외(user hold)."""
        with self._lock:
            task = self.store.get(task_id)
            if task is None:
                raise KeyError(task_id)
            if task.state != TaskState.IN_PROGRESS.value:
                raise ValueError(f"In-Progress 상태만 일시중지 가능 (현재 {task.state})")
            if task.hermes_run_id:
                try:
                    self.client.stop_run(task.hermes_run_id)
                except Exception:
                    logger.warning("pause: stop_run 실패 %s", task.hermes_run_id)
            t = self.store.transition(task_id, TaskState.PAUSED, reason=reason or "user pause",
                                      paused_reason=self.store.USER_HOLD)
            self.sync_md()
            return t

    def resume(self, task_id: str) -> Task:
        """사용자 보류 해제 — 다음 스케줄에서 재개 가능하도록 hold 플래그 제거."""
        with self._lock:
            task = self.store.get(task_id)
            if task is None:
                raise KeyError(task_id)
            if task.state != TaskState.PAUSED.value:
                raise ValueError(f"Paused 상태만 재개 가능 (현재 {task.state})")
            self.store.update_fields(task_id, paused_reason="manual resume")
            self.sync_md()
            return self.store.get(task_id)  # type: ignore[return-value]

    def requeue(self, task_id: str, reason: str = "manual retry") -> Task:
        """NeedsReview 작업을 사람이 확인한 뒤 다시 Pending 으로 되돌린다."""
        with self._lock:
            task = self.store.get(task_id)
            if task is None:
                raise KeyError(task_id)
            if task.state != TaskState.NEEDS_REVIEW.value:
                raise ValueError(f"NeedsReview 상태만 재시도 가능 (현재 {task.state})")
            t = self.store.transition(
                task_id,
                TaskState.PENDING,
                reason=reason,
                hermes_run_id=None,
                result=None,
                error=None,
                retry_not_before=None,
                plan_progress=0,
                plan_activity=None,
                started_at=None,
                finished_at=None,
            )
            self.sync_md()
            return t

    def set_halted(self, halted: bool, reason: str = "") -> None:
        with self._lock:
            self.halted = halted
            self.halt_reason = reason
            if halted:
                logger.error("스케줄러 정지(안전망): %s", reason)
            else:
                logger.info("스케줄러 재가동: %s", reason or "manual")

    def sync_md(self) -> None:
        queue_md.write(self.queue_md_path, self.store.list())

    # ---- 스케줄러 ----
    def _slots_free(self) -> int:
        return self.max_slots - len(self.store.in_progress())

    def _preempt(self, victim: Task, by: Task) -> None:
        """진행 중 작업을 선점하여 Paused 로 전환(컨텍스트 보존)."""
        if victim.hermes_run_id:
            try:
                self.client.stop_run(victim.hermes_run_id)
            except Exception:
                logger.warning("preempt: stop_run 실패 %s", victim.hermes_run_id)
        # 중단된 Heavy 는 Alphred 가 보관한 conversation_history(없으면 원 prompt)로 재개한다(T4-B).
        self.store.transition(
            victim.id, TaskState.PAUSED,
            reason=f"preempted by {by.id[:8]} (prio {by.priority})",
            paused_reason=f"preempted:prio{by.priority}",
        )
        logger.info("preempt %s (prio %s) <- %s (prio %s)",
                    victim.id[:8], victim.priority, by.id[:8], by.priority)
        self.sync_md()

    def _maybe_preempt(self) -> None:
        """슬롯이 가득 찼고, 진행 중 최저 우선순위보다 높은 Pending 이 있으면 선점."""
        active = self.store.in_progress()
        if len(active) < self.max_slots:
            return
        challenger = self.store.next_pending()
        if challenger is None:
            return
        victim = min(active, key=lambda t: t.priority)
        if challenger.priority > victim.priority:  # 동급/저순위는 선점하지 않음(QA-4.4)
            self._preempt(victim, challenger)

    def _start(self, task: Task) -> None:
        # Pending(신규) 또는 Paused(재개) 모두 In-Progress 로 전환. 상태머신이 둘 다 허용.
        resuming = task.state == TaskState.PAUSED.value
        reason = "scheduler resume" if resuming else "scheduler start"
        # 백오프 마커 제거(다시 실행에 들어가므로)
        self.store.transition(task.id, TaskState.IN_PROGRESS, reason=reason,
                              retry_not_before=None)
        history = json.loads(task.conversation_history) if task.conversation_history else None
        plan = json.loads(task.plan) if task.plan else None  # §19: 계획을 실행 힌트로 재활용
        if resuming:
            logger.info("resume %s (prio %s)", task.id[:8], task.priority)
        try:
            run_id = self.client.start_run(
                # P3 프리앰블 + §19 계획 힌트 + (재시도면) §21 검증 피드백
                _autonomous_input(task.prompt, plan, task.verify_feedback),
                conversation_history=history,
                previous_response_id=None if history else task.response_id,
                session_id=task.session_key or task.id,
            )
            self.store.update_fields(task.id, hermes_run_id=run_id)
            logger.info("start %s -> run %s", task.id[:8], run_id)
            self._spawn_progress_tracker(task.id, run_id)  # §19 P3: 단계(도구) 진행 추적
        except Exception as exc:
            # 시작 실패도 transient(예: :8642 연결거부)면 즉시 폐기하지 말고 재큐(백오프).
            logger.warning("start 실패 %s: %s", task.id[:8], exc)
            self._handle_failure(task, str(exc))
        self.sync_md()

    def _spawn_progress_tracker(self, task_id: str, run_id: str) -> None:
        """실 Hermes 클라이언트일 때만 /events SSE 를 소비하는 데몬 스레드 기동(테스트 fake 는 skip)."""
        if not (hasattr(self.client, "_http") and getattr(self.client, "base_url", None)):
            return
        threading.Thread(target=self._track_run, args=(task_id, run_id),
                         daemon=True, name=f"alphred-track-{task_id[:8]}").start()

    def _track_run(self, task_id: str, run_id: str) -> None:
        """백그라운드 run 의 도구 활동을 추적해 task 진행 상태를 갱신(§19 P3).

        Hermes `/v1/runs/{id}/events` 는 tool.started/completed/reasoning 만 보낸다(어시스턴트
        텍스트 없음). 따라서 '완료한 도구 수=진행, 현재 도구=활동' 으로 단계 진행을 표시한다.
        """
        import httpx
        url = f"{self.client.base_url}/runs/{run_id}/events"
        headers = {k: v for k, v in self.client._http.headers.items()
                   if k.lower() == "authorization"}
        done = 0
        try:
            with httpx.stream("GET", url, headers=headers, timeout=None) as r:
                if r.status_code != 200:
                    return
                for line in r.iter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    try:
                        ev = json.loads(line[5:].strip())
                    except Exception:
                        continue
                    name = ev.get("event")
                    if name == "tool.started":
                        self._set_progress(task_id, activity=ev.get("tool") or "")
                    elif name == "tool.completed":
                        done += 1
                        self._set_progress(task_id, progress=done,
                                           activity=ev.get("tool") or "")
        except Exception as e:
            logger.debug("진행 추적 종료 %s: %s", task_id[:8], e)

    def _set_progress(self, task_id: str, *, progress: int | None = None,
                      activity: str | None = None) -> None:
        fields: dict = {}
        if progress is not None:
            fields["plan_progress"] = progress
        if activity is not None:
            fields["plan_activity"] = activity[:60]
        if not fields:
            return
        with self._lock:  # sqlite 단일 연결 → 쓰기 직렬화
            # 종료된 작업에는 더 쓰지 않음(추적 스레드 잔류 방지)
            t = self.store.get(task_id)
            if t and t.state == TaskState.IN_PROGRESS.value:
                self.store.update_fields(task_id, **fields)

    def _finalize_active(self) -> None:
        for task in self.store.in_progress():
            if not task.hermes_run_id:
                continue
            try:
                run = self.client.get_run(task.hermes_run_id)
            except Exception:
                continue  # 일시 오류 → 다음 tick 에 재시도
            outcome = run_outcome(run.get("status"))
            if outcome == "done":
                self._finalize_done(task, run.get("output", ""))
            elif outcome == "failed":
                self._handle_failure(task, run.get("error", "run failed"))
            elif outcome == "cancelled":
                # 진행 중이던 작업이 우리가 의도하지 않게 취소됨 → 실패와 동일 처리
                self._handle_failure(task, "run cancelled unexpectedly")
            # running/unknown 이면 그대로 둔다

    def _verify_retry_budget(self, depth: str | None) -> int:
        """심화도별 검증 재시도 예산(§21 V3) — high 만 자가치유 재시도(쿼터 보호)."""
        return self.judge_max_retries if depth == "high" else 0

    def _finalize_done(self, task: Task, output: str) -> None:
        """run 정상 종료 → §21 검증(Tier0 결정적 → Tier2 judge) 후 마감/자가치유 재시도."""
        report = (verify_artifacts(output) if self.verify
                  else {"passed": True, "checked": 0, "checks": [], "summary": "검증 비활성"})
        tier0_ok = report.get("passed")
        # Tier2 LLM-judge — Tier0 통과 + opt-in + high 심화도 한정(쿼터 보호), fail-open.
        verdict = None
        if tier0_ok and self.judge and task.depth == "high" and (output or "").strip():
            verdict = self._run_judge(task, output)
            if verdict is not None:
                report["judge"] = verdict
        failed = (not tier0_ok) or (verdict is not None and not verdict.get("passed"))
        if not failed:
            self.store.transition(
                task.id, TaskState.COMPLETED, reason="run completed",
                result=output, verify_report=json.dumps(report, ensure_ascii=False),
                plan_activity=None,
            )
            logger.info("completed %s (%s)", task.id[:8], report.get("summary"))
            self.sync_md()
            return
        # 실패 → 실행 가능한 힌트 도출(§21 V3 자가개선 피드백)
        suggestion = _failure_suggestion(report, verdict)
        report["suggestion"] = suggestion
        budget = self._verify_retry_budget(task.depth)
        if (task.verify_attempts or 0) < budget:
            self._requeue_for_verify(task, report, suggestion, verdict)
        else:
            why = (report.get("summary") if not tier0_ok
                   else "수용 기준 미달: " + ((verdict or {}).get("summary") or ""))
            self._mark_needs_review(task, output, report, why)

    def _mark_needs_review(self, task: Task, output: str, report: dict, reason: str) -> None:
        self.store.transition(
            task.id, TaskState.NEEDS_REVIEW, reason="verify failed: " + reason,
            result=output, verify_report=json.dumps(report, ensure_ascii=False),
            plan_activity=None,
        )
        logger.warning("needs-review %s: %s", task.id[:8], reason)
        self.sync_md()

    def _run_judge(self, task: Task, output: str) -> dict | None:
        """LLM-judge 호출(fail-open) — 오류/불가 시 None 반환(통과로 처리)."""
        try:
            v = self.judge(task.prompt, output)
            if v:
                logger.info("judge %s: %s (score=%s)", task.id[:8],
                            "pass" if v.get("passed") else "fail", v.get("score"))
            return v or None
        except Exception as e:
            logger.warning("judge 실패(통과 처리) %s: %s", task.id[:8], e)
            return None

    def _requeue_for_verify(self, task: Task, report: dict, suggestion: str,
                            verdict: dict | None) -> None:
        """Tier3 폐루프 — 실행 가능한 힌트를 피드백으로 재큐(자동재개 Paused)."""
        n = (task.verify_attempts or 0) + 1
        gaps = (verdict or {}).get("unmet") or []
        parts = []
        if suggestion:
            parts.append(suggestion)
        parts += [f"- {g}" for g in gaps]
        fb = "\n".join(parts) or "결과가 요청을 충족하지 못함 — 보완해 다시 완수하세요."
        # 폐루프가 매 틱 즉시 재실행돼 쿼터를 소진하지 않도록 백오프(transient 재시도와 동일 패턴).
        not_before = (datetime.now(timezone.utc)
                      + timedelta(seconds=self.retry_base_seconds)).isoformat()
        self.store.transition(
            task.id, TaskState.PAUSED,
            reason=f"verify retry {n}/{self.judge_max_retries}: judge fail",
            paused_reason=f"verify-retry:{n}", verify_attempts=n, verify_feedback=fb,
            verify_report=json.dumps(report, ensure_ascii=False),
            retry_not_before=not_before, hermes_run_id=None, plan_progress=0, plan_activity=None,
        )
        logger.info("verify-retry %s (%s/%s): %d gap(s)",
                    task.id[:8], n, self.judge_max_retries, len(gaps))
        self.sync_md()

    def _handle_failure(self, task: Task, error: str) -> None:
        """실패 처리 — transient 면 백오프 후 재큐(QA-4.6), 아니면 폐기."""
        if is_transient_error(error) and task.retries < self.max_retries:
            n = task.retries + 1
            backoff = self.retry_base_seconds * (2 ** (n - 1))
            not_before = (datetime.now(timezone.utc) + timedelta(seconds=backoff)).isoformat()
            # In-Progress → Paused(자동재개 대상) + 백오프 마커. next_runnable 이 시각 이후 재선택.
            self.store.transition(
                task.id, TaskState.PAUSED,
                reason=f"transient failure, retry {n}/{self.max_retries} in {backoff:.0f}s",
                paused_reason=f"retry:{n}", retries=n, retry_not_before=not_before, error=error,
            )
            logger.warning("requeue %s (retry %s/%s, backoff %.0fs): %s",
                           task.id[:8], n, self.max_retries, backoff, error[:80])
        else:
            self.store.transition(task.id, TaskState.DISCARDED, reason="run failed", error=error)
            logger.error("discard %s after %s retries: %s", task.id[:8], task.retries, error[:80])
        self.sync_md()

    def recover(self) -> int:
        """크래시 복구(QA-7.7) — 시작 시 In-Progress 로 남은 고아 작업을 정리한다.

        Hermes run 이 이미 종료됐으면 그 결과로 마감, 아니면 재큐(Paused 자동재개)한다.
        """
        with self._lock:
            recovered = 0
            for task in self.store.in_progress():
                run = None
                if task.hermes_run_id:
                    try:
                        run = self.client.get_run(task.hermes_run_id)
                    except Exception:
                        run = None
                outcome = run_outcome((run or {}).get("status"))
                if outcome == "done":
                    self._finalize_done(task, (run or {}).get("output", ""))  # §21 검증 포함
                elif outcome == "running":
                    continue  # Hermes 에서 아직 살아있음 — 그대로 둔다
                else:
                    # 고아(프로세스 사망/실패/취소) → 재큐
                    self.store.transition(task.id, TaskState.PAUSED, reason="recovered: requeue",
                                          paused_reason="recovered")
                recovered += 1
            if recovered:
                self.sync_md()
            logger.info("recover: %s 작업 정리", recovered)
            return recovered

    def tick(self) -> None:
        # 1) 끝난 작업 마감 → 2) 필요 시 Heavy-vs-Heavy 선점 → 3) 빈 슬롯 채우기
        with self._lock:
            # 업스트림(Hermes :8642) 미가동이면 이번 틱 보류(D1: 매 틱 직접 평가 → 갇힘 없음).
            # 폐기하지 않고 다음 틱에 재평가 → :8642 복구 시 그대로 처리.
            if self.ensure_upstream is not None and not self.ensure_upstream():
                return
            self._finalize_active()
            # 안전망 발동 시 자동 시작/재개를 멈춘다(#30719 무한 재시작 방지).
            if self.halted:
                return
            # 실시간 Light 가 진행 중이면 Heavy 슬롯은 비워둔다(자원 양보).
            if self._active_lights > 0:
                return
            self._maybe_preempt()
            while self._slots_free() > 0:
                nxt = self.store.next_runnable()
                if nxt is None:
                    break
                self._start(nxt)
