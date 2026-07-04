/* JobForge dashboard — vanilla JS, no build step.
   Talks to the API at /api/v1 with a JWT from localStorage; polls every 3s. */

const API = "/api/v1";
const $ = (id) => document.getElementById(id);

const state = {
  token: localStorage.getItem("jf_token"),
  user: null,
  orgs: [], projects: [], queues: [],
  orgId: null, projectId: null,
  tab: "overview",
  jobsOffset: 0, jobsTotal: 0,
  registering: false,
};

/* ─── tab labels ─── */
const TAB_LABELS = {
  overview: "Overview",
  queues: "Queues",
  jobs: "Jobs",
  schedules: "Schedules",
  workers: "Workers",
  dlq: "Dead Letters",
};

/* ─── api helper ─── */
async function api(path, opts = {}) {
  const headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
  if (state.token) headers["Authorization"] = "Bearer " + state.token;
  const res = await fetch(API + path, { ...opts, headers });
  if (res.status === 401) { logout(); throw new Error("Session expired"); }
  const data = res.status === 204 ? null : await res.json();
  if (!res.ok) throw new Error(data?.error?.message || "Request failed");
  return data;
}

/* ─── toast ─── */
function toast(msg) {
  const el = $("toast");
  $("toast-msg").textContent = msg;
  el.classList.remove("hidden");
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.add("hidden"), 2800);
}

/* ─── utils ─── */
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const short = (id) => `<td class="mono" title="${esc(id)}">${esc(String(id).slice(0, 8))}…</td>`;
const badge = (s) => `<span class="badge ${esc(s)}">${esc(s)}</span>`;
const when = (iso) => iso ? new Date(iso).toLocaleString(undefined, { month:"short", day:"numeric", hour:"2-digit", minute:"2-digit" }) : "—";

function emptyState(icon, text, hint = "") {
  return `<div class="empty-state">
    <div class="empty-state-icon">${icon}</div>
    <p class="empty-state-text">${text}</p>
    ${hint ? `<p class="empty-state-hint">${hint}</p>` : ""}
  </div>`;
}

/* ─── auth views ─── */
function showView(id) {
  for (const v of ["auth-view", "setup-view", "app-view"]) $(v).classList.add("hidden");
  $(id).classList.remove("hidden");
}

function logout() {
  localStorage.removeItem("jf_token");
  state.token = null;
  showView("auth-view");
}

async function handleAuth() {
  const email = $("auth-email").value.trim();
  const password = $("auth-password").value;
  $("auth-error").classList.add("hidden");
  try {
    if (state.registering) {
      await api("/auth/register", {
        method: "POST",
        body: JSON.stringify({ email, password, name: $("auth-name").value.trim() || email.split("@")[0] }),
      });
    }
    const data = await api("/auth/login", { method: "POST", body: JSON.stringify({ email, password }) });
    state.token = data.access_token;
    localStorage.setItem("jf_token", state.token);
    await boot();
  } catch (e) {
    const el = $("auth-error");
    el.textContent = e.message;
    el.classList.remove("hidden");
  }
}

async function boot() {
  try { state.user = await api("/auth/me"); }
  catch { return showView("auth-view"); }

  const nameEl = $("user-name");
  const avatarEl = $("user-avatar");
  if (nameEl) nameEl.textContent = state.user.name;
  if (avatarEl) avatarEl.textContent = (state.user.name || "U")[0].toUpperCase();

  const orgs = (await api("/orgs")).items;
  if (!orgs.length) return showView("setup-view");
  state.orgs = orgs;
  state.orgId = state.orgId ?? orgs[0].id;
  await loadProjects();
  showView("app-view");
  refresh();
}

async function setupFirstOrg() {
  const org = await api("/orgs", { method: "POST", body: JSON.stringify({ name: $("setup-org").value.trim() || "My Organization" }) });
  await api(`/orgs/${org.id}/projects`, { method: "POST", body: JSON.stringify({ name: $("setup-project").value.trim() || "Default Project" }) });
  await boot();
}

