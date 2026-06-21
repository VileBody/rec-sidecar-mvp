#!/usr/bin/env python3
"""Desktop call simulator for the Clean Start coach.

The seller line is typed, then voiced with the seller voice. The simulated
client answers through the existing client-actor + Inworld TTS pipeline.
"""

from __future__ import annotations

import argparse
import os
import queue
import re
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from tkinter import END, N, S, E, W, BooleanVar, StringVar, TclError, Tk, messagebox
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = REPO_ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from live_client_voice_agent import (  # noqa: E402
    DEFAULT_CHUNK_MS,
    DEFAULT_CEREBRAS_API_BASE,
    DEFAULT_CLIENT_ACTOR_MODEL,
    DEFAULT_INPUT,
    DEFAULT_INWORLD_CLIENT_VOICE,
    DEFAULT_INWORLD_SELLER_VOICE,
    DEFAULT_INWORLD_STT_WS_URL,
    DEFAULT_INWORLD_TTS_API_BASE,
    DEFAULT_INWORLD_TTS_MODEL,
    SessionLogger,
    generate_client_reply,
    load_env_file,
    parse_reference_script,
    play_audio,
    synthesize_inworld_text,
    write_pcm_wav,
)


DEFAULT_LOG_ROOT = REPO_ROOT / "logs" / "call_simulator"
DEFAULT_OPENER = (
    "Здравствуйте, это [Ваше Имя], я звоню вам по вашей заявке на обучение, "
    "удобно ли сейчас поговорить пару минут?"
)


@dataclass(frozen=True)
class ScriptOption:
    number: int
    title: str

    @property
    def label(self) -> str:
        return f"{self.number}. {self.title}"


@dataclass(frozen=True)
class WorkerEvent:
    kind: str
    payload: dict[str, object]


def make_output_dir(root: Path) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    candidate = root / stamp
    suffix = 1
    while candidate.exists():
        suffix += 1
        candidate = root / f"{stamp}-{suffix}"
    candidate.mkdir(parents=True, exist_ok=True)
    return candidate


def list_reference_scripts(path: Path) -> list[ScriptOption]:
    text = path.read_text(encoding="utf-8")
    options = [
        ScriptOption(int(match.group(1)), match.group(2).strip())
        for match in re.finditer(r"(?m)^## Скрипт\s+(\d+)\.\s*(.+)$", text)
    ]
    if not options:
        raise RuntimeError(f"No scripts found in {path}")
    return options


def load_default_env() -> None:
    for env_file in (REPO_ROOT / ".env", REPO_ROOT / ".env.iac"):
        load_env_file(env_file)


def build_runtime_args(*, output_dir: Path, script: int, persona_mode: str) -> argparse.Namespace:
    return argparse.Namespace(
        input=DEFAULT_INPUT,
        script=script,
        output_dir=output_dir,
        audio_device_index=None,
        list_devices=False,
        max_turns=20,
        stt_timeout_secs=12.0,
        stt_idle_finish_secs=1.0,
        chunk_ms=DEFAULT_CHUNK_MS,
        inworld_api_key=None,
        inworld_stt_ws_url=os.getenv("INWORLD_STT_WS_URL", DEFAULT_INWORLD_STT_WS_URL),
        inworld_tts_api_base=os.getenv("INWORLD_TTS_API_BASE", DEFAULT_INWORLD_TTS_API_BASE),
        inworld_tts_model=os.getenv("INWORLD_TTS_MODEL", DEFAULT_INWORLD_TTS_MODEL),
        inworld_seller_voice=os.getenv("INWORLD_TTS_SELLER_VOICE", DEFAULT_INWORLD_SELLER_VOICE),
        inworld_client_voice=os.getenv("INWORLD_TTS_CLIENT_VOICE", DEFAULT_INWORLD_CLIENT_VOICE),
        inworld_language=os.getenv("INWORLD_TTS_LANGUAGE", "ru-RU"),
        client_actor_model=os.getenv("CEREBRAS_MODEL", DEFAULT_CLIENT_ACTOR_MODEL),
        client_actor_temperature=float(os.getenv("CALL_SIM_CLIENT_TEMPERATURE", "0.85")),
        client_actor_max_tokens=int(os.getenv("CALL_SIM_CLIENT_MAX_TOKENS", "220")),
        cerebras_api_key=None,
        cerebras_api_base=os.getenv("CEREBRAS_API_BASE", DEFAULT_CEREBRAS_API_BASE),
        cerebras_reasoning_effort=os.getenv("CEREBRAS_REASONING_EFFORT", "none"),
        persona_mode=persona_mode,
        play=True,
        save_audio=True,
        env_file=[REPO_ROOT / ".env", REPO_ROOT / ".env.iac"],
    )


