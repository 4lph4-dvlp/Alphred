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
| **Context-aware priority (§22)** | A new Heavy task is ranked by an LLM against the *whole queue* — urgency **and dependencies** ("do B before A") reshuffle priorities automatically, even preempting the running task. |
| **Task depth + verification (§21)** | Light tasks stay cheap; heavy ones get planned, verified, and self-healed. A run that only *claims* to have made a file lands in `NeedsReview`, not `Completed`. Override depth with `/depth` / `X-Alphred-Depth` / `--depth`. |
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

Task states: `(AwaitingInput →) Pending → In-Progress → (Paused) → Completed / NeedsReview / Discarded`.
`AwaitingInput` (§34.4, opt-in) holds a task before start while Alphred asks you clarifying
questions with recommended answers; unanswered, it proceeds on recorded assumptions.

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
hermes             # (alternatively) the full stock Hermes TUI directly (no Alphred queue)
```

`alphred` launches a purpose-built terminal client (Textual) that talks to the gateway like any
other client. Quick questions are answered inline; heavy requests are offloaded to the background
queue (shown in a live table), and you get a notification when a queued task finishes — even mid-chat.
Bare `alphred` auto-starts the background daemon for you. (Needs `textual`; installed by default.)

**Way 2 — Run the server (web dashboard + OpenAI-compatible API for apps/devices):**

```bash
alphred serve --port 8643      # gateway + scheduler; auto-launches the Hermes API (:8642)
```

Then open **http://localhost:8643/** for the web dashboard, **http://localhost:8643/chat**
for the web chat, or point any OpenAI-compatible client at `http://localhost:8643/v1`.
(Bring your own Hermes API with `alphred serve --no-auto-hermes`.)

> `serve` binds **127.0.0.1 (local only) by default**. For other devices, issue a key first
> and bind explicitly — see *Multi-device access* below.

---

## Multi-device access — one user, many devices (§35)

One Alphred server, one queue, one state — reachable from every device you own.
All clients share the same engine, so a task started from your phone shows up in the TUI.

| Device / mode | How |
|---|---|
| TUI on the server machine | `alphred` (auto-starts the daemon) |
| **TUI from another machine** | `alphred connect http://<server>:8643 --key <key>` — thin client, never starts a local daemon; sessions stay on the device, the queue lives on the server |
| **Web chat** | `http://<server>:8643/chat` — streaming answers, tool activity, and intake **question cards with recommended answers**; single self-contained page |
| Web dashboard (queue ops) | `http://<server>:8643/` |
| External service / any OpenAI client | base URL `http://<server>:8643/v1` + Bearer key. Heavy ⇒ `202` (poll `GET /v1/runs/{id}` or pass `"delivery":{"webhook":…}` to get the result pushed) |
| **ESP32 / Arduino** | [`examples/esp32/`](examples/esp32/) — minimal sketch (POST → 200 answer / 202 poll). Embedded devices are never asked intake questions (api-source requests proceed on recorded assumptions by design) |

**Setup on the server (once per device):**

```bash
alphred keys issue laptop            # prints the key ONCE (server stores only a hash)
alphred keys issue monitor --scope read   # read = monitoring only (GET); control = everything
alphred keys list / revoke <name>    # revoke = that device is out, instantly
alphred serve --host 0.0.0.0         # explicit external binding — REFUSES to start without a key
alphred service install              # optional: auto-start on logon (schtasks / systemd / launchd)
```

For access beyond a trusted LAN, put a TLS reverse proxy (Caddy/nginx) in front of :8643.

---

## Usage

### A. Just talk — Alphred classifies automatically

This is the main path. Send normal requests; Alphred decides Light vs Heavy per message.

The classifier runs in the **dedicated TUI** (`alphred`) and the **HTTP gateway** (`:8643`). Just
talk — quick questions get answered inline; heavy requests ("refactor the whole codebase") are
offloaded to the background queue automatically.

> Note: for *pure Hermes* (no Alphred queue) just run `hermes` directly. Use `alphred` (TUI) or the
> HTTP API for queue-aware chat.

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
alphred queue discard <id>        # soft-delete (kept as Discarded in history)
alphred queue purge <id>          # permanently delete (irreversible; removes from DB)
alphred queue clear               # permanently delete finished tasks (Completed/NeedsReview/Discarded)
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

Every command except `queue` / `serve` / `setup` / `tui` / `doctor` / `prompt` / `tune` is delegated
verbatim to Hermes (exit codes and streaming preserved). New Hermes subcommands appear automatically.

```bash
alphred version          # == hermes version
alphred gateway run      # == hermes gateway run
hermes                   # for the pure, stock Hermes TUI, run hermes directly
```

**Pure Hermes is guaranteed.** Alphred's identity lives entirely in its own TUI (`alphred`).
It does **not** re-skin or modify Hermes: running `hermes` gives you the original Hermes
logo/banner/colors/identity with zero Alphred footprint.

---

## Gateway API

`alphred serve` (auto-started by the TUI) exposes an HTTP gateway on **`http://localhost:8643`**.

### OpenAI compatibility — yes

Point any OpenAI SDK or tool at the base URL **`http://localhost:8643/v1`** and it just works:

