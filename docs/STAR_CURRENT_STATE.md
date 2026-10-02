# STAR — CURRENT STATE (Phase 0 Repository Audit)

> Phase 0 deliverable #1 of the STAR 2.0 Blueprint.
> Method: full read-only inspection of every top-level directory and key file,
> dependency probe, and a baseline test run. **Nothing was modified during this audit.**
>
> Audit date: 2026-09-22 · Branch: `star-2.0` (from `main` @ upstream import) ·
> Runtime probe environment: Linux x86_64, Python **3.13.14**, no display server,
> no microphone, `pydantic 2.13.4`, `pytest 9.0.3`, PySide6 **not installed**.

---

## 0. Executive summary

| Question | Answer |
|---|---|
| What is this product? | A **desktop voice assistant** ("Star") for Windows-first use, with a PySide6 HUD frontend, a Bengali/English brain, and a separate ultra-type-safe computer-control agent (`agent/`, "v6.0"). |
| Frontend framework | **PySide6 (Qt 6)** — custom-painted overlay window, *not* a web app. |
| Frontend entry point | `run.py` → `Frontend/main.py::main()` → `StarWindow`. |
| Backend entry point | `Backend/bridge.py::AssistantBridge` (Qt object graph) → `Backend/brain.py::AssistantBrain.process()`. |
| Is there an HTTP/WS API today? | **No.** There is no server, no socket, no REST surface. Frontend and Backend live in one process, connected by Qt signals. |
| Low-level agent | `agent/` — L0…L7 layered package: core → geometry → perception → world → motor → safety → skills → planning, plus `facade.ComputerControlAgent`. |
| Tests | 100 collected, **99 passed / 1 failed** (stale expectation in `tests/test_command_router.py::test_google_search`). |
| Biggest asset to reuse | `agent/` (governor, skills, planner, memory, perception) and `Backend/nlu/command_router.py` (1257 lines of Bengali/Banglish intent routing). |
| Biggest gap for STAR 2.0 | No gateway, no typed task/plan schema above the skill layer, no orchestrator, no browser agent, no isolated workspace, no policy/risk layer above the motor governor, no confirmation UX, no structured audit trail. |

---

## 1. Frontend — entry point, framework, components, dependencies

**Framework:** PySide6 (Qt for Python) ≥ 6.5. Pure `QWidget` + `QPainter` custom painting — no Qt Designer `.ui` files, no QML, no web tech.

**Entry chain:**

```
run.py                       (adds repo root + Frontend/ to sys.path, forces UTF-8 stdio)
  └── Frontend/main.py::main()
        └── QApplication → StarWindow (QWidget, frameless, translucent, always-on-top)
run.bat                      (Windows wrapper: `python run.py %*`, pauses on error)
```

**Files (3 096 lines total):**

| File | Lines | Role |
|---|---|---|
| `Frontend/main.py` | 1056 | Window shell, presence modes (ORB / CENTER / RAIL), input bar, header, footer, particles, hex background, bridge wiring |
| `Frontend/reactor.py` | 815 | The central multi-ring "reactor" animation; states `STATE_IDLE / STATE_THINK / STATE_SPEAK / STATE_ACT` |
| `Frontend/content.py` | 417 | `ActivityLog` (per-glyph typewriter) + `MissionCard` widgets |
| `Frontend/panels.py` | 386 | `GlassPanel` — corner-bracket trace, scanline materialize, sweep dismiss |
| `Frontend/result_panel.py` | 210 | `ReachResultPanel` — renders internet/reach results (YouTube cards etc.) |
| `Frontend/tokens.py` | 152 | Design tokens: NAVY/CYAN/GOLD/OK/AMBER/ALERT, `MOTION` table, mode sizes, fonts |

**Key classes in `main.py`:** `HexBackground`, `AmbientParticles`, `CyberButton`, `HeaderBar`, `StatusFooter`, `GlowingInputBar`, `StarWindow`.

**Integration points already present in the frontend (these are the seams Phase 1 must use):**

