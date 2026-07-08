"""alphred CLI — Hermes 의 1:1 drop-in 슈퍼셋.

설계(QA-1):
  - `alphred queue ...` 만 가로챈다(Alphred 신규).
  - 그 외 모든 서브커맨드는 `hermes` 로 **동적 위임**(verbatim). 하드코딩 목록이 아니므로
    신규 Hermes 명령도 자동 노출된다(QA-1.3/1.7). 종료코드·stdin/out/err 패스스루(QA-1.4).
"""
from __future__ import annotations

import os
import subprocess
import sys

from .config import Config
from .runtime import build_manager, resolve_task_id

# Alphred 가 직접 처리하는 서브커맨드(나머지는 전부 Hermes 로 위임)
# 주의: `gateway` 는 가로채지 않고 Hermes 로 위임해 1:1 매핑 유지(QA-1.3).
#       Alphred 자체 HTTP 게이트웨이는 `alphred serve` 로 띄운다.
_INTERCEPT = {"queue", "serve", "setup", "tui", "doctor", "prompt", "tune",
              "keys", "connect", "service", "model"}


def _delegate_to_hermes(argv: list[str]) -> int:
    cfg = Config.load()
    if not cfg.hermes_bin:
        sys.stderr.write(
            "alphred: hermes 실행 파일을 찾을 수 없습니다. "
            "PATH 에 추가하거나 ALPHRED_HERMES_BIN 을 설정하세요.\n"
        )
        return 127
    # stdio 를 그대로 상속 → 색상/스트리밍/대화형 입력 보존.
    # PYTHONUTF8=1: 한국어 Windows(cp949) 로캘에서 Hermes 의 유니코드 입출력 안정화.
    env = dict(os.environ)
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    proc = subprocess.run([cfg.hermes_bin, *argv], env=env)
    return proc.returncode


def _cmd_queue(argv: list[str]) -> int:
    import argparse

    p = argparse.ArgumentParser(prog="alphred queue", description="Alphred 우선순위 큐 관리")
    sub = p.add_subparsers(dest="action", required=True)

    sub.add_parser("list", help="큐 조회")
    sp = sub.add_parser("submit", help="작업 제출")
    sp.add_argument("prompt")
    sp.add_argument("--priority", type=int, default=None)
    sp.add_argument("--kind", choices=["light", "heavy"], default=None)
    sp.add_argument("--depth", choices=["low", "mid", "high"], default=None,
                    help="작업 심화도 고정(미지정 시 자동 판정)")
    sp.add_argument("--source", default="api")
    g = sub.add_parser("show", help="작업 상세"); g.add_argument("id")
    pr = sub.add_parser("prio", help="우선순위 변경"); pr.add_argument("id"); pr.add_argument("priority", type=int)
    dc = sub.add_parser("discard", help="작업 폐기"); dc.add_argument("id")
    pg = sub.add_parser("purge", help="작업 영구 삭제(복구 불가)"); pg.add_argument("id")
    sub.add_parser("clear", help="종료된 작업(완료/검토/폐기) 영구 삭제")
    pa = sub.add_parser("pause", help="진행 중 작업 일시중지(사용자)"); pa.add_argument("id")
    re_ = sub.add_parser("resume", help="일시중지 작업 재개 허용"); re_.add_argument("id")
    rt = sub.add_parser("retry", help="검토 필요 작업을 다시 대기열에 올림"); rt.add_argument("id")
    aq = sub.add_parser("ask", help="자연어로 큐 조회/우선순위 변경/삭제")
    aq.add_argument("request")
    sub.add_parser("tick", help="스케줄러 1회 실행")
    rn = sub.add_parser("run", help="스케줄러 루프 실행"); rn.add_argument("--interval", type=float, default=2.0)
    sub.add_parser("sync", help="QUEUE.MD 재생성")
    sf = sub.add_parser("safety", help="안전망 상태/리셋(#30719)"); sf.add_argument("--reset", action="store_true")
    sub.add_parser("cron-tick", help="만료된 cron 작업을 큐로 편입(1회)")
    su = sub.add_parser("scout-update", help="§39 Scout 작업을 수행하여 모델 카탈로그를 갱신")
    su.add_argument("--verbose", "-v", action="store_true", help="카나리아 테스트 상세 출력")
    su.add_argument("--free", action="store_true", help="무료 모델(free)만 카탈로그에 채택")

    args = p.parse_args(argv)
    cfg = Config.load()
    mgr, store, client = build_manager(cfg)
    try:
        if args.action == "list":
            _print_list(mgr.list())
        elif args.action == "submit":
            from .safety import BlockedPayloadError
            try:
                t = mgr.submit(args.prompt, source=args.source, priority=args.priority,
                               kind=args.kind, depth=args.depth)
            except BlockedPayloadError as e:
                print(f"차단됨(안전망): {e.reason}")
                return 3
            print(f"제출됨: {t.id}  kind={t.kind} prio={t.priority} state={t.state}")
            print(f"사유: {t.classify_reason}")
        elif args.action == "safety":
            from .safety import RestartGuard
            guard = RestartGuard(cfg.guard_path, cfg.restart_window_seconds, cfg.restart_threshold)
            if args.reset:
                guard.reset()
                print("안전망 리셋됨 (재시작 기록 삭제)")
            print(f"재시작 기록: {guard.count()}회 / {cfg.restart_window_seconds:.0f}초 "
                  f"(임계 {cfg.restart_threshold}) → tripped={guard.tripped()}")
        elif args.action == "show":
            t = mgr.get(resolve_task_id(store, args.id))
            if not t:
                print("없음"); return 1
            _print_task(t, store)
        elif args.action == "prio":
            t = mgr.reprioritize(resolve_task_id(store, args.id), args.priority)
            print(f"변경됨: {t.id[:8]} → priority={t.priority}")
        elif args.action == "discard":
            t = mgr.discard(resolve_task_id(store, args.id))
            print(f"폐기됨: {t.id[:8]} → {t.state}")
        elif args.action == "purge":
            tid = resolve_task_id(store, args.id)
            ok = mgr.purge(tid)
            print(f"영구 삭제됨: {tid[:8]}" if ok else "대상 없음")
        elif args.action == "clear":
            n = mgr.clear_history()
            print(f"종료된 작업 {n}건 영구 삭제됨")
        elif args.action == "pause":
            t = mgr.pause(resolve_task_id(store, args.id))
            print(f"일시중지됨: {t.id[:8]} → {t.state}")
        elif args.action == "resume":
            t = mgr.resume(resolve_task_id(store, args.id))
            print(f"재개 허용됨: {t.id[:8]} (다음 스케줄에서 재개)")
        elif args.action == "retry":
            t = mgr.requeue(resolve_task_id(store, args.id))
            print(f"재시도 대기열 등록: {t.id[:8]} → {t.state}")
        elif args.action == "ask":
            from . import nlq
            out = nlq.ask(mgr, store, args.request, nlq.make_hermes_llm(client))
            if out["reply"]:
                print(out["reply"])
            for r in out["results"]:
                print(f"  • {r}")
        elif args.action == "tick":
            mgr.tick(); print("tick 완료"); _print_list(mgr.list())
        elif args.action == "run":
            _run_loop(mgr, args.interval)
        elif args.action == "sync":
            mgr.sync_md(); print(f"QUEUE.MD 갱신: {cfg.queue_md_path}")
        elif args.action == "scout-update":
            from .scout import run_scout_update
            import os
            or_key = os.environ.get("OPENROUTER_API_KEY")
            nim_key = os.environ.get("NVIDIA_API_KEY")
            success = run_scout_update(cfg.alphred_home, openrouter_key=or_key, nim_key=nim_key, verbose=args.verbose, free_only=args.free)
            if success:
                from .config import sync_model_routes
                sync_model_routes(cfg.alphred_home, cfg.hermes_home)
                print("Scout 업데이트 및 model_routes 동기화 완료")
            else:
                print("Scout 업데이트 실패")
                return 1
            return 0
        elif args.action == "cron-tick":
            from .cron_intercept import CronIntercept
            cron = CronIntercept(mgr, cfg.cron_jobs_path, cfg.cron_state_path)
            ids = cron.tick()
            print(f"cron 편입: {len(ids)}건  (jobs={cfg.cron_jobs_path})")
            for i in ids:
                print(f"  → {i[:8]}")
        return 0
    finally:
        client.close()
        store.close()