- `POST /v1/chat/completions`, `POST /v1/responses`, and `GET /v1/models` accept **standard OpenAI request bodies**. For **Light** (quick/interactive) requests Alphred proxies the call to Hermes and returns the **verbatim OpenAI response object** — a true drop-in base URL.
- **The one intentional deviation:** a request Alphred classifies as **Heavy** (long/background work) is intercepted and **enqueued** instead of answered inline. It returns HTTP `202` with `{"id" or "run_id", "status":"queued", ...}`. Fetch the result later via `GET /v1/runs/{id}`.
- **Want pure synchronous OpenAI semantics?** Send `X-Alphred-Kind: light` (forces inline answer, never queues). **Want everything queued?** Send `X-Alphred-Kind: heavy`. With no header, Alphred auto-routes per message.

> In short: short calls behave exactly like OpenAI; long jobs hand you a task id to poll. Existing chat UIs that only do quick turns work unchanged.

### Auth

If `ALPHRED_API_KEY` or `API_SERVER_KEY` is set, every API call needs `Authorization: Bearer $KEY`. With no key set (dev mode) auth is skipped. The dashboard page (`/`) is unauthenticated; its in-page JS still sends the key for API calls.

```bash
export KEY=your-token        # used in the examples below; omit the header if no key is set
```

### Endpoints

