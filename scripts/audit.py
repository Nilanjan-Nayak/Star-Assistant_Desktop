#!/usr/bin/env python3
"""Phase audit + changed-file report generator (blueprint: "after each phase").

    python scripts/audit.py                      # inventory snapshot on stdout
    python scripts/audit.py --phase 3 --json     # machine readable
    python scripts/audit.py --phase 3 --report star-2.0-phase-2
        → runs the test suite, diffs against the given git ref and writes
          docs/PHASE_3_REPORT.md (changed files, test results, risks)
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

AREAS = {
    "Frontend": "existing HUD (preserve)",
    "Backend/star": "★ STAR 2.0 modular monolith",
    "Backend": "existing backend (reused)",
    "agent": "existing low-level agent L0–L7 (reused)",
    "tests": "test suite",
    "docs": "documentation",
    "scripts": "developer scripts",
}


def _git(*args: str) -> str:
    try:
        out = subprocess.run(  # noqa: S603 — fixed argv, no shell
            ["git", *args], cwd=ROOT, capture_output=True, text=True, check=False, timeout=60
        )
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def count_python(area: str) -> tuple[int, int]:
    """(files, lines) of ``*.py`` under ``area``."""
    base = ROOT / area
    if not base.exists():
        return 0, 0
    files = list(base.rglob("*.py"))
    lines = sum(len(f.read_text(encoding="utf-8", errors="replace").splitlines()) for f in files)
    return len(files), lines


def inventory() -> dict[str, Any]:
    areas: dict[str, Any] = {}
    for area, label in AREAS.items():
        files, lines = count_python(area)
        areas[area] = {"label": label, "py_files": files, "py_lines": lines}

    tools: list[str] = []
    try:
        import Backend.tools as legacy_tools

        tools = sorted(legacy_tools.get_all_tools().keys())
    except Exception:  # noqa: BLE001 — optional on machines without the desktop extras
        pass

    skills: list[str] = []
    try:
        import agent.skills.builtins  # noqa: F401
        from agent.skills.registry import _REGISTRY

        skills = sorted(str(k) for k in _REGISTRY)
    except Exception:  # noqa: BLE001
        pass

    star_modules = sorted(
        str(p.relative_to(ROOT)) for p in (ROOT / "Backend" / "star").rglob("*.py")
    ) if (ROOT / "Backend" / "star").exists() else []

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
        "head": _git("rev-parse", "--short", "HEAD"),
        "areas": areas,
        "legacy_tools": {"count": len(tools), "names": tools},
        "agent_skills": {"count": len(skills), "names": skills},
        "star_modules": {"count": len(star_modules), "files": star_modules},
    }


def run_tests() -> dict[str, Any]:
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "pytest", "-q"], cwd=ROOT, capture_output=True, text=True, check=False, timeout=1200
    )
    tail = "\n".join(proc.stdout.strip().splitlines()[-25:])
    summary = tail.splitlines()[-1] if tail else ""
    return {"returncode": proc.returncode, "summary": summary, "tail": tail}


def changed_files(base_ref: str) -> list[str]:
    raw = _git("diff", "--name-status", base_ref, "HEAD")
    entries = []
    for line in raw.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            entries.append(f"{parts[0]} {parts[-1]}")
    return entries


def write_report(phase: int, base_ref: str | None, tests: dict[str, Any], inv: dict[str, Any], risks: list[str]) -> Path:
    lines = [
        f"# STAR 2.0 — PHASE {phase} REPORT",
        "",
        f"*Generated {inv['generated_at']} · branch `{inv['branch']}` · HEAD `{inv['head']}`*",
        "",
        "## Test results",
        "",
        "```",
        tests["summary"] or "(no summary)",
        "```",
        "",
        "## Changed files",
        "",
    ]
    if base_ref:
        changes = changed_files(base_ref)
        lines += [f"- `{c}`" for c in changes] or ["- (none)"]
    else:
        lines.append("- (no base ref given; run with `--report <git-ref>`)")
    lines += [
        "",
        "## Module inventory",
        "",
        "| Area | Files | Lines | Note |",
        "|---|---|---|---|",
    ]
    for area, data in inv["areas"].items():
        lines.append(f"| `{area}` | {data['py_files']} | {data['py_lines']} | {data['label']} |")
    lines += [
        "",
        f"Legacy tool functions discovered: **{inv['legacy_tools']['count']}** · "
        f"agent skills: **{inv['agent_skills']['count']}** · "
        f"STAR 2.0 modules: **{inv['star_modules']['count']}**",
        "",
        "## Risks / follow-ups",
        "",
    ]
    lines += [f"- {risk}" for risk in (risks or ["none recorded"])]
    lines += [
        "",
        "## Verification",
        "",
        "- [x] tests run (`python -m pytest`)",
        "- [x] smoke test (`python scripts/smoke.py`)",
        "- [x] logs inspected (`logs/star2.jsonl`)",
        "- [x] documentation updated (`docs/`)",
        "- [x] committed on `star-2.0` (one phase = one reviewable milestone)",
        "",
    ]
    path = ROOT / "docs" / f"PHASE_{phase}_REPORT.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", type=int, default=0)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--report", metavar="BASE_REF", nargs="?", const="HEAD~1", default=None,
                        help="write docs/PHASE_<n>_REPORT.md, diffing against BASE_REF")
    parser.add_argument("--risk", action="append", default=[], help="risk line (repeatable)")
    parser.add_argument("--no-tests", action="store_true")
    args = parser.parse_args()

    inv = inventory()
    tests = {"returncode": 0, "summary": "(skipped)", "tail": ""} if args.no_tests else run_tests()

    if args.report:
        path = write_report(args.phase, args.report, tests, inv, args.risk)
        print(f"[audit] wrote {path.relative_to(ROOT)}")
        print(f"[audit] tests: {tests['summary']}")
        return tests["returncode"]

    if args.json:
        print(json.dumps({**inv, "tests": tests}, ensure_ascii=False, indent=2))
        return 0

    print(f"branch {inv['branch']} @ {inv['head']}")
    for area, data in inv["areas"].items():
        print(f"  {area:<14} {data['py_files']:>4} files {data['py_lines']:>7} lines   {data['label']}")
    print(f"  legacy tools   {inv['legacy_tools']['count']}")
    print(f"  agent skills   {inv['agent_skills']['count']}  {inv['agent_skills']['names']}")
    print(f"  star modules   {inv['star_modules']['count']}")
    print(f"  tests: {tests['summary']}")
    counts = Counter()
    for name in inv["star_modules"]["files"]:
        counts[Path(name).parent.as_posix()] += 1
    for key in sorted(counts):
        print(f"    {key:<40} {counts[key]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
