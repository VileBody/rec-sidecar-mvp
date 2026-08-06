const $ = (id) => document.getElementById(id);

let currentUser = null;
let sessionId = "";
let state = null;
let connection = { state: "offline", detail: "offline" };
let audio = emptyAudioSnapshot();

const tauri = window.__TAURI__;
const bridge = tauri
  ? { invoke: tauri.core.invoke, listen: tauri.event.listen }
  : createMockBridge();

function createMockBridge() {
  const mode = new URLSearchParams(location.search).get("mock") || "auth";
  const signedIn = mode !== "auth";
  const now = Date.now();
  const mockState = {
    session_id: "sess-personal-preview",
    transcript: mode === "empty" ? [] : [
      { source: "remote_audio", role: "client", text: "Давай сначала сверим план на эту неделю.", final: true, created_at: new Date(now - 16000).toISOString() },
      { source: "seller_mic", role: "seller", text: "Да, начнём с задач, которые уже в работе.", final: true, created_at: new Date(now - 9000).toISOString() },
      { source: "remote_audio", role: "client", text: "Хорошо, первая задача сейчас...", final: false, created_at: new Date(now - 2000).toISOString() },
    ],
    interview: {
      question: "Could you tell me about your most relevant production AI project?",
      auto: {
        question: "Could you tell me about your most relevant production AI project?",
        text: "The most relevant example is the collections copilot I built at Bondora. It combined customer, payment, communication, and policy data in one controlled workflow. I focused on retrieval, guarded tool use, auditability, and safe rollout. It reduced average handling time by around 15 to 20 percent.",
        status: "ready",
        provider: "openrouter",
        model: "google/gemini-3.5-flash",
      },
      help: {
        question: "Could you tell me about your most relevant production AI project?",
        text: "A strong example is my work at Bondora. I helped build a human-in-the-loop collections copilot for a compliance-sensitive workflow. The important part was not only the model. We added policy controls, guarded natural-language-to-SQL, auditability, and gradual rollout. The result was a 15 to 20 percent reduction in average handling time.",
        status: "ready",
        provider: "openrouter",
        model: "google/gemini-3.5-flash",
      },
    },
  };
  const listeners = new Map();
  return {
    listen: async (event, callback) => {
      listeners.set(event, callback);
      return () => listeners.delete(event);
    },
    invoke: async (command, args = {}) => {
      if (command === "auth_status") return signedIn ? { id: "preview", email: "personal@rec.local", role: "personal" } : null;
      if (command === "auth_login") return { id: "preview", email: args.email || "personal@rec.local", role: "personal" };
      if (command === "auth_logout") return null;
      if (command === "session_resume_or_create" || command === "session_current") return { session_id: mockState.session_id, state: mockState };
      if (command === "session_post_event") return null;
      if (command === "diagnostics_log_path") return "~/Library/Application Support/ru.TeamGenius.REC-Personal/logs/rec-personal.log";
      if (command === "audio_configure") {
        audio.config = { ...audio.config, ...(args.config || {}) };
        return audio;
      }
      if (command === "audio_start") {
        if (args.kind === "all" || args.kind === "system") audio.system = activeLane("системный звук стримится");
        if (args.kind === "all" || args.kind === "microphone") audio.microphone = activeLane("микрофон стримится");
        return audio;
      }
      if (command === "audio_stop") {
        if (args.kind === "all" || args.kind === "system") audio.system = waitingLane("системный звук остановлен");
        if (args.kind === "all" || args.kind === "microphone") audio.microphone = waitingLane("микрофон остановлен");
        return audio;
      }
      return null;
    },
  };
}

async function boot() {
  bindControls();
  await bindRuntimeEvents();
  renderAuth(true);
  render();
  try {
    $("logPath").textContent = `лог: ${await bridge.invoke("diagnostics_log_path")}`;
  } catch (_) {
    $("logPath").textContent = "лог недоступен";
  }
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
  } finally {
    document.body.classList.remove("desktop-loading");
  }
}

async function bindRuntimeEvents() {
  await bridge.listen("auth://state", ({ payload }) => {
    currentUser = payload || null;
    if (!currentUser) {
      sessionId = "";
      state = null;
      renderAuth(true);
      render();
      return;
    }
    renderAuth(false);
  });
  await bridge.listen("session://snapshot", ({ payload }) => applySession(payload));
  await bridge.listen("connection://status", ({ payload }) => {
    connection = payload || connection;
    renderConnection();
  });
  await bridge.listen("audio://status", ({ payload }) => {
    audio = payload || emptyAudioSnapshot();
    renderAudio();
  });
  await bridge.listen("audio://diagnostics", ({ payload }) => {
    audio.diagnostics = payload || audio.diagnostics;
  });
}

