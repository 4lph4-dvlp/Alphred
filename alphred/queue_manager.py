"""우선순위 큐 매니저 + 단일 슬롯 스케줄러 (Phase 1).

책임:
  - submit(): 분류 → Pending 등록 → QUEUE.MD 동기화
  - 큐 조회/우선순위 변경/폐기
  - tick(): In-Progress 작업을 폴링해 마감하고, 슬롯이 비면 최고 우선순위 Pending 을 실행
선점(Heavy 중 Light 유입 시 일시중지/재개)은 Phase 2 에서 이 위에 얹는다.
"""
from __future__ import annotations

import httpx
import json
import logging
import re
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import capabilities as capabilities_mod
from . import classifier, queue_md, safety
from .prompt import (autonomous_input, append_preference, default_prompt_text,
                     intake_block, load_preferences, step_input)
from .verify import failure_suggestion, verify_artifacts, verify_step
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
# 백그라운드 실행 하네스(시스템 프롬프트) — 외부 편집 가능 파일에서 로드(§26).
# 사용자 편집본이 있으면 build_manager 가 그걸 주입(QueueManager(system_prompt=...)).
_DEFAULT_HARNESS = default_prompt_text()


def _engine_of(reason: str, prompt: str = "") -> str:
    """분류 근거 문자열 → 판정 엔진 라벨(§34.7 텔레메트리)."""
    r = reason or ""
    if r.startswith("explicit"):
        return "explicit"
    if r.startswith("intent"):
        return "intent"
    if r.startswith("plan:"):
        return "planner"
    if r.startswith("llm:"):
        return "llm"
    if classifier.is_fastpath(r, prompt):
        return "fastpath"
    return "prefilter"


class _LightMarker:
    """실시간 Light 요청이 Heavy 를 선점할 때 사용하는 가상 도전자."""
    id = "light____"
    priority = 10


