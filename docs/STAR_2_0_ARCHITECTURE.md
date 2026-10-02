# STAR 2.0 — ARCHITECTURE

> Phase 0 deliverable #2. Target architecture for the STAR 2.0 coding-agent build,
> derived from the blueprint and constrained by the audit in `STAR_CURRENT_STATE.md`.
>
> **Prime directive:** the existing PySide6 Star frontend stays the visible Star.
> Everything below is built *around* it. New capability ⇒ new file. Reuse ⇒ adapter.

---

## 1. Layer map

```
                        ┌──────────────────────────────────────────────┐
   USER  ──voice/text──▶│  EXISTING STAR FRONTEND (PySide6 HUD)        │  ← PRESERVED
                        │  run.py → Frontend/main.py → StarWindow      │
                        └───────────────┬──────────────────────────────┘
                                        │ Qt signals (unchanged)
                                        ▼
                        ┌──────────────────────────────────────────────┐
                        │ Backend/bridge.py  AssistantBridge           │  ← additive hook only
                        │   └── Backend/star/gateway/frontend_adapter.py │     STAR_GATEWAY_URL
                        └───────────────┬──────────────────────────────┘
                                        │ HTTP (request/response) + WS (events)
════════════════════════════════════════▼═══════════════════════════════════════
  GATEWAY           Backend/star/gateway/{server,api,websocket,events,console}
                    • REST  /api/v1/{health,chat,tasks,tools,memory,audit,confirm,stop}
                    • WS    /ws  → star.mission.v1-style event stream
                    • stdlib asyncio only (no new runtime dependency)
════════════════════════════════════════▼═══════════════════════════════════════
  BRAIN             Backend/star/brain/{schemas,context,reasoning,planning,prediction,reflection}
                    UserRequest → Context → Plan → Task[] → ToolCall[]
════════════════════════════════════════▼═══════════════════════════════════════
  ORCHESTRATOR      Backend/star/orchestrator/{router,runner,checkpoint,recovery}
                    decides agent/tool sequence · cancellation · retry · checkpoints
════════════════════════════════════════▼═══════════════════════════════════════
  AGENTS            Backend/star/agents/{conversation,research,browser,computer,
                                         filesystem,system,coding}
                    each is a goal-oriented worker (Agent ≠ Tool)
════════════════════════════════════════▼═══════════════════════════════════════
  TOOL REGISTRY     Backend/star/tools/{registry,browser,screen,mouse,keyboard,
                                        filesystem,system,code}
                    typed params (pydantic) · risk class · permission · audit
════════════════════════════════════════▼═══════════════════════════════════════
  WORKSPACE         Backend/star/workspace/{manager,session,isolation}
                    data/workspace/<session>/ · isolated browser profile · dry-run default
════════════════════════════════════════▼═══════════════════════════════════════
  EXECUTION LOOP    OBSERVE → ACT → VERIFY → RECOVER   (task level, mirrors agent/ L7)
════════════════════════════════════════▼═══════════════════════════════════════
  LOW-LEVEL BASE    agent/  (L0 core · L1 geometry · L2 perception · L3 world ·
                             L4 motor · L5 safety governor · L6 skills · L7 planning)
                    Backend/{nlu,llm,voice,tools,agent_bridge}  (reused via adapters)
════════════════════════════════════════════════════════════════════════════════
  CROSS-CUTTING     memory/ · learning/ · security/ · observability/ · database/ · config/
```

**Design rule (from the blueprint, enforced in code):**
the **brain** decides *what should happen* · **tools** execute *capabilities* · the
**safety layer** decides *what is allowed* · the **verifier** checks *whether it worked*.

---

## 2. Package layout (target, mapped onto the real repo)

