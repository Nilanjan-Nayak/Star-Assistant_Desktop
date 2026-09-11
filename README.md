# Computer Control Agent v6.0

Ultra type-safe, layered desktop-automation agent. The public surface is one
facade; everything underneath is independently testable, mypy-strict, and
swappable via `Protocol`s.

```
pip install -e ".[dev]"
pytest
agent --describe --dry-run
agent --dry-run "set the volume"
```

Python **3.12+**. Optional extras (`gui`, `ocr`, `web`, `llm`, `win`/`mac`/`linux`)
pull in the real backends; the core package imports cleanly without them.

---

## Why v6 (vs the v5 sketch)

| Area | v5 | **v6** |
|---|---|---|
| **ActionSpec** | One fat model, optional fields, runtime validator | **Discriminated union** (`ClickAction \| TypeAction \| …`). Invalid combinations are type errors. Exhaustive `match` + `assert_never`. |
| **Screenshots** | `Any` / raw PIL | **`Raster`** — frozen RGB buffer, sha256 content-addressed, crop/diff without Pillow. PIL is an adapter at the capture boundary. |
| **Coordinates** | Non-negative only | Signed int16-range — multi-monitor origins can be negative. Half-open bounding boxes. |
| **IDs** | `NewType` constructors | Same + **`parse_*` validators** (`act_<12 hex>`, sha256, skill-name grammar). |
| **Result** | `map_ok` with `# type: ignore` | PEP 695 `Ok[T]` / `Err[E]` with `TypeGuard`, `and_then`, `collect`. |
| **Clock / retry / cancel** | Clock only | **`RetryPolicy`** (jittered backoff) + **`CancellationToken`** (deadline or explicit cancel). |
| **Circuit breaker** | Open/closed | Proper **half-open single trial**. |
| **Safety invariants** | `check()` mutated counters | **`check` is pure, `commit` after all pass** — a later failure no longer burns budget. |
| **Capability / HITL** | Token + escalation callback | `mint_grant()`, `Approver` protocol (`AutoApprove` / `AutoDeny` / `ConsoleApprover`). |
| **Skills → motor** | Skills called `motor.execute` and **bypassed the governor** | **`SkillContext.act()`** always `governor.check` → execute → audit. |
| **Launch skill** | `os.startfile` / `Popen` of anything | Blocklist + no shell metacharacters + `shutil.which` on Linux. |
| **FSM** | Dict of transitions | Frozen table, completeness assertion at import, `can_transition`. |
| **Events** | 4 types | 8-member **discriminated union**, typed `subscribe` returns unsubscribe. |
| **Observability** | JSON logs + metrics | Correlation-id `ContextVar`, nested spans, health registry, action journal. |
| **Tests** | 3 files | Geometry, motor, governor, result, IDs, FSM, breaker, cache, skills, planner, events, react, perception, retry, facade, **Hypothesis**. |
| **Facade** | Constructor only | Async context manager, injectable `WorldModel`, `--health` / `--version` CLI. |

---

## Package layout

```
agent/
├── core/            L0  IDs, enums, Result, clock, events, metrics, breaker, retry, cancel
├── geometry/        L1  PixelCoord, LogicalCoord, BoundingBox, Raster, MonitorInfo
├── perception/      L2  ScreenElement, PerceptionQuery, cascade of strategies
├── world/           L3  ScreenSnapshot, capture, differ, WorldModel, FakeCapture
├── motor/           L4  ActionSpec union, MotorBackend, controller
├── safety/          L5  Governor + composable invariants + capability tokens
├── skills/          L6  Skill[TIn], registry, builtins (volume/brightness/screenshot/launch/youtube)
├── planning/        L7  FSM, StubPlanner / LLMPlanner, ReActAgent, episodic memory, journal
├── facade.py            ComputerControlAgent
└── cli.py               `agent` console script
```

Dependencies only flow **down**. `core` never imports another layer.

---

## Type-safety notes

