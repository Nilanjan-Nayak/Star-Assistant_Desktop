# STAR 2.0 — PHASE 3 REPORT
### Brain: typed schemas · memory-first context · validated reasoning · plans · prediction · reflection

*Branch `star-2.0` · base `star-2.0-phase-2` · blueprint §7 Phase 3*

> **Phase 3 requirements:** "Define `UserRequest`, `Context`, `Plan`, `Task`, `ToolCall` schemas.
> Retrieve relevant memory before planning. Add reflection after execution. Add simple next-action
> prediction from history. Use the existing LLM providers but always validate model output. Never let
> raw model output directly execute arbitrary shell/OS commands."

---

## 1. What was built (8 new modules, ~2 200 lines + 1 377 lines of tests)

| Module | Lines | Purpose |
|---|---|---|
| `Backend/star/brain/schemas.py` | 329 | `UserRequest`, `Context`, `MemoryHit`, `Reasoning`, `RawAction`, `Plan`, `Task`, `ToolCall`, `Verification`, `Prediction`, `Reflection`, `TaskResult`; enums `TaskState` (9), `ToolCallState` (7), `AgentName` (7); id factories reuse `agent.core.ids.new_id` |
| `Backend/star/brain/context.py` | 216 | `ContextBuilder` (session scratch + long-term retrieval **before** planning), `MemoryRetriever` protocol, `StoreRetriever` over the existing `agent.memory.store.MemoryStore`, `NullRetriever`, `time_of_day` |
| `Backend/star/brain/reasoning.py` | 518 | `RouterReasoner` (existing `route_command`), `ProviderReasoner` (existing `get_llm_provider()`), `FallbackReasoner` (offline bn/en answers), `ReasoningCascade`, **`sanitize_actions` / `sanitize_proposed_calls`** |
| `Backend/star/brain/planning.py` | 387 | `StarPlanner`: reasoning → `Plan` of `Task`s grouped per agent, risk + confirmation + block states, truncation, `agent_for_tool`, `default_risk`, `risk_at_least` |
| `Backend/star/brain/prediction.py` | 278 | `PatternStore` (append-only `data/patterns.jsonl`), `HistoryPredictor` (seed + learned pairs, Laplace confidence, **safe tools only**), `NullPredictor` |
| `Backend/star/brain/reflection.py` | 216 | `StarReflector`: verdict (success/partial/failed/blocked/awaiting_confirmation/no_action), retry / recover / escalate recommendation, verifier hook, `NullReflector` |
| `Backend/star/brain/pipeline.py` | 403 | `StarBrain.handle()` — the 7-step pipeline; honest-note correction; episodic memory write; cross-turn pattern learning; `build_brain`, `open_memory_store` |
| `Backend/star/brain/__init__.py` | 82 | public surface (55 exports) |

Modified: `config/settings.py` (+3 brain fields), `main.py` (brain auto-built, brain in the startup
loop, no duplicate `plan.created`), `scripts/smoke.py` (+4 checks), gateway tests (+1 fixture/test).

**Nothing in `Frontend/`, `agent/`, `Backend/llm/`, `Backend/nlu/`, `Backend/tools/` changed.** The
brain *calls* them; it does not copy them.

---

## 2. The pipeline (one turn)

```
text ─► UserRequest ─► ①ContextBuilder.build()      memory retrieved FIRST (blueprint rule)
                        │   session scratch + MemoryStore.recall_relevant + build_context_block
                        │   └─ event context.built
                        ▼
                      ②ReasoningCascade               router → LLM provider chain → offline fallback
                        │   every provider: asyncio.to_thread + wait_for(timeout)
                        │   every action: sanitize_actions / sanitize_proposed_calls
                        │   └─ event reasoning.complete
                        ▼
                      ③StarPlanner.plan()             tasks grouped per agent, risk, confirm/block
                        │   └─ event plan.created
                        ▼
                      ④HistoryPredictor.predict()     suggestions only (executed=False always)
                        │   └─ event prediction.proposed
                        ▼
                      ⑤executor seam (Phase 10)       only if something is still PROPOSED
                        ▼
                      ⑥StarReflector.reflect()        expected vs observed → retry/recover/escalate
                        │   └─ event reflection.result
                        ▼
                      ⑦learn + remember               intra-turn pairs, cross-turn bridge (success only),
                                                      episodic write ─ event memory.updated
                        ▼
                      result dict + event brain.turn_completed
```