```
STAR/
├── Frontend/                     # EXISTING STAR UI — preserve, **byte-identical to main** (no STAR 2.0 file lives here)
├── Backend/
│   ├── star/                     # ★ NEW: the STAR 2.0 modular monolith
│   │   ├── __init__.py           #   version, public re-exports
│   │   ├── main.py               #   composition root: build_app() / serve()
│   │   ├── config/settings.py    #   pydantic-settings style, env-driven, no secrets
│   │   ├── gateway/              #   server.py http.py websocket.py api.py events.py console.py
│   │   ├── brain/                #   schemas.py context.py reasoning.py planning.py
│   │   │                         #   prediction.py reflection.py
│   │   ├── orchestrator/         #   router.py runner.py checkpoint.py recovery.py
│   │   ├── agents/               # ✅ base.py (the StarAgent spine) registry.py — workers live in
│   │   │                         #    their own packages: browser/agent.py, computer/agent.py
│   │   ├── voice/                #   base.py stt.py tts.py language.py speaker.py pipeline.py
│   │   ├── computer/             # ✅ guardrails.py motor.py tools.py agent.py — thin adapters over
│   │   │                         #    agent/motor + agent/safety + agent/world (Phase 6)
│   │   ├── browser/              # ✅ guardrails.py extract.py tools.py agent.py (Phase 5)
│   │   ├── tools/                #   registry.py base.py screen.py mouse.py keyboard.py
│   │   │                         #   filesystem.py system.py browser.py code.py legacy_adapter.py
│   │   ├── memory/               # ✅ layers.py retrieval.py manager.py tools.py (Phase 8)
│   │   │                         #   five layers as adapters over the existing stores — no second store
│   │   ├── learning/             # ✅ schemas.py patterns.py feedback.py validator.py loop.py tools.py (Phase 9)
│   │   │                         #   safe loop: candidate ledger + six gates → promotes into the existing stores
│   │   ├── security/             #   risk.py policy.py identity.py confirmation.py
│   │   │                         #   secrets.py audit.py budget.py
│   │   ├── workspace/            # ✅ session.py isolation.py manager.py tools.py (Phase 7)
│   │   ├── observability/        #   events.py metrics.py logging.py tracing.py
│   │   ├── database/             #   engine.py migrations.py repositories.py
│   │   └── cli.py                #   `python -m Backend.star` / star2 console script
│   ├── bridge.py                 # existing (additive gateway mirror hook)
│   ├── brain.py, agent_bridge.py, config.py, nlu/, llm/, voice/, tools/   # existing — reused
├── agent/                        # existing L0–L7 low-level foundation — reused, never forked
├── data/                         # existing jsonl + workspace/ runtime dirs
├── tests/                        # existing 18 files + new test_star2_*.py
├── docs/                         # STAR_CURRENT_STATE.md, STAR_2_0_ARCHITECTURE.md, phase reports
├── scripts/                      # audit.py, smoke.py, run_gateway.py
├── logs/                         # runtime jsonl (gitignored)
├── .env.example                  # ★ NEW — every STAR_* variable documented, no values
├── pyproject.toml                # additive: [project.optional-dependencies].star2
├── run.py / run.bat              # existing desktop launcher — untouched
└── README.md                     # additive STAR 2.0 section
```

No duplicate subsystems: `computer/`, `tools/screen|mouse|keyboard`, `memory/semantic` are **thin
adapters** over `agent/` — verified by the rule "every new module must import an existing one or
declare in its docstring why it cannot".

---

## 3. Core schemas (`Backend/star/brain/schemas.py`)

```python
Language      = "bn" | "en" | "mixed"
RiskLevel     = LOW | MEDIUM | HIGH | CRITICAL          # security/risk.py
TaskState     = PENDING | RUNNING | WAITING_CONFIRMATION | VERIFYING | DONE | FAILED | CANCELLED | RECOVERED
AgentName     = conversation | research | browser | computer | filesystem | system | coding

UserRequest   { request_id, text, language, source(voice|text|api), speaker_id?, audio_ref?, created_at }
Context       { session_id, user_id, language, working_memory[], retrieved_memory[],
                screen_summary?, active_window?, recent_tasks[], predictions[] }
ToolCall      { call_id, tool, arguments(dict, validated), risk, requires_confirmation,
                state, result?, error?, attempt, duration_ms }
Task          { task_id, goal, agent, steps[ToolCall], state, priority, deadline?,
                checkpoint?, verification{expected, observed, ok}, retries }
Plan          { plan_id, request_id, intent, rationale, tasks[Task], requires_confirmation,
                predicted_next[], created_at }
TaskResult    { task_id, ok, summary(bn), summary_en, artifacts[], evidence[], spoken_response }
StarEvent     { event_id, ts, session_id, kind, phase, payload }   # discriminated union, 16 kinds
```

