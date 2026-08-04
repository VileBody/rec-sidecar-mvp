const tauri = window.__TAURI__ || {
  event: { listen: async () => () => {} },
  core: {
    invoke: async (command) => command === "session_current" ? {
      state: {
        seller_draft: "Давайте зафиксируем текущую задачу и желаемый результат на ближайшие 90 дней.",
        seller_draft_immediate: "",
        seller_streaming: false,
        seller_immediate_streaming: false,
      },
    } : null,
  },
};
let state = null;

async function boot() {
  await tauri.event.listen("session://snapshot", ({ payload }) => {
    state = payload?.state || null;
    render();
  });
  const current = await tauri.core.invoke("session_current");
  state = current?.state || null;
  render();
  document.getElementById("generateReply").onclick = generate;
}

function render() {
  const immediate = state?.seller_draft_immediate || "";
  const automatic = state?.seller_draft || "";
  const text = immediate || automatic;
  const node = document.getElementById("replyText");
  node.textContent = text || "Жду речь клиента...";
  node.classList.toggle("muted", !text);
  document.getElementById("replyMeta").textContent = immediate
    ? (state?.seller_immediate_streaming ? "немедленная генерируется" : "немедленная готова")
    : automatic
      ? (state?.seller_streaming ? "auto генерируется" : "auto готово")
      : "обновляется по речи клиента";
  const pending = Boolean(state?.seller_immediate_streaming);
  const button = document.getElementById("generateReply");
  button.disabled = !state || pending;
  button.textContent = pending ? "Ушел думать" : "Сгенерить сейчас";
}

async function generate() {
  if (!state || state.seller_immediate_streaming) return;
  const button = document.getElementById("generateReply");
  button.disabled = true;
  button.textContent = "Ушел думать";
  try {
    await tauri.core.invoke("session_post_event", {
      event: {
        type: "seller.request",
        trigger: "manual_generate",
        text: "Сгенерируй реплику продавца немедленно под текущий момент разговора. Не валидируй текущую подсказку, дай новый вариант.",
      },
    });
  } finally {
    setTimeout(render, 1200);
  }
}

boot();
