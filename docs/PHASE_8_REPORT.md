# STAR 2.0 — PHASE 8 REPORT
### Layered memory: working · episodic · semantic · preferences · procedural patterns — over the stores Star already had

*Branch `star-2.0` · base `ec0b87f` (`star-2.0-phase-7`) · blueprint §9 "Memory & Learning" + §7 Phase 8*

> **Blueprint §9:** *"Memory layers: **Working memory** (current session/task context), **Episodic memory**
> (past interactions), **Semantic memory** (extracted knowledge), **User preferences**, **Procedural patterns**
> (how a task is usually performed). Safe learning loop: interaction → observation → feedback → candidate
> pattern → validation → memory update → future retrieval. **No uncontrolled self-modification.**
> Prediction must not bypass permissions."*
> **Blueprint §7 Phase 8:** *"Build the memory layer: working, episodic, semantic, preferences retrieval.
> **Inspect existing implementation before introducing another memory store.**"*
> **Done when:** working / episodic / semantic / preferences retrieval works. **Acceptance:** memory retrieves
> relevant context.

---

## 1. What was built (4 new modules + a package init, 2 125 lines + 1 482 lines of tests)

| Module | Lines | Responsibility |
| --- | --- | --- |
| `Backend/star/memory/layers.py` | 635 | the five layers as **adapters** over existing stores: `WorkingMemory`, `EpisodicLayer`, `SemanticLayer`, `PreferenceLayer`, `ProceduralLayer` + `token_overlap` |
| `Backend/star/memory/retrieval.py` | 147 | `hybrid_retrieve()` — parallel, timeout-bounded recall from every layer; `blend()` — weight, dedupe, cap, rank; `RetrievalTrace` |
| `Backend/star/memory/manager.py` | 877 | `MemoryManager` — the one object the brain, gateway, tools and event bus talk to: retrieval, writes, views, `snapshot()`, `describe()`, `health()`, sync/async twins |
| `Backend/star/memory/tools.py` | 394 | 7 typed `ToolSpec`s + `MemoryToolkit` |
| `Backend/star/memory/__init__.py` | 72 | lazy PEP 562 exports |

`tests/test_star2_memory.py` — **99 tests** (working 7 · episodic 7 · semantic/preferences 8 · procedural 4 ·
blending & hybrid retrieval 10 · manager state 9 · manager writes 11 · manager retrieval 10 · snapshot 4 ·
events 2 · tools 11 · application wiring 9 · gateway & console 3), all hermetic: every store lives in
`tmp_path`. Plus 14 new smoke checks (`scripts/smoke.py`, now **54**) and updates to 4 existing tests that
asserted Phase 8 was still `pending`.

**Suite: 595 collected · 594 passed · 1 failed** — the failure is `tests/test_command_router.py::test_google_search`,
the pre-existing baseline failure recorded in Phase 0 (legacy router renames `search_web` → `web_search`),
scheduled for Phase 12. **Smoke: 54 passed, 0 failed.**

---

## 2. No second memory store — what each layer actually adapts

The blueprint's instruction was explicit, so the package owns **zero** storage. Every layer is a thin adapter
over something that already existed and already worked:

| Layer | Existing implementation it adapts | Where it lives |
| --- | --- | --- |
| **working** | `brain.context.ContextBuilder` per-session scratch (`remember_turn` / `working_memory` / `forget_session`) | in process; **attached** to the brain after it is built, so there is exactly one copy of the conversation |
| **episodic** | `agent.planning.memory.EpisodicMemory` (JSONL episode log, `recall_similar` over succeeded episodes) | `STAR_EPISODES_PATH` |
| **semantic** | `agent.memory.store.MemoryStore` rows of kind `fact` (SQLite + hashing-trick embeddings, dedupe on write) | `STAR_MEMORY_DB` |
| **preference** | the *same* `MemoryStore`, rows of kind `preference`, latest value per key (`latest_by_key`) | `STAR_MEMORY_DB` |
| **pattern** (procedural) | `brain.prediction.PatternStore` — JSONL `tool → tool` counters the Phase 3 predictor already writes | `STAR_PATTERNS_PATH`, **read-only in Phase 8** |

Consequences worth stating:

* **One SQLite connection.** `build_application()` opens the store once (`open_memory_store`) and hands the same
  object to the memory manager *and* to `build_brain(..., store=…)`, so the brain and the layers never disagree
  and never double-open the file. Whoever opened it closes it; `sqlite3.close()` twice is a no-op.
