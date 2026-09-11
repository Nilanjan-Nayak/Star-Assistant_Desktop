# Star Assistant — Frontend (The Face)

A runnable skeleton of the HUD from the architecture bible, chapters
21–23 ("The Face: HUD Design System", "Animation Language", "Desktop
Presence Modes"). This is **visual layer only** — no wake word, no LLM,
no OS control. It's built so the real brain can be wired in later
without touching any rendering code.

## Run it

```bash
pip install -r requirements.txt
python main.py
```

You'll see a small glowing reactor orb docked in the bottom-right corner
of your screen (ORB mode). Click it to expand into the CENTER command
center; press **Esc** to collapse back to the orb. Press **Q** to toggle
quiet-hours dimming.

## What's implemented

| Bible chapter | File | What you get |
|---|---|---|
| Ch. 32 Color/type/layout tokens | `tokens.py` | Navy/cyan/gold/alert/ok colors, grid, radii, motion-table durations, all in one place |
| Ch. 21 The Face | `reactor.py` | The reactor: idle breathing (`hex.breathe`), wake bump, hear bloom, think orbit, act tick, speak waveform, stop collapse |
| Ch. 22 Animation Language | `panels.py` | Glass panel with corner-bracket trace + scanline materialize (`panel.in`) and sweep dismiss (`panel.out`) |
| Ch. 23 Desktop Presence Modes | `main.py` | ORB and CENTER modes with `mode.orb` / `mode.center` transitions. RAIL/GHOST are stubbed — see TODOs below |
| Motion table | `content.py` | Per-glyph typewriter activity log (`type.log`, 8ms/char) |

## What's stubbed / not built

This is the frontend only. The bible's other volumes (Hybrid Brain,
Tool Runtime, Governor/safety gating, wake-word, TTS/STT, browser and
terminal cortex, the 600-skill library) are not implemented. Search for
`TODO(backend)` in `main.py` for the two integration points:

- `_demo_cycle()` currently drives the reactor through think/act/speak
  states on a timer, just to show the animation. Replace with real
  state pushed from the Planner over the mission event bus (schema in
  Ch. 34, `star.mission.v1`).
- The mission cards in `content.py` are hardcoded demo data
  (`DEMO_MISSIONS`, `DEMO_LOG_LINES`). Wire these to real IntentGraph /
  mission results.

RAIL (400px side panel) and GHOST (invisible-except-wake) modes from
Ch. 23 aren't wired into `main.py` yet — the geometry math is the same
pattern as `_layout_center()`, just a narrower fixed-width rect docked
to a screen edge for RAIL, and a fully hidden window for GHOST.

## Notes

- Fonts fall back to "Segoe UI" / "Consolas" as stand-ins for the
  bible's "geometric sans" / mono HUD type — swap in real font files if
  you have brand assets.
- I couldn't launch a live GUI in this sandbox (no display, no network
  to install PySide6), so this has been syntax-checked but not
  visually verified end-to-end. Flag anything that looks off once you
  run it and I'll fix it.
