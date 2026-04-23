// Dashboard: fetch /api/summary and render KPIs + tables + Chart.js charts.
// Basic auth is handled by the browser after the initial 401 challenge
// on the /api/summary request (same realm as the HTML page).
//
// Filter state (date range, sources, users) lives in the URL query string
// so the view is shareable. Every Apply re-fetches with those params and
// fully re-renders; we destroy previous Chart instances first because
// Chart.js won't attach a second chart to the same canvas otherwise.

const fmt = new Intl.NumberFormat();
const fmtCompact = new Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 1 });
const ALL_SOURCES = ["ai_web", "code", "desktop"];
const AUTO_REFRESH_MS = 60_000;

// Chart handles so we can destroy on re-render.
let chartBySource = null;
let chartDaily = null;

// Last response kept in memory for CSV export.
let lastData = null;
let autoRefreshTimer = null;
let autoRefreshPaused = false;

// ---------------------------------------------------------------------------
// Theme toggle with localStorage persistence
// ---------------------------------------------------------------------------
function currentTheme() {
  return document.documentElement.getAttribute("data-theme") || "light";
}
function setTheme(t) {
  document.documentElement.setAttribute("data-theme", t);
  localStorage.setItem("theme", t);
  updateThemeIcon(t);
  // Re-render charts so gridline/text colors pick up the new theme.
  if (lastData) renderCharts(lastData, readFilterFromUI().sources);
}
function updateThemeIcon(t) {
  const icon = document.getElementById("icon-theme");
  if (!icon) return;
  // Moon icon for light mode (click to go dark), sun for dark.
  icon.innerHTML = t === "dark"
    ? '<circle cx="12" cy="12" r="5"></circle><line x1="12" y1="1" x2="12" y2="3"></line><line x1="12" y1="21" x2="12" y2="23"></line><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"></line><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"></line><line x1="1" y1="12" x2="3" y2="12"></line><line x1="21" y1="12" x2="23" y2="12"></line><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"></line><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"></line>'
    : '<path d="M21 12.79A9 9 0 1111.21 3 7 7 0 0021 12.79z"></path>';
}

// ---------------------------------------------------------------------------
// Fetch + loading overlay
// ---------------------------------------------------------------------------
function setLoading(on) {
  document.getElementById("loading").classList.toggle("active", on);
}

async function fetchSummary(params) {
  const qs = new URLSearchParams();
  if (params.start) qs.set("start", params.start);
  if (params.end) qs.set("end", params.end);
  if (params.sources && params.sources.length) qs.set("source", params.sources.join(","));
  if (params.users && params.users.length) qs.set("user", params.users.join(","));
  const url = "/api/summary" + (qs.toString() ? "?" + qs.toString() : "");
  const r = await fetch(url, { credentials: "include" });
  if (!r.ok) throw new Error("fetch failed: " + r.status);
  return r.json();
}

function escapeHtml(s) {
  return (s ?? "").toString().replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[c]));
}

// ---------------------------------------------------------------------------
// KPIs
// ---------------------------------------------------------------------------
function renderKPIs(data) {
  const sum = (rows, key) => rows.reduce((acc, r) => acc + (r[key] || 0), 0);
  const todayOut = sum(data.leaderboard.today, "out_tok");
  const weekOut = sum(data.leaderboard.week, "out_tok");
  const allMsgs = sum(data.leaderboard.all, "n");

  const todayUsers = data.leaderboard.today.length;
  const topToday = data.leaderboard.today[0];
  const topAll = data.leaderboard.all[0];

  setKpi("kpi-today", todayOut, topToday ? `top: ${topToday.user} · ${fmtCompact.format(topToday.out_tok)}` : "no activity yet");
  setKpi("kpi-week", weekOut, `${data.leaderboard.week.length} active user${data.leaderboard.week.length === 1 ? "" : "s"}`);
  setKpi("kpi-messages", allMsgs, topAll ? `all-time leader: ${topAll.user}` : "no data");
  setKpi("kpi-users", todayUsers, `${data.leaderboard.all.length} total ever seen`);
}
function setKpi(id, value, sub) {
  document.getElementById(id).textContent = fmtCompact.format(value || 0);
  const subEl = document.getElementById(id + "-sub");
  if (subEl) subEl.textContent = sub || "";
}

