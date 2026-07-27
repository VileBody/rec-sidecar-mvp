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
const ECHO_SUPPRESSION_STORAGE_KEY = "rec-coach-echo-suppression";
const AEC3_ENABLED_STORAGE_KEY = "rec-coach-aec3-enabled";
const AEC3_URL_STORAGE_KEY = "rec-coach-aec3-url";
let sellerSpeaker = localStorage.getItem(SPEAKER_STORAGE_KEY) || "";
let echoSuppressionEnabled = localStorage.getItem(ECHO_SUPPRESSION_STORAGE_KEY) !== "0";
let aec3Enabled = localStorage.getItem(AEC3_ENABLED_STORAGE_KEY) === "1";
let pendingStudentDirection = "";
let studentAnswerLanguage = {};
let replyPipWindow = null;
let audioAdvancedVisible = false;
let manualGenerateInFlight = false;
const audioAec3State = {
  url: localStorage.getItem(AEC3_URL_STORAGE_KEY) || "ws://127.0.0.1:8122",
  ws: null,
  ready: false,
  connecting: false,
  status: "off",
  error: "",
  cleanFrames: 0,
  farFrames: 0,
  lastStats: null,
  lastConnectAttemptAt: 0,
};
const audioEchoState = {
  systemRing: [],
  maxMs: 2500,
  suppressedFrames: 0,
  sentFrames: 0,
  doubleTalkFrames: 0,
  lastLogAt: 0,
  lastBestCorr: 0,
  lastResidual: 1,
  lastLagMs: 0,
  serverRejected: 0,
  serverEchoRejected: 0,
  serverTextRejected: 0,
  serverSourceSuppressed: 0,
  recentRejects: [],
  attributionCounts: {
    micSeller: 0,
    micClient: 0,
    systemClient: 0,
    systemSeller: 0,
    systemSpeaker: 0,
    mixedSeller: 0,
    mixedClient: 0,
    other: 0,
  },
  lastAttribution: "",
  thresholds: {
    vadRms: 0.003,
    echoCorrReject: 0.62,
    echoCorrMaybe: 0.45,
    residualSellerMin: 0.38,
    minSystemRms: 0.0025,
    maxLagMs: 1000,
    minLagMs: 20,
  },
};
const ADMIN_USER_TYPES = ["sales", "student"];
let adminState = {
  mode: "sessions",
  userType: "sales",
  items: [],
  selectedId: "",
  draftTitle: "",
  draftContent: "",
  sessions: [],
  sessionsLoading: false,
  sessionsError: "",
  selectedSessionId: "",
  sessionDetail: null,
  sessionDetailLoading: false,
  sessionDetailError: "",
  detailTab: "transcript",
  loading: false,
  saving: false,
  dirty: false,
  noAccess: false,
  error: "",
  saveError: "",
  status: "загрузка",
  statusKind: "warn",
  newSeq: 0,
};

const telemetry = {
  clientEventSeq: 0,
  lastSnapshotPerf: 0,
  lastSnapshotWall: 0,
  lastSnapshotLogPerf: 0,
  lastReplyRenderLogPerf: 0,
  lastReplyRenderSignature: "",
  lastStateVersion: 0,
  lastAutoGenerationId: "",
  lastManualGenerationId: "",
  loggedVisibleGenerations: new Set(),
  now() {
    return performance.now();
  },
  wallNow() {
    return Date.now();
  },
  newClientEventId(prefix) {
    this.clientEventSeq += 1;
    return `${prefix}_${this.clientEventSeq}`;
  },
  log(event, data = {}) {
    this.logForSession(sessionId, event, data);
  },
  logForSession(targetSessionId, event, data = {}) {
    if (!targetSessionId) return;
    const payload = {
      event,
      source: data.source || "",
      role: data.role || "",
      mode: data.mode || "",
      generation_id: data.generation_id || "",
      state_version: Number(data.state_version || this.lastStateVersion || 0),
      duration_ms: Number(data.duration_ms || 0),
      detail: data.detail || "",
      data: data.data || {},
    };
    fetch(`/v1/sessions/${encodeURIComponent(targetSessionId)}/telemetry/client-log`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      keepalive: true,
      body: JSON.stringify(payload),
    }).catch(() => {});
  },
  noteSnapshotReceived(raw = "") {
    this.lastSnapshotPerf = this.now();
    this.lastSnapshotWall = this.wallNow();
    this.lastStateVersion += 1;
    if (this.lastSnapshotPerf - this.lastSnapshotLogPerf >= 2000) {
      this.lastSnapshotLogPerf = this.lastSnapshotPerf;
      this.log("snapshot_received", {
        state_version: this.lastStateVersion,
        data: { bytes: raw.length },
      });
    }
  },
  noteRendered() {
    const snapshotPerf = this.lastSnapshotPerf || this.now();
    requestAnimationFrame(() => {
      const renderLatency = Math.max(0, this.now() - snapshotPerf);
      const autoGenerationId = state?.seller_generation_id || "";
      const manualGenerationId = state?.seller_immediate_generation_id || "";
      const signature = [
        autoGenerationId,
        manualGenerationId,
        state?.seller_streaming ? "auto:1" : "auto:0",
        state?.seller_immediate_streaming ? "manual:1" : "manual:0",
      ].join("|");
      const shouldLogRender =
        signature !== this.lastReplyRenderSignature ||
        this.now() - this.lastReplyRenderLogPerf >= 3000;
      if (shouldLogRender) {
        this.lastReplyRenderSignature = signature;
        this.lastReplyRenderLogPerf = this.now();
        this.log("reply_render_done", {
          state_version: this.lastStateVersion,
          generation_id: manualGenerationId || autoGenerationId,
          duration_ms: renderLatency,
          data: {
            seller_streaming: Boolean(state?.seller_streaming),
            immediate_streaming: Boolean(state?.seller_immediate_streaming),
          },
        });
      }
      this.noteVisibleGeneration(autoGenerationId, state?.seller_draft, "auto");
      this.noteVisibleGeneration(manualGenerationId, state?.seller_draft_immediate, "manual");
    });
  },
  noteVisibleGeneration(generationId, text, source) {
    if (!generationId || !text || this.loggedVisibleGenerations.has(generationId)) return;
    this.loggedVisibleGenerations.add(generationId);
    this.log("reply_visible", {
      source,
      generation_id: generationId,
      state_version: this.lastStateVersion,
      duration_ms: Math.max(0, this.now() - (this.lastSnapshotPerf || this.now())),
      data: { text_len: String(text || "").length },
    });
  },
};

function sessionStorageKey() {
  const identity = currentUser?.id || currentUser?.email || "dev";
  const role = currentRole();
  return `rec-coach-session:${identity}:${role}`;
}

function rememberSession(id) {
  if (!id) return;
  localStorage.setItem(sessionStorageKey(), id);
}

function sessionStateStorageKey(id = sessionId) {
  return `${sessionStorageKey()}:state:${id || "none"}`;
}

function rememberSessionState(id = sessionId, nextState = state) {
  if (!id || !nextState) return;
  try {
    localStorage.setItem(sessionStateStorageKey(id), JSON.stringify({
      session_id: id,
      saved_at: new Date().toISOString(),
      state: nextState,
    }));
  } catch (_) {
    // Local snapshots are a reload convenience only; server state remains source of truth.
  }
}

function readSessionState(id) {
  if (!id) return null;
  try {
    const cached = JSON.parse(localStorage.getItem(sessionStateStorageKey(id)) || "null");
    if (cached?.session_id !== id || !cached.state) return null;
    return cached.state;
  } catch (_) {
    return null;
  }
}

function forgetSessionState(id = sessionId) {
  if (!id) return;
  localStorage.removeItem(sessionStateStorageKey(id));
}

function forgetSession() {
  forgetSessionState();
  localStorage.removeItem(sessionStorageKey());
}

function isStudentUser() {
  return currentRole() === "student";
}

function isAdminUser() {
  return currentRole() === "admin";
}

function currentRole() {
  return currentUser?.role || currentUser?.user_type || "sales";
}

async function boot() {
  telemetry.log("session_boot_started");
  initAudioControls();
  initSpeakerMap();
  const ok = await loadMe();
  if (ok) await enterCurrentApp();
  telemetry.log("session_boot_done");
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
  const label = isDevUser ? "dev mode" : (currentUser?.email || "не авторизован");
  document.body.classList.toggle("auth-locked", locked);
  $("authPanel").hidden = !locked;
  $("authStatus").textContent = label;
  $("studentAuthStatus").textContent = label;
  $("adminAuthStatus").textContent = label;
  $("logout").hidden = locked || isDevUser;
  $("studentLogout").hidden = locked || isDevUser;
  $("adminLogout").hidden = locked || isDevUser;
  $("newSession").disabled = locked;
  $("studentNewSession").disabled = locked;
  $("adminRefreshPrompts").disabled = locked || !isAdminUser();
  $("salesApp").hidden = !locked && (isStudentUser() || isAdminUser());
  $("studentApp").hidden = locked || !isStudentUser();
  $("adminApp").hidden = locked || !isAdminUser();
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
    await enterCurrentApp();
  } catch (error) {
    $("authError").textContent = error.message;
  } finally {
    $("authLogin").disabled = false;
    $("authRegister").disabled = false;
  }
}

async function enterCurrentApp() {
  if (isAdminUser()) {
    stopCapture("system");
    stopCapture("microphone");
    stopSessionLive();
    sessionId = "";
    state = null;
    adminState.mode = "sessions";
    renderAdmin();
    await loadAdminSessions();
    return;
  }
  await restoreSessionOrCreate();
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

function stopCapturesForSessionChange(nextSessionId) {
  for (const mode of ["system", "microphone"]) {
    const captureState = captureStates[mode];
    if (!captureState?.active) continue;
    if (captureState.sessionId === nextSessionId) continue;
    stopCapture(mode, "остановлен при смене сессии");
  }
}

function applySession(data) {
  stopCapturesForSessionChange(data.session_id || "");
  stopSessionLive();
  sessionId = data.session_id;
  $("session").textContent = sessionId;
  $("studentSession").textContent = sessionId;
  state = data.state;
  rememberSession(sessionId);
  rememberSessionState(sessionId, state);
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
  const cachedState = readSessionState(id);
  if (cachedState) {
    sessionId = id;
    state = cachedState;
    $("session").textContent = sessionId;
    $("studentSession").textContent = sessionId;
    render();
  }
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
    forgetSessionState(id);
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
  forgetSessionState();
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
  telemetry.log("sse_connected");
  events.addEventListener("snapshot", (event) => {
    telemetry.noteSnapshotReceived(event.data || "");
    state = JSON.parse(event.data);
    rememberSessionState(sessionId, state);
    render();
    telemetry.noteRendered();
  });
  events.onerror = () => {
    telemetry.log("sse_disconnected", { detail: "eventsource error" });
    setStreamStatus("reconnecting...");
  };
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
      telemetry.noteSnapshotReceived("");
      state = await res.json();
      rememberSessionState(sessionId, state);
      render();
      telemetry.noteRendered();
    } catch (_) {
      // EventSource remains the primary live path; polling only smooths over proxy hiccups.
    }
  };
  pollTimer = setInterval(poll, 2000);
  setTimeout(poll, 700);
  telemetry.log("polling_fallback_started");
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
  renderPipelineStatus();
  renderStage();
  renderAssist();
  renderStudent();
  renderSpeakerMapStatus();
}

function renderAdmin() {
  if (!$("adminApp")) return;
  const sessionsMode = adminState.mode === "sessions";
  $("adminSessionsTab").classList.toggle("active", sessionsMode);
  $("adminPromptsTab").classList.toggle("active", !sessionsMode);
  $("adminSessionsView").hidden = !sessionsMode;
  $("adminPromptsView").hidden = sessionsMode;
  $("adminRefreshPrompts").textContent = sessionsMode ? "Обновить" : "Обновить";
  if (sessionsMode) {
    renderAdminSessions();
    return;
  }
  renderAdminPrompts();
}

function renderAdminPrompts() {
  const list = $("adminPromptList");
  const tabs = document.querySelectorAll("[data-admin-type]");
  for (const tab of tabs) {
    const active = tab.dataset.adminType === adminState.userType;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-selected", active ? "true" : "false");
    tab.disabled = adminState.loading || adminState.saving;
  }

  if (adminState.loading) {
    list.innerHTML = `<div class="empty">Загружаю prompts...</div>`;
  } else if (adminState.noAccess) {
    list.innerHTML = `<div class="empty">У этого аккаунта нет доступа к админке.</div>`;
  } else if (adminState.error) {
    list.innerHTML = `<div class="empty">Не удалось загрузить prompts: ${escapeHtml(adminState.error)}</div>`;
  } else {
    const items = adminItemsForCurrentType();
    if (!items.length) {
      list.innerHTML = `<div class="empty">Для ${escapeHtml(adminState.userType)} пока нет prompts/playbooks.</div>`;
    } else {
      list.innerHTML = items.map((item) => {
        const identity = adminPromptId(item);
        const active = identity === adminState.selectedId ? " active" : "";
        const title = item.title || item.key || "Без названия";
        const kind = item.kind || "prompt";
        const keyLabel = item.key || "новый_key";
        const updated = item.updated_at ? `обновлено ${formatDateTime(item.updated_at)}` : "без даты обновления";
        const meta = `${kind} · ${updated}`;
        return `
          <button class="admin-prompt-item${active}" data-admin-prompt="${escapeHtml(identity)}">
            <span class="admin-prompt-title">${escapeHtml(title)}</span>
            <span class="admin-prompt-key">${escapeHtml(kind)}/${escapeHtml(keyLabel)}</span>
            <span class="admin-prompt-meta">${escapeHtml(meta)}</span>
          </button>`;
      }).join("");
      for (const button of list.querySelectorAll("[data-admin-prompt]")) {
        button.onclick = () => selectAdminPrompt(button.dataset.adminPrompt);
      }
    }
  }

  const selected = selectedAdminPrompt();
  $("adminPromptTitle").value = selected ? adminState.draftTitle : "";
  $("adminPromptKey").value = selected?.key || "";
  $("adminPromptContent").value = selected ? adminState.draftContent : "";
  $("adminUpdatedAt").textContent = selected?.updated_at
    ? `Обновлено ${formatDateTime(selected.updated_at)}`
    : "Обновление еще не приходило";
  $("adminEditorNote").textContent = adminEditorNote(selected);
  renderAdminStatusOnly();
}

