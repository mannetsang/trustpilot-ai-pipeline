// Company Assistant: board, knowledge base, interview, access, talk. Voice lives in voice.js.
"use strict";

const STATUS_ORDER = ["todo", "in_progress", "done"];
const TYPE_LABEL = { chat_reply: "Chat reply", calendar_event: "Calendar event" };
const STATUS_LABEL = { done: "Done", suggested: "Waiting", failed: "Failed", dismissed: "Dismissed" };
const SYSTEM_GROUPS = [
  ["requested", "Requested by the assistant"], ["needed", "Needed"], ["available", "Credentials exist, not connected yet"],
  ["connected", "Connected"], ["not_used", "Not used"],
];
const COMPANY_LABEL = { superhairpieces: "Superhairpieces", gencbeauty: "Gen'C Beauty", both: "Both" };
const SUGGESTIONS = [
  "Interview me: what do you most need to know?", "What do you know so far about the companies?",
  "Which systems do you need access to first, and how do I connect them?", "What's on my calendar this week?",
];

let state = null;      // /api/state: tasks, actions, runs, settings, status
let kb = null;         // /api/knowledge: projects, questions, systems, facts, partners
let partner = "assistant";
let talkTurns = [];
let editing = null;
let editingProject = null;
let sending = null;
let talkBusy = false;

const $ = (id) => document.getElementById(id);

// Builds DOM nodes; text always goes in as text nodes, never as HTML.
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

const safeUrl = (u) => (typeof u === "string" && u.startsWith("https://") ? u : null);
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
const where = (t) => (t.source_from && t.source_from !== t.space_label ? `${t.space_label} · ${t.source_from}` : t.space_label);

