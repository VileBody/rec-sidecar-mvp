const $ = (id) => document.getElementById(id);

let currentUser = null;
let sessionId = "";
let state = null;
let connection = { state: "offline", detail: "offline" };
let audio = emptyAudioSnapshot();
let audioAdvancedVisible = false;
let manualGenerateInFlight = false;
let sellerSpeaker = "";
let diagnosticsLogPath = "";

const tauri = window.__TAURI__;
const bridge = tauri
  ? { invoke: tauri.core.invoke, listen: tauri.event.listen }
  : createMockBridge();

function createMockBridge() {
  const mockMode = new URLSearchParams(location.search).get("mock") || "auth";
  const mockMain = mockMode !== "auth";
  const now = new Date().toISOString();
  const mockState = {
    session_id: "sess-desktop-preview",
    transcript: [],
    seller_draft: "Давайте зафиксируем текущую ситуацию: какая у вас сейчас основная задача в бизнесе и какой результат вы хотите получить в ближайшие 90 дней?",
    seller_streaming: false,
    seller_generation_id: "preview-auto",
    seller_draft_immediate: "",
    seller_immediate_streaming: false,
    assist: mockMode === "active"
      ? { fast_text: "Зафиксируйте задачу и уточните критерий успеха.", slow_text: "После ответа согласуйте следующий шаг.", streaming: false }
      : { fast_text: "", slow_text: "", streaming: mockMode === "streaming" },
    stage_committed: mockMode === "active"
      ? { stage: "discovery", title: "Диагностика", agenda: "Понять задачу, сроки и критерии успеха." }
      : null,
    scorecard: mockMode === "active"
      ? { readiness: "yellow", readiness_label: "нужно уточнить", next_action: "Спросить о сроках и измеримом результате." }
      : null,
    events: mockEvents(mockMode, now),
  };
  if (mockMode === "streaming") mockState.seller_streaming = true;
  if (mockMode === "error") mockState.seller_draft = "";
  return {
    listen: async () => () => {},
    invoke: async (command, args = {}) => {
      if (command === "auth_status") {
        return mockMain ? { id: "preview", email: "user1@rec.local", role: "sales" } : null;
      }
      if (command === "diagnostics_log_path") {
        return "~/Library/Application Support/ru.TeamGenius.REC-Coach/logs/rec-coach.log";
      }
      if (command === "auth_login" || command === "auth_register") {
        return { id: "preview", email: args.email || "user1@rec.local", role: "sales" };
      }
      if (["session_resume_or_create", "session_create", "session_current"].includes(command)) {
        return { session_id: mockState.session_id, state: mockState };
      }
      if (command === "audio_configure") {
        audio.config = { ...audio.config, ...(args.config || {}) };
        return audio;
      }
      if (command === "audio_start") {
        const kind = args.kind;
        if (kind === "all" || kind === "system") audio.system = activeLane("системный звук стримится");
        if (kind === "all" || kind === "microphone") audio.microphone = activeLane("микрофон стримится");
        return audio;
      }
      if (command === "audio_stop") {
        const kind = args.kind;
        if (kind === "all" || kind === "system") audio.system = waitingLane();
        if (kind === "all" || kind === "microphone") audio.microphone = waitingLane();
        return audio;
      }
      return null;
    },
  };
}

function mockEvents(mode, now) {
  if (mode === "main" || mode === "auth") return [];
  const status = mode === "error" ? "error" : mode === "streaming" ? "sent" : "received";
  return [
    { type: "pipeline.status", created_at: now, data: { component: "stage", status: "received", elapsed_ms: 180 } },
    { type: "pipeline.status", created_at: now, data: { component: "zai_gate", status: "received", action: "continue" } },
    { type: "pipeline.status", created_at: now, data: { component: "seller_reply", status, elapsed_ms: 30007, model: "gemini" } },
    { type: "stage.committed", created_at: now, data: { stage: "discovery" } },
  ];
}

async function boot() {
  bindEvents();
  await bindTauriEvents();
  try {
    diagnosticsLogPath = await bridge.invoke("diagnostics_log_path");
  } catch (_) {
    diagnosticsLogPath = "лог недоступен";
  }
  renderAuth(true);
  render();
  try {
    currentUser = await bridge.invoke("auth_status");
    if (!currentUser) {
      renderAuth(true);
      return;
    }
    renderAuth(false);
    await resumeSession();
    audio = await bridge.invoke("audio_configure", { config: {} });
    renderAudio();
  } catch (error) {
    $("authError").textContent = errorText(error);
    renderAuth(true);
  }
}

