const $ = (id) => document.getElementById(id);
let currentUser = null;
let sessionId = "";
let events = null;
let pollTimer = null;
let state = null;
let captureStates = {
  system: emptyCaptureState("system"),
  microphone: emptyCaptureState("microphone"),
};
const SPEAKER_STORAGE_KEY = "rec-coach-seller-speaker";
let sellerSpeaker = localStorage.getItem(SPEAKER_STORAGE_KEY) || "";
let replyPipWindow = null;

function sessionStorageKey() {
  const identity = currentUser?.id || currentUser?.email || "dev";
  const role = currentUser?.role || "sales";
  return `rec-coach-session:${identity}:${role}`;
}

function rememberSession(id) {
  if (!id) return;
  localStorage.setItem(sessionStorageKey(), id);
}

function forgetSession() {
  localStorage.removeItem(sessionStorageKey());
}

function isStudentUser() {
  return currentUser?.role === "student";
}

async function boot() {
  initSpeakerMap();
  const ok = await loadMe();
  if (ok) await restoreSessionOrCreate();
}

async function loadMe() {
  const res = await fetch("/v1/auth/me", {
    cache: "no-store",
    credentials: "same-origin",
  });
  if (res.ok) {
    const data = await res.json();
    currentUser = data.user || null;
    renderAuth(false);
    return true;
  }
  currentUser = null;
  renderAuth(true);
  $("session").textContent = res.status === 401 ? "нужно войти" : `auth http ${res.status}`;
  return false;
}

function renderAuth(locked) {
  const isDevUser = currentUser?.id === "dev";
  document.body.classList.toggle("auth-locked", locked);
  $("authPanel").hidden = !locked;
  $("authStatus").textContent = isDevUser ? "dev mode" : (currentUser?.email || "не авторизован");
  $("studentAuthStatus").textContent = isDevUser ? "dev mode" : (currentUser?.email || "не авторизован");
  $("logout").hidden = locked || isDevUser;
  $("studentLogout").hidden = locked || isDevUser;
  $("newSession").disabled = locked;
  $("studentNewSession").disabled = locked;
  $("salesApp").hidden = !locked && isStudentUser();
  $("studentApp").hidden = locked || !isStudentUser();
}

async function submitAuth(mode) {
  const email = $("authEmail").value.trim();
  const password = $("authPassword").value;
  $("authError").textContent = "";
  $("authLogin").disabled = true;
  $("authRegister").disabled = true;
  try {
    const path = mode === "register" ? "/v1/auth/register" : "/v1/auth/login";
    const res = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify({ email, password, role: $("authRole").value || "sales" }),
    });
    if (!res.ok) {
      throw new Error(await responseError(res));
    }
    const data = await res.json();
    currentUser = data.user || null;
    renderAuth(false);
    showToast(mode === "register" ? "Аккаунт создан" : "Вошли");
    await restoreSessionOrCreate();
  } catch (error) {
    $("authError").textContent = error.message;
  } finally {
    $("authLogin").disabled = false;
    $("authRegister").disabled = false;
  }
}

async function logout() {
  stopCapture("system");
  stopCapture("microphone");
  stopSessionLive();
  forgetSession();
  await fetch("/v1/auth/logout", {
    method: "POST",
    credentials: "same-origin",
  }).catch(() => {});
  currentUser = null;
  sessionId = "";
  state = null;
  $("session").textContent = "нужно войти";
  $("studentSession").textContent = "нужно войти";
  setStreamStatus("offline");
  render();
  renderAuth(true);
}

async function responseError(res) {
  const data = await res.json().catch(() => ({}));
  return data.error || `HTTP ${res.status}`;
}

function stopSessionLive() {
  if (events) events.close();
  events = null;
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = null;
}

function applySession(data) {
  stopSessionLive();
  sessionId = data.session_id;
  $("session").textContent = sessionId;
  $("studentSession").textContent = sessionId;
  state = data.state;
  rememberSession(sessionId);
  connectStream();
  startStatePolling();
  render();
}

async function restoreSessionOrCreate() {
  const stored = localStorage.getItem(sessionStorageKey());
  if (stored && await resumeSession(stored)) {
    return;
  }
  if (await resumeLatestSession()) {
    return;
  }
  await createSession();
}

async function resumeSession(id) {
  if (!id) return false;
  const res = await fetch(`/v1/sessions/${encodeURIComponent(id)}`, {
    cache: "no-store",
    credentials: "same-origin",
  });
  if (res.status === 401) {
    currentUser = null;
    sessionId = "";
    state = null;
    renderAuth(true);
    $("session").textContent = "нужно войти";
    $("studentSession").textContent = "нужно войти";
    return true;
  }
  if (res.status === 403 || res.status === 404) {
    forgetSession();
    return false;
  }
  if (!res.ok) {
    throw new Error(await responseError(res));
  }
  applySession({ session_id: id, state: await res.json() });
  return true;
}

async function resumeLatestSession() {
  const res = await fetch("/v1/sessions/latest", {
    cache: "no-store",
    credentials: "same-origin",
  });
  if (res.status === 401) {
    currentUser = null;
    sessionId = "";
    state = null;
    renderAuth(true);
    $("session").textContent = "нужно войти";
    $("studentSession").textContent = "нужно войти";
    return true;
  }
  if (res.status === 404) {
    return false;
  }
  if (!res.ok) {
    throw new Error(await responseError(res));
  }
  applySession(await res.json());
  return true;
}