def _print_list(tasks) -> None:
    if not tasks:
        print("(큐가 비어 있음)"); return
    print(f"{'ID':10} {'PRIO':>4} {'STATE':12} {'KIND':6} PROMPT")
    for t in tasks:
        prompt = (t.prompt or "").replace("\n", " ")[:50]
        print(f"{t.id[:8]:10} {t.priority:>4} {t.state:12} {t.kind:6} {prompt}")


def _print_task(t, store) -> None:
    print(f"ID:        {t.id}")
    print(f"상태:      {t.state}   우선순위: {t.priority}   kind: {t.kind}")
    print(f"소스:      {t.source}   분류근거: {t.classify_reason}")
    print(f"run_id:    {t.hermes_run_id}")
    print(f"생성:      {t.created_at}")
    print(f"prompt:    {t.prompt}")
    if t.result:
        print(f"결과:\n{t.result}")
    if t.error:
        print(f"에러:      {t.error}")
    print("이벤트:")
    for e in store.events(t.id):
        print(f"  {e['at']}  {e['from_state']} → {e['to_state']}  ({e['reason']})")


def _run_loop(mgr, interval: float) -> None:
    import time
    print(f"스케줄러 루프 시작 (interval={interval}s, Ctrl+C 종료)")
    try:
        while True:
            mgr.tick()
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n스케줄러 종료")


def main(argv: list[str] | None = None) -> int:
    try:  # Windows 콘솔(cp949)에서도 한글/유니코드 출력 보존
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
        sys.stdin.reconfigure(encoding="utf-8")
    except Exception:
        pass
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in _INTERCEPT:
        if argv[0] == "queue":
            return _cmd_queue(argv[1:])
        if argv[0] == "serve":
            return _cmd_serve(argv[1:])
        if argv[0] == "setup":
            return _cmd_setup(argv[1:])
        if argv[0] == "tui":
            return _cmd_tui(argv[1:])
        if argv[0] == "doctor":
            return _cmd_doctor(argv[1:])
        if argv[0] == "prompt":
            return _cmd_prompt(argv[1:])
        if argv[0] == "tune":
            return _cmd_tune(argv[1:])
        if argv[0] == "keys":
            return _cmd_keys(argv[1:])
        if argv[0] == "connect":
            return _cmd_connect(argv[1:])
        if argv[0] == "service":
            return _cmd_service(argv[1:])
        if argv[0] == "model":
            return _cmd_model(argv[1:])
    # `alphred chat` 은 제거됨 — 순정 Hermes TUI 는 `hermes` 로 직접 실행한다.
    # (Hermes 에 chat 서브커맨드가 없어 'chat' 토큰을 그대로 넘기면 오해석되므로 명시 안내.)
    if argv and argv[0] == "chat":
        sys.stderr.write("alphred: `alphred chat` 은 제거되었습니다. "
                         "순정 Hermes TUI 가 필요하면 `hermes` 를 실행하세요.\n")
        return 2
    # 무인자 `alphred` = 전용 Alphred TUI(기획 §13).
    if not argv or argv == ["--no-daemon"]:
        return _cmd_tui(["--no-daemon"] if argv == ["--no-daemon"] else [])
    # 그 외 전부 Hermes 로 위임
    return _delegate_to_hermes(argv)


def _ensure_daemon():
    """:8643 Alphred 서비스를 보장한다.

    이미 떠 있으면(예: 사용자가 직접 `alphred serve` 실행) None 을 반환해 건드리지 않는다.
    안 떠 있으면 **창 없이** TUI 수명에 묶인 자식으로 기동하고 그 Popen 을 반환한다 →
    호출자(_cmd_tui)가 TUI 종료 시 함께 정리한다. best-effort(실패해도 None).
    """
    try:
        import httpx
        from urllib.parse import urlparse, urlunparse
        from .childproc import spawn_managed
        cfg = Config.load()

        parsed = urlparse(cfg.gateway_url)
        host = parsed.hostname or "127.0.0.1"
        port = str(parsed.port or 8643)

        def _resolve_ping_url(url: str) -> str:
            try:
                p = urlparse(url)
                if p.hostname == "0.0.0.0":
                    netloc = f"127.0.0.1:{p.port}" if p.port is not None else "127.0.0.1"
                    return urlunparse(p._replace(netloc=netloc))
            except Exception:
                pass
            return url

        ping_url = _resolve_ping_url(cfg.gateway_url)

        try:
            if httpx.get(f"{ping_url}/", timeout=2.0).status_code < 500:
                return None  # 이미 가동 중 — 우리가 띄운 게 아니므로 정리 대상 아님
        except Exception:
            pass
        proc = spawn_managed([sys.executable, "-m", "alphred.cli", "serve", "--host", host, "--port", port],
                             log_path=cfg.alphred_home / "serve.log")
        sys.stderr.write("alphred: 백그라운드 큐 서비스를 시작하는 중...\n")
        # :8643 준비를 잠시 대기 → 첫 메시지부터 라우팅 훅이 살아있도록(기획 Fix B).
        # serve 는 Hermes 준비를 블로킹하지 않으므로 보통 1~2초면 뜬다.
        import time
        ready = False
        for _ in range(30):  # 최대 ~15초
            try:
                if httpx.get(f"{ping_url}/", timeout=1.0).status_code < 500:
                    ready = True
                    break
            except Exception:
                pass
            time.sleep(0.5)
        if ready:
            sys.stderr.write("alphred: 큐 서비스 준비 완료.\n")
        else:
            sys.stderr.write("alphred: 큐 준비가 지연됩니다 (채팅은 정상; 큐는 곧 활성화). "
                              "끄려면 ALPHRED_NO_DAEMON=1 또는 `alphred --no-daemon`.\n")
        return proc
    except Exception:
        return None  # 데몬 기동 실패가 TUI 진입을 막지 않도록