| Method & path | Purpose |
|---|---|
| `POST /v1/chat/completions` | OpenAI Chat Completions. Light → sync OpenAI response; Heavy → `202` queued. Stateless (resend full `messages`). |
| `POST /v1/responses` | OpenAI Responses API. Light → sync (preserves multimodal `input` + `previous_response_id`); Heavy → `202` queued. |
| `POST /v1/runs` | **Always async** submit → `202 {run_id}`. Accepts `input`, `session_id`, `conversation_history`. Runs the verification loop. |
| `GET /v1/runs/{id}` | Run status + verification: `status`, `state`, `depth`, `needs_review`, `verify_attempts`, `verify_report`, `output`, `session_id`. |
| `POST /plan` | **Dry-run** — returns `kind`/`priority`/`depth`/`plan`/`estimate` without executing or queueing. |
| `GET /v1/models` | Model list (proxied from Hermes, OpenAI shape). |
| `GET /models/available` | Curated selectable models for the current provider + `current`, plus `reasoning` (models that emit thinking tokens — 💭 badge in the TUI) and `current_reasoning`. |
| `GET /models/tiers` | §29.1 depth→model mapping (`high`/`mid`/`low` + `base`). |
| `POST /models/tiers` | Set/clear a depth's model — body `{"tier":"high\|mid\|low","model":"<name>\|null","provider"?,"base_url"?}`. |
| `POST /models/default` | **Permanently** set the default model — body `{"model":"<name>"}`. Writes `config.yaml` default + clears depth tiers so it sticks (survives restart; routing won't override). Returns `known` (name in provider catalog?). |
| `GET /v1/skills` | Installed Hermes skills (the agent uses them on demand). |
| `GET /queue` | List all tasks (`{"tasks":[...]}`). |
| `GET /queue/{id}` | One task + its state-transition `events`. |
| `POST /queue/{id}/prio` | Set priority — body `{"priority": 1..10}`. |
| `POST /queue/{id}/pause` / `/resume` | Pause an In-Progress task (user hold) / allow resume. |
| `POST /queue/{id}/retry` | Re-queue a `NeedsReview` task (Pending). |
| `POST /queue/{id}/answers` | §34.4 submit intake answers — body `{"answers":[...]}` (strings in question order, or `[{"q","answer"}]`). Promotes `AwaitingInput → Pending`; answers are injected into the run input. |
| `DELETE /queue/{id}` | **Discard** (soft — keeps it as `Discarded` history). |
| `DELETE /queue/{id}/purge` | **Permanently delete** one task (irreversible). |
| `POST /queue/clear` | Permanently delete finished tasks → `{"cleared": n}`. |
| `DELETE /queue/by-session/{session_key}` | Purge every task created by a session (cascade on session delete). |
| `POST /queue/ask` | Natural-language queue control — body `{"q":"..."}`. |
| `GET /queue/{id}/stream` | §33 SSE live stream of a running task (tool activity, intermediate text, final result). Fanned out from Hermes run events; static state+`done` if not running. |
| `GET /`, `GET /dashboard` | Web dashboard (single self-contained page). |
| `GET /chat` | §35.9 web chat — streaming conversation UI with intake question cards (single self-contained page). |
| `GET /safety`, `POST /safety/reset` | Restart-storm guard status / reset the halt. |
| `GET /capabilities`, `POST /capabilities/refresh` | §34.5 live capability snapshot (skills / tools / MCP / coding CLIs / Python libs + per-format producibility) / force re-collect. |

### Request options reference

**Override headers** (all optional; work on `/v1/chat/completions`, `/v1/responses`, `/v1/runs`):

| Header | Values | Effect |
|---|---|---|
| `X-Alphred-Kind` | `light` \| `heavy` | Force sync answer vs. force background queue (skips auto-classification). |
| `X-Alphred-Priority` | `1`..`10` | Set priority (10 = most urgent). **Note:** priority *alone* also picks kind — `≥7 ⇒ Light` (sync), `<7 ⇒ Heavy` (queued). Pair it with `X-Alphred-Kind` to control both independently. |
| `X-Alphred-Depth` | `low` \| `mid` \| `high` | Force task depth (gates verification/retry intensity) instead of auto. |
| `X-Alphred-Source` | `chat`\|`api`\|`cron`\|`subservice`\|`tui` | Tag the origin (audit/routing for sub-services like MCP). |

**Body fields:**

| Field | Endpoints | Meaning |
|---|---|---|
| `messages` / `input` | chat/completions, responses, runs | Standard OpenAI payload (string, message array, or multimodal content parts). |
| `model` | chat/completions, responses | Standard OpenAI model field (passed through to Hermes). |
| `previous_response_id` | responses | OpenAI Responses chaining for multi-turn context. |
| `session_id` | runs | Stable id so successive Heavy runs share one Hermes (server-side) session. Omit → isolated (`session_id` = `run_id`). |
| `conversation_history` | runs | Explicit one-shot context handoff (list of messages). |
| `priority` | queue/{id}/prio | New priority `1..10`. |
| `q` | queue/ask | Natural-language instruction. |

### Examples

#### 1) Quick chat — synchronous, standard OpenAI

```bash
# Short/interactive → answered inline, returns a normal OpenAI chat.completion object
curl -s localhost:8643/v1/chat/completions -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"Summarize this in one line: ..."}]}'
```

```python
# Drop-in with the OpenAI SDK
from openai import OpenAI
client = OpenAI(base_url="http://localhost:8643/v1", api_key="your-token")
print(client.chat.completions.create(
    model="hermes-agent",
    messages=[{"role": "user", "content": "What's 2+2?"}],
).choices[0].message.content)
```

#### 2) Force routing — kind / priority / depth

```bash
# Force SYNC even for a long prompt (pure OpenAI behavior, never queues)
curl -s localhost:8643/v1/chat/completions -H "Authorization: Bearer $KEY" \
  -H "X-Alphred-Kind: light" \
  -d '{"messages":[{"role":"user","content":"Write a quick haiku about queues"}]}'

# Force HEAVY (background queue) with explicit priority + depth
curl -s localhost:8643/v1/chat/completions -H "Authorization: Bearer $KEY" \
  -H "X-Alphred-Kind: heavy" -H "X-Alphred-Priority: 9" -H "X-Alphred-Depth: high" \
  -d '{"messages":[{"role":"user","content":"Refactor the whole codebase"}]}'
# → 202 {"id":"<task>","status":"queued","object":"alphred.task"}
```

#### 3) Multimodal (image / audio) — OpenAI content parts

```bash
curl -s localhost:8643/v1/chat/completions -H "Authorization: Bearer $KEY" \
  -d '{"messages":[{"role":"user","content":[
        {"type":"text","text":"What is in this image?"},
        {"type":"image_url","image_url":{"url":"https://example.com/cat.png"}}]}]}'
# Text + image stays Light (quick); add X-Alphred-Kind: heavy to queue a deep analysis.
```

#### 4) Responses API — multi-turn via previous_response_id

```bash
R1=$(curl -s localhost:8643/v1/responses -H "Authorization: Bearer $KEY" \
       -d '{"input":"Give me 3 startup ideas"}')
RID=$(echo "$R1" | jq -r '.id')
curl -s localhost:8643/v1/responses -H "Authorization: Bearer $KEY" \
  -d "{\"input\":\"Expand idea #2\",\"previous_response_id\":\"$RID\"}"
```

#### 5) Background runs — always async

```bash
# Minimal
curl -s localhost:8643/v1/runs -H "Authorization: Bearer $KEY" \
  -d '{"input":"Build a PDF report on US equities"}'
# → 202 {"run_id":"...","status":"queued","kind":"heavy","priority":4,"depth":"high","session_id":"..."}

# Full combo: priority + depth + source via headers
curl -s localhost:8643/v1/runs -H "Authorization: Bearer $KEY" \
  -H "X-Alphred-Priority: 8" -H "X-Alphred-Depth: mid" -H "X-Alphred-Source: subservice" \
  -d '{"input":"Crawl and summarize these 200 pages"}'
```

#### 6) Sessions & context (multi-turn Heavy)

```bash
# Reuse a stable session_id → successive Heavy runs share one server-side Hermes session
curl -s localhost:8643/v1/runs -H "Authorization: Bearer $KEY" \
  -d '{"input":"Research this quarter'\''s GPU market","session_id":"gpu-research"}'
curl -s localhost:8643/v1/runs -H "Authorization: Bearer $KEY" \
  -d '{"input":"Now turn it into a one-page brief","session_id":"gpu-research"}'

# Explicit one-shot context handoff (no shared session) via conversation_history
curl -s localhost:8643/v1/runs -H "Authorization: Bearer $KEY" \
  -d '{"input":"Continue from where we left off",
       "conversation_history":[{"role":"user","content":"We were drafting a launch plan"},
                               {"role":"assistant","content":"Step 1 was market sizing"}]}'
```

| Path | How context is kept |
|---|---|
| `/v1/chat/completions` | Stateless — resend the whole `messages` array each call. |
| `/v1/responses` | Chain with `previous_response_id`. |
| `/v1/runs` | `session_id` shares one Hermes session; omit → isolated; `conversation_history` = explicit handoff. |
| Dedicated TUI | Auto-persisted under `ALPHRED_HOME/tui_sessions/`, restored on launch, `/sessions` to switch, `/sessions delete <n|id>` to delete (also purges that session's queued tasks). |

#### 7) Dry-run planning (no execution)

```bash
curl -s localhost:8643/plan -H "Authorization: Bearer $KEY" \
  -d '{"message":"Build a PDF report on US equities"}'
# → {"kind":"heavy","priority":4,"depth":"high","classify_reason":"...",
#    "plan":{"subtasks":[...]},"estimate":{"steps":3,"est_llm_calls":7,"band":"high"}}
```

#### 8) Poll a run & read verification

```bash
RID=$(curl -s localhost:8643/v1/runs -H "Authorization: Bearer $KEY" \
        -d '{"input":"Build a PDF report on US equities"}' | jq -r .run_id)
curl -s localhost:8643/v1/runs/$RID -H "Authorization: Bearer $KEY"
# → {"status":"completed","needs_review":false,"depth":"high",
#    "output":"...","verify_report":{"passed":true,"checks":[...],"judge":{...}}, ...}
# needs_review=true ⇒ artifact/acceptance check failed — inspect verify_report.
```

#### 9) Queue management

```bash
curl -s localhost:8643/queue -H "Authorization: Bearer $KEY"                 # list
curl -s localhost:8643/queue/$RID -H "Authorization: Bearer $KEY"            # one + events
curl -s localhost:8643/queue/$RID/prio -H "Authorization: Bearer $KEY" -d '{"priority":9}'
curl -s localhost:8643/queue/$RID/pause  -H "Authorization: Bearer $KEY" -X POST
curl -s localhost:8643/queue/$RID/resume -H "Authorization: Bearer $KEY" -X POST
curl -s localhost:8643/queue/$RID/retry  -H "Authorization: Bearer $KEY" -X POST   # NeedsReview → Pending
curl -s localhost:8643/queue/$RID -H "Authorization: Bearer $KEY" -X DELETE         # discard (soft)
curl -s localhost:8643/queue/$RID/purge -H "Authorization: Bearer $KEY" -X DELETE   # permanent
curl -s localhost:8643/queue/clear -H "Authorization: Bearer $KEY" -X POST          # clear finished
curl -s localhost:8643/queue/by-session/gpu-research -H "Authorization: Bearer $KEY" -X DELETE
```

#### 10) Natural-language queue control

```bash
curl -s localhost:8643/queue/ask -H "Authorization: Bearer $KEY" \
  -d '{"q":"Bump the report task to top priority and cancel the crawl job"}'
```

#### 11) Models, skills, safety

```bash
curl -s localhost:8643/v1/models -H "Authorization: Bearer $KEY"
curl -s localhost:8643/models/available -H "Authorization: Bearer $KEY"   # {current, provider, models[]}
curl -s localhost:8643/v1/skills -H "Authorization: Bearer $KEY"
curl -s localhost:8643/safety -H "Authorization: Bearer $KEY"             # halted? restart count
curl -s localhost:8643/safety/reset -H "Authorization: Bearer $KEY" -X POST
```

### Verification & task depth (§21)

Every **queued Heavy run** (`POST /v1/runs`) goes through a verification loop before it is marked done — Light synchronous calls (`/v1/chat/completions`, `/v1/responses`) are *not* verified.

- **Depth** (`low`/`mid`/`high`) is derived per task and gates how much work/verification happens — so light tasks don't burn tokens. Override the auto-classification with the TUI `/depth low|mid|high` (`/depth auto` to clear), the `X-Alphred-Depth` header, or `alphred queue submit --depth high`.
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

### Output quality — the execution harness (§26)

Every background **Heavy** run is prefixed with a **system prompt / harness** that pushes the model toward deep, evidence-based, *fully completed* results (inspired by how leading chatbots are steered) — not shallow summaries. It also enforces Alphred's hard rules: don't ask the user back, actually create artifacts with tools, verify they exist, and report real paths.

The harness lives in an **editable external file** so you can tune the quality bar for your own domains:

```bash
alphred prompt              # summary: which source is active + path
alphred prompt --show       # print the full active harness
alphred prompt --path       # show the editable file path / source
alphred prompt --init       # copy the default to ALPHRED_HOME/system_prompt.md to edit
#   (edit that file → applies to all future Heavy runs; survives updates. Restart the daemon to load.)
```

- Resolution order: `ALPHRED_HOME/system_prompt.md` (your edit) → packaged default (`alphred/assets/system_prompt.md`).
- The default is written in **English** (token-efficient, steers most models better) while instructing outputs to follow the user's language.
- It tells the agent to **prefer matching skills** and includes **per-format design guides** (Markdown/PDF/DOCX/PPTX/XLSX) plus **cross-platform command guidance** (Windows PowerShell / Linux bash / macOS zsh). The skill/tool list itself is **no longer hardcoded**: the `{{CAPABILITIES}}` marker in the harness is replaced at dispatch with a live inventory (§34.5) — installed skills, coding CLIs actually on PATH, Python libs actually importable, and which file formats are genuinely producible right now (so the model stops "saving" a fake PDF when no PDF library exists). If your edited copy has no marker, nothing is substituted (backward compatible).
- The task's **depth** appends an extra rigor directive (`high` ⇒ research, multi-angle analysis, include methodology, strict self-verify).
- Keep the trailing `## REQUEST` delimiter — the actual request is appended after it.

### Closing the quality gap with commercial agents (§29)

Quality ≈ *50% model + 50% harness*. The §26 harness covers the harness half for Heavy work; §29 adds the rest — all **without modifying the Hermes core** (Alphred only edits your own files/config, gated so the default behavior is unchanged):

- **Model selection (§29.1).** In the TUI, **`/model <name>` permanently sets your default model** (writes `config.yaml`, survives restart, kept until you change it). Use the **provider-prefixed id** (e.g. `meta/llama-3.3-70b-instruct`, `google/gemma-4-31b-it`) — a bare name may 404 at the provider; `/model` (no args) lists valid names, and setting an unknown name warns you. For per-depth routing, set `ALPHRED_MODEL_HIGH/MID/LOW` or `/model high|mid|low <name>` (`/model high auto` clears; a bare `/model <name>` clears all depth tiers). `/model` (no args) shows the mapping. Alphred swaps `config.yaml`'s `model.default` to the right model just before dispatch (Hermes re-reads it per run); single-slot scheduling makes this safe. Same-provider model-ids work out of the box; cross-provider needs that provider's credentials in `.env`. If you set no tiers, `config.yaml` is never touched.
- **Light harness (§29.2).** Quick/sync answers get a short Alphred system message so they aren't bare-chatbot quality (the empty-`SOUL` cold start) — steering the model to answer directly and use tools with discipline (not run a shell command to answer a factual question). Applied to `/v1/chat/completions`, `/v1/responses`, **and the TUI chat** (injected as the session `system_prompt`). On by default; skipped if you already send a `system` message or `X-Alphred-Harness: off`. Edit with `alphred prompt --light --init`.
- **Config quality audit — `alphred tune` (§29.3).** Diagnoses settings that degrade quality (context-compression protection #1, downgraded auxiliary models #2, tool-search overload #4, web-search backend #5) and, with `--apply`, fixes them via a backed-up, idempotent edit of *your* `config.yaml` (`--revert` to undo). Read-only by default; no LLM calls.
- **Alphred-side MoA (§29.4).** For `high`-depth runs, `ALPHRED_MOA=1` adds a critique→synthesize pass that lifts the result above a single model's first try (opt-in; budgeted by `ALPHRED_MOA_SAMPLES`).

### Skills — built-in, optional, and installable

Alphred is a thin layer over the **Hermes agent**, so every Hermes capability is available to Alphred's work — both in TUI chat and in queued background tasks. That includes the agent's **skill-management tools** (`skill_manage`, plus the skills hub), so you can install and manage skills *through Alphred itself* — just ask in plain language.

Skills come in tiers:

| Tier | Where | Exposed by default? |
|---|---|---|
| **Bundled** | `hermes-agent/skills/` | Yes (e.g. `nano-pdf`, `powerpoint`, `ocr-and-documents`, `claude-code`, `codex`, `opencode`). |
| **Active / user** | `~/.hermes/skills/` (`ALPHRED_HOME`'s Hermes home) | Yes — your installed/created skills live here. |
| **Optional** | `hermes-agent/optional-skills/` | **No** — shipped but not active (e.g. `antigravity-cli` = `agy`, `excel-author`, `dcf-model`). |
| **External / hub** | GitHub etc. | No — fetched & security-scanned on install. |

**Installing an optional (or external) skill — three ways:**

1. **Through Alphred (easiest).** In the TUI (or any chat) just ask, e.g. *"install the antigravity-cli skill"*. The Hermes agent runs its `skill_manage` / hub tool to install it into `~/.hermes/skills/` (`allow_lazy_installs` is on by default). A short request like this is answered synchronously (Light).
2. **Config — expose a directory.** Add the optional path to `~/.hermes/config.yaml`:
   ```yaml
   skills:
     external_dirs:
       - <HERMES_HOME>/hermes-agent/optional-skills/autonomous-ai-agents
   ```
3. **Manual copy.** Copy the skill folder into `~/.hermes/skills/<name>` (you may need a skills re-index/restart).

> **Two layers for CLI-wrapper skills.** Skills like `antigravity-cli` (`agy`), `claude-code`, or `codex` are *procedure guides* — they do **not** bundle the program. You also need the **binary on PATH** (verify e.g. `agy --version`). The skill then tells the agent how to drive it via the `terminal` tool (`agy --print "..."`).

After installing/enabling a skill, **restart the Alphred daemon** (e.g. relaunch `alphred`, or stop the background `serve`) so the agent reloads its skill set. Optionally add a nudge in your `system_prompt.md` (e.g. *"for large coding tasks, prefer the `agy` agent when available"*) to steer the agent toward it.

#### Driving a coding agent (Antigravity `agy`) through Alphred

Once the pieces below are in place, you can hand coding tasks to the Antigravity CLI from a normal Alphred chat — the Hermes agent drives `agy` via the `terminal` tool.

**Prerequisites (one-time):**
- **Skill exposed** — `antigravity-cli` visible to the agent (e.g. via `skills.external_dirs` in `~/.hermes/config.yaml`, pointing at `optional-skills/autonomous-ai-agents`). Verify: `curl localhost:8643/v1/skills | grep antigravity`.
- **Binary installed & authenticated** — `agy --version` works; Antigravity manages its own sign-in (OS keyring / browser). If `agy --print` fails on auth, run `agy` once interactively to sign in.
- **Autonomous execution on** — `ALPHRED_AUTONOMOUS_EXEC` (default on) so the background run isn't blocked at the approval gate.
- Restart the Alphred daemon after config changes.

**How to ask (small models need it explicit).** Naming the tool makes it reliable:

```text
Use the Antigravity CLI (agy) to create a Python function that reverses a string,
in a new file at C:/Users/alpha/agy_demo/reverse.py. Run it non-interactively via
the terminal tool — agy --print "..." with workdir set to that folder. Then read
the file back and report agy's output and the file path.
```

A shorter form also works once the nudge/skill are active: *"Use the `agy` coding agent to scaffold … and report the result."* The task is classified Heavy and runs in the background; watch it in the queue panel or poll `GET /v1/runs/{id}`. If `agy` isn't available the agent falls back to `claude-code`/`codex`/`opencode` or `execute_code`.

---

## Diagnostics

```bash
alphred doctor          # hermes binary, :8642/:8643, model/provider, depth-model tiers, planner, verify/judge, Light harness, MoA, queue + verification stats, safety
alphred doctor --json   # machine-readable

alphred tune            # §29.3 audit Hermes config for quality-degrading settings (read-only)
alphred tune --apply    # apply the recommended fixes (backs up config.yaml; --revert to undo)
```

Both run **no live LLM calls** (quota-safe). `doctor` flags any unreachable component with a fix hint.

---

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `ALPHRED_PROFILE` | `basic` | §35.4 preset for the §34 pipeline flags — `basic` (queue/preemption/verify only), `smart` (+IntentCard & planner — recommended), `full` (+intake questions, step-wise orchestration, watchdog). Persist with `alphred setup --profile <name>`; individual `ALPHRED_*` env vars always override. |
| `ALPHRED_HERMES_API` | `http://localhost:8642/v1` | Hermes API base URL |
| `API_SERVER_KEY` / `ALPHRED_API_KEY` | — | gateway auth token |
| `ALPHRED_HOME` | Hermes home | Alphred state directory |
| `ALPHRED_HERMES_BIN` | resolved | Hermes executable for passthrough |
| `ALPHRED_MAX_RETRIES` | `3` | max retries for transient failures |
| `ALPHRED_RETRY_BASE_SECONDS` | `5` | exponential-backoff base |
| `ALPHRED_CLIENT_TIMEOUT` | `300` | Hermes HTTP client timeout (sec) — headroom for long tool turns / installs |
| `ALPHRED_STREAM_READ_TIMEOUT` | `600` | §32 — injected as `HERMES_STREAM_READ_TIMEOUT` into the Hermes gateway Alphred spawns. Raises the LLM inter-token stream-read timeout (Hermes default 120s) so slow free-tier models (e.g. a 70B on NVIDIA NIM) don't fail queued runs with `APITimeoutError: Request timed out.`. Only bounds a single call; the overall run cap still applies. |
| `ALPHRED_AUTONOMOUS_EXEC` | **on** | Inject `HERMES_YOLO_MODE` into the Hermes gateway Alphred spawns, so background runs can actually run `execute_code` / commands (manual-approval mode would otherwise time-out and block them). Hardline ops (disk wipe, shutdown) are still blocked; only applies to gateways Alphred starts. Set `0` to keep Hermes' approval prompts. |
| `ALPHRED_LLM_CLASSIFY` | off | enable simple LLM classification fallback for ambiguous input |
| `ALPHRED_PLANNER` | off | planning. §19: for ambiguous requests, an LLM decomposition decides Heavy/Light. §34.3 (Plan v2): **every Heavy task gets an executable plan at dispatch time** — steps with a goal, tool hint, expected artifact, and per-step done-when criteria, **grounded against the live capability inventory** (a missing library auto-inserts an install step; a missing skill/CLI hint is downgraded to `execute_code`; repairs are listed in `plan.gaps`). The plan is injected into the run and shown in the TUI detail view / `POST /plan` dry-run. |
| `ALPHRED_VERIFY` | **on** | §21 Tier 0 — deterministic artifact verification of completed Heavy runs (free, no LLM). Set `0` to disable. |
| `ALPHRED_JUDGE` | off | §21 Tier 2 — LLM acceptance judge for `high`-depth runs (uses quota) + self-healing retry |
| `ALPHRED_JUDGE_RETRIES` | `2` | §21 Tier 3 — max verification retries for `high` tasks before `NeedsReview` |
| `ALPHRED_RANK` | **on** | §22 LLM queue ranker — on each Heavy submit, re-rank Heavy tasks by relative urgency + dependencies. No-op (no LLM call) unless another Heavy task is contending. Set `0` to disable. |
| `ALPHRED_LIGHT_HARNESS` | **on** | §29.2 — inject a concise Alphred system message in front of **Light** (quick/sync) answers so they aren't bare-chatbot quality. Skipped if the caller already sends a `system` message or `X-Alphred-Harness: off`. Set `0` to disable (pure passthrough). Edit via `alphred prompt --light --init`. |
| `ALPHRED_MODEL_HIGH` / `_MID` / `_LOW` | — | §29.1 depth-based model routing — the model to use for `high`/`mid`/`low` work (depth names, distinct from the Heavy/Light task weight). Overrides `models.json` (set via `/model high\|mid\|low <name>`). Unset depths inherit the base default; if **no** tier is set anywhere, `config.yaml` is never touched. |
| `ALPHRED_MOA` | off | §29.4 Alphred-side Mixture-of-Agents — for `high`-depth runs only, a critique→synthesize pass refines the result before delivery (uses quota). Off by default. |
| `ALPHRED_MOA_SAMPLES` | `2` | §29.4 budget cap (max candidate/refine passes). |
| `ALPHRED_CAPS` | **on** | §34.5 capability registry — a no-LLM live snapshot of what the agent can *actually* use right now (installed skills, active tools, MCP servers, coding-agent CLIs on PATH, Python libs in the Hermes venv → per-format "can I really produce a PDF?" matrix). Injected into the Heavy-run harness at the `{{CAPABILITIES}}` marker; also powers `GET /capabilities`, `alphred doctor`, and deterministic install hints on verification failures. Set `0` for the static harness text. |
| `ALPHRED_CAPS_TTL` | `3600` | §34.5 capability snapshot cache TTL (seconds). Also refreshed on daemon start and right after an install-type task completes. |
| `ALPHRED_INTENT` | off | §34.2 IntentCard — LLM-first intent triage: one structured call decides kind + priority + depth (+ missing-info signals for the clarification stage), replacing regex as the primary judge. Regex stays as the fast path (status queries, installs, very short greetings) and as the fallback when the call fails. Decisions are logged to `intent_log` for accuracy measurement. |
| `ALPHRED_CLARIFY` | off | §34.4 intake questions — when IntentCard flags **critical** missing info on an interactive (TUI/chat) Heavy request, Alphred asks ≤3 clarifying questions **with recommended answers** before starting (Claude-Code-style: press Enter to accept the recommendation). The task waits in `AwaitingInput`; unanswered questions time out and the task proceeds on recorded assumptions (surfaced in the report). Non-interactive sources (api/cron/subservice) are never asked. Requires `ALPHRED_INTENT=1`. |
| `ALPHRED_CLARIFY_TIMEOUT` | `600` | §34.4 how long (seconds) to wait for answers before proceeding on the recommended assumptions. |
| `ALPHRED_ORCHESTRATE` | off | §34.6 StepRunner — `high`-depth tasks with a Plan v2 are executed **step by step**: each step runs as its own narrow Hermes run (sharing one session), its done-when criteria are verified deterministically right after (file/content/exit-code), and **only the failing step is retried** (with feedback) instead of re-running the whole task. Preemption/transient failures resume **from the current step** (completed steps are never redone — their outputs are passed as context). Whole-task verification (§21 Tier0/judge/MoA) still runs at the end; a judge failure appends a `fix` step instead of a full rerun. Requires `ALPHRED_PLANNER=1`. |
| `ALPHRED_TASK_BUDGET` | `25` | §34.6 per-task Hermes-run budget for orchestrated tasks — exceeded ⇒ the task ends in `NeedsReview` with a partial-success report (`steps_done/steps_total`), never an infinite loop. |
| `ALPHRED_STEP_RETRIES` | `2` | §34.6 max retries per step when its acceptance checks fail. Exhausted ⇒ **one replan** (the planner gets the completed work + failure context and plans the remaining work with a different approach; budget carries over) ⇒ then partial-success `NeedsReview`. |
| `ALPHRED_WATCHDOG` | off | §34.6 E3 in-flight watchdog — detects a run going wrong **while it runs**: ≥N consecutive tool failures (from the event stream) or no observable activity for `ALPHRED_STALL_SECONDS` ⇒ stop the run and re-queue it with a corrective hint ("don't repeat the same approach — diagnose first, try a different tool/library"). Orchestrated tasks get the hint on the current step; repeated interventions are capped by `ALPHRED_MAX_RETRIES` ⇒ `NeedsReview`. |
| `ALPHRED_STALL_SECONDS` | `600` | §34.6 E3 no-progress threshold (seconds without any run event / DB activity). |
| `ALPHRED_TOOL_FAIL_LIMIT` | `3` | §34.6 E3 consecutive tool-failure threshold that triggers intervention. |

### Classification (how Heavy/Light is decided)

0. **IntentCard (opt-in, `ALPHRED_INTENT`)** — when enabled, a single structured LLM call judges intent (kind + priority + depth + missing-info) for everything except the cheap fast path below; the prefilter becomes fallback-only. This fixes the "short chat message that is actually heavy work" class of misroutes. With `ALPHRED_CLARIFY=1`, critical missing info on an interactive Heavy request additionally triggers **intake questions with recommended answers** before the task starts (§34.4).
1. **Cheap prefilter (no LLM)** — status/list queries → Light; **skill/package install & enable requests → Heavy** (slow admin ops run in the background, avoiding the sync timeout); explicit large-scope ("entire codebase", "migrate", "crawl", or ≥2 heavy keywords) → Heavy; greetings/short/realtime chat → Light.
2. **Ambiguous middle** — if `ALPHRED_PLANNER` is on, an LLM decomposes the request into coarse sub-tasks; a deterministic rule (≥3 steps, or any heavy/compute/edit step, or ≥2 tool steps ⇒ Heavy) sets the weight. Falls back to conservative Heavy if the planner is off/unavailable.
3. **Executable plan at dispatch (§34.3)** — when the planner is on, every Heavy task gets a **Plan v2** right before it runs: concrete steps with tool hints, expected artifacts, and done-when criteria, built from the request + your intake answers + the live capability inventory, then deterministically repaired against real capabilities (install steps / hint downgrades). Stored on the task, injected into the run, and previewable via `POST /plan`.
4. **Relative ranking (§22)** — when a Heavy task is submitted while other Heavy tasks are queued, an LLM re-ranks them all by relative urgency and **dependencies** (e.g. "do B first" or "B must finish before A is useful" raises B above A — even the running task can be preempted). Priorities for the new *and* existing tasks are adjusted; the scheduler's preemption then reorders execution. On by default (`ALPHRED_RANK`); skipped with no LLM call when nothing is contending.

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
- **Slash commands**: ✅ `/` opens a filtered command palette (`/help`, `/model`, `/depth`, `/plan`, `/clear`, `/queue …`, `/answer`, `/sessions`, `/skills`, `/export`, `/banner`, `/quit`)
- **TUI overhaul T1 (§36)**: ✅ widget-based chat (tool blocks `●/⎿` update in place), Markdown-rendered answers, terminal-adaptive theme (forced background removed), compact welcome panel (full art via `/banner`), status bar with spinner/elapsed time + live queue badges `▶⏳❓⚠`
- **TUI overhaul T2 (§36)**: ✅ Esc interrupts the streaming answer, messages typed while busy auto-send afterwards, intake question cards (↑↓+Enter with ✦recommended preselected), fuzzy slash palette + argument completion (`/model`·`/depth`·`/sessions`·`/queue`), interactive session picker, Shift+Tab depth cycling, Ctrl+O verbose toggle (full thinking/tool output)
- **TUI overhaul T3 — Mission Deck (§36)**: ✅ always-on queue panel retired → 3-tier queue: status-bar badges + **inline task cards** in the conversation (estimate/DoD, step progress bar, current step, preemption reason, verification badge — all updating in place) + **queue deck modal** (`Ctrl+T` / `/queue`: list + detail + single-slot visualization, action keys always shown) · `/answer` summons the question card for any awaiting-input task · completion/review/discard/awaiting transitions raise a toast + terminal bell (`ALPHRED_TUI_BELL`)
- **TUI overhaul T4 — polish (§36)**: ✅ `Ctrl+Y` copies the last answer, `/export` saves the session as Markdown, mouse re-enabled (wheel scroll / click to expand), status bar collapses on narrow terminals, chat widget cap for long sessions
- **Live token streaming**: ✅ streaming answer with queue badges + ⚠needs-attention
- **Plan-aware classification**: ✅ LLM decomposes ambiguous requests into sub-tasks → deterministic Heavy/Light + plan reused at execution (`ALPHRED_PLANNER`)
- **Live step progress**: ✅ background runs tracked via run events → inline task card shows a step progress bar + current step; deck detail shows the full plan checklist + verification evidence
- **Hermes stays stock**: ✅ Alphred's identity lives in its own TUI; `hermes` has zero Alphred traces
- **Output quality (§29)**: ✅ per-depth model routing (`/model high|mid|light`, `ALPHRED_MODEL_*`), Light harness (quick-answer system message), `alphred tune` (Hermes config quality audit/apply), Alphred-side MoA for high-depth — all core-untouched
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
  classifier.py     Light/Heavy classification + planner/judge/ranker/MoA prompts
  nlq.py            natural-language queue management (queue ask / /queue/ask)
  prompt.py         execution harness loaders (§26/§29.2) + background input assembly
  verify.py         §21 artifact verification + hallucination/attention heuristics
  llm_calls.py      Hermes-backed aux LLM callables (classify / plan / judge / rank / MoA)
  hermes_client.py  Hermes :8642 API client
  queue_manager.py  priority queue + single-slot scheduler + preemption + Light fast path
  queue_md.py       QUEUE.MD projection
  eventbus.py       §33 in-process run-event fan-out (Heavy live streaming)
  runtime.py        manager assembly + depth-model applier (§29.1) + event bus
  gateway.py        app assembly (create_app) + scheduler + Hermes(:8642) upstream lifecycle
  server/           FastAPI routers, grouped
    deps.py         GatewayDeps + auth + shared request helpers (route_realtime, task_view, …)
    routes_openai.py  /v1/chat|responses|runs|plan|models|skills + /chat/stream (SSE)
    routes_queue.py   /queue/* (list/prio/pause/resume/retry/discard/purge/clear/ask)
    routes_models.py  /models/available + /models/tiers (§29.1)
    routes_admin.py   / · /dashboard (unauth) + /safety (auth)
  tune.py           §29.3 Hermes config quality audit/apply (core-untouched)
  dashboard.py      web dashboard (single HTML, no dependencies)
  safety.py         #30719 safety net (payload filter + restart-storm guard)
  cron_intercept.py periodic jobs → queue (self-contained cron matcher)
  tui.py            dedicated Alphred TUI — App core (lifecycle/render/session state)
  tui_base.py       TUI constants + command registry + chat/tool/card/question widgets
  tui_commands.py   CommandsMixin — slash palette, input history, command handlers
  tui_queue.py      QueueMixin + QueueDeck — mission deck (badges/cards/deck/live/alerts)
  tui_chat.py       ChatMixin — /chat/stream SSE consumption + rendering
  tui_sessions.py   TUI conversation persistence (restorable sessions)
  splash.py         Alph-RED ASCII banner for the TUI start screen
  cli.py            CLI passthrough + queue / serve / setup / tui / doctor / prompt / tune
poc/                Phase 0 primitive verification
tests/              core-logic tests
```