async function bindTauriEvents() {
  await bridge.listen("auth://state", ({ payload }) => {
    currentUser = payload || null;
    if (!currentUser) {
      sessionId = "";
      state = null;
      renderAuth(true);
      render();
    } else {
      renderAuth(false);
    }
  });
  await bridge.listen("session://snapshot", ({ payload }) => applySession(payload));
  await bridge.listen("connection://status", ({ payload }) => {
    connection = payload || connection;
    $("streamStatus").textContent = connection.detail || connection.state || "offline";
  });
  await bridge.listen("audio://status", ({ payload }) => {
    audio = payload || emptyAudioSnapshot();
    renderAudio();
  });
  await bridge.listen("audio://diagnostics", ({ payload }) => {
    audio.diagnostics = payload || audio.diagnostics;
    renderEchoStatus();
  });
}

async function resumeSession() {
  $("session").textContent = "создаю сессию...";
  applySession(await bridge.invoke("session_resume_or_create"));
}

function applySession(envelope) {
  if (!envelope) return;
  sessionId = envelope.session_id || envelope.state?.session_id || "";
  state = envelope.state || envelope;
  $("session").textContent = sessionId || "сессия";
  render();
}

function renderAuth(locked) {
  document.body.classList.toggle("auth-locked", locked);
  $("authPanel").hidden = !locked;
  $("authStatus").textContent = currentUser?.email || "не авторизован";
  $("logout").hidden = locked;
  $("newSession").disabled = locked;
}

async function submitAuth(register) {
  const email = $("authEmail").value.trim();
  const password = $("authPassword").value;
  $("authError").textContent = "";
  setAuthBusy(true);
  try {
    currentUser = await bridge.invoke(register ? "auth_register" : "auth_login", { email, password });
    renderAuth(false);
    showToast(register ? "Аккаунт создан" : "Вошли");
    await resumeSession();
    audio = await bridge.invoke("audio_configure", { config: {} });
    renderAudio();
  } catch (error) {
    $("authError").textContent = errorText(error);
  } finally {
    setAuthBusy(false);
  }
}

function setAuthBusy(busy) {
  $("authLogin").disabled = busy;
  $("authRegister").disabled = busy;
}

function render() {
  renderDialog();
  renderReply();
  renderPipelineStatus();
  renderAssist();
  renderStage();
  renderAudio();
}

function renderDialog() {
  const dialog = $("dialog");
  if (!state) {
    dialog.innerHTML = `<div class="empty">Создаю диалог...</div>`;
    return;
  }
  const items = dialogItems(state);
  if (!items.length) {
    dialog.innerHTML = `<div class="empty">Пока пусто. Включи захват звука или вставь первую реплику вручную.</div>`;
    return;
  }
  dialog.innerHTML = items.map((item) => `
    <div class="${dialogBubbleClass(item)}">
      <div class="meta">
        <span class="speaker">${escapeHtml(roleLabel(item.role))}</span>
        <span>${formatTime(item.created_at)}</span>
        ${item.source ? `<span>${escapeHtml(sourceLabel(item.source))}</span>` : ""}
        ${item.final === false ? "<span>partial</span>" : ""}
      </div>
      ${escapeHtml(item.text)}
    </div>
  `).join("");
  dialog.parentElement.scrollTop = dialog.parentElement.scrollHeight;
}

function dialogItems(snapshot) {
  const transcript = (snapshot.transcript || [])
    .filter((item) => String(item.text || "").trim())
    .slice(-140);
  if (transcript.length) return transcript;
  const items = (snapshot.messages || []).map((item) => ({ ...item, final: true }));
  if ((snapshot.client_partial || "").trim()) {
    items.push({
      role: "client",
      text: snapshot.client_partial,
      final: false,
      created_at: snapshot.updated_at,
    });
  }
  return items;
}

function dialogBubbleClass(item) {
  const role = item.role || "speaker";
  const side = role === "seller" ? "seller" : role === "client" ? "client" : "speaker";
  return `bubble ${side}${item.final === false ? " partial" : ""}`;
}