def _cmd_setup(argv: list[str]) -> int:
    import argparse
    import sys
    from urllib.parse import urlparse
    from .config import PROFILES, read_profile, set_profile

    p = argparse.ArgumentParser(
        prog="alphred setup",
        description="Alphred 초기 및 상세 설정 마법사")
    p.add_argument("--profile", choices=PROFILES, default=None,
                   help="프로파일 프리셋 영구 설정 (basic, smart, full)")
    args = p.parse_args(argv)

    cfg = Config.load()
    env_path = cfg.alphred_home / ".env"

    def _prompt_input(prompt_text: str, default_value: str | None = None) -> str:
        default_str = f" [{default_value}]" if default_value is not None else ""
        try:
            val = input(f"{prompt_text}{default_str}: ").strip()
        except (EOFError, KeyboardInterrupt):
            val = ""
        return val if val else (default_value or "")

    # 만약 TTY가 아니거나 인자로 profile만 직접 넘어왔다면 비대화형 실행
    if not sys.stdin.isatty() or args.profile:
        profile = args.profile or read_profile(cfg.alphred_home) or "smart"
        set_profile(cfg.alphred_home, profile)
        print(f"프로파일 설정됨: {profile}")
        # 비대화형일 때 기본 .env가 없으면 기본값으로 자동 생성
        if not env_path.exists():
            parsed = urlparse(cfg.gateway_url)
            host = parsed.hostname or "localhost"
            port = str(parsed.port or 8643)
            env_template = (
                "# ==============================================================================\n"
                "# Alphred Environment Configuration\n"
                "# ==============================================================================\n"
                "# 이 파일은 Alphred 설정 마법사에 의해 자동 생성된 환경변수 파일입니다.\n"
                "# ==============================================================================\n\n"
                "# [네트워크 및 접속 설정]\n"
                f"ALPHRED_GATEWAY_URL=http://{host}:{port}\n\n"
                "# ALPHRED_API_KEY=\n\n"
                f"ALPHRED_HERMES_API={cfg.api_base_url}\n"
            )
            try:
                env_path.write_text(env_template, encoding="utf-8")
                print(f"환경변수 설정 파일 생성됨: {env_path}")
            except Exception as e:
                sys.stderr.write(f"alphred: .env 생성 실패 ({e})\n")
        print("설정이 완료되었습니다.")
        return 0

    print("================================================================================")
    print("Alphred 설정 마법사에 오신 것을 환영합니다!")
    print("================================================================================\n")

    # 1. 설정 모드 선택
    print("설정 모드를 선택해 주세요:")
    print("  1) 기본 설정 (Minimum) - 필수적인 네트워크 및 인증 설정")
    print("  2) 전체 상세 설정 (Full) - 리소스 제한, 모델 오버라이드 및 고급 기능 포함")
    mode = _prompt_input("선택 [1]", "1")

    # 2. 동작 프로파일 설정
    curr_prof = read_profile(cfg.alphred_home) or "smart"
    print("\n동작 프로파일을 선택해 주세요:")
    print("  1) basic - 큐/선점/검증 위주 (최소 LLM 호출)")
    print("  2) smart - + 의도판정 및 실행계획 자동 수립 (기본값, 권장)")
    print("  3) full  - + 착수 전 질문, 스텝 단위 실행 및 실행 감시")
    default_opt = "2" if curr_prof == "smart" else ("1" if curr_prof == "basic" else "3")
    pick_prof = _prompt_input("선택", default_opt)
    profile = {"1": "basic", "2": "smart", "3": "full"}.get(pick_prof, "smart")
    set_profile(cfg.alphred_home, profile)
    print(f"-> 프로파일 설정됨: {profile}")

    # 3. 네트워크 및 접속 설정
    parsed = urlparse(cfg.gateway_url)
    curr_host = parsed.hostname or "localhost"
    curr_port = str(parsed.port or 8643)

    host = _prompt_input("\n게이트웨이 호스트 주소를 입력하세요 (로컬 전용: localhost, 외부/Tailscale 허용: 0.0.0.0 또는 특정 IP)", curr_host)
    port = _prompt_input("게이트웨이 포트 번호를 입력하세요", curr_port)
    gateway_url = f"http://{host}:{port}"

    curr_key = cfg.api_key or ""
    api_key = _prompt_input("\n게이트웨이 보안 접속 키(API Key)를 설정하세요 (외부 접속 허용 시 권장, 빈 칸이면 비활성화)", curr_key)

    curr_hermes = cfg.api_base_url or "http://localhost:8642/v1"
    hermes_api = _prompt_input("\n연동할 업스트림 Hermes API 주소를 입력하세요", curr_hermes)

    # 4. Full 상세 설정 (모드 2일 때만 실행)
    slots = "1"
    slots_max = 4
    model_high = ""
    model_mid = ""
    model_low = ""
    orchestrate = "1" if profile == "full" else "0"
    watchdog = "1" if profile == "full" else "0"
    judge = "0"
    moa = "0"

    if mode == "2":
        print("\n--- [추가 리소스 및 고급 파이프라인 설정 (Full 모드)] ---")
        curr_slots = getattr(cfg, "slots", "1")
        curr_slots_max = getattr(cfg, "slots_max", 4)
        slots = _prompt_input("동시 실행 슬롯 개수를 설정하세요 (정수 또는 auto)", str(curr_slots))
        slots_max = int(_prompt_input("최대 동시 실행 슬롯 개수 제한을 설정하세요", str(curr_slots_max)))

        print("\n[LLM 모델 오버라이드 설정 (미입력 시 기본 모델 사용)]")
        model_high = _prompt_input("High 심화도 작업용 LLM 모델 지정 (예: openrouter/google/gemini-2.5-pro)", getattr(cfg, "model_high", "") or "")
        model_mid = _prompt_input("Mid 심화도 작업용 LLM 모델 지정 (예: openrouter/google/gemini-2.5-flash)", getattr(cfg, "model_mid", "") or "")
        model_low = _prompt_input("Low 심화도 작업용 LLM 모델 지정", getattr(cfg, "model_low", "") or "")

        print("\n[고급 파이프라인 기능 수동 제어 (1: 활성화, 0: 비활성화)]")
        d_orch = "1" if getattr(cfg, "orchestrate", profile == "full") else "0"
        d_watch = "1" if getattr(cfg, "watchdog", profile == "full") else "0"
        d_judge = "1" if getattr(cfg, "judge", False) else "0"
        d_moa = "1" if getattr(cfg, "moa", False) else "0"
        
        orchestrate = _prompt_input("Plan v2 스텝 단위 자율 실행(StepRunner) 여부", d_orch)
        watchdog = _prompt_input("도구오류 복구/무진전 감시(Watchdog) 여부", d_watch)
        judge = _prompt_input("LLM-judge 기반 완료 검증 여부", d_judge)
        moa = _prompt_input("Mixture of Agents(MoA) 활성화 여부", d_moa)

    # 5. .env 파일 생성 및 저장
    env_lines = [
        "# ==============================================================================",
        "# Alphred Environment Configuration",
        "# ==============================================================================",
        "# 이 파일은 Alphred 설정 마법사(setup)에 의해 자동 생성된 환경변수 파일입니다.",
        "# ==============================================================================\n",
        "# [네트워크 및 접속 설정]",
        f"ALPHRED_GATEWAY_URL={gateway_url}",
    ]
    if api_key:
        env_lines.append(f"ALPHRED_API_KEY={api_key}")
    else:
        env_lines.append("# ALPHRED_API_KEY=")
    env_lines.append(f"ALPHRED_HERMES_API={hermes_api}")

    if mode == "2":
        env_lines.extend([
            "\n# [실행 리소스 설정]",
            f"ALPHRED_SLOTS={slots}",
            f"ALPHRED_SLOTS_MAX={slots_max}",
            "\n# [LLM 모델 라우팅 오버라이드]",
        ])
        env_lines.append(f"ALPHRED_MODEL_HIGH={model_high}" if model_high else "# ALPHRED_MODEL_HIGH=")
        env_lines.append(f"ALPHRED_MODEL_MID={model_mid}" if model_mid else "# ALPHRED_MODEL_MID=")
        env_lines.append(f"ALPHRED_MODEL_LOW={model_low}" if model_low else "# ALPHRED_MODEL_LOW=")
        
        env_lines.extend([
            "\n# [고급 파이프라인 수동 제어]",
            f"ALPHRED_ORCHESTRATE={orchestrate}",
            f"ALPHRED_WATCHDOG={watchdog}",
            f"ALPHRED_JUDGE={judge}",
            f"ALPHRED_MOA={moa}",
        ])
    
    try:
        env_path.write_text("\n".join(env_lines) + "\n", encoding="utf-8")
        print(f"\n-> 환경변수 설정 저장 완료: {env_path}")
    except Exception as e:
        sys.stderr.write(f"\nalphred: .env 저장 실패 ({e})\n")

    # 6. Hermes 최적화(tune) 연동 질문
    print("\n================================================================================")
    print("Hermes 엔진 최적화(Tuning)")
    print("================================================================================")
    tune_ans = _prompt_input("Hermes 엔진의 최적화 권장 설정(knobs)을 자동으로 점검하고 적용하시겠습니까? (y/n)", "n")
    if tune_ans.lower() in ("y", "yes"):
        try:
            from . import tune as _tune
            print("Hermes 설정을 최적화하는 중...")
            tune_res = _tune.apply(cfg, None)
            print(f"-> 적용 완료: {', '.join(tune_res['applied']) or '(없음 — 이미 권장값이거나 키 부재)'}")
            if tune_res["skipped"]:
                print(f"-> 건너뜀: {', '.join(tune_res['skipped'])}")
            print(f"-> 백업 생성: {tune_res['backup']}")
        except Exception as e:
            print(f"-> 최적화 적용 실패: {e}")

    # 7. 완료 및 종료 안내 메시지
    print("\n================================================================================")
    print("Alphred 설정이 성공적으로 완료되었습니다!")
    print("================================================================================\n")
    print(f"설정 파일 보존 경로: {env_path}")
    print("\nTUI 챗봇 기동하기:")
    print("  $ alphred")
    print("\n다른 기기에서 원격 접속 허용하기:")
    print("  1. 서버에서 접속 보안 키 발급:")
    print("     $ alphred keys issue <기기이름>")
    print("  2. 접속할 기기에서 연결:")
    print(f"     $ alphred connect http://{host if host != '0.0.0.0' else '<서버IP>'}:{port} --key <발급된키>")
    print("\n================================================================================\n")
    return 0


