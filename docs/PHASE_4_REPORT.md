# STAR 2.0 — PHASE 4 REPORT
### Tools: typed registry · risk tiers · permission policy · confirmations · append-only audit · legacy gate

*Branch `star-2.0` · base `star-2.0-phase-3` · blueprint §7 Phase 4 (+ §11 security groundwork)*

> **Phase 4 requirements:** "Create a typed tool registry with metadata (name, description, risk,
> required permission, dry-run behaviour). Add a permission/policy layer. Every tool call goes
> through: validation → permission check → confirmation check → execution → audit log. Add an
> append-only audit log with redaction. Dry-run is the default for anything that touches the OS."

---

## 1. What was built (10 new modules, 2 695 lines + 1 203 lines of tests)

| Module | Lines | Responsibility |
| --- | --- | --- |
| `Backend/star/tools/risk.py` | 127 | `RiskTier` (low→critical), `classify_risk(tool, arguments)` — name **and argument keys** only, never free-text values |
| `Backend/star/tools/spec.py` | 488 | `ToolSpec`, `ToolResult`, `spec_from_function()` (PEP 563-safe), `validate_arguments()`, `RiskTier` coercion |
| `Backend/star/tools/registry.py` | 234 | `StarToolRegistry`: register / decorator / **import the 30 legacy tools** / `list` / `schemas` / `risk_of` / `by_category` / `summary` / `health` |
| `Backend/star/tools/permissions.py` | 354 | `PermissionEngine` — the 7-step decision ladder, recursive shell-payload scan, rate limits, stop gate |
| `Backend/star/tools/executor.py` | 553 | `ToolExecutor` — the single execution path, thread + timeout, plan runner, `execute_confirmed` |
| `Backend/star/tools/legacy_gate.py` | 189 | `LegacyToolGate` — patches the legacy `execute_tool()` funnel so old code cannot bypass policy |
| `Backend/star/tools/__init__.py` | 82 | lazy PEP 562 exports (importing `star.tools` never drags in pyautogui) |
| `Backend/star/security/audit.py` | 180 | `AuditLog` + `JsonlSink` — append-only JSONL, ring buffer, query/tail/read_file, never raises |
| `Backend/star/security/confirmations.py` | 288 | `Confirmation` / `ConfirmationStore` — fingerprints, TTL, states, `mark_approved`, callbacks |
| `Backend/star/security/surface.py` | 160 | `SecuritySurface` — one object the gateway talks to: startup, mute/unmute, resolve, audit queries, health |

`build_tool_registry()`, `build_tool_executor()`, `build_security()` are the factories `main.py` uses.

---

## 2. The execution path — one call, no side doors

```
ToolCall
  └─ registry.get(tool)            unknown → decision "unknown_tool"      (stats.unknown_tool += 1)
  └─ validate_arguments(spec, args) invalid → decision "invalid_args"     (refused BEFORE policy)
  └─ PermissionEngine.check(spec, args)
        0. emergency stop      → deny  "emergency stop is active"
        1. allow/deny lists    → allow / deny
        2. risk tier ceiling   → ≥ deny_risk → deny · ≥ confirm_above_risk → needs_confirmation
        3. shell policy        → deny unless STAR_ALLOW_SHELL=true AND no shell metacharacters
                                 anywhere in the payload (recursive scan, any depth)
        4. rate limits         → per session / per minute → deny
        5. confirmation gate   → pending+approved? reuse : create confirmation → needs_confirmation
        6. dry-run             → simulate (the handler is NEVER called)
  └─ handler in a worker thread with timeout_s
        timeout → "timeout" · exception → "error" · odd return shapes → normalised
  └─ AuditLog.record(...)      every branch writes exactly one entry (redacted, fingerprinted)
  └─ bus.emit("tool.decision") → the HUD / ops console / mission stream see it
  └─ ToolResult.as_call_result() → legacy-compatible dict for the existing router + HUD
```

`ToolResult.decision` ∈ `executed | simulated | denied | needs_confirmation | blocked |
invalid_args | unknown_tool | timeout | error` — the *reason* is always machine-readable, never
inferred from an exception string.

---

## 3. Risk model