async function api(method, path, body) {
  const resp = await fetch(path, {
    method, credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-Chat-Assistant": "1" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (resp.status === 401) { location.reload(); throw new Error("Signed out"); }
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.error || `Request failed (${resp.status})`);
  return data;
}

function toast(msg) {
  const t = $("toast"); t.textContent = msg; t.classList.add("show");
  clearTimeout(toast.timer); toast.timer = setTimeout(() => t.classList.remove("show"), 3800);
}

function ago(iso) {
  if (!iso) return "";
  const s = (Date.now() - new Date(iso.split("#")[0]).getTime()) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return new Date(iso.split("#")[0]).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function todayStr() {
  const d = new Date(); d.setMinutes(d.getMinutes() - d.getTimezoneOffset());
  return d.toISOString().slice(0, 10);
}

// -- loading ---------------------------------------------------------------------------
async function load() {
  [state, kb] = await Promise.all([api("GET", "/api/state"), api("GET", "/api/knowledge")]);
  render();
  checkVersion();
}

// A new deploy changes the server's version; an open tab would otherwise keep running the old
// scripts. Reload when nothing is in progress, or say so when something is.
function checkVersion() {
  const mine = document.body.dataset.version;
  if (!state.version || !mine || state.version === mine) return;
  const busy = talkBusy || !$("voicebar").hidden || document.querySelector("dialog[open]") || $("message").value.trim();
  let tried = null;
  try { tried = sessionStorage.getItem("reloadedFor"); } catch { /* storage unavailable */ }
  if (!busy && tried !== state.version) {   // once per version, so a stuck mismatch can't loop
    try { sessionStorage.setItem("reloadedFor", state.version); } catch { /* storage unavailable */ }
    location.reload(); return;
  }
  if ($("updateBanner")) return;
  $("banners").append(el("div", { class: "banner", id: "updateBanner" },
    el("span", {}, el("b", {}, "A new version is ready. "), "Reload when you're done to get it."),
    el("button", { class: "btn primary", onclick: () => location.reload() }, "Reload")));
}

function render() {
  renderHeader();
  renderBanners();
  renderPartners();
  renderFilters();
  renderBoard();
  renderProjects();
  renderQuestions();
  renderSystems();
  renderActivity();
}

function renderHeader() {
  const run = state.status.last_run;
  $("dot").classList.toggle("on", state.connected && !state.status.connection_error);
  $("autoAct").checked = !!state.settings.auto_act;
  if (!run) { $("lastrun").textContent = "No runs yet. It reads your chats every hour, or press Run now."; return; }
  if (run.error) {
    $("lastrun").textContent = `Last run ${ago(run.started_at)} failed: ${run.error}${run.trace ? " (details in Activity → Runs)" : ""}`;
    return;
  }
  const learned = (run.projects || 0) + (run.facts || 0);
  $("lastrun").textContent = `Last run ${ago(run.finished_at || run.started_at)} · ${plural(run.spaces_processed, "conversation")} · `
    + `${plural(run.tasks_created, "new task")} · ${plural(learned, "thing")} learned · ${plural(run.actions_done, "action")} taken`;
}

function renderBanners() {
  const box = $("banners"); box.replaceChildren();
  if (!state.connected) {
    box.append(el("div", { class: "banner" },
      el("span", {}, el("b", {}, "Connect Google. "), "The assistant needs permission to read your chats, reply as you, see the company directory and add calendar events."),
      el("a", { class: "btn primary", href: "/login?connect=1" }, "Connect")));
  } else if (state.status.connection_error) {
    box.append(el("div", { class: "banner bad" },
      el("span", {}, el("b", {}, "The assistant can't reach Google. "), state.status.connection_error),
      el("a", { class: "btn primary", href: "/login?connect=1" }, "Reconnect")));
  }
  if ((state.status.missing_scopes || []).length) {
    box.append(el("div", { class: "banner" },
      el("span", {}, el("b", {}, "Some permissions weren't granted: "), state.status.missing_scopes.join(", "),
        ". Parts of the assistant won't work until you reconnect and tick every box."),
      el("a", { class: "btn", href: "/login?connect=1" }, "Reconnect")));
  }
}

// -- Talk ------------------------------------------------------------------------------
const partnerInfo = (name) => kb.partners.find((p) => p.name === name) || { label: name, available: false };

function renderPartners() {
  const box = $("partners");
  box.replaceChildren(...kb.partners.map((p) => el("button", {
    class: `partner${p.name === partner ? " active" : ""}`, title: p.available ? p.models || "" : p.detail, "data-partner": p.name,
    onclick: () => selectPartner(p.name),
  }, el("span", { class: `pdot${p.available ? "" : " off"}` }), p.label)),
  el("span", { class: "spacer", style: "flex:1" }),
  el("label", { class: "readaloud", title: "Speak each new reply out loud" },
    el("input", { type: "checkbox", checked: !!(speaker() && speaker().auto),
      onchange: (e) => { if (speaker()) speaker().auto = e.target.checked; } }), "🔊 Read replies aloud"),
  el("button", { class: "btn", onclick: clearTalk, title: "Start a new conversation" }, "Clear"));
  const info = partnerInfo(partner);
  $("startVoice").disabled = !$("voicebar").hidden || !info.available;  // one call at a time
  $("startVoice").title = info.available ? `Talk live with ${info.label}` : info.detail || `${info.label} isn't available`;
  $("message").placeholder = `Message ${partnerInfo(partner).label}… (Enter to send, Shift+Enter for a new line)`;
}

async function selectPartner(name) {
  if (name !== partner && window.companyAssistant.endVoice) window.companyAssistant.endVoice();
  if (name !== partner && speaker()) speaker().stop();
  partner = name;
  renderPartners();
  await loadTalk();
}

async function loadTalk() {
  try { talkTurns = (await api("GET", `/api/talk/${partner}`)).turns; } catch (e) { toast(e.message); talkTurns = []; }
  renderThread();
}

const speaker = () => window.companySpeaker;  // static/speaker.js

function turnNode(t, live = false) {
  const tools = (t.tools || []).map((x) => x.tool);
  const who = partner;  // the thread on screen belongs to the selected partner
  const readAloud = t.role === "assistant" && !live && t.text
    ? el("button", { class: "spk", title: "Read aloud", "aria-label": "Read aloud",
        onclick: (e) => speaker() && speaker().toggle(t.text, who, e.currentTarget) }, "🔊")
    : null;
  return el("div", { class: `msg ${t.role}${live ? " live" : ""}` }, t.text,
    tools.length ? el("div", { class: "tools" }, `Used: ${[...new Set(tools)].join(", ")}`) : null,
    t.at ? el("div", { class: "when" }, `${t.voice ? "🎙 " : ""}${ago(t.at)}`, readAloud) : readAloud);
}

function renderThread(extra = []) {
  const box = $("thread");
  const nodes = talkTurns.map((t) => turnNode(t)).concat(extra);
  if (!nodes.length) {
    box.replaceChildren(el("div", { class: "suggestions" }, SUGGESTIONS.map((s) =>
      el("button", { onclick: () => { $("message").value = s; sendMessage(); } }, s))));
    return;
  }
  box.replaceChildren(...nodes);
  box.scrollTop = box.scrollHeight;
}

async function sendMessage() {
  const text = $("message").value.trim();
  if (!text || talkBusy) return;
  talkBusy = true; $("send").disabled = true;
  $("message").value = "";
  const pending = { role: "user", text, at: new Date().toISOString() };
  talkTurns.push(pending);
  renderThread([el("div", { class: "msg assistant live" }, el("span", { class: "spinner" }), " thinking…")]);
  try {
    const asked = partner;
    const { reply } = await api("POST", `/api/talk/${asked}`, { message: text });
    talkTurns.push(reply);
    renderThread();
    refreshKnowledge();
    if (speaker() && speaker().auto && asked === partner) {
      const buttons = $("thread").querySelectorAll(".msg.assistant .spk");
      speaker().play(reply.text, asked, buttons[buttons.length - 1]);
    }
  } catch (e) {
    talkTurns.pop(); renderThread();
    $("message").value = text;
    toast(e.message);
  } finally {
    talkBusy = false; $("send").disabled = false; $("message").focus();
  }
}

async function clearTalk() {
  if (!confirm(`Clear your conversation with ${partnerInfo(partner).label}? What it learned stays in the knowledge base.`)) return;
  await api("DELETE", `/api/talk/${partner}`).catch((e) => toast(e.message));
  talkTurns = []; renderThread();
}

async function refreshKnowledge() {
  try { kb = await api("GET", "/api/knowledge"); renderProjects(); renderQuestions(); renderSystems(); renderPartners(); }
  catch { /* next full load will catch up */ }
}

$("send").onclick = sendMessage;
$("message").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendMessage(); }
});