// ---------------------------------------------------------------------------
// Leaderboards with rank badges
// ---------------------------------------------------------------------------
function fillLeaderboard(tbodyId, rows) {
  const tbody = document.querySelector(`#${tbodyId} tbody`);
  tbody.innerHTML = "";
  rows.forEach((r, i) => {
    const rankCls = i < 3 ? `rank r${i + 1}` : "rank";
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td><span class="${rankCls}">${i + 1}</span><span class="user-cell">${escapeHtml(r.user)}</span></td>
      <td class="num">${fmt.format(r.n)}</td>
      <td class="num">${fmt.format(r.out_tok ?? 0)}</td>`;
    tbody.appendChild(tr);
  });
  if (!rows.length) {
    tbody.innerHTML = `<tr><td colspan="3" class="muted">No data</td></tr>`;
  }
}

// ---------------------------------------------------------------------------
// Raw totals table
// ---------------------------------------------------------------------------
function renderRawTotals(perUser, sourcesFilter) {
  const tbody = document.querySelector("#raw-totals tbody");
  tbody.innerHTML = "";
  const sources = sourcesFilter && sourcesFilter.length ? sourcesFilter : ALL_SOURCES;
  const users = Object.keys(perUser).sort();
  for (const u of users) {
    for (const s of sources) {
      const b = (perUser[u].by_source || {})[s];
      if (!b) continue;
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td class="user-cell">${escapeHtml(u)}</td>
        <td>${escapeHtml(s)}</td>
        <td class="num">${fmt.format(b.messages)}</td>
        <td class="num">${fmt.format(b.input_tokens)}</td>
        <td class="num">${fmt.format(b.output_tokens)}</td>
        <td class="num">${fmt.format(b.cache_creation_tokens)}</td>
        <td class="num">${fmt.format(b.cache_read_tokens)}</td>
        <td class="num">${fmt.format(b.session_starts)}</td>
        <td class="num">${fmt.format(b.session_ends)}</td>`;
      tbody.appendChild(tr);
    }
  }
  if (!tbody.children.length) {
    tbody.innerHTML = `<tr><td colspan="9" class="muted">No data</td></tr>`;
  }
}

// ---------------------------------------------------------------------------
// Charts
// ---------------------------------------------------------------------------
function chartColors() {
  const css = getComputedStyle(document.documentElement);
  return {
    text: css.getPropertyValue("--text-muted").trim() || "#64748b",
    grid: css.getPropertyValue("--border").trim() || "#e6e6e0",
  };
}

function renderCharts(data, sourcesFilter) {
  renderBySourceChart(data.per_user, sourcesFilter);
  renderDailyChart(data.time_series_daily);
}

function renderBySourceChart(perUser, sourcesFilter) {
  const ctx = document.getElementById("chart-by-source");
  const users = Object.keys(perUser).sort();
  const sources = sourcesFilter && sourcesFilter.length ? sourcesFilter : ALL_SOURCES;
  const colors = { ai_web: "#f59e0b", code: "#3b82f6", desktop: "#10b981" };
  const datasets = sources.map((s) => ({
    label: s,
    backgroundColor: colors[s] || "#94a3b8",
    borderRadius: 4,
    data: users.map((u) => ((perUser[u].by_source || {})[s]?.output_tokens) || 0),
  }));
  const c = chartColors();
  if (chartBySource) chartBySource.destroy();
  chartBySource = new Chart(ctx, {
    type: "bar",
    data: { labels: users, datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { position: "top", labels: { color: c.text, boxWidth: 12, boxHeight: 12 } },
        tooltip: {
          callbacks: {
            label: (ctx) => `${ctx.dataset.label}: ${fmt.format(ctx.parsed.y)} out tokens`,
          },
        },
      },
      scales: {
        x: { stacked: true, ticks: { color: c.text }, grid: { color: c.grid, display: false } },
        y: {
          stacked: true,
          ticks: { color: c.text, callback: (v) => fmtCompact.format(v) },
          grid: { color: c.grid },
          title: { display: true, text: "Output tokens", color: c.text },
        },
      },
    },
  });
}