async function createSession() {
  stopSessionLive();
  state = null;
  render();
  const res = await fetch("/v1/sessions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
    body: JSON.stringify({ auto_opener: !isStudentUser() }),
  });
  if (res.status === 401) {
    currentUser = null;
    renderAuth(true);
    $("session").textContent = "нужно войти";
    return;
  }
  if (!res.ok) {
    throw new Error(await responseError(res));
  }
  const data = await res.json();
  applySession(data);
}

function connectStream() {
  events = new EventSource(`/v1/sessions/${sessionId}/stream`);
  setStreamStatus("streaming");
  events.addEventListener("snapshot", (event) => {
    state = JSON.parse(event.data);
    render();
  });
  events.onerror = () => setStreamStatus("reconnecting...");
}

function startStatePolling() {
  if (pollTimer) clearInterval(pollTimer);
  const poll = async () => {
    if (!sessionId) return;
    try {
      const res = await fetch(`/v1/sessions/${sessionId}`, { cache: "no-store", credentials: "same-origin" });
      if (res.status === 401) {
        currentUser = null;
        renderAuth(true);
        return;
      }
      if (!res.ok) return;
      state = await res.json();
      render();
    } catch (_) {
      // EventSource remains the primary live path; polling only smooths over proxy hiccups.
    }
  };
  pollTimer = setInterval(poll, 2000);
  setTimeout(poll, 700);
}

async function postEvent(payload) {
  if (!sessionId) return;
  const res = await fetch(`/v1/sessions/${sessionId}/events`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
    body: JSON.stringify(payload),
  });
  if (!res.ok) {
    if (res.status === 401) {
      currentUser = null;
      renderAuth(true);
      return;
    }
    showToast(await responseError(res));
  }
}

function render() {
  renderDialog();
  renderReply();
  renderStage();
  renderAssist();
  renderStudent();
  renderSpeakerMapStatus();
}

function renderDialog() {
  const dialog = $("dialog");
  if (!state) {
    renderDialogEmpty(dialog, "Создаю диалог...");
    return;
  }
  const items = dialogItems(state);
  if (!items.length) {
    renderDialogEmpty(dialog, "Пока пусто. Включи захват звука или вставь первую реплику вручную.");
    return;
  }
  if (dialog.dataset.empty !== "0") dialog.innerHTML = "";
  dialog.dataset.empty = "0";
  const container = dialog.parentElement;
  const stickToBottom = container.scrollHeight - container.scrollTop - container.clientHeight < 80;
  const existing = new Map([...dialog.querySelectorAll(".bubble[data-key]")].map((node) => [node.dataset.key, node]));
  const used = new Set();
  items.forEach((item, index) => {
    const key = dialogKey(item, index);
    let node = existing.get(key);
    if (!node) {
      node = document.createElement("div");
      node.dataset.key = key;
    }
    const nextClass = dialogBubbleClass(item);
    if (node.className !== nextClass) node.className = nextClass;
    const nextHtml = dialogBubbleHTML(item);
    if (node.dataset.html !== nextHtml) {
      node.innerHTML = nextHtml;
      node.dataset.html = nextHtml;
    }
    dialog.appendChild(node);
    used.add(key);
  });
  for (const [key, node] of existing.entries()) {
    if (!used.has(key)) node.remove();
  }
  if (stickToBottom) container.scrollTop = container.scrollHeight;
}

function dialogItems(currentState) {
  const transcript = (currentState.transcript || [])
    .filter((item) => String(item.text || "").trim())
    .slice(-140)
    .map((item, index) => ({
      ...item,
      id: item.id || `tr:${index}:${item.created_at || ""}:${item.role || ""}:${item.segment_id || ""}`,
    }));
  if (transcript.length) return transcript;

  const items = (currentState.messages || []).map((item, index) => ({
    ...item,
    id: `msg:${index}:${item.created_at || ""}:${item.role || ""}`,
    final: true,
  }));
  const partial = (currentState.client_partial || "").trim();
  if (partial) {
    items.push({
      id: "client-partial",
      role: "client",
      text: partial,
      created_at: currentState.updated_at,
      final: false,
    });
  }
  return items;
}

function renderDialogEmpty(dialog, text) {
  if (dialog.dataset.empty === text) return;
  dialog.dataset.empty = text;
  dialog.innerHTML = `<div class="empty">${escapeHtml(text)}</div>`;
}

function dialogKey(item, index) {
  return item.id || `${item.role || "speaker"}:${item.source || ""}:${item.speaker || ""}:${item.segment_id || ""}:${index}`;
}

function dialogBubbleClass(item) {
  const role = item.role || "speaker";
  const side = role === "seller" ? "seller" : role === "client" ? "client" : "speaker";
  const partial = item.final ? "" : " partial";
  return `bubble ${side}${partial}`;
}

function dialogBubbleHTML(item) {
  const role = item.role || "speaker";
  const source = item.source ? sourceLabel(item.source) : "";
  return `
    <div class="meta">
      <span class="speaker">${escapeHtml(roleLabel(role))}</span>
      <span>${formatTime(item.created_at)}</span>
      ${source ? `<span>${escapeHtml(source)}</span>` : ""}
      ${item.final ? "" : "<span>partial</span>"}
    </div>
    ${escapeHtml(item.text)}`;
}