def _cmd_prompt(argv: list[str]) -> int:
    """백그라운드 실행 하네스(시스템 프롬프트, §26) 보기/경로/편집본 생성."""
    import argparse
    from . import prompt as _prompt

    p = argparse.ArgumentParser(
        prog="alphred prompt",
        description="실행 하네스 관리 — 기본=백그라운드 Heavy(시스템 프롬프트), --light=즉답(Light) 하네스")
    p.add_argument("--light", action="store_true",
                   help="대상을 Light(즉답) 하네스로 — §29.2, 동기 응답 품질")
    p.add_argument("--show", action="store_true", help="현재 적용 중인 하네스 전문 출력")
    p.add_argument("--path", action="store_true", help="적용 중인 소스(편집본/기본) 경로 표시")
    p.add_argument("--init", action="store_true",
                   help="기본 하네스를 ALPHRED_HOME 의 편집본으로 복사(편집 시작점)")
    p.add_argument("--force", action="store_true", help="--init 시 기존 편집본 덮어쓰기")
    args = p.parse_args(argv)

    cfg = Config.load()
    light = args.light
    label = "Light(즉답) 하네스" if light else "백그라운드 실행 하네스(시스템 프롬프트)"
    user_path = (_prompt.user_light_prompt_path(cfg.alphred_home) if light
                 else _prompt.user_prompt_path(cfg.alphred_home))
    using_user = user_path.exists()
    _init = _prompt.init_user_light_prompt if light else _prompt.init_user_prompt
    _load = (lambda h: _prompt.load_light_harness(h)) if light else _prompt.load_harness

    if args.init:
        path, wrote = _init(cfg.alphred_home, overwrite=args.force)
        if wrote:
            print(f"편집용 {label} 를 생성했습니다: {path}")
            scope = "동기 Light 응답" if light else "이후 모든 Heavy 작업"
            print(f"이 파일을 수정하면 {scope}에 반영됩니다(데몬 재기동 필요).")
        else:
            print(f"이미 편집본이 있습니다: {path}  (덮어쓰려면 --force)")
        return 0
    if args.path:
        print(f"대상: {label}")
        print(f"적용 소스: {'사용자 편집본' if using_user else '패키지 기본값'}")
        print(f"편집본 경로: {user_path}  ({'존재' if using_user else '없음 — --init 로 생성'})")
        return 0
    if args.show:
        print(_load(cfg.alphred_home))
        return 0
    # 기본: 요약
    print(label)
    if light:
        print(f"  활성(ALPHRED_LIGHT_HARNESS): {'ON' if cfg.light_harness else 'OFF'}")
    print(f"  적용 소스: {'사용자 편집본' if using_user else '패키지 기본값'}")
    print(f"  편집본 경로: {user_path}")
    print("  명령: [--light] --show(전문) · --path(경로) · --init(편집본 생성) [--force]")
    return 0


def _cmd_tune(argv: list[str]) -> int:
    """§29.3 Hermes 설정 품질 감사·적용 — 보고서 #1/#2/#4/#5 완화(코어 무수정, 백업·원복)."""
    import argparse

    from . import tune as _tune

    p = argparse.ArgumentParser(
        prog="alphred tune",
        description="Hermes config 품질 감사(기본=읽기전용). --apply 로 동의 적용, --revert 로 원복. "
                    "--get/--set 으로 임의 스칼라(agent.max_turns 등) 조회·조정.")
    p.add_argument("--apply", nargs="*", metavar="ID", default=None,
                   help="권장 설정 적용(인자 없으면 적용 가능한 전부, 또는 knob id 나열). 백업 생성.")
    p.add_argument("--revert", action="store_true", help="tune 백업으로 config.yaml 원복")
    p.add_argument("--get", metavar="PATH",
                   help="config.yaml 스칼라 조회 (점 표기, 예: agent.max_turns)")
    p.add_argument("--set", nargs=2, metavar=("PATH", "VALUE"),
                   help="config.yaml 스칼라 설정(백업 후, 예: --set agent.max_turns 200)")
    p.add_argument("--json", action="store_true", help="감사 결과를 JSON 으로 출력")
    args = p.parse_args(argv)
    cfg = Config.load()

    if args.revert:
        ok = _tune.revert(cfg)
        print("원복 완료(config.yaml 백업 복원)." if ok else "원복할 tune 백업이 없습니다.")
        return 0 if ok else 1

    if args.get:
        val = _tune.get_scalar(cfg, args.get)
        if val is None:
            print(f"(없음) {args.get} — config.yaml 에 해당 스칼라 키가 없습니다.")
            return 1
        print(val)
        return 0

    if args.set:
        path, value = args.set
        res = _tune.set_scalar(cfg, path, value)
        if not res["ok"]:
            print(f"실패: {res.get('error')}")
            return 1
        if res["changed"]:
            print(f"{path} = {value}  (백업: {res['backup']})")
            print("데몬 재기동 후 반영됩니다(Hermes 게이트웨이가 config 를 다시 읽음).")
        else:
            print(f"{path} = {value} (변경 없음)")
        return 0

    if args.apply is not None:
        ids = args.apply or None
        res = _tune.apply(cfg, ids)
        print(f"적용: {', '.join(res['applied']) or '(없음 — 이미 권장값이거나 키 부재)'}")
        if res["skipped"]:
            print(f"건너뜀: {', '.join(res['skipped'])}")
        print(f"백업: {res['backup']}")
        print("데몬 재기동 후 반영됩니다(Hermes 게이트웨이가 config 를 다시 읽음).")
        return 0

    rep = _tune.audit(cfg)
    if args.json:
        import json
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return 0
    print("Hermes 설정 품질 감사 (보고서 5대 원인 중 설정으로 완화 가능한 것)\n")
    print(f"  {'원인':4} {'항목':18} {'현재':>10}  {'권장':>8}  상태")
    print("  " + "-" * 70)
    for r in rep["rows"]:
        cur = "(없음)" if r["current"] is None else str(r["current"])
        rec = "자문" if r["advisory"] else str(r["recommended"])
        if r["advisory"]:
            status = "ℹ 자문(키 필요)"
        elif r["action"]:
            status = "⚠ 권장과 다름"
        else:
            status = "✓ 양호"
        print(f"  {r['cause']:4} {r['label']:18} {cur:>10}  {rec:>8}  {status}")
        print(f"       └ {r['why']}")
    if rep["aux_overrides"]:
        print("\n  #2 보조모델 수동 지정 감지(약한 모델이면 압축·요약 품질 저하 위험):")
        for o in rep["aux_overrides"]:
            print(f"     auxiliary.{o['slot']}.model = {o['model']}")
    else:
        print("\n  #2 보조모델: 전부 auto(메인 모델 사용) — 양호.")
    actionable = [r["id"] for r in rep["rows"] if r["action"]]
    print("\n  적용: alphred tune --apply" + (f"   (대상: {', '.join(actionable)})" if actionable
                                            else "   (적용할 변경 없음)"))
    print("  원복: alphred tune --revert")
    print("  임의 설정: alphred tune --get <path> · --set <path> <value>  (예: agent.max_turns)")
    return 0