`StarEvent` kinds (the `star.mission.v1` stream the frontend README asks for):
`session.started, request.received, language.detected, context.built, plan.created,
task.started, task.progress, tool.called, tool.result, confirmation.requested,
confirmation.resolved, verification.result, memory.updated, safety.blocked,
task.completed, task.failed, task.recovered, response.spoken, workspace.created,
workspace.destroyed, emergency.stop`.

---

## 4. Security model

| Risk | Examples | Control |
|---|---|---|
| **LOW** | search, read public page, conversation, list files in workspace | auto-allow if tool is allowlisted |
| **MEDIUM** | open app, edit ordinary file, set volume, navigate browser | policy check; optional confirmation |
| **HIGH** | delete data, send message/email, install software, write outside workspace | explicit confirmation + audit |
| **CRITICAL** | credentials, `rm -rf`, registry edits, disabling security, destructive shell | strong confirmation / **deny by default** |

Enforced by four cooperating layers (defence in depth):

1. `security/risk.py` — classifier: tool metadata + argument inspection (path escape, shell metacharacters, destructive verbs, Bengali and English keyword lists reused from `GovernorConfig.forbidden_keywords`).
2. `security/policy.py` — allowlist/denylist per tool, parameter validation against the tool's pydantic model, per-session action budget, rate limit, dry-run override, `PolicyDecision{ALLOW, ALLOW_WITH_CONFIRMATION, DENY}`.
3. `security/confirmation.py` — pending-approval registry with TTL; the gateway raises `confirmation.requested`; a human resolves it via UI/WS/REST. `AutoDeny` in CI.
4. `agent/safety/governor.py` — unchanged, still gates every motor action (budget, rate, wall clock, forbidden regions, keyword blocklist, capability tokens).

Plus: `security/secrets.py` (env/`.env` loader, redaction in logs and audit, never persisted),
`security/audit.py` (append-only JSONL + SQLite mirror, correlation ids),
`security/identity.py` (user/session/speaker binding), and an **emergency stop** that cancels all
running tasks, kills workspace processes and mutes the motor controller.

**Hard rule:** raw model output never executes a shell/OS command directly. LLM output is parsed into
`ToolCall`s, validated against a pydantic schema, classified, policy-checked and only then executed.

---

## 5. Memory & learning

✅ **Built in Phase 8** — as five *adapters* in `memory/layers.py`, with **no second store** (the blueprint's
"inspect existing implementation before introducing another memory store"):

| Layer | Store | Implementation |
|---|---|---|
| Working | the brain's per-session scratch | `WorkingMemory` **attached** to `brain.context.ContextBuilder` (one buffer, bounded deques only as a fallback) |
| Episodic | `episodes.jsonl` (JSONL) | reuse `agent.planning.memory.EpisodicMemory` — *no* SQLite mirror, so `build_context_block` stays clean |
| Semantic | `star_memory.db` (SQLite, hashing embeddings) | reuse `agent.memory.store.MemoryStore`, rows `kind=fact` |
| Preferences | the same `MemoryStore`, rows `kind=preference`, `key/value` | reuse `latest_by_key` / `preferred_int/preferred_str` |
| Procedural patterns | `data/patterns.jsonl` | reuse `brain.prediction.PatternStore` — read-only in Phase 8; **Phase 9 promotes validated candidates into it** |