Result shape (consumed by the gateway, HUD mirror and console):
`ok, response, honest_note, intent, source, language, session_id, latency_ms, dry_run, request_id,`
`plan{}, tasks[], task_count, reflection{}, predictions[], reasoning{}, context{}, memory_hits, events_published`.

---

## 3. "Never let raw model output execute anything" — how it is enforced

`sanitize_actions()` / `sanitize_proposed_calls()` run on **every** provider result:

| Rule | Effect |
|---|---|
| tool name must match `^[a-z0-9_][a-z0-9_.\-]{0,63}$` | `"Not A Tool!"`, `"bad name!"` dropped |
| tool must be registered (`Backend.tools.registry.get_all_tools()`, 30 tools discovered) | hallucinated tools dropped, logged |
| argument keys must be identifiers | `"bad key!"` dropped |
| values JSON-safe, strings ≤ 4 000 chars, nesting ≤ 3 | long strings truncated, deep dicts capped, exotic objects → `repr` |
| `command/cmd/shell/script/code/exec/bash/powershell` arguments containing `; & \| \` $ > <` are dropped unless `STAR_ALLOW_SHELL=true` (default **false**) | `{"command": "ls; rm -rf /"}` → `{}` |
| proposals become `ToolCallState.PROPOSED`, never executed by the brain | execution only via the orchestrator + policy (Phase 10/11) |
| OpenAI/Ollama shapes accepted (`{tool,args}`, `{name,arguments:"json"}`, `{function:{name,arguments}}`) | provider-agnostic |

Every drop/coercion is recorded in `Reasoning.notes` and surfaced in `Plan.rationale`, so the audit
trail shows *what the model asked for* and *what was refused*.

---

## 4. Memory before planning (verified, not just claimed)

`StoreRetriever` adapts the existing `agent.memory.store.MemoryStore` (SQLite + hashing embeddings):
`recall_relevant(query, top_k)` → `MemoryHit{layer, kind, score, key, value}` and
`build_context_block(query, top_k=…)` → the prompt-ready digest handed to the LLM.

* Layers mapped: `fact → semantic`, `preference → preference`, `episode → episodic`.
* The brain writes one **episodic** record per turn (`"volume 40 koro → command [set_volume]"`).
* Blocking SQLite calls run in threads with a 4 s timeout; a locked/corrupt DB degrades to
  `NullRetriever` instead of failing the turn.
* Order is asserted in tests **and** in the smoke test: `context.built` is always published before
  `plan.created`.

Phase 8 will replace `StoreRetriever` with the layered `MemoryManager` (working/episodic/semantic/
preferences + hybrid ranking) behind the same `MemoryRetriever` protocol — no brain change needed.

---

## 5. Planning: agents, tasks, risk and confirmations

* One `Task` per **agent** (`conversation`, `research`, `browser`, `computer`, `filesystem`, `system`,
  `coding`), mapped by namespace prefix → exact legacy tool name → keyword fallback.
  Verified: `set_volume→system`, `take_screenshot→computer`, `search_web→research`,
  `youtube_open_history_page→browser`, `browser.navigate→browser`, `file.read→filesystem`,
  `code.run_python→coding`, `recall_memory→conversation`.
* The reply itself is always task #0 (`conversation`), so a plan explains the answer even with no tools.
* Task states from the blueprint: `pending / running / waiting_confirmation / verifying / done /
  failed / cancelled / recovered / blocked`.
* Risk decides state: `risk ≥ STAR_CONFIRM_ABOVE_RISK (high)` → `WAITING_CONFIRMATION`
  (`plan.requires_confirmation = true`); `risk ≥ STAR_DENY_RISK (critical)` → `BLOCKED`.
  Example: `lock_workstation → high → waiting_confirmation`, `format_disk → critical → blocked`.
* Risk is a **pluggable classifier** (`risk_classifier=` seam). The keyword default is deliberately
  conservative (unknown ⇒ `medium`); Phase 4 replaces it with typed registry metadata and Phase 11
  with the policy engine — the planner code will not change.