function renderReply() {
  const text = state?.seller_draft || "";
  $("replyText").innerHTML = `<div class="rich-text">${text ? renderRichText(text) : "Жду речь клиента..."}</div>`;
  $("replyText").classList.toggle("muted", !text);
  $("replyMeta").textContent = text
    ? (state?.seller_streaming ? "генерируется" : "готово")
    : "обновляется по речи клиента";
  $("copyReply").disabled = !text;

  const immediate = state?.seller_draft_immediate || "";
  $("immediateReplyText").innerHTML = `<div class="rich-text">${immediate ? renderRichText(immediate) : "Жду ручную генерацию..."}</div>`;
  $("immediateReplyText").classList.toggle("muted", !immediate);
  syncGenerateReplyButton();
  renderManualReplyStatus();
}

function syncGenerateReplyButton() {
  const pending = manualReplyPending();
  $("generateReply").disabled = !state || pending;
  $("generateReply").classList.toggle("loading", pending);
  $("generateReplyLabel").textContent = pending ? "Ушел думать" : "Сгенерить сейчас";
}

function manualReplyPending() {
  if (manualGenerateInFlight || state?.seller_immediate_streaming) return true;
  const event = latestPipelineStatus("manual_reply");
  const status = eventData(event).status || "";
  if (!["sent", "queued"].includes(status)) return false;
  const startedAt = new Date(event?.created_at || "").getTime();
  return Number.isNaN(startedAt) || Date.now() - startedAt < 90000;
}

function renderPipelineStatus() {
  const node = $("pipelineStatus");
  if (!state) {
    node.innerHTML = `<div class="pipeline-empty">pipeline: жду сессию</div>`;
    return;
  }
  const stage = state.stage_committed || state.stage_candidate;
  const stageEvent = latestEvent("stage.committed") || latestEvent("stage.candidate");
  const stageLabel = stage?.stage ? `${stage.stage}${stage.title ? ` · ${stage.title}` : ""}` : "stage неизвестен";
  node.innerHTML = `
    <div class="pipeline-head"><span>статус</span><span>${escapeHtml(stageLabel)} · ${stageEvent ? humanDurationSince(stageEvent.created_at) : "еще нет"}</span></div>
    <div class="pipeline-grid">
      ${pipelineCardHTML("Stage", latestPipelineStatus("stage"), "ждем речи клиента")}
      ${pipelineCardHTML("ZAI gate", latestPipelineStatus("pivot_gate") || latestPipelineStatus("ready_gate") || latestPipelineStatus("zai_gate"), "ждет новой partial-фразы")}
      ${pipelineCardHTML("Gemini / reply", latestPipelineStatus("seller_reply"), state.seller_streaming ? "получаем реплику" : "ждем следующего момента")}
    </div>`;
}

function renderManualReplyStatus() {
  const node = $("manualReplyStatus");
  if (!state) {
    node.innerHTML = `<div class="pipeline-empty">manual: жду сессию</div>`;
    return;
  }
  node.innerHTML = `<div class="pipeline-grid">${pipelineCardHTML("Gemini direct", latestPipelineStatus("manual_reply"), state.seller_immediate_streaming ? "получаем реплику" : "готов к прямому запросу")}</div>`;
}

function pipelineCardHTML(label, event, fallback) {
  const data = eventData(event);
  const status = data.status || "";
  const details = [data.elapsed_ms ? `${Math.round(Number(data.elapsed_ms))} ms` : "", data.model, data.action, data.trigger].filter(Boolean).join(" · ");
  return `<div class="pipeline-card ${pipelineKind(status)}">
    <div class="pipeline-label">${escapeHtml(label)}</div>
    <div class="pipeline-primary">${escapeHtml(event ? pipelineStatusText(data, event) : fallback)}</div>
    ${details ? `<div class="pipeline-detail">${escapeHtml(details)}</div>` : ""}
  </div>`;
}

function pipelineStatusText(data, event) {
  if (data.status === "sent") return `отправлено · ожидаем ${humanDurationSince(event.created_at)}`;
  if (data.status === "queued") return "в очереди · ждет предыдущий запрос";
  if (data.status === "received") return "получено · ждем следующего момента";
  if (data.status === "skipped") return data.detail || "skip · ждем следующего момента";
  if (data.status === "error") return data.detail ? `ошибка · ${data.detail}` : "ошибка";
  return data.detail || data.status || "ждем";
}

function pipelineKind(status) {
  if (status === "received") return "ok";
  if (["sent", "queued"].includes(status)) return "wait";
  if (status === "skipped") return "skip";
  if (status === "error") return "bad";
  return "";
}

function stateEvents(type = "") {
  const events = Array.isArray(state?.events) ? state.events : [];
  return type ? events.filter((event) => event.type === type) : events;
}