function renderAdminSessions() {
  const list = $("adminSessionsList");
  $("adminSessionsStatus").textContent = adminState.sessionsLoading
    ? "загрузка"
    : adminState.sessionsError
      ? "ошибка"
      : `${adminState.sessions.length} созв.`;
  $("adminSessionsStatus").className = `status-pill ${adminState.sessionsError ? "err" : adminState.sessionsLoading ? "warn" : "on"}`;
  $("adminRefreshPrompts").disabled = adminState.sessionsLoading || !isAdminUser();

  if (adminState.sessionsLoading && !adminState.sessions.length) {
    list.innerHTML = `<div class="empty">Загружаю сессии...</div>`;
  } else if (adminState.sessionsError) {
    list.innerHTML = `<div class="empty">Не удалось загрузить созвоны: ${escapeHtml(adminState.sessionsError)}</div>`;
  } else if (!adminState.sessions.length) {
    list.innerHTML = `<div class="empty">Пока нет созвонов. Как только пользователи начнут сессии, они появятся здесь.</div>`;
  } else {
    list.innerHTML = `
      <div class="admin-session-row admin-session-head">
        <div>Пользователь</div>
        <div>Роль</div>
        <div>Старт</div>
        <div>Длительн.</div>
        <div>Реплики</div>
        <div>Events</div>
      </div>
      ${adminState.sessions.map(adminSessionRowHTML).join("")}
    `;
    for (const button of list.querySelectorAll("[data-admin-session]")) {
      button.onclick = () => selectAdminSession(button.dataset.adminSession);
    }
  }
  renderAdminSessionDetail();
}

function adminSessionRowHTML(item) {
  const active = item.session_id === adminState.selectedSessionId ? " active" : "";
  return `
    <button class="admin-session-row${active}" data-admin-session="${escapeHtml(item.session_id || "")}">
      <div class="admin-session-cell primary">
        <span class="admin-session-email">${escapeHtml(item.user_email || "unknown")}</span>
        <span class="admin-session-id">${escapeHtml(item.session_id || "")}</span>
      </div>
      <div class="admin-session-cell">${escapeHtml(item.user_role || "")}</div>
      <div class="admin-session-cell">${escapeHtml(formatDateTime(item.created_at))}</div>
      <div class="admin-session-cell">${escapeHtml(formatDuration(item.duration_seconds || 0))}</div>
      <div class="admin-session-cell">${escapeHtml(String(item.transcript_count || 0))}</div>
      <div class="admin-session-cell">${escapeHtml(String(item.event_count || 0))}</div>
    </button>
  `;
}

function renderAdminSessionDetail() {
  const detail = adminState.sessionDetail;
  const selected = adminState.sessions.find((item) => item.session_id === adminState.selectedSessionId);
  $("adminSessionTitle").textContent = selected
    ? `${selected.user_email || "unknown"} · ${selected.session_id || ""}`
    : "Выберите созвон";
  $("adminSessionMeta").textContent = selected
    ? `${formatDuration(selected.duration_seconds || 0)} · ${selected.event_count || 0} events`
    : "eventlog";
  for (const tab of document.querySelectorAll("[data-admin-detail-tab]")) {
    const active = tab.dataset.adminDetailTab === adminState.detailTab;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-selected", active ? "true" : "false");
  }
  const box = $("adminSessionDetail");
  if (adminState.sessionDetailLoading) {
    box.innerHTML = `<div class="empty">Загружаю детали созвона...</div>`;
    return;
  }
  if (adminState.sessionDetailError) {
    box.innerHTML = `<div class="empty">Не удалось загрузить детали: ${escapeHtml(adminState.sessionDetailError)}</div>`;
    return;
  }
  if (!selected) {
    box.innerHTML = `<div class="empty">Выберите созвон слева.</div>`;
    return;
  }
  if (!detail) {
    box.innerHTML = `<div class="empty">Детали еще не загружены.</div>`;
    return;
  }
  if (adminState.detailTab === "events") {
    const eventsList = Array.isArray(detail.events) ? detail.events : [];
    box.innerHTML = eventsList.length
      ? eventsList.slice(-300).map(adminEventHTML).join("")
      : `<div class="empty">Eventlog пуст.</div>`;
    return;
  }
  const transcript = Array.isArray(detail.transcript) ? detail.transcript : [];
  box.innerHTML = transcript.length
    ? transcript.slice(-300).map(adminTranscriptHTML).join("")
    : `<div class="empty">Транскрипт пуст. Можно переключиться на Eventlog и проверить сырые события.</div>`;
}

function adminTranscriptHTML(item) {
  const finalLabel = item.final ? "final" : "partial";
  const speaker = item.speaker ? ` · ${item.speaker}` : "";
  return `
    <div class="admin-transcript-item">
      <div class="admin-detail-meta">${escapeHtml(roleLabel(item.role || ""))}${escapeHtml(speaker)} · ${escapeHtml(formatTime(item.created_at))} · ${escapeHtml(finalLabel)}</div>
      <div>${escapeHtml(item.text || "")}</div>
    </div>
  `;
}

function adminEventHTML(event) {
  const payload = event.data ? JSON.stringify(event.data, null, 2) : "";
  return `
    <div class="admin-event-item">
      <div class="admin-detail-meta">${escapeHtml(event.type || "")} · ${escapeHtml(event.source || "")} · ${escapeHtml(formatTime(event.created_at))}</div>
      <div>${escapeHtml(event.id || "")}</div>
      ${payload ? `<pre>${escapeHtml(payload)}</pre>` : ""}
    </div>
  `;
}

function renderAdminStatusOnly() {
  const selected = selectedAdminPrompt();
  const disabled = adminEditorDisabled(selected);
  $("adminStatus").textContent = adminState.status;
  $("adminStatus").className = `status-pill ${adminState.statusKind}`;
  $("adminPromptTitle").disabled = disabled;
  $("adminPromptKey").disabled = !selected || adminState.loading || adminState.saving || adminState.noAccess || Boolean(adminState.error);
  $("adminPromptKey").readOnly = !selected?.is_new;
  $("adminPromptContent").disabled = disabled;
  $("adminSavePrompt").disabled = disabled || !adminState.dirty;
  $("adminSavePrompt").textContent = adminState.saving ? "Сохраняю..." : "Сохранить";
  $("adminRevertPrompt").disabled = disabled || !adminState.dirty;
  $("adminRefreshPrompts").disabled = adminState.loading || adminState.saving || !isAdminUser();
  $("adminNewPrompt").disabled = adminState.loading || adminState.saving || adminState.noAccess || Boolean(adminState.error);
  $("adminNewPlaybook").disabled = adminState.loading || adminState.saving || adminState.noAccess || Boolean(adminState.error);
  if (adminState.saveError) {
    $("adminEditorNote").textContent = `Не удалось сохранить: ${adminState.saveError}`;
  }
}

function adminEditorDisabled(selected) {
  return adminState.loading || adminState.saving || adminState.noAccess || Boolean(adminState.error) || !selected;
}

function adminEditorNote(selected) {
  if (adminState.noAccess) return "Попросите владельца выдать admin-доступ.";
  if (adminState.error) return "Можно обновить список, когда backend endpoint будет готов.";
  if (!selected) return "Выберите prompt или playbook слева.";
  if (adminState.dirty) return "Есть несохраненные изменения.";
  return "Редактирование применяется через admin API.";
}

function adminItemsForCurrentType() {
  return adminState.items
    .filter((item) => (item.user_type || "sales") === adminState.userType)
    .sort((a, b) => `${a.kind || "prompt"}:${a.key || ""}`.localeCompare(`${b.kind || "prompt"}:${b.key || ""}`));
}

function adminPromptId(item) {
  if (!item) return "";
  if (item.is_new) return item.id;
  return `${item.user_type || "sales"}:${item.kind || "prompt"}:${item.key || item.id || ""}`;
}

function selectedAdminPrompt() {
  const items = adminItemsForCurrentType();
  return items.find((item) => adminPromptId(item) === adminState.selectedId) || null;
}

function syncAdminSelection(preferExisting = true) {
  const items = adminItemsForCurrentType();
  if (!items.length) {
    adminState.selectedId = "";
    adminState.draftTitle = "";
    adminState.draftContent = "";
    adminState.dirty = false;
    return;
  }
  const hasCurrent = items.some((item) => adminPromptId(item) === adminState.selectedId);
  if (!preferExisting || !hasCurrent) {
    adminState.selectedId = adminPromptId(items[0]);
  }
  resetAdminDraftFromSelection();
}

function resetAdminDraftFromSelection() {
  const selected = selectedAdminPrompt();
  adminState.draftTitle = selected?.title || "";
  adminState.draftContent = selected?.content ?? selected?.body ?? "";
  adminState.dirty = false;
  adminState.saveError = "";
}

async function loadAdminPrompts() {
  adminState.loading = true;
  adminState.saving = false;
  adminState.noAccess = false;
  adminState.error = "";
  adminState.saveError = "";
  adminState.status = "загрузка";
  adminState.statusKind = "warn";
  renderAdmin();
  try {
    const res = await fetch("/v1/admin/prompts", {
      cache: "no-store",
      credentials: "same-origin",
    });
    if (res.status === 401) {
      currentUser = null;
      renderAuth(true);
      return;
    }
    if (res.status === 403) {
      adminState.items = [];
      adminState.noAccess = true;
      adminState.status = "нет доступа";
      adminState.statusKind = "err";
      syncAdminSelection(false);
      return;
    }
    if (!res.ok) {
      throw new Error(await responseError(res));
    }
    const data = await res.json();
    adminState.items = Array.isArray(data.items) ? data.items : [];
    adminState.status = adminState.items.length ? "готово" : "пусто";
    adminState.statusKind = "on";
    syncAdminSelection(true);
  } catch (error) {
    adminState.error = error.message;
    adminState.status = "ошибка";
    adminState.statusKind = "err";
  } finally {
    adminState.loading = false;
    renderAdmin();
  }
}

async function loadAdminSessions() {
  adminState.sessionsLoading = true;
  adminState.sessionsError = "";
  adminState.sessionDetailError = "";
  renderAdmin();
  try {
    const res = await fetch("/v1/admin/sessions?limit=200", {
      cache: "no-store",
      credentials: "same-origin",
    });
    if (res.status === 401) {
      currentUser = null;
      renderAuth(true);
      return;
    }
    if (res.status === 403) {
      adminState.sessions = [];
      adminState.sessionsError = "нет admin-доступа";
      return;
    }
    if (!res.ok) {
      throw new Error(await responseError(res));
    }
    const data = await res.json();
    adminState.sessions = Array.isArray(data.items) ? data.items : [];
    const hasSelection = adminState.sessions.some((item) => item.session_id === adminState.selectedSessionId);
    if (!hasSelection) {
      adminState.selectedSessionId = adminState.sessions[0]?.session_id || "";
      adminState.sessionDetail = null;
    }
  } catch (error) {
    adminState.sessionsError = error.message;
  } finally {
    adminState.sessionsLoading = false;
    renderAdmin();
  }
  if (adminState.selectedSessionId && !adminState.sessionDetail) {
    await loadAdminSessionDetail(adminState.selectedSessionId);
  }
}

async function selectAdminSession(sessionID) {
  if (!sessionID || sessionID === adminState.selectedSessionId) return;
  adminState.selectedSessionId = sessionID;
  adminState.sessionDetail = null;
  adminState.sessionDetailError = "";
  renderAdmin();
  await loadAdminSessionDetail(sessionID);
}

async function loadAdminSessionDetail(sessionID) {
  adminState.sessionDetailLoading = true;
  adminState.sessionDetailError = "";
  renderAdmin();
  try {
    const res = await fetch(`/v1/admin/sessions/${encodeURIComponent(sessionID)}`, {
      cache: "no-store",
      credentials: "same-origin",
    });
    if (res.status === 401) {
      currentUser = null;
      renderAuth(true);
      return;
    }
    if (!res.ok) {
      throw new Error(await responseError(res));
    }
    adminState.sessionDetail = await res.json();
  } catch (error) {
    adminState.sessionDetailError = error.message;
  } finally {
    adminState.sessionDetailLoading = false;
    renderAdmin();
  }
}

