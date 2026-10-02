# STAR 2.0 — PHASE 5 REPORT
### Browser agent: guardrailed URLs · stdlib fetch/extract · typed browser tools · OBSERVE→ACT→VERIFY→RECOVER

*Branch `star-2.0` · base `star-2.0-phase-4` · blueprint §7 Phase 5 (+ §4 agents-vs-tools)*

> **Phase 5 requirements:** "Add a browser agent that can open pages, read content, extract links and
> perform simple web actions. Every browser action must go through the tool registry and the
> permission layer. Add guardrails: no `file://`, no private hosts by default, bounded fetches,
> dry-run first."
>
> **Blueprint §4:** *"Agents are goal-oriented workers… Tools are capabilities. The orchestrator
> sequences agents."*

---

## 1. What was built (6 new modules, 2 295 lines + 731 lines of tests)

| Module | Lines | Responsibility |
| --- | --- | --- |
| `Backend/star/browser/guardrails.py` | 305 | `BrowserGuardrails.check_url()` → `UrlVerdict(ok, rule, reason, risk)`; `find_urls()`, `is_private_name()` |
| `Backend/star/browser/extract.py` | 363 | `fetch_url()` (bounded GET, redirect cap, guard re-checked per hop) + `extract_page()` / `read_page()` (stdlib HTML → text-only observation) |
| `Backend/star/browser/tools.py` | 527 | 8 typed browser `ToolSpec`s + `BrowserToolkit` (legacy reuse, honest unavailability, one normalised result shape) |
| `Backend/star/browser/agent.py` | 443 | `BrowserAgent` — goal → bounded plan → OBSERVE/ACT/VERIFY/RECOVER; `build_browser()` factory |
| `Backend/star/agents/base.py` | 550 | `StarAgent` (the loop every agent shares), `AgentStepPlan` / `AgentStep` / `AgentRun`, `AgentRegistry` |
| `Backend/star/{browser,agents}/__init__.py` | 107 | lazy PEP 562 exports — importing the packages stays cheap |

`tests/test_star2_browser.py` — **44 tests**, hermetic: all network tests hit a local
`http.server` on an ephemeral port with `allow_private_hosts` enabled *for that test only*.

---

## 2. Guardrails — the URL policy nothing talks its way past

Checked on every step, in this order (the order matters, so an SSRF attempt reports `private_host`
and not merely "wrong port"):

| # | Rule | Refusal (`rule`) |
| --- | --- | --- |
| 1 | non-empty, ≤ 2048 chars | `empty` / `length` |
| 2 | scheme ∈ `http, https` (`file:`, `javascript:`, `data:`, `ftp:`, `about:` all refused — with or without `//`) | `scheme` |
| 3 | no embedded credentials (`https://user:pass@host`) — they would land in logs and history | `userinfo` |
| 4 | valid host, valid IDNA | `host` / `idna` |
| 5 | host deny list (exact **and** suffix: `bad.example` blocks `sub.bad.example`) | `denylist` |
| 6 | host allow list (when set, everything else is refused) | `allowlist` |
| 7 | private/loopback/link-local/multicast/reserved **IP literals** (`127.0.0.1`, `10.x`, `192.168.x`, `169.254.169.254`) | `private_host` |
| 8 | private **hostnames** — `localhost`, `*.local`, `*.lan`, `*.internal`, `metadata.google.internal` — caught without DNS, because the DNS check is off by default | `private_host` |
| 9 | optional `resolve_hosts=true`: every address a name resolves to must be public (SSRF hardening); unresolvable ⇒ refused | `dns` |
| 10 | explicit port must be in `80, 443` (configurable) | `port` |

An allowed URL still carries a `risk`: an IP literal or a non-default port makes it `medium`, which
feeds Phase 4's `classify_risk` (so it can trip the confirmation gate). Every refusal emits the
existing `safety.blocked` event — no new protocol vocabulary was invented for it.

Fetches are bounded the same way: `timeout_s=8`, `max_bytes=2 000 000` (with a `truncated` flag),
`max_redirects=3`, and **the guard is re-run on every redirect hop**, so `https://ok.example/`
cannot 302 the agent into `http://169.254.169.254/latest/meta-data`.