* Plans are capped (`STAR_MAX_PLAN_TASKS=8`, `max_tool_calls_per_task=12`) and truncation is counted.

---

## 6. Prediction — suggestions that can never act

* Seeds (all reversible, all low/medium risk): `play_music→set_volume`, `take_screenshot→see_screen`,
  `launch_application→see_screen`, `set_volume→get_volume`, `remember_fact→recall_memory`, …
* Learned pairs are stored append-only in `data/patterns.jsonl` (`{a,b,count,ts}`) and scored with
  Laplace smoothing `count/(count+2)`; history beats seeds when both apply.
* **Cross-turn** learning: last tool of turn *N* → first tool of turn *N+1*, recorded only when both
  turns succeeded — Star never copies its own failures.
* Hard safety filter: a predicted tool whose risk reaches the confirmation/deny threshold, or that is
  on the tool deny-list, is dropped and counted (`stats.unsafe_filtered`).
* `Prediction.executed` is always `False`; the orchestrator (Phase 10) must turn a prediction into a
  normal task that passes policy before anything runs.
* Disabled entirely with `STAR_PREDICTION=false`.

---

## 7. Reflection — expected vs observed, and what to do next

* Verdict from task/call evidence: `success`, `partial`, `failed`, `blocked`,
  `awaiting_confirmation`, `no_action`, `unknown`.
* `should_retry` — a failed call, `retries < max_retries`, risk ≤ medium, **and not dry-run**
  (nothing really ran, so there is nothing to retry).
* `should_recover` — a failed/recovered `computer` or `browser` task (Phase 6/10 own the recovery).
* `should_escalate` — blocked, awaiting confirmation, or retries exhausted.
* Optional `verifier` hook (async, `(Task, Context) → (ok, note)`) can downgrade a `success` —
  Phase 6 supplies real screen/output verification; a broken verifier is logged, never fatal.
* **Honesty rule:** when verification disagrees with the tool layer's claim, Star appends a short
  correction in the *reply's* language. Real example from this sandbox (no network):

  > `ইউটিউবে তোমার জন্য 'bangla top hit songs' চালিয়ে দিয়েছি বন্ধু! মজে শোনো।`
  > `(তবে একটা ধাপ ব্যর্থ হয়েছে — আমি ঠিক করে আবার চেষ্টা করবো।)`

  with `ok=false`, `reflection.verdict="failed"`. A confident sentence over a failed step is worse
  than a short correction.

---

## 8. Changed files (`git diff --name-status star-2.0-phase-2`)

```
A  Backend/star/brain/{__init__,schemas,context,reasoning,planning,prediction,reflection,pipeline}.py
M  Backend/star/config/settings.py     (+3  llm_timeout_s, router_timeout_s, reasoning_chain)
M  Backend/star/main.py                (+33 brain auto-build, brain startup, no duplicate plan.created)
M  scripts/smoke.py                    (+28 4 new checks: banglish profile, plan+reflection, predictions, memory order)
A  tests/test_star2_brain.py           (1 377 lines, 91 tests)
M  tests/test_star2_gateway.py         (+60 real-brain gateway fixture + integration test)
A  docs/PHASE_3_REPORT.md
```

---

## 9. Test results

```
$ python -m pytest tests/test_star2_brain.py -q
91 passed in 2.53s

$ python -m pytest
1 failed, 295 passed in 31.92s      ← 296 total (was 204 after Phase 2; +92 this phase)

$ python scripts/smoke.py
[smoke] 17 passed, 0 failed         ← was 13 in Phase 2
```

Only failure remains the **pre-existing** `tests/test_command_router.py::test_google_search`
(legacy tool rename `google_search` → `web_search`; scheduled for Phase 12).