Retrieval (`memory/retrieval.py`) = hybrid: every layer asked in parallel under a timeout, relevance clamped to
`[0,1]` and multiplied by that layer's weight, duplicates collapsed, a per-layer cap so one loud layer cannot
monopolise the answer, and a trace (candidates/duplicates/timeouts/errors) published as `memory.retrieved`. The
ranked list is injected into `Context` **before** planning via `MemoryManager`, which implements the Phase 3
`MemoryRetriever` protocol. `memory/manager.py` is the single face for brain, gateway (`snapshot()`), tools and
events; `memory/tools.py` adds 7 typed tools (`memory_recall`, `memory_list_preferences`, `memory_list_episodes`,
`memory_read_working`, `memory_status`, `memory_preference_set`, `memory_forget` — the last one high-risk and
confirmation-gated because deletion is irreversible).

✅ **Learning loop built in Phase 9** (`learning/`): `interaction → observation → feedback → candidate pattern →
validation → memory update → future retrieval`. Finished runs/turns and explicit feedback become **inert
candidates** (`CandidateKind` is a closed enum — **{pattern, preference}**, no code/weight kind) in an append-only
JSONL ledger. A **dry-run never claims success** (it records a sighting with no outcome) and a terminal candidate
is never reopened. `CandidateValidator` runs six **kind-aware** gates — `frequency`, `success_rate`, `risk`,
`no_code`, `no_secret`, `approval` — where a pattern is an inference from repetition (≥ 3 sightings, ≥ 0.8 success)
and a preference is a **user-approved** habit (≥ 1 statement, explicit positive feedback, no rejection); an
explicit rejection outranks everything, and a broken risk classifier **fails closed**. Promotion writes to
**exactly two places, both borrowed** — `brain.predictor.store.record([before, after])` and
`memory.set_preference_now(key, value)` — so there is **no second store** and **no self-modification of executable
code or model weights, ever** (`no_self_modification: true` is reported by the snapshot, the console and
`learning_status`). Four typed tools (`learning_status`/`learning_list_candidates` low, `learning_feedback`/
`learning_cycle` medium); the loop is built **always** (a disabled loop reports `off` and touches no disk), and
observation is hooked into `_remember_chat`/`_remember_run` off the event loop.

Prediction (`brain/prediction.py`) proposes likely next actions from context/history and is now **reinforced** by
the learning loop's promoted patterns; every prediction still traverses the normal policy/tool boundary —
prediction can never bypass permissions, and the loop never auto-executes a prediction.

---

## 6. Voice

```
STTProvider (protocol)      → GoogleDualProvider (wraps Backend/bridge VoiceListener logic)
                              WhisperProvider (optional extra) · BrowserSTTProvider (Web Speech API
                              from the ops console / any web client) · FileSTTProvider (tests)
TTSProvider (protocol)      → EdgeTTSProvider (wraps Backend/voice/tts.VoiceEngine) · NullTTS (tests)
language.py                 → script detection (Bengali range), Banglish transliteration lexicon,
                              code-switch handling, per-language STT/TTS routing, response language policy
speaker.py                  → SpeakerVerifier seam (enrol/compare), deliberately separate from STT
pipeline.py                 → barge-in: VAD-ish speech_started → stop TTS → cancel in-flight task
                              (CancellationToken) → new request; transcript retention configurable
                              (STAR_VOICE_KEEP_TRANSCRIPTS=false by default)
```

---

## 7. Invisible / background workspace

```
VISIBLE   (existing frontend)      conversation · task progress · result · activity indicator
BACKGROUND (WorkspaceManager)      isolated dirs · isolated browser profile · screen capture ·
                                   vision/perception · mouse+keyboard controller · checkpoints · verifier

FLOW  Voice → Plan → Permission → Workspace → Observe → Act → Observe → Verify → Recover/Complete → Voice
```