async function loadProjects() {
  state.projects = (await api(`/orgs/${state.orgId}/projects`)).items;
  state.projectId = state.projects[0]?.id ?? null;
  if (state.projectId) await loadQueues();
  renderSelectors();
}

async function loadQueues() {
  state.queues = state.projectId
    ? (await api(`/projects/${state.projectId}/queues`)).items : [];
  for (const selId of ["j-queue", "jf-queue", "s-queue"]) {
    const el = $(selId);
    if (el) el.innerHTML = state.queues.map(
      (q) => `<option value="${q.id}">${esc(q.name)}</option>`).join("");
  }
}

function renderSelectors() {
  $("sel-org").innerHTML = state.orgs.map(
    (o) => `<option value="${o.id}" ${o.id === state.orgId ? "selected" : ""}>${esc(o.name)}</option>`).join("");
  $("sel-project").innerHTML = state.projects.map(
    (p) => `<option value="${p.id}" ${p.id === state.projectId ? "selected" : ""}>${esc(p.name)}</option>`).join("");
}

/* ─── tabs & polling ─── */
function switchTab(name) {
  state.tab = name;
  document.querySelectorAll(".nav-item").forEach(
    (t) => t.classList.toggle("active", t.dataset.tab === name));
  document.querySelectorAll(".tab-panel").forEach(
    (p) => p.classList.toggle("hidden", p.id !== "tab-" + name));
  const titleEl = $("topbar-title");
  if (titleEl) titleEl.textContent = TAB_LABELS[name] || name;
  refresh();
}

async function refresh() {
  if (!state.projectId || $("app-view").classList.contains("hidden")) return;
  try {
    if (state.tab === "overview") await renderOverview();
    else if (state.tab === "queues") await renderQueues();
    else if (state.tab === "jobs") await renderJobs();
    else if (state.tab === "schedules") await renderSchedules();
    else if (state.tab === "workers") await renderWorkers();
    else if (state.tab === "dlq") await renderDlq();
  } catch (e) { console.error(e); }
}
setInterval(refresh, 3000);

/* ─── overview ─── */
const STAT_DEFS = [
  { key: "queued_pending", label: "Pending",
    icon: `<svg viewBox="0 0 20 20" fill="none" width="18" height="18"><path d="M3 5h14M3 10h14M3 15h10" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>` },
  { key: "active", label: "Active",
    icon: `<svg viewBox="0 0 20 20" fill="none" width="18" height="18"><path d="M10 4v6l4 2" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/><circle cx="10" cy="10" r="7" stroke="currentColor" stroke-width="1.8"/></svg>` },
  { key: "completed", label: "Completed",
    icon: `<svg viewBox="0 0 20 20" fill="none" width="18" height="18"><path d="M4 10l4.5 4.5L16 6" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>` },
  { key: "dead", label: "Dead",
    icon: `<svg viewBox="0 0 20 20" fill="none" width="18" height="18"><circle cx="10" cy="10" r="7" stroke="currentColor" stroke-width="1.8"/><path d="M10 6v4M10 13v1" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>` },
  { key: "online_workers", label: "Workers Online",
    icon: `<svg viewBox="0 0 20 20" fill="none" width="18" height="18"><circle cx="7" cy="7" r="3" stroke="currentColor" stroke-width="1.8"/><path d="M1 17c0-2.761 2.686-5 6-5s6 2.239 6 5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/><path d="M14 7a2 2 0 010 4M16 15c0-1.5-1-3-2-3.5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>` },
];