Coverage added: schema/state/enum contracts · JSON round-trip · id prefixes · agent mapping (19 cases) ·
risk classification (14 cases) + ordering · sanitiser rules (valid/invalid/shell/depth/keys/LLM shapes) ·
fallback reasoner in bn+en · cascade order, skipping, error swallowing, no-answer path ·
router + provider reasoners (monkeypatched, incl. timeout, exception, non-dict, memory passing) ·
`build_reasoner` always ends in fallback · context builder scratch memory, retrieval, events,
failing retriever/store · `time_of_day` · planner grouping, conversation task, confirmation/block,
injected + broken risk classifier, truncation, empty turn, proposals stay PROPOSED ·
pattern store learn/persist/reload/corrupt-file tolerance · predictor seeds, learned history,
unsafe filtering, disable switch, learn-only-success · reflector verdicts, retry/recover/escalate
rules, verifier hook, broken verifier, disable · StarBrain full pipeline, language detection,
honest notes in both languages, reasoner crash isolation, episodic writes, failing memory writes,
executor seam, cross-turn learning, lifecycle/introspection, real SQLite store, JSON serialisability ·
gateway integration with the real brain.

---

## 10. Risks

1. **The legacy router executes as it routes.** Phase 3 therefore records those actions as already-run
   evidence and reflects on them; it cannot yet refuse one *before* it happens. That gate arrives with
   the typed registry + policy (Phase 4/11) and the orchestrator seam (Phase 10), which is already
   wired (`executor=`) and tested.
2. **Keyword risk classification is coarse.** `default_risk` is a conservative placeholder; a tool can
   be mis-tiered until Phase 4 attaches typed metadata. Unknown ⇒ `medium`, destructive words ⇒
   `critical`, so the failure mode is "asks too often", never "acts silently".
3. **Provider latency.** The LLM stage is bounded by `STAR_LLM_TIMEOUT_S` (25 s) and runs in a
   thread; the deterministic router answers first, so commands never wait on the network.
4. **Memory growth.** One episodic row per turn. Phase 8 adds retention/compaction; the store already
   de-duplicates identical text.
5. **Hashing embedder is weak.** `recall_relevant` uses `HashingEmbedder` (no model download). Good
   enough for key/fact recall, weak for paraphrase — Phase 8 can swap in `SentenceTransformerEmbedder`
   behind the same protocol.

## 11. Verification checklist

- [x] `UserRequest` / `Context` / `Plan` / `Task` / `ToolCall` (+ `Verification`, `Prediction`, `Reflection`) defined, JSON-safe, tested
- [x] memory retrieved **before** planning — asserted by event order in tests and in smoke
- [x] reflection after execution with expected vs observed + retry/recover/escalate
- [x] next-action prediction from history, safe-only, never executed
- [x] existing LLM providers reused (`get_llm_provider`, `route_command`) — none rewritten
- [x] model output always validated; shell payloads dropped unless `STAR_ALLOW_SHELL=true`
- [x] brain auto-built by `build_application`, injectable for tests, health/describe/snapshot exposed
- [x] 92 new tests green; whole suite green apart from the known pre-existing failure; smoke 17/17
- [x] frontend, `agent/`, legacy backend untouched
- [x] one commit, tag `star-2.0-phase-3`

## 12. Next: exact Phase 4 scope (Tools)

1. `tools/spec.py` — `ToolSpec` (name, description, category, agent, `risk`, parameters JSON-schema,
   `confirm_above`, `timeout_s`, `idempotent`, `reversible`, handler).
2. `tools/registry.py` — typed `StarToolRegistry`: `register`, `get`, `list`, `schema_for`, `risk_of`;
   **imports the 30 legacy tools from `Backend.tools.registry` as specs** (no duplicate implementations).
3. `tools/permissions.py` — allow/deny lists, per-session rate limits, `STAR_ALLOW_SHELL` enforcement,
   dry-run interception (a dry-run call returns a simulated result and never touches the OS).
4. `tools/executor.py` — the single execution path: validate args against the schema → permission check
   → confirmation check → audit entry → run in a thread with timeout → typed `ToolResult`.
5. `tools/audit.py` — append-only `logs/audit.jsonl` with redaction, tool, args hash, risk, decision,
   duration, outcome, request/plan/task/call ids.
6. Wiring: `StarPlanner` gets `risk_classifier=registry.risk_of`; `StarBrain.executor` gets a
   `ToolExecutor` so `PROPOSED` calls actually run through policy; gateway `/api/v1/tools` serves typed specs.
7. Tests: spec validation, legacy import parity (30 tools), permission decisions, rate limiting,
   dry-run never calls the handler, audit redaction, timeout handling, planner/registry risk agreement.