class QueueManager:
    def __init__(self, store: Store, client: HermesClient, queue_md_path: Path,
                 max_slots: int = 1, max_retries: int = 3, retry_base_seconds: float = 5.0,
                 llm_classify=None, ensure_upstream=None, planner=None, verify: bool = True,
                 judge=None, judge_max_retries: int = 2, ranker=None,
                 system_prompt: str | None = None, apply_model=None, moa=None,
                 event_bus=None, capabilities=None, intent=None,
                 clarify=None, clarify_timeout: float = 600.0, planner2=None,
                 orchestrate: bool = False, task_budget: int = 25, step_retries: int = 2,
                 watchdog: bool = False, stall_seconds: float = 600.0,
                 tool_fail_limit: int = 3, prefs_path: Path | None = None):
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
        self._intent_cache: dict[tuple, dict] = {}  # §34.2 IntentCard 판정 캐시(입력+맥락)
        # §21 검증·수용: 완료 run 의 산출물을 Tier0(결정적, 무비용) 검사. 실패 시 NeedsReview.
        self.verify = verify
        # §21 Tier2: LLM-judge 콜러블(opt-in, high 한정). 미흡 시 Tier3 폐루프 재시도.
        self.judge = judge
        self.judge_max_retries = judge_max_retries
        # 큐 상대 우선순위 재정렬(§22): (new_task, queue) -> [{id,priority,reason}]|None.
        # Heavy 제출 시 큐에 비교 대상이 있으면 호출, 신규+기존 우선순위를 재배정.
        self.ranker = ranker
        # 백그라운드 실행 하네스(§26) — 외부 편집본/기본 자산. Heavy run 입력 앞에 prepend.
        self.system_prompt = system_prompt or _DEFAULT_HARNESS
        # §29.1 depth별 모델 라우팅 — 디스패치 직전 Hermes config.default 를 그 depth 모델로 맞춘다.
        #   apply_model(depth)->None. None 이면 무동작(단일 모델). 단일슬롯+선점이 호출을 직렬화.
        self.apply_model = apply_model
        # §29.4 Alphred-side MoA — high 한정 멀티에이전트 정제기. moa(request, result, depth)->str|None.
        self.moa = moa
        # §33 실행 이벤트 팬아웃 버스 — _track_run 이 Hermes /events 를 소비하며 여기로 publish,
        #     게이트웨이 라이브 스트림이 subscribe(단일소비 Hermes 를 다중구독으로 팬아웃). None=미사용.
        self.event_bus = event_bus
        # §34.5 능력 레지스트리(duck-type: harness_section()/invalidate()/snapshot()).
        # 디스패치 시 하네스 {{CAPABILITIES}} 마커에 실물 인벤토리를 주입하고, 설치류 작업
        # 완료 직후 invalidate 해 다음 스냅샷이 새 능력을 반영하게 한다. None=정적 폴백.
        self.capabilities = capabilities
        # §34.2 IntentCard 콜러블(opt-in): (prompt, context) -> intent dict|None. fail-open.
        self.intent = intent
        # §34.4 인테이크 질문 콜러블(opt-in): (prompt, missing_info, context) ->
        # {questions, assumptions_if_silent}|None. critical 부족정보가 있는 대화형 Heavy 만.
        self.clarify = clarify
        self.clarify_timeout = clarify_timeout
        # §34.3 Plan v2 콜러블(opt-in): 디스패치 직전 실행·검증 가능한 계획 생성(능력 접지).
        self.planner2 = planner2
        self._plan2_cache: dict[tuple, dict] = {}
        # §34.6 StepRunner(opt-in): high 심화도 Plan v2 작업을 스텝 단위로 실행·검증한다.
        # 스텝 상태는 plan JSON 인라인(state/attempts/output/runs_used) — 별도 테이블 없음.
        self.orchestrate = orchestrate
        self.task_budget = task_budget
        self.step_retries = step_retries
        # §34.6 E3 watchdog(opt-in): 실행 중 도구오류 루프/무진전을 감지해 중단·교정 재개.
        # 트래커 스레드는 신호만 표시(_runaway/_last_event), 개입은 tick(스케줄러)이 수행.
        self.watchdog = watchdog
        self.stall_seconds = stall_seconds
        self.tool_fail_limit = tool_fail_limit
        self._runaway: dict[str, str] = {}
        self._last_event: dict[str, float] = {}
        # §34.4 C3 선호 기억 — 인테이크 답변 축적 파일(clarify 재질문 방지 + 실행 주입).
        self.prefs_path = prefs_path
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
               plan: dict | None = None, classify_reason: str | None = None,
               depth: str | None = None, intent: dict | None = None,
               context: str | None = None) -> Task:
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
            k, prio, reason, plan, intent = self._classify(
                prompt, source=source, explicit_priority=priority, explicit_kind=kind,
                context=context,
            )
        # §21 작업 심화도 — 사용자 명시 오버라이드(/depth, X-Alphred-Depth) 우선,
        # 다음은 IntentCard 판정(§34.2), 없으면 계획 기반 자동 판정.
        if depth not in ("low", "mid", "high"):
            depth = ((intent or {}).get("depth")
                     if (intent or {}).get("depth") in ("low", "mid", "high")
                     else classifier.plan_to_depth(plan, k))
        # §34.4 인테이크 — 대화형 Heavy 착수 전 질문(추천답변 포함). 두 소스를 결합:
        #   ① 형식갭(§34.5 D4, 무LLM 결정적): 요청 형식의 생성 수단 부재 → "설치 후 진행?"
        #   ② critical 부족정보(IntentCard) → clarify LLM 질문
        # 명시 오버라이드는 사용자가 이미 결정한 것 → 질문 생략. 실패는 전부 fail-open.
        questions = assumptions = None
        state = TaskState.PENDING.value
        deadline = None
        if (self.clarify is not None and not (reason or "").startswith("explicit")
                and k == TaskKind.HEAVY.value and classifier.is_interactive_source(source)):
            qs: list[dict] = []
            asm: list[str] = []
            gap = classifier.format_gap_question(intent, self._caps_formats())
            if gap:
                qs.append(gap[0])
                asm.append(gap[1])
            if classifier.needs_clarification(intent, source=source, kind=k):
                pack = self._get_clarify(prompt, intent, context)
                if pack:
                    room = 3 - len(qs)               # 총 질문 ≤3 상한 유지
                    qs += pack["questions"][:room]
                    asm += pack["assumptions_if_silent"][:room]
            if qs:
                questions, assumptions = qs, asm
                state = TaskState.AWAITING_INPUT.value
                deadline = (datetime.now(timezone.utc)
                            + timedelta(seconds=self.clarify_timeout)).isoformat()
        with self._lock:
            task = Task(
                id=new_id(), source=source, kind=k, priority=prio,
                state=state, prompt=prompt,
                session_key=session_key,
                # 맥락 핸드오프(P3): TUI 직전 대화를 백그라운드 실행에 동봉
                conversation_history=(json.dumps(conversation_history, ensure_ascii=False)
                                      if conversation_history else None),
                delivery=json.dumps(delivery, ensure_ascii=False) if delivery else None,
                classify_reason=reason, depth=depth,
                plan=json.dumps(plan, ensure_ascii=False) if plan else None,
                intent=json.dumps(intent, ensure_ascii=False) if intent else None,
                questions=(json.dumps(questions, ensure_ascii=False) if questions else None),
                assumptions=(json.dumps(assumptions, ensure_ascii=False)
                             if assumptions else None),
                input_deadline=deadline,
            )
            self.store.create(task)
            self.sync_md()
        logger.info("submit %s kind=%s prio=%s state=%s (%s)",
                    task.id[:8], k, prio, state, reason)
        # §22 큐 상대 우선순위 재정렬: Heavy 가 큐에서 경쟁할 때만(락 밖 LLM 호출).
        # AwaitingInput 은 아직 실행 대상이 아니라 승격 시점(answer/타임아웃)에 재정렬.
        if (k == TaskKind.HEAVY.value and self.ranker is not None
                and state == TaskState.PENDING.value):
            self._rerank_heavy_queue(task.id)
            return self.store.get(task.id) or task
        return task

    def _caps_formats(self) -> dict | None:
        """능력 매트릭스의 형식 섹션(fail-open) — 인테이크 형식갭 질문·계획 접지 공용."""
        if self.capabilities is None:
            return None
        try:
            return self.capabilities.snapshot().get("formats")
        except Exception:
            return None

    def _get_clarify(self, prompt: str, card: dict | None,
                     context: str | None = None) -> dict | None:
        """질문 생성 호출(fail-open) — 실패/빈 결과면 None(질문 없이 진행).

        §34.4 C3: 축적된 선호를 동봉해 이미 답한 것을 다시 묻지 않게 한다.
        (preferences 미지원 콜러블은 TypeError 폴백으로 하위호환.)
        """
        missing = (card or {}).get("missing_info") or []
        prefs = load_preferences(self.prefs_path) if self.prefs_path is not None else None
        try:
            try:
                return self.clarify(prompt, missing, context, preferences=prefs)
            except TypeError:
                return self.clarify(prompt, missing, context)
        except Exception as e:
            logger.warning("clarify 실패(질문 없이 진행): %s", e)
            return None

    def answer(self, task_id: str, answers: list | dict) -> Task:
        """§34.4 인테이크 답변 수신 — 답변 저장 후 실행 대기열(Pending)로 승격."""
        with self._lock:
            t = self.store.get(task_id)
            if t is None:
                raise KeyError(task_id)
            if t.state != TaskState.AWAITING_INPUT.value:
                raise ValueError(f"답변 대기 상태가 아닙니다: {t.state}")
            task = self.store.transition(
                task_id, TaskState.PENDING, reason="answers received",
                answers=json.dumps(answers, ensure_ascii=False), input_deadline=None)
            self.sync_md()
        logger.info("answers %s → Pending", task_id[:8])
        # §34.4 C3 — 답변을 선호 파일에 축적(다음 인테이크에서 같은 질문 방지). fail-open.
        if self.prefs_path is not None:
            try:
                qs = json.loads(t.questions) if t.questions else []
            except Exception:
                qs = []
            items = answers if isinstance(answers, list) else [answers]
            for i, a in enumerate(items):
                if isinstance(a, dict):
                    append_preference(self.prefs_path, str(a.get("q") or ""),
                                      str(a.get("answer") or a.get("a") or ""))
                elif isinstance(a, str):
                    q = (qs[i].get("q", "") if i < len(qs) and isinstance(qs[i], dict)
                         else "")
                    append_preference(self.prefs_path, q, a)
        if task.kind == TaskKind.HEAVY.value and self.ranker is not None:
            self._rerank_heavy_queue(task.id)
            return self.store.get(task.id) or task
        return task

    _RANK_STATES = (TaskState.PENDING.value, TaskState.IN_PROGRESS.value, TaskState.PAUSED.value)

    def _rerank_heavy_queue(self, trigger_id: str) -> None:
        """Heavy 제출 시 큐의 활성 Heavy 작업들을 LLM 으로 상대 재정렬한다(§22).

        비교 대상(다른 활성 Heavy)이 없으면 LLM 을 호출하지 않는다(no-op). LLM 호출은 락 밖.
        실패/None 이면 기존 우선순위를 유지(graceful).
        """
        if self.ranker is None:
            return
        with self._lock:                              # 1) 스냅샷
            heavy = [t for t in self.store.list()
                     if t.kind == TaskKind.HEAVY.value and t.state in self._RANK_STATES]
            trigger = self.store.get(trigger_id)
        others = [t for t in heavy if t.id != trigger_id]
        if trigger is None or not others:
            return
        new_task = {"prompt": trigger.prompt, "depth": trigger.depth, "kind": trigger.kind,
                    "plan": json.loads(trigger.plan) if trigger.plan else None}
        queue_view = [{"id": t.id[:8], "prompt": t.prompt, "priority": t.priority,
                       "state": t.state, "kind": t.kind} for t in others]
        try:                                          # 2) LLM 랭킹(락 밖)
            rankings = self.ranker(new_task, queue_view)
        except Exception as e:
            logger.warning("rerank 실패(랭커 오류), 우선순위 유지: %s", e)
            return
        if not rankings:
            return
        by8 = {t.id[:8]: t.id for t in others}
        with self._lock:                              # 3) 적용
            changed = []
            for r in rankings:
                rid = str(r["id"])
                full = trigger_id if rid.upper() == "NEW" else by8.get(rid)
                if full is None:
                    continue
                cur = self.store.get(full)
                if cur is None or cur.priority == r["priority"]:
                    continue
                try:
                    self.store.set_priority(full, r["priority"])
                except ValueError:
                    continue
                changed.append((full, cur.priority, r["priority"], r.get("reason", "")))
                if full == trigger_id and r.get("reason"):
                    merged = ((cur.classify_reason or "") + f" | rank: {r['reason']}")[:240]
                    self.store.update_fields(full, classify_reason=merged)
            for full, old, new, reason in changed:
                logger.info("rerank %s prio %s→%s (%s)", full[:8], old, new, reason)
            if changed:
                self.sync_md()

    def classify_only(self, prompt: str, **kw) -> tuple[str, int, str]:
        k, prio, reason, _plan, _intent = self._classify(prompt, **kw)
        return k, prio, reason

    def classify_full(self, prompt: str, **kw) -> tuple[str, int, str, dict | None, dict | None]:
        """분류 + 계획/IntentCard 까지 반환. 게이트웨이가 submit 으로 넘겨 재활용한다."""
        return self._classify(prompt, **kw)

    def _classify(self, prompt: str, **kw
                  ) -> tuple[str, int, str, dict | None, dict | None]:
        """분류 파이프라인 → (kind, prio, reason, plan, intent). 판정은 intent_log 에 기록.

        경로(§34.2): 사전필터 fast-path(정책/즉결) → IntentCard(opt-in, LLM 1콜 통합 판정)
        → [폴백] 모호 시 계획기반(§19) → 단순 LLM 분류 → 사전필터 보수 기본값(Heavy).
        context(§34.2 A2) = 최근 대화 요약 — IntentCard 입력에만 쓰인다(사전필터는 무관).
        """
        context = kw.pop("context", None)
        k, prio, reason, plan, intent = self._classify_inner(prompt, _context=context, **kw)
        try:  # §34.7 텔레메트리 — 판정 근거를 남겨 의도 정확도를 사후 측정(fail-open)
            self.store.log_intent(
                source=kw.get("source", TaskSource.API.value),
                engine=_engine_of(reason, prompt),
                kind=k, priority=prio, depth=(intent or {}).get("depth"),
                confidence=(intent or {}).get("confidence"), reason=reason, prompt=prompt)
        except Exception:
            pass
        return k, prio, reason, plan, intent

    def _classify_inner(self, prompt: str, _context: str | None = None, **kw
                        ) -> tuple[str, int, str, dict | None, dict | None]:
        k, prio, reason, ambiguous = classifier.prefilter(prompt, **kw)
        # §34.2 IntentCard — fast-path(명시/상태조회/설치류/실시간·인사)를 제외한 전부를
        # LLM 1콜로 통합 판정. 기존의 "확신-Heavy 는 계획 없이 진입" 역설도 이 경로가 해소.
        if self.intent is not None and not classifier.is_fastpath(reason, prompt):
            card = self._get_intent(prompt, _context)
            if card is not None:
                ik, iprio, ireason = classifier.intent_to_classification(
                    card, source=kw.get("source", TaskSource.API.value))
                return ik, iprio, ireason, None, card
        if not ambiguous:
            return k, prio, reason, None, None
        # 모호 → 계획 기반 분류(§19): 분해 후 결정적 규칙으로 판정 + 계획을 실행에 재활용
        if self.planner:
            plan = self._get_plan(prompt)
            if plan:
                pk, pprio, preason = classifier.plan_to_weight(
                    plan, source=kw.get("source", TaskSource.API.value))
                return pk, pprio, preason, plan, None
        # 계획기가 없거나 실패 → 레거시 단순 LLM 분류 폴백(있으면)
        if self.llm_classify:
            try:
                r = self.llm_classify(prompt)
                if r:
                    return r[0], r[1], "llm: " + (r[2] or ""), None, None
            except Exception as e:
                logger.warning("LLM 분류 실패, 휴리스틱 유지: %s", e)
        # 최종 폴백 = 사전필터의 보수적 기본값(Heavy)
        return k, prio, reason, None, None

    def _get_intent(self, prompt: str, context: str | None = None) -> dict | None:
        """IntentCard 1회 판정·캐시(같은 입력+맥락 재질의 방지). 실패는 None(사전필터 폴백)."""
        key = ((prompt or "").strip(), (context or "")[:400])
        if key in self._intent_cache:
            return self._intent_cache[key]
        try:
            card = self.intent(prompt, context)
        except Exception as e:
            logger.warning("IntentCard 실패, 사전필터 폴백: %s", e)
            return None
        if card:
            if len(self._intent_cache) > 256:
                self._intent_cache.clear()
            self._intent_cache[key] = card
        return card

    def _get_plan_v2(self, prompt: str, *, intent: dict | None = None,
                     intake: str | None = None, draft: dict | None = None) -> dict | None:
        """Plan v2 생성(§34.3) — 능력 인벤토리 접지 + 결정적 갭 수리 + 캐시. 실패는 None.

        접지: 플래너 프롬프트에 콤팩트 인벤토리를 동봉하고, 반환된 계획을 실물과 대조해
        (plan_gaps) 미설치 형식은 설치 스텝을 앞에 삽입, 없는 스킬/CLI 힌트는 execute_code
        로 강등한다(§34.9 — 수리는 LLM 재질의가 아니라 코드가). 수리 내역은 plan.gaps.
        """
        if self.planner2 is None:
            return None
        key = ((prompt or "").strip(), (intake or "")[:400])
        if key in self._plan2_cache:
            return self._plan2_cache[key]
        caps_ctx, snapshot = None, None
        if self.capabilities is not None:
            try:
                snapshot = self.capabilities.snapshot()
                caps_ctx = capabilities_mod.planner_context(snapshot)
            except Exception:
                snapshot = None
        try:
            plan = self.planner2(prompt, capabilities=caps_ctx, intent=intent,
                                 intake=intake, draft=draft)
        except Exception as e:
            logger.warning("Plan v2 생성 실패(계획 없이 실행): %s", e)
            return None
        if not plan:
            return None
        if snapshot is not None:
            gaps = capabilities_mod.plan_gaps(plan, snapshot)
            if gaps:
                plan = capabilities_mod.apply_gap_fixes(plan, gaps)
                logger.info("plan 접지 수리 %d건: %s", len(gaps),
                            "; ".join(plan.get("gaps") or [])[:200])
        if len(self._plan2_cache) > 128:
            self._plan2_cache.clear()
        self._plan2_cache[key] = plan
        return plan

    def preview_plan(self, prompt: str, intent: dict | None = None) -> dict | None:
        """드라이런(/plan)용 Plan v2 미리보기 — 접지 포함, 실행/저장 없음."""
        return self._get_plan_v2(prompt, intent=intent)

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
            # §29.1 동기 Light 응답은 light tier 모델로(설정된 경우). Heavy 미시작 불변식 하 안전.
            if self.apply_model is not None:
                try:
                    self.apply_model("low")
                except Exception as e:
                    logger.debug("Light 모델 라우팅 적용 실패: %s", e)

    def light_end(self) -> None:
        with self._lock:
            self._active_lights = max(0, self._active_lights - 1)

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
        self._notify_delivery(task_id)   # §35.2 webhook(있으면) — 락 밖에서 조회·발사
        return t

    def purge(self, task_id: str) -> bool:
        """작업을 영구 삭제한다(복구 불가). discard 와 달리 DB 에서 완전히 제거.

        진행 중이면 Hermes run 을 먼저 중단한 뒤 삭제한다. 존재했으면 True.
        """
        with self._lock:
            task = self.store.get(task_id)
            if task is None:
                raise KeyError(task_id)
            if task.state == TaskState.IN_PROGRESS.value and task.hermes_run_id:
                try:
                    self.client.stop_run(task.hermes_run_id)
                except Exception:
                    logger.warning("purge: stop_run 실패 %s", task.hermes_run_id)
            ok = self.store.delete(task_id)
            self.sync_md()
            return ok

    def clear_history(self) -> int:
        """종료된 작업(Completed/NeedsReview/Discarded)을 영구 삭제하고 건수 반환."""
        with self._lock:
            n = self.store.delete_by_states([
                TaskState.COMPLETED.value,
                TaskState.NEEDS_REVIEW.value,
                TaskState.DISCARDED.value,
            ])
            self.sync_md()
            return n

    def purge_session(self, session_key: str) -> int:
        """해당 세션에서 생성된 모든 작업을 영구 삭제한다(세션 삭제 시 연쇄).

        세션은 작업의 출처이므로, 세션이 사라지면 그 세션에서 만든 큐 작업도 함께 제거한다.
        진행 중이면 Hermes run 을 먼저 중단한다. 삭제 건수 반환.
        """
        if not session_key:
            return 0
        with self._lock:
            tasks = [t for t in self.store.list() if t.session_key == session_key]
            for t in tasks:
                if t.state == TaskState.IN_PROGRESS.value and t.hermes_run_id:
                    try:
                        self.client.stop_run(t.hermes_run_id)
                    except Exception:
                        logger.warning("purge_session: stop_run 실패 %s", t.hermes_run_id)
                self.store.delete(t.id)
            if tasks:
                self.sync_md()
            return len(tasks)

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
        # §29.1 이 run 이 읽을 Hermes config.default 를 작업 심화도에 맞는 모델로 맞춘다(있으면).
        if self.apply_model is not None:
            try:
                self.apply_model(task.depth)
            except Exception as e:
                logger.warning("모델 라우팅 적용 실패(기본 모델 유지) %s: %s", task.id[:8], e)
        # §34.5 능력 인벤토리({{CAPABILITIES}} 주입) + §34.4 인테이크/선호 블록 — 공용 헬퍼.
        caps_text, intake = self._run_context(task)
        # §34.3 디스패치 시점 계획(Plan v2) — Heavy + 플래너 on + 아직 v2 계획 없음일 때 1회.
        # 큐 대기시간을 활용해 답변/가정까지 반영해 계획한다. 재개(Paused)·검증 재시도는
        # 저장된 계획을 재사용(재계획 없음). §19 v1 분해가 있으면 초안으로 전달(콜 재활용).
        if (self.planner2 is not None and task.kind == TaskKind.HEAVY.value
                and not (plan and (plan.get("version") == 2
                                   or isinstance(plan.get("steps"), list)))):
            intent_card = None
            if task.intent:
                try:
                    intent_card = json.loads(task.intent)
                except Exception:
                    intent_card = None
            p2 = self._get_plan_v2(task.prompt, intent=intent_card,
                                   intake=intake or None, draft=plan)
            if p2:
                plan = p2
                self.store.update_fields(task.id, plan=json.dumps(p2, ensure_ascii=False))
        # §34.6 StepRunner — high 심화도 + v2 계획이면 스텝 단위 실행으로 분기.
        # 재개(Paused→)도 이 경로: _next_step 이 완료 스텝을 건너뛰어 현재 스텝부터
        # 이어간다(E4 스텝 경계 재개 — 전체 대화 재전송 불필요).
        if self._is_orchestrated(task, plan):
            self._start_step(task, plan, caps_text=caps_text, intake=intake)
            self.sync_md()
            return
        try:
            run_id = self.client.start_run(
                # §26 하네스 + §34.5 능력 + §19 계획 힌트 + §34.4 인테이크
                # + 심화도 지시 + (재시도면) §21 검증 피드백
                autonomous_input(task.prompt, plan, task.verify_feedback,
                                 harness=self.system_prompt, depth=task.depth,
                                 capabilities=caps_text, intake=intake or None),
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

    # ---- §34.6 StepRunner — high 심화도 Plan v2 의 스텝 단위 실행·검증 ----
    @staticmethod
    def _plan_is_v2(plan: dict | None) -> bool:
        return bool(plan and isinstance(plan.get("steps"), list) and plan.get("steps"))

    def _is_orchestrated(self, task: Task, plan: dict | None) -> bool:
        return (self.orchestrate and task.kind == TaskKind.HEAVY.value
                and task.depth == "high" and self._plan_is_v2(plan))

    @staticmethod
    def _next_step(plan: dict) -> dict | None:
        """다음 실행 스텝 — 선행(needs)이 모두 done 인 첫 미완료 스텝.

        잘못된 needs 로 교착이면 첫 미완료 스텝으로 폴백(멈추지 않기). 전부 done 이면 None.
        """
        steps = plan.get("steps") or []
        done = {s.get("id") for s in steps if s.get("state") == "done"}
        for s in steps:
            if s.get("state") == "done":
                continue
            if all(n in done for n in (s.get("needs") or [])):
                return s
        for s in steps:
            if s.get("state") != "done":
                return s
        return None

    @staticmethod
    def _last_output(plan: dict) -> str:
        outs = [s.get("output") for s in plan.get("steps") or []
                if s.get("state") == "done" and s.get("output")]
        return outs[-1] if outs else ""

    def _save_plan(self, task_id: str, plan: dict) -> None:
        """plan JSON(스텝 인라인 상태 포함) 저장 + 실측 스텝 진행률 갱신."""
        done_n = sum(1 for s in plan.get("steps") or [] if s.get("state") == "done")
        self.store.update_fields(task_id, plan=json.dumps(plan, ensure_ascii=False),
                                 plan_progress=done_n)

    def _run_context(self, task: Task) -> tuple[str | None, str]:
        """스텝/단일 실행 공용 — (능력 인벤토리 텍스트, 인테이크+선호 블록). 전부 fail-open."""
        caps_text = None
        if self.capabilities is not None:
            try:
                caps_text = self.capabilities.harness_section()
            except Exception:
                pass
        try:
            intake = intake_block(
                json.loads(task.questions) if task.questions else None,
                json.loads(task.answers) if task.answers else None,
                json.loads(task.assumptions) if task.assumptions else None)
        except Exception:
            intake = ""
        # §34.4 C3 — 축적된 사용자 선호를 실행 입력에 동봉("관련 있을 때만 적용" 명시).
        if self.prefs_path is not None:
            prefs = load_preferences(self.prefs_path)
            if prefs:
                intake = ((intake + "\n\n") if intake else "") + \
                    "[USER PREFERENCES — apply when relevant, unless the request says " \
                    "otherwise]\n" + prefs
        return caps_text, intake

    def _start_step(self, task: Task, plan: dict, *,
                    caps_text: str | None = None, intake: str = "") -> None:
        """다음 스텝의 Hermes run 을 시작한다(예산 가드 → 스텝 선정 → 좁은 입력 → run)."""
        used = int(plan.get("runs_used") or 0)
        if used >= self.task_budget:                      # E5 예산 가드
            self._budget_exhausted(task, plan)
            return
        step = self._next_step(plan)
        if step is None:
            if task.verify_feedback:                      # 전체 검증 재시도 → fix 스텝 추가
                step = self._append_fix_step(plan, task.verify_feedback)
                self.store.update_fields(task.id, verify_feedback=None)
                task.verify_feedback = None
            else:                                         # 이상 상태 — 마지막 출력으로 마감
                self._save_plan(task.id, plan)
                self._finalize_done(task, self._last_output(plan))
                return
        inp = step_input(task.prompt, plan, step, capabilities=caps_text,
                         intake=intake or None, feedback=step.get("feedback"))
        step["state"] = "running"
        plan["runs_used"] = used + 1
        try:
            run_id = self.client.start_run(inp, session_id=task.session_key or task.id)
            self.store.update_fields(
                task.id, hermes_run_id=run_id,
                plan_activity=("스텝: " + (step.get("goal") or ""))[:60])
            self._save_plan(task.id, plan)
            logger.info("step %s (%s) -> run %s [run %s/%s]",
                        task.id[:8], step.get("id"), run_id,
                        plan["runs_used"], self.task_budget)
            # 스텝 진행률은 실측(§34.6)이므로 도구 카운트가 덮어쓰지 않게 한다.
            self._spawn_progress_tracker(task.id, run_id, update_progress=False)
        except Exception as exc:
            self._save_plan(task.id, plan)                # runs_used 소모는 기록 유지
            logger.warning("step 시작 실패 %s: %s", task.id[:8], exc)
            self._handle_failure(task, str(exc))

    def _finalize_step(self, task: Task, output: str) -> None:
        """스텝 run 종료 → 수용검사(E2) → done/스텝 재시도/부분성공 NeedsReview/전체 마감."""
        try:
            plan = json.loads(task.plan) if task.plan else None
        except Exception:
            plan = None
        if not self._plan_is_v2(plan):
            self._finalize_done(task, output)             # 방어 — 계획 유실 시 기존 경로
            return
        steps = plan["steps"]
        step = next((s for s in steps if s.get("state") == "running"), None)
        if step is None:
            self._finalize_done(task, output)
            return
        rep = verify_step(step, output)
        if rep["passed"]:
            step["state"] = "done"
            step["output"] = (output or "").strip()[:400]
            step.pop("feedback", None)
            if self._next_step(plan) is None and not task.verify_feedback:
                self._save_plan(task.id, plan)            # 전체 완료 → 기존 검증·마감 경로
                logger.info("steps 완료 %s (%d/%d)", task.id[:8], len(steps), len(steps))
                self._finalize_done(task, output)
                return
            self._save_plan(task.id, plan)
            caps_text, intake = self._run_context(task)
            self._start_step(task, plan, caps_text=caps_text, intake=intake)
            return
        # 스텝 수용검사 실패 → 그 스텝만 재시도(E2 — 전체 재실행 없음)
        step["attempts"] = int(step.get("attempts") or 0) + 1
        step["feedback"] = rep.get("feedback") or rep.get("summary") or ""
        if step["attempts"] > self.step_retries:
            # §34.6 재계획 1회 — 실패 맥락으로 남은 작업을 다른 접근으로 재계획.
            if self.planner2 is not None and not plan.get("replanned"):
                new_plan = self._replan(task, plan, step, rep)
                if new_plan is not None:
                    self._save_plan(task.id, new_plan)
                    logger.info("replan %s — 실패 스텝 %s 이후 재계획(%d스텝)",
                                task.id[:8], step.get("id"), len(new_plan["steps"]))
                    caps_text, intake = self._run_context(task)
                    self._start_step(task, new_plan, caps_text=caps_text, intake=intake)
                    return
            done_n = sum(1 for s in steps if s.get("state") == "done")
            self._save_plan(task.id, plan)
            report = {"passed": False, "checked": len(rep["checks"]),
                      "checks": rep["checks"], "steps_done": done_n,
                      "steps_total": len(steps),
                      "summary": (f"스텝 '{(step.get('goal') or '')[:40]}' 수용검사 "
                                  f"{step['attempts']}회 실패 — 스텝 {done_n}/{len(steps)} "
                                  f"완료(부분 성공)")}
            self._mark_needs_review(task, output, report, report["summary"])
            return
        self._save_plan(task.id, plan)
        logger.info("step verify 실패 %s (%s) — 재시도 %s/%s",
                    task.id[:8], step.get("id"), step["attempts"], self.step_retries)
        caps_text, intake = self._run_context(task)
        self._start_step(task, plan, caps_text=caps_text, intake=intake)

    def _replan(self, task: Task, plan: dict, failed_step: dict, rep: dict) -> dict | None:
        """재계획 1회(§34.6) — 완료 작업·실패 맥락을 동봉해 남은 작업만 다시 계획한다.

        접지(갭 검출/수리) 재적용, runs_used 예산 승계(E5 상한 유지), replanned 플래그로
        1회 한정, 구 계획은 previous_steps 로 이력 보존(§34.3). 실패는 None(부분성공 경로).
        """
        done = [s for s in plan.get("steps") or [] if s.get("state") == "done"]
        lines = [f"- DONE ({s.get('id')}): {s.get('goal', '')} → "
                 f"{(s.get('output') or '완료')[:150]}" for s in done]
        lines.append(f"- FAILED ({failed_step.get('id')}): {failed_step.get('goal', '')} — "
                     f"tried {failed_step.get('attempts')}x; verification said: "
                     f"{(rep.get('feedback') or rep.get('summary') or '')[:300]}")
        replan_ctx = ("REPLAN — the previous plan got stuck. Completed work and the failure "
                      "are listed below. Plan ONLY the remaining work, and use a DIFFERENT "
                      "approach for the failed part (different tool/library/simpler path). "
                      "Do NOT repeat completed steps.\n" + "\n".join(lines))
        caps_ctx, snapshot = None, None
        if self.capabilities is not None:
            try:
                snapshot = self.capabilities.snapshot()
                caps_ctx = capabilities_mod.planner_context(snapshot)
            except Exception:
                snapshot = None
        intent_card = None
        if task.intent:
            try:
                intent_card = json.loads(task.intent)
            except Exception:
                intent_card = None
        _, intake = self._run_context(task)
        try:
            new_plan = self.planner2(task.prompt, capabilities=caps_ctx, intent=intent_card,
                                     intake=intake or None, replan=replan_ctx)
        except Exception as e:
            logger.warning("replan 실패(부분성공 경로로): %s", e)
            return None
        if not new_plan:
            return None
        if snapshot is not None:
            gaps = capabilities_mod.plan_gaps(new_plan, snapshot)
            if gaps:
                new_plan = capabilities_mod.apply_gap_fixes(new_plan, gaps)
        new_plan["replanned"] = True
        new_plan["runs_used"] = int(plan.get("runs_used") or 0)   # 예산 승계(E5)
        new_plan["previous_steps"] = [
            {"id": s.get("id"), "goal": (s.get("goal") or "")[:80], "state": s.get("state")}
            for s in plan.get("steps") or []]
        return new_plan

    def _budget_exhausted(self, task: Task, plan: dict) -> None:
        """E5 — run 예산 초과: 부분 성공을 보고하고 NeedsReview(무한 루프 차단)."""
        steps = plan.get("steps") or []
        done_n = sum(1 for s in steps if s.get("state") == "done")
        report = {"passed": False, "checked": 0, "checks": [],
                  "steps_done": done_n, "steps_total": len(steps),
                  "summary": (f"작업 예산 초과(run {plan.get('runs_used')}/"
                              f"{self.task_budget}) — 스텝 {done_n}/{len(steps)} "
                              f"완료(부분 성공)")}
        self._save_plan(task.id, plan)
        partial = self._last_output(plan) or "(산출물 없음 — 스텝별 출력은 계획 상세 참조)"
        self._mark_needs_review(task, partial, report, report["summary"])

    @staticmethod
    def _append_fix_step(plan: dict, feedback: str) -> dict:
        """전체 검증(judge/Tier0) 실패 재시도를 오케스트레이션과 정합시키는 보완 스텝."""
        n = sum(1 for s in plan.get("steps") or []
                if str(s.get("id") or "").startswith("fix"))
        step = {"id": f"fix{n + 1}",
                "goal": ("Address the verification feedback and complete the "
                         "deliverable: " + feedback[:300]),
                "tool_hint": None, "needs": [],
                "expected": {"type": "text", "format": None, "path_hint": None},
                "accept": []}
        plan.setdefault("steps", []).append(step)
        return step

    def _spawn_progress_tracker(self, task_id: str, run_id: str,
                                update_progress: bool = True) -> None:
        """실 Hermes 클라이언트일 때만 /events SSE 를 소비하는 데몬 스레드 기동(테스트 fake 는 skip).

        update_progress=False(오케스트레이션 스텝 run)면 plan_progress 는 실측 스텝 수가
        유지되고, 트래커는 라이브 이벤트 팬아웃(§33)과 현재 도구 표시만 담당한다.
        """
        if not (hasattr(self.client, "_http") and getattr(self.client, "base_url", None)):
            return
        threading.Thread(target=self._track_run, args=(task_id, run_id, update_progress),
                         daemon=True, name=f"alphred-track-{task_id[:8]}").start()

    def _track_run(self, task_id: str, run_id: str, update_progress: bool = True) -> None:
        """백그라운드 run 의 이벤트 스트림을 소비 — 진행 상태 갱신(§19 P3) + 라이브 팬아웃(§33).

        Hermes `/v1/runs/{id}/events` 는 단일소비 SSE(tool.started/completed·message.delta 등).
        여기서 한 번만 소비하며 ① 완료 도구 수/현재 도구로 진행률을 갱신하고 ② 파싱한 이벤트를
        event_bus 로 publish 해 게이트웨이 라이브 스트림이 다중 구독자(TUI)에게 팬아웃한다.
        update_progress=False(§34.6 스텝 run)면 진행률은 건드리지 않는다(활동 표시만).
        """
        url = f"{self.client.base_url}/runs/{run_id}/events"
        headers = {k: v for k, v in self.client._http.headers.items()
                   if k.lower() == "authorization"}
        done = 0
        consec_fail = 0                                 # §34.6 E3 — 연속 도구 실패 카운트
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
                    self._last_event[task_id] = time.monotonic()   # E3 무진전 판정 근거
                    if self.event_bus is not None:      # §33 라이브 뷰로 팬아웃
                        self.event_bus.publish(task_id, ev)
                    name = ev.get("event")
                    if name == "tool.started":
                        self._set_progress(task_id, activity=ev.get("tool") or "")
                    elif name == "tool.completed":
                        done += 1
                        consec_fail = 0                 # 성공이 실패 루프를 끊음
                        self._set_progress(
                            task_id, progress=(done if update_progress else None),
                            activity=ev.get("tool") or "")
                    elif name == "tool.failed":
                        consec_fail += 1
                        if (self.watchdog and consec_fail >= self.tool_fail_limit
                                and task_id not in self._runaway):
                            self._flag_runaway(
                                task_id, f"연속 도구 실패 {consec_fail}회"
                                         f"({ev.get('tool') or 'tool'})")
        except Exception as e:
            logger.debug("진행 추적 종료 %s: %s", task_id[:8], e)
        finally:
            self._last_event.pop(task_id, None)
            if self.event_bus is not None:
                self.event_bus.close(task_id)           # 구독자에게 종료 알림

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

    # ---- §34.6 E3: watchdog — 잘못 가는 실행을 세우고 교정 피드백으로 재개 ----
    def _flag_runaway(self, task_id: str, reason: str) -> None:
        """트래커 스레드가 폭주 신호를 표시한다(개입은 tick 스케줄러 스레드가 수행)."""
        self._runaway[task_id] = reason
        logger.warning("watchdog 신호 %s: %s", task_id[:8], reason)

    def _watchdog_check(self) -> None:
        """실행 중 작업의 폭주(도구오류 루프)/무진전을 감지해 개입한다(tick 내 호출)."""
        active = self.store.in_progress()
        active_ids = {t.id for t in active}
        for stale in [k for k in self._runaway if k not in active_ids]:
            self._runaway.pop(stale, None)              # 종료된 작업의 잔여 신호 정리
        now_mono = time.monotonic()
        for task in active:
            if not task.hermes_run_id:
                continue
            reason = self._runaway.pop(task.id, None)
            if reason is None:
                last = self._last_event.get(task.id)
                if last is not None:                     # 트래커 가동 — 이벤트 시각 기준
                    if now_mono - last > self.stall_seconds:
                        reason = f"무진전 {int(self.stall_seconds)}s(이벤트 없음)"
                else:                                    # 트래커 미가동 — DB 갱신 시각 근사
                    try:
                        upd = datetime.fromisoformat(task.updated_at)
                        if ((datetime.now(timezone.utc) - upd).total_seconds()
                                > self.stall_seconds):
                            reason = f"무진전 {int(self.stall_seconds)}s(갱신 없음)"
                    except Exception:
                        pass
            if reason:
                self._watchdog_intervene(task, reason)

    def _watchdog_intervene(self, task: Task, reason: str) -> None:
        """중단(stop_run) → 교정 피드백과 함께 백오프 재큐. 반복 개입은 max_retries 상한."""
        if task.hermes_run_id:
            try:
                self.client.stop_run(task.hermes_run_id)
            except Exception:
                logger.warning("watchdog: stop_run 실패 %s", task.hermes_run_id)
        n = (task.retries or 0) + 1
        if n > self.max_retries:
            report = {"passed": False, "checked": 0, "checks": [],
                      "summary": f"watchdog 반복 개입 후에도 진전 없음: {reason}"}
            self._mark_needs_review(task, task.result or "", report, report["summary"])
            return
        hint = (f"이전 실행이 감시(watchdog)에 의해 중단되었습니다: {reason}. "
                "같은 접근을 그대로 반복하지 말고, 실패 원인을 먼저 진단한 뒤 다른 방법"
                "(다른 도구/라이브러리/더 단순한 경로)으로 완수하세요.")
        fields: dict = {}
        plan = None
        try:
            plan = json.loads(task.plan) if task.plan else None
        except Exception:
            plan = None
        if self._is_orchestrated(task, plan):            # 현재 스텝에 피드백(스텝 재시작 시 주입)
            step = next((s for s in plan["steps"] if s.get("state") == "running"), None)
            if step is not None:
                step["feedback"] = hint
                fields["plan"] = json.dumps(plan, ensure_ascii=False)
        else:                                            # 단발 — 재실행 입력의 피드백 슬롯 재사용
            fields["verify_feedback"] = hint
        backoff = self.retry_base_seconds * (2 ** (n - 1))
        not_before = (datetime.now(timezone.utc) + timedelta(seconds=backoff)).isoformat()
        self.store.transition(
            task.id, TaskState.PAUSED, reason=f"watchdog: {reason}",
            paused_reason=f"watchdog:{n}", retries=n, retry_not_before=not_before,
            hermes_run_id=None, plan_activity=None, **fields)
        logger.warning("watchdog 개입 %s (%s/%s): %s",
                       task.id[:8], n, self.max_retries, reason)
        self.sync_md()

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
                if self.orchestrate and task.depth == "high":
                    # §34.6 스텝 run 종료 — 수용검사 후 다음 스텝/재시도/마감을 결정.
                    # (_finalize_step 은 v2 계획이 없으면 기존 경로로 위임 — 방어적)
                    self._finalize_step(task, run.get("output", ""))
                else:
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
        # §34.4 가정 표면화 — 무응답으로 채택한 가정을 보고서에 남긴다(상세뷰/⚠ 근거).
        if task.assumptions and not task.answers:
            try:
                report["assumptions"] = json.loads(task.assumptions)
            except Exception:
                pass
        tier0_ok = report.get("passed")
        # Tier2 LLM-judge — Tier0 통과 + opt-in + high 심화도 한정(쿼터 보호), fail-open.
        verdict = None
        if tier0_ok and self.judge and task.depth == "high" and (output or "").strip():
            verdict = self._run_judge(task, output)
            if verdict is not None:
                report["judge"] = verdict
        failed = (not tier0_ok) or (verdict is not None and not verdict.get("passed"))
        if not failed:
            # §29.4 MoA — high 한정 opt-in. 통과한 결과를 비평·종합으로 한 번 더 끌어올린다(fail-open).
            final = output
            if self.moa is not None and task.depth == "high" and (output or "").strip():
                improved = self._run_moa(task, output)
                if improved and improved.strip() and improved.strip() != (output or "").strip():
                    final = improved
                    report["moa"] = {"applied": True}
            self.store.transition(
                task.id, TaskState.COMPLETED, reason="run completed",
                result=final, verify_report=json.dumps(report, ensure_ascii=False),
                plan_activity=None,
            )
            logger.info("completed %s (%s)", task.id[:8], report.get("summary"))
            # §34.5 설치/활성화류 작업이 끝나면 능력 스냅샷을 무효화 → 다음 조회가 새
            # 스킬/라이브러리를 반영한다(fail-open).
            if (self.capabilities is not None
                    and "install" in (task.classify_reason or "").lower()):
                try:
                    self.capabilities.invalidate()
                except Exception:
                    pass
            self.sync_md()
            self._notify_delivery(task.id)   # §35.2 webhook(있으면)
            return
        # 실패 → 실행 가능한 힌트 도출(§21 V3 자가개선 피드백).
        # §34.5 형식 매트릭스가 있으면 "생성 수단 부재/보유"를 결정적으로 짚는다(fail-open).
        formats = None
        if self.capabilities is not None:
            try:
                formats = self.capabilities.snapshot().get("formats")
            except Exception:
                formats = None
        suggestion = failure_suggestion(report, verdict, formats)
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
        self._notify_delivery(task.id)   # §35.2 webhook(있으면)

    def _notify_delivery(self, task_id: str) -> None:
        """§35.2 webhook — Task.delivery {"webhook": url} 이면 종결 상태를 POST(fail-open).

        임베디드/외부 서비스가 폴링 없이 완료를 받는 채널. 백그라운드 스레드에서 전송하며
        (재시도 1회·10s 타임아웃) 실패해도 작업 상태에는 영향이 없다.
        """
        t = self.store.get(task_id)
        if not t or not t.delivery:
            return
        try:
            url = (json.loads(t.delivery) or {}).get("webhook")
        except Exception:
            return
        if not url or not str(url).startswith(("http://", "https://")):
            return
        payload = {"id": t.id, "state": t.state, "prompt": (t.prompt or "")[:500],
                   "result": (t.result or "")[:4000], "error": t.error,
                   "depth": t.depth, "priority": t.priority}
        try:
            rep = json.loads(t.verify_report) if t.verify_report else None
            if isinstance(rep, dict):
                payload["verify"] = {"passed": rep.get("passed"),
                                     "summary": rep.get("summary")}
        except Exception:
            pass

        def _post():
            for attempt in (1, 2):                     # 재시도 1회
                try:
                    httpx.post(url, json=payload, timeout=10.0)
                    return
                except Exception:
                    if attempt == 1:
                        time.sleep(2)
            logger.warning("webhook 전송 실패(무시) %s → %s", task_id[:8], url)

        threading.Thread(target=_post, daemon=True,
                         name=f"alphred-webhook-{task_id[:8]}").start()

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

    def _run_moa(self, task: Task, output: str) -> str | None:
        """§29.4 MoA 정제 호출(fail-open) — 오류/불가 시 None(원본 유지)."""
        try:
            improved = self.moa(task.prompt, output)
            if improved:
                logger.info("moa %s: 개선본 채택(%d→%d자)",
                            task.id[:8], len(output or ""), len(improved))
            return improved or None
        except Exception as e:
            logger.warning("moa 실패(원본 유지) %s: %s", task.id[:8], e)
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
                    if self.orchestrate and task.depth == "high":
                        self._finalize_step(task, (run or {}).get("output", ""))
                    else:
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

    def _promote_awaiting(self) -> None:
        """§34.4 답변 대기 타임아웃 — 마감이 지난 AwaitingInput 을 가정 기록 후 Pending 승격.

        인테이크는 백그라운드 자율성을 깨지 않는다: 사용자가 답하지 않아도 작업은
        assumptions_if_silent 를 채택하고 진행되며, 가정은 상세뷰/완료보고에 표면화된다.
        """
        now = datetime.now(timezone.utc).isoformat()
        for t in self.store.list(states=[TaskState.AWAITING_INPUT.value]):
            if t.input_deadline and t.input_deadline <= now:
                self.store.transition(
                    t.id, TaskState.PENDING,
                    reason="input timeout — proceeding on assumptions",
                    input_deadline=None)
                logger.info("awaiting-input timeout %s → 가정 진행", t.id[:8])
                self.sync_md()

    def tick(self) -> None:
        # 1) 끝난 작업 마감 → 2) 필요 시 Heavy-vs-Heavy 선점 → 3) 빈 슬롯 채우기
        with self._lock:
            # 업스트림(Hermes :8642) 미가동이면 이번 틱 보류(D1: 매 틱 직접 평가 → 갇힘 없음).
            # 폐기하지 않고 다음 틱에 재평가 → :8642 복구 시 그대로 처리.
            if self.ensure_upstream is not None and not self.ensure_upstream():
                return
            self._promote_awaiting()   # §34.4 질문 타임아웃 → 가정 진행
            self._finalize_active()
            if self.watchdog:          # §34.6 E3 — 폭주/무진전 실행 개입
                self._watchdog_check()
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