async function selectAdminMode(mode) {
  if (!["sessions", "prompts"].includes(mode) || adminState.mode === mode) return;
  if (adminState.dirty && !confirm("Оставить несохраненные изменения?")) return;
  adminState.mode = mode;
  renderAdmin();
  if (mode === "sessions" && !adminState.sessions.length && !adminState.sessionsLoading) {
    await loadAdminSessions();
  }
  if (mode === "prompts" && !adminState.items.length && !adminState.loading) {
    await loadAdminPrompts();
  }
}

function selectAdminUserType(type) {
  if (!ADMIN_USER_TYPES.includes(type)) return;
  if (adminState.dirty && !confirm("Оставить несохраненные изменения?")) return;
  adminState.userType = type;
  adminState.saveError = "";
  syncAdminSelection(false);
  adminState.status = adminState.error ? "ошибка" : adminState.noAccess ? "нет доступа" : "готово";
  adminState.statusKind = adminState.error || adminState.noAccess ? "err" : "on";
  renderAdmin();
}

function selectAdminPrompt(id) {
  if (id === adminState.selectedId) return;
  if (adminState.dirty && !confirm("Оставить несохраненные изменения?")) return;
  adminState.selectedId = id;
  adminState.status = "готово";
  adminState.statusKind = "on";
  resetAdminDraftFromSelection();
  renderAdmin();
}

function markAdminDirty() {
  adminState.draftTitle = $("adminPromptTitle").value;
  adminState.draftContent = $("adminPromptContent").value;
  const selected = selectedAdminPrompt();
  if (selected?.is_new) {
    selected.key = $("adminPromptKey").value.trim();
  }
  adminState.dirty = true;
  adminState.saveError = "";
  adminState.status = "есть изменения";
  adminState.statusKind = "warn";
  renderAdminStatusOnly();
}

function createAdminPromptDraft(kind) {
  if (adminState.dirty && !confirm("Оставить несохраненные изменения?")) return;
  adminState.newSeq += 1;
  const item = {
    id: `new:${adminState.userType}:${kind}:${adminState.newSeq}`,
    user_type: adminState.userType,
    kind,
    key: "",
    title: "",
    content: "",
    enabled: true,
    is_new: true,
  };
  adminState.items = [item, ...adminState.items];
  adminState.selectedId = adminPromptId(item);
  adminState.draftTitle = "";
  adminState.draftContent = "";
  adminState.dirty = true;
  adminState.saveError = "";
  adminState.status = "новый";
  adminState.statusKind = "warn";
  renderAdmin();
  $("adminPromptKey").focus();
}

function revertAdminPrompt() {
  const selected = selectedAdminPrompt();
  if (selected?.is_new) {
    const id = adminPromptId(selected);
    adminState.items = adminState.items.filter((item) => adminPromptId(item) !== id);
    syncAdminSelection(false);
    adminState.status = adminState.items.length ? "готово" : "пусто";
    adminState.statusKind = "on";
    renderAdmin();
    return;
  }
  resetAdminDraftFromSelection();
  adminState.status = "готово";
  adminState.statusKind = "on";
  renderAdmin();
}

