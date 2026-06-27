#!/usr/bin/env python3
"""Seed Clean Start API traffic and collect a small tracing dataset.

The script intentionally uses only the Python stdlib so it can be run from a
fresh checkout. It creates synthetic users, posts realistic sales/student
events, waits for async workers, then exports admin event logs and Tempo trace
lookups into a timestamped folder.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_BASE_URL = "https://rec.teamgenius.ru"
DEFAULT_TEMPO_URL = "http://127.0.0.1:3200"
DEFAULT_PASSWORD = "TraceSeed-2026!"


SALES_SCENARIOS = [
    {
        "name": "price_skeptic",
        "seller": "Здравствуйте, хочу за пару минут понять, есть ли смысл обсуждать обучение.",
        "client": [
            "Сразу скажу, покупать ничего не планирую, особенно если это снова дорогой курс.",
            "Я уже проходил пару программ, в итоге все закончилось вдохновением на неделю.",
            "Мне нужно понять, как это реально окупится, а не просто красиво звучит.",
        ],
        "manual": True,
        "assist": True,
    },
    {
        "name": "busy_owner",
        "seller": "Добрый день, давайте быстро поймем вашу текущую ситуацию и где сейчас затык.",
        "client": [
            "У меня бизнес и операционка, я работаю почти каждый день без нормального выхода.",
            "Деньги вроде есть, но свободных на эксперименты нет, все уходит в оборот.",
            "Если честно, я не понимаю, когда мне еще учиться и внедрять.",
        ],
        "manual": False,
        "assist": True,
    },
    {
        "name": "offtopic_hostile",
        "seller": "Скажите, какую задачу в доходе или профессии вы хотите решить первой?",
        "client": [
            "А вы вообще кто такие, почему я должен вам доверять?",
            "Мне все эти созвоны напоминают обычную продажу воздуха.",
            "Давайте без психологических заходов, говорите конкретно, что будет после оплаты.",
        ],
        "manual": True,
        "assist": False,
    },
    {
        "name": "target_gap",
        "seller": "Подскажите, какой результат был бы для вас доказательством, что разговор полезный?",
        "client": [
            "Я хочу выйти хотя бы на плюс двести тысяч в месяц, но не понимаю, через что.",
            "Самостоятельно пробовал, но постоянно бросаю, потому что нет системы и контроля.",
            "Если бы появился понятный план на месяц, я бы уже видел смысл продолжать.",
        ],
        "manual": False,
        "assist": False,
    },
    {
        "name": "bank_objection",
        "seller": "Если ценность понятна, давайте обсудим, какой формат оплаты был бы реалистичным.",
        "client": [
            "Ценность я примерно понимаю, но полмиллиона сейчас достать не могу.",
            "Кредитов боюсь, у меня уже был неприятный опыт с рассрочками.",
            "Мне нужен вариант, где я понимаю ежемесячную нагрузку и риски.",
        ],
        "manual": True,
        "assist": True,
    },
    {
        "name": "partner_decision",
        "seller": "Кто кроме вас влияет на решение, если формат окажется подходящим?",
        "client": [
            "Мне надо будет обсудить с женой, одному такие решения я не принимаю.",
            "Она обычно против обучения, потому что уже видела, как я покупал и не проходил.",
            "Если будет понятный договор и план, мне проще будет объяснить ей смысл.",
        ],
        "manual": False,
        "assist": True,
    },
]


STUDENT_SCENARIOS = [
    {
        "name": "python_gil_ru_en",
        "direction": "ru-en",
        "text": "Чем в питоне отличаются асинхронность от мультипроцессинга и что такое GIL?",
        "question": "Объясни коротко и дай два практических примера.",
    },
    {
        "name": "interview_en_ru",
        "direction": "en-ru",
        "text": "Can you explain the difference between indexes and constraints in PostgreSQL?",
        "question": "Сделай ответ в формате tl;dr и примеры для собеседования.",
    },
    {
        "name": "system_design_ru_en",
        "direction": "ru-en",
        "text": "Как объяснить идемпотентность в платежной системе простыми словами?",
        "question": "Дай короткий ответ и два предметных примера.",
    },
]


@dataclass
class SessionResult:
    kind: str
    scenario: str
    session_id: str
    email: str
    event_count: int
    trace_ids: list[str]
    event_types: dict[str, int]
    timings: list[dict[str, Any]]
    detail_path: str


class APIClient:
    def __init__(self, base_url: str, *, verify_tls: bool = False) -> None:
        self.base_url = base_url.rstrip("/")
        self.context = None if verify_tls else ssl._create_unverified_context()
        self.token = ""

    def clone(self) -> "APIClient":
        other = APIClient(self.base_url, verify_tls=self.context is None)
        other.token = self.token
        return other

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(
            self.base_url + path,
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=self.context) as res:
                raw = res.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"{method} {path} -> HTTP {exc.code}: {raw}") from exc
        if not raw:
            return {}
        return json.loads(raw)

    def auth(self, email: str, password: str, role: str | None = None) -> dict[str, Any]:
        payload = {"email": email, "password": password}
        if role:
            payload["role"] = role
        try:
            out = self.request("POST", "/v1/auth/register", payload)
        except RuntimeError as exc:
            if "already" not in str(exc).lower() and "409" not in str(exc):
                raise
            out = self.request("POST", "/v1/auth/login", {"email": email, "password": password})
        self.token = out.get("token", "")
        if not self.token:
            raise RuntimeError(f"auth response for {email} has no token: {out}")
        return out

    def login(self, email: str, password: str) -> dict[str, Any]:
        out = self.request("POST", "/v1/auth/login", {"email": email, "password": password})
        self.token = out.get("token", "")
        if not self.token:
            raise RuntimeError(f"login response for {email} has no token: {out}")
        return out


def now_slug() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'").strip('"')
    return values


def post_event(client: APIClient, session_id: str, payload: dict[str, Any]) -> None:
    client.request("POST", f"/v1/sessions/{session_id}/events", payload, timeout=45)


def get_admin_detail(admin: APIClient, session_id: str) -> dict[str, Any]:
    return admin.request("GET", f"/v1/admin/sessions/{session_id}", timeout=30)


def count_event_types(events: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for event in events:
        typ = str(event.get("type", ""))
        counts[typ] = counts.get(typ, 0) + 1
    return dict(sorted(counts.items()))


def event_trace_ids(events: list[dict[str, Any]]) -> list[str]:
    ids = {
        str(event.get("trace_id", "")).strip()
        for event in events
        if str(event.get("trace_id", "")).strip()
    }
    for event in events:
        data = decode_data(event)
        trace_id = str(data.get("trace_id", "")).strip()
        if trace_id:
            ids.add(trace_id)
    return sorted(ids)


def decode_data(event: dict[str, Any]) -> dict[str, Any]:
    data = event.get("data")
    if isinstance(data, dict):
        return data
    if isinstance(data, str) and data:
        try:
            parsed = json.loads(data)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def pipeline_timings(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    timings: list[dict[str, Any]] = []
    for event in events:
        if event.get("type") != "pipeline.status":
            continue
        data = decode_data(event)
        timings.append(
            {
                "created_at": event.get("created_at"),
                "component": data.get("component"),
                "status": data.get("status"),
                "action": data.get("action"),
                "trigger": data.get("trigger"),
                "elapsed_ms": data.get("elapsed_ms"),
                "provider": data.get("provider"),
                "model": data.get("model"),
                "detail": data.get("detail"),
            }
        )
    return timings


def wait_for_events(
    admin: APIClient,
    session_id: str,
    wanted: set[str],
    *,
    timeout: float,
    min_events: int = 1,
) -> dict[str, Any]:
    deadline = time.time() + timeout
    last: dict[str, Any] = {}
    while time.time() < deadline:
        last = get_admin_detail(admin, session_id)
        events = last.get("events", [])
        types = {event.get("type") for event in events}
        if len(events) >= min_events and wanted.issubset(types):
            return last
        time.sleep(1.0)
    return last or get_admin_detail(admin, session_id)


def run_sales_scenario(
    base: APIClient,
    admin: APIClient,
    out_dir: Path,
    run_id: str,
    index: int,
    scenario: dict[str, Any],
) -> SessionResult:
    client = base.clone()
    email = f"trace-sales-{run_id}-{index}@rec.local"
    client.auth(email, DEFAULT_PASSWORD, "sales")
    created = client.request("POST", "/v1/sessions", {"auto_opener": True}, timeout=45)
    session_id = created["session_id"]
    print(f"[sales:{index}] {scenario['name']} session={session_id}")
    time.sleep(1.5)
    post_event(client, session_id, {"type": "seller.input", "text": scenario["seller"]})
    time.sleep(0.5)
    for turn_index, text in enumerate(scenario["client"], 1):
        first = text[: max(18, min(len(text), 34))]
        second = text[: max(len(first), min(len(text), 80))]
        post_event(
            client,
            session_id,
            {
                "type": "stt.partial",
                "role": "client",
                "source": "browser-system-audio",
                "speaker": "speaker_2",
                "segment_id": f"{scenario['name']}-{turn_index}-p1",
                "text": first,
            },
        )
        time.sleep(0.45 + random.random() * 0.35)
        if second != first:
            post_event(
                client,
                session_id,
                {
                    "type": "stt.partial",
                    "role": "client",
                    "source": "browser-system-audio",
                    "speaker": "speaker_2",
                    "segment_id": f"{scenario['name']}-{turn_index}-p2",
                    "text": second,
                },
            )
            time.sleep(0.45 + random.random() * 0.35)
        post_event(
            client,
            session_id,
            {
                "type": "stt.final",
                "role": "client",
                "source": "browser-system-audio",
                "speaker": "speaker_2",
                "segment_id": f"{scenario['name']}-{turn_index}",
                "text": text,
            },
        )
        time.sleep(5.5)
        state = client.request("GET", f"/v1/sessions/{session_id}", timeout=30)
        seller_text = (
            state.get("seller_draft_immediate")
            or state.get("seller_draft")
            or "Понял вас, давайте уточню один важный момент."
        )
        post_event(
            client,
            session_id,
            {
                "type": "stt.final",
                "role": "seller",
                "source": "seller_mic",
                "speaker": "seller",
                "segment_id": f"{scenario['name']}-{turn_index}-seller",
                "text": seller_text,
            },
        )
        time.sleep(0.8)
    if scenario.get("manual"):
        post_event(client, session_id, {"type": "seller.request", "trigger": "manual_generate"})
    if scenario.get("assist"):
        post_event(client, session_id, {"type": "assist.request", "trigger": "button"})
    detail = wait_for_events(
        admin,
        session_id,
        {"seller.done", "stage.committed"},
        timeout=28,
        min_events=12,
    )
    path = out_dir / f"{index:02d}_sales_{scenario['name']}_{session_id}.json"
    write_json(path, detail)
    events = detail.get("events", [])
    return SessionResult(
        kind="sales",
        scenario=scenario["name"],
        session_id=session_id,
        email=email,
        event_count=len(events),
        trace_ids=event_trace_ids(events),
        event_types=count_event_types(events),
        timings=pipeline_timings(events),
        detail_path=str(path),
    )


def run_student_scenario(
    base: APIClient,
    admin: APIClient,
    out_dir: Path,
    run_id: str,
    index: int,
    scenario: dict[str, Any],
) -> SessionResult:
    client = base.clone()
    email = f"trace-student-{run_id}-{index}@rec.local"
    client.auth(email, DEFAULT_PASSWORD, "student")
    created = client.request("POST", "/v1/sessions", {"auto_opener": False}, timeout=45)
    session_id = created["session_id"]
    print(f"[student:{index}] {scenario['name']} session={session_id}")
    post_event(client, session_id, {"type": "student.direction", "direction": scenario["direction"]})
    post_event(
        client,
        session_id,
        {
            "type": "student.input",
            "direction": scenario["direction"],
            "text": scenario["text"],
        },
    )
    time.sleep(3.0)
    post_event(
        client,
        session_id,
        {
            "type": "student.answer.request",
            "trigger": "button",
            "text": scenario["question"],
        },
    )
    detail = wait_for_events(
        admin,
        session_id,
        {"student.translate.done", "student.answer.done"},
        timeout=24,
        min_events=8,
    )
    path = out_dir / f"{index:02d}_student_{scenario['name']}_{session_id}.json"
    write_json(path, detail)
    events = detail.get("events", [])
    return SessionResult(
        kind="student",
        scenario=scenario["name"],
        session_id=session_id,
        email=email,
        event_count=len(events),
        trace_ids=event_trace_ids(events),
        event_types=count_event_types(events),
        timings=pipeline_timings(events),
        detail_path=str(path),
    )


def tempo_get_json(tempo_url: str, path: str, timeout: float = 5.0) -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen(tempo_url.rstrip("/") + path, timeout=timeout) as res:
            return json.loads(res.read().decode("utf-8"))
    except Exception:
        return None


def extract_trace_summary(trace: dict[str, Any] | None) -> dict[str, Any] | None:
    if not trace:
        return None
    services: set[str] = set()
    spans: list[dict[str, Any]] = []
    for batch in trace.get("batches", []):
        service = "unknown"
        for attr in batch.get("resource", {}).get("attributes", []):
            if attr.get("key") == "service.name":
                service = attr.get("value", {}).get("stringValue", "unknown")
                services.add(service)
        for scope_span in batch.get("scopeSpans", []):
            for span in scope_span.get("spans", []):
                spans.append(
                    {
                        "service": service,
                        "name": span.get("name"),
                        "span_id": span.get("spanId"),
                        "parent_span_id": span.get("parentSpanId"),
                    }
                )
    return {"services": sorted(services), "span_count": len(spans), "sample_spans": spans[:20]}


def collect_tempo(
    results: list[SessionResult],
    tempo_url: str,
    out_dir: Path,
) -> dict[str, Any]:
    all_trace_ids = sorted({trace_id for result in results for trace_id in result.trace_ids})
    traces: dict[str, Any] = {}
    for trace_id in all_trace_ids:
        trace = tempo_get_json(tempo_url, f"/api/traces/{trace_id}", timeout=8)
        summary = extract_trace_summary(trace)
        traces[trace_id] = summary or {"missing": True}
    service_index: dict[str, Any] = {}
    for service in [
        "clean-start-gateway",
        "clean-start-seller-worker",
        "clean-start-stage-worker",
        "clean-start-scorecard-worker",
        "clean-start-assist-worker",
        "clean-start-student-worker",
        "llm-sidecar",
    ]:
        query = urllib.parse.quote(f"service.name={service}")
        service_index[service] = tempo_get_json(
            tempo_url,
            f"/api/search?tags={query}&limit=20",
            timeout=8,
        )
    out = {"trace_ids": all_trace_ids, "traces": traces, "service_index": service_index}
    write_json(out_dir / "tempo_index.json", out)
    return out


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, default=str) + "\n" for row in rows),
        encoding="utf-8",
    )


def write_summary(path: Path, results: list[SessionResult], tempo: dict[str, Any] | None) -> None:
    lines = [
        "# Tracing Dataset Seed",
        "",
        f"- Generated at: {datetime.now(timezone.utc).isoformat()}",
        f"- Sessions: {len(results)}",
        f"- Events: {sum(r.event_count for r in results)}",
        f"- Trace ids from events: {len({t for r in results for t in r.trace_ids})}",
        "",
        "## Sessions",
        "",
    ]
    for result in results:
        lines.extend(
            [
                f"### {result.kind}: {result.scenario}",
                "",
                f"- session_id: `{result.session_id}`",
                f"- email: `{result.email}`",
                f"- events: {result.event_count}",
                f"- trace_ids: {', '.join('`' + t + '`' for t in result.trace_ids[:8]) or 'none'}",
                f"- detail: `{result.detail_path}`",
                "- event types:",
            ]
        )
        for typ, count in result.event_types.items():
            lines.append(f"  - `{typ}`: {count}")
        if result.timings:
            lines.append("- pipeline timings:")
            for timing in result.timings[-10:]:
                label = " / ".join(
                    str(timing.get(key) or "")
                    for key in ("component", "status", "action")
                    if timing.get(key)
                )
                elapsed = timing.get("elapsed_ms")
                model = timing.get("model") or ""
                lines.append(f"  - {label}: {elapsed} ms {model}".rstrip())
        lines.append("")
    if tempo:
        lines.extend(["## Tempo Service Search", ""])
        for service, data in tempo.get("service_index", {}).items():
            traces = data.get("traces", []) if isinstance(data, dict) else []
            sample = ", ".join("`" + str(t.get("traceID")) + "`" for t in traces[:5])
            lines.append(f"- `{service}`: {len(traces)} traces {sample}")
        lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=os.getenv("REC_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--tempo-url", default=os.getenv("TEMPO_URL", DEFAULT_TEMPO_URL))
    parser.add_argument("--out-dir", type=Path, default=Path("logs/tracing_dataset") / now_slug())
    parser.add_argument("--sales-count", type=int, default=len(SALES_SCENARIOS))
    parser.add_argument("--student-count", type=int, default=len(STUDENT_SCENARIOS))
    parser.add_argument("--env-users", type=Path, default=Path(".env.clean-start-users"))
    parser.add_argument("--verify-tls", action="store_true")
    parser.add_argument("--skip-tempo", action="store_true")
    args = parser.parse_args(argv)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    base = APIClient(args.base_url, verify_tls=args.verify_tls)
    admin = APIClient(args.base_url, verify_tls=args.verify_tls)
    env = parse_env_file(args.env_users)
    admin_email = env.get("CLEAN_START_USER_6_EMAIL") or os.getenv("REC_ADMIN_EMAIL")
    admin_password = env.get("CLEAN_START_USER_6_PASSWORD") or os.getenv("REC_ADMIN_PASSWORD")
    if not admin_email or not admin_password:
        raise RuntimeError("admin credentials not found in .env.clean-start-users or env")
    admin.login(admin_email, admin_password)

    run_id = now_slug().replace("-", "")
    results: list[SessionResult] = []
    try:
        for index, scenario in enumerate(SALES_SCENARIOS[: max(0, args.sales_count)], 1):
            results.append(run_sales_scenario(base, admin, args.out_dir, run_id, index, scenario))
        for index, scenario in enumerate(STUDENT_SCENARIOS[: max(0, args.student_count)], 1):
            results.append(run_student_scenario(base, admin, args.out_dir, run_id, index, scenario))
    finally:
        rows = [result.__dict__ for result in results]
        write_json(args.out_dir / "sessions.json", rows)
        write_jsonl(args.out_dir / "sessions.jsonl", rows)

    print("[tempo] waiting for collector flush...")
    time.sleep(8.0)
    tempo = None
    if not args.skip_tempo:
        tempo = collect_tempo(results, args.tempo_url, args.out_dir)
    write_summary(args.out_dir / "summary.md", results, tempo)
    print(f"[done] out={args.out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