Tiers are ordered `low < medium < high < critical`. Defaults: `confirm_above_risk=high`,
`deny_risk=critical`. The 30 imported legacy tools classify as **14 low / 13 medium / 3 high**
(`execute_autonomous_goal`, `execute_computer_skill`, `lock_workstation`), highest = high, none
critical by default.

Two rules that cost a debugging session and are now pinned by tests:

* `classify_risk()` inspects the tool **name** and the argument **keys** only. Free-text values are
  never risk input — otherwise `{"note": "delete everything"}` would escalate a harmless read.
* Argument keys can raise a spec's tier: a `low` spec called with `set_volume`/`brightness`-style
  keys is treated as `medium`. The spec tier is a floor, not a ceiling.

---

## 4. Permissions engine

* **Lists:** `STAR_TOOL_ALLOWLIST` (empty = everything registered), `STAR_TOOL_DENYLIST` — deny wins.
* **Shell:** `STAR_ALLOW_SHELL=false` by default. Even when enabled, `_find_shell_payload()` walks
  the arguments depth-first looking for shell metacharacters under shell-ish keys (`command`, `cmd`,
  `script`, `shell`, …), so `{"steps": [{"cmd": "rm -rf ~"}]}` is denied — nesting does not help.
* **Rate limits:** `STAR_MAX_ACTIONS_SESSION=200`, `STAR_MAX_ACTIONS_MINUTE=60`.
* **Unsimulatable tools:** a spec with `dry_run_safe=False` is not faked in dry-run; it is refused
  with a clear reason instead of pretending to succeed.
* **Stop gate:** `PermissionEngine.bind_stop_gate()` — `SecuritySurface` binds it to its own
  `stopped` flag for both self-built *and* injected engines, so `/api/v1/stop` mutes tools too.

---

## 5. Confirmations (blueprint #7: explicit approval for sensitive actions)

* Identity = `fingerprint(tool, arguments)` — a stable, order-insensitive hash. Same call again
  inside `STAR_CONFIRMATION_TTL_S=120` reuses the approval; different arguments ⇒ new prompt.
* States: `pending → approved | denied | expired`. A denial is **not** an approval and is not
  reusable; expiry is checked on read *and* on reuse.
* `mark_approved()` flips the store **without** firing `on_resolve` — that is what makes
  `execute_confirmed()` loop-free: approve → mark → re-run the call → policy now sees an approved
  confirmation instead of asking again forever.
* An explicit human "yes" runs for real (`dry_run=False` in `execute_confirmed`); automation without
  a human stays simulated. That asymmetry is deliberate and documented here.

Live loop (real run, this sandbox):

```
call lock_workstation {}      → decision=needs_confirmation, confirmation_id=conf_45990e5e314d
app.confirmations()           → [{tool: lock_workstation, risk: high, state: pending, …}]
resolve_confirmation(approve) → ok=True, state=approved, execution.decision=executed
call lock_workstation {} again→ decision=simulated   (approval reused inside TTL)
audit                         → tool.decision: needs_confirmation, executed, simulated, simulated
```

---

## 6. Audit log — append-only, redacted, and it never breaks the agent

`STAR_AUDIT_PATH=logs/audit.jsonl`. One JSON object per line:

```json
{"ts":"2026-09-23T08:39:58.923+00:00","seq":1,"kind":"tool.decision","tool":"get_volume",
 "decision":"simulated","ok":true,"risk":"low","dry_run":true,"arguments":{},
 "duration_ms":0.19,"output":"dry-run: get_volume()","rules":["dry_run"],
 "reason":"dry-run: 'get_volume' would run with 0 argument(s)","session_id":"live",
 "request_id":"","plan_id":"","task_id":"","call_id":"c1",
 "arguments_fingerprint":"e996a9a28b0a23b8591ee2e9c5873396","persisted":true}
```

* **Redaction is key-based *and* value-based.** `redact_payload()` in
  `Backend/star/observability/logging.py` now redacts any credential-like key
  (`api_key|token|password|secret|credential|authorization|access_key|private_key|session_key`)
  *and* anything matching secret shapes (`sk-…`, long base64/hex), recursing through dicts and
  lists. `{"token": "abc123"}` — which no value pattern would ever catch — becomes `[REDACTED]`.
* Huge arguments are truncated, not dropped; the fingerprint stays comparable.
* `JsonlSink.write()` swallows `OSError`; `persisted` is computed with `path.is_file()`
  (a *directory* at that path used to report `exists()` = True and lie), and the audit health then
  reports `degraded` instead of silently claiming durability.