async function saveAdminPrompt() {
  const selected = selectedAdminPrompt();
  if (!selected || adminState.saving) return;
  const payload = {
    user_type: adminState.userType,
    kind: selected.kind || "prompt",
    key: selected.key || $("adminPromptKey").value.trim(),
    title: adminState.draftTitle.trim(),
    content: adminState.draftContent,
  };
  const target = payload.key;
  if (!target) {
    adminState.saveError = "key is required";
    adminState.status = "ошибка";
    adminState.statusKind = "err";
    renderAdminStatusOnly();
    return;
  }
  adminState.saving = true;
  adminState.saveError = "";
  adminState.status = "сохранение";
  adminState.statusKind = "warn";
  renderAdminStatusOnly();
  try {
    const res = await fetch(`/v1/admin/prompts/${encodeURIComponent(target)}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify(payload),
    });
    if (res.status === 401) {
      currentUser = null;
      renderAuth(true);
      return;
    }
    if (res.status === 403) {
      adminState.noAccess = true;
      adminState.status = "нет доступа";
      adminState.statusKind = "err";
      return;
    }
    if (!res.ok) {
      throw new Error(await responseError(res));
    }
    const data = await res.json().catch(() => ({}));
    const saved = data.item || data;
    const oldId = adminPromptId(selected);
    const next = {
      ...selected,
      ...payload,
      ...saved,
      user_type: payload.user_type,
      kind: saved.kind ?? payload.kind,
      key: payload.key,
      title: saved.title ?? payload.title,
      content: saved.content ?? payload.content,
      is_new: false,
    };
    adminState.items = adminState.items.map((item) => adminPromptId(item) === oldId ? next : item);
    adminState.selectedId = adminPromptId(next);
    adminState.draftTitle = next.title || "";
    adminState.draftContent = next.content || "";
    adminState.dirty = false;
    adminState.status = "сохранено";
    adminState.statusKind = "on";
    showToast("Prompt сохранен");
  } catch (error) {
    adminState.saveError = error.message;
    adminState.status = "ошибка";
    adminState.statusKind = "err";
  } finally {
    adminState.saving = false;
    renderAdmin();
  }
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
  if (role === "student_original") return "собеседник";
  if (role === "student_self") return "мы";
  if (String(role || "").startsWith("speaker_")) {
    const speaker = String(role).replace("speaker_", "");
    if (sellerSpeaker && speaker === sellerSpeaker) return `мы · speaker ${speaker}`;
    if (sellerSpeaker && speaker === otherSpeaker(sellerSpeaker)) return `клиент · speaker ${speaker}`;
    return role.replace("_", " ");
  }
  return role || "speaker";
}

function sourceLabel(source) {
  if (source === "remote_audio" || source === "browser-system-audio" || source === "student_system_audio" || source === "student-system-audio") return "system";
  if (source === "seller_mic" || source === "browser-microphone-test" || source === "student_mic" || source === "student-mic") return "mic";
  if (source === "mixed_audio") return "mixed";
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

function formatDateTime(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleString("ru-RU", {
    day: "2-digit",
    month: "2-digit",
    year: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function formatDuration(value) {
  const seconds = Math.max(0, Number(value || 0));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  const restSeconds = seconds % 60;
  if (minutes < 60) return restSeconds ? `${minutes}m ${restSeconds}s` : `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  const restMinutes = minutes % 60;
  return restMinutes ? `${hours}h ${restMinutes}m` : `${hours}h`;
}

function renderReply() {
  const text = state?.seller_draft || "";
  $("replyText").innerHTML = text
    ? `<div class="rich-text">${renderRichText(text)}</div>`
    : `<div class="rich-text">Жду речь клиента...</div>`;
  $("replyText").classList.toggle("muted", !text);
  const immediateText = state?.seller_draft_immediate || "";
  $("immediateReplyText").innerHTML = immediateText
    ? `<div class="rich-text">${renderRichText(immediateText)}</div>`
    : `<div class="rich-text">Жду ручную генерацию...</div>`;
  $("immediateReplyText").classList.toggle("muted", !immediateText);
  const streaming = state?.seller_streaming ? "генерируется" : "готово";
  $("replyMeta").textContent = text ? streaming : "обновляется по речи клиента";
  $("copyReply").disabled = !text;
  syncGenerateReplyButton();
  renderManualReplyStatus();
  syncReplyPip();
}

function syncGenerateReplyButton() {
  const button = $("generateReply");
  const label = $("generateReplyLabel");
  if (!button || !label) return;
  const generating = manualReplyPending();
  button.disabled = !state || generating;
  button.classList.toggle("loading", generating);
  label.textContent = generating ? "Ушел думать" : "Сгенерить сейчас";
}

function renderPipelineStatus() {
  const node = $("pipelineStatus");
  if (!node) return;
  if (!state) {
    node.innerHTML = `<div class="pipeline-empty">pipeline: жду сессию</div>`;
    return;
  }
  const stageStatus = latestPipelineStatus("stage");
  const gateStatus =
    latestPipelineStatus("pivot_gate") ||
    latestPipelineStatus("ready_gate") ||
    latestPipelineStatus("zai_gate");
  const replyStatus = latestPipelineStatus("seller_reply");
  const committedEvent = latestEvent("stage.committed");
  const candidateEvent = latestEvent("stage.candidate");
  const stage = state.stage_committed || state.stage_candidate;
  const stageSince = committedEvent || candidateEvent;
  const stageDuration = stageSince ? humanDuration((Date.now() - new Date(stageSince.created_at).getTime()) / 1000) : "еще нет";
  const stageLabel = stage?.stage ? `${stage.stage}${stage.title ? ` · ${stage.title}` : ""}` : "stage неизвестен";
  node.innerHTML = `
    <div class="pipeline-head">
      <span>статус</span>
      <span>${escapeHtml(stageLabel)} · ${escapeHtml(stageDuration)}</span>
    </div>
    <div class="pipeline-grid">
      ${pipelineCardHTML("Stage", stageStatus, "ждем речи клиента")}
      ${pipelineCardHTML("ZAI gate", gateStatus, "ждет новой partial-фразы")}
      ${pipelineCardHTML("Gemini / reply", replyStatus, state.seller_streaming ? "получаем реплику" : "ждем следующего момента")}
    </div>
  `;
}

function renderManualReplyStatus() {
  const node = $("manualReplyStatus");
  if (!node) return;
  if (!state) {
    node.innerHTML = `<div class="pipeline-empty">manual: жду сессию</div>`;
    return;
  }
  const manualStatus = latestPipelineStatus("manual_reply");
  node.innerHTML = `
    <div class="pipeline-grid">
      ${pipelineCardHTML("Gemini direct", manualStatus, state.seller_immediate_streaming ? "получаем реплику" : "готов к прямому запросу")}
    </div>
  `;
}

function manualReplyPending() {
  if (manualGenerateInFlight) return true;
  if (state?.seller_immediate_streaming) return true;
  const event = latestPipelineStatus("manual_reply");
  const status = eventData(event).status || "";
  if (status !== "sent" && status !== "queued") return false;
  const startedAt = new Date(event?.created_at || "").getTime();
  if (Number.isNaN(startedAt)) return true;
  return Date.now() - startedAt < 90000;
}

function pipelineCardHTML(label, event, fallback) {
  const data = eventData(event);
  const status = data.status || "";
  const kind = pipelineKind(status);
  const primary = event ? pipelineStatusText(data, event) : fallback;
  const detailBits = [];
  if (data.elapsed_ms) detailBits.push(`${Math.round(Number(data.elapsed_ms))} ms`);
  if (data.model) detailBits.push(data.model);
  if (data.action) detailBits.push(data.action);
  if (data.trigger) detailBits.push(data.trigger);
  const detail = detailBits.join(" · ");
  return `
    <div class="pipeline-card ${kind}">
      <div class="pipeline-label">${escapeHtml(label)}</div>
      <div class="pipeline-primary">${escapeHtml(primary)}</div>
      ${detail ? `<div class="pipeline-detail">${escapeHtml(detail)}</div>` : ""}
    </div>
  `;
}

function pipelineStatusText(data, event) {
  const status = data.status || "";
  if (status === "sent") return `отправлено · ожидаем ${humanDurationSince(event.created_at)}`;
  if (status === "queued") return "в очереди · ждет предыдущий запрос";
  if (status === "received") return "получено · ждем следующего момента";
  if (status === "skipped") return data.detail || "skip · ждем следующего момента";
  if (status === "error") return data.detail ? `ошибка · ${data.detail}` : "ошибка";
  return data.detail || status || "ждем";
}

function pipelineKind(status) {
  if (status === "received") return "ok";
  if (status === "sent" || status === "queued") return "wait";
  if (status === "skipped") return "skip";
  if (status === "error") return "bad";
  return "";
}

function latestPipelineStatus(component) {
  const all = stateEvents("pipeline.status");
  for (let i = all.length - 1; i >= 0; i--) {
    if (eventData(all[i]).component === component) return all[i];
  }
  return null;
}

function latestEvent(type) {
  const all = stateEvents(type);
  return all.length ? all[all.length - 1] : null;
}

function stateEvents(type = "") {
  const all = Array.isArray(state?.events) ? state.events : [];
  return type ? all.filter((item) => item.type === type) : all;
}

function eventData(event) {
  return event?.data && typeof event.data === "object" ? event.data : {};
}

function humanDurationSince(value) {
  if (!value) return "0s";
  const time = new Date(value).getTime();
  if (Number.isNaN(time)) return "0s";
  return humanDuration((Date.now() - time) / 1000);
}

function humanDuration(secondsValue) {
  const seconds = Math.max(0, Math.floor(Number(secondsValue || 0)));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  if (minutes < 60) return rest ? `${minutes}m ${rest}s` : `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  const restMinutes = minutes % 60;
  return restMinutes ? `${hours}h ${restMinutes}m` : `${hours}h`;
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
    <div class="stage-body stage-clock">На стадии: ${escapeHtml((latestEvent("stage.committed") || latestEvent("stage.candidate")) ? humanDurationSince((latestEvent("stage.committed") || latestEvent("stage.candidate")).created_at) : "еще нет")}</div>
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
    $("studentSelf").innerHTML = `<div class="empty">Микрофон появится здесь.</div>`;
    $("studentTranslated").innerHTML = `<div class="empty">Перевод появится после первой финальной фразы.</div>`;
    $("studentAssistLog").innerHTML = `<div class="empty">Нажми «Помоги» или задай вопрос ниже.</div>`;
    return;
  }
  const student = state?.student || {};
  const serverDirection = student.direction || "en-ru";
  if (pendingStudentDirection && pendingStudentDirection === serverDirection) {
    pendingStudentDirection = "";
  }
  const direction = pendingStudentDirection || serverDirection || $("studentDirection")?.value || "en-ru";
  if ($("studentDirection").value !== direction) $("studentDirection").value = direction;
  $("studentDirectionStatus").textContent = direction === "ru-en" ? "RU -> EN" : "EN -> RU";
  $("studentTranslationMeta").textContent = student.translation_streaming ? "перевожу..." : "gpt-oss-120b";
  $("studentAnswerMeta").textContent = student.answer_streaming ? "gemini думает..." : (student.answer_model || "gemini-3.5-flash");

  const originals = Array.isArray(student.originals) ? student.originals : [];
  $("studentOriginal").innerHTML = originals.length
    ? originals.slice(-80).map((item) => studentItemHTML(item.text, `${sourceLanguageForDirection(direction).toUpperCase()} · ${formatTime(item.created_at)}${item.final ? "" : " · partial"}`)).join("")
    : `<div class="empty">Пока пусто. Включи захват звука или вставь фразу вручную.</div>`;

  const selfItems = Array.isArray(student.self) ? student.self : [];
  $("studentSelf").innerHTML = selfItems.length
    ? selfItems.slice(-60).map((item) => studentItemHTML(item.text, `${targetLanguageForDirection(direction).toUpperCase()} · ${formatTime(item.created_at)}${item.final ? "" : " · partial"}`)).join("")
    : `<div class="empty">Микрофон пока пуст. Это не попадает в «Помоги».</div>`;

  const translations = Array.isArray(student.translations) ? student.translations : [];
  $("studentTranslated").innerHTML = translations.length
    ? translations.slice(-80).map((item) => studentItemHTML(item.text, `${targetLanguageForDirection(item.direction || direction).toUpperCase()} · ${formatTime(item.created_at)} · ${escapeHtml(item.model || "gpt-oss-120b")}`)).join("")
    : `<div class="empty">Перевод появится после первой финальной фразы.</div>`;

  const answerItems = Array.isArray(student.answer_items) ? student.answer_items : [];
  if (answerItems.length) {
    $("studentAssistLog").innerHTML = answerItems.slice(-40).map(studentAnswerBubbleHTML).join("");
  } else if (student.answer_text) {
    $("studentAssistLog").innerHTML = `<div class="assist-msg">${escapeHtml(student.answer_text)}</div>`;
  } else if (student.answer_streaming) {
    $("studentAssistLog").innerHTML = `<div class="assist-msg">Думаю...</div>`;
  } else {
    $("studentAssistLog").innerHTML = `<div class="empty">Нажми «Помоги» или задай вопрос ниже.</div>`;
  }
  bindStudentAnswerLanguageSwitches();
}

function studentItemHTML(text, meta) {
  return `<div class="student-item"><div class="meta">${escapeHtml(meta)}</div>${escapeHtml(text)}</div>`;
}

function studentAnswerBubbleHTML(item) {
  const role = item.role === "user" ? "user" : "assistant";
  const label = role === "user"
    ? (item.trigger === "button" ? "Помоги" : "Вопрос")
    : "Ответ";
  const languages = studentAnswerLanguages(item);
  const translationReady = Boolean(item.translation_text);
  const showTranslation = role === "assistant" && translationReady && studentAnswerLanguage[item.id] === "translation";
  const visibleLanguage = showTranslation ? languages.translation : languages.original;
  const metaBits = [label, formatTime(item.created_at)];
  if (item.model) metaBits.push(item.model);
  if (role === "assistant") metaBits.push(visibleLanguage);
  if (item.streaming) metaBits.push("пишет");
  if (item.translation_streaming && role === "assistant") metaBits.push(`${languages.translation} готовится`);
  const text = showTranslation
    ? item.translation_text
    : (item.text || (item.streaming ? "Думаю..." : ""));
  const switchButton = role === "assistant"
    ? studentAnswerSwitchButton(item, showTranslation, translationReady, languages)
    : "";
  const body = role === "assistant"
    ? `<div class="rich-text">${renderRichText(text)}</div>`
    : `<div class="plain-text">${escapeHtml(text)}</div>`;
  return `
    <div class="student-answer-bubble ${role}">
      <div class="student-answer-head">
        <div class="meta">${metaBits.map(escapeHtml).join(" · ")}</div>
        ${switchButton}
      </div>
      ${body}
    </div>
  `;
}

function studentAnswerLanguages(item) {
  return item.translation_direction === "en-ru"
    ? { original: "EN", translation: "RU" }
    : { original: "RU", translation: "EN" };
}

function studentAnswerSwitchButton(item, showTranslation, translationReady, languages) {
  const waiting = item.translation_streaming && !translationReady;
  const nextLanguage = showTranslation ? languages.original : languages.translation;
  const label = waiting ? `${languages.translation}...` : nextLanguage;
  const title = waiting
    ? `Перевод на ${languages.translation} готовится`
    : `Показать ${nextLanguage}`;
  return `
    <button
      type="button"
      class="answer-switch"
      data-student-answer-switch="${escapeHtml(item.id || "")}"
      ${translationReady ? "" : "disabled"}
      title="${escapeHtml(title)}"
    >${escapeHtml(label)}</button>
  `;
}

function bindStudentAnswerLanguageSwitches() {
  for (const button of document.querySelectorAll("[data-student-answer-switch]")) {
    button.onclick = () => {
      const id = button.dataset.studentAnswerSwitch;
      if (!id) return;
      studentAnswerLanguage[id] = studentAnswerLanguage[id] === "translation" ? "original" : "translation";
      renderStudent();
    };
  }
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
  telemetry.log("copy_reply_clicked", { source: "auto", generation_id: state?.seller_generation_id || "", data: { text_len: text.length } });
  await navigator.clipboard.writeText(text);
  showToast("Реплика скопирована");
}

async function copyImmediateReply() {
  const text = state?.seller_draft_immediate || "";
  if (!text) return;
  telemetry.log("copy_reply_clicked", { source: "manual", generation_id: state?.seller_immediate_generation_id || "", data: { text_len: text.length } });
  await navigator.clipboard.writeText(text);
  showToast("Немедленная реплика скопирована");
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
  telemetry.log("pip_opened");
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
        --green: #1f7a4d;
        font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      }
      * { box-sizing: border-box; }
      html, body { width: 100%; height: 100%; margin: 0; overflow: hidden; background: var(--paper); color: var(--ink); }
      body { display: grid; grid-template-rows: auto 1fr auto; }
      header { padding: 18px 20px 8px; border-bottom: 1px solid var(--line); }
      .eyebrow { color: #8a92a4; font-size: 12px; font-weight: 850; letter-spacing: .14em; text-transform: uppercase; }
      .meta { margin-top: 6px; color: var(--muted); font-size: 13px; font-weight: 650; }
      #pipReplyText { display: block; padding: 22px 24px; font-size: clamp(21px, 6.6vw, 34px); line-height: 1.2; font-weight: 760; overflow: auto; word-break: break-word; }
      #pipReplyText.muted { color: #9aa3b5; font-weight: 680; }
      .rich-text { min-height: 100%; display: grid; align-content: center; gap: 10px; }
      .rich-text p { margin: 0; }
      .rich-text ul, .rich-text ol { margin: 0; padding-left: 1.25em; }
      .rich-text li + li { margin-top: 6px; }
      .rich-text code { border: 1px solid var(--line); border-radius: 6px; padding: 1px 5px; background: #eef1f6; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: .9em; }
      footer { display: flex; align-items: center; justify-content: stretch; gap: 10px; padding: 14px 18px; border-top: 1px solid var(--line); background: var(--paper-soft); }
      button { appearance: none; border: 1px solid #cfd8e8; border-radius: 14px; min-height: 44px; padding: 9px 16px; background: var(--paper); color: var(--ink); font: inherit; font-weight: 800; cursor: pointer; }
      button.generate { width: 100%; min-height: 58px; display: inline-flex; align-items: center; justify-content: center; gap: 10px; background: var(--green); color: white; border-color: var(--green); font-size: 18px; }
      button:disabled { opacity: .45; cursor: default; }
      button.generate:disabled { opacity: 1; background: #2f9a66; border-color: #7fc8a1; color: #eefbf3; }
      .spinner { display: none; width: 17px; height: 17px; border: 3px solid rgba(255,255,255,.4); border-top-color: #fff; border-radius: 999px; animation: spin .8s linear infinite; }
      button.loading .spinner { display: inline-block; }
      @keyframes spin { to { transform: rotate(360deg); } }
    </style>
    <header>
      <div class="eyebrow">следующая реплика</div>
      <div id="pipReplyMeta" class="meta">обновляется по речи клиента</div>
    </header>
    <main id="pipReplyText" class="muted">Жду речь клиента...</main>
    <footer>
      <button id="pipGenerateReply" class="generate">
        <span class="spinner" aria-hidden="true"></span>
        <span id="pipGenerateReplyLabel">Сгенерить ответ</span>
      </button>
    </footer>
  `;
  doc.getElementById("pipGenerateReply").onclick = () => generateReply().catch((error) => showToast(error.message));
  syncReplyPip();
}

function syncReplyPip() {
  if (!replyPipWindow || replyPipWindow.closed) return;
  const doc = replyPipWindow.document;
  const immediateText = state?.seller_draft_immediate || "";
  const autoText = state?.seller_draft || "";
  const text = immediateText || autoText;
  const textNode = doc.getElementById("pipReplyText");
  const metaNode = doc.getElementById("pipReplyMeta");
  const generateButton = doc.getElementById("pipGenerateReply");
  const generateLabel = doc.getElementById("pipGenerateReplyLabel");
  if (!textNode || !metaNode || !generateButton || !generateLabel) return;
  textNode.innerHTML = text
    ? `<div class="rich-text">${renderRichText(text)}</div>`
    : `<div class="rich-text">Жду речь клиента...</div>`;
  textNode.classList.toggle("muted", !text);
  if (immediateText) {
    metaNode.textContent = state?.seller_immediate_streaming ? "немедленная генерируется" : "немедленная готова";
  } else {
    metaNode.textContent = text ? (state?.seller_streaming ? "auto генерируется" : "auto готово") : "обновляется по речи клиента";
  }
  const generating = manualReplyPending();
  generateButton.disabled = !state || generating;
  generateButton.classList.toggle("loading", generating);
  generateLabel.textContent = generating ? "Ушел думать" : "Сгенерить сейчас";
}

async function generateReply() {
  if (!state || manualReplyPending()) return;
  manualGenerateInFlight = true;
  syncGenerateReplyButton();
  syncReplyPip();
  try {
    telemetry.log("manual_generate_clicked", {
      generation_id: state?.seller_immediate_generation_id || "",
      data: {
        auto_generation_id: state?.seller_generation_id || "",
        has_auto_reply: Boolean(state?.seller_draft),
      },
    });
    await postEvent({
      type: "seller.request",
      trigger: "manual_generate",
      text: "Сгенерируй реплику продавца немедленно под текущий момент разговора. Не валидируй текущую подсказку, дай новый вариант.",
    });
  } finally {
    setTimeout(() => {
      manualGenerateInFlight = false;
      syncGenerateReplyButton();
      syncReplyPip();
    }, 1200);
  }
}

async function requestAssist(trigger, text = "") {
  await postEvent({ type: "assist.request", trigger, text });
}

async function requestStudentAnswer(trigger, text = "") {
  await postEvent({ type: "student.answer.request", trigger, text });
}

async function updateStudentDirection() {
  const direction = $("studentDirection").value || "en-ru";
  pendingStudentDirection = direction;
  applyStudentDirectionToCapture(direction);
  renderStudent();
  await postEvent({ type: "student.direction", direction });
}

function applyStudentDirectionToCapture(direction) {
  const systemState = captureStates.system;
  if (systemState?.active && systemState.role === "student_original") {
    systemState.direction = direction;
    systemState.language = sourceLanguageForDirection(direction);
  }
  const micState = captureStates.microphone;
  if (micState?.active && micState.role === "student_self") {
    micState.direction = direction;
    micState.language = targetLanguageForDirection(direction);
  }
}

async function sendStudentOriginal() {
  const text = $("studentManualText").value.trim();
  if (!text) return;
  await postEvent({ type: "student.input", text, direction: $("studentDirection").value || "en-ru" });
  $("studentManualText").value = "";
}

async function startCapture({ automatic = false, student = false } = {}) {
  if (captureStates.system.active) {
    if (captureStates.system.sessionId !== sessionId) {
      stopCapture("system", "перезапускаю захват для текущей сессии");
    } else {
      stopCapture("system");
      return false;
    }
  }
  if (!sessionId) {
    setCaptureStatus("err", "сессия еще не готова");
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
  const captureSource = student ? "student_system_audio" : "remote_audio";
  const requestStartedAt = telemetry.now();
  const clientDiagnostics = captureClientDiagnostics();
  let pickerDiagnostics = {};
  let pickerPendingTimer = null;
  try {
    setCaptureStatus("warn", automatic ? "запрашиваю доступ" : "выберите вкладку/экран со звуком");
    telemetry.log("system_capture_requested", {
      source: captureSource,
      mode: "system",
      data: clientDiagnostics,
    });
    pickerPendingTimer = setTimeout(() => {
      pickerPendingTimer = null;
      const pendingDurationMs = Math.max(0, telemetry.now() - requestStartedAt);
      telemetry.log("system_capture_picker_pending", {
        source: captureSource,
        mode: "system",
        duration_ms: pendingDurationMs,
        detail: "picker_open_or_waiting_for_user",
        data: {
          ...captureClientDiagnostics(),
          reason_code: "picker_open_or_waiting_for_user",
          request_duration_ms: Math.round(pendingDurationMs),
        },
      });
      setCaptureStatus("warn", "окно выбора открыто · выбери источник или нажми «Отмена»");
    }, 8000);
    const stream = await navigator.mediaDevices.getDisplayMedia({ video: true, audio: true });
    if (pickerPendingTimer) {
      clearTimeout(pickerPendingTimer);
      pickerPendingTimer = null;
    }
    pickerDiagnostics = captureStreamDiagnostics(stream);
    const pickerDurationMs = Math.max(0, telemetry.now() - requestStartedAt);
    telemetry.log("system_capture_picker_result", {
      source: captureSource,
      mode: "system",
      duration_ms: pickerDurationMs,
      data: pickerDiagnostics,
    });
    const audioTracks = stream.getAudioTracks();
    if (!audioTracks.length) {
      const missingTrackError = missingSystemAudioTrackError(pickerDiagnostics);
      for (const track of stream.getTracks()) track.stop();
      throw missingTrackError;
    }
    const captureState = startAudioStream(stream, {
      mode: "system",
      sourceLabel: captureSource,
      roleOverride: student ? "student_original" : "client",
      direction: student ? ($("studentDirection").value || "en-ru") : "",
      language: student ? sourceLanguageForDirection($("studentDirection").value || "en-ru") : "",
    });
    telemetry.log("system_capture_started", {
      source: captureSource,
      mode: "system",
      duration_ms: Math.max(0, telemetry.now() - requestStartedAt),
      data: {
        ...pickerDiagnostics,
        audio_context_state: captureState.context?.state || "",
      },
    });
    setCaptureStatus("on", student ? "system · собеседник стримится" : "захват включен");
    $("captureToggle").textContent = "Стоп";
    $("studentCaptureToggle").textContent = "Стоп system";
    updateBothStatus();
    return true;
  } catch (error) {
    if (pickerPendingTimer) {
      clearTimeout(pickerPendingTimer);
      pickerPendingTimer = null;
    }
    const diagnosis = classifySystemCaptureError(error);
    const captureErrorText = diagnosis.message;
    const durationMs = Math.max(0, telemetry.now() - requestStartedAt);
    telemetry.log("system_capture_failed", {
      source: captureSource,
      mode: "system",
      duration_ms: durationMs,
      detail: `${diagnosis.code}: ${captureErrorText}`,
      data: {
        ...clientDiagnostics,
        ...pickerDiagnostics,
        ...(error?.captureDiagnostics || {}),
        reason_code: diagnosis.code,
        error_name: error?.name || "",
        error_message: error?.message || "",
        constraint: error?.constraint || "",
        request_duration_ms: Math.round(durationMs),
      },
    });
    const needsClick = automatic && diagnosis.code === "invalid_user_gesture_or_focus";
    setCaptureStatus("err", needsClick ? "нужен клик для захвата" : captureErrorText);
    if (!automatic) showToast(captureErrorText, 7000);
    $("captureToggle").textContent = "Включить";
    $("studentCaptureToggle").textContent = "Включить system";
    updateBothStatus();
    return false;
  }
}

function captureClientDiagnostics() {
  const ua = String(navigator.userAgent || "");
  const brands = Array.from(navigator.userAgentData?.brands || [])
    .map((item) => String(item?.brand || "").trim())
    .filter(Boolean);
  const supportedConstraints = navigator.mediaDevices?.getSupportedConstraints?.() || {};
  const relevantConstraints = {};
  for (const key of [
    "channelCount",
    "deviceId",
    "echoCancellation",
    "noiseSuppression",
    "sampleRate",
    "sampleSize",
    "suppressLocalAudioPlayback",
  ]) {
    if (supportedConstraints[key]) relevantConstraints[key] = true;
  }
  return {
    browser: brands.join(", ") || captureBrowserFamily(ua),
    platform: navigator.userAgentData?.platform || navigator.platform || "",
    mobile: Boolean(navigator.userAgentData?.mobile),
    secure_context: Boolean(window.isSecureContext),
    document_focused: typeof document.hasFocus === "function" ? document.hasFocus() : null,
    visibility_state: document.visibilityState || "",
    display_capture_supported: Boolean(navigator.mediaDevices?.getDisplayMedia),
    supported_audio_constraints: relevantConstraints,
  };
}

function captureBrowserFamily(userAgent) {
  if (/Edg\//i.test(userAgent)) return "Edge";
  if (/OPR\//i.test(userAgent)) return "Opera";
  if (/Firefox\//i.test(userAgent)) return "Firefox";
  if (/CriOS\//i.test(userAgent)) return "Chrome iOS";
  if (/Chrome\//i.test(userAgent)) return "Chrome/Chromium";
  if (/Safari\//i.test(userAgent)) return "Safari";
  return "unknown";
}

function captureTrackDiagnostics(track) {
  if (!track) return {};
  let settings = {};
  try {
    settings = track.getSettings?.() || {};
  } catch (_) {
    settings = {};
  }
  const safeSettings = {};
  for (const key of [
    "displaySurface",
    "logicalSurface",
    "cursor",
    "channelCount",
    "sampleRate",
    "sampleSize",
    "echoCancellation",
    "noiseSuppression",
    "autoGainControl",
    "suppressLocalAudioPlayback",
  ]) {
    if (settings[key] !== undefined) safeSettings[key] = settings[key];
  }
  return {
    kind: track.kind || "",
    ready_state: track.readyState || "",
    muted: Boolean(track.muted),
    enabled: Boolean(track.enabled),
    content_hint: track.contentHint || "",
    settings: safeSettings,
  };
}

function captureStreamDiagnostics(stream) {
  const audioTracks = Array.from(stream?.getAudioTracks?.() || []);
  const videoTracks = Array.from(stream?.getVideoTracks?.() || []);
  const audioDiagnostics = audioTracks.map(captureTrackDiagnostics);
  const videoDiagnostics = videoTracks.map(captureTrackDiagnostics);
  return {
    audio_track_count: audioTracks.length,
    video_track_count: videoTracks.length,
    display_surface: videoDiagnostics[0]?.settings?.displaySurface || "",
    stream_active: Boolean(stream?.active),
    audio_tracks: audioDiagnostics,
    video_tracks: videoDiagnostics,
  };
}

function createSystemCaptureError(code, message, captureDiagnostics = {}) {
  const error = new Error(message);
  error.name = "SystemCaptureError";
  error.captureCode = code;
  error.captureDiagnostics = captureDiagnostics;
  return error;
}

function missingSystemAudioTrackError(diagnostics) {
  const surface = String(diagnostics?.display_surface || "");
  if (surface === "browser") {
    return createSystemCaptureError(
      "tab_audio_not_shared",
      "Вкладка выбрана без аудио. В окне шаринга включи «Поделиться аудио вкладки» и выбери вкладку, где реально играет звук.",
      diagnostics,
    );
  }
  if (surface === "window") {
    return createSystemCaptureError(
      "window_audio_unavailable",
      "Окно выбрано без аудиодорожки. На macOS выбери вкладку Chrome/Arc со включённым «Поделиться аудио вкладки».",
      diagnostics,
    );
  }
  if (surface === "monitor") {
    return createSystemCaptureError(
      "screen_audio_unavailable",
      "Весь экран выбран без системной аудиодорожки. Попробуй вкладку Chrome/Arc со включённым «Поделиться аудио вкладки».",
      diagnostics,
    );
  }
  return createSystemCaptureError(
    "audio_track_missing",
    "Браузер отдал экран, но не отдал аудиотрек. Выбери вкладку со звуком и включи «Поделиться аудио вкладки».",
    diagnostics,
  );
}

function classifySystemCaptureError(error) {
  if (error?.captureCode) {
    return {
      code: String(error.captureCode),
      message: String(error.message || "Не удалось включить системный звук."),
    };
  }
  const name = String(error?.name || "");
  const message = String(error?.message || "");
  const lower = `${name} ${message}`.toLowerCase();
  if (lower.includes("permission denied by system")) {
    return {
      code: "os_permission_denied",
      message: "macOS запретила захват для браузера. Разреши Screen & System Audio Recording / Screen Recording и полностью перезапусти браузер.",
    };
  }
  if (lower.includes("notallowederror") || lower.includes("permission denied")) {
    return {
      code: "picker_cancelled_or_permission_denied",
      message: "Окно выбора закрыто или браузеру не выдан доступ. Выбери источник заново и проверь разрешение записи экрана в macOS.",
    };
  }
  if (lower.includes("aborterror")) {
    return {
      code: "picker_aborted",
      message: "Браузер прервал выбор источника. Закрой другие окна шаринга и попробуй ещё раз.",
    };
  }
  if (lower.includes("invalidstateerror")) {
    return {
      code: "invalid_user_gesture_or_focus",
      message: "Захват нужно запустить кликом в активной вкладке. Вернись на REC и нажми «Включить» ещё раз.",
    };
  }
  if (lower.includes("notreadableerror")) {
    return {
      code: "source_not_readable",
      message: "Источник выбран, но браузер не может его прочитать. Останови другие записи экрана и перезапусти браузер.",
    };
  }
  if (lower.includes("audio track")) {
    return {
      code: "audio_track_missing",
      message: "Источник выбран без звука. Включи «Поделиться аудио вкладки» или выбери вкладку со звуком.",
    };
  }
  if (lower.includes("notfounderror")) {
    return {
      code: "source_not_found",
      message: "Браузер не нашёл доступный источник. Попробуй Chrome/Arc и вкладку со звуком вместо всего экрана.",
    };
  }
  if (lower.includes("overconstrainederror")) {
    return {
      code: "constraints_unsupported",
      message: "Браузер не поддерживает запрошенные параметры захвата. Обнови Chrome/Arc и попробуй снова.",
    };
  }
  if (lower.includes("typeerror")) {
    return {
      code: "capture_api_invalid_or_unsupported",
      message: "Этот браузер или контекст страницы не поддерживает такой захват. Открой REC по HTTPS в актуальном Chrome/Arc.",
    };
  }
  return {
    code: "capture_failed",
    message: message || "Не удалось включить системный звук.",
  };
}

function systemCaptureErrorMessage(error) {
  return classifySystemCaptureError(error).message;
}

async function startStudentCapture() {
  return startCapture({ automatic: false, student: true });
}

async function startMicTest({ student = false } = {}) {
  if (captureStates.microphone.active) {
    if (captureStates.microphone.sessionId !== sessionId) {
      stopCapture("microphone", "перезапускаю микрофон для текущей сессии");
    } else {
      stopCapture("microphone");
      return false;
    }
  }
  if (!sessionId) {
    setMicStatus("err", "сессия еще не готова");
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
    const captureSource = student ? "student_mic" : "seller_mic";
    telemetry.log("mic_capture_requested", { source: captureSource, mode: "microphone" });
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        echoCancellation: { ideal: true },
        noiseSuppression: { ideal: true },
        autoGainControl: { ideal: true },
        channelCount: { ideal: 1 },
      },
    });
    const captureState = startAudioStream(stream, {
      mode: "microphone",
      sourceLabel: captureSource,
      roleOverride: student ? "student_self" : "seller",
      direction: student ? ($("studentDirection").value || "en-ru") : "",
      language: student ? targetLanguageForDirection($("studentDirection").value || "en-ru") : "",
    });
    telemetry.log("mic_capture_started", { source: captureSource, mode: "microphone" });
    const settings = stream.getAudioTracks()[0]?.getSettings?.() || {};
    captureState.micSettings = settings;
    logAudioEvent(captureState, "mic_settings", JSON.stringify({
      echoCancellation: settings.echoCancellation ?? null,
      noiseSuppression: settings.noiseSuppression ?? null,
      autoGainControl: settings.autoGainControl ?? null,
      channelCount: settings.channelCount ?? null,
      sampleRate: settings.sampleRate ?? null,
    }));
    renderEchoStatus();
    setMicStatus("on", student ? "микрофон · мы стримимся" : "микрофон включен · скажи фразу");
    $("micToggle").textContent = "Стоп микрофон";
    $("studentMicToggle").textContent = "Стоп mic";
    updateBothStatus();
    return true;
  } catch (error) {
    telemetry.log("mic_capture_failed", { source: student ? "student_mic" : "seller_mic", mode: "microphone", detail: error.message });
    setMicStatus("err", error.message);
    $("micToggle").textContent = "Проверить микрофон";
    $("studentMicToggle").textContent = "Включить mic";
    updateBothStatus();
    return false;
  }
}

async function startBothAudio() {
  const systemCurrent = captureStates.system.active && captureStates.system.sessionId === sessionId;
  const micCurrent = captureStates.microphone.active && captureStates.microphone.sessionId === sessionId;
  if (systemCurrent && micCurrent) {
    stopCapture("system");
    stopCapture("microphone");
    updateBothStatus("оба источника остановлены");
    return;
  }
  $("bothToggle").disabled = true;
  updateBothStatus("включаю звонок");
  try {
    if (!micCurrent) await startMicTest();
    if (!systemCurrent) await startCapture({ automatic: false });
  } finally {
    $("bothToggle").disabled = false;
    updateBothStatus();
  }
}

async function startStudentBothAudio() {
  const systemCurrent = captureStates.system.active && captureStates.system.sessionId === sessionId;
  const micCurrent = captureStates.microphone.active && captureStates.microphone.sessionId === sessionId;
  if (systemCurrent && micCurrent) {
    stopCapture("system");
    stopCapture("microphone");
    updateBothStatus("оба источника остановлены");
    return;
  }
  $("studentBothToggle").disabled = true;
  updateBothStatus("включаю student audio");
  try {
    if (!systemCurrent) await startCapture({ automatic: false, student: true });
    if (!micCurrent) await startMicTest({ student: true });
  } finally {
    $("studentBothToggle").disabled = false;
    updateBothStatus();
  }
}

function startAudioStream(stream, { mode, sourceLabel, roleOverride = "", direction = "", language = "" }) {
  const streamSessionId = sessionId;
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
    sessionId: streamSessionId,
    direction,
    language,
    speechOpen: false,
    lastVoiceAt: 0,
    lastEndTurnAt: 0,
    sentChunks: 0,
    reconnectAttempts: 0,
    reconnectTimer: null,
    reconnectDisabled: false,
    fatalSTTError: "",
    signalProbeTimer: null,
    observedFrames: 0,
    voicedFrames: 0,
    maxObservedRms: 0,
    startedAt: telemetry.now(),
    wsConnectStartedAt: 0,
    stopping: false,
  };
  captureStates[mode] = nextState;
  logAudioEvent(nextState, "start", "", {
    ...captureStreamDiagnostics(stream),
    audio_context_state: context.state,
    audio_context_sample_rate: context.sampleRate,
  });
  sink.gain.value = 0;
  processor.onaudioprocess = (event) => {
    if (captureStates[mode] !== nextState) return;
    const input = event.inputBuffer.getChannelData(0);
    observeCaptureSignal(nextState, input);
    if (nextState.ws?.readyState !== WebSocket.OPEN) return;
    streamAudioFrame(nextState, input);
  };
  context.onstatechange = () => {
    if (captureStates[mode] !== nextState) return;
    logAudioEvent(nextState, "audio_context_state", context.state, {
      audio_context_state: context.state,
    });
    if (context.state === "suspended") {
      setAudioStatus(mode, "warn", mode === "microphone" ? "микрофон · AudioContext приостановлен" : "аудиотрек есть · AudioContext приостановлен");
    }
  };
  source.connect(processor);
  processor.connect(sink);
  sink.connect(context.destination);
  if (context.state === "suspended") {
    logAudioEvent(nextState, "audio_context_resume_requested", context.state);
    context.resume()
      .then(() => {
        if (captureStates[mode] === nextState) {
          logAudioEvent(nextState, "audio_context_resumed", context.state);
        }
      })
      .catch((error) => {
        if (captureStates[mode] !== nextState) return;
        logAudioEvent(nextState, "audio_context_resume_failed", error?.message || "", {
          error_name: error?.name || "",
          error_message: error?.message || "",
          audio_context_state: context.state,
        });
        setAudioStatus(mode, "err", mode === "microphone" ? "микрофон: AudioContext не запустился" : "аудиотрек есть, но AudioContext не запустился");
      });
  }
  connectSTTWebSocket(nextState);
  nextState.signalProbeTimer = setTimeout(() => probeCapturePipeline(nextState), 3000);
  for (const track of stream.getAudioTracks()) {
    track.onended = () => {
      if (captureStates[mode] === nextState) {
        logAudioEvent(nextState, "track_ended", track.readyState, {
          track: captureTrackDiagnostics(track),
        });
        stopCapture(mode, mode === "microphone" ? "микрофон завершен браузером" : "захват завершен браузером");
      }
    };
    track.onmute = () => {
      if (captureStates[mode] === nextState) {
        logAudioEvent(nextState, "track_mute", track.readyState, {
          track: captureTrackDiagnostics(track),
        });
        setAudioStatus(mode, "warn", mode === "microphone" ? "микрофон без сигнала" : "захват без сигнала");
      }
    };
    track.onunmute = () => {
      if (captureStates[mode] === nextState) {
        logAudioEvent(nextState, "track_unmute", track.readyState, {
          track: captureTrackDiagnostics(track),
        });
        setAudioStatus(mode, "on", mode === "microphone" ? "микрофон включен" : "захват включен");
      }
    };
  }
  return nextState;
}

function observeCaptureSignal(captureState, samples) {
  const rms = float32Rms(samples);
  captureState.observedFrames += 1;
  captureState.maxObservedRms = Math.max(captureState.maxObservedRms, rms);
  if (rms >= audioEchoState.thresholds.vadRms) captureState.voicedFrames += 1;
}

function webSocketStateLabel(ws) {
  if (!ws) return "missing";
  if (ws.readyState === WebSocket.CONNECTING) return "connecting";
  if (ws.readyState === WebSocket.OPEN) return "open";
  if (ws.readyState === WebSocket.CLOSING) return "closing";
  if (ws.readyState === WebSocket.CLOSED) return "closed";
  return String(ws.readyState);
}

function probeCapturePipeline(captureState) {
  if (captureStates[captureState.mode] !== captureState || captureState.stopping) return;
  captureState.signalProbeTimer = null;
  const contextState = captureState.context?.state || "missing";
  const wsState = webSocketStateLabel(captureState.ws);
  let reasonCode = "pcm_flowing";
  if (captureState.reconnectDisabled) {
    reasonCode = "stt_non_retryable_error";
  } else if (contextState !== "running") {
    reasonCode = "audio_context_not_running";
  } else if (captureState.observedFrames === 0) {
    reasonCode = "pcm_frames_missing";
  } else if (wsState !== "open") {
    reasonCode = "stt_websocket_not_open";
  } else if (captureState.maxObservedRms < 0.0001) {
    reasonCode = "audio_track_silent";
  }
  const diagnosticData = {
    reason_code: reasonCode,
    elapsed_ms: Math.round(Math.max(0, telemetry.now() - captureState.startedAt)),
    audio_context_state: contextState,
    observed_frames: captureState.observedFrames,
    voiced_frames: captureState.voicedFrames,
    max_rms: Number(captureState.maxObservedRms.toFixed(6)),
    ws_state: wsState,
    sent_chunks: captureState.sentChunks,
    ...captureStreamDiagnostics(captureState.stream),
  };
  logAudioEvent(captureState, "capture_pipeline_probe", reasonCode, diagnosticData);
  if (reasonCode === "stt_non_retryable_error") {
    setAudioStatus(captureState.mode, "err", captureState.fatalSTTError || "STT недоступен");
  } else if (reasonCode === "audio_context_not_running") {
    setAudioStatus(captureState.mode, "err", captureState.mode === "microphone" ? "микрофон: AudioContext не работает" : "аудиотрек есть · AudioContext не работает");
  } else if (reasonCode === "pcm_frames_missing") {
    setAudioStatus(captureState.mode, "err", captureState.mode === "microphone" ? "микрофон есть, но PCM не приходит" : "аудиотрек есть, но PCM не приходит");
  } else if (reasonCode === "stt_websocket_not_open") {
    setAudioStatus(captureState.mode, "warn", captureState.mode === "microphone" ? "PCM идёт · STT не подключён" : "системный PCM идёт · STT не подключён");
  }
}

function connectSTTWebSocket(captureState) {
  if (!captureState?.active) return;
  const { mode, role, sourceLabel, direction, language, sessionId: streamSessionId } = captureState;
  captureState.wsConnectStartedAt = telemetry.now();
  const ws = new WebSocket(sttStreamURL({ sessionId: streamSessionId, role, sourceLabel, direction, language }));
  captureState.ws = ws;
  ws.onopen = () => {
    if (captureStates[mode] === captureState) {
      captureState.reconnectAttempts = 0;
      logAudioEvent(captureState, "ws_open", "", {
        connect_duration_ms: Math.round(Math.max(0, telemetry.now() - captureState.wsConnectStartedAt)),
        ws_state: webSocketStateLabel(ws),
      });
      setAudioStatus(mode, "on", mode === "microphone" ? "микрофон стримится" : "захват стримится");
    }
  };
  ws.onmessage = (event) => {
    if (captureStates[mode] !== captureState) return;
    const data = JSON.parse(event.data || "{}");
    if (data.type === "error") {
      const serverError = String(data.error || "STT stream error");
      const retryable = data.retryable !== false && !isNonRetryableSTTError(serverError);
      const displayError = sttDisplayError(serverError);
      if (!retryable) {
        captureState.reconnectDisabled = true;
        captureState.fatalSTTError = displayError;
      }
      logAudioEvent(captureState, "stt_stream_error", serverError, {
        server_message: serverError,
        retryable,
      });
      setAudioStatus(mode, "err", displayError);
    } else if (data.type === "stt.rejected") {
      recordSTTRejection(captureState, data);
      const reason = data.reason ? ` · ${data.reason}` : "";
      setAudioStatus(mode, "on", mode === "microphone" ? `микрофон · подавлено${reason}` : `захват · подавлено${reason}`);
    } else if (data.type === "stt.final") {
      recordSTTAttribution(captureState, data);
      const roleText = roleLabel(data.role);
      const speakerText = data.speaker ? ` · spk ${data.speaker}` : "";
      setAudioStatus(mode, "on", mode === "microphone" ? "микрофон · распознано" : `захват · ${roleText}${speakerText}`);
    }
  };
  ws.onerror = () => {
    if (captureStates[mode] === captureState) {
      logAudioEvent(captureState, "ws_error", "", {
        ws_state: webSocketStateLabel(ws),
      });
      if (captureState.reconnectDisabled) {
        setAudioStatus(mode, "err", captureState.fatalSTTError || "STT недоступен");
      } else {
        setAudioStatus(mode, "warn", "STT stream error · переподключаю");
      }
    }
  };
  ws.onclose = (event) => {
    if (captureStates[mode] !== captureState || captureState.stopping) return;
    logAudioEvent(captureState, "ws_close", `${event.code}${event.reason ? `: ${event.reason}` : ""}`, {
      close_code: event.code,
      close_reason: event.reason || "",
      clean_close: Boolean(event.wasClean),
      ws_lifetime_ms: Math.round(Math.max(0, telemetry.now() - captureState.wsConnectStartedAt)),
    });
    if (captureState.reconnectDisabled) {
      setAudioStatus(mode, "err", captureState.fatalSTTError || "STT недоступен");
      return;
    }
    scheduleSTTReconnect(captureState);
  };
}

function isNonRetryableSTTError(message) {
  const lower = String(message || "").toLowerCase();
  return [
    "balance exhausted",
    "insufficient balance",
    "insufficient credits",
    "payment required",
    "invalid api key",
    "invalid api_key",
  ].some((marker) => lower.includes(marker));
}

function sttDisplayError(message) {
  const lower = String(message || "").toLowerCase();
  if (lower.includes("balance exhausted") || lower.includes("insufficient balance") || lower.includes("insufficient credits")) {
    return "Soniox: закончился баланс";
  }
  if (lower.includes("payment required")) {
    return "STT: требуется оплата";
  }
  if (lower.includes("invalid api key") || lower.includes("invalid api_key")) {
    return "STT: неверный API-ключ";
  }
  return String(message || "STT stream error");
}

function reconnectCaptureSTT(mode, reason) {
  const captureState = captureStates[mode];
  if (!captureState?.active || captureState.reconnectDisabled) return;
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
  if (captureState.reconnectDisabled) {
    setAudioStatus(mode, "err", captureState.fatalSTTError || "STT недоступен");
    return;
  }
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
  if (captureState.signalProbeTimer) {
    clearTimeout(captureState.signalProbeTimer);
    captureState.signalProbeTimer = null;
  }
  captureStates[mode] = emptyCaptureState(mode);
  logAudioEvent(captureState, "stop", stoppedText);
  if (captureState.context) captureState.context.onstatechange = null;
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
    $("studentMicToggle").textContent = "Включить mic";
  } else {
    setCaptureStatus("warn", stoppedText || "захват остановлен");
    $("captureToggle").textContent = "Включить";
    $("studentCaptureToggle").textContent = "Включить system";
  }
  updateBothStatus();
  if (!captureStates.system.active && !captureStates.microphone.active) {
    closeAec3Connection("idle");
  }
}

function streamAudioFrame(captureState, samples) {
  const now = Date.now();
  const rms = float32Rms(samples);
  const pcm = downsampleToPCM16(samples, captureState.context.sampleRate, 16000);
  if (captureState.mode === "system" && captureState.sourceLabel === "remote_audio" && rms >= audioEchoState.thresholds.minSystemRms) {
    rememberSystemReferenceFrame(pcm, now, rms);
    sendAec3Far(pcm);
  }
  if (captureState.mode === "microphone" && sendAec3Near(pcm)) return;

  processPCMForSTT(captureState, pcm, { now, rms });
}

function processPCMForSTT(captureState, pcm, { now = Date.now(), rms = pcm16Rms(pcm), skipEchoFilter = false, statusPrefix = "" } = {}) {
  if (captureStates[captureState.mode] !== captureState || captureState.ws?.readyState !== WebSocket.OPEN) return;
  let isVoice = rms >= audioEchoState.thresholds.vadRms;
  let echo = null;
  if (captureState.mode === "microphone" && isVoice && echoSuppressionEnabled && !skipEchoFilter) {
    echo = classifyMicEchoFrame(pcm, now, rms);
    audioEchoState.lastBestCorr = echo.bestCorr;
    audioEchoState.lastResidual = echo.residualRatio;
    audioEchoState.lastLagMs = echo.lagMs;
    if (echo.echoOnly) {
      audioEchoState.suppressedFrames += 1;
      maybeLogEchoStats(captureState);
      setAudioStatus(captureState.mode, "on", "микрофон · эхо клиента подавлено");
      if (captureState.speechOpen && now - captureState.lastVoiceAt >= 650) {
        sendStreamEndTurn(captureState);
        captureState.speechOpen = false;
        setAudioStatus(captureState.mode, "on", "микрофон · эхо подавлено · жду финал");
      }
      renderEchoStatus();
      return;
    } else if (echo.doubleTalk) {
      audioEchoState.doubleTalkFrames += 1;
    }
    renderEchoStatus();
  }
  if (isVoice) {
    captureState.speechOpen = true;
    captureState.lastVoiceAt = now;
  }

  if (captureState.speechOpen && !isVoice && now - captureState.lastVoiceAt >= 650) {
    sendStreamEndTurn(captureState);
    captureState.speechOpen = false;
    setAudioStatus(captureState.mode, "on", statusPrefix || (captureState.mode === "microphone" ? "микрофон · жду финал" : "захват · жду финал"));
    return;
  }

  if (!isVoice && !captureState.speechOpen) {
    setAudioStatus(captureState.mode, "on", statusPrefix || (captureState.mode === "microphone" ? "микрофон · жду речь" : "захват · жду речь"));
    return;
  }

  const pcmBase64 = pcm16ToBase64(pcm);
  captureState.ws.send(JSON.stringify({ audio_chunk: { content: pcmBase64 } }));
  captureState.sentChunks += 1;
  if (captureState.mode === "microphone") {
    audioEchoState.sentFrames += 1;
    maybeLogEchoStats(captureState);
  }
  if (captureState.sentChunks % 12 === 0) {
    setAudioStatus(captureState.mode, "on", statusPrefix || (captureState.mode === "microphone" ? "микрофон · слышу речь" : "захват · слышу речь"));
  }
}

function rememberSystemReferenceFrame(pcm16, nowMs, rms) {
  audioEchoState.systemRing.push({
    pcm: pcm16,
    at: nowMs,
    rms,
  });
  const cutoff = nowMs - audioEchoState.maxMs;
  while (audioEchoState.systemRing.length && audioEchoState.systemRing[0].at < cutoff) {
    audioEchoState.systemRing.shift();
  }
}

function classifyMicEchoFrame(micPcm16, nowMs, micRms) {
  const thresholds = audioEchoState.thresholds;
  let bestCorr = 0;
  let bestResidual = 1;
  let bestLagMs = 0;
  for (let i = audioEchoState.systemRing.length - 1; i >= 0; i--) {
    const frame = audioEchoState.systemRing[i];
    const lagMs = nowMs - frame.at;
    if (lagMs < thresholds.minLagMs) continue;
    if (lagMs > thresholds.maxLagMs) break;
    if (frame.rms < thresholds.minSystemRms) continue;
    const corr = normalizedCrossCorrelation(micPcm16, frame.pcm);
    if (corr > bestCorr) {
      bestCorr = corr;
      bestResidual = estimateResidualRatio(micPcm16, frame.pcm);
      bestLagMs = lagMs;
    }
  }
  const echoOnly = (bestCorr >= thresholds.echoCorrReject && bestResidual < thresholds.residualSellerMin) ||
    (bestCorr >= thresholds.echoCorrMaybe && bestResidual < thresholds.residualSellerMin * 0.7);
  const doubleTalk = bestCorr >= thresholds.echoCorrReject && bestResidual >= thresholds.residualSellerMin;
  return { bestCorr, residualRatio: bestResidual, lagMs: bestLagMs, echoOnly, doubleTalk, micRms };
}

function normalizedCrossCorrelation(a, b) {
  const n = Math.min(a.length, b.length);
  if (n < 32) return 0;
  const stride = Math.max(1, Math.floor(n / 256));
  let dot = 0;
  let aa = 0;
  let bb = 0;
  for (let i = 0; i < n; i += stride) {
    const av = a[i];
    const bv = b[i];
    dot += av * bv;
    aa += av * av;
    bb += bv * bv;
  }
  if (aa <= 0 || bb <= 0) return 0;
  return Math.abs(dot / Math.sqrt(aa * bb));
}

function estimateResidualRatio(mic, ref) {
  const n = Math.min(mic.length, ref.length);
  if (n < 32) return 1;
  const stride = Math.max(1, Math.floor(n / 256));
  let dot = 0;
  let rr = 0;
  let mm = 0;
  for (let i = 0; i < n; i += stride) {
    const mv = mic[i];
    const rv = ref[i];
    dot += mv * rv;
    rr += rv * rv;
    mm += mv * mv;
  }
  if (rr <= 0 || mm <= 0) return 1;
  const alpha = dot / rr;
  let residual = 0;
  for (let i = 0; i < n; i += stride) {
    const value = mic[i] - alpha * ref[i];
    residual += value * value;
  }
  return Math.sqrt(residual / mm);
}

function maybeLogEchoStats(captureState) {
  const now = Date.now();
  if (now - audioEchoState.lastLogAt < 5000) return;
  audioEchoState.lastLogAt = now;
  logAudioEvent(captureState, "echo_stats", JSON.stringify({
    suppression: echoSuppressionEnabled,
    suppressedFrames: audioEchoState.suppressedFrames,
    sentFrames: audioEchoState.sentFrames,
    doubleTalkFrames: audioEchoState.doubleTalkFrames,
    bestCorr: Number(audioEchoState.lastBestCorr.toFixed(3)),
    residual: Number(audioEchoState.lastResidual.toFixed(3)),
    lagMs: Math.round(audioEchoState.lastLagMs),
    systemRing: audioEchoState.systemRing.length,
    serverRejected: audioEchoState.serverRejected,
    serverEchoRejected: audioEchoState.serverEchoRejected,
    serverSourceSuppressed: audioEchoState.serverSourceSuppressed,
    attribution: audioEchoState.attributionCounts,
    lastAttribution: audioEchoState.lastAttribution,
  }));
}

function sendStreamEndTurn(captureState) {
  const now = Date.now();
  if (captureState.ws.readyState !== WebSocket.OPEN || now - captureState.lastEndTurnAt < 700) return;
  captureState.ws.send(JSON.stringify({ end_turn: {} }));
  captureState.lastEndTurnAt = now;
}

function sttStreamURL({ sessionId: targetSessionId, role, sourceLabel, direction = "", language = "" }) {
  const scheme = location.protocol === "https:" ? "wss:" : "ws:";
  const params = new URLSearchParams({ role, source: sourceLabel });
  if (role === "mixed" && sellerSpeaker) {
    params.set("seller_speaker", sellerSpeaker);
  }
  if (direction) params.set("direction", direction);
  if (language) params.set("language", language);
  return `${scheme}//${location.host}/v1/sessions/${encodeURIComponent(targetSessionId)}/stt/live?${params}`;
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
    role: mode === "microphone" ? "seller" : "client",
    sourceLabel: mode === "microphone" ? "seller_mic" : "remote_audio",
    sessionId: "",
    direction: "",
    language: "",
    speechOpen: false,
    lastVoiceAt: 0,
    lastEndTurnAt: 0,
    sentChunks: 0,
    reconnectAttempts: 0,
    reconnectTimer: null,
    reconnectDisabled: false,
    fatalSTTError: "",
    signalProbeTimer: null,
    observedFrames: 0,
    voicedFrames: 0,
    maxObservedRms: 0,
    startedAt: 0,
    wsConnectStartedAt: 0,
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

function pcm16ToBase64(samples) {
  return bytesToBase64(new Uint8Array(samples.buffer, samples.byteOffset, samples.byteLength));
}

function base64ToInt16(base64) {
  const binary = atob(base64 || "");
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  const samples = new Int16Array(Math.floor(bytes.length / 2));
  const view = new DataView(bytes.buffer);
  for (let i = 0; i < samples.length; i++) {
    samples[i] = view.getInt16(i * 2, true);
  }
  return samples;
}

function pcm16Rms(samples) {
  let sum = 0;
  for (const sample of samples) {
    const value = sample / 32768;
    sum += value * value;
  }
  return Math.sqrt(sum / Math.max(samples.length, 1));
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
  $("studentMicStatus").textContent = text;
  $("studentMicPill").textContent = kind === "on" ? "вкл" : kind === "err" ? "ошибка" : "ожидание";
  $("studentMicPill").className = `status-pill ${kind}`;
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
    $("studentBothStatus").textContent = text;
  } else if (systemOn && micOn) {
    $("bothStatus").textContent = "звонок пишется: system=клиент, mic=мы";
    $("studentBothStatus").textContent = "student пишется: system=собеседник, mic=мы";
  } else if (systemOn) {
    $("bothStatus").textContent = "включен системный звук · клиент";
    $("studentBothStatus").textContent = "включен system · собеседник";
  } else if (micOn) {
    $("bothStatus").textContent = "включен только микрофон";
    $("studentBothStatus").textContent = "включен только mic · мы";
  } else {
    $("bothStatus").textContent = "system audio = клиент, микрофон = мы";
    $("studentBothStatus").textContent = "system audio = собеседник, микрофон = мы";
  }
  $("bothToggle").textContent = systemOn && micOn ? "Стоп всё" : "Включить всё";
  $("studentBothToggle").textContent = systemOn && micOn ? "Стоп всё" : "Включить всё";
}

function initAudioControls() {
  const echoToggle = $("echoSuppressionToggle");
  if (echoToggle) {
    echoToggle.checked = echoSuppressionEnabled;
    echoToggle.onchange = () => {
      echoSuppressionEnabled = echoToggle.checked;
      localStorage.setItem(ECHO_SUPPRESSION_STORAGE_KEY, echoSuppressionEnabled ? "1" : "0");
      renderEchoStatus();
    };
  }
  const aec3Toggle = $("aec3Toggle");
  if (aec3Toggle) {
    aec3Toggle.onclick = () => {
      aec3Enabled = !aec3Enabled;
      localStorage.setItem(AEC3_ENABLED_STORAGE_KEY, aec3Enabled ? "1" : "0");
      if (!aec3Enabled) closeAec3Connection("disabled");
      renderEchoStatus();
      ensureAec3Connection();
    };
  }
  const aec3Url = $("aec3Url");
  if (aec3Url) {
    aec3Url.value = audioAec3State.url;
    aec3Url.onchange = () => {
      audioAec3State.url = aec3Url.value.trim() || "ws://127.0.0.1:8122";
      localStorage.setItem(AEC3_URL_STORAGE_KEY, audioAec3State.url);
      closeAec3Connection("url_changed");
      renderEchoStatus();
      ensureAec3Connection();
    };
  }
  const advancedToggle = $("audioAdvancedToggle");
  if (advancedToggle) {
    advancedToggle.onclick = () => {
      audioAdvancedVisible = !audioAdvancedVisible;
      renderAudioAdvanced();
    };
  }
  renderAudioAdvanced();
  renderEchoStatus();
}

function renderAudioAdvanced() {
  const panel = $("audioAdvancedPanel");
  const button = $("audioAdvancedToggle");
  if (panel) panel.hidden = !audioAdvancedVisible;
  if (button) button.textContent = audioAdvancedVisible ? "Скрыть" : "Диагностика";
}

function recordSTTAttribution(captureState, data) {
  const source = normalizeClientCaptureSource(data.source || captureState.sourceLabel);
  const role = data.role || "";
  const counts = audioEchoState.attributionCounts;
  if (source === "seller_mic" && role === "seller") {
    counts.micSeller += 1;
  } else if (source === "seller_mic" && role === "client") {
    counts.micClient += 1;
  } else if (source === "remote_audio" && role === "client") {
    counts.systemClient += 1;
  } else if (source === "remote_audio" && role === "seller") {
    counts.systemSeller += 1;
  } else if (source === "remote_audio" && String(role).startsWith("speaker_")) {
    counts.systemSpeaker += 1;
  } else if (source === "mixed_audio" && role === "seller") {
    counts.mixedSeller += 1;
  } else if (source === "mixed_audio" && role === "client") {
    counts.mixedClient += 1;
  } else {
    counts.other += 1;
  }
  const speakerText = data.speaker ? ` · spk ${data.speaker}` : "";
  audioEchoState.lastAttribution = `${sourceLabel(source)} -> ${roleLabel(role)}${speakerText}`;
  renderEchoStatus();
}

function recordSTTRejection(captureState, data) {
  const reason = data.reason || "unknown";
  audioEchoState.serverRejected += 1;
  if (reason === "system_seller_suppressed_by_active_mic") {
    audioEchoState.serverSourceSuppressed += 1;
  } else if (reason.includes("echo_into")) {
    audioEchoState.serverEchoRejected += 1;
  } else {
    audioEchoState.serverTextRejected += 1;
  }
  const source = normalizeClientCaptureSource(data.source || captureState.sourceLabel);
  const score = typeof data.echo_score === "number" && Number.isFinite(data.echo_score)
    ? ` · score=${data.echo_score.toFixed(2)}`
    : "";
  const text = compactDebugText(data.text || "", 36);
  const preview = text ? ` · "${text}"` : "";
  audioEchoState.recentRejects.unshift(`${sourceLabel(source)} -> ${roleLabel(data.role)} · ${reason}${score}${preview}`);
  audioEchoState.recentRejects = audioEchoState.recentRejects.slice(0, 4);
  renderEchoStatus();
}

function normalizeClientCaptureSource(source) {
  const value = String(source || "").trim();
  if (value === "browser-microphone-test") return "seller_mic";
  if (value === "browser-system-audio") return "remote_audio";
  if (value === "browser-audio") return "mixed_audio";
  if (value === "student-system-audio") return "student_system_audio";
  if (value === "student-mic") return "student_mic";
  return value;
}

function compactDebugText(text, maxLen) {
  const normalized = String(text || "").replace(/\s+/g, " ").trim();
  if (normalized.length <= maxLen) return normalized;
  return `${normalized.slice(0, Math.max(0, maxLen - 1))}…`;
}

function formatPercent(part, total) {
  if (!total) return "0%";
  return `${Math.round((part / total) * 100)}%`;
}

function renderEchoStatus() {
  const status = $("echoStatus");
  const metrics = $("echoDebugMetrics");
  const aec3Status = $("aec3Status");
  const aec3Toggle = $("aec3Toggle");
  const micSettings = captureStates.microphone?.micSettings || {};
  const aec = micSettings.echoCancellation === true
    ? "AEC on"
    : micSettings.echoCancellation === false
      ? "AEC off"
      : "AEC unknown";
  const mode = aec3Enabled
    ? `AEC3 ${audioAec3State.ready ? "ready" : audioAec3State.status}`
    : (echoSuppressionEnabled ? "эхо подавляется" : "эхо-фильтр выкл");
  if (status) {
    status.textContent = `микрофон: ${aec} · ${mode}`;
  }
  if (aec3Toggle) {
    aec3Toggle.textContent = aec3Enabled ? "AEC3 вкл" : "AEC3 выкл";
    aec3Toggle.className = aec3Enabled ? "blue" : "ghost";
  }
  if (aec3Status) {
    const stats = audioAec3State.lastStats || {};
    const details = audioAec3State.error
      ? audioAec3State.error
      : `clean=${audioAec3State.cleanFrames} · far=${audioAec3State.farFrames} · delay=${stats.delay_ms ?? "-"}ms · erl=${formatOptionalNumber(stats.echo_return_loss)} · erle=${formatOptionalNumber(stats.echo_return_loss_enhancement)} · residual=${formatOptionalNumber(stats.residual_echo_likelihood)}`;
    aec3Status.textContent = aec3Enabled
      ? `${audioAec3State.ready ? "ready" : audioAec3State.status} · ${details}`
      : "локальный helper выключен";
  }
  if (metrics) {
    const counts = audioEchoState.attributionCounts;
    const localEchoTotal = audioEchoState.sentFrames + audioEchoState.suppressedFrames;
    const expectedRoutes = counts.micSeller + counts.systemClient;
    const suspiciousRoutes = counts.micClient + counts.systemSeller;
    const routeTotal = expectedRoutes + suspiciousRoutes;
    const routeAccuracy = routeTotal ? formatPercent(expectedRoutes, routeTotal) : "жду";
    const recentRejects = audioEchoState.recentRejects.length
      ? audioEchoState.recentRejects.map((item) => `  - ${item}`).join("\n")
      : "  - пока нет";
    metrics.textContent = [
      `audio suppress: sent=${audioEchoState.sentFrames} · suppressed=${audioEchoState.suppressedFrames} (${formatPercent(audioEchoState.suppressedFrames, localEchoTotal)}) · double-talk=${audioEchoState.doubleTalkFrames}`,
      `audio signal: corr=${audioEchoState.lastBestCorr.toFixed(2)} · residual=${audioEchoState.lastResidual.toFixed(2)} · lag=${Math.round(audioEchoState.lastLagMs)}ms · refs=${audioEchoState.systemRing.length}`,
      `server reject: total=${audioEchoState.serverRejected} · echo=${audioEchoState.serverEchoRejected} · source=${audioEchoState.serverSourceSuppressed} · text=${audioEchoState.serverTextRejected}`,
      `attribution: ok=${routeAccuracy} · mic->мы=${counts.micSeller} · mic->клиент=${counts.micClient} · system->клиент=${counts.systemClient} · system->мы=${counts.systemSeller} · system->spk=${counts.systemSpeaker}`,
      `last route: ${audioEchoState.lastAttribution || "жду STT"}`,
      `recent rejects:\n${recentRejects}`,
    ].join("\n");
  }
}

function formatOptionalNumber(value) {
  return typeof value === "number" && Number.isFinite(value) ? value.toFixed(2) : "-";
}

function shouldUseAec3() {
  return aec3Enabled && !isStudentUser();
}

function ensureAec3Connection() {
  if (!shouldUseAec3()) return false;
  if (audioAec3State.ready && audioAec3State.ws?.readyState === WebSocket.OPEN) return true;
  if (audioAec3State.connecting) return false;
  const now = Date.now();
  if (now - audioAec3State.lastConnectAttemptAt < 1500) return false;
  audioAec3State.lastConnectAttemptAt = now;
  audioAec3State.connecting = true;
  audioAec3State.status = "connecting";
  audioAec3State.error = "";
  renderEchoStatus();

  try {
    const ws = new WebSocket(audioAec3State.url);
    audioAec3State.ws = ws;
    ws.onopen = () => {
      audioAec3State.connecting = false;
      audioAec3State.status = "hello";
      sendAec3JSON({ type: "hello", sample_rate_hz: 16000, channels: 1 });
      renderEchoStatus();
    };
    ws.onmessage = (event) => handleAec3Message(event.data);
    ws.onerror = () => {
      audioAec3State.error = "helper websocket error";
      audioAec3State.status = "error";
      audioAec3State.ready = false;
      audioAec3State.connecting = false;
      renderEchoStatus();
    };
    ws.onclose = () => {
      audioAec3State.ready = false;
      audioAec3State.connecting = false;
      audioAec3State.status = "closed";
      renderEchoStatus();
    };
  } catch (error) {
    audioAec3State.ready = false;
    audioAec3State.connecting = false;
    audioAec3State.status = "error";
    audioAec3State.error = error.message;
    renderEchoStatus();
  }

  return false;
}

function closeAec3Connection(reason = "") {
  const ws = audioAec3State.ws;
  audioAec3State.ws = null;
  audioAec3State.ready = false;
  audioAec3State.connecting = false;
  audioAec3State.status = reason || "closed";
  audioAec3State.error = "";
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: "close" }));
    ws.close();
  } else if (ws && ws.readyState === WebSocket.CONNECTING) {
    ws.close();
  }
}