function latestEvent(type) {
  const events = stateEvents(type);
  return events.at(-1) || null;
}

function latestPipelineStatus(component) {
  return stateEvents("pipeline.status").reverse().find((event) => eventData(event).component === component) || null;
}

function eventData(event) {
  return event?.data && typeof event.data === "object" ? event.data : {};
}

function renderAssist() {
  const assist = state?.assist || {};
  const messages = [];
  if (assist.fast_text) messages.push(`<div class="assist-msg fast"><div class="rich-text">${renderRichText(assist.fast_text)}</div></div>`);
  if (assist.slow_text) messages.push(`<div class="assist-msg"><div class="rich-text">${renderRichText(assist.slow_text)}</div></div>`);
  if (assist.streaming && !messages.length) messages.push(`<div class="assist-msg">Думаю...</div>`);
  $("assistLog").innerHTML = messages.join("") || `<div class="empty">Нажми «Помоги» или задай уточнение ниже.</div>`;
}

function renderStage() {
  if (!state) {
    $("stage").innerHTML = `<div class="empty">Жду данных.</div>`;
    return;
  }
  const stage = state.stage_committed || state.stage_candidate;
  const score = state.scorecard;
  const signals = Array.isArray(score?.raw?.signals || stage?.scorecard?.signals)
    ? (score?.raw?.signals || stage?.scorecard?.signals).slice(0, 6)
    : [];
  const metrics = [
    `<span class="metric ${scoreColor(score)}">${escapeHtml(score?.readiness_label || "нет оценки")}</span>`,
    `<span class="metric">${escapeHtml(stage?.stage || "stage неизвестен")}</span>`,
    ...signals.map((signal) => `<span class="metric ${signalColor(signal.state)}">${escapeHtml(signal.label || signal.id || "")}</span>`),
  ];
  const stageEvent = latestEvent("stage.committed") || latestEvent("stage.candidate");
  $("stage").innerHTML = `
    <div class="metric-row">${metrics.join("")}</div>
    <div class="stage-title">${escapeHtml(stage?.title || "Стадия еще не определена")}</div>
    <div class="stage-body stage-clock">На стадии: ${stageEvent ? humanDurationSince(stageEvent.created_at) : "еще нет"}</div>
    <div class="stage-body">${escapeHtml(stage?.agenda || "Жду речи клиента, чтобы понять текущую стадию.")}</div>
    <div class="stage-body">${escapeHtml(score?.next_action || stage?.step || "")}</div>`;
}

function renderAudio() {
  const system = audio.system || waitingLane();
  const microphone = audio.microphone || waitingLane();
  setAudioStatus("capture", system);
  setAudioStatus("mic", microphone);
  $("captureToggle").textContent = system.active ? "Стоп" : "Включить";
  $("micToggle").textContent = microphone.active ? "Стоп микрофон" : "Проверить микрофон";
  $("bothToggle").textContent = system.active && microphone.active ? "Стоп всё" : "Включить всё";
  $("bothStatus").textContent = system.active || microphone.active
    ? `system ${system.active ? "вкл" : "выкл"} · mic ${microphone.active ? "вкл" : "выкл"}`
    : "system audio = клиент, микрофон = мы";
  renderEchoStatus();
}

function setAudioStatus(prefix, lane) {
  const pill = $(`${prefix}Pill`);
  const status = $(`${prefix}Status`);
  const klass = lane.state === "on" ? "on" : lane.state === "error" ? "err" : "warn";
  pill.className = `status-pill ${klass}`;
  pill.textContent = lane.state === "on" ? "включено" : lane.state === "error" ? "ошибка" : lane.state === "connecting" ? "подключение" : "ожидание";
  status.textContent = lane.detail || (prefix === "mic" ? "тест выключен" : "нажми «Включить», чтобы захватить весь системный звук");
}