class CallSimulatorApp:
    def __init__(
        self,
        root: Tk,
        *,
        initial_script: int = 2,
        initial_persona_mode: str = "hostile",
        play_audio_enabled: bool = True,
    ) -> None:
        load_default_env()
        self.root = root
        self.root.title("Call Simulator")
        self.root.geometry("1120x760")
        self.root.minsize(900, 620)

        self.events: queue.Queue[WorkerEvent] = queue.Queue()
        self.script_options = list_reference_scripts(DEFAULT_INPUT)
        self.script_by_label = {option.label: option for option in self.script_options}
        initial_option = self.find_script_option(initial_script)
        self.persona_var = StringVar(value=initial_persona_mode)
        self.script_var = StringVar(value=initial_option.label)
        self.play_audio_var = BooleanVar(value=play_audio_enabled)
        self.status_var = StringVar(value="ready")
        self.output_dir_var = StringVar(value="")

        self.args: argparse.Namespace | None = None
        self.reference = None
        self.logger: SessionLogger | None = None
        self.history: list[tuple[str, str]] = []
        self.turn_index = 0
        self.busy = False
        self.last_seller_audio: Path | None = None
        self.last_client_audio: Path | None = None

        self.configure_style()
        self.build_ui()
        self.reset_session()
        self.root.after(100, self.drain_events)

    def find_script_option(self, script_number: int) -> ScriptOption:
        for option in self.script_options:
            if option.number == script_number:
                return option
        return self.script_options[0]

    def configure_style(self) -> None:
        self.root.configure(bg="#0e1118")
        style = ttk.Style()
        style.theme_use("clam")
        style.configure(".", background="#0e1118", foreground="#e8edf7", fieldbackground="#151b27")
        style.configure("Panel.TFrame", background="#151b27", borderwidth=1, relief="solid")
        style.configure("Header.TLabel", background="#151b27", foreground="#e8edf7", font=("Helvetica", 17, "bold"))
        style.configure("Muted.TLabel", background="#151b27", foreground="#98a4b8", font=("Helvetica", 12))
        style.configure("Section.TLabel", background="#151b27", foreground="#a7b0c2", font=("Helvetica", 10, "bold"))
        style.configure("Primary.TButton", font=("Helvetica", 13, "bold"), padding=(16, 10))
        style.configure("TButton", font=("Helvetica", 12), padding=(14, 9))
        style.configure("TCombobox", padding=8)
        style.map("Primary.TButton", background=[("active", "#2d7d55"), ("!disabled", "#1f6f50")])

    def build_ui(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.columnconfigure(1, weight=0)
        self.root.rowconfigure(1, weight=1)

        header = ttk.Frame(self.root, style="Panel.TFrame", padding=18)
        header.grid(row=0, column=0, columnspan=2, sticky=(W, E), padx=16, pady=(16, 10))
        header.columnconfigure(1, weight=1)

        ttk.Label(header, text="Call Simulator", style="Header.TLabel").grid(row=0, column=0, sticky=W)
        ttk.Label(header, textvariable=self.status_var, style="Muted.TLabel").grid(row=1, column=0, sticky=W, pady=(2, 0))

        controls = ttk.Frame(header, style="Panel.TFrame")
        controls.grid(row=0, column=1, rowspan=2, sticky=E)
        ttk.Button(controls, text="Новый диалог", command=self.reset_session).grid(row=0, column=0, padx=(0, 8))
        ttk.Button(controls, text="Повторить продавца", command=lambda: self.replay_audio("seller")).grid(row=0, column=1, padx=(0, 8))
        ttk.Button(controls, text="Повторить клиента", command=lambda: self.replay_audio("client")).grid(row=0, column=2, padx=(0, 8))
        ttk.Checkbutton(controls, text="Проигрывать звук", variable=self.play_audio_var).grid(row=0, column=3)

        left = ttk.Frame(self.root, padding=0)
        left.grid(row=1, column=0, sticky=(N, S, E, W), padx=(16, 8), pady=(0, 16))
        left.columnconfigure(0, weight=1)
        left.rowconfigure(1, weight=1)

        config = ttk.Frame(left, style="Panel.TFrame", padding=16)
        config.grid(row=0, column=0, sticky=(W, E), pady=(0, 10))
        config.columnconfigure(1, weight=1)
        ttk.Label(config, text="Сценарий", style="Section.TLabel").grid(row=0, column=0, sticky=W, padx=(0, 12))
        script_box = ttk.Combobox(config, textvariable=self.script_var, state="readonly", values=[option.label for option in self.script_options])
        script_box.grid(row=0, column=1, sticky=(W, E), padx=(0, 12))
        script_box.bind("<<ComboboxSelected>>", lambda _event: self.reset_session())
        ttk.Label(config, text="Персона", style="Section.TLabel").grid(row=0, column=2, sticky=W, padx=(0, 12))
        persona_box = ttk.Combobox(config, textvariable=self.persona_var, state="readonly", width=12, values=["neutral", "cold", "hostile"])
        persona_box.grid(row=0, column=3, sticky=E)
        persona_box.bind("<<ComboboxSelected>>", lambda _event: self.reset_session())

        transcript_panel = ttk.Frame(left, style="Panel.TFrame", padding=16)
        transcript_panel.grid(row=1, column=0, sticky=(N, S, E, W))
        transcript_panel.columnconfigure(0, weight=1)
        transcript_panel.rowconfigure(1, weight=1)
        ttk.Label(transcript_panel, text="Диалог", style="Section.TLabel").grid(row=0, column=0, sticky=W)
        self.transcript = ScrolledText(
            transcript_panel,
            wrap="word",
            height=20,
            bg="#101621",
            fg="#e8edf7",
            insertbackground="#e8edf7",
            relief="flat",
            borderwidth=0,
            font=("Helvetica", 15),
            padx=14,
            pady=12,
        )
        self.transcript.grid(row=1, column=0, sticky=(N, S, E, W), pady=(10, 0))
        self.transcript.tag_configure("seller_header", foreground="#6ca2ff", font=("Helvetica", 10, "bold"))
        self.transcript.tag_configure("client_header", foreground="#4fd08f", font=("Helvetica", 10, "bold"))
        self.transcript.tag_configure("seller", foreground="#e8edf7", justify="right", lmargin1=180, lmargin2=180, rmargin=16, spacing3=14)
        self.transcript.tag_configure("client", foreground="#e8edf7", justify="left", lmargin1=16, lmargin2=16, rmargin=180, spacing3=14)
        self.transcript.tag_configure("system", foreground="#98a4b8", justify="center", spacing1=6, spacing3=12)
        self.transcript.configure(state="disabled")

        right = ttk.Frame(self.root, padding=0)
        right.grid(row=1, column=1, sticky=(N, S, E, W), padx=(8, 16), pady=(0, 16))
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)

        opener = ttk.Frame(right, style="Panel.TFrame", padding=16)
        opener.grid(row=0, column=0, sticky=(W, E), pady=(0, 10))
        opener.columnconfigure(0, weight=1)
        ttk.Label(opener, text="Открывашка продавца", style="Section.TLabel").grid(row=0, column=0, sticky=W)
        self.opener_text = ScrolledText(
            opener,
            height=5,
            wrap="word",
            bg="#101621",
            fg="#e8edf7",
            relief="flat",
            borderwidth=0,
            font=("Helvetica", 16, "bold"),
            padx=12,
            pady=10,
        )
        self.opener_text.grid(row=1, column=0, sticky=(W, E), pady=(10, 10))
        self.opener_text.insert("1.0", DEFAULT_OPENER)
        ttk.Button(opener, text="Скопировать открывашку", command=self.copy_opener).grid(row=2, column=0, sticky=W)

        input_panel = ttk.Frame(right, style="Panel.TFrame", padding=16)
        input_panel.grid(row=1, column=0, sticky=(N, S, E, W))
        input_panel.columnconfigure(0, weight=1)
        input_panel.rowconfigure(1, weight=1)
        ttk.Label(input_panel, text="Что сказал продавец", style="Section.TLabel").grid(row=0, column=0, sticky=W)
        self.seller_input = ScrolledText(
            input_panel,
            height=10,
            wrap="word",
            bg="#101621",
            fg="#e8edf7",
            insertbackground="#e8edf7",
            relief="flat",
            borderwidth=0,
            font=("Helvetica", 15),
            padx=12,
            pady=10,
        )
        self.seller_input.grid(row=1, column=0, sticky=(N, S, E, W), pady=(10, 12))
        self.seller_input.bind("<Command-Return>", lambda _event: self.submit_seller_line())
        self.seller_input.bind("<Control-Return>", lambda _event: self.submit_seller_line())
        self.seller_input.bind("<Command-v>", self.paste_into_seller)
        self.seller_input.bind("<Command-V>", self.paste_into_seller)
        self.seller_input.bind("<Control-v>", self.paste_into_seller)
        self.seller_input.bind("<Control-V>", self.paste_into_seller)
        self.seller_input.bind("<Command-a>", self.select_seller_input)
        self.seller_input.bind("<Command-A>", self.select_seller_input)
        buttons = ttk.Frame(input_panel, style="Panel.TFrame")
        buttons.grid(row=2, column=0, sticky=(W, E))
        self.send_button = ttk.Button(buttons, text="Клиент отвечает", style="Primary.TButton", command=self.submit_seller_line)
        self.send_button.grid(row=0, column=0, sticky=W)
        ttk.Button(buttons, text="Вставить", command=self.paste_into_seller).grid(row=0, column=1, sticky=W, padx=(8, 0))
        ttk.Button(buttons, text="Очистить поле", command=self.clear_input).grid(row=0, column=2, sticky=W, padx=(8, 0))

        footer = ttk.Label(self.root, textvariable=self.output_dir_var, foreground="#697589", background="#0e1118")
        footer.grid(row=2, column=0, columnspan=2, sticky=(W, E), padx=18, pady=(0, 10))

    def reset_session(self) -> None:
        if self.busy:
            messagebox.showinfo("Call Simulator", "Дождись завершения текущего ответа клиента.")
            return
        label = self.script_var.get()
        option = self.script_by_label.get(label, self.script_options[0])
        output_dir = make_output_dir(DEFAULT_LOG_ROOT)
        self.args = build_runtime_args(
            output_dir=output_dir,
            script=option.number,
            persona_mode=self.persona_var.get(),
        )
        self.reference = parse_reference_script(self.args.input, option.number)
        self.logger = SessionLogger(output_dir)
        self.logger.log(
            "session_start",
            app="desktop_call_simulator",
            mode="both_sides_voice",
            script_number=self.reference.number,
            script_title=self.reference.title,
            persona_mode=self.persona_var.get(),
            seller_voice=self.args.inworld_seller_voice,
            client_voice=self.args.inworld_client_voice,
            client_actor_model=self.args.client_actor_model,
        )
        self.history = []
        self.turn_index = 0
        self.last_seller_audio = None
        self.last_client_audio = None
        self.status_var.set(f"ready · script {self.reference.number} · {self.persona_var.get()}")
        self.output_dir_var.set(str(output_dir))
        self.clear_transcript()
        self.append_system(f"Сценарий {self.reference.number}: {self.reference.title}")
        self.append_system("Продавец и клиент озвучиваются разными голосами.")

    def submit_seller_line(self) -> str:
        if self.busy:
            return "break"
        text = " ".join(self.seller_input.get("1.0", END).split()).strip()
        if not text:
            messagebox.showinfo("Call Simulator", "Вставь или набери реплику продавца.")
            return "break"
        self.clear_input()
        self.turn_index += 1
        turn = self.turn_index
        self.history.append(("Seller", text))
        if self.logger:
            self.logger.log("seller_text", turn=turn, text=text)
            self.logger.append_dialogue("Seller", text)
        self.append_message("seller", "Продавец", text)
        self.set_busy(True, "seller voice...")
        thread = threading.Thread(target=self.client_worker, args=(turn, text, list(self.history)), daemon=True)
        thread.start()
        return "break"

    def client_worker(self, turn: int, seller_text: str, history_snapshot: list[tuple[str, str]]) -> None:
        try:
            if self.args is None or self.reference is None or self.logger is None:
                raise RuntimeError("Session is not initialized.")
            seller_tts_started_at = time.monotonic()
            seller_pcm = synthesize_inworld_text(self.args, seller_text, speaker="Seller")
            seller_tts_elapsed_ms = int((time.monotonic() - seller_tts_started_at) * 1000)
            seller_audio_path = self.args.output_dir / f"seller_turn_{turn:03d}.wav"
            write_pcm_wav(seller_audio_path, seller_pcm)
            self.logger.log(
                "seller_audio",
                turn=turn,
                elapsed_ms=seller_tts_elapsed_ms,
                audio_path=str(seller_audio_path),
            )
            self.events.put(
                WorkerEvent(
                    "seller_audio",
                    {"turn": turn, "audio_path": str(seller_audio_path), "tts_ms": seller_tts_elapsed_ms},
                )
            )
            if self.play_audio_var.get():
                play_started_at = time.monotonic()
                play_audio(seller_audio_path)
                play_elapsed_ms = int((time.monotonic() - play_started_at) * 1000)
                self.logger.log("seller_audio_played", turn=turn, elapsed_ms=play_elapsed_ms)

            llm_started_at = time.monotonic()
            client_text = generate_client_reply(
                args=self.args,
                reference=self.reference,
                history=history_snapshot,
                seller_transcript=seller_text,
            )
            llm_elapsed_ms = int((time.monotonic() - llm_started_at) * 1000)
            self.logger.log("client_reply_text", turn=turn, text=client_text, elapsed_ms=llm_elapsed_ms)
            self.events.put(WorkerEvent("client_text", {"turn": turn, "text": client_text, "llm_ms": llm_elapsed_ms}))

            tts_started_at = time.monotonic()
            client_pcm = synthesize_inworld_text(self.args, client_text, speaker="Client")
            tts_elapsed_ms = int((time.monotonic() - tts_started_at) * 1000)
            audio_path = self.args.output_dir / f"client_turn_{turn:03d}.wav"
            write_pcm_wav(audio_path, client_pcm)
            self.logger.log(
                "client_reply_audio",
                turn=turn,
                elapsed_ms=tts_elapsed_ms,
                audio_path=str(audio_path),
            )
            self.events.put(
                WorkerEvent(
                    "client_audio",
                    {"turn": turn, "text": client_text, "audio_path": str(audio_path), "tts_ms": tts_elapsed_ms},
                )
            )
            if self.play_audio_var.get():
                play_started_at = time.monotonic()
                play_audio(audio_path)
                play_elapsed_ms = int((time.monotonic() - play_started_at) * 1000)
                self.logger.log("client_audio_played", turn=turn, elapsed_ms=play_elapsed_ms)
            self.events.put(WorkerEvent("done", {"turn": turn}))
        except Exception as exc:
            self.events.put(WorkerEvent("error", {"turn": turn, "error": str(exc)}))

    def drain_events(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                self.handle_worker_event(event)
        except queue.Empty:
            pass
        self.root.after(100, self.drain_events)

    def handle_worker_event(self, event: WorkerEvent) -> None:
        if event.kind == "seller_audio":
            self.last_seller_audio = Path(str(event.payload["audio_path"]))
            self.status_var.set(f"seller audio · {event.payload.get('tts_ms')} ms")
        elif event.kind == "client_text":
            self.status_var.set(f"client generated · {event.payload.get('llm_ms')} ms")
            self.append_message("client", "Клиент", str(event.payload["text"]))
            self.history.append(("Client", str(event.payload["text"])))
            if self.logger:
                self.logger.append_dialogue("Client", str(event.payload["text"]))
        elif event.kind == "client_audio":
            self.last_client_audio = Path(str(event.payload["audio_path"]))
            self.status_var.set(f"client audio · {event.payload.get('tts_ms')} ms")
        elif event.kind == "done":
            self.set_busy(False, f"ready · turn {event.payload.get('turn')}")
        elif event.kind == "error":
            self.set_busy(False, "error")
            if self.logger:
                self.logger.log("turn_error", turn=event.payload.get("turn"), error=event.payload.get("error"))
            messagebox.showerror("Call Simulator", str(event.payload.get("error")))

    def replay_audio(self, role: str) -> None:
        if self.busy:
            return
        audio_path = self.last_seller_audio if role == "seller" else self.last_client_audio
        if audio_path is None or not audio_path.exists():
            messagebox.showinfo("Call Simulator", f"Пока нет аудио-реплики: {role}.")
            return
        self.set_busy(True, f"replaying {role}...")
        thread = threading.Thread(target=self.replay_worker, args=(audio_path,), daemon=True)
        thread.start()

    def replay_worker(self, path: Path) -> None:
        try:
            play_audio(path)
            self.events.put(WorkerEvent("done", {"turn": self.turn_index}))
        except Exception as exc:
            self.events.put(WorkerEvent("error", {"turn": self.turn_index, "error": str(exc)}))

    def set_busy(self, busy: bool, status: str) -> None:
        self.busy = busy
        self.status_var.set(status)
        self.send_button.configure(state="disabled" if busy else "normal")

    def clear_input(self) -> None:
        self.seller_input.delete("1.0", END)

    def paste_into_seller(self, _event: object | None = None) -> str:
        try:
            value = self.root.clipboard_get()
        except TclError:
            self.status_var.set("clipboard empty")
            return "break"
        if value:
            self.seller_input.focus_set()
            self.seller_input.insert("insert", value)
            self.status_var.set("pasted")
        return "break"

    def select_seller_input(self, _event: object | None = None) -> str:
        self.seller_input.tag_add("sel", "1.0", "end-1c")
        self.seller_input.mark_set("insert", "end-1c")
        self.seller_input.see("insert")
        return "break"

    def copy_opener(self) -> None:
        value = self.opener_text.get("1.0", END).strip()
        self.root.clipboard_clear()
        self.root.clipboard_append(value)
        self.status_var.set("opener copied")

    def clear_transcript(self) -> None:
        self.transcript.configure(state="normal")
        self.transcript.delete("1.0", END)
        self.transcript.configure(state="disabled")

    def append_system(self, text: str) -> None:
        self.transcript.configure(state="normal")
        self.transcript.insert(END, f"{text}\n\n", ("system",))
        self.transcript.configure(state="disabled")
        self.transcript.see(END)

    def append_message(self, role: str, speaker: str, text: str) -> None:
        header_tag = "client_header" if role == "client" else "seller_header"
        body_tag = "client" if role == "client" else "seller"
        self.transcript.configure(state="normal")
        self.transcript.insert(END, f"{speaker.upper()}\n", (header_tag, body_tag))
        self.transcript.insert(END, f"{text}\n\n", (body_tag,))
        self.transcript.configure(state="disabled")
        self.transcript.see(END)


def parse_cli_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--script", type=int, default=2)
    parser.add_argument("--persona-mode", choices=["neutral", "cold", "hostile"], default="hostile")
    parser.add_argument("--no-play-audio", action="store_true")
    parser.add_argument("--check", action="store_true", help="Validate imports/env/script parsing without opening the UI.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_cli_args(sys.argv[1:] if argv is None else argv)
    if args.check:
        load_default_env()
        options = list_reference_scripts(DEFAULT_INPUT)
        selected = next((option for option in options if option.number == args.script), options[0])
        output_dir = DEFAULT_LOG_ROOT / "check-no-write"
        runtime_args = build_runtime_args(
            output_dir=output_dir,
            script=selected.number,
            persona_mode=args.persona_mode,
        )
        print(f"ok scripts={len(options)} selected={selected.number} model={runtime_args.client_actor_model}")
        return 0
    root = Tk()
    CallSimulatorApp(
        root,
        initial_script=args.script,
        initial_persona_mode=args.persona_mode,
        play_audio_enabled=not args.no_play_audio,
    )
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