function sendAec3JSON(payload) {
  const ws = audioAec3State.ws;
  if (!ws || ws.readyState !== WebSocket.OPEN) return false;
  ws.send(JSON.stringify(payload));
  return true;
}

function handleAec3Message(raw) {
  let data = null;
  try {
    data = JSON.parse(raw || "{}");
  } catch (error) {
    audioAec3State.error = `bad helper json: ${error.message}`;
    renderEchoStatus();
    return;
  }

  if (data.type === "ready") {
    audioAec3State.ready = true;
    audioAec3State.status = "ready";
    audioAec3State.error = "";
  } else if (data.type === "ack") {
    audioAec3State.lastStats = data.stats || audioAec3State.lastStats;
    if (data.what === "far") audioAec3State.farFrames += Number(data.frames || 0);
  } else if (data.type === "clean") {
    audioAec3State.lastStats = data.stats || audioAec3State.lastStats;
    if (data.samples > 0 && data.pcm16) {
      const captureState = captureStates.microphone;
      if (captureState?.active) {
        const pcm = base64ToInt16(data.pcm16);
        audioAec3State.cleanFrames += 1;
        processPCMForSTT(captureState, pcm, {
          now: Date.now(),
          rms: pcm16Rms(pcm),
          skipEchoFilter: true,
          statusPrefix: "микрофон · AEC3",
        });
      }
    }
  } else if (data.type === "error") {
    audioAec3State.error = data.error || "helper error";
    audioAec3State.status = "error";
  }
  renderEchoStatus();
}