| Seam | Signature | Meaning |
|---|---|---|
| `StarWindow._on_cmd_submitted` | typed text → `bridge.send_query(str)` | text command entry |
| `AssistantBridge.state_changed` | `Signal(str)` — `'idle' \| 'hear' \| 'think' \| 'act' \| 'speak'` | drives reactor state |
| `AssistantBridge.log_emitted` | `Signal(str, str)` — (text, `'cyan'\|'gold'\|'ok'\|'alert'`) | activity log lines |
| `AssistantBridge.response_ready` | `Signal(str)` | final assistant text |
| `AssistantBridge.reach_result_ready` | `Signal(dict)` | structured result cards |
| `Frontend/README.md` note | "Ch. 34 `star.mission.v1` event schema" | the frontend *expects* a mission/event bus — STAR 2.0's gateway events fulfil it |

**Frontend dependencies:** `PySide6>=6.5` only (`Frontend/requirements.txt`). It also imports `Backend.bridge` (which pulls `pygame`, `speech_recognition`, `pyautogui`, `pycaw` …) — so the frontend is **not** dependency-isolated today.

---

## 2. Backend — entry point, services, routes, dependencies

There is no server entry point. The backend is an **in-process service graph** rooted at two singletons.

```
Backend/bridge.py
├── VoiceListener(QObject)     mic thread → speech_recognition → Google STT (bn-IN + en-IN in parallel)
│                              → `_prefer_english()` disambiguation → utterance_recognized
├── SpeechPlayer(QObject)      pygame/SDL2 mixer playback, echo-settling delay, barge-in stop
├── BrainWorker(QThread)       runs AssistantBrain.process() off the UI thread
└── AssistantBridge(QObject)   the only object the Frontend talks to

Backend/brain.py::AssistantBrain.process(query) -> dict
    1. agent.get_memory_context(query)          (semantic recall → prompt block)
    2. provider.process_query(query, memory)    (LLM or offline intent engine)
    3. conditional episodic remember()          (conversation turns only)
    4. synthesize_speech(response)              (edge-tts → cached mp3 path)
    returns {response, audio_path, actions, source, memory_context}
```

**Backend modules:**

| Path | Lines | Purpose |
|---|---|---|
| `Backend/autonomous_agent.py` | 2037 | **Legacy monolith** — an earlier single-file copy of the `agent/` layers (own `ActionKind`, `EventBus`, `CircuitBreaker`, `Metrics`, governor…). Superseded by `agent/`. |
| `Backend/llm/knowledge.py` | 1683 | Offline knowledge/intent tables |
| `Backend/nlu/command_router.py` | 1257 | Bengali/Banglish/English rule router → `{actions:[{tool,args,result}]}`; 20+ `_route_*` functions |
| `Backend/llm/provider.py` | 847 | `LocalOllamaProvider`, `GoogleGeminiProvider`, `OfflineIntentEngine`, `get_llm_provider()` |
| `Backend/bridge.py` | 420 | Qt voice/brain bridge (above) |
| `Backend/agent_bridge.py` | 250 | **Thread-safe adapter** to `agent.facade.ComputerControlAgent`: dedicated daemon asyncio loop, `quick()/see()/run()/remember()/health()` |
| `Backend/llm/agent_planner.py` | 193 | `AgentLLMPlanner` — implements the `agent` `Planner` protocol via LLM JSON, falls back to `StubPlanner` |
| `Backend/voice/tts.py` | 170 | `VoiceEngine` — edge-tts with voice auto-fallback (bn → en), mp3 cache, offline fallback |
| `Backend/tools/**` | ~3 000 | Auto-discovered tool functions (`@register_tool`) |
| `Backend/config.py` | 90 | Env-driven config + the Bengali system persona prompt |

**Tool registry (`Backend/tools/registry.py`)** — decorator based, **loosely typed**: parameter schema is inferred from the function signature and collapses every annotation to `string|number|boolean`. No permission metadata, no risk level, no audit, `execute_tool()` catches all exceptions and returns `{"success": False, "error": …}`.

**Registered tools discovered at runtime in the probe environment (30):**
`adjust_brightness, adjust_volume, calculate_math, close_application, execute_autonomous_goal, execute_computer_skill, get_agent_metrics, get_brightness, get_system_status, get_volume, google_search, launch_application, lock_workstation, open_special_folder, recall_memory, remember_fact, search_web, see_screen, set_brightness, set_mute, set_volume, take_screenshot, web_quick_answer, web_search, wikipedia_search, youtube_clear_local_history, youtube_get_history, youtube_open_history_page, youtube_play_from_history, youtube_play_last`

**Duplicate tool names found:** `google_search` vs `search_web` vs `web_search`; `Backend/tools/system_*.py` (flat legacy files) vs `Backend/tools/system/*.py` (package) — the flat ones are legacy copies.