function renderDailyChart(series) {
  const ctx = document.getElementById("chart-daily");
  const byUser = {};
  const allDays = new Set();
  for (const row of series) {
    (byUser[row.user] ||= {})[row.day] = row.out_tok;
    allDays.add(row.day);
  }
  const days = [...allDays].sort((a, b) => a - b);
  const labels = days.map((d) => new Date(d * 86400 * 1000).toISOString().slice(0, 10));
  const palette = ["#3b82f6", "#10b981", "#f59e0b", "#ec4899", "#8b5cf6", "#06b6d4", "#ef4444"];
  const datasets = Object.keys(byUser).sort().map((u, i) => ({
    label: u,
    borderColor: palette[i % palette.length],
    backgroundColor: palette[i % palette.length] + "20",
    data: days.map((d) => byUser[u][d] || 0),
    tension: 0.3,
    fill: false,
    pointRadius: 2,
    pointHoverRadius: 5,
    borderWidth: 2,
  }));
  const c = chartColors();
  if (chartDaily) chartDaily.destroy();
  chartDaily = new Chart(ctx, {
    type: "line",
    data: { labels, datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { position: "top", labels: { color: c.text, boxWidth: 12 } },
        tooltip: {
          callbacks: {
            label: (ctx) => `${ctx.dataset.label}: ${fmt.format(ctx.parsed.y)}`,
          },
        },
      },
      scales: {
        x: { ticks: { color: c.text, maxRotation: 0 }, grid: { color: c.grid, display: false } },
        y: {
          ticks: { color: c.text, callback: (v) => fmtCompact.format(v) },
          grid: { color: c.grid },
          title: { display: true, text: "Output tokens", color: c.text },
        },
      },
    },
  });
}

// ---------------------------------------------------------------------------
// Filter UI (state <-> URL, reactive)
// ---------------------------------------------------------------------------
function readFilterFromUI() {
  const start = document.getElementById("f-start").value;
  const end = document.getElementById("f-end").value;
  const sources = [...document.querySelectorAll("#f-sources input:checked")].map((el) => el.value);
  const users = [...document.getElementById("f-users").selectedOptions].map((o) => o.value);
  return {
    start: start ? Math.floor(new Date(start + "T00:00:00").getTime() / 1000) : null,
    end: end ? Math.floor(new Date(end + "T23:59:59").getTime() / 1000) : null,
    sources,
    users,
  };
}

function writeFilterToUrl(f) {
  const qs = new URLSearchParams();
  if (f.start) qs.set("start", f.start);
  if (f.end) qs.set("end", f.end);
  if (f.sources.length && f.sources.length !== ALL_SOURCES.length)
    qs.set("source", f.sources.join(","));
  if (f.users.length) qs.set("user", f.users.join(","));
  const q = qs.toString();
  history.replaceState(null, "", q ? "?" + q : window.location.pathname);
}

function applyFilterFromUrl() {
  const qs = new URLSearchParams(window.location.search);
  const start = qs.get("start");
  const end = qs.get("end");
  if (start) {
    document.getElementById("f-start").value = new Date(Number(start) * 1000)
      .toISOString().slice(0, 10);
  }
  if (end) {
    document.getElementById("f-end").value = new Date(Number(end) * 1000)
      .toISOString().slice(0, 10);
  }
  const srcParam = qs.get("source");
  if (srcParam) {
    const wanted = new Set(srcParam.split(",").filter(Boolean));
    document.querySelectorAll("#f-sources input").forEach((el) => {
      el.checked = wanted.has(el.value);
    });
  }
}

function populateUserSelect(knownUsers) {
  const sel = document.getElementById("f-users");
  const qs = new URLSearchParams(window.location.search);
  const wanted = new Set((qs.get("user") || "").split(",").filter(Boolean));
  const current = new Set([...sel.selectedOptions].map((o) => o.value));
  sel.innerHTML = "";
  for (const u of knownUsers) {
    const opt = document.createElement("option");
    opt.value = u;
    opt.textContent = u;
    if (wanted.has(u) || current.has(u)) opt.selected = true;
    sel.appendChild(opt);
  }
}

function resetFilter() {
  document.getElementById("f-start").value = "";
  document.getElementById("f-end").value = "";
  document.querySelectorAll("#f-sources input").forEach((el) => (el.checked = true));
  [...document.getElementById("f-users").options].forEach((o) => (o.selected = false));
  history.replaceState(null, "", window.location.pathname);
  refresh();
}