function sendAec3Far(pcm) {
  if (!shouldUseAec3()) return false;
  ensureAec3Connection();
  if (!audioAec3State.ready || audioAec3State.ws?.readyState !== WebSocket.OPEN) return false;
  return sendAec3JSON({ type: "far", pcm16: pcm16ToBase64(pcm) });
}

function sendAec3Near(pcm) {
  if (!shouldUseAec3()) return false;
  ensureAec3Connection();
  if (!audioAec3State.ready || audioAec3State.ws?.readyState !== WebSocket.OPEN) return false;
  if (audioAec3State.ws.bufferedAmount > 1_000_000) {
    audioAec3State.error = "helper backlog; fallback to raw mic";
    return false;
  }
  return sendAec3JSON({ type: "near", pcm16: pcm16ToBase64(pcm) });
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

function logAudioEvent(captureState, event, detail = "", data = {}) {
  const targetSessionId = captureState?.sessionId || sessionId;
  if (!targetSessionId || !captureState) return;
  telemetry.logForSession(targetSessionId, event === "echo_stats" ? "audio_stream_stats" : event, {
    source: captureState.sourceLabel,
    role: captureState.role,
    mode: captureState.mode,
    detail,
    data,
  });
  fetch(`/v1/sessions/${encodeURIComponent(targetSessionId)}/audio/log`, {
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

function showToast(text, durationMs = 1600) {
  const toast = $("toast");
  toast.textContent = text;
  toast.classList.add("show");
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => toast.classList.remove("show"), durationMs);
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

function renderRichText(value) {
  const lines = String(value || "").replace(/\r\n/g, "\n").split("\n");
  const html = [];
  let list = "";
  const closeList = () => {
    if (!list) return;
    html.push(`</${list}>`);
    list = "";
  };
  const openList = (type) => {
    if (list === type) return;
    closeList();
    list = type;
    html.push(`<${type}>`);
  };

  for (const line of lines) {
    const trimmed = line.trim();
    if (!trimmed) {
      closeList();
      continue;
    }
    const bullet = trimmed.match(/^[-*]\s+(.+)$/);
    if (bullet) {
      openList("ul");
      html.push(`<li>${renderInlineMarkdown(bullet[1])}</li>`);
      continue;
    }
    const numbered = trimmed.match(/^\d+[.)]\s+(.+)$/);
    if (numbered) {
      openList("ol");
      html.push(`<li>${renderInlineMarkdown(numbered[1])}</li>`);
      continue;
    }
    closeList();
    html.push(`<p>${renderInlineMarkdown(trimmed)}</p>`);
  }
  closeList();
  return html.join("") || "";
}

function renderInlineMarkdown(value) {
  return escapeHtml(value)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/__([^_]+)__/g, "<strong>$1</strong>")
    .replace(/\*([^*]+)\*/g, "<em>$1</em>")
    .replace(/_([^_]+)_/g, "<em>$1</em>");
}

$("newSession").onclick = () => {
  createSession().catch((error) => $("session").textContent = error.message);
};
$("studentNewSession").onclick = () => {
  createSession().catch((error) => $("studentSession").textContent = error.message);
};
$("logout").onclick = () => logout().catch((error) => showToast(error.message));
$("studentLogout").onclick = () => logout().catch((error) => showToast(error.message));
$("adminLogout").onclick = () => logout().catch((error) => showToast(error.message));
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
$("studentMicToggle").onclick = () => startMicTest({ student: true });
$("studentBothToggle").onclick = startStudentBothAudio;
$("openReplyPip").onclick = () => openReplyPip().catch((error) => showToast(error.message));
$("copyReply").onclick = copyReply;
$("replyText").onclick = copyReply;
$("immediateReplyText").onclick = copyImmediateReply;
$("generateReply").onclick = () => generateReply().catch((error) => showToast(error.message));
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
};
for (const tab of document.querySelectorAll("[data-admin-type]")) {
  tab.onclick = () => selectAdminUserType(tab.dataset.adminType);
}
$("adminSessionsTab").onclick = () => selectAdminMode("sessions").catch((error) => showToast(error.message));
$("adminPromptsTab").onclick = () => selectAdminMode("prompts").catch((error) => showToast(error.message));
for (const tab of document.querySelectorAll("[data-admin-detail-tab]")) {
  tab.onclick = () => {
    adminState.detailTab = tab.dataset.adminDetailTab || "transcript";
    renderAdmin();
  };
}
$("adminRefreshPrompts").onclick = () => {
  const loader = adminState.mode === "sessions" ? loadAdminSessions : loadAdminPrompts;
  loader().catch((error) => showToast(error.message));
};
$("adminNewPrompt").onclick = () => createAdminPromptDraft("prompt");
$("adminNewPlaybook").onclick = () => createAdminPromptDraft("playbook");
$("adminPromptTitle").oninput = markAdminDirty;
$("adminPromptKey").oninput = markAdminDirty;
$("adminPromptContent").oninput = markAdminDirty;
$("adminSavePrompt").onclick = () => saveAdminPrompt().catch((error) => showToast(error.message));
$("adminRevertPrompt").onclick = revertAdminPrompt;

setInterval(() => {
  if (!state || $("salesApp").hidden) return;
  renderPipelineStatus();
  renderManualReplyStatus();
  syncGenerateReplyButton();
  syncReplyPip();
  renderStage();
  renderEchoStatus();
}, 1000);

boot().catch((error) => {
  $("session").textContent = error.message;
  renderAuth(true);
});