**Tools that failed to import in the probe (8):** every module that imports `pyautogui` (`tools/media/web_media.py`, `tools/media/youtube_*.py`, `tools/web/control.py`, `tools/web/reader.py`, `tools/youtube/*.py`). The auto-discovery loader swallows the `ImportError` and logs a warning — graceful, but it means **browser/YouTube control silently disappears on machines without the `gui` extra.**

---

## 3. `agent/` — the existing low-level computer-control foundation

This is the highest-quality code in the repository: `mypy --strict`, PEP 695 generics, discriminated unions, frozen Pydantic models, protocol seams, and a dependency rule that **only flows down**.

```
L0 agent/core/       ids (NewType + parse_*), enums, Result (Ok/Err), clock, events (8-kind union + BUS),
                     errors, metrics, tracing (correlation id + spans), health registry, retry, cancel,
                     circuit breaker, exhaustiveness (assert_never), logging
L1 agent/geometry/   PixelCoord, LogicalCoord, BoundingBox (half-open), Raster (sha256 content-addressed), MonitorInfo
L2 agent/perception/ ScreenElement, PerceptionQuery, PerceptionStrategy protocol,
                     strategies/{a11y, ocr, template, vision_llm}, cascade, cache, ocr_engine
L3 agent/world/      ScreenSnapshot, ScreenCapture (mss → PIL → gnome-screenshot/grim/scrot), differ, WorldModel, FakeCapture
L4 agent/motor/      ActionSpec (11-member discriminated union), MotorBackend protocol,
                     NullBackend (dry-run) / PyAutoGUIBackend, MotorController, ActionResult
L5 agent/safety/     GovernorConfig (+ for_level presets), SafetyGovernor, 6 composable invariants,
                     CapabilityGrant/mint_grant, Approver protocol (AutoApprove/AutoDeny/ConsoleApprover)
L6 agent/skills/     Skill[TIn] ABC, SkillContext protocol (gated act()), SkillRegistry, SkillResult,
                     builtins/{volume, brightness, screenshot, see, launch, youtube}
L7 agent/planning/   StateMachine (frozen transition table), PlanStep, Episode, Planner protocol,
                     StubPlanner (keyword + memory-driven defaults), LLMPlanner, ReActAgent,
                     EpisodicMemory (episodes.jsonl), ActionJournal
     agent/memory/   MemoryStore (SQLite `star_memory.db`, hashing-trick embeddings, cosine + token-boost recall,
                     preferred_int/preferred_str, learn_from_goal/learn_from_skill), embed, extract, sync (FolderSync)
     agent/facade.py ComputerControlAgent — async context manager, wires everything, registers health checks
     agent/cli.py    `agent` console script (--describe --health --dry-run --see --remember --recall --metrics --skill)
```

**Safety governor invariants (all pure `check` + deferred `commit`):** total budget, rate limit (trailing 60 s), wall clock, forbidden regions, keyword blocklist (`delete`, `format`, `rm -rf`, `sudo`, `mkfs`, `:(){`, `invoke-expression`, …), capability token (required at `PARANOID`).
Safety levels: `PERMISSIVE / NORMAL / STRICT / PARANOID` with numeric presets.

**Critical property to preserve:** `SkillContext.act()` **always** runs `governor.check → motor.execute → governor.log`. Nothing in `agent/` may bypass that path.

**ReAct loop already implements** `PERCEIVING → PLANNING → ACTING → VERIFYING → REFLECTING → (SUCCEEDED|FAILED|BLOCKED|ABORTED)` — i.e. the blueprint's `OBSERVE → ACT → VERIFY → RECOVER` at the *motor* level. STAR 2.0 must add the same loop at the *task* level (above skills), not re-implement it below.

---

## 4. APIs and events

**Public APIs today:** none over a network. The in-process surfaces are:

| Surface | Contract |
|---|---|
| `AssistantBridge.send_query(str)` | fire-and-forget; results arrive as Qt signals |
| `AgentBridge.run/quick/see/remember/health` | blocking wrappers over `asyncio.run_coroutine_threadsafe` (timeouts 15–120 s) |
| `ComputerControlAgent.run(goal) -> Episode` | async; full ReAct episode |
| `ComputerControlAgent.quick(skill, **params) -> SkillResult` | async; single skill |
| `SkillRegistry.run(SkillName, dict) -> SkillResult` | async |
| `route_command(text) -> {"actions": [...], "response": ...} \| None` | sync, pure-ish |
| `get_llm_provider().process_query(text, memory_context) -> dict` | sync, blocking network |

