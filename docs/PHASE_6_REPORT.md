# STAR 2.0 — PHASE 6 REPORT
### Computer agent: guardrailed screen/mouse/keyboard · reused motor & safety governor · dry-run first

*Branch `star-2.0` · base `b48abe5` (frontend zero-touch fix) · blueprint §7 Phase 6 (+ §4 agents-vs-tools)*

> **Phase 6 requirements:** "Add a computer agent for screen capture, mouse and keyboard control.
> Every physical action must be validated by the safety layer. Dry-run first. Never click outside
> known bounds, never type destructive commands, respect forbidden regions."
>
> **Blueprint §2 (non-negotiables):** *"reuse the existing `agent/` computer-control, safety and
> planning code — do not build duplicate subsystems."*

---

## 1. What was built (5 new modules, 1 301 lines + 512 lines of tests)

| Module | Lines | Responsibility |
| --- | --- | --- |
| `Backend/star/computer/guardrails.py` | 280 | `ComputerGuardrails.check()` → `ActionVerdict(ok, rule, reason, risk)`; async `authorise()` adds the **reused** `SafetyGovernor`; refusal ring buffer for the UI |
| `Backend/star/computer/motor.py` | 167 | `build_motor()` → `MotorStack` (`NullBackend` vs `PyAutoGUIBackend`), `describe_action()` audit helper |
| `Backend/star/computer/tools.py` | 344 | 10 typed `ToolSpec`s + `ComputerToolkit` (`act` / `preview` / `perceive`) |
| `Backend/star/computer/agent.py` | 454 | `ComputerAgent` — goal → plan → ACT (pre-guarded) → OBSERVE → VERIFY → RECOVER; `build_computer()` |
| `Backend/star/computer/__init__.py` | 56 | lazy PEP 562 exports — importing the package never pulls in `pyautogui` |

`tests/test_star2_computer.py` — **48 tests**, hermetic: `NullBackend` + `FakeCapture`, no display,
no `pyautogui`, no network. Gateway surface covered by 5 more tests in `tests/test_star2_gateway.py`.

**Nothing was duplicated.** The physical layer is the repository's existing
"Computer Control Agent v6.0" core; STAR 2.0 adds the *policy and protocol surface* around it:

| Existing module (reused as-is) | Used for |
| --- | --- |
| `agent/motor/spec.py` — `spec_from_kind(kind, params)`, frozen pydantic specs, `PixelCoord` ≤ 65 535 | typed action objects; out-of-range coordinates raise at spec build time |
| `agent/motor/controller.py` — `MotorController.execute(spec)` with retry + pixel verify | ACT→VERIFY mechanics |
| `agent/motor/backends/` — `NullBackend` (records `.executed`), `PyAutoGUIBackend` (lazy import) | the two motor modes |
| `agent/safety/governor.py` + `agent/safety/config.py` — `SafetyGovernor`, `GovernorConfig.for_level` | the **six invariants**: `total_budget`, `rate_limit`, `wall_clock`, `forbidden_region`, `keyword_blocklist`, `capability` |
| `agent/world/` — `WorldModel`, `FakeCapture`, `ScreenDiffer`, `ScreenCapture` | OBSERVE + change detection |
| `agent/geometry/` — `BoundingBox`, `MonitorInfo`, `raster.solid()` | forbidden regions, fake screen |
| `agent/core/enums.py` — `ActionKind`, `SafetyLevel` | the shared action vocabulary |
| `Backend/star/voice/language.py` — `normalize_text()` | Bengali digits → ASCII, wake-word strip (trilingual goals) |
| Phase 4 `ToolRegistry` / `PermissionEngine` / `ToolExecutor` / `AuditLog` / `ConfirmationStore` | validation, permissions, confirmation, audit, dry-run — inherited, not re-implemented |

---

## 2. Guardrails — checked *before* the motor ever sees an action

`ComputerGuardrails.check(spec)` is synchronous, total (never raises) and runs in this order:

| # | Rule | Refusal (`rule`) | Risk | Example |
| --- | --- | --- | --- | --- |
| 1 | coordinate inside the configured screen | `bounds` | high | `click at 9000,9000` on a 1920×1080 screen |
| 2 | coordinate outside every forbidden region | `forbidden_region` | critical | clicking inside `"100,100,200,120"` |
| 3 | typed text is not a destructive command | `destructive_text` | critical | `type 'sudo rm -rf /'`, `format c:`, `shutdown -h now` |
| 4 | typed text within `max_text_chars` | `text_length` | medium | 2 001 characters |
| 5 | hotkey not on the blocked list | `blocked_hotkey` | critical | `ctrl+alt+del`, `ctrl+alt+backspace`, `alt+sysrq` |
| 6 | scroll amount within `max_scroll` | `scroll` | medium | `scroll 99` with cap 20 |
| 7 | wait within `max_wait_s` | `wait` | medium | `wait 300s` with cap 30 |
| 8 | **`SafetyGovernor.check()`** (async, via `authorise()`) | `governor` (+ `governor_rule`) | high | session action budget exhausted |

Rule 8 is the important one: the existing governor's six invariants are wired to STAR 2.0 settings
(`STAR_SAFETY_LEVEL`, `STAR_MAX_ACTIONS_SESSION`, `STAR_MAX_ACTIONS_MINUTE`, forbidden regions,
dry-run) through `GovernorConfig.for_level(...)`. A refusal therefore reports the *legacy* rule name
honestly (`BudgetExhausted`, `ForbiddenRegion`, …) instead of a new vocabulary.

Every refusal emits the existing `safety.blocked` event (`layer="computer_guardrails"`) and is kept
in an 8-entry ring buffer that `GET /api/v1/computer` returns as `guardrails.refusals` — the console
shows what was stopped and why, which is how a human learns to trust the layer.

---

## 3. Motor stack — dry-run means *nothing moves*, and it says so

```
build_motor(settings)  →  MotorStack(mode, backend, world, controller, dry_run, note)
```

| `STAR_COMPUTER_BACKEND` | dry-run | Resulting mode |
| --- | --- | --- |
| `auto` (default) | **on** | `null` — `NullBackend` + `FakeCapture(solid raster)`; every action is recorded, nothing moves |
| `auto` | off | `live` — `PyAutoGUIBackend`, imported lazily; falls back to `null` with an honest note if it is missing |
| `null` | either | `null` — explicit recording-only mode |
| `pyautogui` | off | `live`, or `null` + `"pyautogui unavailable"` when the import fails |

Two details that keep the loop honest:

* In `null` mode the fake screen never changes, so a `verify=True` spec would burn three retries and
  log misleading warnings. `MotorStack.execute()` therefore rewrites the spec with
  `model_copy(update={"verify": False})` — specs are frozen, so this is the only correct way.
* `describe_action(spec)` produces the JSON-safe audit view (`kind` uses `ActionKind.value`, never
  `str(enum)` — Python 3.13 renders that as `"ActionKind.CLICK"`).

`MotorStack.describe()` reports `mode`, `backend`, `dry_run`, the human-readable `note`, and
`actions / succeeded / failed` counters. This sandbox has no display and no `pyautogui`, so every
verification in this report ran in `null` mode — that is the honest state, and it is visible in the API.

---

## 4. Tools — 10 typed specs, no private execution path

Registered by `register_computer_tools(registry, settings, bus=)` into the Phase 4 registry, so they
inherit parameter validation, the permission ladder, confirmation, audit and dry-run simulation.

| Tool | Risk | What it does |
| --- | --- | --- |
| `screen_capture` | low | capture the screen (perception only — never moves anything) |
| `screen_read` | low | read what is on screen (delegates to the legacy OCR `see_screen` skill) |
| `mouse_move` | low | move the pointer to a coordinate |
| `scroll` | low | scroll the wheel, optionally at a coordinate, capped per call |
| `wait` | low | bounded wait (user `seconds` → spec `duration`, clamped to `max_wait_s`) |
| `mouse_click` | medium | click at a coordinate (left/right/middle, 1–3 clicks) |
| `key_press` | medium | one key (`enter`, `tab`, `esc`, …) |
| `keyboard_type` | medium | type text into the focused field |
| `hotkey` | **high** | key combination — trips Phase 4's confirmation gate above `high` |
| `mouse_drag` | **high** | drag from the current pointer position to a coordinate |

All ten are `dry_run_safe=True`: in dry-run the executor *simulates* them and the intended action is
written to the audit log with its arguments, instead of the tool refusing to exist. Handlers return
one normalised shape (`success`, `error`, `result{verdict, action, via}`) and catch pydantic
`ValidationError` from `spec_from_kind` — an impossible coordinate becomes an honest refusal
(`"invalid arguments: …"`), never a crash.

