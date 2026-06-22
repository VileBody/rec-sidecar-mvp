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
let pendingStudentDirection = "";
let studentAnswerLanguage = {};
let replyPipWindow = null;
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

function sessionStorageKey() {
  const identity = currentUser?.id || currentUser?.email || "dev";
  const role = currentRole();
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
  return currentRole() === "student";
}

function isAdminUser() {
  return currentRole() === "admin";
}

function currentRole() {
  return currentUser?.role || currentUser?.user_type || "sales";
}

async function boot() {
  initSpeakerMap();
  const ok = await loadMe();
  if (ok) await enterCurrentApp();
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
  pendingStudentDirection = direction;
  applyStudentDirectionToCapture(direction);
  renderStudent();
  await postEvent({ type: "student.direction", direction });
}

function applyStudentDirectionToCapture(direction) {
  const captureState = captureStates.system;
  if (!captureState?.active || captureState.role !== "student_original") return;
  captureState.direction = direction;
  captureState.language = sourceLanguageForDirection(direction);
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

boot().catch((error) => {
  $("session").textContent = error.message;
  renderAuth(true);
});