**Event bus:** `agent.core.events.BUS` (module-level singleton `EventBus`) with an 8-member discriminated union:
`action.started`, `action.completed`, `safety.blocked`, `state.changed`, `episode.started`, `episode.ended`, `skill.started`, `skill.completed`.
`subscribe(event_type, handler) -> unsubscribe`. Handlers are async; exceptions are logged, never propagated.
⚠️ There is a **second, incompatible** `EventBus` inside `Backend/autonomous_agent.py` (legacy).

**Persistence artefacts written at runtime (all gitignored):** `audit.jsonl`, `episodes.jsonl`, `star_memory.db` (+WAL/SHM), `Backend/cache/audio/*.mp3`, `see.png`, `capture.png`.

---

## 5. Run commands

```bash
# ── existing product ──────────────────────────────────────────────
pip install -r requirements.txt          # full desktop stack (PySide6, PyAudio, pygame, edge-tts, pyautogui, pycaw, mss, pytesseract, psutil)
python run.py                            # or: run.bat  (Windows)
pip install -e ".[dev]"                  # agent core + dev tools only (no GUI needed)
pytest                                   # 100 tests, no display required
agent --describe --dry-run               # list skills
agent --dry-run "set the volume"         # dry-run ReAct episode
agent --see --query Search               # OCR the screen (read-only)
agent --remember "..." --recall volume   # long-term memory
python -c "import Backend.tools; print(len(Backend.tools.get_all_tools()))"

# ── environment probe (what this audit actually ran) ───────────────
python -m pytest -q                      → 1 failed, 99 passed in 1.73s
python -c "import Backend.tools"         → 30 tools, 8 modules skipped (pyautogui missing)
python -c "import agent.skills.builtins" → skills: brightness, launch, screenshot, see, volume, youtube
```

`PySide6`, `pygame`, `speech_recognition`, `pyautogui`, `edge-tts` are **not installable/usable headless** in the audit sandbox, so `run.py` could not be executed here. It was verified by static inspection instead (import graph, entry function, signal wiring). **Phase 1 must keep `run.py` working unchanged on the developer's Windows machine.**

---

## 6. Environment variables (complete list found by grep)

| Variable | Default | Used by |
|---|---|---|
| `STAR_LOCAL_LLM_URL` | `http://localhost:11434/v1` | `Backend/config.py` → `LocalOllamaProvider` |
| `STAR_LOCAL_LLM_MODEL` | `star-local:latest` | idem |
| `STAR_USE_LOCAL_LLM` | `true` | provider selection |
| `GEMINI_API_KEY` | `""` | `GoogleGeminiProvider` (secret — never hard-coded ✔) |
| `STAR_USE_GEMINI` | `false` | provider selection |
| `STAR_GEMINI_MODEL` | `gemini-2.5-flash` | idem |
| `STAR_TTS_VOICE` | `bn-IN-BashkarNeural` | `Backend/voice/tts.py` |
| `STAR_TTS_RATE` | `+0%` | idem |
| `STAR_TTS_PITCH` | `+0Hz` | idem |
| `YOUTUBE_API_KEY` | — | `Backend/tools/youtube*/analytics.py` (secret ✔) |

All are read through `os.getenv` with defaults; **no secret is hard-coded anywhere** — confirmed by grep for `sk-`, `AIza`, `api_key =`, `token =`.

---

## 7. Tests and baseline result

Configuration (`pyproject.toml`): `asyncio_mode = "auto"`, `testpaths = ["tests"]`, `filterwarnings = ["error"]`, `addopts = "-q --strict-markers --strict-config"`, coverage `source = ["agent"]`.

**Baseline command:** `python -m pytest`
**Baseline result:** `1 failed, 99 passed in 1.73s` (Python 3.13.14, pytest 9.0.3, pytest-asyncio + hypothesis installed for the run).