// -- Board -----------------------------------------------------------------------------
function renderFilters() {
  const select = $("spaceFilter"); const current = select.value;
  const labels = new Map();
  for (const t of state.tasks) if (t.space) labels.set(t.space, t.space_label || t.space);
  select.replaceChildren(el("option", { value: "" }, "All conversations"),
    ...[...labels.entries()].sort((a, b) => a[1].localeCompare(b[1])).map(([k, v]) => el("option", { value: k }, v)));
  select.value = labels.has(current) ? current : "";
}

function visibleTasks() {
  const q = $("q").value.trim().toLowerCase();
  const space = $("spaceFilter").value;
  const owner = $("ownerFilter").value;
  return state.tasks.filter((t) => {
    if (space && t.space !== space) return false;
    if (owner === "me" && !t.owner_is_me) return false;
    if (owner === "others" && t.owner_is_me) return false;
    if (q && !`${t.title} ${t.detail || ""} ${t.owner || ""} ${t.space_label || ""}`.toLowerCase().includes(q)) return false;
    return true;
  });
}

function taskCard(t) {
  const meta = el("div", { class: "meta" }, el("span", { class: `pri ${t.priority || "medium"}`, title: `${t.priority || "medium"} priority` }));
  meta.append(el("span", { class: `chip ${t.owner_is_me ? "me" : ""}` }, t.owner_is_me ? "You" : (t.owner || "Unassigned")));
  if (t.due) {
    const today = todayStr();
    const open = t.status !== "done";
    const cls = open && t.due < today ? "overdue" : open && t.due === today ? "today" : "";
    meta.append(el("span", { class: `chip ${cls}` }, t.due === today ? "Due today" : `Due ${t.due}`));
  }
  if (t.origin === "assistant") meta.append(el("span", { class: "chip", title: "Added by the assistant" }, "AI"));
  const next = STATUS_ORDER[STATUS_ORDER.indexOf(t.status) + 1];
  const card = el("div", { class: "card", tabindex: "0", role: "button",
      onclick: () => openTask(t), onkeydown: (e) => { if (e.key === "Enter") openTask(t); } },
    el("div", { class: "title" }, t.title), meta,
    t.space_label ? el("div", { class: "src", title: t.source_excerpt || "" }, where(t)) : null);
  if (next) card.append(el("button", { class: "advance", title: next === "done" ? "Mark done" : "Start", "aria-label": next === "done" ? "Mark done" : "Start",
    onclick: (e) => { e.stopPropagation(); updateTask(t.id, { status: next }).catch((err) => toast(err.message)); } }, next === "done" ? "✓" : "→"));
  return card;
}

function renderBoard() {
  const tasks = visibleTasks();
  const showDone = $("showDone").checked;
  $("boardCount").textContent = state.tasks.filter((t) => t.status !== "done").length;
  for (const col of document.querySelectorAll(".col")) {
    const status = col.dataset.status;
    let items = tasks.filter((t) => t.status === status);
    col.querySelector(".count").textContent = items.length;
    if (status === "done" && !showDone) items = [];
    items.sort((a, b) => (a.due || "9999").localeCompare(b.due || "9999") || (b.created_at || "").localeCompare(a.created_at || ""));
    const box = col.querySelector(".cards");
    box.replaceChildren(...items.map(taskCard));
    if (!items.length) box.append(el("div", { class: "empty" },
      status === "done" && !showDone ? "Tick “Show done” to see finished tasks" : "Nothing here"));
  }
}

