// logsite app: setup, errors, watching. Plain JavaScript, no build step.
"use strict";

const $ = (sel, el = document) => el.querySelector(sel);
const main = () => $("#main");
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

let token = $('meta[name="logsite-token"]')?.content || sessionStorage.getItem("logsite-token") || "";

async function api(path, body) {
  const opts = { headers: { "X-Logsite-Token": token } };
  if (body !== undefined) {
    opts.method = "POST";
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  if (res.status === 401) { askToken(); throw new Error("needs the token"); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok && data.error) throw new Error(data.error);
  return data;
}

function askToken() {
  main().innerHTML = `<h1>Sign in</h1><div class="card"><label>Access token
    <input type="password" id="tok"></label><div class="row">
    <button class="primary" id="go">Continue</button></div>
    <p class="muted">The token this logsite server was started with (<code>--token</code>).</p></div>`;
  $("#go").onclick = () => { token = $("#tok").value.trim();
    sessionStorage.setItem("logsite-token", token); route(); };
}

const when = (iso) => {
  if (!iso) return "–";
  const d = new Date(String(iso).replace(" ", "T"));
  const mins = Math.round((Date.now() - d) / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins} min ago`;
  if (mins < 48 * 60) return `${Math.round(mins / 60)} h ago`;
  return d.toLocaleDateString();
};

let status = null;

async function refreshStatus() {
  status = await api("/api/status");
  $("#project-name").textContent = status.project_name;
  $("#project-name").title = status.project;
  const w = status.watch;
  $("#watch-pill").innerHTML = w.running
    ? `<span class="pill on">watching</span> every ${esc(w.every)}`
    : `<span class="pill">not watching</span>`;
  return status;
}

let timer = null;
const again = (fn, ms = 10000) => { clearTimeout(timer); if (status.watch.running) timer = setTimeout(fn, ms); };

async function route() {
  clearTimeout(timer);
  const [, page, id] = location.hash.split("/");
  document.querySelectorAll("[data-nav]").forEach((a) =>
    a.classList.toggle("on", a.dataset.nav === (page || "errors")));
  try {
    await refreshStatus();
    if (!status.configured && page !== "settings") return setup(0);
    if (page === "watch") return watchPage();
    if (page === "settings") return settingsPage();
    if (page === "errors" && id) return errorPage(id);
    return errorsPage();
  } catch (e) {
    if (e.message !== "needs the token") main().innerHTML = `<div class="result bad">${esc(e.message)}</div>`;
  }
}
window.addEventListener("hashchange", route);

// --- forms shared by setup and settings -----------------------------------------------------

function backendForm(el, settings, onChange) {
  const state = { type: settings.backend.type, values: { ...settings.backend.values } };
  const draw = () => {
    const kinds = Object.entries(settings.backends);
    const spec = settings.backends[state.type];
    el.innerHTML = `<div class="choices">${kinds.map(([k, v]) =>
      `<div class="choice ${k === state.type ? "on" : ""}" data-k="${k}">${esc(v.label)}</div>`).join("")}</div>
      <div id="fields"></div>`;
    el.querySelectorAll(".choice").forEach((c) => (c.onclick = () => {
      state.type = c.dataset.k; state.values = {}; draw(); onChange?.(); }));
    if (!spec) return;
    $("#fields", el).innerHTML = spec.fields.map((f) => {
      const saved = settings.backend.type === state.type && settings.backend.secrets[f.key];
      return `<label>${esc(f.label)} ${f.required ? "" : '<span class="opt">(optional)</span>'}
        <input type="${f.kind === "secret" ? "password" : "text"}" data-key="${f.key}"
          value="${esc(state.values[f.key] || "")}"
          placeholder="${esc(saved ? "saved (leave empty to keep it)" : f.placeholder)}"></label>`;
    }).join("");
    el.querySelectorAll("input[data-key]").forEach((i) => (i.oninput = () => {
      state.values[i.dataset.key] = i.value; onChange?.(); }));
  };
  draw();
  return state;
}

async function testBackend(state, out, button) {
  button.disabled = true; out.className = "result"; out.textContent = "Connecting…";
  try {
    const r = await api("/api/test/backend", state);
    if (!r.ok) { out.className = "result bad"; out.textContent = r.error; return false; }
    out.className = "result ok";
    const lines = r.lines.map((l) => `${l.time ? l.time.slice(11, 19) : "        "}  ${l.message}`).join("\n");
    out.innerHTML = `Connected. ${r.lines.length ? "Some recent lines:" : "No lines in the last 24 hours yet."}
      ${lines ? `<pre>${esc(lines)}</pre>` : ""}
      ${r.errors}${r.errors_capped ? "+" : ""} error(s) in the last 24 hours.`;
    return true;
  } catch (e) { out.className = "result bad"; out.textContent = e.message; return false; }
  finally { button.disabled = false; }
}

async function testSlack(webhook, out, button) {
  button.disabled = true; out.className = "result"; out.textContent = "Sending…";
  const r = await api("/api/test/slack", { webhook }).catch((e) => ({ ok: false, error: e.message }));
  out.className = `result ${r.ok ? "ok" : "bad"}`;
  out.textContent = r.ok ? "Sent: check the channel." : r.error;
  button.disabled = false;
  return r.ok;
}

async function testStore(url, out, button) {
  button.disabled = true; out.className = "result"; out.textContent = "Connecting…";
  const r = await api("/api/test/store", { url }).catch((e) => ({ ok: false, error: e.message }));
  out.className = `result ${r.ok ? "ok" : "bad"}`;
  out.textContent = r.ok ? "Connected: the tables are ready." : r.error;
  button.disabled = false;
}

// --- setup ----------------------------------------------------------------------------------

const draft = { slack: "", postgres: false, dbUrl: "", service: "", watch: true };

async function setup(step) {
  const settings = await api("/api/settings");
  const steps = `<div class="steps">${[0, 1, 2, 3].map((i) =>
    `<span class="${i <= step ? "on" : ""}"></span>`).join("")}</div>`;
  const head = (title, sub) => `<h1>${title}</h1><p class="muted">${sub}</p>${steps}`;
  if (step === 0) {
    main().innerHTML = head("Connect your logs", "Where does production write its logs? logsite reads them where they are; nothing is copied anywhere.")
      + `<div class="card"><div id="bf"></div><div class="row">
         <button id="test">Test connection</button><button class="primary" id="next" disabled>Next</button></div>
         <div id="out"></div></div>`;
    if (!settings.backend.type) settings.backend.type = "loki";
    const state = backendForm($("#bf"), settings, () => { $("#next").disabled = true; });
    window.setupBackend = state;
    $("#test").onclick = async () => { $("#next").disabled = !(await testBackend(state, $("#out"), $("#test"))); };
    $("#next").onclick = () => setup(1);
  } else if (step === 1) {
    main().innerHTML = head("Your code", "logsite maps each error to the line of code that raised it, in this project:")
      + `<div class="card"><div class="kv"><div>Project</div><div><code>${esc(status.project)}</code></div></div>
         <label>Service name <span class="opt">(shown in Slack messages)</span>
         <input type="text" id="svc" value="${esc(draft.service || settings.service || status.project_name)}"></label>
         <p class="muted">To use another project, start logsite from its folder: <code>logsite app</code>.</p>
         <div class="row"><button id="back">Back</button><button class="primary" id="next">Next</button></div></div>`;
    $("#back").onclick = () => setup(0);
    $("#next").onclick = () => { draft.service = $("#svc").value.trim(); setup(2); };
  } else if (step === 2) {
    main().innerHTML = head("Slack", "Where should logsite post new errors, regressions and spikes?")
      + `<div class="card"><label>Incoming webhook URL
         <input type="password" id="hook" value="${esc(draft.slack)}" placeholder="${settings.slack.saved ? "saved (leave empty to keep it)" : "https://hooks.slack.com/services/..."}"></label>
         <p class="muted">In Slack: Apps → Incoming Webhooks → Add to Slack → pick a channel → copy the URL.</p>
         <div class="row"><button id="back">Back</button><button id="test">Send test message</button>
         <button class="primary" id="next">Next</button><a href="javascript:void 0" id="skip">Skip for now</a></div>
         <div id="out"></div></div>`;
    $("#back").onclick = () => setup(1);
    $("#test").onclick = () => testSlack($("#hook").value.trim(), $("#out"), $("#test"));
    $("#next").onclick = () => { draft.slack = $("#hook").value.trim(); setup(3); };
    $("#skip").onclick = () => { draft.slack = ""; setup(3); };
  } else {
    main().innerHTML = head("Where to keep errors", "Every error logsite sees is stored for later analysis.")
      + `<div class="card">
         <label class="check"><input type="radio" name="db" value="local" ${draft.postgres ? "" : "checked"}> On this machine (SQLite, nothing to set up)</label>
         <label class="check"><input type="radio" name="db" value="pg" ${draft.postgres ? "checked" : ""}> In my Postgres</label>
         <div id="pg" ${draft.postgres ? "" : "hidden"}><label>Postgres URL
           <input type="password" id="dburl" value="${esc(draft.dbUrl)}" placeholder="postgresql://user:password@host:5432/logsite"></label>
           <div class="row"><button id="test">Test connection</button></div><div id="out"></div></div>
         <label class="check"><input type="checkbox" id="watch" ${draft.watch ? "checked" : ""}> Start watching production now (checks every 5 minutes)</label>
         <div class="row"><button id="back">Back</button><button class="primary" id="done">Finish</button></div>
         <div id="saved"></div></div>`;
    document.querySelectorAll('input[name="db"]').forEach((r) => (r.onchange = () => {
      draft.postgres = r.value === "pg" && r.checked; $("#pg").hidden = !draft.postgres; }));
    $("#test").onclick = () => testStore($("#dburl").value.trim(), $("#out"), $("#test"));
    $("#back").onclick = () => setup(2);
    $("#done").onclick = async () => {
      draft.dbUrl = $("#dburl").value.trim(); draft.watch = $("#watch").checked;
      const form = { backend: window.setupBackend, service: draft.service,
        store: { postgres: draft.postgres, url: draft.dbUrl } };
      if (draft.slack) form.slack = { webhook: draft.slack };
      try {
        $("#done").disabled = true;
        await api("/api/settings", form);
        if (draft.watch) await api("/api/watch/start", {});
        location.hash = "#/errors"; route();
      } catch (e) {
        $("#saved").className = "result bad"; $("#saved").textContent = e.message; $("#done").disabled = false;
      }
    };
  }
}

// --- errors ---------------------------------------------------------------------------------

let hours = 24;

async function errorsPage() {
  if (!location.hash.startsWith("#/errors") && location.hash) return;
  const data = await api(`/api/errors?hours=${hours}`);
  await refreshStatus();
  const pick = [1, 24, 168].map((h) => `<button data-h="${h}" ${h === hours ? 'class="primary"' : ""}>${h === 168 ? "7 days" : h + " h"}</button>`).join("");
  const rows = data.groups.map((g) => `<tr class="click" data-id="${esc(g.id)}">
    <td><strong>${esc(g.title)}</strong>${g.root_cause ? `<div class="muted">root cause: ${esc(g.root_cause)}</div>` : ""}</td>
    <td><code>${esc(g.raised_at || g.logged_at || "–")}</code></td>
    <td class="num">${g.recent.toLocaleString()}</td>
    <td class="muted">${when(g.last_seen)}</td></tr>`).join("");
  main().innerHTML = `<h1>Errors</h1><p class="muted">Grouped by cause, from what logsite has stored${status.watch.running ? "" : ". Start watching to collect new ones"}.</p>
    <div class="row">${pick}</div>
    <div class="card">${data.groups.length ? `<table><tr><th>Error</th><th>Where</th><th class="num">Count</th><th>Last seen</th></tr>${rows}</table>`
      : `<div class="empty">${status.watch.running && !status.watch.last_check
          ? "First check running: reading the last 24 hours of logs…"
          : `No errors stored in this period.${status.watch.running ? " logsite is watching: new ones will show up here." : ` <a href="#/watch">Start watching</a> to collect them.`}`}</div>`}</div>`;
  again(errorsPage);
  main().querySelectorAll("[data-h]").forEach((b) => (b.onclick = () => { hours = +b.dataset.h; errorsPage(); }));
  main().querySelectorAll("tr[data-id]").forEach((r) => (r.onclick = () => (location.hash = `#/errors/${r.dataset.id}`)));
}

async function errorPage(id) {
  const d = await api(`/api/errors/${id}`);
  const g = d.group;
  const where = (at, code, url) => at ? `<code>${url ? `<a href="${esc(url)}" target="_blank" rel="noopener">${esc(at)}</a>` : esc(at)}</code>${code ? `<pre>${esc(code)}</pre>` : ""}` : "–";
  const counts = Object.fromEntries(d.hourly.map((h) => [h.hour.replace("T", " "), h.n]));
  const hoursBack = [...Array(168).keys()].reverse().map((i) => {
    const t = new Date(Date.now() - i * 3600e3);
    const key = t.toISOString().slice(0, 13).replace("T", " ");
    return [key, counts[key] || 0];
  });
  const max = Math.max(1, ...hoursBack.map(([, n]) => n));
  const bars = hoursBack.map(([k, n]) => `<span title="${esc(k)}:00 UTC · ${n}" style="height:${n ? Math.max(4, Math.round((n / max) * 100)) : 0}%"></span>`).join("");
  const events = d.events.map((e, i) => `<tr class="click" data-i="${i}"><td class="muted">${when(e.at)}</td>
    <td>${esc(e.message)}</td><td class="muted">${esc(e.service || "")}</td><td class="muted">${esc(e.release || "")}</td></tr>
    <tr hidden data-tb="${i}"><td colspan="4"><pre>${esc(e.traceback || JSON.stringify(e.fields, null, 2))}</pre>
    ${e.trace_id ? `<div class="muted">trace: <code>${esc(e.trace_id)}</code> · <code>logsite trace ${esc(e.trace_id)}</code></div>` : ""}</td></tr>`).join("");
  main().innerHTML = `<p><a href="#/errors">← Errors</a></p><h1>${esc(g.title)}</h1>
    <p class="muted">${g.events.toLocaleString()} stored · first seen ${when(g.first_seen)} · last seen ${when(g.last_seen)}</p>
    <div class="card"><div class="kv">
      <div>Raised at</div><div>${where(g.raised_at, g.raised_code, g.raised_url)}${g.raised_in ? `<span class="muted">in ${esc(g.raised_in)}()</span>` : ""}</div>
      <div>Logged at</div><div>${where(g.logged_at, g.logged_code, g.logged_url)}</div>
      ${g.root_cause ? `<div>Root cause</div><div>${esc(g.root_cause)}${g.origin ? ` <span class="muted">(${esc(g.origin)})</span>` : ""}</div>` : ""}
    </div></div>
    <div class="card"><strong>Last 7 days</strong><div class="bars">${bars || '<span style="height:1px"></span>'}</div></div>
    <div class="card"><div class="row" style="margin:0"><strong style="flex:1">Reproduce it</strong>
      <button class="primary" id="repro">Write a failing test</button></div><div id="repro-out"></div></div>
    <div class="card"><strong>Recent occurrences</strong> <span class="muted">(click one for its stack trace)</span>
      <table><tr><th>When</th><th>Message</th><th>Service</th><th>Release</th></tr>${events}</table></div>
    ${d.alerts.length ? `<div class="card"><strong>Alerts sent</strong>${d.alerts.map((a) => `<div>${esc(a.kind)} · ${when(a.sent_at)} · ${esc(a.channels || "nowhere")}</div>`).join("")}</div>` : ""}`;
  main().querySelectorAll("tr[data-i]").forEach((r) => (r.onclick = () => {
    const tb = $(`tr[data-tb="${r.dataset.i}"]`); tb.hidden = !tb.hidden; }));
  $("#repro").onclick = async () => {
    const out = $("#repro-out"); $("#repro").disabled = true;
    out.className = "result"; out.textContent = "Reading the logs and the code…";
    const r = await api(`/api/errors/${id}/repro`, {}).catch((e) => ({ ok: false, error: e.message }));
    $("#repro").disabled = false;
    if (!r.ok) { out.className = "result bad"; out.textContent = r.error; return; }
    out.className = "result";
    out.innerHTML = r.test
      ? `<pre id="test-code">${esc(r.test)}</pre>
         <div class="row"><button class="primary" id="save" ${r.exists ? "disabled" : ""}>${r.exists ? "Saved in the project" : `Save to ${esc(r.path)}`}</button>
         <button id="copy">Copy</button></div><div id="save-out"></div>
         <p class="muted">Then check it fails the way production did:</p>
         <pre>logsite agent --expect ${esc(r.case.id)} -- pytest ${esc(r.path)}</pre>`
      : esc(r.note || "No test for this one.");
    $("#copy")?.addEventListener("click", () => navigator.clipboard.writeText(r.test));
    $("#save")?.addEventListener("click", async () => {
      const w = await api(`/api/errors/${id}/repro`, { write: true }).catch((e) => ({ ok: false, error: e.message }));
      $("#save-out").className = `result ${w.ok ? "ok" : "bad"}`;
      $("#save-out").textContent = w.ok ? `Saved ${w.path}.` : w.error;
      if (w.ok) { $("#save").disabled = true; $("#save").textContent = "Saved in the project"; }
    });
  };
}

// --- watching -------------------------------------------------------------------------------

async function watchPage() {
  const w = status.watch;
  const last = w.last;
  main().innerHTML = `<h1>Watching production</h1>
    <p class="muted">logsite checks ${esc(status.backend?.label || "your logs")} every ${esc(w.every)}, stores every error, and posts new errors, regressions and spikes${status.slack ? " to Slack" : ""}.</p>
    <div class="card"><div class="row" style="margin:0"><span class="pill ${w.running ? "on" : ""}">${w.running ? "watching" : "stopped"}</span>
      <span style="flex:1"></span><button class="primary" id="toggle">${w.running ? "Stop watching" : "Start watching"}</button></div>
      <div class="kv" style="margin-top:14px">
        <div>Last check</div><div>${when(w.last_check)}</div>
        ${last ? `<div>Found</div><div>${last.events} error(s) in ${last.groups} group(s), ${last.stored} new stored</div>
        <div>Alerts</div><div>${last.alerts ? `${last.alerts} sent to ${esc(last.sent.join(", ") || "nowhere")}` : "none"}</div>
        ${last.export_error ? `<div>Warehouse</div><div class="result bad">${esc(last.export_error)}</div>` : last.exported ? `<div>Warehouse</div><div>${last.exported} exported</div>` : ""}` : ""}
        <div>Slack</div><div>${status.slack ? "connected" : 'not connected: <a href="#/settings">add it</a>'}</div>
        <div>Errors kept in</div><div><code>${esc(status.store)}</code></div>
      </div>${w.error ? `<div class="result bad">Last check failed: ${esc(w.error)}</div>` : ""}</div>`;
  again(async () => { if (location.hash === "#/watch") { await refreshStatus(); watchPage(); } });
  $("#toggle").onclick = async () => {
    await api(w.running ? "/api/watch/stop" : "/api/watch/start", {});
    await refreshStatus(); watchPage();
  };
}

// --- settings -------------------------------------------------------------------------------

async function settingsPage() {
  const s = await api("/api/settings");
  main().innerHTML = `<h1>Settings</h1><p class="muted">Saved to <code>${esc(status.config_file)}</code>; passwords and tokens are kept apart, in a file only you can read.</p>
    ${status.config_error ? `<div class="result bad">The config file has a problem: ${esc(status.config_error)}</div>` : ""}
    <div class="card"><strong>Logs</strong><div id="bf"></div><div class="row">
      <button id="tb">Test connection</button><button class="primary" id="sb">Save</button></div><div id="ob"></div></div>
    <div class="card"><strong>Slack</strong><label>Incoming webhook URL
      <input type="password" id="hook" placeholder="${s.slack.saved ? "saved (leave empty to keep it)" : "https://hooks.slack.com/services/..."}"></label>
      <div class="row"><button id="ts">Send test message</button><button class="primary" id="ss">Save</button></div><div id="os"></div></div>
    <div class="card"><strong>Alerts</strong>
      ${["new", "regression", "spike"].map((k) => `<label class="check"><input type="checkbox" data-alert="${k}" ${s.watch.alert_on.includes(k) ? "checked" : ""}>
        ${{ new: "New errors", regression: "Errors back after a fix", spike: "Spikes" }[k]}</label>`).join("")}
      <label>Check every <select id="every">${["1m", "5m", "15m", "1h"].map((e) => `<option ${e === s.watch.every ? "selected" : ""}>${e}</option>`).join("")}</select></label>
      <label>Service name <input type="text" id="svc" value="${esc(s.service)}" placeholder="${esc(status.project_name)}"></label>
      <div class="row"><button class="primary" id="sa">Save</button></div><div id="oa"></div></div>
    <div class="card"><strong>Database</strong>
      <label class="check"><input type="radio" name="db" value="local" ${s.store.postgres ? "" : "checked"}> On this machine (SQLite)</label>
      <label class="check"><input type="radio" name="db" value="pg" ${s.store.postgres ? "checked" : ""}> In my Postgres</label>
      <label>Postgres URL <input type="password" id="dburl" placeholder="${s.store.postgres ? "saved (leave empty to keep it)" : "postgresql://user:password@host:5432/logsite"}"></label>
      <label>Keep errors for (days) <input type="text" id="ret" value="${esc(s.store.retention_days)}"></label>
      <div class="row"><button id="td">Test connection</button><button class="primary" id="sd">Save</button></div><div id="od"></div></div>`;
  const state = backendForm($("#bf"), s);
  const saved = (out, p) => p.then(() => { out.className = "result ok"; out.textContent = "Saved."; refreshStatus(); })
    .catch((e) => { out.className = "result bad"; out.textContent = e.message; });
  $("#tb").onclick = () => testBackend(state, $("#ob"), $("#tb"));
  $("#sb").onclick = () => saved($("#ob"), api("/api/settings", { backend: state }));
  $("#ts").onclick = () => testSlack($("#hook").value.trim(), $("#os"), $("#ts"));
  $("#ss").onclick = () => saved($("#os"), api("/api/settings", { slack: { webhook: $("#hook").value.trim() } }));
  $("#sa").onclick = () => saved($("#oa"), api("/api/settings", {
    watch: { every: $("#every").value, alert_on: [...main().querySelectorAll("[data-alert]:checked")].map((c) => c.dataset.alert) },
    service: $("#svc").value.trim() }));
  $("#td").onclick = () => testStore($("#dburl").value.trim(), $("#od"), $("#td"));
  $("#sd").onclick = () => {
    const pg = main().querySelector('input[name="db"]:checked').value === "pg";
    saved($("#od"), api("/api/settings", { store: { postgres: pg, url: $("#dburl").value.trim(), retention_days: $("#ret").value } }));
  };
}

route();
