#!/usr/bin/env python3
"""Benchmark stage + scorecard latency across providers on cumulative dialogue prefixes."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from llm_service.app.config import Settings
from llm_service.app.providers import CerebrasClient, ProviderError, VertexClient
from llm_service.app.scorecard import normalize_scorecard, safe_parse_scorecard, scorecard_system_prompt
from llm_service.app.stage_assets import STAGE_AGENDA_BY_TAG, clamp_stage_forward, parse_stage_detection, stage_detection_system_prompt


DEFAULT_INPUT = (
    REPO_ROOT
    / "sales_scripts"
    / "paper_roleplays"
    / "iteration_full_v7_final"
    / "scenario_10_scenario-10.md"
)
DEFAULT_OUTPUT_ROOT = REPO_ROOT / "runs" / "stage_scorecard_bench"
DEFAULT_LANES = (
    "zai-glm-4.7",
    "gpt-oss-120b",
    "gemini-3.5-flash-minimal",
    "gemini-3.5-flash-low",
)


@dataclass(frozen=True)
class Utterance:
    speaker: str
    text: str


@dataclass(frozen=True)
class ConversationSample:
    event_facts: str
    pre_call_brief: str
    utterances: tuple[Utterance, ...]


@dataclass(frozen=True)
class LaneSpec:
    name: str
    provider: str
    model: str
    thinking_level: str | None = None


@dataclass
class PrefixResult:
    prefix_replies: int
    stage_latency_ms: int | None = None
    scorecard_latency_ms: int | None = None
    total_latency_ms: int | None = None
    stage: str | None = None
    confidence: float | None = None
    readiness: str | None = None
    readiness_label: str | None = None
    score: float | None = None
    hit_count: int | None = None
    miss_count: int | None = None
    next_action: str | None = None
    summary: str | None = None
    stage_raw: str | None = None
    scorecard_raw: str | None = None
    stage_error: str | None = None
    scorecard_error: str | None = None
    context_preview: str | None = None


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


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--replies", type=int, default=10, help="Cumulative replies to benchmark.")
    parser.add_argument("--lanes", nargs="*", default=["all"], help="all or explicit lane names.")
    parser.add_argument("--history-lines", type=int, default=32)
    parser.add_argument("--stage-max-tokens", type=int, default=96)
    parser.add_argument("--scorecard-max-tokens", type=int, default=2048)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--env-file",
        type=Path,
        action="append",
        default=[Path(".env"), Path(".env.iac")],
    )
    return parser.parse_args(argv)


def parse_lanes(values: list[str]) -> tuple[str, ...]:
    if not values or any(value.lower() == "all" for value in values):
        return DEFAULT_LANES
    selected: list[str] = []
    for value in values:
        for part in value.split(","):
            part = part.strip()
            if part:
                selected.append(part)
    return tuple(selected)


def lane_specs(names: tuple[str, ...]) -> list[LaneSpec]:
    mapping = {
        "zai-glm-4.7": LaneSpec(
            name="zai-glm-4.7",
            provider="cerebras",
            model="zai-glm-4.7",
        ),
        "gpt-oss-120b": LaneSpec(
            name="gpt-oss-120b",
            provider="cerebras",
            model="gpt-oss-120b",
        ),
        "gemini-3.5-flash-minimal": LaneSpec(
            name="gemini-3.5-flash-minimal",
            provider="vertex",
            model="gemini-3.5-flash",
            thinking_level="minimal",
        ),
        "gemini-3.5-flash-low": LaneSpec(
            name="gemini-3.5-flash-low",
            provider="vertex",
            model="gemini-3.5-flash",
            thinking_level="low",
        ),
    }
    unknown = [name for name in names if name not in mapping]
    if unknown:
        raise SystemExit(f"Unknown lane(s): {', '.join(unknown)}")
    return [mapping[name] for name in names]


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def section_between(text: str, start_marker: str, end_marker: str) -> str:
    start = text.find(start_marker)
    if start == -1:
        return ""
    start += len(start_marker)
    if not end_marker:
        end = len(text)
    else:
        end = text.find(end_marker, start)
        if end == -1:
            end = len(text)
    return text[start:end].strip()


def normalize_line(line: str) -> str:
    line = line.strip()
    line = line.lstrip("-").strip()
    return " ".join(line.split())


def parse_roleplay_markdown(path: Path) -> ConversationSample:
    text = read_text(path)
    profile_head = section_between(text, "- Buyer persona:", "## Complex Buyer Profile")
    profile_section = section_between(text, "## Complex Buyer Profile", "## Shared Agenda")
    shared_agenda = section_between(text, "## Shared Agenda", "## Dialogue")
    dialogue_section = section_between(text, "## Dialogue", "")

    event_lines: list[str] = []
    for raw_line in shared_agenda.splitlines():
        line = normalize_line(raw_line)
        if not line or line.startswith("Источник фактуры:") or line.startswith("Формат:"):
            continue
        event_lines.append(line)
    event_facts = "\n".join(event_lines).strip()

    brief_lines: list[str] = []
    buyer_persona = normalize_line(profile_head)
    if buyer_persona:
        brief_lines.append(f"Buyer persona: {buyer_persona}")
    for raw_line in profile_section.splitlines():
        line = normalize_line(raw_line)
        if line:
            brief_lines.append(line)
    pre_call_brief = "\n".join(brief_lines).strip()

    utterances: list[Utterance] = []
    for raw_line in dialogue_section.splitlines():
        line = raw_line.strip()
        if line.startswith("**Продавец:**"):
            utterances.append(Utterance("Продавец", line.removeprefix("**Продавец:**").strip()))
        elif line.startswith("**Покупатель:**"):
            utterances.append(Utterance("Клиент", line.removeprefix("**Покупатель:**").strip()))

    if not event_facts:
        raise SystemExit(f"Could not parse event facts from {path}")
    if not pre_call_brief:
        raise SystemExit(f"Could not parse pre-call brief from {path}")
    if not utterances:
        raise SystemExit(f"Could not parse dialogue utterances from {path}")

    return ConversationSample(
        event_facts=event_facts,
        pre_call_brief=pre_call_brief,
        utterances=tuple(utterances),
    )


def dialogue_text(utterances: list[Utterance], *, limit: int | None = None) -> str:
    selected = utterances[-limit:] if limit is not None and limit > 0 else utterances
    return "\n".join(f"{item.speaker}: {item.text}" for item in selected)


def build_context(sample: ConversationSample, utterances: list[Utterance], history_lines: int) -> str:
    return (
        "--- Событие / продукт ---\n"
        f"{sample.event_facts}\n\n"
        "--- Pre-call brief ---\n"
        f"{sample.pre_call_brief}\n\n"
        "--- Диалог ---\n"
        f"{dialogue_text(utterances, limit=history_lines)}\n"
    )


def warmup_settings(settings: Settings, lane: LaneSpec) -> Settings:
    if lane.provider == "vertex":
        return replace(
            settings,
            provider="vertex",
            vertex_model=lane.model,
            vertex_stage_model=lane.model,
            vertex_thinking_level=lane.thinking_level,
        )
    return replace(settings, provider="cerebras")


async def benchmark_lane(
    sample: ConversationSample,
    lane: LaneSpec,
    settings: Settings,
    *,
    replies: int,
    history_lines: int,
    stage_max_tokens: int,
    scorecard_max_tokens: int,
    temperature: float,
) -> list[PrefixResult]:
    lane_settings = warmup_settings(settings, lane)
    client = httpx.AsyncClient(
        timeout=90.0,
        proxy=lane_settings.outbound_proxy if lane.provider == "cerebras" else None,
    )
    cerebras = CerebrasClient(lane_settings, client)
    vertex = VertexClient(lane_settings, client)
    results: list[PrefixResult] = []
    current_stage: str | None = None

    try:
        if lane.provider == "vertex":
            try:
                await vertex.auth_headers()
            except Exception as exc:
                print(f"[bench] vertex auth warmup failed lane={lane.name} error={exc}", flush=True)

        for prefix_replies in range(1, min(replies, len(sample.utterances)) + 1):
            prefix_utterances = list(sample.utterances[:prefix_replies])
            context = build_context(sample, prefix_utterances, history_lines)
            result = PrefixResult(
                prefix_replies=prefix_replies,
                context_preview=context[:1000],
            )
            total_started_at = time.monotonic()
            stage_user_content = (
                f"{context}\n\n"
                "--- Текущий stage из предыдущего шага ---\n"
                f"{current_stage or '(пока неизвестен)'}\n"
            )

            stage_started_at = time.monotonic()
            try:
                if lane.provider == "vertex":
                    stage_raw = await vertex.generate_stage_detection(
                        model=lane.model,
                        system_prompt=stage_detection_system_prompt(),
                        user_content=stage_user_content,
                        temperature=temperature,
                        thinking_level=lane.thinking_level,
                    )
                else:
                    stage_raw = await cerebras.text(
                        model=lane.model,
                        system_prompt=stage_detection_system_prompt(),
                        user_content=stage_user_content,
                        temperature=temperature,
                        prompt_cache_key=None,
                        max_tokens=stage_max_tokens,
                    )
                stage_latency_ms = int((time.monotonic() - stage_started_at) * 1000)
                stage, confidence = parse_stage_detection(stage_raw)
                stage = clamp_stage_forward(current_stage, stage)
                current_stage = stage
                result.stage = stage
                result.confidence = confidence
                result.stage_raw = stage_raw
                result.stage_latency_ms = stage_latency_ms
            except Exception as exc:
                stage_latency_ms = int((time.monotonic() - stage_started_at) * 1000)
                current_stage = current_stage or "S2.1"
                result.stage = current_stage
                result.stage_error = str(exc)
                result.stage_latency_ms = stage_latency_ms

            agenda = STAGE_AGENDA_BY_TAG[current_stage]
            scorecard_started_at = time.monotonic()
            try:
                prompt = scorecard_system_prompt(current_stage, agenda)
                if lane.provider == "vertex":
                    scorecard_raw = await vertex.generate_scorecard(
                        model=lane.model,
                        system_prompt=prompt,
                        user_content=stage_user_content,
                        temperature=temperature,
                    )
                else:
                    scorecard_raw = await cerebras.text(
                        model=lane.model,
                        system_prompt=prompt,
                        user_content=stage_user_content,
                        temperature=temperature,
                        prompt_cache_key=None,
                        max_tokens=scorecard_max_tokens,
                    )
                scorecard_latency_ms = int((time.monotonic() - scorecard_started_at) * 1000)
                raw = safe_parse_scorecard(scorecard_raw)
                scorecard = normalize_scorecard(
                    stage=current_stage,
                    agenda=agenda,
                    raw=raw,
                    context=context,
                )
                result.scorecard_raw = scorecard_raw
                result.scorecard_latency_ms = scorecard_latency_ms
                result.readiness = scorecard.readiness
                result.readiness_label = scorecard.readiness_label
                result.score = scorecard.score
                result.hit_count = scorecard.hit_count
                result.miss_count = scorecard.miss_count
                result.next_action = scorecard.next_action
                result.summary = scorecard.summary
            except Exception as exc:
                scorecard_latency_ms = int((time.monotonic() - scorecard_started_at) * 1000)
                result.scorecard_error = str(exc)
                result.scorecard_latency_ms = scorecard_latency_ms

            result.total_latency_ms = int((time.monotonic() - total_started_at) * 1000)
            results.append(result)
            print(
                f"[bench] lane={lane.name} replies={prefix_replies} "
                f"stage={result.stage or '-'} readiness={result.readiness or '-'} "
                f"total_ms={result.total_latency_ms}",
                flush=True,
            )
    finally:
        await client.aclose()

    return results


def stats(values: list[int]) -> dict[str, float] | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(round((len(ordered) - 1) * 0.95))))
    return {
        "avg": round(sum(ordered) / len(ordered), 1),
        "median": round(statistics.median(ordered), 1),
        "p95": round(float(ordered[index]), 1),
    }


def summarize_lane(results: list[PrefixResult]) -> dict[str, Any]:
    stage_values = [item.stage_latency_ms for item in results if item.stage_latency_ms is not None]
    scorecard_values = [
        item.scorecard_latency_ms for item in results if item.scorecard_latency_ms is not None
    ]
    total_values = [item.total_latency_ms for item in results if item.total_latency_ms is not None]
    return {
        "requests": len(results),
        "stage_successes": sum(1 for item in results if not item.stage_error),
        "scorecard_successes": sum(1 for item in results if not item.scorecard_error),
        "stage_latency_ms": stats([value for value in stage_values if value is not None]),
        "scorecard_latency_ms": stats([value for value in scorecard_values if value is not None]),
        "total_latency_ms": stats([value for value in total_values if value is not None]),
        "last_stage": results[-1].stage if results else None,
        "last_readiness": results[-1].readiness if results else None,
    }


def write_outputs(
    output_dir: Path,
    sample: ConversationSample,
    lane_results: dict[str, list[PrefixResult]],
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    summary: dict[str, Any] = {
        "input_event_facts": sample.event_facts,
        "input_pre_call_brief": sample.pre_call_brief,
        "utterance_count": len(sample.utterances),
        "lanes": {},
    }

    lines = [
        "# Stage + Scorecard Benchmark",
        "",
        "## Input",
        "",
        f"- Utterances: {len(sample.utterances)}",
        "",
        "## Summary",
        "",
        "| Lane | Stage ok | Scorecard ok | Stage avg ms | Scorecard avg ms | Total avg ms | Last stage | Last readiness |",
        "| --- | ---: | ---: | ---: | ---: | ---: | --- | --- |",
    ]

    for lane_name, results in lane_results.items():
        lane_summary = summarize_lane(results)
        summary["lanes"][lane_name] = {
            "summary": lane_summary,
            "results": [asdict(item) for item in results],
        }
        stage_avg = (
            lane_summary["stage_latency_ms"]["avg"]
            if lane_summary["stage_latency_ms"]
            else "-"
        )
        scorecard_avg = (
            lane_summary["scorecard_latency_ms"]["avg"]
            if lane_summary["scorecard_latency_ms"]
            else "-"
        )
        total_avg = (
            lane_summary["total_latency_ms"]["avg"]
            if lane_summary["total_latency_ms"]
            else "-"
        )
        lines.append(
            "| "
            f"{lane_name} | {lane_summary['stage_successes']}/{lane_summary['requests']} "
            f"| {lane_summary['scorecard_successes']}/{lane_summary['requests']} "
            f"| {stage_avg} | {scorecard_avg} | {total_avg} "
            f"| {lane_summary['last_stage'] or '-'} | {lane_summary['last_readiness'] or '-'} |"
        )

    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (output_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


async def run(args: argparse.Namespace) -> int:
    sample = parse_roleplay_markdown(args.input)
    selected_lanes = lane_specs(parse_lanes(args.lanes))
    output_dir = (args.output_dir or (DEFAULT_OUTPUT_ROOT / time.strftime("%Y%m%d-%H%M%S"))).resolve()

    if args.dry_run:
        print(f"Input: {args.input}")
        print(f"Output: {output_dir}")
        print(f"Replies: {args.replies}")
        print("Lanes:")
        for lane in selected_lanes:
            print(f"- {lane.name} ({lane.provider} / {lane.model} / thinking={lane.thinking_level})")
        return 0

    settings = Settings.from_env()
    lane_results: dict[str, list[PrefixResult]] = {}
    for lane in selected_lanes:
        print(f"[bench] starting lane={lane.name}", flush=True)
        lane_results[lane.name] = await benchmark_lane(
            sample,
            lane,
            settings,
            replies=args.replies,
            history_lines=args.history_lines,
            stage_max_tokens=args.stage_max_tokens,
            scorecard_max_tokens=args.scorecard_max_tokens,
            temperature=args.temperature,
        )

    write_outputs(output_dir, sample, lane_results)
    print(f"[bench] wrote results to {output_dir}", flush=True)
    return 0


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    for env_file in args.env_file:
        load_env_file(env_file)
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
