# STAR 2.0 — PHASE 9 REPORT
### The safe learning loop: observation → candidate → six gates → promotion into the stores Star already has

*Branch `star-2.0` · base `b7c21c7` (`star-2.0-phase-8`) · blueprint §9 "Memory & Learning" + §7 Phase 9*

> **Blueprint §9:** *"Safe learning loop: **interaction → observation → feedback → candidate pattern →
> validation → memory update → future retrieval.** Do **not** permit uncontrolled self-modification of
> executable code or model weights. A prediction must still pass the normal policy/tool boundary."*
> **Blueprint §7 Phase 9:** *"Learning from experience — **safe patterns only**."*
> **Done when:** Star turns finished work + explicit feedback into *candidate* patterns/preferences, validates
> them against the gates, and promotes only the safe ones into the memory the procedural layer already reads.
> **Acceptance:** a validated candidate lands in a real store; a candidate that fails any gate never does.

---

## 1. What was built (6 new modules + a package init, 1 937 lines + 1 518 lines of tests)

| Module | Lines | Responsibility |
| --- | --- | --- |
| `Backend/star/learning/schemas.py` | 191 | the inert vocabulary: `Candidate` (kind/signature/evidence/state), `CandidateKind` **{pattern, preference}**, `CandidateState`, `Feedback`, `FeedbackSignal`, `ValidationResult`, `Promotion` — all `extra="forbid"` |
| `Backend/star/learning/patterns.py` | 314 | `CandidateStore` — append-only JSONL ledger, last-wins replay, dedupe by signature, bounded eviction, dry-run-aware observation |
| `Backend/star/learning/feedback.py` | 121 | `FeedbackLog` — append-only JSONL of explicit feedback, **redacted** on write, newest-first reads |
| `Backend/star/learning/validator.py` | 237 | `CandidateValidator` — the six **kind-aware** gates, pure and side-effect-free; the code/secret scanners; `describe()` |
| `Backend/star/learning/loop.py` | 610 | `LearningLoop` — observe → feedback → validate → promote → cycle; borrows the brain's `PatternStore` and the shared `MemoryManager`; `snapshot()`/`describe()`/`health()`; `build_learning()` |
| `Backend/star/learning/tools.py` | 381 | 4 typed `ToolSpec`s + `LearningToolkit` (honest, never raises) |
| `Backend/star/learning/__init__.py` | 83 | lazy PEP 562 exports |

`tests/test_star2_learning.py` — **141 tests** (schemas 11 · candidate store 18 · feedback log 8 · validator
gates 20 · loop observation 14 · loop feedback 8 · validation + promotion 21 · lifecycle & views 10 · tools 19 ·
application wiring 7 · gateway & console 5), all hermetic: every ledger and promotion target lives in
`tmp_path`. Plus **11 new smoke checks** (`scripts/smoke.py`, now **65**) and two additive-only guard updates
(tool count 64 → 68, route count 31 → 34).

**Suite: 736 collected · 735 passed · 1 failed** — the failure is `tests/test_command_router.py::test_google_search`,
the pre-existing baseline recorded in Phase 0 (legacy router renames `search_web` → `web_search`), scheduled for
Phase 12. **Smoke: 65 passed, 0 failed.**

---

## 2. The candidate ledger — inert data, closed kinds

A **candidate** is a pattern or preference Star is *considering*, plus the evidence for it. It is **inert**:
nothing in a `Candidate` can run, and nothing reaches a real store until the validator clears every gate and the
loop promotes it. The kind is a **closed enum** — this is the first anti-self-modification guarantee, and it is
structural:

```python
class CandidateKind(str, Enum):
    PATTERN    = "pattern"      # "before → after": two tool names that co-occurred and worked
    PREFERENCE = "preference"   # "key = value": a user-approved habit
```

There is **no `code` kind, no `weight` kind, no `file` kind** — `CandidateKind("code")` raises. A candidate
carries only strings (`before`/`after`, `key`/`value`), integer evidence counters (`observations`, `successes`,
`failures`, `approvals`, `rejections`), a `risk` tier, a bounded `evidence` list, and a `state`
(`observing → validated → promoted`, or `→ rejected`). `success_rate` and `is_terminal` are derived.