`workspace/manager.py` creates `data/workspace/<session_id>/{browser_profile,downloads,scratch,logs}`,
tracks lifecycle (`create/attach/snapshot/destroy`), enforces a max-session count and TTL, and exposes a
`WorkspaceSession` handle to agents. **No virtual-desktop technology is hard-coded** — `isolation.py`
defines the seam (`IsolationBackend`: `NullIsolation` → `ProcessIsolation` → future `WindowsVirtualDesktop`).
Dry-run stays the development default (`STAR_DRY_RUN=true`).

---

## 8. Agents vs tools

An **Agent** is a goal-oriented worker; a **Tool** is a capability; the **Orchestrator** decides which
agent/tool sequence runs.

| Agent | Responsibility | Primary tools | Reuses |
|---|---|---|---|
| Conversation | normal dialogue, persona, Bengali warmth | `respond`, `remember` | `Backend/llm/provider.py` |
| Research | search / read / compare / summarise | `web_search`, `web_read`, `wikipedia` | `Backend/tools/web/*` |
| Browser | navigate and interact with web UI | `browser_*` | `Backend/tools/web/control.py`, Playwright (optional) |
| Computer | observe desktop, control mouse/keyboard | `screen_*`, `mouse_*`, `keyboard_*` | `agent/` L2–L6 via `ComputerControlAgent` |
| Filesystem | read/create/transform authorised files | `fs_*` | new, workspace-confined |
| System | approved app/system actions | `app_launch`, `volume`, `brightness`, `lock` | `agent/skills/builtins`, `Backend/tools/system` |
| Coding | inspect/edit/test code | `code_read`, `code_write`, `code_run` | new, sandboxed to workspace |

---

## 9. Phase plan and the EXACT Phase 1 scope

| Phase | Build | Done when |
|---|---|---|
| **0** | Baseline + inventory | ✅ this document + `STAR_CURRENT_STATE.md` + baseline `1 failed, 99 passed` |
| **1** | Frontend integration | ✅ zero-touch adapter (`gateway/frontend_adapter.py`); `git diff main -- Frontend/` empty; 19 adapter tests · `star-2.0-phase-1` |
| **2** | Voice | ✅ STT/TTS providers + bn/en routing + barge-in; transcripts opt-in · `star-2.0-phase-2` |
| **3** | Brain | ✅ typed schemas + memory-before-planning + prediction/reflection · `star-2.0-phase-3` |
| **4** | Tools | ✅ typed registry (48 tools) + permission ladder + confirmations + audit + dry-run · `star-2.0-phase-4` |
| **5** | Browser agent | ✅ 10-rule URL guardrails + bounded fetch/extract + 8 typed tools · `star-2.0-phase-5` |
| **6** | Computer agent | ✅ 8 guardrail rules + the reused six-invariant governor + 10 typed tools; dry-run default, `null` motor headless · `star-2.0-phase-6` |
| **7** | Invisible workspace | ✅ jailed sessions + quotas + TTL + checkpoints/restore + verified destroy + the isolation seam (`null`/`process`/`windows_virtual_desktop`); 9 typed tools; agents run inside a session · `star-2.0-phase-7` |
| **8** | Memory | ✅ five layers (working attached to the brain · episodic · semantic · preferences · procedural) over the **existing** stores, hybrid weighted retrieval with a trace, 7 typed tools, memory slot live (14 capabilities); `git diff main -- Frontend/` empty; 99 tests · smoke 54 · `star-2.0-phase-8` |
| **9** | Learning | ✅ the safe loop (observation → inert candidate → six kind-aware gates → promotion) over the **existing** stores: a closed `{pattern, preference}` ledger, dry-run never claims success, `no_code`/`no_secret`/risk/approval gates, **no self-modification of code or weights**, promotes only into the borrowed `PatternStore` + preference layer; 4 typed tools, learning slot live (15 capabilities); `git diff main -- Frontend/` empty; 141 tests · smoke 65 · `star-2.0-phase-9` |
| **10** | Advanced orchestration | ✅ multi-agent routing (conversation, browser, computer, tools) + JSONL checkpoints (pause/resume/rollback) + bounded honest recovery (RECOVERED state) + graceful cancellation; 6 typed tools, orchestrator slot live (16 capabilities, 38 routes, 74 tools); `git diff main -- Frontend/` empty; 23 tests · smoke 71 · `star-2.0-phase-10` |
| **11** | Security | ✅ multi-tiered security policy + secret vault (<set>/<unset>) + active regex credential scanner/redaction + role hierarchy (Owner 'Nilanjan', Operator, Guest, System) + action/minute sliding budgets + circuit-breaker emergency stop; 5 typed tools, 3 HTTP routes; 27 tests · smoke 79 · `star-2.0-phase-11` |
| **12** | Optimisation & Packaging | ✅ 100% test pass rate (767/767 passed, baseline fixed), persona & comprehension verified across Bengali/Banglish/English, zero frontend diff invariant enforced, packaging & entry point verified · `star-2.0-phase-12` |