// ---------------------------------------------------------------------------
// CSV export
// ---------------------------------------------------------------------------
function exportCSV() {
  if (!lastData) return;
  const perUser = lastData.per_user || {};
  const header = [
    "user", "source", "messages", "input_tokens", "output_tokens",
    "cache_creation_tokens", "cache_read_tokens", "session_starts", "session_ends",
  ];
  const rows = [header.join(",")];
  for (const u of Object.keys(perUser).sort()) {
    const bs = perUser[u].by_source || {};
    for (const s of Object.keys(bs).sort()) {
      const b = bs[s];
      rows.push([
        csvEscape(u), csvEscape(s), b.messages, b.input_tokens, b.output_tokens,
        b.cache_creation_tokens, b.cache_read_tokens, b.session_starts, b.session_ends,
      ].join(","));
    }
  }
  const blob = new Blob([rows.join("\n")], { type: "text/csv" });
  const a = document.createElement("a");
  const ts = new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-");
  a.href = URL.createObjectURL(blob);
  a.download = `claude-usage-${ts}.csv`;
  a.click();
  URL.revokeObjectURL(a.href);
}
function csvEscape(v) {
  const s = String(v ?? "");
  return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

// ---------------------------------------------------------------------------
// Auto-refresh
// ---------------------------------------------------------------------------
function startAutoRefresh() {
  stopAutoRefresh();
  autoRefreshTimer = setInterval(() => { if (!autoRefreshPaused) refresh({ silent: true }); }, AUTO_REFRESH_MS);
}
function stopAutoRefresh() {
  if (autoRefreshTimer) { clearInterval(autoRefreshTimer); autoRefreshTimer = null; }
}
function togglePause() {
  autoRefreshPaused = !autoRefreshPaused;
  const pill = document.getElementById("live-pill");
  const label = document.getElementById("live-label");
  const btn = document.getElementById("btn-pause");
  if (autoRefreshPaused) {
    pill.classList.remove("live"); pill.classList.add("paused");
    label.textContent = "paused";
    btn.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="5 3 19 12 5 21 5 3"></polygon></svg> Resume`;
  } else {
    pill.classList.remove("paused"); pill.classList.add("live");
    label.textContent = "live";
    btn.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="6" y="4" width="4" height="16"></rect><rect x="14" y="4" width="4" height="16"></rect></svg> Pause`;
  }
}

// ---------------------------------------------------------------------------
// Main refresh
// ---------------------------------------------------------------------------
async function refresh(opts) {
  const silent = opts && opts.silent;
  const f = readFilterFromUI();
  writeFilterToUrl(f);
  if (!silent) setLoading(true);
  try {
    const data = await fetchSummary(f);
    lastData = data;
    document.getElementById("generated-at").textContent =
      "Updated " + new Date(data.generated_at * 1000).toLocaleTimeString();
    populateUserSelect(data.known_users || []);
    renderKPIs(data);
    fillLeaderboard("lb-today", data.leaderboard.today);
    fillLeaderboard("lb-week", data.leaderboard.week);
    fillLeaderboard("lb-all", data.leaderboard.all);
    renderRawTotals(data.per_user, f.sources);
    renderCharts(data, f.sources);
  } catch (e) {
    // Don't stack error banners on silent re-polls.
    if (!silent && !document.querySelector(".error-banner")) {
      document.body.insertAdjacentHTML(
        "afterbegin",
        `<div class="error-banner">Failed to load dashboard: ${escapeHtml(e.message)}</div>`,
      );
    }
  } finally {
    if (!silent) setLoading(false);
  }
}

// ---------------------------------------------------------------------------
// Bootstrap
// ---------------------------------------------------------------------------
(function main() {
  applyFilterFromUrl();
  updateThemeIcon(currentTheme());

  document.getElementById("f-apply").addEventListener("click", () => refresh());
  document.getElementById("f-reset").addEventListener("click", resetFilter);
  document.querySelectorAll("#f-sources input").forEach((el) =>
    el.addEventListener("change", () => refresh()),
  );
  document.getElementById("f-start").addEventListener("change", () => refresh());
  document.getElementById("f-end").addEventListener("change", () => refresh());
  document.getElementById("f-users").addEventListener("change", () => refresh());

  document.getElementById("btn-refresh").addEventListener("click", () => refresh());
  document.getElementById("btn-pause").addEventListener("click", togglePause);
  document.getElementById("btn-theme").addEventListener("click", () => {
    setTheme(currentTheme() === "dark" ? "light" : "dark");
  });
  document.getElementById("btn-export").addEventListener("click", exportCSV);

  refresh();
  startAutoRefresh();
})();