async function renderOverview() {
  const m = await api(`/projects/${state.projectId}/metrics/overview`);
  const c = m.counts;
  const vals = {
    queued_pending: c.queued + c.scheduled,
    active: c.claimed + c.running,
    completed: c.completed,
    dead: c.dead,
    online_workers: m.online_workers,
  };
  $("stat-cards").innerHTML = STAT_DEFS.map((d) => `
    <div class="stat-card">
      <div class="stat-icon">${d.icon}</div>
      <div class="stat-num">${vals[d.key] ?? 0}</div>
      <div class="stat-label">${d.label}</div>
    </div>`).join("");

  drawChart($("chart"), m.throughput_per_min);

  const maxDepth = Math.max(1, ...m.queues.map((q) => q.depth));
  $("queue-health").innerHTML = m.queues.length
    ? `<div class="qh-list">${m.queues.map((q) => `
      <div class="qh-row">
        <span class="qh-name">${esc(q.name)} ${q.paused ? badge("paused") : ""}</span>
        <div class="qh-track"><div class="qh-fill" style="width:${Math.max(4, (q.depth / maxDepth) * 100)}%"></div></div>
        <span class="qh-count">${q.depth} pending</span>
      </div>`).join("")}</div>`
    : emptyState(
        `<svg viewBox="0 0 24 24" fill="none" width="22" height="22"><path d="M4 6h16M4 12h16M4 18h10" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>`,
        "No queues yet", "Create a queue in the Queues tab to get started.");
}

function drawChart(canvas, data) {
  const dpr = window.devicePixelRatio || 1;
  const W = canvas.clientWidth;
  const H = 120;
  canvas.width = W * dpr;
  canvas.height = H * dpr;
  canvas.style.height = H + "px";
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, W, H);

  const max = Math.max(1, ...data);
  const bw = W / data.length;
  const pad = 20;

  /* subtle grid lines in purple */
  ctx.strokeStyle = "rgba(79,3,65,0.25)";
  ctx.lineWidth = 1;
  for (let i = 0; i <= 4; i++) {
    const y = pad + ((H - pad * 2) / 4) * i;
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke();
  }

  /* bars */
  data.forEach((v, i) => {
    const h = (v / max) * (H - pad * 2);
    const x = i * bw + 2;
    const y = H - pad - h;
    const bWidth = bw - 4;
    const bHeight = Math.max(h, 2);

    if (v) {
      const grad = ctx.createLinearGradient(0, y, 0, H - pad);
      grad.addColorStop(0, "rgba(166,25,138,0.9)");
      grad.addColorStop(1, "rgba(79,3,65,0.95)");
      ctx.fillStyle = grad;
    } else {
      ctx.fillStyle = "rgba(79,3,65,0.12)";
    }
    const r = Math.min(3, bWidth / 2);
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.lineTo(x + bWidth - r, y);
    ctx.quadraticCurveTo(x + bWidth, y, x + bWidth, y + r);
    ctx.lineTo(x + bWidth, y + bHeight);
    ctx.lineTo(x, y + bHeight);
    ctx.lineTo(x, y + r);
    ctx.quadraticCurveTo(x, y, x + r, y);
    ctx.fill();
  });

  ctx.fillStyle = "rgba(30,0,25,0.55)";
  ctx.font = "11px Inter, sans-serif";
  ctx.fillText("30 min ago", 4, H - 5);
  ctx.textAlign = "center";
  ctx.fillText("max " + max + " / min", W / 2, H - 5);
  ctx.textAlign = "right";
  ctx.fillText("now", W - 4, H - 5);
  ctx.textAlign = "left";
}

/* ─── queues ─── */
async function renderQueues() {
  await loadQueues();
  if (!state.queues.length) {
    $("queues-table").innerHTML = emptyState(
      `<svg viewBox="0 0 24 24" fill="none" width="22" height="22"><path d="M4 6h16M4 12h16M4 18h10" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>`,
      "No queues yet", "Use the form above to create your first queue.");
    return;
  }
  const rows = await Promise.all(state.queues.map(async (q) => {
    const s = await api(`/queues/${q.id}/stats`);
    return `<tr>
      <td><span style="font-weight:500">${esc(q.name)}</span></td>
      <td>${q.priority}</td>
      <td>${q.concurrency_limit}</td>
      <td>${q.paused ? badge("paused") : badge("online")}</td>
      <td>${s.depth}</td>
      <td>${s.active}</td>
      <td>${s.counts.completed}</td>
      <td>${s.counts.dead > 0 ? `<span style="color:var(--red);font-weight:600">${s.counts.dead}</span>` : s.counts.dead}</td>
      <td>${s.avg_duration_s != null ? s.avg_duration_s + "s" : "—"}</td>
      <td>
        <button class="btn ${q.paused ? "success" : "ghost"} small-btn" onclick="togglePause(${q.id}, ${q.paused})">
          ${q.paused ? "Resume" : "Pause"}
        </button>
      </td>
    </tr>`;
  }));
  $("queues-table").innerHTML = `<div class="table-wrap"><table>
    <thead><tr>
      <th>Name</th><th>Priority</th><th>Concurrency</th><th>State</th>
      <th>Pending</th><th>Active</th><th>Done</th><th>Dead</th><th>Avg time</th><th></th>
    </tr></thead>
    <tbody>${rows.join("")}</tbody>
  </table></div>`;
}