`ComputerToolkit.preview(kind, params)` runs the guardrails **without** executing, which is what lets
the agent refuse a bad plan while still in dry-run. `perceive()` delegates to the legacy desktop
vision tools and reports `"needs the desktop vision tools — not available here"` when they are absent.

Because governor checks are async while tool handlers are sync (they run in a worker thread),
`_run_async()` runs them with `asyncio.run()` in a worker thread and falls back to a
`ThreadPoolExecutor` when a loop is already running (the legacy-gate path).

---

## 5. The agent — OBSERVE → ACT → VERIFY → RECOVER

`ComputerAgent(StarAgent)` implements the blueprint's loop:

* **PLAN** (`plan(goal)`) — trilingual. Goals in English, Banglish and Bangla all parse, and Bengali
  digits are normalised first (`৪৮০,৩২০ এ ক্লিক করো` → `click at 480,320`). Intent detection covers
  capture, read, move, click (button + click count), drag, type (quoted text), press, hotkey, scroll
  (direction + amount) and wait. Perception steps come first ("know the screen before touching it"),
  keyboard intents are emitted **in the order the human wrote them**, and any motor intent that did
  not already ask for a screenshot gets an optional trailing `capture` — VERIFY needs eyes. The plan
  is honest and complete; truncation to `max_steps` happens in `run()`, so a budget cut is reported as
  `BUDGET_EXHAUSTED`, not `DONE`.
* **ACT** — `toolkit.preview()` runs the guardrails *before* the executor, so a dry-run refuses a
  dangerous action rather than pretending to simulate it. The executor then applies the full Phase 4
  ladder (stop gate → allow/deny → risk → rate limits → confirmation → dry-run).
* **OBSERVE / VERIFY** — the observation is merged with the plan's arguments so the verification note
  is specific: `simulated in dry-run — recorded the intended click at 480,320; nothing moved`.
* **RECOVER** — bounded at 2 per run, and deliberately conservative: a missed click gets exactly one
  careful retry (capture → reposition → click again); policy refusals, missing desktop capability and
  pending confirmations are **never** retried ("retrying would be wrong"); optional steps are skipped.
* **SUMMARISE** — a plain-language line naming every action taken, plus `result.actions` (the
  `describe_action` audit views) and `result.motor` (mode/backend/counters).

### New run state: `waiting_confirmation`

A high-risk action (`hotkey`, `mouse_drag`) parks the run instead of failing it:

```
POST /api/v1/computer {"goal":"hotkey alt+tab","dry_run":false}
→ {"ok":false,"state":"waiting_confirmation","confirmation_id":"conf_26c8…",
   "summary":"… approve conf_26c8… to continue"}
POST /api/v1/confirmations {"confirmation_id":"conf_26c8…","approve":true}
→ {"ok":true,"state":"approved","execution":{"decision":"executed","success":true}}
GET  /api/v1/computer → motor.actions = 1     # the null motor recorded it
```

`AgentRun.confirmation_id` is now populated on **every** exit path (inside `_finish`), so a caller
always knows what to approve; `AgentRunState.WAITING_CONFIRMATION` was added to the terminal set.

---

## 6. Surface: gateway + console (Frontend/ untouched)

| Route | Purpose |
| --- | --- |
| `GET /api/v1/computer` | motor mode/backend/counters, guardrail config + stats + refusals, agent description, settings, `dry_run`, `last_run`, `runs` |
| `POST /api/v1/computer` | `{"goal": …, "dry_run": bool?, "session_id": …}` → one run; `422` on an empty goal; `{"stopped":true}` under emergency stop |

The route table is now **26 routes** (additive only). The HTML console gained a **Computer** tab
(inline CSS/JS, no CDN): motor + guardrails + the six invariants, the refusal log, a goal input with
a `dry-run` checkbox that is **checked by default**, and a `window.confirm()` gate before any real
run. A parked run shows its `confirmation_id` and points at the Approvals tab.

`app.computer_state()` / `app.run_computer_goal()` were added to `StarApplication` and to
`contracts.py`, the computer slot joins the lifecycle (`startup`/`aclose`/health), and
`build_application()` wires `build_computer(cfg, bus=, executor=, registry=, stop_gate=)` plus
registers it in the `AgentRegistry` alongside the browser worker.