def _cmd_doctor(argv: list[str]) -> int:
    """진단(§12.4 D2) — :8642/:8643/모델/큐/플래너 상태를 한 번에 점검·출력."""
    import argparse

    p = argparse.ArgumentParser(prog="alphred doctor",
                                description="Alphred/Hermes 런타임 상태 일괄 점검(관측성)")
    p.add_argument("--json", action="store_true", help="JSON 으로 출력")
    p.add_argument("--deep", action="store_true",
                   help="§35.4 Hermes 프리미티브 라이브 스모크(run 생성/완주/중단 — 소량 LLM 사용)")
    args = p.parse_args(argv)
    cfg = Config.load()
    report = _collect_doctor(cfg, deep=args.deep)
    if args.json:
        import json
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["ok"] else 1
    _print_doctor(report)
    return 0 if report["ok"] else 1


def _deep_smoke(cfg: Config) -> list[tuple[str, bool, str]]:
    """§35.4 doctor --deep — Hermes 프리미티브 라이브 스모크(옵트인, 소량 LLM).

    Phase 0 PoC(runs 생성/완주/중단)의 축약판 — 새 Hermes 버전 호환성 검증 절차.
    """
    import time as _t

    from .gateway import _upstream_key
    from .hermes_client import HermesClient, run_outcome
    out: list[tuple[str, bool, str]] = []
    key = cfg.api_key or _upstream_key(cfg)
    c = HermesClient(cfg.api_base_url, key, timeout=30.0)
    try:
        ok = c.health()
        out.append(("deep: /models 인증", ok, "OK" if ok else "미가동/키 불일치"))
        if not ok:
            return out
        rid = c.start_run("Reply with exactly: OK")
        out.append(("deep: POST /runs", bool(rid), str(rid)))
        status = "?"
        for _ in range(60):                       # 최대 ~2분 폴링
            status = (c.get_run(rid) or {}).get("status")
            if run_outcome(status) in ("done", "failed", "cancelled"):
                break
            _t.sleep(2)
        out.append(("deep: run 완주", run_outcome(status) == "done", f"status={status}"))
        rid2 = c.start_run("Count slowly from 1 to 50, one number per line.")
        _t.sleep(1)
        c.stop_run(rid2)
        _t.sleep(2)
        st2 = (c.get_run(rid2) or {}).get("status")
        out.append(("deep: /runs/{id}/stop", run_outcome(st2) != "running", f"status={st2}"))
    except Exception as e:
        out.append(("deep: 예외", False, f"{type(e).__name__}: {e}"))
    finally:
        c.close()
    return out


