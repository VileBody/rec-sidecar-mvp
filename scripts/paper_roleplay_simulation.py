#!/usr/bin/env python3
"""Generate markdown paper roleplays through the LLM sidecar wrappers."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from dataclasses import replace
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from llm_service.app.config import Settings
from llm_service.app.paper_roleplay import (
    DEFAULT_INPUT,
    DEFAULT_MAX_REPLIES,
    DEFAULT_OUTPUT_DIR,
    DEFAULT_TURN_PAIRS,
    PaperRoleplayGenerator,
    RoleplayConfig,
    extract_buyer_profiles,
    extract_event_facts,
    load_seed_markdown,
    write_roleplay_outputs,
)


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def parse_script_filter(values: list[str]) -> set[int] | None:
    if not values or any(value.lower() == "all" for value in values):
        return None
    selected: set[int] = set()
    for value in values:
        for part in value.split(","):
            part = part.strip()
            if part:
                selected.add(int(part))
    return selected


def select_profiles(markdown: str, script_filter: set[int] | None, count: int | None):
    profiles = extract_buyer_profiles(markdown)
    if script_filter is not None:
        profiles = [profile for profile in profiles if profile.number in script_filter]
    if count is not None:
        profiles = profiles[:count]
    return profiles


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--script", nargs="*", default=["all"], help="all, 1, 1,3,7")
    parser.add_argument("--count", type=int, default=None)
    parser.add_argument("--turn-pairs", type=int, default=DEFAULT_TURN_PAIRS)
    parser.add_argument("--max-replies", type=int, default=DEFAULT_MAX_REPLIES)
    parser.add_argument("--stop-on-terminal", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--buyer-model", default=os.getenv("PAPER_ROLEPLAY_BUYER_MODEL"))
    parser.add_argument("--buyer-temperature", type=float, default=0.85)
    parser.add_argument("--buyer-max-tokens", type=int, default=320)
    parser.add_argument("--seller-model", default=os.getenv("PAPER_ROLEPLAY_SELLER_MODEL"))
    parser.add_argument("--seller-temperature", type=float, default=0.45)
    parser.add_argument("--seller-max-tokens", type=int, default=260)
    parser.add_argument("--use-seller-agent", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--history-lines", type=int, default=18)
    parser.add_argument("--seller-name", default="Ирина")
    parser.add_argument("--buyer-name", default="Алексей")
    parser.add_argument("--run-id-prefix", default=f"paper-roleplay-{int(time.time())}")
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--stage-provider", choices=("auto", "cerebras", "local", "heuristic"), default="auto")
    parser.add_argument("--use-live-stage", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--env-file",
        type=Path,
        action="append",
        default=[Path(".env"), Path(".env.iac")],
    )
    return parser.parse_args(argv)


async def run(args: argparse.Namespace) -> int:
    markdown = load_seed_markdown(args.input)
    profiles = select_profiles(markdown, parse_script_filter(args.script), args.count)
    if not profiles:
        print("No buyer profiles matched selection.", file=sys.stderr)
        return 2

    event_facts = extract_event_facts(markdown)
    output_dir = args.output_dir or (
        DEFAULT_OUTPUT_DIR / time.strftime("%Y%m%d-%H%M%S")
    )
    output_dir = output_dir.resolve()

    if args.dry_run:
        print(f"Would write: {output_dir}")
        print(f"Turn pairs: {args.turn_pairs}")
        print(f"Max replies: {args.max_replies}")
        print(f"Stop on terminal: {args.stop_on_terminal}")
        print(f"Stage provider: {args.stage_provider}")
        print(f"Use seller agent: {args.use_seller_agent}")
        for profile in profiles:
            print(f"[{profile.number:02d}] {profile.title} — {profile.persona}")
        return 0

    settings = Settings.from_env()
    if not args.use_live_stage and settings.intelligence_transport != "rest":
        settings = replace(settings, intelligence_transport="rest")

    config = RoleplayConfig(
        turn_pairs=args.turn_pairs,
        max_replies=args.max_replies,
        stop_on_terminal=args.stop_on_terminal,
        buyer_model=args.buyer_model,
        buyer_temperature=args.buyer_temperature,
        buyer_max_tokens=args.buyer_max_tokens,
        seller_model=args.seller_model,
        seller_temperature=args.seller_temperature,
        seller_max_tokens=args.seller_max_tokens,
        use_seller_agent=args.use_seller_agent,
        history_lines=args.history_lines,
        seller_name=args.seller_name,
        buyer_name=args.buyer_name,
        run_id_prefix=args.run_id_prefix,
        concurrency=args.concurrency,
        stage_provider=args.stage_provider,
    )

    generator = PaperRoleplayGenerator(settings)
    try:
        print(
            f"[paper-roleplay] generating {len(profiles)} scenario(s), "
            f"up to {args.max_replies} replies each",
            flush=True,
        )
        results = await generator.generate_many(
            profiles=profiles,
            event_facts=event_facts,
            config=config,
        )
    finally:
        await generator.aclose()

    paths = write_roleplay_outputs(results, output_dir)
    print(f"[paper-roleplay] wrote {len(paths)} markdown file(s) to {output_dir}")
    for path in paths:
        print(path)
    return 0


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    for env_file in args.env_file:
        load_env_file(env_file)
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