function renderEchoStatus() {
  const config = audio.config || { echo_filter: true, aec3: false, seller_speaker: "" };
  const diagnostics = audio.diagnostics || {};
  sellerSpeaker = config.seller_speaker || "";
  $("echoSuppressionToggle").checked = config.echo_filter;
  $("aec3Toggle").textContent = config.aec3 ? "AEC3 вкл" : "AEC3 выкл";
  $("aec3Toggle").className = config.aec3 ? "blue" : "ghost";
  $("echoStatus").textContent = `микрофон: native · ${config.aec3 ? "AEC3 встроен" : config.echo_filter ? "эхо подавляется" : "эхо-фильтр выкл"}`;
  $("aec3Status").textContent = config.aec3
    ? `ready · render=${diagnostics.aec3_render_frames || 0} · capture=${diagnostics.aec3_capture_frames || 0}`
    : "встроенный процессор выключен";
  $("sellerSpeaker").value = sellerSpeaker;
  $("speakerMapStatus").textContent = sellerSpeaker
    ? `мы = speaker ${sellerSpeaker}, клиент = speaker ${sellerSpeaker === "1" ? "2" : "1"}`
    : "выбери свой голос после первых реплик";
  $("echoDebugMetrics").textContent = [
    `audio suppress: suppressed=${diagnostics.suppressed_frames || 0} · double-talk=${diagnostics.double_talk_frames || 0}`,
    `audio signal: corr=${Number(diagnostics.best_correlation || 0).toFixed(2)} · residual=${Number(diagnostics.residual_ratio || 0).toFixed(2)} · lag=${diagnostics.lag_ms || 0}ms`,
    `AEC3: render=${diagnostics.aec3_render_frames || 0} · capture=${diagnostics.aec3_capture_frames || 0}`,
    `last route: ${diagnostics.last_route || "жду STT"}`,
    `log: ${diagnosticsLogPath || "инициализируется"}`,
  ].join("\n");
}

function bindEvents() {
  $("authLogin").onclick = () => submitAuth(false);
  $("authRegister").onclick = () => submitAuth(true);
  $("authPassword").onkeydown = (event) => { if (event.key === "Enter") submitAuth(false); };
  $("authEmail").onkeydown = (event) => { if (event.key === "Enter") $("authPassword").focus(); };

  $("logout").onclick = async () => {
    try { await bridge.invoke("auth_logout"); } catch (error) { showToast(errorText(error)); }
  };
  $("newSession").onclick = async () => {
    try { applySession(await bridge.invoke("session_create")); } catch (error) { showToast(errorText(error)); }
  };
  $("bothToggle").onclick = () => toggleAudio("all");
  $("captureToggle").onclick = () => toggleAudio("system");
  $("micToggle").onclick = () => toggleAudio("microphone");
  $("echoSuppressionToggle").onchange = () => configureAudio({ echo_filter: $("echoSuppressionToggle").checked });
  $("aec3Toggle").onclick = () => configureAudio({ aec3: !audio.config?.aec3 });
  $("sellerSpeaker").onchange = () => configureAudio({ seller_speaker: $("sellerSpeaker").value });
  $("audioAdvancedToggle").onclick = () => {
    audioAdvancedVisible = !audioAdvancedVisible;
    $("audioAdvancedPanel").hidden = !audioAdvancedVisible;
    $("audioAdvancedToggle").textContent = audioAdvancedVisible ? "Скрыть" : "Диагностика";
  };
  $("openReplyPip").onclick = () => bridge.invoke("reply_window_open").catch((error) => showToast(errorText(error)));
  $("copyReply").onclick = () => copyText(state?.seller_draft || "", "Реплика скопирована");
  $("replyText").onclick = () => copyText(state?.seller_draft || "", "Реплика скопирована");
  $("immediateReplyText").onclick = () => copyText(state?.seller_draft_immediate || "", "Немедленная реплика скопирована");
  $("generateReply").onclick = generateReply;
  $("help").onclick = () => postEvent({ type: "assist.request", trigger: "button" });
  $("askAssist").onclick = async () => {
    const text = $("assistQuestion").value.trim();
    if (!text) return;
    $("assistQuestion").value = "";
    await postEvent({ type: "assist.request", trigger: "chat", text });
  };
  $("clearAssist").onclick = () => {
    $("assistQuestion").value = "";
    $("assistLog").innerHTML = `<div class="empty">Нажми «Помоги» или задай уточнение ниже.</div>`;
  };
}

async function toggleAudio(kind) {
  const active = kind === "all"
    ? audio.system?.active && audio.microphone?.active
    : audio[kind]?.active;
  try {
    audio = await bridge.invoke(active ? "audio_stop" : "audio_start", { kind });
    renderAudio();
  } catch (error) {
    showToast(errorText(error), 3000);
  }
}

async function configureAudio(config) {
  try {
    audio = await bridge.invoke("audio_configure", { config });
    renderAudio();
  } catch (error) {
    showToast(errorText(error));
  }
}