| File | Focus |
|---|---|
| `test_geometry.py` | coords, half-open bbox, raster hashing/crop/diff |
| `test_ids.py` | NewType parsers, prefix + hex validation |
| `test_result.py` | `Ok/Err` map/and_then/collect |
| `test_fsm.py` | transition table completeness, illegal transitions |
| `test_motor.py` | spec union, NullBackend recording |
| `test_governor.py` | all 6 invariants, commit-after-check |
| `test_circuit_breaker.py` | closed/open/half-open single trial |
| `test_retry_cancel.py` | jittered backoff, deadline cancellation |
| `test_cache.py` | perception cache TTL |
| `test_perception.py`, `test_see.py` | strategies, cascade, OCR skill with `FakeCapture` |
| `test_skills.py` | registry, param validation, gated `act()` |
| `test_planner.py`, `test_react.py` | StubPlanner, episode lifecycle |
| `test_memory.py` | SQLite store, dedupe, preferences |
| `test_events.py` | bus subscribe/unsubscribe |
| `test_facade.py` | `ComputerControlAgent` end-to-end (dry-run) |
| `test_hypothesis.py` | property-based geometry/id invariants |
| `test_command_router.py` | Bengali/Banglish routing (198 lines) |
| `test_false_positives.py` (repo root, **outside `testpaths`**) | router false-positive guards — not collected by default |

**The single failure (pre-existing, NOT caused by this work):**

```
tests/test_command_router.py::test_google_search
  route_command("google e kobita search koro")
  → actions[0]["tool"] == "web_search"        (actual)
  → test asserts           "search_web"       (expected)
```

Root cause: the router was renamed `search_web → web_search` (both names are still registered as tools, plus a third alias `google_search`) and the test was never updated. It is a **naming/duplication defect**, not a behavioural regression. Recorded here; scheduled for the Phase 12 regression pass (alias consolidation + test correction), not fixed in Phase 0 because Phase 0 is audit-only.

---

## 8. Reusable modules (build STAR 2.0 *on* these)

| STAR 2.0 need | Reuse this | Why |
|---|---|---|
| Computer control (mouse/keyboard/screen) | `agent.facade.ComputerControlAgent`, `agent/motor`, `agent/world`, `agent/perception` | Already dry-run capable, governor-gated, `FakeCapture`-testable |
| Safety layer for OS actions | `agent/safety/*` (governor, invariants, capability, approver) | Pure `check`/`commit`, level presets, audit dump |
| Skill execution + registry | `agent/skills/*` | Typed params, metrics, events, `SkillContext.act()` gate |
| Task FSM + episode record | `agent/planning/state_machine.py`, `episode.py`, `step.py`, `react_agent.py` | Frozen transition table = the OBSERVE→ACT→VERIFY→RECOVER spine |
| Long-term memory (SQLite) | `agent/memory/store.py` (+ `embed`, `extract`, `sync`) | Local-first, dedupe fixed, `preferred_*` API, hashing embeddings, optional sentence-transformers seam |
| Episodic memory | `agent/planning/memory.py` | JSONL episodes + `recall_similar` |
| Thread bridging into Qt | `Backend/agent_bridge.py` | Daemon asyncio loop + `run_coroutine_threadsafe`, already battle-tested |
| Bengali/Banglish NLU | `Backend/nlu/command_router.py` | 1257 lines of real intent routing incl. Bengali digits |
| LLM providers | `Backend/llm/provider.py`, `Backend/llm/agent_planner.py` | Ollama + Gemini + offline engine, `Planner` protocol adapter |
| TTS | `Backend/voice/tts.py` | edge-tts bn/en voices, mp3 cache, fallback chain |
| STT | `Backend/bridge.py::VoiceListener` | Dual bn-IN/en-IN recognition + `_prefer_english` heuristic |
| Web/Youtube/system tools | `Backend/tools/**` (30 registered) | Wrap, don't rewrite |
| Observability primitives | `agent/core/{metrics,tracing,health,logging,events}` | Correlation ids, spans, health registry |
| Retry/cancel/breaker | `agent/core/{retry,cancel,circuit_breaker}` | Needed by the orchestrator |
| Training data + persona | `data/*.jsonl`, `Backend/config.py::SYSTEM_PERSONA_PROMPT`, `Modelfile` | Voice/persona continuity |

---

## 9. Risky / legacy modules