* **One scratch buffer.** `memory.attach_working(brain.context_builder)` makes the working layer delegate. The
  layer keeps a private bounded `deque` only as a fallback (no brain, or the brain's buffer raises) — and it
  **drops** its private copy on attach instead of running two buffers side by side.
* **One pattern store.** `memory.attach_patterns(brain.predictor.store)` borrows the brain's live instance, so
  what Phase 3's `learn_bridge` records is immediately visible to the procedural layer.
* **Episodes are not copied into SQLite.** `MemoryStore.ingest_episode()` exists, but calling it would write a
  second, verbose rendering of every interaction into the store, where `build_context_block()` would then feed
  `"Episode ep_…: goal='…' steps=[…]"` to the model. One episode, one home: the JSONL log.

---

## 3. Hybrid retrieval — one ranked answer from five stores that disagree about scores

The layers do not speak the same language: semantic returns an embedding cosine, episodic a token overlap,
preferences a key/text match, patterns a conditional frequency, working a recency-weighted overlap. Blending raw
numbers would be meaningless, so `hybrid_retrieve()`:

1. asks **every layer in parallel** (`asyncio.to_thread` + a per-layer `STAR_MEMORY_TIMEOUT_S`), because SQLite
   and JSONL reads are blocking and one slow store must not stall a reply;
2. clamps each candidate's relevance to `[0, 1]` and multiplies it by that layer's **weight** — the only place
   where one layer is allowed to outrank another, and it is visible in `describe()`, `snapshot()` and the console;
3. **dedupes** on normalised text, keeping the best score (the same fact can be reachable from two layers);
4. applies a **per-layer cap** (`ceil(limit / 2)`) so one loud layer cannot monopolise the answer, then fills any
   leftover slots from the deferred candidates so good matches are never wasted;
5. returns a **trace** — candidates per layer, duplicates dropped, timeouts, errors — which the manager publishes
   as `memory.retrieved`. A dead layer is reported, never hidden, and never fatal.

Default weights (`.env`-tunable): working `1.00` · preference `0.95` · episodic `0.85` · semantic `0.80` ·
pattern `0.60`. `MemoryHit.score` is hard-bounded to `[-1, 1]` by the Phase 3 schema, and blending respects it.

A real retrieval from the demo run (one fact, one preference, one dry-run browser goal, one tool chat):

```
GET /api/v1/memory?q=desk lamp volume&limit=8
layers  working 4 · episodic 2 · semantic 1 · preference 1 · pattern 0
  semantic    0.487  fact     Nilanjan's desk lamp is on the left of the monitor
  episodic    0.442  episode  volume 40 koro → set_volume
  working     0.357  turn     conversation: volume 40 koro → done
  working     0.275  turn     volume 40 koro
  working     0.188  turn     star: ভলিউম একদম তোমার কথামতো 40% এ সেট করে দিয়েছি…
  working     0.062  turn     browser: [dry-run] read https://example.com → done
trace   candidates 6 · duplicates 0 · timeouts [] · errors [] · returned 6
```

The preference does **not** appear: "desk lamp volume" shares no token with `reply_language = bn`, and inventing
relevance is worse than returning less.

---

## 4. The manager — one face, three callers, honest failure

`MemoryManager` satisfies the Phase 3 `MemoryRetriever` protocol (`name` / `available()` / `retrieve()` /
`digest()`), so `build_brain(..., retriever=manager)` drops it in and **memory is still retrieved before the
brain plans** — the blueprint's Phase 3 invariant, now fed by five layers instead of one.

| Caller | Entry point | Notes |
| --- | --- | --- |
| brain (`ContextBuilder.build`) | `await retrieve(text, limit=6)` + `await digest(text, limit=6)` | `digest()` reuses the hits from the `retrieve()` that immediately preceded it (same query), so the pair costs one retrieval, and it appends the layers the store block cannot know: `(working)`, `(episodic)`, `(pattern)` |
| gateway / console | `memory_snapshot()` (sync) and `memory_snapshot_async()` | `GET /api/v1/memory` now prefers the async variant, so the event loop is never blocked by SQLite |
| tools | `search()`, `remember_now()`, `set_preference_now()`, `record_episode_now()`, `forget_now()` | sync twins, because the Phase 4 executor runs handlers with `asyncio.to_thread` — a worker thread has no loop, so `_run_sync()` uses `asyncio.run` there and a private 2-worker executor when a loop *is* running |
| app lifecycle | `startup()` / `aclose()` / `close()` | idempotent; closes only the store it opened |

Writes are routed by `kind`, and every route is explicit:

| `remember(kind=…)` | Layer | Behaviour |
| --- | --- | --- |
| `fact`, `semantic`, `knowledge`, … | semantic | `MemoryStore.remember(kind=FACT)` — deduped by the store |
| `preference`, `pref`, `habit` | preference | needs a key; when none is given one is derived from the first four tokens (`ami bengali te kotha bolte chai` → `ami_bengali_te_kotha`) and returned in the response |
| `episode`, `interaction`, `run` | episodic | builds an `Episode` (goal + `PlanStep`s) and appends it to the JSONL log |
| `working`, `turn`, `scratch` | working | one bounded turn in the session scratch |
| `pattern`, `procedural` | — | **refused on purpose**: "procedural patterns are learned from finished runs, not written by hand (Phase 9 validates candidates)" |
| anything unrecognised | semantic | a fact the user asked Star to keep is better stored than dropped because the label was unfamiliar |

Failures are reported, never raised: a locked database, a full disk or an unwritable episode log becomes
`{"ok": false, "layer": …, "error": …}` plus a `memory.failed` event, and retrieval continues with the layers
that answered. `health()` returns `ok` / `degraded` (some layers have no backing store) / `off`
(`STAR_MEMORY_ENABLED=false`), and `describe()` names the store behind every layer — so "what does Star remember
and where" is always answerable from the API.

**Events** (all three are official `EVENT_KINDS`, published with `phase=EventPhase.MEMORY`):

* `memory.updated` — every write, with `layer`, `action` (`remember` / `preference_set` / `episode` / `forget` /
  `forget_session` / `attached` / `startup`), ids and latency;
* `memory.retrieved` — query, candidates, returned, duplicates, timeouts, errors, top hit, latency;
* `memory.failed` — a store refused or timed out (added to `EVENT_KINDS` this phase).

---

## 5. Tools — seven typed specs, one ladder

The legacy registry already exposes `remember_fact` and `recall_memory` (both `origin="legacy"`, untouched), and
the application now records episodes by itself after every run — so Phase 8 adds only what nothing else offered.
Handlers are **sync** (the executor threads them) and every answer carries a one-line `output` the brain can say
out loud.

| Tool | Risk | What it does |
| --- | --- | --- |
| `memory_recall` | low | one blended search across all five layers + the trace (`layers_used`, `candidates`, `duplicates_dropped`, `layer_timeouts`, `layer_errors`) |
| `memory_list_preferences` | low | latest value per key |
| `memory_list_episodes` | low | recent interactions with `succeeded` / `failed` counts |
| `memory_read_working` | low | the session's last turns + capacity + whether it is delegated to the brain |
| `memory_status` | low | layers, backing stores, weights, limits, counters, last retrieval trace, `no_second_store: true` |
| `memory_preference_set` | medium | store a user-approved habit (`set_` ⇒ a mutation, but of Star's notes, never of the OS) |
| `memory_forget` | **high**, `reversible=False` | delete one record by id — above `STAR_CONFIRM_ABOVE_RISK`, so it stops at a confirmation and only really deletes after a human yes; simulated while dry-run is on |

The read tools are named so the Phase 4 keyword classifier *agrees* with their declared tier (`recall`, `list_`,
`read_`, `status` ⇒ low) instead of defaulting an innocuous read to `medium`. The ladder is unchanged and applies
in full: `highest(spec.risk, classify_risk(name, args))` → deny tier → shell policy → rate limit → confirmation →
dry-run. Verified end-to-end in the smoke run:

```
memory_recall {query:"smoke desk lamp"}      → executed (dry_run=false) · 5 hits · layers preference,semantic,working
memory_forget {memory_id:166}                → high · needs_confirmation · record still present
POST /api/v1/confirmations {approve:true}    → executed · removed true · record gone · audited
memory_recall (global dry-run)               → simulated · handler never called
memory_recall {}                             → invalid_args: missing required argument 'query'
memory_forget {memory_id:"abc"}              → invalid_args: argument 'memory_id' is not a valid integer
emergency stop active                        → denied before the handler
```

**Prediction ≠ permission.** The procedural layer only *reports* what co-occurred; nothing in this package can
approve a tool call, lower a risk tier or bypass the governor. Writing patterns by hand is refused; Phase 9 owns
candidate validation.

---

## 6. Wiring — memory now happens in the flow, not beside it

`Backend/star/main.py` (+108 lines):

* `build_application()` builds the memory slot **before** the browser/computer/brain slots, opens the store once,
  registers the seven tools, then passes `retriever=memory` and `store=memory.store` to `build_brain()`. After the
  brain exists it calls `attach_working(brain.context_builder)` and `attach_patterns(brain.predictor.store)`.
* `chat()` → `_remember_chat()`: the reply joins working memory as `star: …`, and the exchange becomes an episode
  **only when Star actually called a tool** — small talk is not an episode, otherwise episodic memory fills with
  noise and stops being a record of what Star did.
* `run_browser_goal()` / `run_computer_goal()` → `_remember_run()`: one episode per finished run, built from the
  run's real `AgentStep`s (skipped steps excluded), with `succeeded` from the run state. A simulated run is stored
  as `"[dry-run] <goal>"` so the log never claims Star did something it only rehearsed.
* `memory_snapshot()` stays sync for the protocol; `memory_snapshot_async()` was added and `gateway/api.py`
  prefers it.
* The `("memory", …)` slot is no longer `pending`: `capabilities()` now really includes `memory` (14 capabilities),
  `health()["checks"]["memory"]` reports the layer map, and `_phase_pending(8, …)` only survives for callers that
  inject `memory=None`.

---

## 7. Surface: gateway + console (`Frontend/` still untouched)

`git diff main -- Frontend/ Backend/bridge.py run.py run.bat agent/` is **empty** — the visible Star and the
legacy agent core are byte-identical to `main`, as the rule requires. Everything landed in `Backend/star/`.

No new routes were needed: `GET/POST /api/v1/memory` existed since Phase 1 and were waiting for this slot. The
console's **Memory** tab now renders what the snapshot actually carries:

* layer table with `items`, **weight**, state pill, backing store and detail (e.g. *"300-record scan window ·
  2 written here"*);
* preferences (key / value / updated), retrieval results (layer / text / score / kind) **plus the trace line**
  (candidates · returned · duplicates dropped · timeouts · errors);
* recent episodes (when / goal / steps / outcome), the working buffer for the active session, and the procedural
  `tool → tool` pairs;
* an explicit banner when `STAR_MEMORY_ENABLED=false`, saying that retrieval and writes are refused and that
  **nothing stored is deleted**.

### A real bug found and fixed while extending the tab

The console's entire `<script>` failed to parse. Phase 7 had added

```python
window.confirm('Delete this session\'s files? …')      # inside a Python """…""" string
```

Python turns `\'` into `'`, so the browser received `'Delete this session's files? …'` — a syntax error that
kills **every** tab, not just the workspace one. It survived Phase 7 because the smoke test asserted on HTML
substrings, never on JavaScript validity. Fixed by using double quotes in JS, and now guarded by
`test_console_javascript_is_syntactically_valid`, which runs `node --check` over the emitted script (skips when
node is absent) plus a pure-Python regression assertion. `node --check` passes.

Also completed the event vocabulary: `memory.failed` (new this phase) and seven Phase 7 workspace kinds
(`workspace.closed/restored/expired/blocked/write/used/spawn`) were being emitted but not declared in
`EVENT_KINDS`, so they went out flagged `_unknown_kind`. All are official now, and the smoke test asserts that no
event on the stream carries `_unknown_kind`.

---

## 8. Configuration (`STAR_MEMORY_*`, all optional, documented in `.env.example`)

| Variable | Default | Meaning |
| --- | --- | --- |
| `STAR_MEMORY_ENABLED` | `true` | `false` ⇒ the manager is still built (so the console and tools report `off` honestly) but touches **no disk**, retrieves nothing and refuses writes |
| `STAR_MEMORY_WORKING_SESSIONS` | `32` | how many sessions keep scratch |
| `STAR_MEMORY_WORKING_TURNS` | `8` | turns kept per session |
| `STAR_MEMORY_RETRIEVAL_LIMIT` | `8` | blended hits handed to the brain |
| `STAR_MEMORY_PER_LAYER` | `5` | candidates gathered per layer |
| `STAR_MEMORY_EPISODE_RECALL` | `5` | similar past interactions recalled |
| `STAR_MEMORY_RECENT_SCAN` | `300` | window used for layer counts and the preference list |
| `STAR_MEMORY_TIMEOUT_S` | `4.0` | bound on every blocking store call |
| `STAR_MEMORY_WEIGHT_{WORKING,PREFERENCE,EPISODIC,SEMANTIC,PATTERN}` | `1.0 / 0.95 / 0.85 / 0.8 / 0.6` | the only place one layer may outrank another |

`STAR_MEMORY_DB`, `STAR_EPISODES_PATH` and `STAR_PATTERNS_PATH` already existed and are still the single source of
truth for *where* memory lives. No secrets, no new dependencies.

---

## 9. Verification

```
pytest tests/ --ignore tests/test_hypothesis.py     → 595 collected · 594 passed · 1 failed (pre-existing
                                                      test_command_router.py::test_google_search, Phase 0 baseline)
pytest tests/test_star2_memory.py                   → 99 passed
python scripts/smoke.py                             → 54 passed, 0 failed   (was 40 in Phase 7)
node --check <emitted console.js>                   → OK
git diff main -- Frontend/ Backend/bridge.py run.py run.bat agent/   → empty
```

Smoke checks added (14): five layers reported with their stores · no-second-store assertion · fact write ·
preference write · blended ranked retrieval with in-range scores · preference listing · dry-run episodes recorded
and labelled · working memory delegated to the brain · seven tools with the right risk tiers · `memory_recall`
through the executor · `memory_forget` stopping at a confirmation · approval really deleting (and the smoke
records cleaned up afterwards) · `memory.updated`/`memory.retrieved` on the stream with no `_unknown_kind` ·
memory health green.

Test hygiene fixed on the way: four star2 test files built the application without redirecting the memory paths,
so Phase 8 would have written episodes and facts into the **repo root** (`episodes.jsonl` had already grown to
184 KB / 36 test episodes, and `EpisodicMemory` loads that file eagerly at startup — test data leaking into
retrieval). `tests/test_star2_gateway.py`, `test_star2_frontend_adapter.py` and `test_star2_browser.py` now set
`STAR_MEMORY_DB` / `STAR_EPISODES_PATH` / `STAR_PATTERNS_PATH` to `tmp_path`. After the fix the star2 suite is
hermetic; the remaining repo-root `episodes.jsonl` / `star_memory.db` writes come from the **legacy** agent tests,
which use those default paths by design (both files are gitignored).

---

## 10. Honest limitations

1. **Retrieval is lexical, not semantic.** The reused `MemoryStore` scores with hashing-trick embeddings and the
   other layers use token overlap, so "desk lamp" will not surface "the light on my table". Nothing was invented
   here on purpose — a real embedder is a drop-in via `MemoryStore(embedder=…)` and belongs to Phase 12 tuning.
2. **Layer counts come from a bounded scan.** `MemoryStore.count()` returns *all* kinds, so semantic/preference
   counts are computed over the last `STAR_MEMORY_RECENT_SCAN` (300) records; the `detail` string says so instead
   of pretending to be exact.
3. **Episodes are recalled only when they succeeded.** That is `EpisodicMemory.recall_similar`'s existing rule and
   it is right for "how did I do this before", but it means failures are visible in the console list and not in
   retrieval. `memory_list_episodes` shows both.
4. **The procedural layer is read-only.** Nothing validates or promotes candidate patterns yet; that is Phase 9's
   safe learning loop. Hand-written patterns are refused.
5. **Working memory is per session and in process.** It is not persisted, and with no brain attached it falls back
   to a bounded `deque` — a restart loses the scratch, by design.
6. **`STAR_MEMORY_ENABLED=false` disables the layered manager, not the legacy store.** The brain still opens its
   Phase 3 `MemoryStore` and the legacy `remember_fact` / `recall_memory` tools keep working; the flag turns off
   Phase 8 retrieval, writes, tools and disk use, and says so in `health()`.
7. **No forgetting policy.** Nothing expires or compresses old facts and episodes; `memory_forget` (confirmed) and
   `forget_session` are manual. Retention belongs with Phase 11's security/budget work.

---

## 11. Commit

```
feat(star-2.0): phase 8 — layered memory (working/episodic/semantic/preferences/patterns) over the existing stores
tag: star-2.0-phase-8
```

Next: **Phase 9** — learning from experience: the safe loop
`interaction → observation → feedback → candidate pattern → validation → memory update → future retrieval`,
promoting validated candidates into the `PatternStore` the procedural layer already reads, with
**no uncontrolled self-modification** and no bypass of the permission ladder.