function openTask(t) {
  editing = t || null;
  const f = $("taskForm");
  $("taskDialogTitle").textContent = t ? "Edit task" : "New task";
  f.title.value = t ? t.title : "";
  f.detail.value = t ? t.detail || "" : "";
  f.owner.value = t ? t.owner || "" : "";
  f.due.value = t ? t.due || "" : "";
  f.status.value = t ? t.status : "todo";
  f.priority.value = t ? t.priority || "medium" : "medium";
  f.owner_is_me.checked = t ? !!t.owner_is_me : true;
  $("deleteTask").hidden = !t;
  const src = $("taskSource"); src.replaceChildren();
  if (t && t.space_label) {
    src.append(`From ${where(t)}${t.source_excerpt ? `: “${t.source_excerpt}” ` : " "}`);
    const link = safeUrl(t.space_uri);
    if (link) src.append(el("a", { href: link, target: "_blank", rel: "noopener" }, "Open chat"));
  }
  $("taskDialog").showModal();
}

async function updateTask(id, fields) {
  const { task } = await api("PATCH", `/api/tasks/${encodeURIComponent(id)}`, fields);
  state.tasks = state.tasks.map((t) => (t.id === id ? task : t));
  renderBoard();
}

$("taskForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  const fields = { title: f.title.value, detail: f.detail.value, owner: f.owner.value, due: f.due.value,
    status: f.status.value, priority: f.priority.value, owner_is_me: f.owner_is_me.checked };
  try {
    if (editing) await updateTask(editing.id, fields);
    else { const { task } = await api("POST", "/api/tasks", fields); state.tasks.unshift(task); renderFilters(); renderBoard(); }
    $("taskDialog").close();
  } catch (err) { toast(err.message); }
});
$("cancelTask").onclick = () => $("taskDialog").close();
$("deleteTask").onclick = async () => {
  if (!editing || !confirm("Delete this task?")) return;
  try {
    await api("DELETE", `/api/tasks/${encodeURIComponent(editing.id)}`);
    state.tasks = state.tasks.filter((t) => t.id !== editing.id);
    $("taskDialog").close(); renderFilters(); renderBoard();
  } catch (err) { toast(err.message); }
};
$("newTask").onclick = () => openTask(null);
for (const id of ["q", "spaceFilter", "ownerFilter", "showDone"]) $(id).addEventListener("input", renderBoard);

// -- Projects --------------------------------------------------------------------------
function renderProjects() {
  const company = $("companyFilter").value;
  const status = $("projectStatusFilter").value;
  const items = kb.projects.filter((p) => (!company || p.company === company || p.company === "both") && (!status || p.status === status));
  $("projectCount").textContent = kb.projects.filter((p) => p.status !== "done").length;
  const openTasks = (pid) => state.tasks.filter((t) => t.project_id === pid && t.status !== "done").length;
  const box = $("projects");
  if (!items.length) {
    box.replaceChildren(el("div", { class: "empty" }, kb.projects.length ? "No projects match the filters"
      : "No projects yet. They appear as the assistant reads your chats and hears your answers."));
    return;
  }
  box.replaceChildren(...items.map((p) => el("div", { class: "card", tabindex: "0", role: "button",
      onclick: () => openProject(p), onkeydown: (e) => { if (e.key === "Enter") openProject(p); } },
    el("div", { class: "title" }, p.name),
    el("div", { class: "meta" },
      el("span", { class: `chip ${p.status || ""}` }, p.status || "unknown"),
      p.company ? el("span", { class: "chip" }, COMPANY_LABEL[p.company] || p.company) : null,
      p.owner ? el("span", { class: "chip" }, p.owner) : null,
      p.deadline ? el("span", { class: "chip" }, `Due ${p.deadline}`) : null,
      openTasks(p.id) ? el("span", { class: "chip" }, plural(openTasks(p.id), "open task")) : null,
      (p.spaces || []).length ? el("span", { class: "chip" }, plural(p.spaces.length, "chat")) : null),
    (p.goal || p.summary) ? el("div", { class: "body" }, p.goal || p.summary) : null,
    p.next_steps ? el("div", { class: "body" }, `Next: ${p.next_steps}`) : null)));
}

function openProject(p) {
  editingProject = p || null;
  const f = $("projectForm");
  $("projectDialogTitle").textContent = p ? "Edit project" : "New project";
  for (const k of ["name", "owner", "deadline", "goal", "summary", "next_steps"]) f[k].value = p ? p[k] || "" : "";
  f.company.value = p ? p.company || "" : "";
  f.status.value = p ? p.status || "active" : "active";
  $("deleteProject").hidden = !p;
  $("projectSource").textContent = p && p.source ? `Added from ${p.source} · updated ${ago(p.updated_at)}` : "";
  $("projectDialog").showModal();
}