function bindControls() {
  $("authLogin").onclick = submitAuth;
  $("authPassword").onkeydown = (event) => { if (event.key === "Enter") submitAuth(); };
  $("authEmail").onkeydown = (event) => { if (event.key === "Enter") $("authPassword").focus(); };
  $("logout").onclick = logout;
  $("allToggle").onclick = () => toggleAudio("all");
  $("systemToggle").onclick = () => toggleAudio("system");
  $("microphoneToggle").onclick = () => toggleAudio("microphone");
  $("echoFilter").onchange = () => configureAudio({ echo_filter: $("echoFilter").checked });
  $("aec3Toggle").onclick = () => configureAudio({ aec3: !audio.config?.aec3 });
  $("helpGenerate").onclick = requestInterviewHelp;
}

async function submitAuth() {
  $("authError").textContent = "";
  $("authLogin").disabled = true;
  try {
    currentUser = await bridge.invoke("auth_login", {
      email: $("authEmail").value.trim(),
      password: $("authPassword").value,
    });
    renderAuth(false);
    await resumeSession();
    audio = await bridge.invoke("audio_configure", { config: {} });
    renderAudio();
  } catch (error) {
    $("authError").textContent = errorText(error);
  } finally {
    $("authLogin").disabled = false;
  }
}

async function logout() {
  try {
    await bridge.invoke("auth_logout");
  } catch (error) {
    showToast(errorText(error));
  }
  currentUser = null;
  sessionId = "";
  state = null;
  audio = emptyAudioSnapshot();
  renderAuth(true);
  render();
}

async function resumeSession() {
  $("session").textContent = "подключаю сессию...";
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
}

function render() {
  renderTranscript();
  renderInterview();
  renderConnection();
  renderAudio();
}

function renderInterview() {
  const interview = state?.interview || {};
  const auto = interview.auto || {};
  const help = interview.help || {};
  renderInterviewLane({
    lane: auto,
    questionNode: $("autoQuestion"),
    answerNode: $("autoAnswer"),
    statusNode: $("autoStatus"),
    metaNode: $("autoMeta"),
    question: auto.question || interview.question || "",
    emptyQuestion: "Вопрос появится после речи интервьюера.",
    emptyAnswer: "Суфлёр начнёт писать ответ автоматически.",
    emptyStatus: "жду вопрос",
    mode: "автоматически",
  });
  renderInterviewLane({
    lane: help,
    questionNode: $("helpQuestion"),
    answerNode: $("helpAnswer"),
    statusNode: $("helpStatus"),
    metaNode: $("helpMeta"),
    question: help.question || interview.question || "",
    emptyQuestion: "Нажми кнопку — Gemini отдельно ответит по последнему вопросу.",
    emptyAnswer: "Здесь появится второй, независимый ответ.",
    emptyStatus: "не запускался",
    mode: "независимый ручной вызов",
  });
  $("helpGenerate").disabled = Boolean(help.streaming);
  $("helpGenerate").textContent = help.streaming ? "Генерируется…" : "Сгенерировать";
}

function renderInterviewLane({ lane, questionNode, answerNode, statusNode, metaNode, question, emptyQuestion, emptyAnswer, emptyStatus, mode }) {
  questionNode.textContent = question || emptyQuestion;
  answerNode.textContent = lane.text || emptyAnswer;
  answerNode.classList.toggle("waiting", !lane.text);
  const status = lane.status || "";
  let label = emptyStatus;
  let statusClass = "warn";
  if (lane.streaming || status === "streaming" || status === "identifying") {
    label = status === "identifying" ? "ищу вопрос" : "пишет ответ";
    statusClass = "on";
  } else if (status === "ready" || lane.text) {
    label = "готово";
    statusClass = "on";
  } else if (status === "error") {
    label = "ошибка";
    statusClass = "err";
  }
  statusNode.textContent = label;
  statusNode.className = `status-pill ${statusClass}`;
  const model = [lane.provider, lane.model].filter(Boolean).join(" / ") || "Gemini";
  metaNode.textContent = lane.error ? `Ошибка: ${lane.error}` : `${model} · ${mode}`;
}