function roleLabel(role) {
  if (role === "seller") return "мы";
  if (role === "client") return "клиент";
  if (role === "student_original") return "оригинал";
  if (String(role || "").startsWith("speaker_")) {
    const speaker = String(role).replace("speaker_", "");
    if (sellerSpeaker && speaker === sellerSpeaker) return `мы · speaker ${speaker}`;
    if (sellerSpeaker && speaker === otherSpeaker(sellerSpeaker)) return `клиент · speaker ${speaker}`;
    return role.replace("_", " ");
  }
  return role || "speaker";
}

function sourceLabel(source) {
  if (source === "browser-system-audio") return "system";
  if (source === "browser-microphone-test") return "mic";
  return source;
}

function formatTime(value) {
  if (!value) return "--:--:--";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "--:--:--";
  return date.toLocaleTimeString("ru-RU", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function renderReply() {
  const text = state?.seller_draft || "";
  $("replyText").textContent = text || "Жду речь клиента...";
  $("replyText").classList.toggle("muted", !text);
  const streaming = state?.seller_streaming ? "генерируется" : "готово";
  $("replyMeta").textContent = text ? streaming : "обновляется по речи клиента";
  $("copyReply").disabled = !text;
  $("markSaid").disabled = !text;
  syncReplyPip();
}

function renderStage() {
  if (!state) {
    $("stage").innerHTML = `<div class="empty">Жду данных.</div>`;
    return;
  }
  const stage = state.stage_committed || state.stage_candidate;
  const score = state.scorecard;
  const rawSignals = score?.raw?.signals || stage?.scorecard?.signals || [];
  const signals = Array.isArray(rawSignals) ? rawSignals.slice(0, 6) : [];
  const metricBits = [
    `<span class="metric ${scoreColor(score)}">${escapeHtml(score?.readiness_label || "нет оценки")}</span>`,
    `<span class="metric">${escapeHtml(stage?.stage || "stage неизвестен")}</span>`,
  ];
  for (const signal of signals) {
    metricBits.push(`<span class="metric ${signalColor(signal.state)}">${escapeHtml(signal.label || signal.id || "")}</span>`);
  }
  $("stage").innerHTML = `
    <div class="metric-row">${metricBits.join("")}</div>
    <div class="stage-title">${escapeHtml(stage?.title || "Стадия еще не определена")}</div>
    <div class="stage-body">${escapeHtml(stage?.agenda || "Жду речи клиента, чтобы понять текущую стадию.")}</div>
    <div class="stage-body">${escapeHtml(score?.next_action || stage?.step || "")}</div>
  `;
}

function renderAssist() {
  const assist = state?.assist || {};
  const bits = [];
  if (assist.fast_text) {
    bits.push(`<div class="assist-msg fast">${escapeHtml(assist.fast_text)}</div>`);
  }
  if (assist.slow_text) {
    bits.push(`<div class="assist-msg">${escapeHtml(assist.slow_text)}</div>`);
  }
  if (assist.streaming && !bits.length) {
    bits.push(`<div class="assist-msg">Думаю...</div>`);
  }
  $("assistLog").innerHTML = bits.join("") || `<div class="empty">Нажми «Помоги» или задай уточнение ниже.</div>`;
}

function renderStudent() {
  if (!state) {
    $("studentOriginal").innerHTML = `<div class="empty">Создаю диалог...</div>`;
    $("studentTranslated").innerHTML = `<div class="empty">Перевод появится после первой финальной фразы.</div>`;
    $("studentAssistLog").innerHTML = `<div class="empty">Нажми «Помоги» или задай вопрос ниже.</div>`;
    return;
  }
  const student = state?.student || {};
  const direction = student.direction || $("studentDirection")?.value || "en-ru";
  if ($("studentDirection").value !== direction) $("studentDirection").value = direction;
  $("studentDirectionStatus").textContent = direction === "ru-en" ? "RU -> EN" : "EN -> RU";
  $("studentTranslationMeta").textContent = student.translation_streaming ? "перевожу..." : "gpt-oss-120b";
  $("studentAnswerMeta").textContent = student.answer_streaming ? "gemini думает..." : (student.answer_model || "gemini-3.5-flash");

  const originals = Array.isArray(student.originals) ? student.originals : [];
  $("studentOriginal").innerHTML = originals.length
    ? originals.slice(-80).map((item) => studentItemHTML(item.text, `${sourceLanguageForDirection(direction).toUpperCase()} · ${formatTime(item.created_at)}${item.final ? "" : " · partial"}`)).join("")
    : `<div class="empty">Пока пусто. Включи захват звука или вставь фразу вручную.</div>`;

  const translations = Array.isArray(student.translations) ? student.translations : [];
  $("studentTranslated").innerHTML = translations.length
    ? translations.slice(-80).map((item) => studentItemHTML(item.text, `${targetLanguageForDirection(item.direction || direction).toUpperCase()} · ${formatTime(item.created_at)} · ${escapeHtml(item.model || "gpt-oss-120b")}`)).join("")
    : `<div class="empty">Перевод появится после первой финальной фразы.</div>`;

  if (student.answer_text) {
    $("studentAssistLog").innerHTML = `<div class="assist-msg">${escapeHtml(student.answer_text)}</div>`;
  } else if (student.answer_streaming) {
    $("studentAssistLog").innerHTML = `<div class="assist-msg">Думаю...</div>`;
  } else {
    $("studentAssistLog").innerHTML = `<div class="empty">Нажми «Помоги» или задай вопрос ниже.</div>`;
  }
}

function studentItemHTML(text, meta) {
  return `<div class="student-item"><div class="meta">${escapeHtml(meta)}</div>${escapeHtml(text)}</div>`;
}

function sourceLanguageForDirection(direction) {
  return direction === "ru-en" ? "ru" : "en";
}

function targetLanguageForDirection(direction) {
  return direction === "ru-en" ? "en" : "ru";
}

async function copyReply() {
  const text = state?.seller_draft || "";
  if (!text) return;
  await navigator.clipboard.writeText(text);
  showToast("Реплика скопирована");
}

async function markSaid() {
  const text = state?.seller_draft || "";
  if (!text) return;
  await postEvent({ type: "seller.input", text });
  showToast("Добавил в диалог как нашу реплику");
}

async function openReplyPip() {
  if (!("documentPictureInPicture" in window)) {
    showToast("Этот браузер не поддерживает окошко поверх вкладок");
    return;
  }
  if (replyPipWindow && !replyPipWindow.closed) {
    replyPipWindow.focus();
    syncReplyPip();
    return;
  }
  replyPipWindow = await window.documentPictureInPicture.requestWindow({
    width: 560,
    height: 380,
  });
  replyPipWindow.addEventListener("pagehide", () => {
    replyPipWindow = null;
  });
  const doc = replyPipWindow.document;
  doc.body.innerHTML = `
    <style>
      :root {
        color-scheme: light;
        --ink: #222734;
        --muted: #7b8496;
        --line: #d9e0ec;
        --paper: #ffffff;
        --paper-soft: #f6f8fc;
        --blue: #2f6ee7;
        --blue-soft: #eef4ff;
        font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      }
      * { box-sizing: border-box; }
      html, body { width: 100%; height: 100%; margin: 0; overflow: hidden; background: var(--paper); color: var(--ink); }
      body { display: grid; grid-template-rows: auto 1fr auto; }
      header { padding: 18px 20px 8px; border-bottom: 1px solid var(--line); }
      .eyebrow { color: #8a92a4; font-size: 12px; font-weight: 850; letter-spacing: .14em; text-transform: uppercase; }
      .meta { margin-top: 6px; color: var(--muted); font-size: 13px; font-weight: 650; }
      #pipReplyText { display: flex; align-items: center; padding: 22px 24px; font-size: clamp(25px, 8.4vw, 44px); line-height: 1.13; font-weight: 820; overflow: auto; word-break: break-word; }
      #pipReplyText.muted { color: #9aa3b5; font-weight: 680; }
      footer { display: flex; align-items: center; justify-content: space-between; gap: 10px; padding: 14px 18px; border-top: 1px solid var(--line); background: var(--paper-soft); }
      button { appearance: none; border: 1px solid #cfd8e8; border-radius: 14px; min-height: 44px; padding: 9px 16px; background: var(--paper); color: var(--ink); font: inherit; font-weight: 800; cursor: pointer; }
      button.primary { background: var(--blue); color: white; border-color: var(--blue); }
      button:disabled { opacity: .45; cursor: default; }
    </style>
    <header>
      <div class="eyebrow">следующая реплика</div>
      <div id="pipReplyMeta" class="meta">обновляется по речи клиента</div>
    </header>
    <main id="pipReplyText" class="muted">Жду речь клиента...</main>
    <footer>
      <button id="pipMarkSaid">Я это сказал</button>
      <button id="pipRefresh" class="primary">Перегенерить</button>
    </footer>
  `;
  doc.getElementById("pipMarkSaid").onclick = markSaid;
  doc.getElementById("pipRefresh").onclick = refreshReply;
  syncReplyPip();
}

function syncReplyPip() {
  if (!replyPipWindow || replyPipWindow.closed) return;
  const doc = replyPipWindow.document;
  const text = state?.seller_draft || "";
  const textNode = doc.getElementById("pipReplyText");
  const metaNode = doc.getElementById("pipReplyMeta");
  const markButton = doc.getElementById("pipMarkSaid");
  if (!textNode || !metaNode || !markButton) return;
  textNode.textContent = text || "Жду речь клиента...";
  textNode.classList.toggle("muted", !text);
  metaNode.textContent = text ? (state?.seller_streaming ? "генерируется" : "готово") : "обновляется по речи клиента";
  markButton.disabled = !text;
}

function refreshReply() {
  return postEvent({
    type: "seller.request",
    trigger: "manual_refresh",
    text: "Перегенерируй следующую реплику продавца короче и ближе к текущему контексту.",
  });
}

async function requestAssist(trigger, text = "") {
  await postEvent({ type: "assist.request", trigger, text });
}

async function requestStudentAnswer(trigger, text = "") {
  await postEvent({ type: "student.answer.request", trigger, text });
}

async function updateStudentDirection() {
  const direction = $("studentDirection").value || "en-ru";
  await postEvent({ type: "student.direction", direction });
  reconnectCaptureSTT("system", "student_direction_changed");
}

async function sendStudentOriginal() {
  const text = $("studentManualText").value.trim();
  if (!text) return;
  await postEvent({ type: "student.input", text, direction: $("studentDirection").value || "en-ru" });
  $("studentManualText").value = "";
}

async function startCapture({ automatic = false, student = false } = {}) {
  if (captureStates.system.active) {
    stopCapture("system");
    return false;
  }
  if (!window.isSecureContext && !["127.0.0.1", "localhost"].includes(location.hostname)) {
    setCaptureStatus("err", "для захвата нужен HTTPS или localhost");
    if (!automatic) showToast("Chrome блокирует системный звук на обычном HTTP");
    return false;
  }
  if (!navigator.mediaDevices?.getDisplayMedia) {
    setCaptureStatus("err", "браузер не поддерживает захват");
    return false;
  }
  try {
    setCaptureStatus("warn", automatic ? "запрашиваю доступ" : "выберите вкладку/экран со звуком");
    const stream = await navigator.mediaDevices.getDisplayMedia({ video: true, audio: true });
    const audioTracks = stream.getAudioTracks();
    if (!audioTracks.length) throw new Error("audio track не выбран");
    startAudioStream(stream, {
      mode: "system",
      sourceLabel: student ? "student-system-audio" : "browser-system-audio",
      roleOverride: student ? "student_original" : "mixed",
      direction: student ? ($("studentDirection").value || "en-ru") : "",
      language: student ? sourceLanguageForDirection($("studentDirection").value || "en-ru") : "",
    });
    setCaptureStatus("on", "захват включен");
    $("captureToggle").textContent = "Стоп";
    $("studentCaptureToggle").textContent = "Стоп";
    updateBothStatus();
    return true;
  } catch (error) {
    setCaptureStatus("warn", automatic ? "нужен клик для захвата" : error.message);
    $("captureToggle").textContent = "Включить";
    $("studentCaptureToggle").textContent = "Включить";
    updateBothStatus();
    return false;
  }
}

async function startStudentCapture() {
  return startCapture({ automatic: false, student: true });
}

async function startMicTest() {
  if (captureStates.microphone.active) {
    stopCapture("microphone");
    return false;
  }
  if (!window.isSecureContext && !["127.0.0.1", "localhost"].includes(location.hostname)) {
    setMicStatus("err", "для микрофона нужен HTTPS или localhost");
    return false;
  }
  if (!navigator.mediaDevices?.getUserMedia) {
    setMicStatus("err", "браузер не поддерживает микрофон");
    return false;
  }
  try {
    setMicStatus("warn", "запрашиваю микрофон");
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    });
    startAudioStream(stream, {
      mode: "microphone",
      sourceLabel: "browser-microphone-test",
    });
    setMicStatus("on", "микрофон включен · скажи фразу");
    $("micToggle").textContent = "Стоп микрофон";
    updateBothStatus();
    return true;
  } catch (error) {
    setMicStatus("err", error.message);
    $("micToggle").textContent = "Проверить микрофон";
    updateBothStatus();
    return false;
  }
}