$("projectForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  const fields = {};
  for (const k of ["name", "company", "status", "owner", "deadline", "goal", "summary", "next_steps"]) fields[k] = f[k].value;
  try {
    if (editingProject) await api("PATCH", `/api/projects/${encodeURIComponent(editingProject.id)}`, fields);
    else await api("POST", "/api/projects", fields);
    $("projectDialog").close(); await refreshKnowledge();
  } catch (err) { toast(err.message); }
});
$("cancelProject").onclick = () => $("projectDialog").close();
$("deleteProject").onclick = async () => {
  if (!editingProject || !confirm("Delete this project? Its tasks stay on the board.")) return;
  try { await api("DELETE", `/api/projects/${encodeURIComponent(editingProject.id)}`); $("projectDialog").close(); await refreshKnowledge(); }
  catch (err) { toast(err.message); }
};
$("newProject").onclick = () => openProject(null);
for (const id of ["companyFilter", "projectStatusFilter"]) $(id).addEventListener("input", renderProjects);

// -- Questions (the interview) ------------------------------------------------------------
function renderQuestions() {
  const open = kb.questions.filter((q) => q.status === "open")
    .sort((a, b) => (a.priority || 2) - (b.priority || 2) || (a.created_at || "").localeCompare(b.created_at || ""));
  const answered = kb.questions.filter((q) => q.status === "answered").slice(0, 40);
  const qc = $("questionCount"); qc.textContent = open.length; qc.classList.toggle("hot", open.length > 0);
  $("openQuestions").replaceChildren(...(open.length ? open.map(questionItem) : [el("div", { class: "empty" }, "No open questions. The assistant adds more as it learns.")]));
  $("answeredQuestions").replaceChildren(...(answered.length ? answered.map((q) => el("div", { class: "item" },
    el("div", { class: "q" }, q.question),
    el("div", { class: "reply" }, q.answer || ""),
    q.digest ? el("div", { class: "why" }, `Assistant: ${q.digest}`) : null,
    el("div", { class: "why" }, `Answered ${ago(q.answered_at)}`))) : [el("div", { class: "empty" }, "Nothing answered yet")]));
}

function questionItem(q) {
  const box = el("textarea", { placeholder: "Your answer (short is fine)", "aria-label": "Answer" });
  const answerBtn = el("button", { class: "btn primary" }, "Answer");
  answerBtn.onclick = async () => {
    const answer = box.value.trim();
    if (!answer) return;
    answerBtn.disabled = true; answerBtn.replaceChildren(el("span", { class: "spinner" }), " Saving and learning…");
    try {
      const { note } = await api("POST", `/api/questions/${encodeURIComponent(q.id)}/answer`, { answer });
      toast(note || "Saved");
      await Promise.all([refreshKnowledge(), load()]);
    } catch (e) { toast(e.message); answerBtn.disabled = false; answerBtn.textContent = "Answer"; }
  };
  return el("div", { class: "item" },
    el("div", { class: "top" }, el("span", { class: `pri ${q.priority === 1 ? "high" : q.priority === 3 ? "low" : "medium"}` }),
      el("span", {}, q.priority === 1 ? "Important" : q.priority === 3 ? "When you have time" : "Normal"),
      q.source ? el("span", {}, `· ${q.source}`) : null),
    el("div", { class: "q" }, q.question),
    q.why ? el("div", { class: "why" }, `Why: ${q.why}`) : null,
    box,
    el("div", { class: "actions" }, answerBtn,
      el("button", { class: "btn", onclick: () => askInTalk(q) }, "Discuss in Talk"),
      el("button", { class: "btn", onclick: async () => {
        await api("POST", `/api/questions/${encodeURIComponent(q.id)}/dismiss`).catch((e) => toast(e.message));
        refreshKnowledge();
      } }, "Skip")));
}

function askInTalk(q) {
  switchTab("talk");
  if (partner !== "assistant") selectPartner("assistant");
  $("message").value = `Let's talk about this question [${q.id}]: ${q.question}`;
  $("message").focus();
}

// -- Access -----------------------------------------------------------------------------
function renderSystems() {
  const pending = kb.systems.filter((s) => s.status === "requested" || s.status === "needed").length;
  $("accessCount").textContent = pending;
  const box = $("systems");
  box.replaceChildren(...SYSTEM_GROUPS.map(([status, title]) => {
    const items = kb.systems.filter((s) => (s.status || "needed") === status);
    if (!items.length) return null;
    return el("div", {}, el("h3", {}, `${title} (${items.length})`), ...items.map((s) => {
      const select = el("select", { "aria-label": `Status of ${s.name}` },
        ...SYSTEM_GROUPS.map(([v, label]) => el("option", { value: v }, label.split(" (")[0])));
      select.value = s.status || "needed";
      select.onchange = async () => {
        try { await api("PATCH", `/api/systems/${encodeURIComponent(s.id)}`, { status: select.value }); refreshKnowledge(); }
        catch (e) { toast(e.message); }
      };
      return el("div", { class: "item" },
        el("div", { class: "top" }, el("b", {}, s.name), s.category ? el("span", { class: "chip" }, s.category) : null,
          el("span", { class: "spacer", style: "flex:1" }), select),
        s.unlocks ? el("div", { class: "reply" }, s.unlocks) : null,
        s.why ? el("div", { class: "why" }, `Why the assistant asked: ${s.why}`) : null,
        status !== "connected" && status !== "not_used" ? el("div", { class: "actions" },
          el("button", { class: "btn", onclick: () => {
            switchTab("talk");
            if (partner !== "assistant") selectPartner("assistant");
            $("message").value = `How do I give you access to ${s.name}? Walk me through it step by step.`;
            $("message").focus();
          } }, "How do I connect this?")) : null);
    }));
  }).filter(Boolean));
}