---

## 3. Tools — the browser has no private execution path

All 8 capabilities are registered as typed Phase 4 specs (registry now holds **38 tools**:
30 legacy + 8 browser), so each inherits validation → permissions → confirmation → audit → dry-run:

| Tool | Risk | `dry_run_safe` | Notes |
| --- | --- | --- | --- |
| `browser_open` | medium | ✓ | guardrails → legacy `web_open_url` when present, else `webbrowser.open` |
| `browser_read` | low | ✓ | guardrails → legacy `web_read_page` when present, else stdlib `read_page` |
| `browser_links` | low | ✓ | absolute URLs + anchor text, de-duplicated, capped |
| `browser_search` | low | ✓ | engine URL (google/duckduckgo/bing/youtube/wikipedia); opening obeys `STAR_BROWSER_AUTO_OPEN` |
| `browser_close_tab` | medium | ✓ | desktop only, **not reversible** |
| `browser_snapshot` | low | ✓ | delegates to the legacy vision tools |
| `browser_click` | **high** | ✗ | never faked — needs a real confirmation *and* desktop tooling |
| `browser_type` | **high** | ✗ | same |

Reuse before duplication (blueprint #1):

* Where a legacy desktop tool exists it **is** the implementation; the audit entry records it in
  `result.via` (`web_read_page`, `web_open_url`, `take_screenshot`, …).
* Legacy tools return inconsistent shapes (`web_read_page` is flat: `url`/`title`/`content`/
  `total_length`/`message`). `_wrap()` normalises every one of them to
  `{"success", "result": {…}, "error"?, "message"?}` and renames `content → text`,
  `total_length → text_chars`, so the agent's OBSERVE step reads exactly one shape regardless of
  which implementation ran. The legacy Bengali `message` survives as the tool `output`.
* Where the desktop tool cannot load (this sandbox: no `pyautogui`, and the legacy browser-control
  module is Windows-only) the tool says *"not available here"* instead of pretending — and the agent
  turns that into an honest `failed` run rather than a fake success.

---

## 4. The agent spine (`Backend/star/agents/base.py`)

`StarAgent.run(goal)` is the blueprint's loop, implemented once:

```
OBSERVE  plan(goal) → bounded AgentStepPlan list (side-effect free, dry-run previewable)
  for each step (budget = STAR_BROWSER_MAX_STEPS, checked against the stop gate first):
    ACT      executor.call(tool, args, ids…)      ← policy, confirmation, audit, dry-run
    OBSERVE  observe(plan, outcome, context)      ← raw result → the few facts that matter
    VERIFY   verify(plan, observation, step)      ← expected vs observed, with a reason string
    RECOVER  recover(...)                        ← ≤ 2 honest attempts, then the truth
state → done | dry_run | failed | blocked | budget_exhausted | no_plan
events → agent.started · agent.step · agent.completed / agent.failed
```

Rules that keep it honest:

* **A simulated step is a verified step, with a note that says so** — `"simulated in dry-run —
  nothing was really fetched or clicked"`. The run state becomes `dry_run`, never `done`.
* **A guardrail refusal is `blocked`, not `failed`** (`observation.blocked_by_policy` → the run
  state), because policy refusing is the system working, not breaking.
* **`needs_confirmation` never auto-recovers** — it stops and reports the confirmation id.
* **Recovery is bounded** (2 per run) and specific: a window that cannot be opened falls back to
  *reading* the URL; an unreadable page falls back to one of its own links. Anything else reports
  "no recovery available".
* `startup()` is idempotent, because the app starts an agent both directly and via the registry.

`AgentRegistry` is the orchestrator's (Phase 10) view of the workers: `register` (duplicate ⇒
`ValueError` unless `replace=True`), `dispatch(goal)` by score, `describe()`, `runs()`, `health()`,
`startup()/aclose()`.

---

## 5. What the browser agent understands

Planning is deterministic, trilingual and refuses to guess:

| Goal | Plan |
| --- | --- |
| `open https://example.com and tell me what it says` | open *(optional)* → read |
| `read https://en.wikipedia.org/wiki/Star` | read |
| `links on example.com` | links |
| `search rust async book` | search |
| `wikipedia search কলকাতা` | search(engine=wikipedia) → read `bn.wikipedia.org/…` *(optional)* |
| `ওয়েবসাইটটা খুলে পড়ে শোনাও https://example.com` | open *(optional)* → read |
| `open file:///etc/passwd` | open → **guardrails refuse it explicitly** (never re-read as a search query) |
| `what is 2+2` | *no plan* — score 0, so the orchestrator picks another agent |

Keywords cover English, Banglish (`kholo`, `browse koro`, `padho`) and Bengali (`খুলে`, `পড়ো`,
`সার্চ`, `লিংক`, `ওয়েবসাইট`). The plan is *not* truncated to the budget — it reports the whole truth
and `run()` truncates, so a too-small budget ends as `budget_exhausted` instead of silently
pretending the goal was smaller.

---

## 6. Wiring

| File | Change |
| --- | --- |
| `Backend/star/main.py` | builds the browser agent after the executor (`build_browser(cfg, bus, executor, registry, stop_gate=security.stopped)`), fills the `agents` slot with an `AgentRegistry`, adds `browser` to capabilities/health/startup/aclose, and adds `browser_state()` + `run_browser_goal()` |
| `Backend/star/contracts.py` | the gateway protocol learns `browser_state()` / `run_browser_goal()` |
| `Backend/star/gateway/api.py` | `GET /api/v1/browser` (agent + guardrails + last run) and `POST /api/v1/browser` (`{"goal", "dry_run"?}`) — **24 routes**, additive only |
| `Backend/star/gateway/console.py` | new **Browser** tab: guardrail posture, URL-check counters, agent stats, a "Run goal" box and the last run's step table; the Agents tab now renders the real `describe()` shape. Still 100 % inline CSS/JS — no CDN, verified with `node --check` |
| `Backend/star/config/settings.py` | frozen `BrowserSettings` (14 fields) + `STAR_BROWSER_*` env parsing, incl. an `_ints()` helper for the port list |
| `Backend/star/observability/events.py` | the kind vocabulary now matches reality: 19 kinds the backend was already emitting (Phases 2–4) plus the 4 agent kinds are official instead of flagged `_unknown_kind` on the wire — **55 kinds** |
| `.env.example` | documented Browser section (14 keys, paranoid defaults) |
| `scripts/smoke.py` | 5 new live checks → **22 total** |

---

## 7. Verification (real runs, this sandbox)

```
POST /api/v1/browser {"goal":"read https://example.com"}            (dry-run default)
  → state=dry_run · step read/browser_read decision=simulated
    note="simulated in dry-run — nothing was really fetched or clicked"

POST /api/v1/browser {"goal":"read https://example.com","dry_run":false}
  → state=done · decision=executed · title="Example Domain" · 128 chars · via=star.extract
    audit: tool.decision browser_read executed persisted=true

POST /api/v1/browser {"goal":"links on https://example.com","dry_run":false}
  → state=done · summary="listed 1 link(s) on https://example.com"

POST /api/v1/browser {"goal":"open file:///etc/passwd","dry_run":false}
  → state=blocked · "blocked by browser guardrails (scheme): scheme 'file' is not allowed"

POST /api/v1/browser {"goal":"open http://169.254.169.254/latest/meta-data","dry_run":false}
  → state=blocked · rule=private_host          (cloud metadata SSRF, refused)

GET  /api/v1/browser  → ok · agent=browser · tools=8 · schemes=[http, https]
GET  /api/v1/agents   → [{name: browser, …}]
GET  /api/v1/tools    → 38 tools (30 legacy + 8 browser)
```

Local-server runs (hermetic fixture) additionally prove: real title/text/headings/link extraction,
Bangla text preserved, `<script>`/`<style>` content never leaking into the observation, byte-cap
truncation, 404 handling, redirect counting and a redirect into a private address being refused.

---

## 8. Test results