def _collect_doctor(cfg: Config, deep: bool = False) -> dict:
    """런타임 상태를 수집한다(기본은 라이브 LLM 호출 없음 — 쿼터 보호. --deep 만 예외)."""
    import httpx

    def _ping(url: str) -> dict:
        from urllib.parse import urlparse, urlunparse
        try:
            p = urlparse(url)
            if p.hostname == "0.0.0.0":
                netloc = f"127.0.0.1:{p.port}" if p.port is not None else "127.0.0.1"
                url = urlunparse(p._replace(netloc=netloc))
        except Exception:
            pass
        try:
            r = httpx.get(url, timeout=2.5)
            return {"up": r.status_code < 500, "status": r.status_code}
        except Exception as e:
            return {"up": False, "error": type(e).__name__}

    rep: dict = {"checks": [], "ok": True}

    def add(name: str, ok: bool, detail: str) -> None:
        rep["checks"].append({"name": name, "ok": ok, "detail": detail})
        if not ok:
            rep["ok"] = False

    # 1) hermes 바이너리
    add("hermes 실행파일", bool(cfg.hermes_bin), cfg.hermes_bin or "찾을 수 없음(ALPHRED_HERMES_BIN)")
    # 2) Hermes API (:8642)
    h = _ping(f"{cfg.api_base_url}/models")
    rep["hermes_api"] = h
    add("Hermes API (:8642)", h["up"],
        f"{cfg.api_base_url} → {'응답' if h['up'] else h.get('error') or h.get('status')}")
    # 2b) --deep: Hermes 프리미티브 라이브 스모크(§35.4, 옵트인)
    if deep:
        if h["up"]:
            for name, ok, detail in _deep_smoke(cfg):
                add(name, ok, detail)
        else:
            add("deep 스모크", False, "Hermes API 미가동 — 건너뜀")
    # 3) Alphred 게이트웨이 (:8643)
    g = _ping(f"{cfg.gateway_url}/")
    rep["gateway"] = g
    add("Alphred 게이트웨이 (:8643)", g["up"],
        f"{cfg.gateway_url} → {'응답' if g['up'] else g.get('error') or g.get('status')}")
    # 4) 모델/provider (config 읽기 — 호출 없음)
    try:
        from .config import read_model_config
        mc = read_model_config(cfg.hermes_home)
        model = mc.get("default") or "(미설정)"
        provider = mc.get("provider") or (model.split("/")[0] if "/" in model else "?")
        rep["model"] = {"default": mc.get("default"), "provider": mc.get("provider")}
        add("모델/provider", bool(mc.get("default")), f"{model}  (provider={provider})")
    except Exception as e:
        add("모델/provider", False, f"config 읽기 실패: {e}")
    # 4c) Hermes 동시 run 수 점검 (F1, §38.2 G)
    try:
        from .config import read_config_scalar
        val = read_config_scalar(cfg.hermes_home, ["gateway", "api_server", "max_concurrent_runs"])
        max_concurrent = int(val) if val is not None else 10
        try:
            slots_limit = int(cfg.slots)
        except (ValueError, TypeError):
            slots_limit = cfg.slots_max

        ok = max_concurrent >= slots_limit + 2
        add("Hermes 동시성 설정(F1)", ok,
            f"gateway.api_server.max_concurrent_runs={max_concurrent} "
            f"vs slots_limit={slots_limit} (권장: max_concurrent >= slots_limit + 2)")
    except Exception as e:
        add("Hermes 동시성 설정(F1)", True, f"점검 실패: {e}")
    # 4b) depth별 모델 라우팅(§29.1) — 설정 시에만 의미
    try:
        tiers = cfg.get_tiers()
        rep["model_tiers"] = tiers
        if cfg.has_model_tiers():
            def _lbl(t):
                v = tiers.get(t)
                return v.get("model") if isinstance(v, dict) else "(base)"
            add("depth별 모델(§29.1)", True,
                f"high={_lbl('high')} · mid={_lbl('mid')} · low={_lbl('low')} "
                f"· base={tiers.get('base') or '?'}")
        else:
            add("depth별 모델(§29.1)", True, "미설정(단일 모델) — /model high|mid|low <이름> 으로 설정")
    except Exception as e:
        add("depth별 모델(§29.1)", True, f"읽기 실패: {e}")
    # 5) 플래너/LLM 분류 플래그
    add("플래너(ALPHRED_PLANNER)", True, "ON" if cfg.planner else "OFF")
    add("LLM 분류(ALPHRED_LLM_CLASSIFY)", True, "ON" if cfg.llm_classify else "OFF")
    add("산출물 검증(ALPHRED_VERIFY)", True, "ON (Tier0 결정적)" if cfg.verify else "OFF")
    add("수용 judge(ALPHRED_JUDGE)", True,
        f"ON (Tier2 LLM, high, 재시도≤{cfg.judge_max_retries})" if cfg.judge else "OFF (쿼터 절약)")
    add("Light 하네스(ALPHRED_LIGHT_HARNESS)", True,
        "ON (즉답 품질 시스템 메시지)" if cfg.light_harness else "OFF (순정 패스스루)")
    add("MoA(ALPHRED_MOA)", True,
        f"ON (high 한정, 표본≤{cfg.moa_samples})" if cfg.moa else "OFF (단일 모델 직관)")
    add("프로파일(ALPHRED_PROFILE)", True,
        f"{cfg.profile} — basic(큐만)/smart(+의도·계획)/full(+질문·스텝·감시), "
        f"변경: alphred setup --profile <이름>")
    add("IntentCard(ALPHRED_INTENT)", True,
        "ON (LLM-first 의도 판정)" if cfg.intent else "OFF (정규식 사전필터)")
    add("인테이크 질문(ALPHRED_CLARIFY)", True,
        (f"ON (추천답변 질문, 타임아웃 {cfg.clarify_timeout:.0f}s)" if cfg.clarify and cfg.intent
         else "ON (IntentCard OFF — 무효)" if cfg.clarify
         else "OFF (질문 없이 가정 진행)"))
    add("StepRunner(ALPHRED_ORCHESTRATE)", True,
        (f"ON (high 한정 스텝 실행 · 예산 {cfg.task_budget}run · 스텝 재시도≤{cfg.step_retries})"
         if cfg.orchestrate else "OFF (단발 실행)"))
    add("watchdog(ALPHRED_WATCHDOG)", True,
        (f"ON (연속 도구실패≥{cfg.tool_fail_limit} · 무진전 {cfg.stall_seconds:.0f}s → 중단·교정)"
         if cfg.watchdog else "OFF (실행 중 무개입)"))
    # 5b) 능력 레지스트리(§34.5) — 로컬 프로브(CLI/라이브러리)는 항상, 스킬/툴셋은 :8642 필요
    if cfg.caps:
        try:
            from .capabilities import CapabilityRegistry
            from .hermes_client import HermesClient as _HC
            _c = _HC(cfg.api_base_url, cfg.api_key, timeout=5.0) if h["up"] else None
            try:
                caps = CapabilityRegistry(cfg, _c)
                s = caps.summary()
            finally:
                if _c is not None:
                    _c.close()
            rep["capabilities"] = s["counts"]
            fmts = s.get("formats") or {}
            capable = [f for f, v in fmts.items() if v.get("capable")]
            nocap = [f for f, v in fmts.items() if not v.get("capable")]
            cnt = s["counts"]
            detail = (f"스킬 {cnt['skills']} · 도구 {cnt['tools']} · CLI {cnt['cli_agents']} "
                      f"· 라이브러리 {cnt['pylibs']} · MCP {cnt['mcp_servers']}")
            if capable:
                detail += " · 생성가능 " + ",".join(sorted(capable))
            if nocap:
                detail += " · 불가 " + ",".join(sorted(nocap))
            add("능력 레지스트리(§34.5)", True, detail)
        except Exception as e:
            add("능력 레지스트리(§34.5)", True, f"수집 불가: {e}")
    else:
        add("능력 레지스트리(§34.5)", True, "OFF (ALPHRED_CAPS=0 — 정적 하네스)")
    # 6) 큐 상태 (게이트웨이 가동 시) / DB 직접
    counts: dict = {}
    if g["up"]:
        try:
            tasks = httpx.get(f"{cfg.gateway_url}/queue",
                              headers=_auth_headers(cfg), timeout=3.0).json().get("tasks", [])
            for t in tasks:
                counts[t["state"]] = counts.get(t["state"], 0) + 1
        except Exception:
            pass
    if not counts:
        try:
            from .db import Store
            store = Store(cfg.db_path)
            try:
                for t in store.list():
                    counts[t.state] = counts.get(t.state, 0) + 1
            finally:
                store.close()
        except Exception:
            pass
    rep["queue"] = counts
    add("큐", True, ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "(비어 있음)")
    # 7) 검증 통계(§21 V3) — DB 직접 집계(LLM 호출 없음)
    try:
        import json as _json

        from .db import Store
        store = Store(cfg.db_path)
        try:
            rows = store.list()
            intent_stats = store.intent_stats()
        finally:
            store.close()
        completed = sum(1 for t in rows if t.state == "Completed")
        needs = sum(1 for t in rows if t.state == "NeedsReview")
        finished = completed + needs
        depth_dist: dict = {}
        retried: list = []
        scores: list = []
        for t in rows:
            if getattr(t, "depth", None):
                depth_dist[t.depth] = depth_dist.get(t.depth, 0) + 1
            if (getattr(t, "verify_attempts", 0) or 0) > 0:
                retried.append(t.verify_attempts)
            if getattr(t, "verify_report", None):
                try:
                    j = (_json.loads(t.verify_report) or {}).get("judge")
                    if isinstance(j, dict) and isinstance(j.get("score"), (int, float)):
                        scores.append(j["score"])
                except Exception:
                    pass
        rep["verify_stats"] = {"completed": completed, "needs_review": needs,
                               "depth": depth_dist, "judge_scores": scores}
        rate = f"{completed}/{finished} ({100 * completed // finished}%)" if finished else "N/A"
        detail = f"통과율 {rate} · 검토필요 {needs}"
        if depth_dist:
            detail += " · 심화도 " + ",".join(f"{k}:{v}" for k, v in sorted(depth_dist.items()))
        if retried:
            detail += f" · 평균 재시도 {sum(retried) / len(retried):.1f}"
        if scores:
            detail += f" · judge 평균 {sum(scores) / len(scores):.0f}"
        add("검증 통계(§21)", True, detail)
        # 7b) §34.7 지표 — 의도/인테이크/오케스트레이션 품질 지표(무LLM)
        m = _collect_metrics(rows, intent_stats)
        rep["metrics"] = m
        bits = []
        if m["classified"]:
            bits.append(f"분류 {m['classified']}건"
                        + (f"(명시 오버라이드 {m['explicit_ratio']:.0%})"
                           if m["explicit_ratio"] is not None else ""))
        if m["needs_review_rate"] is not None:
            bits.append(f"NeedsReview율 {m['needs_review_rate']:.0%}")
        if m["asked"] :
            bits.append(f"질문율 {m['ask_rate']:.0%}"
                        + (f" · 추천 채택률 {m['recommend_adopt_rate']:.0%}"
                           if m["recommend_adopt_rate"] is not None else ""))
        if m["orchestrated"]:
            bits.append(f"오케스트레이션 {m['orchestrated']}건 · 평균 {m['avg_runs']:.1f}run"
                        + (f" · 스텝 1회 통과율 {m['step_first_pass_rate']:.0%}"
                           if m["step_first_pass_rate"] is not None else ""))
        add("지표(§34.7)", True, " · ".join(bits) or "(데이터 없음)")
    except Exception as e:
        add("검증 통계(§21)", True, f"집계 불가: {e}")
    # 8) 안전망(재시작 가드)
    try:
        from .safety import RestartGuard
        guard = RestartGuard(cfg.guard_path, cfg.restart_window_seconds, cfg.restart_threshold)
        add("안전망(#30719)", not guard.tripped(),
            f"재시작 {guard.count()}회/{cfg.restart_window_seconds:.0f}s (임계 {cfg.restart_threshold})")
    except Exception as e:
        add("안전망(#30719)", False, str(e))
    return rep