async function startBothAudio() {
  if (captureStates.system.active && captureStates.microphone.active) {
    stopCapture("system");
    stopCapture("microphone");
    updateBothStatus("оба источника остановлены");
    return;
  }
  $("bothToggle").disabled = true;
  updateBothStatus("включаю звонок");
  try {
    if (!captureStates.microphone.active) await startMicTest();
    if (!captureStates.system.active) await startCapture({ automatic: false });
  } finally {
    $("bothToggle").disabled = false;
    updateBothStatus();
  }
}

function startAudioStream(stream, { mode, sourceLabel, roleOverride = "", direction = "", language = "" }) {
  const context = new AudioContext();
  const source = context.createMediaStreamSource(stream);
  const processor = context.createScriptProcessor(2048, 1, 1);
  const sink = context.createGain();
  const role = roleOverride || (mode === "microphone" ? "seller" : "mixed");
  const nextState = {
    active: true,
    stream,
    context,
    source,
    processor,
    sink,
    ws: null,
    mode,
    role,
    sourceLabel,
    direction,
    language,
    speechOpen: false,
    lastVoiceAt: 0,
    lastEndTurnAt: 0,
    sentChunks: 0,
    reconnectAttempts: 0,
    reconnectTimer: null,
    stopping: false,
  };
  captureStates[mode] = nextState;
  logAudioEvent(nextState, "start");
  sink.gain.value = 0;
  processor.onaudioprocess = (event) => {
    if (captureStates[mode] !== nextState || nextState.ws?.readyState !== WebSocket.OPEN) return;
    const input = event.inputBuffer.getChannelData(0);
    streamAudioFrame(nextState, input);
  };
  connectSTTWebSocket(nextState);
  source.connect(processor);
  processor.connect(sink);
  sink.connect(context.destination);
  for (const track of stream.getAudioTracks()) {
    track.onended = () => {
      if (captureStates[mode] === nextState) {
        logAudioEvent(nextState, "track_ended", track.readyState);
        stopCapture(mode, mode === "microphone" ? "микрофон завершен браузером" : "захват завершен браузером");
      }
    };
    track.onmute = () => {
      if (captureStates[mode] === nextState) {
        logAudioEvent(nextState, "track_mute", track.readyState);
        setAudioStatus(mode, "warn", mode === "microphone" ? "микрофон без сигнала" : "захват без сигнала");
      }
    };
    track.onunmute = () => {
      if (captureStates[mode] === nextState) {
        logAudioEvent(nextState, "track_unmute", track.readyState);
        setAudioStatus(mode, "on", mode === "microphone" ? "микрофон включен" : "захват включен");
      }
    };
  }
}