// -- Activity -----------------------------------------------------------------------------
function actionItem(a) {
  const link = safeUrl(a.space_uri);
  const top = el("div", { class: "top" },
    el("span", { class: `status ${a.status}` }, STATUS_LABEL[a.status] || a.status),
    el("b", {}, TYPE_LABEL[a.type] || a.type),
    el("span", {}, "in"),
    link ? el("a", { href: link, target: "_blank", rel: "noopener" }, a.space_label || "chat") : el("span", {}, a.space_label || ""),
    el("span", {}, `· ${ago(a.executed_at || a.created_at)}`));
  const body = [top, el("div", { class: "quote" }, `${a.source_from || "Someone"}: ${a.source_excerpt || ""}`)];
  if (a.type === "chat_reply") body.push(el("div", { class: "reply" }, a.reply_text || ""));
  if (a.type === "calendar_event") body.push(el("div", { class: "reply" },
    `${a.event_title} · ${(a.event_start || "").replace("T", " ").slice(0, 16)}–${(a.event_end || "").slice(11, 16)}`
    + (a.event_attendees && a.event_attendees.length ? ` · with ${a.event_attendees.join(", ")}` : "")));
  const why = [a.reason, a.blocked_reason ? `Held back: ${a.blocked_reason}` : "", a.error ? `Error: ${a.error}` : ""].filter(Boolean).join(" · ");
  if (why) body.push(el("div", { class: "why" }, why));
  const result = safeUrl(a.result);
  if (a.status === "done" && result && a.type === "calendar_event") body.push(el("a", { href: result, target: "_blank", rel: "noopener" }, "Open event"));
  if (a.status === "suggested" || a.status === "failed") {
    body.push(el("div", { class: "actions" },
      el("button", { class: "btn primary", onclick: () => sendAction(a) }, a.type === "chat_reply" ? "Review & send" : "Create event"),
      el("button", { class: "btn", onclick: () => dismissAction(a) }, "Dismiss")));
  }
  return el("div", { class: "item" }, body);
}

function renderActivity() {
  const waiting = state.actions.filter((a) => a.status === "suggested");
  const recent = state.actions.filter((a) => a.status !== "suggested").slice(0, 60);
  const wc = $("waitingCount"); wc.textContent = waiting.length; wc.classList.toggle("hot", waiting.length > 0);
  $("waiting").replaceChildren(...(waiting.length ? waiting.map(actionItem) : [el("div", { class: "empty" }, "Nothing waiting for your approval")]));
  $("recent").replaceChildren(...(recent.length ? recent.map(actionItem) : [el("div", { class: "empty" }, "No actions yet")]));
  $("runs").replaceChildren(...(state.runs.length ? state.runs.map((r) => el("tr", {},
    el("td", {}, new Date(r.started_at).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })),
    el("td", {}, r.trigger === "schedule" ? "Hourly" : "Manual"),
    el("td", {}, r.error ? "–" : r.spaces_processed),
    el("td", {}, r.error ? "–" : r.messages),
    el("td", {}, r.error ? "–" : `${r.tasks_created} / ${r.tasks_updated}`),
    el("td", {}, r.error ? "–" : `${r.projects || 0} projects · ${r.facts || 0} facts · ${r.questions || 0} questions`),
    el("td", {}, r.error ? "–" : `${r.actions_done} / ${r.actions_suggested}`),
    el("td", { class: (r.error || (r.errors || []).length) ? "err" : "" },
      r.error || (r.errors || []).map((e) => `${e.space}: ${e.error}`).join("; ") || "–",
      r.trace ? el("details", {}, el("summary", {}, "Technical details"),
        el("pre", { style: "white-space:pre-wrap;font-size:11px;max-height:260px;overflow:auto" }, r.trace)) : null)))
    : [el("tr", {}, el("td", { colspan: "8", class: "empty" }, "No runs yet"))]));
}

async function sendAction(a) {
  if (a.type === "chat_reply") {
    sending = a;
    $("sendContext").textContent = `${a.space_label || ""} · ${a.source_from || ""}: “${a.source_excerpt || ""}”`;
    $("sendForm").reply_text.value = a.reply_text || "";
    $("sendDialog").showModal();
    return;
  }
  if (!confirm(`Create “${a.event_title}” and invite ${(a.event_attendees || []).join(", ") || "no one"}?`)) return;
  await finishSend(a, {});
}