window.togglePause = async (id, paused) => {
  await api(`/queues/${id}/${paused ? "resume" : "pause"}`, { method: "POST" });
  toast(paused ? "Queue resumed" : "Queue paused");
  renderQueues();
};

async function createQueue() {
  try {
    await api(`/projects/${state.projectId}/queues`, {
      method: "POST",
      body: JSON.stringify({
        name: $("q-name").value.trim(),
        priority: +$("q-priority").value,
        concurrency_limit: +$("q-concurrency").value,
      }),
    });
    $("q-name").value = "";
    toast("Queue created");
    renderQueues();
  } catch (e) { toast(e.message); }
}

/* ─── jobs ─── */
async function renderJobs() {
  if (!$("jf-queue").value) await loadQueues();
  const qid = $("jf-queue").value;
  if (!qid) {
    $("jobs-table").innerHTML = emptyState(
      `<svg viewBox="0 0 24 24" fill="none" width="22" height="22"><rect x="3" y="3" width="18" height="18" rx="2" stroke="currentColor" stroke-width="1.8"/><path d="M8 12l3 3 5-5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
      "Create a queue first", "Jobs need a queue to live in.");
    return;
  }
  const status = $("jf-status").value;
  const params = new URLSearchParams({ limit: 15, offset: state.jobsOffset });
  if (status) params.set("status", status);
  const data = await api(`/queues/${qid}/jobs?` + params);
  state.jobsTotal = data.total;
  const pageEl = $("jobs-page");
  if (pageEl) pageEl.textContent =
    `${data.total ? state.jobsOffset + 1 : 0}–${Math.min(state.jobsOffset + 15, data.total)} of ${data.total}`;
  if (!data.items.length) {
    $("jobs-table").innerHTML = emptyState(
      `<svg viewBox="0 0 24 24" fill="none" width="22" height="22"><rect x="3" y="3" width="18" height="18" rx="2" stroke="currentColor" stroke-width="1.8"/><path d="M8 12l3 3 5-5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
      "No jobs found", status ? `No jobs with status "${status}".` : "Enqueue your first job above.");
    return;
  }
  $("jobs-table").innerHTML = `<div class="table-wrap"><table>
    <thead><tr>
      <th>ID</th><th>Type</th><th>Status</th><th>Priority</th>
      <th>Attempts</th><th>Run at</th><th>Finished</th>
    </tr></thead>
    <tbody>${data.items.map((j) => `<tr class="clickable" onclick="openJob('${j.id}')">
      ${short(j.id)}
      <td><code style="font-size:12.5px;color:var(--text-2)">${esc(j.type)}</code></td>
      <td>${badge(j.status)}</td>
      <td>${j.priority}</td>
      <td><span style="font-size:12.5px">${j.attempts}/${j.max_attempts}</span></td>
      <td style="font-size:12.5px;color:var(--text-2)">${when(j.run_at)}</td>
      <td style="font-size:12.5px;color:var(--text-2)">${when(j.finished_at)}</td>
    </tr>`).join("")}</tbody>
  </table></div>`;
}