`CandidateStore` persists candidates as **append-only JSONL** and replays **last-wins** (the newest line for a
`candidate_id` is the truth), so a process can append without ever rewriting history. Observations **dedupe by
signature** (`take_screenshot→see_screen`, `reply_language=bn`): the hundredth sighting bumps counters on the
*same* candidate, it does not create a hundredth row in memory. The working set is **bounded**
(`STAR_LEARNING_MAX_CANDIDATES`, default 500) — eviction drops the **oldest terminal** candidates first, then the
least-recently-seen, so a live candidate is never starved by graveyard.

Two rules make the evidence honest:

* **A dry-run never claims success.** `observe_pattern(..., succeeded=None)` records the *sighting* without an
  outcome. A rehearsal proves intent, not that the workflow really works, so it can never push a candidate over
  the success-rate gate on its own. `AgentRun.dry_run` defaults to `True`, and `observe_run`/`observe_chat`
  forward it, so a simulated browser/computer goal is counted as an observation with **no** success.
* **A terminal candidate is not reopened.** A promoted pattern stays promoted (so it is written **once**); a
  pattern the user rejected stays rejected even if it is sighted again.

---

## 3. The six gates — kind-aware (the safety core)

`CandidateValidator.validate()` runs every gate in a fixed order and returns a `ValidationResult` with the
**pass/fail of each gate** and human-readable reasons — the console shows *why*, never a bare yes/no.

| # | Gate | A **pattern** must… | A **preference** must… |
| --- | --- | --- | --- |
| 1 | `frequency` | be sighted ≥ `STAR_LEARNING_MIN_FREQUENCY` (3) times | be stated ≥ **1** time |
| 2 | `success_rate` | succeed ≥ `STAR_LEARNING_MIN_SUCCESS_RATE` (0.8) of *decided* runs, with ≥ 1 decided | be approved ≥ 0.8 of (approvals + rejections), with ≥ 1 decision |
| 3 | `risk` | reach **no higher** than `STAR_LEARNING_MAX_RISK` (medium) | same (a preference write is a medium `set_`) |
| 4 | `no_code` | carry no executable-code / weight-update marker, and only plain slug names | same |
| 5 | `no_secret` | name no credential key and carry no credential-shaped material | same |
| 6 | `approval` | — (patterns learn from **outcomes**) | have **explicit positive feedback and zero rejections** |

The gates are **kind-aware on purpose**. A *pattern* is an inference from repetition, so it needs frequency and a
success rate. A *preference* is **not** an inference — it is a habit the user *stated*, so one statement counts
and the `approval` gate (a human "yes", no "no") is what makes it safe. `ok` is `all(gates.values())`, **and an
explicit rejection outranks everything**: `rejections > 0` forces `ok = False` even if every other gate passed —
the user said stop, so Star stops.

**Hard vs soft failures.** The loop distinguishes *unsafe* from *not-yet-proven*:

* a **hard** gate (`risk`, `no_code`, `no_secret`, `approval`) failing ⇒ the candidate is **`rejected`**
  (terminal) and a `learning.rejected` event records the reason;
* a **soft** gate (`frequency`, `success_rate`) failing ⇒ the candidate **stays `observing`** with its reasons
  remembered — more evidence may still qualify it, but it is never promoted on insufficient evidence.

A **broken risk classifier fails closed**: if `risk_of` raises, the candidate is treated as `high` risk and
refused, because "I could not classify it" must never mean "learn it".

---

## 4. Anti-self-modification — structural *and* behavioural

The blueprint's hardest constraint is *"no uncontrolled self-modification of executable code or model weights"*.
Phase 9 enforces it four ways, and the test suite asserts each:

1. **Closed kinds (§2).** A candidate can only be a pattern or a preference — both pure data. There is no
   representation for "new code" or "new weights", so there is nothing to promote them into.
