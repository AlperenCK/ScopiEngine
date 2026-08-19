(() => {
  "use strict";

  // Everything before "/_ui/" in this page's own URL — "" when the app is
  // served at the root, "/scopi" when a reverse proxy maps a public subpath
  // onto this server's root (e.g. IIS ARR rewriting example.com/scopi/* to
  // this app transparently). Every absolute API path below is built from
  // this instead of a bare leading "/", so the same static files work
  // unmodified at any mount point a proxy puts them at.
  const UI_BASE = (() => {
    const marker = "/_ui/";
    const index = window.location.pathname.indexOf(marker);
    return index === -1 ? "" : window.location.pathname.slice(0, index);
  })();

  const RECENT_KEY = "scopi.ui.recentQueries";
  const RECENT_MAX = 10;
  const LATENCY_MAX = 20;
  const PREFERRED_COLUMNS = ["@timestamp", "level", "service", "host", "status", "message"];

  const HELP_EXAMPLES = [
    "level:ERROR AND service:auth",
    "status:>=500",
    'message:"connection refused"',
    "message:conn*",
    "level:(ERROR OR WARN OR FATAL)",
    "@timestamp:>now-1h",
    "_exists_:trace_id",
    "trace_id.keyword:tr-1250e40d54",
    "level:ERROR AND service:auth | sort -@timestamp | limit 20",
    "service:auth | fields service,status,message | limit 50",
    "status:>=500 | stats count() by service",
  ];

  const el = (id) => document.getElementById(id);
  const indexSelect = el("index-select");
  const queryInput = el("query-input");
  const searchBtn = el("search-btn");
  const searchMeta = el("search-meta");
  const errorBanner = el("error-banner");
  const resultsTable = el("results-table");
  const resultsHead = el("results-head");
  const resultsBody = el("results-body");
  const emptyState = el("empty-state");
  const recentList = el("recent-queries");
  const sparkline = el("latency-sparkline");
  const latencyLabel = el("latency-label");
  const indicesBody = el("indices-body");
  const indexDetail = el("index-detail");
  const indexDetailTitle = el("index-detail-title");
  const indexDetailBody = el("index-detail-body");
  const clusterStatus = el("cluster-status");
  const clusterLabel = el("cluster-label");
  const helpExamples = el("help-examples");
  const sessionInfo = el("session-info");
  const sessionPrincipal = el("session-principal");
  const logoutBtn = el("logout-btn");
  const settingsAuthOffNote = el("settings-auth-off-note");
  const accountsBody = el("accounts-body");
  const accountForm = el("account-form");
  const accountUsername = el("account-username");
  const accountPassword = el("account-password");
  const accountError = el("account-error");

  let latencies = [];

  async function fetchJSON(url, options) {
    const res = await fetch(url, options);
    let body = null;
    try {
      body = await res.json();
    } catch {
      body = null;
    }
    if (!res.ok) {
      const reason = body && body.error && body.error.reason ? body.error.reason : res.statusText;
      throw new Error(reason);
    }
    return body;
  }

  function showError(message) {
    errorBanner.textContent = message;
    errorBanner.hidden = false;
  }

  function clearError() {
    errorBanner.hidden = true;
    errorBanner.textContent = "";
  }

  function recentQueries(index) {
    try {
      const all = JSON.parse(localStorage.getItem(RECENT_KEY) || "{}");
      return all[index] || [];
    } catch {
      return [];
    }
  }

  function pushRecentQuery(index, query) {
    if (!query.trim()) return;
    try {
      const all = JSON.parse(localStorage.getItem(RECENT_KEY) || "{}");
      const list = (all[index] || []).filter((q) => q !== query);
      list.unshift(query);
      all[index] = list.slice(0, RECENT_MAX);
      localStorage.setItem(RECENT_KEY, JSON.stringify(all));
    } catch {
      /* localStorage unavailable — recent-query history is a convenience, not required */
    }
  }

  function renderRecentQueries(index) {
    const list = recentQueries(index);
    recentList.innerHTML = "";
    if (list.length === 0) {
      const li = document.createElement("li");
      li.className = "empty";
      li.textContent = "none yet";
      recentList.appendChild(li);
      return;
    }
    for (const q of list) {
      const li = document.createElement("li");
      li.textContent = q;
      li.title = q;
      li.addEventListener("click", () => {
        queryInput.value = q;
        runSearch();
      });
      recentList.appendChild(li);
    }
  }

  function drawSparkline() {
    const ctx = sparkline.getContext("2d");
    const w = sparkline.width;
    const h = sparkline.height;
    ctx.clearRect(0, 0, w, h);
    if (latencies.length < 2) {
      latencyLabel.textContent = latencies.length === 1 ? `${latencies[0]} ms` : "no queries yet";
      return;
    }
    const max = Math.max(...latencies, 1);
    const min = 0;
    const stepX = w / (latencies.length - 1);
    ctx.beginPath();
    latencies.forEach((v, i) => {
      const x = i * stepX;
      const y = h - ((v - min) / (max - min || 1)) * (h - 4) - 2;
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    const gradient = ctx.createLinearGradient(0, 0, w, 0);
    gradient.addColorStop(0, "#1CC8F0");
    gradient.addColorStop(1, "#A855F7");
    ctx.strokeStyle = gradient;
    ctx.lineWidth = 2;
    ctx.stroke();
    latencyLabel.textContent = `last ${latencies.length} — ${latencies[latencies.length - 1]} ms`;
  }

  function recordLatency(ms) {
    latencies.push(ms);
    if (latencies.length > LATENCY_MAX) latencies.shift();
    drawSparkline();
  }

  function levelBadge(value) {
    const span = document.createElement("span");
    const known = ["ERROR", "FATAL", "WARN", "WARNING", "INFO", "DEBUG"];
    const upper = String(value).toUpperCase();
    span.className = "level-badge" + (known.includes(upper) ? ` level-${upper}` : "");
    span.textContent = value;
    return span;
  }

  function pickColumns(hits) {
    const seen = new Set();
    for (const hit of hits) {
      for (const key of Object.keys(hit._source || {})) seen.add(key);
    }
    const columns = PREFERRED_COLUMNS.filter((c) => seen.has(c));
    if (columns.length === 0) {
      return Array.from(seen).slice(0, 6);
    }
    return columns;
  }

  function renderResults(response) {
    const hits = response.hits.hits;
    resultsHead.innerHTML = "";
    resultsBody.innerHTML = "";

    if (hits.length === 0) {
      resultsTable.hidden = true;
      emptyState.hidden = false;
      emptyState.innerHTML = "<p>No hits.</p>";
      return;
    }

    emptyState.hidden = true;
    resultsTable.hidden = false;

    const columns = pickColumns(hits);
    for (const col of columns) {
      const th = document.createElement("th");
      th.textContent = col;
      resultsHead.appendChild(th);
    }
    const scoreTh = document.createElement("th");
    scoreTh.textContent = "score";
    resultsHead.appendChild(scoreTh);

    for (const hit of hits) {
      const tr = document.createElement("tr");
      for (const col of columns) {
        const td = document.createElement("td");
        const value = (hit._source || {})[col];
        if (col === "level" && value !== undefined) {
          td.appendChild(levelBadge(value));
        } else if (value === undefined) {
          td.textContent = "";
        } else if (typeof value === "object") {
          td.textContent = JSON.stringify(value);
          td.className = "mono";
        } else {
          td.textContent = String(value);
          if (col === "message") {
            td.className = "mono truncate";
            td.title = String(value);
          }
        }
        tr.appendChild(td);
      }
      const scoreTd = document.createElement("td");
      scoreTd.textContent = hit._score != null ? hit._score.toFixed(3) : "";
      tr.appendChild(scoreTd);
      resultsBody.appendChild(tr);
    }
  }

  function renderMeta(response) {
    const total = response.hits.total.value;
    const relation = response.hits.total.relation === "gte" ? "+" : "";
    let text = `${total}${relation} hit(s) in ${response.took} ms`;
    searchMeta.innerHTML = "";
    searchMeta.appendChild(document.createTextNode(text));
    if (response.scopi && response.scopi.scopiql && response.scopi.scopiql.sort_truncated) {
      const warn = document.createElement("span");
      warn.className = "warn";
      warn.textContent = " — sort truncated at max_sort_candidates";
      searchMeta.appendChild(warn);
    }
    searchMeta.hidden = false;
  }

  async function runSearch() {
    const index = indexSelect.value;
    if (!index) return;
    const query = queryInput.value;
    clearError();
    const started = performance.now();
    try {
      const url = `${UI_BASE}/${encodeURIComponent(index)}/_search?q=${encodeURIComponent(query)}&size=25`;
      const response = await fetchJSON(url);
      const clientMs = Math.round(performance.now() - started);
      recordLatency(typeof response.took === "number" ? response.took : clientMs);
      renderMeta(response);
      renderResults(response);
      pushRecentQuery(index, query);
      renderRecentQueries(index);
    } catch (err) {
      resultsTable.hidden = true;
      emptyState.hidden = true;
      showError(err.message || String(err));
    }
  }

  async function loadIndices() {
    let indices = [];
    try {
      indices = await fetchJSON(`${UI_BASE}/_cat/indices`);
      clusterStatus.className = "status-dot ok";
      clusterLabel.textContent = "connected";
    } catch (err) {
      clusterStatus.className = "status-dot error";
      clusterLabel.textContent = "unreachable";
      showError(err.message || String(err));
      return [];
    }
    indexSelect.innerHTML = "";
    if (indices.length === 0) {
      emptyState.hidden = false;
      resultsTable.hidden = true;
      searchBtn.disabled = true;
      queryInput.disabled = true;
    } else {
      emptyState.hidden = true;
      searchBtn.disabled = false;
      queryInput.disabled = false;
      for (const info of indices) {
        const opt = document.createElement("option");
        opt.value = info.index;
        opt.textContent = `${info.index} (${info.docs_count})`;
        indexSelect.appendChild(opt);
      }
      renderRecentQueries(indexSelect.value);
    }

    indicesBody.innerHTML = "";
    for (const info of indices) {
      const tr = document.createElement("tr");
      tr.innerHTML =
        `<td>${info.index}</td><td>${info.docs_count}</td>` +
        `<td>${info.segment_count}</td><td class="mono">${info.created_at || ""}</td>`;
      tr.style.cursor = "pointer";
      tr.addEventListener("click", () => showIndexDetail(info.index));
      indicesBody.appendChild(tr);
    }
    return indices;
  }

  async function showIndexDetail(index) {
    try {
      const stats = await fetchJSON(`${UI_BASE}/${encodeURIComponent(index)}/_stats`);
      indexDetailTitle.textContent = index;
      indexDetailBody.textContent = JSON.stringify(stats, null, 2);
      indexDetail.hidden = false;
    } catch (err) {
      showError(err.message || String(err));
    }
  }

  function activateTab(name) {
    document.querySelectorAll(".nav-item").forEach((b) => {
      b.classList.toggle("active", b.dataset.tab === name);
    });
    document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
    el(`tab-${name}`).classList.add("active");
  }

  function setupTabs() {
    document.querySelectorAll(".nav-item").forEach((btn) => {
      btn.addEventListener("click", () => activateTab(btn.dataset.tab));
    });
  }

  function setupHelp() {
    helpExamples.innerHTML = "";
    for (const example of HELP_EXAMPLES) {
      const li = document.createElement("li");
      li.textContent = example;
      li.addEventListener("click", () => {
        queryInput.value = example;
        activateTab("search");
        if (indexSelect.value) runSearch();
      });
      helpExamples.appendChild(li);
    }
  }

  function setupSearch() {
    searchBtn.addEventListener("click", runSearch);
    queryInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") runSearch();
    });
    indexSelect.addEventListener("change", () => renderRecentQueries(indexSelect.value));
  }

  async function checkSession() {
    try {
      const info = await fetchJSON(`${UI_BASE}/_ui/api/session`);
      if (info.auth_enabled) {
        sessionInfo.hidden = false;
        sessionPrincipal.textContent = info.principal;
        sessionPrincipal.title = `${info.principal} (${info.auth_method})`;
        settingsAuthOffNote.hidden = true;
      } else {
        sessionInfo.hidden = true;
        settingsAuthOffNote.hidden = false;
      }
    } catch {
      // A 401 here would already have redirected the page itself to the
      // login screen server-side before this script ever ran, so reaching
      // this branch means a transient network error — leave the sidebar as-is.
    }
  }

  function setupLogout() {
    logoutBtn.addEventListener("click", async () => {
      try {
        await fetch(`${UI_BASE}/_ui/api/logout`, { method: "POST" });
      } finally {
        window.location.href = `${UI_BASE}/_ui/login.html`;
      }
    });
  }

  async function loadAccounts() {
    let accounts;
    try {
      accounts = await fetchJSON(`${UI_BASE}/_ui/api/accounts`);
    } catch (err) {
      showError(err.message || String(err));
      return;
    }
    accountsBody.innerHTML = "";
    for (const account of accounts) {
      const tr = document.createElement("tr");
      const statusText = account.disabled ? "disabled" : "enabled";
      tr.innerHTML =
        `<td>${account.username}</td><td>${statusText}</td>` +
        `<td class="mono">${account.created_at}</td>`;

      const actionsTd = document.createElement("td");
      const actions = document.createElement("div");
      actions.className = "account-actions";

      const toggleBtn = document.createElement("button");
      toggleBtn.type = "button";
      toggleBtn.textContent = account.disabled ? "Enable" : "Disable";
      toggleBtn.addEventListener("click", () => toggleAccount(account.username, !account.disabled));
      actions.appendChild(toggleBtn);

      const deleteBtn = document.createElement("button");
      deleteBtn.type = "button";
      deleteBtn.className = "danger";
      deleteBtn.textContent = "Delete";
      deleteBtn.addEventListener("click", () => deleteAccount(account.username));
      actions.appendChild(deleteBtn);

      actionsTd.appendChild(actions);
      tr.appendChild(actionsTd);
      accountsBody.appendChild(tr);
    }
  }

  async function toggleAccount(username, disable) {
    try {
      const verb = disable ? "disable" : "enable";
      await fetchJSON(`${UI_BASE}/_ui/api/accounts/${encodeURIComponent(username)}/${verb}`, {
        method: "POST",
      });
      await loadAccounts();
    } catch (err) {
      showError(err.message || String(err));
    }
  }

  async function deleteAccount(username) {
    try {
      await fetchJSON(`${UI_BASE}/_ui/api/accounts/${encodeURIComponent(username)}`, { method: "DELETE" });
      await loadAccounts();
    } catch (err) {
      showError(err.message || String(err));
    }
  }

  function setupSettings() {
    setupLogout();
    accountForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      accountError.hidden = true;
      try {
        await fetchJSON(`${UI_BASE}/_ui/api/accounts`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            username: accountUsername.value,
            password: accountPassword.value,
          }),
        });
        accountForm.reset();
        await loadAccounts();
      } catch (err) {
        accountError.textContent = err.message || String(err);
        accountError.hidden = false;
      }
    });
  }

  async function init() {
    setupTabs();
    setupSearch();
    setupHelp();
    setupSettings();
    drawSparkline();
    await Promise.all([loadIndices(), checkSession(), loadAccounts()]);
  }

  init();
})();