async function createJob() {
  try {
    const payload = JSON.parse($("j-payload").value || "{}");
    const delay = +$("j-delay").value;
    await api(`/queues/${$("j-queue").value}/jobs`, {
      method: "POST",
      body: JSON.stringify({
        type: $("j-type").value, payload,
        priority: +$("j-priority").value,
        ...(delay > 0 ? { delay_s: delay } : {}),
      }),
    });
    toast("Job enqueued");
    renderJobs();
  } catch (e) { toast(e.message); }
}

/* ─── job drawer ─── */
window.openJob = async (id) => {
  const j = await api(`/jobs/${id}`);
  const actions = [];
  if (["queued", "scheduled"].includes(j.status))
    actions.push(`<button class="btn danger" onclick="jobAction('${j.id}','cancel')">Cancel Job</button>`);
  if (["dead", "cancelled", "completed"].includes(j.status))
    actions.push(`<button class="btn primary" onclick="jobAction('${j.id}','retry')">Retry Job</button>`);

  $("drawer-body").innerHTML = `
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:16px">
      ${badge(j.status)}
      <code style="font-size:12px;color:var(--muted)">${esc(j.id)}</code>
    </div>
    <div class="drawer-meta">
      <div class="drawer-meta-item">
        <div class="drawer-meta-key">Type</div>
        <div class="drawer-meta-val">${esc(j.type)}</div>
      </div>
      <div class="drawer-meta-item">
        <div class="drawer-meta-key">Attempts</div>
        <div class="drawer-meta-val">${j.attempts} / ${j.max_attempts}</div>
      </div>
      <div class="drawer-meta-item">
        <div class="drawer-meta-key">Priority</div>
        <div class="drawer-meta-val">${j.priority}</div>
      </div>
      <div class="drawer-meta-item">
        <div class="drawer-meta-key">Worker</div>
        <div class="drawer-meta-val">${j.claimed_by ? esc(j.claimed_by.slice(0, 8)) + "…" : "—"}</div>
      </div>
    </div>
    ${actions.length ? `<div class="drawer-actions">${actions.join("")}</div>` : ""}
    <div class="drawer-section-label">Payload</div>
    <pre>${esc(JSON.stringify(j.payload, null, 2))}</pre>
    ${j.result ? `<div class="drawer-section-label">Result</div><pre>${esc(JSON.stringify(j.result, null, 2))}</pre>` : ""}
    ${j.last_error ? `<div class="drawer-section-label">Last Error</div><pre style="color:var(--red)">${esc(j.last_error)}</pre>` : ""}
    <div class="drawer-section-label">Executions (${j.executions.length})</div>
    <div class="table-wrap"><table>
      <thead><tr><th>#</th><th>Status</th><th>Started</th><th>Finished</th></tr></thead>
      <tbody>${j.executions.map((e) => `<tr>
        <td style="color:var(--muted);font-size:12.5px">#${e.attempt}</td>
        <td>${badge(e.status)}</td>
        <td style="font-size:12px;color:var(--text-2)">${when(e.started_at)}</td>
        <td style="font-size:12px;color:var(--text-2)">${when(e.finished_at)}</td>
      </tr>`).join("")}</tbody>
    </table></div>
    <div class="drawer-section-label">Logs</div>
    <div style="background:var(--bg-2);border:1px solid var(--border);border-radius:var(--radius-sm);padding:12px 14px">
      ${j.logs.length
        ? j.logs.map((l) => `<div class="log-line">
            <span class="lvl-${esc(l.level)}">[${esc(l.level).toUpperCase()}]</span>
            <span class="log-ts">${when(l.ts)}</span>
            <span>${esc(l.message)}</span>
          </div>`).join("")
        : `<p style="font-size:12.5px;color:var(--muted)">No logs recorded.</p>`}
    </div>`;

  $("drawer").classList.remove("hidden");
};

window.jobAction = async (id, action) => {
  try {
    await api(`/jobs/${id}/${action}`, { method: "POST" });
    toast(action === "cancel" ? "Job cancelled" : "Job requeued");
    $("drawer").classList.add("hidden");
    renderJobs();
  } catch (e) { toast(e.message); }
};