def _collect_metrics(rows, intent_stats: dict) -> dict:
    """§34.7 지표 — DB 집계만으로 계산(무LLM, 순수 함수).

    · classified/explicit_ratio: intent_log 총 판정 수와 명시 오버라이드 비율(암묵 정답 신호)
    · needs_review_rate: 종료(Completed+NeedsReview) 대비 NeedsReview 비율
    · ask_rate/recommend_adopt_rate: Heavy 중 질문 발생률 · 답변이 추천(✦)과 일치한 비율
    · orchestrated/avg_runs/step_first_pass_rate: 스텝 실행 작업 수·평균 run·스텝 1회 통과율
    """
    import json as _json

    total_cls = sum(sum(v.values()) for v in (intent_stats or {}).values())
    explicit = sum((intent_stats or {}).get("explicit", {}).values())
    completed = sum(1 for t in rows if t.state == "Completed")
    needs = sum(1 for t in rows if t.state == "NeedsReview")
    finished = completed + needs
    heavy = [t for t in rows if t.kind == "heavy"]
    asked = adopt_hit = adopt_total = 0
    orch_runs: list[int] = []
    first_pass = step_retried = 0
    for t in heavy:
        qs, ans, plan = [], None, None
        try:
            qs = _json.loads(t.questions) if getattr(t, "questions", None) else []
            ans = _json.loads(t.answers) if getattr(t, "answers", None) else None
            plan = _json.loads(t.plan) if getattr(t, "plan", None) else None
        except Exception:
            pass
        if qs:
            asked += 1
            if isinstance(ans, list):
                recs = [next((o.get("label") for o in (q.get("options") or [])
                              if o.get("recommended")), None) for q in qs]
                for i, a in enumerate(ans):
                    label = a.get("answer") if isinstance(a, dict) else a
                    if i < len(recs) and recs[i]:
                        adopt_total += 1
                        if str(label).strip() == str(recs[i]).strip():
                            adopt_hit += 1
        steps = (plan or {}).get("steps") or []
        if steps and any("state" in s for s in steps):
            orch_runs.append(int((plan or {}).get("runs_used") or 0))
            for s in steps:
                if s.get("state") == "done":
                    if int(s.get("attempts") or 0) > 0:
                        step_retried += 1
                    else:
                        first_pass += 1
    step_done = first_pass + step_retried
    return {
        "classified": total_cls,
        "explicit_ratio": (explicit / total_cls) if total_cls else None,
        "intent_engines": intent_stats or {},
        "needs_review_rate": (needs / finished) if finished else None,
        "asked": asked,
        "ask_rate": (asked / len(heavy)) if heavy else 0.0,
        "recommend_adopt_rate": (adopt_hit / adopt_total) if adopt_total else None,
        "orchestrated": len(orch_runs),
        "avg_runs": (sum(orch_runs) / len(orch_runs)) if orch_runs else 0.0,
        "step_first_pass_rate": (first_pass / step_done) if step_done else None,
    }


def _auth_headers(cfg: Config) -> dict:
    return {"Authorization": f"Bearer {cfg.api_key}"} if cfg.api_key else {}


def _print_doctor(rep: dict) -> None:
    print("Alphred doctor — 런타임 점검\n")
    for c in rep["checks"]:
        mark = "[OK] " if c["ok"] else "[!!] "
        print(f"  {mark}{c['name']:28} {c['detail']}")
    print()
    print("종합: " + ("정상" if rep["ok"] else "주의 — 위 [!!] 항목 확인 필요"))
    if not rep.get("hermes_api", {}).get("up"):
        print("  · Hermes API 미응답: `alphred serve`(자동 기동) 또는 Hermes 게이트웨이 확인.")
    if not rep.get("gateway", {}).get("up"):
        print("  · 게이트웨이 미응답: `alphred serve` 로 :8643 기동.")


def _cmd_tui(argv: list[str]) -> int:
    """전용 Alphred TUI(Textual) — 게이트웨이(:8643) 터미널 클라이언트(기획 §13)."""
    no_daemon = "--no-daemon" in argv
    try:
        from .tui import run_tui
    except Exception as e:
        sys.stderr.write(
            f"alphred: TUI 의존성을 불러올 수 없습니다 ({e}). `pip install textual` 후 다시 시도하세요.\n"
            "  (또는 `hermes` 로 순정 Hermes TUI 를 쓰거나 `alphred queue list` 로 큐를 확인하세요.)\n")
        return 1
    cfg = Config.load()
    proc = None
    if not no_daemon and "ALPHRED_NO_DAEMON" not in os.environ:
        proc = _ensure_daemon()  # :8643 보장. 우리가 띄웠을 때만 핸들 반환(정리 대상)
    try:
        run_tui(cfg.gateway_url, cfg.api_key, cfg.alphred_home)
    finally:
        if proc is not None:
            from .childproc import terminate_managed
            terminate_managed(proc)  # TUI 종료(정상/예외) 시 serve+hermes 트리 정리
    return 0


def _cmd_keys(argv: list[str]) -> int:
    """클라이언트(디바이스) 키 관리(§35.1) — 기기당 1개 발급 권장(회수 단위 = 기기)."""
    import argparse

    from . import clientkeys
    p = argparse.ArgumentParser(prog="alphred keys", description="디바이스 접속 키 관리")
    sub = p.add_subparsers(dest="cmd", required=True)
    pi = sub.add_parser("issue", help="새 키 발급(평문은 지금 한 번만 표시)")
    pi.add_argument("name", help="기기 이름 (예: 노트북, web, esp32-거실)")
    pi.add_argument("--scope", choices=clientkeys.SCOPES, default="control",
                    help="read=모니터링 전용(GET만) / control=전부 (기본)")
    sub.add_parser("list", help="키 목록(평문 없음)")
    pr = sub.add_parser("revoke", help="키 회수(즉시 무효)")
    pr.add_argument("name")
    a = p.parse_args(argv)
    cfg = Config.load()
    if a.cmd == "issue":
        try:
            key = clientkeys.issue(cfg.alphred_home, a.name, a.scope)
        except ValueError as e:
            print(f"오류: {e}")
            return 2
        print(f"발급됨: {a.name}  (scope={a.scope})")
        print(f"\n  {key}\n")
        print("  ↑ 이 키는 지금 한 번만 표시됩니다(서버에는 해시만 저장). 기기에 보관하세요.")
        print("  기기에서 사용: `alphred connect <서버URL> --key <키>` 또는")
        print("  HTTP 헤더 `Authorization: Bearer <키>` / 환경변수 ALPHRED_API_KEY")
        return 0
    if a.cmd == "list":
        rows = clientkeys.list_keys(cfg.alphred_home)
        if not rows:
            print("(발급된 키 없음 — 키가 하나라도 생기면 인증이 필수가 됩니다)")
            return 0
        for r in rows:
            print(f"  {r['name']:<20} scope={r['scope']:<8} 발급 {r['created_at']}"
                  f"  마지막 사용 {r['last_seen'] or '-'}")
        return 0
    ok = clientkeys.revoke(cfg.alphred_home, a.name)
    print(f"회수됨: {a.name}" if ok else f"없음: {a.name}")
    return 0 if ok else 1


def _cmd_connect(argv: list[str]) -> int:
    """씬클라이언트 TUI(§35.9 모드 b) — 원격 Alphred 서버 접속. 로컬 데몬을 절대 띄우지 않는다."""
    import argparse
    p = argparse.ArgumentParser(prog="alphred connect",
                                description="원격 Alphred 서버에 TUI 로 접속(씬클라이언트)")
    p.add_argument("url", help="서버 주소 (예: 192.168.0.10:8643, http://myhost:8643)")
    p.add_argument("--key", default=None, help="접속 키(미지정 시 ALPHRED_API_KEY 환경변수)")
    a = p.parse_args(argv)
    url = a.url.strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        url = "http://" + url
    key = a.key or os.environ.get("ALPHRED_API_KEY") or os.environ.get("API_SERVER_KEY")
    import httpx
    try:  # 접속/인증을 TUI 진입 전에 검증 — 실패는 명확한 에러(로컬 폴백 없음)
        r = httpx.get(f"{url}/queue", timeout=5.0,
                      headers={"Authorization": f"Bearer {key}"} if key else {})
    except Exception as e:
        sys.stderr.write(
            f"alphred: 서버에 연결할 수 없습니다: {url} ({type(e).__name__})\n"
            "  서버에서 `alphred serve --host 0.0.0.0` 가동·포트·방화벽을 확인하세요.\n")
        return 2
    if r.status_code == 401:
        sys.stderr.write(
            "alphred: 인증 실패(401) — 서버에서 `alphred keys issue <기기이름>` 으로 키를\n"
            "  발급받아 `--key` 또는 ALPHRED_API_KEY 로 전달하세요.\n")
        return 2
    if r.status_code >= 500:
        sys.stderr.write(f"alphred: 서버 오류(HTTP {r.status_code}) — 서버 로그를 확인하세요.\n")
        return 2
    try:
        from .tui import run_tui
    except Exception as e:
        sys.stderr.write(f"alphred: TUI 의존성을 불러올 수 없습니다 ({e}).\n")
        return 1
    cfg = Config.load()
    sys.stderr.write(f"alphred: {url} 에 접속합니다 (세션 기록은 이 기기에 보관).\n")
    run_tui(url, key, cfg.alphred_home)   # 세션=기기 로컬, 큐/실행=서버(단일 코어)
    return 0