function connectSTTWebSocket(captureState) {
  if (!captureState?.active) return;
  const { mode, role, sourceLabel, direction, language } = captureState;
  const ws = new WebSocket(sttStreamURL({ role, sourceLabel, direction, language }));
  captureState.ws = ws;
  ws.onopen = () => {
    if (captureStates[mode] === captureState) {
      captureState.reconnectAttempts = 0;
      logAudioEvent(captureState, "ws_open");
      setAudioStatus(mode, "on", mode === "microphone" ? "микрофон стримится" : "захват стримится");
    }
  };
  ws.onmessage = (event) => {
    if (captureStates[mode] !== captureState) return;
    const data = JSON.parse(event.data || "{}");
    if (data.type === "error") {
      setAudioStatus(mode, "err", data.error || "STT stream error");
    } else if (data.type === "stt.final") {
      const roleText = roleLabel(data.role);
      const speakerText = data.speaker ? ` · spk ${data.speaker}` : "";
      setAudioStatus(mode, "on", mode === "microphone" ? "микрофон · распознано" : `захват · ${roleText}${speakerText}`);
    }
  };
  ws.onerror = () => {
    if (captureStates[mode] === captureState) {
      logAudioEvent(captureState, "ws_error");
      setAudioStatus(mode, "warn", "STT stream error · переподключаю");
    }
  };
  ws.onclose = () => {
    if (captureStates[mode] !== captureState || captureState.stopping) return;
    logAudioEvent(captureState, "ws_close");
    scheduleSTTReconnect(captureState);
  };
}