/* ─── schedules ─── */
async function renderSchedules() {
  await loadQueues();
  const lists = await Promise.all(state.queues.map(async (q) => {
    const items = (await api(`/queues/${q.id}/schedules`)).items;
    return items.map((s) => ({ ...s, queueName: q.name }));
  }));
  const all = lists.flat();
  if (!all.length) {
    $("schedules-table").innerHTML = emptyState(
      `<svg viewBox="0 0 24 24" fill="none" width="22" height="22"><circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="1.8"/><path d="M12 7v5l3 3" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
      "No schedules yet", "Create a cron-based recurring job using the form above.");
    return;
  }
  $("schedules-table").innerHTML = `<div class="table-wrap"><table>
    <thead><tr>
      <th>Name</th><th>Queue</th><th>Cron</th><th>Type</th><th>Next run</th><th>State</th><th>Actions</th>
    </tr></thead>
    <tbody>${all.map((s) => `<tr>
      <td style="font-weight:500">${esc(s.name)}</td>
      <td style="color:var(--text-2)">${esc(s.queueName)}</td>
      <td><code style="font-size:12px;color:var(--accent-light)">${esc(s.cron_expr)}</code></td>
      <td style="font-size:12.5px;color:var(--text-2)">${esc(s.job_type)}</td>
      <td style="font-size:12.5px;color:var(--text-2)">${when(s.next_run_at)}</td>
      <td>${s.enabled ? badge("online") : badge("paused")}</td>
      <td>
        <div style="display:flex;gap:6px">
          <button class="btn ghost small-btn" onclick="toggleSchedule(${s.id}, ${s.enabled})">
            ${s.enabled ? "Disable" : "Enable"}
          </button>
          <button class="btn danger small-btn" onclick="deleteSchedule(${s.id})">Delete</button>
        </div>
      </td>
    </tr>`).join("")}</tbody>
  </table></div>`;
}

async function createSchedule() {
  try {
    await api(`/queues/${$("s-queue").value}/schedules`, {
      method: "POST",
      body: JSON.stringify({
        name: $("s-name").value.trim(),
        cron_expr: $("s-cron").value.trim(),
        job_type: $("s-type").value,
        payload: { subject: "scheduled run" },
      }),
    });
    toast("Schedule created");
    renderSchedules();
  } catch (e) { toast(e.message); }
}

window.toggleSchedule = async (id, enabled) => {
  await api(`/schedules/${id}`, { method: "PATCH", body: JSON.stringify({ enabled: !enabled }) });
  renderSchedules();
};
window.deleteSchedule = async (id) => {
  await api(`/schedules/${id}`, { method: "DELETE" });
  toast("Schedule deleted");
  renderSchedules();
};

/* ─── workers ─── */
async function renderWorkers() {
  const items = (await api("/workers")).items;
  if (!items.length) {
    $("workers-table").innerHTML = emptyState(
      `<svg viewBox="0 0 24 24" fill="none" width="22" height="22"><circle cx="9" cy="7" r="4" stroke="currentColor" stroke-width="1.8"/><path d="M3 21v-2a4 4 0 014-4h4a4 4 0 014 4v2" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/><path d="M16 3.13a4 4 0 010 7.75M21 21v-2a4 4 0 00-3-3.87" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>`,
      "No workers registered", `Start a worker with: <code style="font-size:11.5px;color:var(--accent-light)">python -m worker.main</code>`);
    return;
  }
  $("workers-table").innerHTML = `<div class="table-wrap"><table>
    <thead><tr>
      <th>ID</th><th>Hostname</th><th>PID</th><th>Concurrency</th>
      <th>Running</th><th>Status</th><th>Last heartbeat</th>
    </tr></thead>
    <tbody>${items.map((w) => `<tr>
      ${short(w.id)}
      <td style="font-weight:500">${esc(w.hostname)}</td>
      <td style="color:var(--text-2);font-size:12.5px">${w.pid}</td>
      <td>${w.concurrency}</td>
      <td>${w.running_jobs > 0 ? `<span style="color:var(--amber);font-weight:600">${w.running_jobs}</span>` : w.running_jobs}</td>
      <td>${badge(w.status)}</td>
      <td style="font-size:12.5px;color:var(--muted)">${w.heartbeat_age_s}s ago</td>
    </tr>`).join("")}</tbody>
  </table></div>`;
}

/* ─── dlq ─── */
async function renderDlq() {
  const data = await api(`/projects/${state.projectId}/dlq?limit=50`);
  const countBadge = $("dlq-count");
  if (countBadge) {
    countBadge.textContent = data.items.length;
    countBadge.classList.toggle("visible", data.items.length > 0);
  }
  if (!data.items.length) {
    $("dlq-table").innerHTML = emptyState(
      `<svg viewBox="0 0 24 24" fill="none" width="22" height="22"><path d="M12 3l9 18H3L12 3z" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/><path d="M12 9v5M12 16.5v.5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>`,
      "Dead letter queue is empty", "All jobs are processing normally. 🎉");
    return;
  }
  $("dlq-table").innerHTML = `<div class="table-wrap"><table>
    <thead><tr>
      <th>Job ID</th><th>Type</th><th>Attempts</th><th>Last error</th><th>Failed at</th><th>Action</th>
    </tr></thead>
    <tbody>${data.items.map((d) => `<tr>
      ${short(d.job_id)}
      <td><code style="font-size:12.5px;color:var(--text-2)">${esc(d.job_type)}</code></td>
      <td>${d.attempts}</td>
      <td style="font-size:12.5px;color:var(--red);max-width:240px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc((d.last_error || "").slice(0, 80))}</td>
      <td style="font-size:12.5px;color:var(--muted)">${when(d.failed_at)}</td>
      <td>${d.requeued_at
        ? `<span style="font-size:12px;color:var(--muted)">Requeued</span>`
        : `<button class="btn primary small-btn" onclick="retryDlq(${d.id})">Retry</button>`}</td>
    </tr>`).join("")}</tbody>
  </table></div>`;
}