**Phase 1 exact scope (next milestone, one reviewable commit):**

1. `Backend/star/config/settings.py` — env-driven `Settings` (host, port, dry-run, safety level, paths, feature flags). No secrets.
2. `Backend/star/gateway/http.py` — dependency-free HTTP/1.1 + RFC 6455 WebSocket framing on `asyncio` streams (parse/serialise, masking, ping/pong/close, fragmentation).
3. `Backend/star/gateway/server.py` — `GatewayServer` (start/stop, bind `0.0.0.0`, keep-alive, graceful shutdown).
4. `Backend/star/gateway/api.py` — routes: `GET /api/v1/health`, `GET /api/v1/info`, `POST /api/v1/chat`, `GET /api/v1/events` (SSE-ish poll fallback), `WS /ws`.
5. `Backend/star/gateway/events.py` — `StarEventBus` bridging `agent.core.events.BUS` → gateway subscribers with a bounded replay ring buffer.
6. `Backend/star/gateway/console.py` — single-file **ops/diagnostics console** (inline CSS/JS, no CDN) so the gateway is verifiable in a browser. *This is a developer tool, explicitly not a replacement for the Star HUD.*
7. `Backend/star/gateway/frontend_adapter.py` — thin optional client (stdlib only). Zero-touch: it *connects to* the bridge's existing Qt signals instead of adding hooks to them, so `Frontend/` and `Backend/bridge.py` stay exactly as shipped.
8. `Backend/bridge.py` — additive, env-gated mirror hook (`STAR_GATEWAY_URL`); when unset the code path is identical to today.
9. `scripts/run_gateway.py`, `.env.example`, tests `tests/test_star2_gateway.py`, `tests/test_star2_settings.py`.
10. Run full suite; write `docs/PHASE_1_REPORT.md`; commit `feat(star-2.0): phase 1 — gateway + frontend adapter`; tag `star-2.0-phase-1`.

**Phase 1 exit criteria:** HUD pixels unchanged · `run.py` unchanged · gateway answers health/chat ·
WS streams events · frontend adapter connects when the env var is set and is a no-op when it is not ·
all pre-existing tests still pass (same single known failure) · new tests green.

---

## 10. Non-negotiables carried into every phase

1. Inspect before changing. 2. Preserve the frontend's visual design and UX. 3. Never rewrite the
frontend. 4. No deletion of working functionality without a migration plan. 5. Reuse existing
computer-control/safety/planning/memory code. 6. Keep brain, orchestration, agents, tools, computer
control, memory, security and UI separate. 7. Every OS action passes a safety/policy layer.
8. Computer automation starts in dry-run. 9. New core modules ship with tests. 10. Never hard-code
secrets. 11. Modular monolith first. 12. Sensitive actions require explicit confirmation.
13. `OBSERVE → ACT → VERIFY → RECOVER`. 14. Visible Star UI stays independent from background automation.

After each phase: **tests → smoke test → logs → changed-file report → documentation → commit.**
One phase = one reviewable milestone. `main` stays stable; risky work lives on `star-2.0`.