```
tests/test_star2_browser.py    44 passed   guardrails · extract · tools · agent · registry · wiring
tests/test_star2_gateway.py    21 passed   (+4 new: browser GET/POST, validation+stop, route table = 24)
full suite (--ignore=tests/test_hypothesis.py)   395 passed, 1 failed
scripts/smoke.py               22 passed, 0 failed
```

The one failure is still the pre-existing baseline `test_command_router.py::test_google_search`
(`search_web` vs `web_search` naming) — Phase 12. `tests/test_hypothesis.py` stays uncollectable in
this sandbox (`hypothesis` not installed).

---

## 9. Non-negotiables checklist (blueprint §3)

| # | Rule | Phase 5 evidence |
| --- | --- | --- |
| 1 | inspect before changing | legacy `web/*` tools read first; their implementations are *reused*, and the flat/nested result mismatch is normalised rather than reimplemented |
| 2 | every action passes policy | the agent has no I/O of its own — every hop is a `ToolExecutor.call` (validated, permission-checked, audited) |
| 3 | dry-run default | `STAR_DRY_RUN=true` ⇒ `browser_read` never touches the network (proven by monkeypatching `fetch_url` to explode) |
| 4 | tests for new core modules | 44 new tests, 731 lines, hermetic |
| 5 | no hard-coded secrets | URLs with embedded credentials are refused outright; only env config |
| 6 | modular monolith | `browser/` + `agents/` packages, lazy exports, no new third-party dependency |
| 7 | explicit confirmation | `browser_click` / `browser_type` are high-risk and `dry_run_safe=False` ⇒ confirmation required, never simulated |
| 8 | OBSERVE→ACT→VERIFY→RECOVER | implemented once in `StarAgent.run`, with a bounded recovery budget |
| 9 | visible UI independent of automation | HUD untouched; the ops console gained a read-only Browser tab plus one explicit "Run goal" action |
| 10 | audit everything | every browser hop writes the same audit entry as any other tool call, incl. refusals |

---

## 10. Known limits (honest list)

1. **No real DOM.** Reading is HTTP + `html.parser`: no JavaScript rendering, no session/cookie
   jar, no in-page interaction beyond what the legacy desktop tools offer. A headless-browser
   backend (Playwright) is a later, additive `extract` implementation behind the same tools.
2. `browser_click` / `browser_type` are **unavailable in this sandbox** (no `pyautogui`, Windows-only
   legacy module). They are registered, risk-rated, confirmation-gated and tested for their honest
   "not available here" answer; Phase 6 (computer agent) is where real input actions land.
3. The brain does not yet auto-dispatch browser goals — `AgentRegistry.dispatch()` is ready and
   tested, and Phase 10's orchestrator is what sequences agents. Today the browser agent is reached
   through `POST /api/v1/browser`, `app.run_browser_goal()` or the console.
4. Search falls back to *returning the search URL* when the legacy `web_search` tool is not
   importable; parsing a SERP is deliberately not attempted (fragile, and against most ToS).
5. Rate limiting is inherited from the permission engine (per session/minute), not per host — a
   per-host politeness delay is Phase 12 optimisation work.

---

## 11. Next: exact Phase 6 scope (Computer agent)

1. `Backend/star/computer/` — screen/mouse/keyboard worker on the shared `StarAgent` spine,
   reusing `agent/motor/backends/*`, `agent/perception/*` and `agent/safety/*` (inspect first).
2. Tools: `screen_capture`, `screen_read` (OCR via the existing `see` skill), `mouse_move/click`,
   `keyboard_type`, `hotkey`, `window_focus` — all typed, all risk-rated, `dry_run_safe=False`
   for anything that moves the mouse or types.
3. **Dry-run first, always**: a simulated step records the exact intended coordinates/keys and the
   safety governor's invariants, and touches nothing.
4. Guardrails: the existing safety governor's 6 invariants re-checked through the Phase 4 permission
   engine; critical actions (`lock_workstation`, destructive keys) stay confirmation-gated.
5. VERIFY via screen diff (`agent/perception`) — expected vs observed pixels/text, recorded in
   `Verification`.
6. Gateway `POST /api/v1/computer` + a console tab; tests with a fake motor backend (no display
   needed) covering dry-run, confirmation, budget, verification and emergency stop.