window.retryDlq = async (id) => {
  try {
    await api(`/dlq/${id}/retry`, { method: "POST" });
    toast("Job requeued from DLQ");
    renderDlq();
  } catch (e) { toast(e.message); }
};

/* ─── wiring ─── */
$("auth-submit").onclick = handleAuth;
$("auth-password").addEventListener("keydown", (e) => e.key === "Enter" && handleAuth());

$("auth-toggle").onclick = (e) => {
  e.preventDefault();
  state.registering = !state.registering;
  const nameField = $("field-name");
  if (nameField) nameField.classList.toggle("hidden", !state.registering);
  $("auth-submit").textContent = state.registering ? "Create account" : "Sign in";
  $("auth-toggle-label").textContent = state.registering ? "Already have an account?" : "Don't have an account?";
  $("auth-toggle").textContent = state.registering ? "Sign in" : "Create one";
};

$("setup-submit").onclick = setupFirstOrg;
$("btn-logout").onclick = logout;

$("sel-org").onchange = async (e) => {
  state.orgId = +e.target.value;
  await loadProjects();
  refresh();
};
$("sel-project").onchange = async (e) => {
  state.projectId = +e.target.value;
  await loadQueues();
  refresh();
};

document.querySelectorAll(".nav-item").forEach((t) => t.onclick = () => switchTab(t.dataset.tab));

$("q-create").onclick = createQueue;
$("j-create").onclick = createJob;
$("s-create").onclick = createSchedule;

$("jf-queue").onchange = $("jf-status").onchange = () => { state.jobsOffset = 0; renderJobs(); };
$("jobs-prev").onclick = () => { state.jobsOffset = Math.max(0, state.jobsOffset - 15); renderJobs(); };
$("jobs-next").onclick = () => {
  if (state.jobsOffset + 15 < state.jobsTotal) { state.jobsOffset += 15; renderJobs(); }
};

$("drawer-close").onclick = () => $("drawer").classList.add("hidden");
$("drawer-backdrop").onclick = () => $("drawer").classList.add("hidden");

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") $("drawer").classList.add("hidden");
});

/* ─── boot ─── */
if (state.token) boot(); else showView("auth-view");