function renderTranscript() {
  const list = $("transcript");
  const items = (state?.transcript || [])
    .filter((item) => String(item.text || "").trim())
    .slice(-300);
  if (!items.length) {
    list.innerHTML = `<div class="empty">${sessionId ? "Нажми «Включить всё», чтобы начать транскрибацию." : "Подключаю сессию..."}</div>`;
    $("transcriptMeta").textContent = "системный звук и микрофон";
    return;
  }
  const pinnedToBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 80;
  list.innerHTML = items.map((item) => {
    const source = transcriptSource(item.source);
    return `<article class="personal-transcript-item ${source.kind}${item.final === false ? " partial" : ""}">
      <div class="personal-transcript-item-head">
        <i class="source-dot ${source.kind}" aria-hidden="true"></i>
        <span class="personal-source-label">${escapeHtml(source.label)}</span>
        <time>${escapeHtml(formatTime(item.created_at))}</time>
        ${item.final === false ? "<span>слушаю…</span>" : ""}
      </div>
      <div class="personal-transcript-text">${escapeHtml(item.text)}</div>
    </article>`;
  }).join("");
  if (pinnedToBottom) list.scrollTop = list.scrollHeight;
  const finals = items.filter((item) => item.final !== false).length;
  $("transcriptMeta").textContent = `${finals} реплик · системный звук и микрофон`;
}

function transcriptSource(source) {
  if (["seller_mic", "browser-microphone-test", "student_mic"].includes(source)) {
    return { kind: "microphone", label: "Микрофон" };
  }
  return { kind: "system", label: "Системный звук" };
}

function renderConnection() {
  const node = $("streamStatus");
  const status = connection.state || "offline";
  node.textContent = status === "streaming" ? "live" : connection.detail || status;
  node.className = `status-pill ${status === "streaming" || status === "online" ? "on" : status === "offline" || status === "error" ? "err" : "warn"}`;
}

function renderAudio() {
  const system = audio.system || waitingLane();
  const microphone = audio.microphone || waitingLane();
  renderLane("system", system);
  renderLane("microphone", microphone);
  const bothActive = system.active && microphone.active;
  const anyActive = system.active || microphone.active;
  $("allToggle").textContent = anyActive ? "Стоп всё" : "Включить всё";
  $("allToggle").className = `primary personal-record-button${anyActive ? " recording" : ""}`;
  $("allStatus").textContent = anyActive
    ? `system ${system.active ? "вкл" : "выкл"} · mic ${microphone.active ? "вкл" : "выкл"} · можно свернуть окно`
    : "продолжится, когда окно свёрнуто";
  $("echoFilter").checked = audio.config?.echo_filter !== false;
  $("aec3Toggle").textContent = audio.config?.aec3 ? "AEC3 вкл" : "AEC3 выкл";
  $("aec3Toggle").className = audio.config?.aec3 ? "blue" : "ghost";
  $("filterStatus").textContent = audio.config?.aec3
    ? "AEC3 + cross-source echo suppression"
    : audio.config?.echo_filter === false ? "эхо-фильтр выключен" : "cross-source echo suppression включён";
  $("allToggle").setAttribute("aria-pressed", bothActive ? "true" : "false");
}

function renderLane(prefix, lane) {
  const pill = $(`${prefix}Pill`);
  const stateClass = lane.state === "on" ? "on" : lane.state === "error" ? "err" : "warn";
  pill.className = `status-pill ${stateClass}`;
  pill.textContent = lane.state === "on" ? "включено" : lane.state === "error" ? "ошибка" : lane.state === "connecting" ? "подключение" : "ожидание";
  $(`${prefix}Status`).textContent = lane.detail || (prefix === "system" ? "звук приложений и звонка" : "ваш голос");
}

async function toggleAudio(kind) {
  const active = kind === "all"
    ? Boolean(audio.system?.active || audio.microphone?.active)
    : Boolean(audio[kind]?.active);
  try {
    audio = await bridge.invoke(active ? "audio_stop" : "audio_start", { kind });
    renderAudio();
  } catch (error) {
    showToast(errorText(error), 3600);
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

async function requestInterviewHelp() {
  if (!sessionId || state?.interview?.help?.streaming) return;
  $("helpGenerate").disabled = true;
  try {
    await bridge.invoke("session_post_event", {
      event: { type: "interview.help.request", trigger: "button", text: "" },
    });
  } catch (error) {
    showToast(errorText(error), 3600);
  } finally {
    setTimeout(() => renderInterview(), 800);
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

function waitingLane(detail = "") {
  return { active: false, state: "waiting", detail, sent_frames: 0, dropped_frames: 0 };
}

function activeLane(detail) {
  return { active: true, state: "on", detail, sent_frames: 0, dropped_frames: 0 };
}

function formatTime(value) {
  const date = new Date(value || "");
  if (Number.isNaN(date.getTime())) return "--:--:--";
  return date.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function errorText(error) {
  if (typeof error === "string") return error;
  return error?.message || String(error || "неизвестная ошибка");
}

function escapeHtml(value) {
  return String(value || "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#039;",
  }[char]));
}

function showToast(text, duration = 1800) {
  const toast = $("toast");
  toast.textContent = text;
  toast.classList.add("show");
  setTimeout(() => toast.classList.remove("show"), duration);
}

boot();