function reconnectCaptureSTT(mode, reason) {
  const captureState = captureStates[mode];
  if (!captureState?.active) return;
  logAudioEvent(captureState, "ws_reconnect_requested", reason);
  setAudioStatus(mode, "warn", mode === "microphone" ? "микрофон · переподключаю STT" : "захват · переподключаю STT");
  if (captureState.reconnectTimer) {
    clearTimeout(captureState.reconnectTimer);
    captureState.reconnectTimer = null;
  }
  if (captureState.ws?.readyState === WebSocket.OPEN) {
    captureState.ws.send(JSON.stringify({ close_stream: {} }));
    captureState.ws.close();
    return;
  }
  if (captureState.ws?.readyState === WebSocket.CONNECTING) {
    captureState.ws.close();
    return;
  }
  scheduleSTTReconnect(captureState);
}

function scheduleSTTReconnect(captureState) {
  const mode = captureState.mode;
  const hasLiveTrack = Array.from(captureState.stream?.getAudioTracks?.() || []).some((track) => track.readyState === "live");
  if (!hasLiveTrack) {
    stopCapture(mode, mode === "microphone" ? "микрофон STT закрыт" : "захват STT закрыт");
    return;
  }
  if (captureState.reconnectTimer) return;
  const attempt = captureState.reconnectAttempts + 1;
  captureState.reconnectAttempts = attempt;
  const delay = Math.min(5000, 500 * Math.pow(1.6, attempt - 1));
  logAudioEvent(captureState, "ws_reconnect_scheduled", `${Math.round(delay)}ms`);
  setAudioStatus(mode, "warn", mode === "microphone" ? "микрофон · переподключаю STT" : "захват · переподключаю STT");
  captureState.reconnectTimer = setTimeout(() => {
    captureState.reconnectTimer = null;
    if (captureStates[mode] !== captureState || captureState.stopping) return;
    logAudioEvent(captureState, "ws_reconnect", `attempt=${attempt}`);
    connectSTTWebSocket(captureState);
  }, delay);
}

function stopCapture(mode, stoppedText = "") {
  const captureState = captureStates[mode];
  if (!captureState?.active) return;
  captureState.stopping = true;
  if (captureState.reconnectTimer) {
    clearTimeout(captureState.reconnectTimer);
    captureState.reconnectTimer = null;
  }
  captureStates[mode] = emptyCaptureState(mode);
  logAudioEvent(captureState, "stop", stoppedText);
  if (captureState.processor) captureState.processor.disconnect();
  if (captureState.sink) captureState.sink.disconnect();
  if (captureState.source) captureState.source.disconnect();
  if (captureState.context) captureState.context.close();
  if (captureState.stream) for (const track of captureState.stream.getTracks()) track.stop();
  if (captureState.ws && captureState.ws.readyState === WebSocket.OPEN) {
    captureState.ws.send(JSON.stringify({ close_stream: {} }));
    captureState.ws.close();
  } else if (captureState.ws && captureState.ws.readyState === WebSocket.CONNECTING) {
    captureState.ws.close();
  }
  if (mode === "microphone") {
    setMicStatus("warn", stoppedText || "микрофон остановлен");
    $("micToggle").textContent = "Проверить микрофон";
  } else {
    setCaptureStatus("warn", stoppedText || "захват остановлен");
    $("captureToggle").textContent = "Включить";
    $("studentCaptureToggle").textContent = "Включить";
  }
  updateBothStatus();
}