async function finishSend(a, body) {
  try {
    const { action } = await api("POST", `/api/actions/${encodeURIComponent(a.id)}/send`, body);
    state.actions = state.actions.map((x) => (x.id === a.id ? { ...action, id: a.id } : x));
    renderActivity();
    toast(action.status === "done" ? "Done" : `Failed: ${action.error}`);
  } catch (e) { toast(e.message); }
}

$("sendForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = e.target.reply_text.value.trim();
  if (!text) return;
  $("sendDialog").close();
  await finishSend(sending, { reply_text: text });
});
$("cancelSend").onclick = () => $("sendDialog").close();

async function dismissAction(a) {
  try {
    const { action } = await api("POST", `/api/actions/${encodeURIComponent(a.id)}/dismiss`);
    state.actions = state.actions.map((x) => (x.id === a.id ? { ...action, id: a.id } : x));
    renderActivity();
  } catch (e) { toast(e.message); }
}

// -- Settings -----------------------------------------------------------------------------
function renderSettings() {
  const tiers = $("tiers"); tiers.replaceChildren();
  for (const [key, label] of kb.autonomy_categories) {
    const current = state.settings.autonomy[key] || "ask";
    const seg = el("div", { class: "seg", role: "group", "aria-label": label },
      ...["auto", "ask"].map((v) => el("button", { type: "button", class: v === current ? "on" : "",
        onclick: async () => {
          try {
            state.settings = (await api("PATCH", "/api/settings", { autonomy: { [key]: v } })).settings;
            renderSettings();
          } catch (e) { toast(e.message); }
        } }, v === "auto" ? "Auto" : "Ask")));
    tiers.append(el("span", {}, label), seg);
  }
  $("partnersEnabled").checked = state.settings.partners_enabled !== false;
  $("openaiModel").value = state.settings.openai_model || "";
  $("readBots").checked = !!state.settings.read_bot_posts;
}

$("openSettings").onclick = () => { renderSettings(); $("partnerResults").textContent = ""; $("settingsDialog").showModal(); };
$("closeSettings").onclick = () => $("settingsDialog").close();
$("partnersEnabled").addEventListener("change", async (e) => {
  try { state.settings = (await api("PATCH", "/api/settings", { partners_enabled: e.target.checked })).settings; refreshKnowledge(); }
  catch (err) { e.target.checked = !e.target.checked; toast(err.message); }
});
$("openaiModel").addEventListener("change", async (e) => {
  try { state.settings = (await api("PATCH", "/api/settings", { openai_model: e.target.value })).settings; toast("Saved"); }
  catch (err) { toast(err.message); }
});
$("readBots").addEventListener("change", async (e) => {
  try { state.settings = (await api("PATCH", "/api/settings", { read_bot_posts: e.target.checked })).settings; }
  catch (err) { e.target.checked = !e.target.checked; toast(err.message); }
});
$("testPartners").onclick = async () => {
  const out = $("partnerResults"); out.replaceChildren(el("span", { class: "spinner" }), " testing…");
  try {
    const { results } = await api("POST", "/api/partners/test");
    out.textContent = kb.partners.map((p) => [p, results[p.name]]).filter(([, r]) => r)
      .map(([p, r]) => `${p.label}: ${r.ok ? "working" : r.detail}`).join(" · ");
  } catch (e) { out.textContent = e.message; }
};

$("autoAct").addEventListener("change", async (e) => {
  try {
    const { settings } = await api("PATCH", "/api/settings", { auto_act: e.target.checked });
    state.settings = settings;
    toast(settings.auto_act ? "The assistant will act on its own (within your autonomy settings)" : "Paused: everything waits for your approval");
  } catch (err) { e.target.checked = !e.target.checked; toast(err.message); }
});

// -- live progress of a run (manual or hourly) ---------------------------------------
const STALE_MS = 10 * 60 * 1000;  // a "running" record this old means the run died
let pollTimer = null;
let ownRun = false;               // true while this tab's Run now request is open
let lastFound = "";               // tasks/actions found so far, to refresh the board as they arrive

const isLive = (p) => !!(p && p.running && p.updated_at && Date.now() - new Date(p.updated_at).getTime() < STALE_MS);

function eta(p) {
  if (!p.reading_started_at || p.done < 2 || p.done >= p.total) return p.total ? "estimating time left" : "";
  const perConversation = (Date.now() - new Date(p.reading_started_at).getTime()) / p.done;
  const mins = Math.round((perConversation * (p.total - p.done)) / 60000);
  return mins < 1 ? "less than a minute left" : `about ${mins} min left`;
}