---

## 7. Configuration (`STAR_COMPUTER_*`, all optional)

```
STAR_COMPUTER_ENABLED=true
STAR_COMPUTER_BACKEND=auto          # auto | null | pyautogui
STAR_COMPUTER_SCREEN_WIDTH=1920     # bounds every coordinate is checked against
STAR_COMPUTER_SCREEN_HEIGHT=1080
STAR_COMPUTER_FORBIDDEN_REGIONS=    # "x,y,w,h" comma separated
STAR_COMPUTER_BLOCKED_HOTKEYS=ctrl+alt+del,ctrl+alt+backspace,alt+sysrq
STAR_COMPUTER_MAX_TEXT_CHARS=2000
STAR_COMPUTER_MAX_SCROLL=20
STAR_COMPUTER_MAX_WAIT_S=30
STAR_COMPUTER_MAX_STEPS=8           # step budget per goal
STAR_COMPUTER_STEP_TIMEOUT_S=20
STAR_COMPUTER_VERIFY_DELAY_S=0.4
STAR_COMPUTER_CHANGE_THRESHOLD=0.02
```

No secrets anywhere; `.env.example` documents every key with its safety consequence.

---

## 8. Verification

| Check | Result |
| --- | --- |
| `tests/test_star2_computer.py` | **48 passed** — guardrails 8 · motor stack 5 · tools 8 · agent 26 (14 of them the parametrised trilingual planner) · app wiring 1 |
| `tests/test_star2_gateway.py` | 5 new computer-endpoint tests pass; route count assertion updated 24 → 26 |
| Full suite (`--ignore=tests/test_hypothesis.py`) | **452 collected · 451 passed · 1 failed** — the failure is the pre-existing `tests/test_command_router.py::test_google_search` (network-dependent, scheduled for Phase 12) |
| `scripts/smoke.py --phase 6` | **29 passed, 0 failed** (was 22; +7 computer checks) |
| `git diff main -- Frontend/ Backend/bridge.py run.py run.bat agent/` | **empty** — the frontend and the legacy physical layer are byte-identical |

Live behaviour observed through the gateway in this sandbox (`null` motor, dry-run default):

```
click at 480,320            → dry_run   [move, click, capture] all simulated, motor.actions = 0
click at 4000,3000          → blocked   bounds: outside the known screen 1920x1080
type 'sudo rm -rf / now'    → blocked   destructive_text
hotkey ctrl+alt+del         → blocked   blocked_hotkey
hotkey alt+tab (dry_run off)→ waiting_confirmation → approve → executed, motor.actions = 1
scroll down 5               → dry_run   [scroll, capture]
৪৮০,৩২০ এ ক্লিক করো            → dry_run   [move(480,320), click, capture]   # Bengali digits
click at 100,100 (dry_run off) → done   motor recorded both actions, verify noted the null backend
```

---

## 9. Honest limitations

1. **No hardware verification in this environment.** There is no display and `pyautogui` is not
   installed, so the `live` path (real mouse/keyboard) is exercised only through the `NullBackend`
   contract. The code paths are the existing, already-tested `agent/motor` ones; the switch is one
   `MotorStack.mode` value.
2. **Perception depends on legacy desktop tooling.** `screen_capture` / `screen_read` delegate to
   `take_screenshot` / `see_screen`; without them they report unavailability instead of inventing a
   screenshot. VERIFY therefore degrades to "the motor reported success" in a headless run, and the
   verification note says exactly that.
3. **Planning is rule-based, not vision-driven.** The agent does not yet decide *where* to click from
   a screenshot (no UI-element grounding); it acts on coordinates/keys the user or a future planner
   supplies. Adding element grounding is a natural Phase 10 orchestration concern.
4. **`max_scroll` / `max_wait_s` are per-action caps**, not per-run budgets; the per-run budget is the
   governor's `total_budget` + `max_steps`, which is the deliberate division of responsibility.

---

## 10. Commit

```
feat(star-2.0): phase 6 — computer agent (screen/mouse/keyboard) on the reused motor + safety governor
tag: star-2.0-phase-6
```

Next: **Phase 7** — the invisible, isolated background workspace (`Backend/star/workspace/`),
so long-running automation never writes into the user's visible project tree.