* Reads are corrupt-tolerant: `read_file()` skips malformed lines; `tail()`/`query()` serve the ring
  buffer even when the file is gone. Recording never raises into the tool path.

---

## 7. The legacy gate — blueprint #2 with no exceptions

Non-negotiable: *every OS action passes the safety/policy layer.* The existing brain calls
`Backend.tools.registry.execute_tool()` from ~40 sites, so Phase 4 wraps the funnel itself:

`ToolExecutor.startup()` installs `LegacyToolGate` over every loaded `Backend` module that exposes
`execute_tool`; `aclose()` removes it. Verified live, calling the **old** functions directly:

```
Backend.nlu.command_router.execute_tool("get_volume")        → simulated (dry-run), audited
Backend.tools.registry.execute_tool("set_volume", level=42)  → simulated (dry-run), audited
```

Two hard-won properties:

* **Retirement, not just restore.** A module imported *after* `install()` binds the gate object at
  import time, so restoring recorded modules is not enough. `uninstall()` sets `_retired=True` and
  sweeps *all* loaded `Backend` modules; the gate `__call__` bypasses straight to the original
  function once retired (`stats.retired_hits`). Without this, a gate from an earlier test leaked
  into the app and later chats silently degraded to `unknown_tool`.
* **Never eats an error.** If the executor itself fails, the gate returns a legacy-shaped
  `{"success": False, "error": …}` and works when called from the event-loop thread too.
* `STAR_LEGACY_GATE=false` disables it (used by one test to prove both directions).

---

## 8. Typed registry — reuse, no duplicates

`import_legacy()` pulls the **30 tools already written** in `Backend/tools/registry.py` into typed
`ToolSpec`s: `origin="legacy"`, real module path, category/agent mapping, risk, JSON-schema
parameters derived from the handler signature, `timeout_s`, `idempotent`, `reversible`,
`dry_run_safe`. `summary()` reports `without_handler: []` — nothing is registered that cannot run.

`spec_from_function()` uses `typing.get_type_hints()`, so handlers written with
`from __future__ import annotations` (PEP 563 string annotations) still yield correct parameter
types; unions and `list[…]`/`dict[…]` origins map to JSON kinds instead of degrading to "string".

`ToolSpec.public()` never exposes the handler; `ToolResult` is `extra="forbid"`;
`as_call_result()` merges handler data at the top level so the legacy shape
(`{"success": …, "result": …}`) survives unchanged for the existing router and the PySide6 HUD —
and reserved keys (`success`, `decision`, `dry_run`, `duration_ms`) can never be shadowed.

---

## 9. Wiring (what changed in existing files)

| File | Change |
| --- | --- |
| `Backend/star/main.py` (62 lines changed) | builds registry → audit → confirmations → permissions → executor → `SecuritySurface`; adds the **`executor` capability slot** (separate from `tools`, Phase 10 puts the orchestrator above it); planner gets `risk_classifier=registry.risk_of`; brain gets the executor so `PROPOSED` calls really run; startup/aclose order fixed (security first up, last down) |
| `Backend/star/brain/planning.py` (183 lines changed) | risk helpers re-exported from `tools.risk` (one implementation), plus `task_state_from_calls()` |
| `Backend/star/brain/pipeline.py` (28 lines changed) | appends an honest dry-run note when calls were simulated, so the assistant never claims it changed the OS |
| `Backend/star/brain/__init__.py` (101 lines changed) | fully lazy PEP 562 exports — importing the brain no longer imports tools |
| `Backend/star/config/settings.py` (2 lines changed) | `legacy_gate` on `SecuritySettings` |
| `Backend/star/observability/logging.py` (40 lines changed) | key-based secret redaction in `redact_payload()` |
| `tests/test_star2_gateway.py`, `tests/test_star2_brain.py` | expectations updated for the real registry payloads |
| `.gitignore` | `.env.*` used to swallow `.env.example` — negated, so the documented template is finally tracked (it contains no secret values) |

Gateway routes were declared in earlier phases as contracts and are **real** now: `GET /api/v1/tools`
(30 typed specs with risk + permission metadata), `GET /api/v1/audit` (redacted tail),
`GET|POST /api/v1/confirmations` (list / approve-deny), `POST /api/v1/stop` (emergency stop that
mutes the tool layer).

