# Alphred

**English** | [한국어](README_ko.md)

> A priority-queue middleware and state-control wrapper that sits on top of
> [Hermes Agent](https://github.com/NousResearch/hermes-agent) — **without modifying its core.**

Talk to Alphred like any AI assistant. When you ask for something quick ("translate this",
"what's the weather"), it answers **immediately**. When you ask for something heavy
("refactor the whole codebase", "crawl and summarize these 200 pages"), it queues the work,
runs it in the background by priority, and pauses lower-priority work when something urgent
arrives — then resumes it. **You don't have to label anything; Alphred decides.**

Full design doc: [`docs/Alphred-실행기획안.md`](docs/Alphred-실행기획안.md) (Korean)

---

## What is Alphred?

Hermes Agent is a capable single-turn agent runtime. Alphred wraps it to add the one thing a
real always-on assistant needs: **knowing what to do first.**

Every incoming request is classified as:

- **Light** — needs an immediate answer (chat, lookups, short Q&A, translation). Served right away.
- **Heavy** — time-consuming background work (analysis, refactor, crawl, reports). Queued and
  scheduled by priority.

Alphred runs a single-slot scheduler over a persistent priority queue. If a Light request (or a
higher-priority Heavy one) arrives while a Heavy task is running, Alphred **preempts** the running
task (pause → handle the urgent one → resume). State lives in SQLite (the source of truth) and is
mirrored to a human-readable `QUEUE.MD`.

The Hermes core is never patched — Alphred talks to it over its HTTP API and reuses its home
directory, so Hermes updates don't break anything.

---

## Why Alphred?

| Capability | What it means for you |
|---|---|
| **Automatic Light/Heavy routing** | Just talk. Alphred judges urgency per message — no flags, no manual queueing. Override only when you want to. |
| **Preemptive priority queue** | Urgent requests don't wait behind a long job. The long job is paused and resumed automatically. |
| **Task depth + verification (§21)** | Light tasks stay cheap; heavy ones get planned, verified, and self-healed. A run that only *claims* to have made a file lands in `NeedsReview`, not `Completed`. |
| **Resilience built in** | Transient failures (429 / rate limits / network) are auto-requeued with exponential backoff; orphaned tasks recover after a crash/restart. |
| **OpenAI-compatible gateway** | Point any OpenAI client at Alphred's base URL — chat completions, responses, and async runs all work. |
| **Dedicated terminal UI** | An Alph-RED Textual TUI with live tool activity, a slash-command palette, queue control, multiline input, and restorable sessions — while Hermes stays pristine. |
| **Web dashboard, zero deps** | Drag-and-drop reprioritize, pause/resume/discard, and submit — a single self-contained HTML page. |

---

## How it works

```
                 ┌─────────────────────────── Alphred ───────────────────────────┐
   request  ──►  │  classify (keyword + length + source, LLM fallback if unsure)  │
 (chat / API /   │            │                                                   │
  voice / cron)  │     ┌──────┴───────┐                                           │
                 │   Light          Heavy                                         │
                 │     │              │                                           │
                 │  preempt        enqueue ──► priority queue (SQLite + QUEUE.MD) │
                 │  running          │                    │                       │
                 │  Heavy            │            single-slot scheduler           │
                 │     │             │          (pause / resume / retry / recover)│
                 │     ▼             ▼                    ▼                       │
                 └─────┴─────────────┴──────── Hermes HTTP API ───────────────────┘
                                                         │
                                                    LLM provider
```

Task states: `Pending → In-Progress → (Paused) → Completed / Discarded`.

---

## Quick Start

**Install once** (Hermes must already be installed):

**Option A — installer script** (handles pip install and PATH for you):

```bash
# Windows (PowerShell)
powershell -ExecutionPolicy Bypass -File scripts\install.ps1
# macOS / Linux
bash scripts/install.sh
```

**Option B — manual:**

```bash
pip install -e .
alphred setup     # runs Hermes onboarding (configure your LLM provider). Hermes stays stock.
```

> **`alphred: command not found`?** pip installed `alphred` into a Scripts dir that isn't on your
> PATH (it prints a warning saying where). Either add that dir to PATH, or just use
> `python -m alphred.cli ...` instead of `alphred ...` everywhere below — both are equivalent.

Then pick how you want to use it — both drive the **same** Alphred engine (one queue, one
state), so a task you start in the terminal is visible from the web and vice versa.

**Way 1 — Chat in the terminal (dedicated Alphred TUI):**

```bash
alphred            # dedicated Alphred TUI: chat + live queue table + completion alerts
alphred chat       # (alternatively) the full Hermes TUI directly, with its live tool UI
```

`alphred` launches a purpose-built terminal client (Textual) that talks to the gateway like any
other client. Quick questions are answered inline; heavy requests are offloaded to the background
queue (shown in a live table), and you get a notification when a queued task finishes — even mid-chat.
Bare `alphred` auto-starts the background daemon for you. (Needs `textual`; installed by default.)

**Way 2 — Run the server (web dashboard + OpenAI-compatible API for apps/devices):**

```bash
alphred serve --port 8643      # gateway + scheduler; auto-launches the Hermes API (:8642)
```

Then open **http://localhost:8643/** for the web dashboard, or point any OpenAI-compatible
client (web app, Android, ESP32, …) at `http://localhost:8643/v1`.
(Bring your own Hermes API with `alphred serve --no-auto-hermes`.)

---

## Usage

### A. Just talk — Alphred classifies automatically

This is the main path. Send normal requests; Alphred decides Light vs Heavy per message.

The classifier runs in the **dedicated TUI** (`alphred`) and the **HTTP gateway** (`:8643`). Just
talk — quick questions get answered inline; heavy requests ("refactor the whole codebase") are
offloaded to the background queue automatically.

> Note: `alphred chat` is *pure Hermes* (no Alphred queue). Use `alphred` (TUI) or the HTTP API
> for queue-aware chat.

Over the OpenAI-compatible HTTP API (any client, just change the base URL):

```bash
curl http://localhost:8643/v1/chat/completions \
  -H "Authorization: Bearer $API_SERVER_KEY" \
  -d '{"messages":[{"role":"user","content":"Summarize this 300-page report"}]}'
# Heavy → 202 {"id": "...", "status": "queued"}   (poll GET /v1/runs/{id})
# Light → normal OpenAI response, immediately
```

**How the decision is made:**

| Signal | Routes to |
|---|---|
| Heavy keywords (`refactor`, `crawl`, `analyze`, `report`, `migrate`, `batch`, `build`, `train`, …) | **Heavy** |
| Light keywords (`hi`, `what`, `translate`, `calculate`, …), or text ≤ 60 chars, or a chat-source message | **Light** |
| Ambiguous | Conservative **Heavy** (or an LLM judges it, if the LLM fallback is enabled) |

### B. Override the decision (when you want manual control)

The automatic decision is the default — these are only for forcing a choice.

```bash
# CLI: submit directly with explicit priority/kind (flags are optional; omit them to auto-classify)
alphred queue submit "Refactor the entire codebase" --priority 3   # force Heavy, priority 3
alphred queue submit "Urgent question" --priority 10               # force high priority

# HTTP: override headers
curl http://localhost:8643/v1/chat/completions \
  -H "X-Alphred-Kind: heavy" -H "X-Alphred-Priority: 2" ...
```

### C. Manage the queue

```bash
alphred queue list                # list tasks (priority order)
alphred queue show <id>           # details + state-transition history
alphred queue prio <id> 8         # change priority
alphred queue pause <id>          # pause / resume / discard
alphred queue resume <id>
alphred queue discard <id>
alphred queue run                 # run the scheduler loop directly (without the gateway)
```

Or just ask in plain language — Alphred reads the queue and acts on your request:

```bash
alphred queue ask "What's in the queue right now?"
alphred queue ask "Bump the report task to top priority"
alphred queue ask "Cancel the crawl job"
```

The web dashboard at `http://localhost:8643/` does all of the above with drag-and-drop.

### D. Hermes passthrough (`alphred` is a 1:1 superset of `hermes`)

Every command except `queue` / `serve` / `setup` / `tui` / `doctor` is delegated verbatim to Hermes
(exit codes and streaming preserved). New Hermes subcommands appear automatically.

```bash
alphred version          # == hermes version
alphred gateway run      # == hermes gateway run
alphred chat             # == hermes  (pure, stock Hermes TUI — no Alphred traces)
```

**Pure Hermes is guaranteed.** Alphred's identity lives entirely in its own TUI (`alphred`).
It does **not** re-skin or modify Hermes: running `hermes` (or `alphred chat`) gives you the
original Hermes logo/banner/colors/identity with zero Alphred footprint.

---

## Gateway API

| Endpoint | Behavior |
|---|---|
| `POST /v1/chat/completions` | **Light** (immediate, preempts running Heavy). Stateless — send the full `messages` array each call. |
| `POST /v1/responses` | **Light** (preserves multimodal input & `previous_response_id` for continuity) |
| `POST /v1/runs` | **Heavy** (async; returns `run_id`). Accepts `session_id` and `conversation_history` for session continuity; runs the verification loop. |
| `GET /v1/runs/{id}` | task status + verification: `state`, `depth`, `needs_review`, `verify_attempts`, `verify_report`, `session_id` |
| `POST /plan` | **dry-run** — returns `kind`/`depth`/`plan`/`estimate` without executing or queueing |
| `GET /v1/models`, `GET /models/available` | model list (proxied / real selectable per provider) |
| `GET /v1/skills` | installed Hermes skills (the agent uses them on demand) |
| `GET/POST/DELETE /queue/...` | queue management (list / prio / pause / resume / discard) |
| `POST /queue/ask` | natural-language queue management (`{"q": "..."}`) |
| `GET /`, `GET /dashboard` | web dashboard |
| `GET /safety`, `POST /safety/reset` | restart-storm guard status / reset |

Headers: `X-Alphred-Priority` (1..10), `X-Alphred-Kind` (`light`|`heavy`), `X-Alphred-Source`, `X-Alphred-Depth`.
Auth: if `ALPHRED_API_KEY` / `API_SERVER_KEY` is set, a `Bearer` token is required.

### Sessions

| Path | How session/context is kept |
|---|---|
| `POST /v1/chat/completions` | **Stateless** (OpenAI semantics) — you resend the whole `messages` array; no server-side session. |
| `POST /v1/responses` | Chain with `previous_response_id` (OpenAI Responses API). |
| `POST /v1/runs` | Pass a stable `session_id` so successive Heavy runs share one Hermes session (server-side context). Omit it and each run is isolated (`session_id` = its `run_id`). `conversation_history` does an explicit one-shot handoff. |
| Dedicated TUI | Auto-managed: each conversation is a session persisted under `ALPHRED_HOME/tui_sessions/`, restored on launch, switchable via `/sessions`. The active session (and model) is shown in the output panel's title bar. |

```bash
# Multi-turn Heavy session over the API: reuse the same session_id
curl -s localhost:8643/v1/runs -H "Authorization: Bearer $KEY" \
  -d '{"input":"Research this quarter's GPU market","session_id":"gpu-research"}'
curl -s localhost:8643/v1/runs -H "Authorization: Bearer $KEY" \
  -d '{"input":"Now turn it into a one-page brief","session_id":"gpu-research"}'
```

### Verification & task depth (§21)

Every **queued Heavy run** (`POST /v1/runs`) goes through a verification loop before it is marked done — Light synchronous calls (`/v1/chat/completions`, `/v1/responses`) are *not* verified.

- **Depth** (`low`/`mid`/`high`) is derived per task and gates how much work/verification happens — so light tasks don't burn tokens.
- **Tier 0 (deterministic, free, default on):** if the result claims it saved a file, Alphred checks the file really exists, is non-empty, and matches its format signature (e.g., `%PDF`). A bad/missing artifact → task ends in **`NeedsReview`** instead of `Completed`.
- **Tier 2 (LLM judge, opt-in, `high` only):** a second model checks the result against acceptance criteria it infers from the request → `{passed, score, unmet[]}`.
- **Tier 3 (self-healing retry):** on failure, `high` tasks are re-queued with an actionable hint (e.g., "produce a real PDF, install a library if needed"), up to a budget; then `NeedsReview`.

```bash
# 1) Preview what Alphred will do (no execution):
curl -s localhost:8643/plan -H "Authorization: Bearer $KEY" \
  -d '{"message":"Build a PDF report on US equities"}'
# → {"depth":"high","estimate":{"est_llm_calls":7,...},"plan":{...}}

# 2) Submit, then poll — the status carries verification results:
RID=$(curl -s localhost:8643/v1/runs -H "Authorization: Bearer $KEY" \
        -d '{"input":"Build a PDF report on US equities"}' | jq -r .run_id)
curl -s localhost:8643/v1/runs/$RID -H "Authorization: Bearer $KEY"
# → {"status":"completed","needs_review":false,"depth":"high",
#    "verify_report":{"passed":true,"checks":[...],"judge":{...}}, ...}
#   needs_review=true means the artifact/acceptance check failed — inspect verify_report.
```

---

## Diagnostics

```bash
alphred doctor          # hermes binary, :8642/:8643, model/provider, planner, verify/judge, queue + verification stats, safety
alphred doctor --json   # machine-readable
```

Runs **no live LLM calls** (quota-safe). Flags any unreachable component with a fix hint.

---

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `ALPHRED_HERMES_API` | `http://localhost:8642/v1` | Hermes API base URL |
| `API_SERVER_KEY` / `ALPHRED_API_KEY` | — | gateway auth token |
| `ALPHRED_HOME` | Hermes home | Alphred state directory |
| `ALPHRED_HERMES_BIN` | resolved | Hermes executable for passthrough |
| `ALPHRED_MAX_RETRIES` | `3` | max retries for transient failures |
| `ALPHRED_RETRY_BASE_SECONDS` | `5` | exponential-backoff base |
| `ALPHRED_LLM_CLASSIFY` | off | enable simple LLM classification fallback for ambiguous input |
| `ALPHRED_PLANNER` | off | plan-aware classification: LLM decomposes ambiguous requests into sub-tasks, judges Heavy/Light from the plan, and reuses the plan at execution |
| `ALPHRED_VERIFY` | **on** | §21 Tier 0 — deterministic artifact verification of completed Heavy runs (free, no LLM). Set `0` to disable. |
| `ALPHRED_JUDGE` | off | §21 Tier 2 — LLM acceptance judge for `high`-depth runs (uses quota) + self-healing retry |
| `ALPHRED_JUDGE_RETRIES` | `2` | §21 Tier 3 — max verification retries for `high` tasks before `NeedsReview` |

### Classification (how Heavy/Light is decided)

1. **Cheap prefilter (no LLM)** — status queries → Light; explicit large-scope ("entire codebase", "migrate", "crawl", or ≥2 heavy keywords) → Heavy; greetings/short/realtime chat → Light.
2. **Ambiguous middle** — if `ALPHRED_PLANNER` is on, an LLM decomposes the request into coarse sub-tasks; a deterministic rule (≥3 steps, or any heavy/compute/edit step, or ≥2 tool steps ⇒ Heavy) sets the weight. Falls back to conservative Heavy if the planner is off/unavailable.
3. **Reuse** — the sub-task plan is stored on the task, injected as a hint at execution, and shown in the TUI detail view.

---

## Status

- **Phase 0 (PoC)**: ✅ Hermes primitives (async run / stop / resume) verified (`poc/`)
- **Phase 1 (queue + state machine + CLI passthrough)**: ✅ end-to-end verified with real Gemini
- **Phase 2 (preemptive scheduling)**: ✅ Light-during-Heavy → pause → resume, verified live
- **Phase 3 (Alphred Gateway)**: ✅ OpenAI-compatible HTTP gateway + scheduler daemon
- **Phase 4 (operational resilience + dashboard)**: ✅ transient requeue, crash recovery, web dashboard
- **#30719 safety net**: ✅ lifecycle-command blocking + restart-storm auto-halt (`/safety`)
- **Cron intercept**: ✅ periodic jobs folded into the queue (`queue cron-tick`)
- **Classifier LLM fallback / multimodal / MCP source tagging**: ✅
- **Dedicated Alphred TUI**: ✅ Textual terminal client (chat + live queue table + completion alerts)
- **Live tool-activity (SSE)**: ✅ TUI streams `tool.started/completed` + the answer via gateway `/chat/stream`
- **Slash commands**: ✅ `/` opens a filtered command palette (`/help`, `/model`, `/clear`, `/queue …`, `/skills`, `/quit`)
- **Live token streaming + queue UX**: ✅ streaming answer, queue table with priority + ⚠needs-attention
- **Queue keyboard control**: ✅ Tab to the queue panel → ↑/↓ select, Enter detail, `c` cancel, `p/r` pause/resume, `+/-` priority
- **Plan-aware classification**: ✅ LLM decomposes ambiguous requests into sub-tasks → deterministic Heavy/Light + plan reused at execution (`ALPHRED_PLANNER`)
- **Live step progress**: ✅ background runs tracked via run events → queue table shows `진행 k⚙`, detail view shows current tool + sub-task checklist
- **Hermes stays stock**: ✅ Alphred's identity lives in its own TUI; `hermes` has zero Alphred traces
- Next: real voice/image devices, update daemonization

---

## Development

```bash
pip install -e ".[dev]"
pytest -q
```

## Architecture

```
alphred/
  config.py         configuration (reuses Hermes home/bin resolution)
  models.py         Task / TaskState
  state_machine.py  enforces allowed transitions
  db.py             SQLite store (source of truth, atomic transitions + audit log)
  classifier.py     Light/Heavy classification
  nlq.py            natural-language queue management (queue ask / /queue/ask)
  hermes_client.py  Hermes :8642 API client
  queue_manager.py  priority queue + single-slot scheduler + preemption + Light fast path
  queue_md.py       QUEUE.MD projection
  gateway.py        FastAPI gateway + background scheduler + crash recovery
  dashboard.py      web dashboard (single HTML, no dependencies)
  safety.py         #30719 safety net (payload filter + restart-storm guard)
  cron_intercept.py periodic jobs → queue (self-contained cron matcher)
  tui.py            dedicated Alphred TUI (Textual gateway client)
  tui_sessions.py   TUI conversation persistence (restorable sessions)
  splash.py         Alph-RED ASCII banner for the TUI start screen
  cli.py            CLI passthrough + queue / serve / setup / tui / doctor commands
poc/                Phase 0 primitive verification
tests/              core-logic tests
```