2. **The `no_code` gate.** Eight scanner families reject any candidate whose fields look like an attempt to
   smuggle execution: `import/exec/eval/compile/__import__/subprocess/os.system/popen`; `lambda/globals/setattr/…(…)`;
   weight/file extensions (`.py .so .dll .exe .bin .pt .pth .onnx .ckpt .safetensors .gguf`);
   `model_weights/state_dict/load_state/checkpoint/fine_tune`; destructive shell (`rm -rf`, `mkfs`, `dd if=`,
   fork bombs, `shutdown`, `reboot`); shell metacharacters and pipes (`; | ` $ && || $( curl wget`); Python
   `def/class` definitions; injected `<script>/javascript:/on…=`. Tool names and preference keys must also match
   `^[A-Za-z0-9_.:\-]{1,80}$` — a plain slug, never a payload.
3. **The `no_secret` gate.** A candidate whose key names a credential (`is_secret_key`) or whose value/evidence
   is credential-shaped (`redact(text) != text`) is refused — learning never launders a secret into memory.
4. **Only two writes exist, both data-into-existing-stores.** Promotion is *exactly* one of:
   * `patterns.record([before, after])` — reinforce a `tool → tool` counter in the brain's `PatternStore`;
   * `memory.set_preference_now(key, value)` — store a key/value habit in the shared preference layer.

   The loop opens **no file, writes no code, touches no weight, spawns no process**. A prediction the
   `PatternStore` later makes is *still* just a proposal: it crosses the normal Phase 4 permission ladder and
  Phase 11 policy boundary before anything runs — **prediction never bypasses permission**, and the loop never
  auto-executes a prediction.

`snapshot()`, `describe()` and `learning_status` all carry `no_self_modification: true` / `anti_self_modification:
true` so the guarantee is visible in the console and the API, not just in the code.

---

## 5. The loop — §9's pipeline, step by step

```
interaction → observation → feedback → candidate → validation → memory update → future retrieval
```

| Stage | Method | What happens |
| --- | --- | --- |
| **observation** | `observe_sequence(tools, succeeded=…)` | every adjacent `before → after` pair in a finished tool sequence becomes/updates a candidate; risk = `highest(before, after)`; emits `learning.observed` |
| | `observe_run(run)` / `observe_chat(result)` | adapters that read the tool sequence off a finished `AgentRun` (skipped steps ignored) or a brain result dict (a `failed`/`blocked` reflection verdict ⇒ not succeeded); both forward `dry_run` |
| **feedback** | `record_feedback(signal, …)` | the *human* half. `positive`/`correction` approve the target; `negative` **rejects** it (terminal). A stated `key`/`value` with a positive signal **seeds a preference candidate and approves it exactly once** (the double-approval bug is guarded by an `approved_ids` set). Free text is redacted into the `FeedbackLog`; emits `learning.feedback` |
| **validation** | `validate_candidate(c)` | runs the six gates, moves the candidate to `validated` / `rejected` / stays `observing` |
| **memory update** | `promote(c)` | writes a **validated** candidate into its one store, marks it `promoted` (terminal, once), emits `learning.promoted` (+ `pattern.learned` for a pattern). A non-validated candidate is refused |
| **sweep** | `cycle(promote=…, limit=…)` | validate every observing candidate, promote the validated ones, bounded by `STAR_LEARNING_MAX_PROMOTIONS` (8) per cycle; emits `learning.validated`. **Never bypasses a gate** |
| **future retrieval** | — | a promoted pattern is now a counter in the `PatternStore` the Phase 3 predictor and the Phase 8 procedural layer already read; a promoted preference is now in the layer `memory_recall` already blends |

**Auto-promote.** With `STAR_LEARNING_AUTO_PROMOTE=true` (default), observation and feedback validate-and-promote
inline, so a pattern that has just earned its third success is learned immediately. With it `false`, observation
only *accumulates* — candidates wait for an explicit `cycle()` (the tool, the route, or startup). Either way the
gates are identical; the flag only decides *when* the sweep runs.

---

## 6. Promotion targets — borrowed, never reopened (no second store)

The blueprint rule from Phase 8 still holds: **inspect and reuse before introducing another store.** The learning
loop owns **no** long-term store. It keeps two *staging* ledgers (the candidate JSONL and the feedback JSONL) and
promotes only into stores the application already built:

| Target | Borrowed from | Attached by |
| --- | --- | --- |
| `PatternStore` (procedural `tool → tool` counters) | the **brain's** live predictor store (`brain.predictor.store`) | `learning.attach_patterns(...)` in `build_application` |
| `MemoryManager` preference layer | the **shared** Phase 8 memory manager | `learning.attach_memory(...)` in `build_application` |

`test_learning_borrows_the_brain_pattern_store` asserts `app.learning.patterns is app.brain.predictor.store` and
`test_learning_borrows_the_shared_memory_manager` asserts `app.learning.memory is app.memory` — **identity**, not
a copy. There is exactly one `PatternStore` and one `MemoryStore` in the process. If a target is missing
(`brain`/`memory` not wired), promotion fails **honestly** (`ok=False`, "no pattern store attached") rather than
silently dropping the candidate or opening a private store.

---

## 7. Tools — four typed specs, one ladder

| Tool | Risk | What it does |
| --- | --- | --- |
| `learning_status` | low | the gates, candidate counts by state, promotion targets, counters, last cycle/promotion, `no_self_modification` — read-only |
| `learning_list_candidates` | low | the inert ledger with evidence + gate results, filterable by `kind`/`state` — read-only |
| `learning_feedback` | medium | record a signal; optionally state a `key`/`value` preference (the only thing that can approve one) |
| `learning_cycle` | medium | one bounded validation (+ promotion) sweep, or validate/promote a single candidate by id |

All four are `category=LEARNING`, `agent=CONVERSATION`, `dry_run_safe=True`, `origin=star2`. The reads are `low`;
the two that can change what Star knows are `medium` — a mutation of Star's *notes*, never of the OS, so they sit
below the confirmation threshold but above read-only. `LearningToolkit` **never raises**: a missing loop, a dead
ledger or a bad signal becomes `ok=False` with a reason and a one-line `output` the brain can say aloud, because a
learning problem must never take down a reply. `register_learning_tools(registry, loop=None)` is allowed — the
tools then answer honestly that learning "is not wired in this build", so the registry surface is complete in
every configuration.

---

## 8. Wiring — learning happens *in* the flow, not beside it

`build_application` builds the learning slot **always** (exactly like memory): a *disabled* loop is still a real
object that reports `health()="off"`, returns an informative `snapshot()` ("nothing is observed, validated or
promoted, and no learning files are touched") and whose four tools answer honestly — so the surface never lies
about a phase that *is* implemented but switched off. `enabled=False` means it constructs **no** ledger and
touches **no** disk.

Observation is hooked into the two places finished work already lands:

* `_remember_chat(...)` → `await asyncio.to_thread(self.learning.observe_chat, result, …)`
* `_remember_run(agent, goal, run, ...)` → `await asyncio.to_thread(self.learning.observe_run, run, …)`

Both run **off the event loop** (`to_thread`) and *before* the memory `None`-check, so a finished chat, browser
run or computer run is observed by memory **and** learning from the same hook. The loop's own writes are bounded
and synchronous; nothing in the observation path can block a reply.

**Gateway** (`gateway/api.py`, now **34 routes**): `GET /api/v1/learning` (the snapshot), `POST /api/v1/learning`
(record feedback; a stated preference promotes), `POST /api/v1/learning/cycle` (one sweep). `StarApplicationProtocol`
gained `learning_snapshot` / `learning_feedback` / `learning_cycle`. A bad `signal` is a `422`, not a `500`.

---

## 9. Surface: gateway + console (`Frontend/` still untouched)

The ops console (`gateway/console.py`) gained a **Learning** tab (after Memory): the six gates and the
anti-self-modification flag, the candidate ledger (kind/state/evidence/reasons), the recent feedback log, the last
cycle and last promotion, a **feedback form** (`signal`/`key`/`value`/`note`) and a **Run cycle** button. The
disabled state is explained in the UI (`STAR_LEARNING_ENABLED`). The emitted JavaScript passes `node --check`
(guarded by `test_console_javascript_is_syntactically_valid`), and the console pulls **no** external resource.

`git diff main -- Frontend/ Backend/bridge.py run.py run.bat agent/` is **empty** — the visible Star is byte-identical
to `main`; Phase 9 is backend-only behind the gateway, exactly as the zero-touch rule requires.

---

## 10. Configuration (`STAR_LEARNING_*`, all optional, documented in `.env.example`)

| Variable | Default | Meaning |
| --- | --- | --- |
| `STAR_LEARNING_ENABLED` | `true` | `false` ⇒ the loop is still built (reports `off` honestly) but constructs no ledger, observes nothing, promotes nothing and touches **no disk** |
| `STAR_LEARNING_AUTO_PROMOTE` | `true` | validate-and-promote inline on observation/feedback; `false` ⇒ accumulate only, promote on an explicit `cycle()` |
| `STAR_LEARNING_MIN_FREQUENCY` | `3` | sightings a **pattern** needs (a **preference** needs 1) |
| `STAR_LEARNING_MIN_SUCCESS_RATE` | `0.8` | success/approval rate a candidate needs over its decided evidence |
| `STAR_LEARNING_MAX_RISK` | `medium` | risk ceiling — a pattern reaching `high`/`critical` is refused |
| `STAR_LEARNING_MAX_CANDIDATES` | `500` | ledger bound; oldest terminal candidates evicted first |
| `STAR_LEARNING_MAX_PROMOTIONS` | `8` | promotions per cycle (bounded sweep) |
| `STAR_LEARNING_REQUIRE_APPROVAL` | `true` | a preference needs explicit positive feedback and no rejection |
| `STAR_LEARNING_TIMEOUT_S` | `4.0` | bound on the loop's blocking calls |

`STAR_CANDIDATES_PATH` and `STAR_FEEDBACK_PATH` (default `data/learning/candidates.jsonl` and
`data/learning/feedback.jsonl`, both **gitignored**) say *where* the staging ledgers live. No secrets, no new
dependencies.

---

## 11. Verification

```
pytest tests/ --ignore tests/test_hypothesis.py     → 736 collected · 735 passed · 1 failed (pre-existing
                                                      test_command_router.py::test_google_search, Phase 0 baseline)
pytest tests/test_star2_learning.py                  → 141 passed
python scripts/smoke.py                              → 65 passed, 0 failed   (was 54 in Phase 8)
node --check <emitted console.js>                    → OK
git diff main -- Frontend/ Backend/bridge.py run.py run.bat agent/   → empty
```

Smoke checks added (11): the loop + its six gates + `no_self_modification` over HTTP · four risk-tiered learning
tools · `learning_status` through the executor (read-only, audited) · `learning_list_candidates` filterable by
kind · a stated preference promoted into the **one** preference store (then cleaned up) · a bad signal → `422` ·
**a credential-named candidate rejected by the `no_secret` gate with nothing stored** · `POST …/cycle` → a bounded
sweep · `learning_cycle` through the executor · `learning.observed/feedback/promoted` on the mission stream with
no `_unknown_kind` · learning health reporting the two stores it borrows.

Test hygiene: the five app-building fixtures (`test_star2_gateway/browser/computer/workspace/frontend_adapter`)
now also redirect `STAR_CANDIDATES_PATH` / `STAR_FEEDBACK_PATH` to `tmp_path` (the Phase 8 lesson applied up
front), and `data/learning/` is gitignored, so no learning ledger can leak into the repo or into another test.

---

## 12. Honest limitations

1. **Evidence is co-occurrence, not causation.** A pattern is "these two tools were adjacent and the run
   succeeded", which is exactly what the Phase 3 `PatternStore` already counts. The loop reinforces that count;
   it does not infer *why* the pair worked, and it never claims a causal rule.
2. **Lexical risk classification.** The risk gate uses the registry's keyword classifier (`classify_risk`), so an
   unfamiliar tool defaults to `medium`. That is safe (it fails toward refusal, and a broken classifier fails
   closed to `high`), but it is not a semantic understanding of what a tool does.
3. **Preferences need a human yes.** By design a preference cannot be inferred from repetition — only explicit
   positive feedback approves one. That is the safe choice, but it means Star will not silently adopt a habit the
   user never confirmed.
4. **No decay or forgetting.** A rejected candidate stays rejected and an observing candidate keeps its evidence;
   nothing expires old candidates beyond the bounded eviction. A retention/decay policy belongs with Phase 11's
   budget work.
5. **Promotion is bounded, not queued.** `MAX_PROMOTIONS` caps a single cycle; candidates beyond the cap simply
   wait for the next cycle rather than being dropped, but there is no priority ordering among equally-qualified
   candidates beyond recency.
6. **The loop is single-process staging.** The ledgers are append-only JSONL with last-wins replay — correct for
   one desktop process, not a multi-writer database. That matches Star's modular-monolith scope.

---

## 13. Commit

```
feat(star-2.0): phase 9 — the safe learning loop (observation → candidate → six gates → promotion) over the existing stores
tag: star-2.0-phase-9
```

Next: **Phase 10** — advanced orchestration: multi-agent routing, checkpoints, recovery and cancellation, so a
plan that spans several agents can be paused, resumed and recovered without losing the work the learning loop has
already observed.