function streamAudioFrame(captureState, samples) {
  const now = Date.now();
  const rms = float32Rms(samples);
  const isVoice = rms >= 0.003;
  if (isVoice) {
    captureState.speechOpen = true;
    captureState.lastVoiceAt = now;
  }

  if (captureState.speechOpen && !isVoice && now - captureState.lastVoiceAt >= 650) {
    sendStreamEndTurn(captureState);
    captureState.speechOpen = false;
    setAudioStatus(captureState.mode, "on", captureState.mode === "microphone" ? "микрофон · жду финал" : "захват · жду финал");
    return;
  }

  if (!isVoice && !captureState.speechOpen) {
    setAudioStatus(captureState.mode, "on", captureState.mode === "microphone" ? "микрофон · жду речь" : "захват · жду речь");
    return;
  }

  const pcm = downsampleToPCM16(samples, captureState.context.sampleRate, 16000);
  const pcmBase64 = bytesToBase64(new Uint8Array(pcm.buffer));
  captureState.ws.send(JSON.stringify({ audio_chunk: { content: pcmBase64 } }));
  captureState.sentChunks += 1;
  if (captureState.sentChunks % 12 === 0) {
    setAudioStatus(captureState.mode, "on", captureState.mode === "microphone" ? "микрофон · слышу речь" : "захват · слышу речь");
  }
}

function sendStreamEndTurn(captureState) {
  const now = Date.now();
  if (captureState.ws.readyState !== WebSocket.OPEN || now - captureState.lastEndTurnAt < 700) return;
  captureState.ws.send(JSON.stringify({ end_turn: {} }));
  captureState.lastEndTurnAt = now;
}

function sttStreamURL({ role, sourceLabel, direction = "", language = "" }) {
  const scheme = location.protocol === "https:" ? "wss:" : "ws:";
  const params = new URLSearchParams({ role, source: sourceLabel });
  if (role === "mixed" && sellerSpeaker) {
    params.set("seller_speaker", sellerSpeaker);
  }
  if (direction) params.set("direction", direction);
  if (language) params.set("language", language);
  return `${scheme}//${location.host}/v1/sessions/${sessionId}/stt/live?${params}`;
}

function emptyCaptureState(mode) {
  return {
    active: false,
    stream: null,
    context: null,
    source: null,
    processor: null,
    sink: null,
    ws: null,
    mode,
    role: mode === "microphone" ? "seller" : "mixed",
    sourceLabel: mode === "microphone" ? "browser-microphone-test" : "browser-system-audio",
    direction: "",
    language: "",
    speechOpen: false,
    lastVoiceAt: 0,
    lastEndTurnAt: 0,
    sentChunks: 0,
    reconnectAttempts: 0,
    reconnectTimer: null,
    stopping: false,
  };
}

function float32Rms(samples) {
  let sum = 0;
  for (const sample of samples) sum += sample * sample;
  return Math.sqrt(sum / Math.max(samples.length, 1));
}

function downsampleToPCM16(samples, inRate, outRate) {
  const ratio = inRate / outRate;
  const outLen = Math.floor(samples.length / ratio);
  const out = new Int16Array(outLen);
  for (let i = 0; i < outLen; i++) {
    const sample = samples[Math.floor(i * ratio)];
    const clamped = Math.max(-1, Math.min(1, sample));
    out[i] = clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff;
  }
  return out;
}

function bytesToBase64(bytes) {
  let binary = "";
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
  }
  return btoa(binary);
}

function setCaptureStatus(kind, text) {
  $("captureStatus").textContent = text;
  $("capturePill").textContent = kind === "on" ? "вкл" : kind === "err" ? "ошибка" : "ожидание";
  $("capturePill").className = `status-pill ${kind}`;
  $("studentCaptureStatus").textContent = text;
  $("studentCapturePill").textContent = kind === "on" ? "вкл" : kind === "err" ? "ошибка" : "ожидание";
  $("studentCapturePill").className = `status-pill ${kind}`;
}

function setMicStatus(kind, text) {
  $("micStatus").textContent = text;
  $("micPill").textContent = kind === "on" ? "вкл" : kind === "err" ? "ошибка" : "ожидание";
  $("micPill").className = `status-pill ${kind}`;
}

function setAudioStatus(mode, kind, text) {
  if (mode === "microphone") {
    setMicStatus(kind, text);
  } else {
    setCaptureStatus(kind, text);
  }
  updateBothStatus();
}

function updateBothStatus(text = "") {
  const systemOn = captureStates.system.active;
  const micOn = captureStates.microphone.active;
  if (text) {
    $("bothStatus").textContent = text;
  } else if (systemOn && micOn) {
    $("bothStatus").textContent = "звонок пишется: system=диаризация, mic=мы";
  } else if (systemOn) {
    $("bothStatus").textContent = "включен системный звук · Soniox diarization";
  } else if (micOn) {
    $("bothStatus").textContent = "включен только микрофон";
  } else {
    $("bothStatus").textContent = "system audio = speaker_1/speaker_2, микрофон = мы";
  }
  $("bothToggle").textContent = systemOn && micOn ? "Стоп всё" : "Включить всё";
}

