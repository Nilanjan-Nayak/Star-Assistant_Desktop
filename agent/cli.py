"""CLI entry point."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import NoReturn

from agent import __version__
from agent.core.enums import MemoryKind, SafetyLevel
from agent.core.logging import get_logger
from agent.facade import ComputerControlAgent
from agent.safety.config import GovernorConfig

_log = get_logger("agent.cli")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent",
        description="Computer Control Agent v6.0",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            '  agent "Open YouTube and play lo-fi beats"\n'
            "  agent --skill volume --params '{\"level\":75}'\n"
            "  agent --describe\n"
            "  agent --dry-run \"delete something\"    # governor blocks\n"
        ),
    )
    parser.add_argument("goal", nargs="?", help="Natural-language goal")
    parser.add_argument("--skill", help="Run a single skill directly")
    parser.add_argument("--params", default="{}", help="JSON params for --skill")
    parser.add_argument("--dry-run", action="store_true", help="Use NullBackend")
    parser.add_argument(
        "--safety",
        choices=[s.value for s in SafetyLevel],
        default="normal",
    )
    parser.add_argument(
        "--describe",
        action="store_true",
        help="List available skills and JSON schemas",
    )
    parser.add_argument("--metrics", action="store_true", help="Print metrics snapshot at end")
    parser.add_argument("--health", action="store_true", help="Print health report and exit")
    parser.add_argument("--verbose", "-v", action="count", default=0)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--memory-db",
        default="star_memory.db",
        help="SQLite path for long-term memory",
    )
    parser.add_argument("--remember", help="Store a memory fact/preference and exit")
    parser.add_argument(
        "--remember-kind",
        choices=[k.value for k in MemoryKind],
        default=MemoryKind.FACT.value,
    )
    parser.add_argument("--remember-key", help="Structured preference key (e.g. volume.level)")
    parser.add_argument("--remember-value", help="Structured preference value (e.g. 30)")
    parser.add_argument("--recall", help="Semantic-search memories and exit")
    parser.add_argument("--memories", action="store_true", help="Print recent memories and exit")
    parser.add_argument("--backup", help="Copy memory DB into this folder and exit")
    parser.add_argument(
        "--see",
        action="store_true",
        help="Capture the screen, OCR it, print text, save see.png",
    )
    parser.add_argument(
        "--query",
        help="With --see, only return words containing this text",
    )
    parser.add_argument(
        "--save",
        default="see.png",
        help="With --see, screenshot path (relative). Empty string skips save.",
    )
    return parser


async def _run(args: argparse.Namespace) -> None:
    if args.verbose >= 1:
        _log.setLevel(logging.DEBUG)

    cfg = GovernorConfig.for_level(SafetyLevel(args.safety), dry_run=args.dry_run)
    agent = ComputerControlAgent(
        governor_config=cfg,
        dry_run=args.dry_run,
        memory_path=Path(args.memory_db),
    )

    if args.remember:
        rec = agent.remember(
            args.remember,
            kind=MemoryKind(args.remember_kind),
            key=args.remember_key,
            value=args.remember_value,
        )
        print(rec.model_dump_json(indent=2, exclude={"embedding"}))
        return

    if args.recall:
        hits = agent.recall(args.recall)
        print(
            json.dumps(
                [h.model_dump(mode="json", exclude={"embedding"}) for h in hits],
                indent=2,
                default=str,
            )
        )
        return

    if args.memories:
        recent = agent.semantic.recall_recent(20)
        print(
            json.dumps(
                [h.model_dump(mode="json", exclude={"embedding"}) for h in recent],
                indent=2,
                default=str,
            )
        )
        return

    if args.backup:
        dest = agent.backup_memory(args.backup)
        print(str(dest))
        return

    if args.health:
        print(json.dumps(agent.health().as_dict(), indent=2))
        return

    if args.describe:
        print(json.dumps(agent.registry.describe(), indent=2, default=str))
        return

    if args.skill:
        try:
            params = json.loads(args.params)
        except json.JSONDecodeError as exc:
            print(f"invalid --params JSON: {exc}", file=sys.stderr)
            sys.exit(2)
        if not isinstance(params, dict):
            print("--params must be a JSON object", file=sys.stderr)
            sys.exit(2)
        result = await agent.quick(args.skill, **params)
        print(result.model_dump_json(indent=2))
    elif args.goal:
        episode = await agent.run(args.goal)
        print(episode.model_dump_json(indent=2))
    else:
        _build_parser().print_help()

    if args.metrics:
        print("\n─── METRICS ───", file=sys.stderr)
        print(
            json.dumps(agent.snapshot_metrics(), indent=2, default=str),
            file=sys.stderr,
        )


def main() -> NoReturn:
    args = _build_parser().parse_args()
    try:
        asyncio.run(_run(args))
        sys.exit(0)
    except KeyboardInterrupt:
        print("\naborted by user", file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    main()