| Module | Risk | Decision |
|---|---|---|
| `Backend/autonomous_agent.py` (2037 lines) | **Duplicate** of the whole `agent/` layer with its own enums, EventBus, governor, metrics. Two sources of truth; drift already visible. | ⛔ Do **not** import from STAR 2.0. Do **not** delete (it may still be referenced by user scripts). Quarantine: document as legacy, add a deprecation note in Phase 12. |
| `Backend/tools/system_brightness.py`, `system_volume.py`, `system_info.py`, `youtube_*.py` (flat files) | Legacy flat duplicates of the `system/` and `youtube/` packages. | ⛔ Do not extend; wrap the package versions only. |
| `Backend/tools/registry.py` | Loose typing, no permissions/risk/audit, schema inference loses types. | ♻️ Keep as the *legacy* registry; STAR 2.0 gets a new typed `ToolRegistry` that **adapts** legacy tools instead of replacing them. |
| `Backend/llm/knowledge.py` (1683 lines) | Huge offline table; overlaps `command_router`. | ♻️ Read-only reuse via `OfflineIntentEngine`. |
| `Backend/bridge.py::VoiceListener` | Blocking thread + network STT; Qt-coupled. | ♻️ Wrap behind an `STTProvider` protocol; keep the Qt class untouched. |
| Any module importing `pyautogui`/`pycaw` at import time | Hard-crashes tool discovery on non-Windows / headless. | ♻️ STAR 2.0 must import tools **lazily** and degrade to dry-run. |
| `test_false_positives.py` at repo root | Outside `testpaths`, silently unrun. | ♻️ Move/include in Phase 12 (do not delete). |

---

## 10. Files that MUST NOT be rewritten

**Frozen — visual identity and working entry points:**

```
Frontend/main.py            Frontend/reactor.py         Frontend/panels.py
Frontend/content.py         Frontend/result_panel.py    Frontend/tokens.py
Frontend/README.md          Frontend/requirements.txt
run.py                      run.bat
agent/**                    (all L0–L7 layers — extend via protocols, never fork)
Backend/bridge.py           (additive hooks only)
Backend/brain.py            Backend/agent_bridge.py     Backend/config.py
Backend/voice/tts.py        Backend/nlu/command_router.py
Backend/llm/**              Backend/tools/**
data/*.jsonl                Modelfile                   pyproject.toml (additive only)
ANALYSIS_AND_FIXES.md       ASSISTANT_TRAINING_GUIDE.md README.md  LICENSE
```

Rule applied throughout Phase 1+: **new capability = new file.** Existing files receive only additive, feature-flagged hooks (`STAR_GATEWAY_URL` unset ⇒ byte-identical behaviour).

---

## 11. Proposed integration points

1. **`Backend/star/` — new package** (the STAR 2.0 modular monolith). Nothing else moves.
2. **Gateway seam** — a stdlib `asyncio` HTTP + WebSocket server (`Backend/star/gateway/`) exposes `POST /api/v1/chat`, task/plan/memory/audit/tools endpoints and `WS /ws` for the `star.mission.v1`-style event stream the frontend README already anticipates.
3. **Frontend seam** — `Backend/star/gateway/frontend_adapter.py` (thin, optional, Qt-free client). It attaches **from the outside** to the signals `AssistantBridge` already emits (`log_emitted`, `response_ready`, `state_changed`, `speaking_started/finished`) and mirrors them to the gateway only when `STAR_GATEWAY_URL` is set. Nothing in `Frontend/`, `Backend/bridge.py` or `run.py` is modified — `git diff main` for those paths is empty, and a test enforces it.
4. **Brain seam** — `Backend/star/brain/` defines `UserRequest / Context / Plan / Task / ToolCall`; it calls `get_llm_provider()` and `route_command()` rather than re-implementing them.
5. **Tool seam** — `Backend/star/tools/registry.py` adapts (a) `agent` skills via `ComputerControlAgent.quick`, (b) legacy `Backend.tools` functions via `execute_tool`, into one typed, permission-annotated registry.
6. **Safety seam** — ✅ built: every STAR 2.0 action is classified `LOW/MEDIUM/HIGH/CRITICAL` by `Backend/star/security/policy.py`; motor actions additionally pass `agent`'s `SafetyGovernor` (unchanged) — see `Backend/star/computer/guardrails.py`.
7. **Memory seam** — ✅ built: `Backend/star/memory/` layers working / episodic / semantic / preferences / procedural patterns **over** `agent.memory.store.MemoryStore` + `agent.planning.memory.EpisodicMemory` + `brain.prediction.PatternStore` + the brain's session scratch. No second SQLite store; `MemoryManager` implements the Phase 3 `MemoryRetriever` protocol, so retrieval still happens **before** planning — see `PHASE_8_REPORT.md`.
8. **Workspace seam** — ✅ built: `Backend/star/workspace/` gives every background session a jailed `data/workspace/<session-id>/` directory (realpath-checked, quota-bound, TTL-expired, checkpointable) + per-session browser profile isolation; dry-run is the default.
9. **Observability seam** — subscribe to `agent.core.events.BUS` and re-publish as gateway events; JSONL audit under `logs/`.