- **`NewType` IDs** — `ActionId` is not a `StepId`. Parsers enforce the prefix.
- **Discriminated `ActionSpec`** — `click(10, 10)` cannot carry `text=`. JSON round-trips through `parse_action`.
- **`Literal` event `kind`** — mypy narrows `Event` on `.kind`.
- **`Protocol` seams** — `Clock`, `MotorBackend`, `CaptureBackend`, `PerceptionStrategy`, `Planner`, `Approver`, `Invariant`, `SkillContext`.
- **`Raster`** replaces `Any` screenshots; tests never import Pillow.
- **Half-open boxes** — `contains` is `[x, x+width) × [y, y+height)`, so the far edge is not inside.
- **PEP 561** — `agent/py.typed` is shipped. `mypy --strict` is the default in `pyproject.toml`.

---

## Safety governor

Six composable invariants, each a pure `check` plus an optional `commit`:

1. **Total budget** — episode action cap.
2. **Rate limit** — trailing 60 s.
3. **Wall clock** — episode elapsed time.
4. **Forbidden regions** — pointer actions cannot land in configured boxes.
5. **Keyword blocklist** — typed / hotkey payloads.
6. **Capability token** — required at `SafetyLevel.PARANOID`.

`GovernorConfig.for_level(SafetyLevel.PARANOID)` tightens every numeric cap and turns capability tokens on.

Dry-run uses `NullBackend` (records specs, no OS calls) but **still runs the governor**.

---

## Quick API

```python
import asyncio
from agent import ComputerControlAgent
from agent.safety.config import GovernorConfig
from agent.core.enums import SafetyLevel

async def main() -> None:
    async with ComputerControlAgent(
        governor_config=GovernorConfig.for_level(SafetyLevel.NORMAL),
        dry_run=True,
    ) as agent:
        episode = await agent.run("set the volume")
        print(episode.final_state, episode.step_count)
        print(await agent.quick("volume", level=40))

asyncio.run(main())
```

Factories for raw motor specs live in `agent.motor.spec`:

```python
from agent.motor import click, type_text, hotkey, wait
spec = click(100, 200)
```

---

## Long-term memory (the brain that grows)

This does **not** make clicks or OCR more accurate. Those are perception/motor.
It **does** make planning personal: after `set volume to 40`, a later
`set the volume` uses **40** instead of the hardcoded 70.

- Local-first: `star_memory.db` (SQLite + hashing-trick embeddings, no extra deps)
- Optional real vectors: `pip install -e ".[memory]"` (`sentence-transformers`)
- Backup: `FolderSync` (copy to a folder / rclone / Drive desktop). `DriveSync` is a typed seam waiting for API credentials.

```
agent --remember "volume at night is 30" --remember-kind preference \
      --remember-key volume.level --remember-value 30
agent --recall volume
agent --dry-run "set the volume"     # planner now uses 30
agent --backup ./drive-folder
```

```python
agent.remember("I like lo-fi at night", kind=MemoryKind.PREFERENCE)
print(agent.memory_context("play something"))
```

---

## Seeing the screen (OCR)

Read-only: capture pixels, then read text. Does **not** move the mouse.

```
agent --see                         # OCR everything, save see.png
agent --see --query Search          # only words containing "Search"
agent --dry-run "screen dakho"      # planner → see skill
agent --skill see --params '{"query":"*"}'
```

Backends (first one that works):

1. **Capture** — `mss` → Pillow `ImageGrab` (Windows/macOS) → `gnome-screenshot` / `grim` / `scrot`
2. **OCR** — Tesseract (`eng`+`ben` if trained data is installed) → EasyOCR → empty (still saves the PNG)

```
# Debian/Ubuntu
sudo apt install tesseract-ocr tesseract-ocr-ben
pip install -e ".[gui,ocr]"
```

---

## CLI

```
agent --describe
agent --health
agent --skill volume --params '{"level":40}' --dry-run
agent --dry-run --safety paranoid "delete something"   # blocked
agent --metrics "set the volume"
```

---

## Testing without a GUI

`agent.world.fake.FakeCapture` + `solid(w, h, rgb)` mint deterministic screens.
`FrozenClock` drives TTLs, breakers and rate limits. `NullBackend` records
actions. The pytest suite uses only these — no display required.

```
pytest -q
```