function renderProgress(p) {
  const box = $("progress");
  if (!isLive(p)) { box.hidden = true; return; }
  const reading = p.phase === "Reading conversations" && p.total > 0;
  const pct = reading ? Math.round((p.done / p.total) * 100) : 0;
  const head = el("div", { class: "line1" },
    el("span", { class: "spinner", "aria-hidden": "true" }),
    el("span", { class: "phase" }, reading ? `Reading conversations: ${p.done} of ${p.total}` : `${p.phase}…`),
    el("span", { class: "eta" }, [p.trigger === "schedule" ? "hourly run" : "", eta(p)].filter(Boolean).join(" · ")));
  const bar = el("div", { class: `bar${reading ? "" : " indeterminate"}`, role: "progressbar", "aria-label": "Run progress",
      "aria-valuemin": "0", "aria-valuemax": "100", "aria-valuenow": reading ? String(pct) : null },
    el("div", { style: reading ? `width:${pct}%` : null }));
  const now = (p.current || []).length ? el("div", { class: "now" }, `Now reading: ${p.current.join(", ")}`) : null;
  const sofar = reading ? el("div", { class: "sofar" }, "So far: " + [
    plural(p.tasks_created, "new task"),
    p.tasks_updated ? `${p.tasks_updated} updated` : "",
    `${plural(p.actions_done, "action")} taken`,
    p.actions_suggested ? `${p.actions_suggested} waiting for you` : "",
    p.problems ? plural(p.problems, "problem") : "",
  ].filter(Boolean).join(" · ")) : null;
  box.replaceChildren(head, bar, now, sofar);
  box.hidden = false;
}

function setRunning(on) {
  const btn = $("runNow"); btn.disabled = on; btn.textContent = on ? "Running…" : "Run now";
}

async function pollProgress() {
  let p;
  try { p = await api("GET", "/api/progress"); } catch { return; }
  renderProgress(p);
  const live = isLive(p);
  setRunning(live || ownRun);
  if (live && !pollTimer) pollTimer = setInterval(pollProgress, 2000);
  const found = `${p.tasks_created}/${p.tasks_updated}/${p.actions_done}/${p.actions_suggested}`;
  if (live && found !== lastFound && !document.querySelector("dialog[open]")) load().catch(() => {});
  lastFound = live ? found : "";
  if (!live && pollTimer) {
    clearInterval(pollTimer); pollTimer = null;
    if (!ownRun) {  // an hourly run (or another tab's) just ended: show its results
      await load().catch(() => {});
      if (p.finished_at) toast(p.error ? `Run failed: ${p.error}`
        : `Run finished: ${plural(p.tasks_created, "new task")}, ${plural(p.actions_done, "action")} taken`);
    }
  }
}

$("runNow").onclick = async () => {
  ownRun = true; setRunning(true);
  if (!pollTimer) pollTimer = setInterval(pollProgress, 2000);
  setTimeout(pollProgress, 700);
  try {
    const r = await api("POST", "/api/run");
    toast(`Read ${plural(r.spaces_processed, "conversation")}: ${plural(r.tasks_created, "new task")}, ${plural(r.actions_done, "action")} taken`);
  } catch (e) {
    toast(/already in progress/.test(e.message) ? "A run is already going. Showing its progress." : e.message);
  }
  await pollProgress();  // hides the panel once the run is over
  ownRun = false;
  setRunning(!!pollTimer);
  load().catch((e) => toast(e.message));
};

$("logout").onclick = async () => { await api("POST", "/logout").catch(() => {}); location.reload(); };

// -- tabs ---------------------------------------------------------------------------------
function switchTab(name) {
  for (const t of document.querySelectorAll(".tab")) t.classList.toggle("active", t.dataset.tab === name);
  for (const v of ["talk", "board", "projects", "questions", "access", "activity"]) $(`view-${v}`).hidden = v !== name;
  try { localStorage.setItem("tab", name); } catch { /* storage unavailable */ }
}
for (const tab of document.querySelectorAll(".tab")) tab.onclick = () => switchTab(tab.dataset.tab);

// Exposed for voice.js
window.companyAssistant = { el, toast, api, renderThread, turnNode, loadTalk, refreshKnowledge, partnerInfo, get partner() { return partner; } };

(async function start() {
  let tab = "talk";
  try { tab = localStorage.getItem("tab") || "talk"; } catch { /* storage unavailable */ }
  switchTab(tab);
  try { await load(); await loadTalk(); await pollProgress(); } catch (e) { $("lastrun").textContent = e.message; }
})();
// Every minute: notice an hourly run starting, and refresh unless a dialog is open or a reply is pending.
setInterval(() => {
  if (!pollTimer) pollProgress();
  if (!document.querySelector("dialog[open]") && !talkBusy) load().catch(() => {});
}, 60000);
