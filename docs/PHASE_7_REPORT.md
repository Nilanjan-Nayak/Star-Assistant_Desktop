# STAR 2.0 — PHASE 7 REPORT
### The invisible background workspace: jailed sessions · quotas · checkpoints · isolation seam

*Branch `star-2.0` · base `2605319` (`star-2.0-phase-6`) · blueprint §8 "Invisible / Background Desktop" + §7 Phase 7*

> **Blueprint §8:** *"The target is not simply minimizing a window. Star should have a controlled
> execution workspace separate from the visible Star UI."*
> VISIBLE = the existing Star frontend (conversation, task progress, result, optional activity
> indicator). BACKGROUND = isolated browser/session, screen capture, vision/perception,
> mouse/keyboard controller, **task checkpoint**, verifier.
> **FLOW:** `Voice → Plan → Permission → **Workspace** → Observe → Act → Observe → Verify → Recover/Complete → Voice response`
> *"For Windows, start with a workspace/session-manager abstraction. Do not hard-code one
> virtual-desktop technology into every agent… Keep dry-run as the development default."*

---

## 1. What was built (5 new modules, 1 486 lines + 723 lines of tests)

| Module | Lines | Responsibility |
| --- | --- | --- |
| `Backend/star/workspace/session.py` | 166 | `WorkspaceSession` (identity, jailed root, state, TTL, quota counters, notes), `Checkpoint`, `WorkspaceState`, `WorkspaceKind` |
| `Backend/star/workspace/isolation.py` | 213 | the isolation **seam**: `NullIsolation`, `ProcessIsolation` (opt-in `spawn`), `VirtualDesktopIsolation` (honest placeholder) + `build_isolation()` fallback chain |
| `Backend/star/workspace/manager.py` | 738 | `WorkspaceManager` — create/acquire/reuse, the **path jail**, quotas, TTL expiry, checkpoints, restore, close, verified destroy, events, audit |
| `Backend/star/workspace/tools.py` | 303 | 9 typed `ToolSpec`s + `WorkspaceToolkit` |
| `Backend/star/workspace/__init__.py` | 66 | lazy PEP 562 exports |

`tests/test_star2_workspace.py` — **40 tests** (session model 3 · isolation backends 6 ·
sessions/jail/quotas 11 · checkpoints 5 · close/destroy 7 · tools 5 · agents + app wiring 3),
hermetic: everything
happens inside a temporary `workspace_root`; the single spawn test starts `sys.executable -c pass`
and **waits for it**, so nothing is orphaned. Plus 4 new gateway tests (`tests/test_star2_gateway.py`,
now 30) and 11 new smoke checks (`scripts/smoke.py`, now **40**).