def _cmd_service(argv: list[str]) -> int:
    """OS 서비스 등록(§35.4) — 로그온 시 `alphred serve` 자동 기동.

    Windows 는 작업 스케줄러(schtasks)로 직접 등록/해제하고, Linux/macOS 는 유닛 파일을
    생성해 설치 명령을 안내한다(권한 필요 작업은 사용자 확인 하에).
    플래그(ALPHRED_PROFILE 등)는 파일 기반 설정이므로 서비스에서도 그대로 반영된다.
    """
    import argparse
    import platform
    import subprocess
    p = argparse.ArgumentParser(prog="alphred service")
    p.add_argument("action", choices=("install", "uninstall", "status"))
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", default="8643")
    a = p.parse_args(argv)
    cfg = Config.load()
    sysname = platform.system()
    task_name = "AlphredServe"
    if sysname == "Windows":
        if a.action == "install":
            tr = f'"{sys.executable}" -m alphred.cli serve --host {a.host} --port {a.port}'
            r = subprocess.run(["schtasks", "/Create", "/TN", task_name, "/TR", tr,
                                "/SC", "ONLOGON", "/F"], capture_output=True, text=True)
            print((r.stdout or r.stderr).strip())
            if r.returncode == 0:
                print(f"등록됨 — 지금 시작: schtasks /Run /TN {task_name}")
            return r.returncode
        if a.action == "uninstall":
            r = subprocess.run(["schtasks", "/Delete", "/TN", task_name, "/F"],
                               capture_output=True, text=True)
            print((r.stdout or r.stderr).strip())
            return r.returncode
        r = subprocess.run(["schtasks", "/Query", "/TN", task_name],
                           capture_output=True, text=True)
        print((r.stdout or r.stderr).strip())
        return r.returncode
    # Linux/macOS — 유닛/plist 파일 생성 + 설치 안내(권한 작업은 수동)
    if sysname == "Linux":
        unit = (f"[Unit]\nDescription=Alphred agent server\nAfter=network.target\n\n"
                f"[Service]\nExecStart={sys.executable} -m alphred.cli serve "
                f"--host {a.host} --port {a.port}\nRestart=on-failure\n\n"
                f"[Install]\nWantedBy=default.target\n")
        path = cfg.alphred_home / "alphred.service"
        if a.action == "install":
            path.write_text(unit, encoding="utf-8")
            print(f"유닛 파일 생성: {path}")
            print("설치: mkdir -p ~/.config/systemd/user && "
                  f"cp {path} ~/.config/systemd/user/ && "
                  "systemctl --user enable --now alphred")
        else:
            print("해제: systemctl --user disable --now alphred / 상태: "
                  "systemctl --user status alphred")
        return 0
    plist_path = cfg.alphred_home / "com.alphred.serve.plist"
    if a.action == "install":
        plist = (f'<?xml version="1.0" encoding="UTF-8"?>\n'
                 f'<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
                 f'"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
                 f'<plist version="1.0"><dict>\n'
                 f'  <key>Label</key><string>com.alphred.serve</string>\n'
                 f'  <key>ProgramArguments</key><array>\n'
                 f'    <string>{sys.executable}</string><string>-m</string>'
                 f'<string>alphred.cli</string><string>serve</string>'
                 f'<string>--host</string><string>{a.host}</string>'
                 f'<string>--port</string><string>{a.port}</string>\n'
                 f'  </array>\n'
                 f'  <key>RunAtLoad</key><true/>\n'
                 f'</dict></plist>\n')
        plist_path.write_text(plist, encoding="utf-8")
        print(f"plist 생성: {plist_path}")
        print(f"설치: cp {plist_path} ~/Library/LaunchAgents/ && "
              "launchctl load ~/Library/LaunchAgents/com.alphred.serve.plist")
    else:
        print("해제: launchctl unload ~/Library/LaunchAgents/com.alphred.serve.plist")
    return 0


def _cmd_model(argv: list[str]) -> int:
    """사용 가능한 모델 목록 조회 서브커맨드."""
    cfg = Config.load()
    try:
        d = cfg.fetch_available_models()
    except Exception as e:
        sys.stderr.write(f"Error fetching models: {e}\n")
        return 1
        
    from rich.console import Console
    from rich.table import Table
    from rich import box
    
    console = Console()
    
    cur = d.get("current") or "(config 기본값)"
    badge = " 💭 추론" if d.get("current_reasoning") else ""
    console.print(f"[bold yellow]현재 모델[/]: {cur}{badge}")
    
    if cfg.has_model_tiers():
        tiers = cfg.get_tiers()
        def _lbl(t):
            v = tiers.get(t)
            if isinstance(v, dict):
                r = f" 💭{v.get('reasoning')}" if v.get("reasoning") else ""
                return f"{v.get('model') or 'base'}{r}({v.get('source', '')})"
            return "base"
        console.print(f"[bold yellow]깊이별 모델[/]: high={_lbl('high')} · mid={_lbl('mid')} · low={_lbl('low')} · base={tiers.get('base') or '?'}")
    else:
        console.print("[dim]깊이별 모델: 미설정(단일 모델).[/]")
        
    models = d.get("models") or []
    if models:
        table = Table(box=box.ROUNDED, show_header=True, title="사용 가능한 모델 목록", title_style="bold yellow")
        table.add_column("Provider", style="cyan", no_wrap=True)
        table.add_column("Model", style="green")
        table.add_column("Category", style="magenta")
        
        for m in models:
            prov = m.get("provider_label") or m.get("provider") or ""
            m_id = m.get("id") or ""
            m_display = f"{m_id} 💭" if m.get("reasoning") else m_id
            cats = ", ".join(m.get("categories") or [])
            table.add_row(prov, m_display, cats)
            
        console.print(table)
        console.print("[dim]💭=추론(사고 과정 표시 가능)[/]")
    else:
        console.print("[dim]사용 가능한 모델 목록을 가져오지 못했습니다. provider 설정이나 인증을 확인하세요.[/]")
        
    return 0


def _cmd_serve(argv: list[str]) -> int:
    import argparse
    from .gateway import serve
    p = argparse.ArgumentParser(prog="alphred serve", description="Alphred 게이트웨이 기동")
    # §35.1 안전 기본값: 로컬 전용. 외부/다기기 접속은 --host 0.0.0.0 + 접속 키 필수.
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8643)
    p.add_argument("--interval", type=float, default=1.0, help="스케줄러 tick 주기(초)")
    p.add_argument("--no-auto-hermes", action="store_true",
                   help="Hermes API 게이트웨이 자동 기동을 끄고 외부 인스턴스를 사용")
    args = p.parse_args(argv)
    serve(host=args.host, port=args.port, interval=args.interval,
          auto_hermes=not args.no_auto_hermes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