async function generateReply() {
  if (!state || manualReplyPending()) return;
  manualGenerateInFlight = true;
  syncGenerateReplyButton();
  try {
    await postEvent({
      type: "seller.request",
      trigger: "manual_generate",
      text: "Сгенерируй реплику продавца немедленно под текущий момент разговора. Не валидируй текущую подсказку, дай новый вариант.",
    });
  } finally {
    setTimeout(() => {
      manualGenerateInFlight = false;
      syncGenerateReplyButton();
    }, 1200);
  }
}

async function postEvent(event) {
  try {
    await bridge.invoke("session_post_event", { event });
  } catch (error) {
    showToast(errorText(error));
  }
}

async function copyText(text, toast) {
  if (!text) return;
  try {
    await navigator.clipboard.writeText(text);
    showToast(toast);
  } catch (error) {
    showToast(`Не удалось скопировать: ${errorText(error)}`);
  }
}

function emptyAudioSnapshot() {
  return {
    system: waitingLane(),
    microphone: waitingLane(),
    config: { echo_filter: true, aec3: false, seller_speaker: "" },
    diagnostics: {},
  };
}

function waitingLane() {
  return { active: false, state: "waiting", detail: "", sent_frames: 0, dropped_frames: 0 };
}

function activeLane(detail) {
  return { active: true, state: "on", detail, sent_frames: 0, dropped_frames: 0 };
}

function roleLabel(role) {
  if (role === "seller") return "мы";
  if (role === "client") return "клиент";
  if (String(role || "").startsWith("speaker_")) return String(role).replace("_", " ");
  return role || "speaker";
}

function sourceLabel(source) {
  if (["remote_audio", "browser-system-audio"].includes(source)) return "system";
  if (["seller_mic", "browser-microphone-test"].includes(source)) return "mic";
  return source || "";
}

function formatTime(value) {
  const date = new Date(value || "");
  if (Number.isNaN(date.getTime())) return "--:--:--";
  return date.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function humanDurationSince(value) {
  const time = new Date(value || "").getTime();
  if (Number.isNaN(time)) return "0s";
  const seconds = Math.max(0, Math.floor((Date.now() - time) / 1000));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  return minutes < 60 ? `${minutes}m ${seconds % 60}s` : `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}

function scoreColor(score) {
  const readiness = String(score?.readiness || "").toLowerCase();
  if (readiness.includes("green")) return "green";
  if (readiness.includes("red")) return "red";
  return "yellow";
}

function signalColor(value) {
  const signal = String(value || "").toLowerCase();
  if (["green", "hit", "ok"].includes(signal)) return "green";
  if (["red", "miss", "bad"].includes(signal)) return "red";
  return "yellow";
}

function renderRichText(value) {
  const lines = String(value || "").replace(/\r\n/g, "\n").split("\n");
  const html = [];
  let list = "";
  const closeList = () => { if (list) { html.push(`</${list}>`); list = ""; } };
  for (const line of lines) {
    const trimmed = line.trim();
    if (!trimmed) { closeList(); continue; }
    const bullet = trimmed.match(/^[-*]\s+(.+)$/);
    const numbered = trimmed.match(/^\d+[.)]\s+(.+)$/);
    if (bullet || numbered) {
      const type = bullet ? "ul" : "ol";
      if (list !== type) { closeList(); list = type; html.push(`<${type}>`); }
      html.push(`<li>${renderInlineMarkdown((bullet || numbered)[1])}</li>`);
    } else {
      closeList();
      html.push(`<p>${renderInlineMarkdown(trimmed)}</p>`);
    }
  }
  closeList();
  return html.join("");
}

function renderInlineMarkdown(value) {
  return escapeHtml(value)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/__([^_]+)__/g, "<strong>$1</strong>")
    .replace(/\*([^*]+)\*/g, "<em>$1</em>")
    .replace(/_([^_]+)_/g, "<em>$1</em>");
}

function escapeHtml(value) {
  return String(value || "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;",
  }[char]));
}

function errorText(error) {
  if (typeof error === "string") return error;
  return error?.message || String(error || "неизвестная ошибка");
}

function showToast(text, duration = 1600) {
  const toast = $("toast");
  toast.textContent = text;
  toast.classList.add("show");
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => toast.classList.remove("show"), duration);
}

setInterval(() => {
  if (!state) return;
  renderPipelineStatus();
  renderManualReplyStatus();
  renderStage();
  syncGenerateReplyButton();
}, 1000);

boot();