Nothing was duplicated: the workspace reuses `PathsSettings.workspace_root` (already the home of
`_profiles`), the Phase 4 registry/permission ladder/confirmation store/audit log, and the existing
event bus (`EventPhase.WORKSPACE` was already in the enum and the console's phase list).

---

## 2. The jail — the property everything else depends on

`WorkspaceManager.resolve(session, path, write=…)` is the only way a path enters a session:

| Attempt | Result |
| --- | --- |
| `../../outside.txt`, `notes/../../../x` | refused — `jail_escape` (critical path, `blocked_by_policy=True`) |
| absolute path outside the session (`/etc/passwd`) | refused — `jail_escape` |
| **symlink** inside the session pointing outside | refused — `jail_escape` (resolution uses `os.path.realpath`, so hops are followed and caught) |
| `_checkpoints/…` (the snapshot store) | refused — `reserved_path` |
| empty path / > 1024 chars | refused — `empty_path` / `path_length` |
| a session root that is *not* inside `STAR_WORKSPACE_ROOT` | refused — `jail_escape` (defence in depth, checked again before any delete) |

Every refusal is counted (`stats["escapes"]`), noted on the session, written to the audit log as a
`workspace.decision` entry, published as a `workspace.blocked` event, and it turns
`health()["status"]` **degraded** — an escape attempt is a security event, not an error to swallow.

Bounds around the jail:

* **sessions** — `max_sessions` (default 4) counts *alive* sessions (`active`/`idle`/`suspended`);
  `session_ttl_s` (default 3600) expires them, and expiry runs on startup and before every create, so
  a timed-out session frees its own slot.
* **files** — per session `max_files` (500) and `max_bytes` (50 MB), per write `max_write_bytes` (2 MB).
* **checkpoints** — `max_checkpoints` (5) per session, `checkpoint_max_bytes` (10 MB) per snapshot;
  when a cap bites, the snapshot is marked `truncated` and the note says so — caps are reported, never hidden.
* **processes** — `allow_spawn` is **false** by default; a spawn in dry-run only records the intent.

---

## 3. Isolation — an abstraction that admits what it cannot do

| `STAR_ISOLATION_BACKEND` | `available()` | What it really does |
| --- | --- | --- |
| `null` (default) | true | jail + quotas + checkpoints. `describe()` states plainly: *"directory jail + quotas only — the session is NOT hidden from the desktop by the OS"* |
| `process` | true | the same jail **plus** the declared intent that session work runs in its own OS process; `spawn(argv)` starts it with `cwd` inside the jail — only when `STAR_WORKSPACE_ALLOW_SPAWN=true` and dry-run is off, otherwise it refuses with `blocked_by_policy` |
| `windows_virtual_desktop` | **false** | Windows-only by design and *not wired to any vendor API yet*: it reports itself unavailable with the reason and `build_isolation()` falls back to `process`, then `null` |

That last row is the blueprint's instruction implemented literally: the seam exists, the agents never
learn which technology sits behind it, and swapping in a real virtual-desktop implementation later
touches `isolation.py` only. An unknown backend name falls back to `null` with a logged warning.

---

## 4. Checkpoints — the "task checkpoint" the blueprint asks for

```
manager.checkpoint(session_id, label)  →  <root>/_checkpoints/ckpt_<ts>_<rand>/
                                            ├── manifest.json  (id, label, created_at, files, bytes,
                                            │                   truncated, entries[{path, bytes, sha256_16}])
                                            └── <the session's files, same relative layout>
manager.restore(session_id, checkpoint_id="")   # "" ⇒ newest
```

* IDs stay unique even inside one second (`ckpt_<epoch>_<uuid6>`), and evicting the oldest snapshot
  deletes its directory — the store never silently grows past `max_checkpoints`.
* A checkpoint **suspends** the session (`state=suspended`); a restore brings it back to `active`.
  Both are honest about what they did (`restored`, `skipped`, `truncated`).
* Restore refuses with `unknown_checkpoint` / `missing_manifest` rather than guessing.
* Deleting a session's files (`remove_files=true`) refuses in dry-run, and in live mode it re-verifies
  that the target really is a session directory inside the workspace root **and** not a reserved
  `_`-prefixed sibling, before `shutil.rmtree`.

---

## 5. Tools — nine typed specs, one ladder

Registered into the Phase 4 registry (`agent="filesystem"`, `category="files"`, `origin="star2"`,
tag `phase7`), so they inherit validation, permissions, confirmation, audit and dry-run:

| Tool | Risk | Notes |
| --- | --- | --- |
| `workspace_list`, `workspace_state` | low | read-only views of sessions / manager |
| `workspace_read`, `workspace_files` | low | jailed reads, length-limited |
| `workspace_checkpoint` | low | a snapshot only ever *adds* files |
| `workspace_create`, `workspace_write`, `workspace_restore` | medium | mutate inside the jail |
| `workspace_close` | **medium → high** | closing keeps files (medium); `remove_files=true` raises it to **high** ⇒ explicit human approval, and dry-run only records the intent |

**Why the argument is `remove_files` and not `destroy`:** Phase 4's `classify_risk` inspects the tool
name and argument *keys*, and the bare key `destroy` is on its `critical` list — with the default
`STAR_DENY_RISK=critical` that would make routine session cleanup **impossible** (denied, not
confirmed). Deleting a jailed scratch directory the workspace itself created is honestly *high*, not
*critical* (`critical` is `format`/`wipe`/`rm -rf`/registry territory). The ladder computes
`highest(spec.risk, classify_risk(name, keys))`, so the observed behaviour is:

```
POST …/close {}                    → risk medium → executed, files kept
POST …/close {"remove_files":true} → risk high   → needs_confirmation → approve → destroyed
                                                 → deny    → denied, directory untouched
```

One invariant the toolkit documents and relies on: **if a handler is running, the call is live** — the
executor simulates `dry_run_safe` tools in dry-run without reaching the handler. So the toolkit passes
`dry_run=False` down explicitly; otherwise a session created under a global dry-run would keep
refusing real work after an operator approved one live call.

---

## 6. Agents now run *inside* the flow the blueprint draws

`StarAgent` gained an optional `workspace` and a `workspace_kind`; `new_context()` — which runs after
planning and before the first step, exactly where §8 puts the workspace — acquires or reuses a
session, and `summarise_result()` reports it:

```jsonc
// POST /api/v1/computer {"goal":"click at 480,320"}
"result": {
  "actions": [ … ],
  "motor":   { "mode": "null", … },
  "workspace": {
    "session_id": "ws_fa9ddc1ad9eb", "kind": "computer",
    "path": "/…/data/workspace/ws_fa9ddc1ad9eb",
    "isolation": "null", "state": "idle", "dry_run": true
  }
}
```

`ComputerAgent.workspace_kind = "computer"`, `BrowserAgent.workspace_kind = "browser"`, so each agent
gets its own session and reuses it across runs (`stats["reused"]`). If the workspace is unavailable
(budget full, disabled, filesystem error) the run **still happens** and the result carries
`workspace_error` instead — a missing scratch directory must never block a task.
`WorkspaceManager.browser_profile(session)` gives the browser agent a per-session profile directory
inside the jail.

---

## 7. Surface: gateway + console (Frontend/ still untouched)

| Route | Purpose |
| --- | --- |
| `GET /api/v1/workspaces` | manager state: isolation (+ its honest note), root, alive/max sessions, TTL, quotas, counters (incl. `escapes`), and every session |
| `POST /api/v1/workspaces` | open a session `{kind, label, ttl_s?, dry_run?}` |
| `GET /api/v1/workspaces/{id}` | one session: state, quota counters, `files_list`, `checkpoints`, notes |
| `POST /api/v1/workspaces/{id}/close` | `{remove_files?, dry_run?}` — `remove_files` needs approval |
| `POST /api/v1/workspaces/{id}/checkpoint` | `{label?, dry_run?}` |
| `POST /api/v1/workspaces/{id}/restore` | `{checkpoint_id?, dry_run?}` |

The route table is now **31 routes**; the registry **57 tools** (48 + 9); capabilities **13**
(`workspace` added). The console's Workspace tab was rebuilt (inline CSS/JS, no CDN): isolation
backend + note, root, session/quota counters, the safety line (`checkpoints · restores · refused ·
**jail escapes**`), a kind/label form to open a session, and per-session `ckpt` / `close` / `delete`
buttons — `delete` asks `window.confirm()` first and, when the ladder parks it, prints the
`confirmation_id` and points at the Approvals tab.

`StarApplication` gained `workspace_state()`, `workspace_session()`, `create_workspace()`,
`close_workspace()`, `checkpoint_workspace()`, `restore_workspace()` (all in `contracts.py`); mutating
calls go **through the executor**, so the gateway surface gets the same risk → permission →
confirmation → audit → dry-run ladder as any other tool call, and `{"stopped": true}` under emergency stop.

---

## 8. Configuration (`STAR_WORKSPACE_*`, all optional, documented in `.env.example`)

```
STAR_WORKSPACE_ENABLED=true
STAR_WORKSPACE_MAX_SESSIONS=4         STAR_WORKSPACE_TTL_S=3600
STAR_ISOLATION_BACKEND=null           # null | process | windows_virtual_desktop (falls back)
STAR_WORKSPACE_CHECKPOINTS=true       STAR_WORKSPACE_MAX_CHECKPOINTS=5
STAR_WORKSPACE_MAX_FILES=500          STAR_WORKSPACE_MAX_BYTES=50000000
STAR_WORKSPACE_MAX_WRITE_BYTES=2000000  STAR_WORKSPACE_CHECKPOINT_MAX_BYTES=10000000
STAR_WORKSPACE_ALLOW_SPAWN=false      # opt-in only
```

No secrets. `STAR_WORKSPACE_ROOT` (Phase 0) remains the jail's parent.

---

## 9. Verification

| Check | Result |
| --- | --- |
| `tests/test_star2_workspace.py` | **40 passed** |
| `tests/test_star2_gateway.py` | 30 passed (4 new workspace-endpoint tests; route assertion 26 → 31) |
| Full suite (`--ignore=tests/test_hypothesis.py`) | **496 collected · 495 passed · 1 failed** — the failure is the pre-existing, network-dependent `tests/test_command_router.py::test_google_search` (Phase 12) |
| `scripts/smoke.py --phase 7` | **40 passed, 0 failed** (was 29; +11 workspace checks) |
| `git diff main -- Frontend/ Backend/bridge.py run.py run.bat agent/` | **empty** |

Observed live through the gateway (dry-run default, one call explicitly live):

```
GET  /api/v1/workspaces                → isolation null · "NOT hidden from the desktop by the OS"
POST /api/v1/workspaces                → simulated (dry-run) · sessions_alive stays 0
POST /api/v1/workspaces {dry_run:false}→ executed · jailed dir + _checkpoints/ created
GET  /api/v1/workspaces/{id}           → kind coding · files_list [] · checkpoints []
POST …/checkpoint                      → executed · state suspended · manifest with sha256 prefixes
POST …/restore                         → executed · state active
POST …/close {}                        → medium · executed · "files kept for inspection"
POST …/close {remove_files:true}       → high · needs_confirmation · directory still there
POST /api/v1/confirmations {approve}   → executed · destroyed true · directory gone
GET  /api/v1/workspaces/ws_nope        → {"ok": false, "error": "unknown workspace session 'ws_nope'"}
audit                                  → tool.decision + workspace.decision (workspace_destroy)
```

Also fixed while wiring (additive, no behaviour loss): the browser/computer/workspace tool modules no
longer emit a **second** `tool.registered` event per spec — `StarToolRegistry.register()` already
announces each tool with a richer payload, so the console's event stream now shows one event per tool.

---

## 10. Honest limitations

1. **`null` isolation is not OS-level isolation.** A background session is a jailed directory with
   quotas; it does not hide windows, input focus or the clipboard from the visible desktop. The
   abstraction is in place (`process`, `windows_virtual_desktop`) but stronger separation is future work.
2. **No real virtual-desktop integration.** `windows_virtual_desktop` always reports unavailable and
   falls back — deliberately, per the blueprint's "do not hard-code one technology".
3. **Spawning is opt-in and unexercised in production paths.** No agent calls `spawn()`; it exists so
   browser/app execution can be isolated later, and it is refused unless `STAR_WORKSPACE_ALLOW_SPAWN=true`.
4. **Checkpoints copy files; they do not snapshot processes or browser state.** Restoring brings back
   the session's files, not a running page — resuming a browser session means re-navigating.
5. **Quotas are per session, not global.** A machine-wide disk budget belongs to Phase 11's
   security/budget work, where the other resource limits live.
6. **The visible HUD does not render workspace sessions yet.** The gateway console does; the PySide6
   frontend stays untouched by rule, and mirroring these events into it is an opt-in adapter concern.

---

## 11. Commit

```
feat(star-2.0): phase 7 — invisible background workspace (jailed sessions, checkpoints, isolation seam)
tag: star-2.0-phase-7
```

Next: **Phase 8** — layered memory (working / episodic / semantic / preferences + hybrid retrieval)
built **over** the existing `agent.memory.store.MemoryStore` and `agent.planning.memory.EpisodicMemory`,
replacing the `pending` memory slot in `main.py`.