---

## 10. Test results

```
tests/test_star2_tools.py      35 passed   risk · spec · registry · permissions · executor · gate · app
tests/test_star2_security.py   20 passed   fingerprints · audit · confirmations · surface
full suite (--ignore=tests/test_hypothesis.py)   347 passed, 1 failed
scripts/smoke.py               17 passed, 0 failed
```

The single failure is the pre-existing baseline `tests/test_command_router.py::test_google_search`
(`search_web` vs `web_search` naming) carried since Phase 0 — scheduled for Phase 12, untouched
here. `tests/test_hypothesis.py` cannot be collected in this sandbox (`hypothesis` is not
installed); it is unrelated to Phase 4.

Selected test names (all green): `test_gate_routes_legacy_execute_tool_through_policy`,
`test_gate_installs_and_uninstalls_across_modules`, `test_executor_simulates_in_dry_run_without_calling_the_handler`,
`test_executor_really_runs_when_dry_run_is_off`, `test_executor_handles_errors_timeouts_and_unknown_tools`,
`test_permissions_lists_and_shell_policy`, `test_permissions_confirmation_gate`,
`test_permissions_rate_limits`, `test_audit_records_are_stamped_redacted_and_persisted`,
`test_audit_never_raises_when_it_cannot_write`, `test_confirmation_reuse_and_distinct_calls`,
`test_confirmation_expiry`, `test_confirmation_denial_is_not_an_approval`,
`test_surface_blocks_critical_actions_outright`, `test_classify_risk_ignores_free_text_argument_values`.

---

## 11. Non-negotiables checklist (blueprint §3)

| # | Rule | Phase 4 evidence |
| --- | --- | --- |
| 2 | every OS action passes the policy layer | `LegacyToolGate` over the legacy `execute_tool()` funnel — proven by calling the old functions directly |
| 3 | dry-run default for automation | `STAR_DRY_RUN=true`; simulated calls never touch the handler; brain says so out loud |
| 4 | tests for new core modules | 55 new tests, 1 203 lines |
| 5 | no hard-coded secrets | env-only config; audit redaction by key **and** value; `.env.example` tracked with empty values |
| 6 | modular monolith | 10 modules behind lazy `__init__`s; nothing imports pyautogui at module level |
| 7 | explicit confirmation for sensitive actions | `ConfirmationStore` + `POST /api/v1/confirmations` + `execute_confirmed()` |
| 9 | visible UI independent of automation | HUD keeps the legacy result shape; new decisions arrive as events, not UI changes |
| 10 | audit everything | append-only JSONL with ids, risk, decision, duration, fingerprint |

---

## 12. Known limits (honest list)

1. Risk tiers for legacy tools are heuristic (name + argument keys). Phase 6/11 will refine them
   per skill, and `confirm_above` allows a per-tool override today.
2. `execute_confirmed()` runs for real on approval — correct by design, but it means an approval is
   a genuine authorisation. TTL (120 s) and fingerprinting bound the blast radius.
3. The audit file has no rotation/size cap yet (entries are truncated individually; `seq` is
   monotonic). Rotation is Phase 12 packaging work.
4. 8 legacy tool modules cannot load in this sandbox (`pyautogui` missing) — auto-discovery skips
   them, so the registry holds 30 tools here; a real desktop adds those modules' tools on top.
5. Rate limits are per-process counters, not persisted across restarts.

---

## 13. Next: exact Phase 5 scope (Browser agent)

1. `agents/browser_agent.py` — goal-oriented worker: open / read / extract / fill / click /
   screenshot, built on the existing `Backend/tools/web/*` capabilities (inspect first, reuse).
2. Every browser action registers as a `ToolSpec` (agent=`browser`) so it inherits risk, policy,
   confirmation and audit — no private execution path.
3. Step budget + timeout per goal, dry-run plan preview, and a structured `BrowserObservation`
   (url, title, text, links, screenshot path) for the OBSERVE→ACT→VERIFY loop.
4. Gateway: `POST /api/v1/browser` verbs + browser state in `/api/v1/status`; ops console panel.
5. Tests: navigation guardrails (deny `file://`, private hosts), extraction parsing, dry-run never
   launches a browser, budget exhaustion, verification-after-action.