---

## 12. Phase 0 verification checklist

- [x] Repository cloned/opened (`/home/user/STAR`, branch `star-2.0`)
- [x] `Frontend/` **not** modified (git status clean for `Frontend/`)
- [x] Every top-level directory and key file inspected
- [x] Exact frontend entry point identified (`run.py` → `Frontend/main.py::main`)
- [x] Backend entry point identified (`Backend/bridge.py` → `Backend/brain.py`)
- [x] Computer-control modules mapped (`agent/` L0–L7 + `Backend/agent_bridge.py`)
- [x] Safety, planning, memory modules mapped
- [x] Current tests run → `1 failed, 99 passed`
- [x] Current application run attempted → blocked headless (PySide6/pygame/pyautogui unavailable); documented
- [x] `docs/STAR_CURRENT_STATE.md` created (this file)
- [x] `docs/STAR_2_0_ARCHITECTURE.md` created
- [x] Reusable vs duplicate/conflicting components listed
- [x] Untouchable files listed
- [x] Exact Phase 1 plan given (see `docs/STAR_2_0_ARCHITECTURE.md` §9)

---

## 13. Build status (this section is updated as phases land — the inventory above stays a Phase 0 snapshot)

| Phase | Deliverable | Report | Tag |
|---|---|---|---|
| 0 | Baseline + inventory (this file, `STAR_2_0_ARCHITECTURE.md`) | — | `star-2.0-phase-0` |
| 1 | Gateway + zero-touch frontend adapter | `PHASE_1_REPORT.md` | `star-2.0-phase-1` |
| 2 | Voice: STT/TTS providers, bn/en routing, barge-in | `PHASE_2_REPORT.md` | `star-2.0-phase-2` |
| 3 | Brain: typed schemas, memory-first context, prediction/reflection | `PHASE_3_REPORT.md` | `star-2.0-phase-3` |
| 4 | Tools: typed registry, permission ladder, confirmations, audit, dry-run | `PHASE_4_REPORT.md` | `star-2.0-phase-4` |
| 5 | Browser agent: URL guardrails, bounded fetch/extract, 8 typed tools | `PHASE_5_REPORT.md` | `star-2.0-phase-5` |
| 6 | Computer agent: guardrailed screen/mouse/keyboard on the **reused** `agent/motor` + `SafetyGovernor`, dry-run first | `PHASE_6_REPORT.md` | `star-2.0-phase-6` |
| 7 | Invisible workspace: jailed sessions, quotas, TTL, checkpoints, isolation seam, 9 typed tools | `PHASE_7_REPORT.md` | `star-2.0-phase-7` |
| 8 | Layered memory: five layers over the existing stores, hybrid weighted retrieval, 7 typed tools | `PHASE_8_REPORT.md` | `star-2.0-phase-8` |
| 9 | Safe learning loop: inert `{pattern, preference}` candidates, six kind-aware gates, **no self-modification**, promotes into the borrowed stores, 4 typed tools | `PHASE_9_REPORT.md` | `star-2.0-phase-9` |
| 10 | Advanced orchestration: multi-agent routing (conversation, browser, computer, tools), JSONL checkpoints (pause/resume/rollback), bounded honest recovery (RECOVERED state), graceful task cancellation, 6 typed tools | `PHASE_10_REPORT.md` | `star-2.0-phase-10` |
| 11–12 | Security hardening · optimisation/packaging | pending | — |

Totals after Phase 10: **74 tools** (68 + 6 orchestration) · **16 live capabilities** (orchestrator live) · **38 routes** · test suite **759 collected / 758 passed** (the 1 failure is the pre-existing baseline
`test_command_router.py::test_google_search`, scheduled for Phase 12) · smoke **71/71**.

Standing invariants verified at every phase: `git diff main -- Frontend/ Backend/bridge.py run.py run.bat agent/` is
**empty**; dry-run is the default; no secret is hard-coded; the legacy `agent/` physical layer is reused, never forked.