function initSpeakerMap() {
  const select = $("sellerSpeaker");
  select.value = ["1", "2"].includes(sellerSpeaker) ? sellerSpeaker : "";
  select.onchange = () => {
    sellerSpeaker = select.value;
    if (sellerSpeaker) {
      localStorage.setItem(SPEAKER_STORAGE_KEY, sellerSpeaker);
    } else {
      localStorage.removeItem(SPEAKER_STORAGE_KEY);
    }
    renderSpeakerMapStatus();
    renderDialog();
    reconnectCaptureSTT("system", "speaker_map_changed");
  };
  renderSpeakerMapStatus();
}

function renderSpeakerMapStatus() {
  const node = $("speakerMapStatus");
  if (!node) return;
  if (!sellerSpeaker) {
    node.textContent = "выбери свой голос после первых реплик";
    return;
  }
  node.textContent = `мы = speaker ${sellerSpeaker}, клиент = speaker ${otherSpeaker(sellerSpeaker)}`;
}

function otherSpeaker(speaker) {
  if (speaker === "1") return "2";
  if (speaker === "2") return "1";
  return "";
}

function logAudioEvent(captureState, event, detail = "") {
  if (!sessionId || !captureState) return;
  fetch(`/v1/sessions/${sessionId}/audio/log`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
    keepalive: true,
    body: JSON.stringify({
      mode: captureState.mode,
      role: captureState.role,
      source: captureState.sourceLabel,
      event,
      detail,
    }),
  }).catch(() => {});
}

function setStreamStatus(text) {
  $("streamStatus").textContent = text;
}

function scoreColor(score) {
  const readiness = String(score?.readiness || "").toLowerCase();
  if (readiness.includes("green")) return "green";
  if (readiness.includes("red")) return "red";
  return "yellow";
}

function signalColor(value) {
  const state = String(value || "").toLowerCase();
  if (["green", "hit", "ok"].includes(state)) return "green";
  if (["red", "miss", "bad"].includes(state)) return "red";
  if (["yellow", "pending", "gray"].includes(state)) return "yellow";
  return "";
}

function showToast(text) {
  const toast = $("toast");
  toast.textContent = text;
  toast.classList.add("show");
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => toast.classList.remove("show"), 1600);
}

function escapeHtml(value) {
  return String(value || "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#039;",
  }[ch]));
}

$("newSession").onclick = () => {
  createSession().catch((error) => $("session").textContent = error.message);
};
$("studentNewSession").onclick = () => {
  createSession().catch((error) => $("studentSession").textContent = error.message);
};
$("logout").onclick = () => logout().catch((error) => showToast(error.message));
$("studentLogout").onclick = () => logout().catch((error) => showToast(error.message));
$("authLogin").onclick = () => submitAuth("login");
$("authRegister").onclick = () => submitAuth("register");
$("authPassword").onkeydown = (event) => {
  if (event.key === "Enter") submitAuth("login");
};
$("authEmail").onkeydown = (event) => {
  if (event.key === "Enter") $("authPassword").focus();
};
$("bothToggle").onclick = startBothAudio;
$("captureToggle").onclick = () => startCapture({ automatic: false });
$("studentCaptureToggle").onclick = startStudentCapture;
$("micToggle").onclick = startMicTest;
$("openReplyPip").onclick = () => openReplyPip().catch((error) => showToast(error.message));
$("copyReply").onclick = copyReply;
$("replyText").onclick = copyReply;
$("markSaid").onclick = markSaid;
$("refreshReply").onclick = refreshReply;
$("help").onclick = () => requestAssist("button");
$("askAssist").onclick = () => {
  const text = $("assistQuestion").value.trim();
  requestAssist("chat", text);
  $("assistQuestion").value = "";
};
$("clearAssist").onclick = () => {
  $("assistQuestion").value = "";
  $("assistLog").innerHTML = `<div class="empty">Нажми «Помоги» или задай уточнение ниже.</div>`;
};
$("sendSeller").onclick = () => {
  const text = $("manualText").value.trim();
  if (!text) return;
  postEvent({ type: "seller.input", text });
  $("manualText").value = "";
};
$("sendClient").onclick = () => {
  const text = $("manualText").value.trim();
  if (!text) return;
  postEvent({ type: "client.final", text });
  $("manualText").value = "";
};
$("sendPartial").onclick = () => {
  const text = $("manualText").value.trim();
  if (!text) return;
  postEvent({ type: "client.partial", text });
};
$("studentDirection").onchange = () => updateStudentDirection().catch((error) => showToast(error.message));
$("studentSendOriginal").onclick = () => sendStudentOriginal().catch((error) => showToast(error.message));
$("studentHelp").onclick = () => requestStudentAnswer("button");
$("studentAsk").onclick = () => {
  const text = $("studentQuestion").value.trim();
  requestStudentAnswer("chat", text);
  $("studentQuestion").value = "";
};
$("studentClear").onclick = () => {
  $("studentQuestion").value = "";
  $("studentAssistLog").innerHTML = `<div class="empty">Нажми «Помоги» или задай вопрос ниже.</div>`;
};

boot().catch((error) => {
  $("session").textContent = error.message;
  renderAuth(true);
});
