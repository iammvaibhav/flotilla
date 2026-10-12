import { EditorView, basicSetup } from "https://esm.sh/codemirror@6.0.1";
import { yaml } from "https://esm.sh/@codemirror/lang-yaml@6.1.1";
import { autocompletion } from "https://esm.sh/@codemirror/autocomplete@6.18.1";
import { oneDark } from "https://esm.sh/@codemirror/theme-one-dark@6.1.2";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
const EFFORTS = ["off", "minimal", "low", "medium", "high", "xhigh", "max", "auto"];

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "content-type": "application/json" }, ...opts,
    body: opts.body && typeof opts.body !== "string" ? JSON.stringify(opts.body) : opts.body,
  });
  if (res.status === 401) { location.href = "/login"; throw new Error("login"); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw Object.assign(new Error(data.detail || data.error || res.statusText), { data, status: res.status });
  return data;
}

/** Stream a job's log into `pre`; resolve with the job summary. */
function follow(jobId, pre, onLine) {
  pre.hidden = false;
  pre.textContent = "";
  return new Promise((resolve) => {
    const es = new EventSource(`/api/jobs/${jobId}/stream`);
    es.onmessage = (e) => {
      const line = JSON.parse(e.data);
      pre.textContent += line + "\n";
      pre.scrollTop = pre.scrollHeight;
      onLine && onLine(line);
    };
    es.addEventListener("done", (e) => { es.close(); resolve(JSON.parse(e.data)); });
    es.onerror = () => { es.close(); resolve({ status: "failed" }); };
  });
}

function colorDiff(text) {
  return text.split("\n").map((l) => {
    const cls = l.startsWith("+") && !l.startsWith("+++") ? "add" : l.startsWith("-") && !l.startsWith("---") ? "del" : l.startsWith("@@") ? "hunk" : "";
    return `<span class="${cls}">${esc(l)}</span>`;
  }).join("\n");
}

// ------------------------------------------------------------------ tabs
let fleet = null;
$$("nav button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
function showTab(name) {
  $$("nav button").forEach((b) => b.classList.toggle("on", b.dataset.tab === name));
  $$("main > section").forEach((s) => (s.hidden = s.id !== `tab-${name}`));
  ({ jobs: loadJobs, config: initConfig, onboard: initOnboard }[name] || (() => {}))();
  history.replaceState(null, "", `#${name}`);
}

// ------------------------------------------------------------------ hosts
async function loadFleet() {
  fleet = await api("/api/fleet");
  $("#ctl").textContent = `controller: ${fleet.controller}`;
  const groups = $("#groups");
  groups.innerHTML = Object.entries(fleet.groups).map(([g, names]) => `
    <h2>${esc(g)} <span class="muted">(${names.length})</span></h2>
    <div class="grid">${names.map((n) => hostCard(fleet.hosts.find((h) => h.name === n))).join("")}</div>`).join("");
  $$(".card", groups).forEach((c) => c.addEventListener("click", () => openHost(c.dataset.host)));
}

function hostCard(h) {
  return `<div class="card" data-host="${esc(h.name)}">
    <h3><span class="dot" data-dot="${esc(h.name)}"></span>${esc(h.name)}</h3>
    <div class="muted mono">${esc(h.user)}@${esc(h.address)} · ${esc(h.platform)}</div>
    <div style="margin-top:.35rem">${h.roles.map((r) => `<span class="tag">${esc(r)}</span>`).join("")}</div>
    <div class="kv" data-kv="${esc(h.name)}"><span>status</span><span class="muted">loading…</span></div>
  </div>`;
}

async function loadStatus(fresh = false) {
  $("#status-note").textContent = "checking hosts…";
  const t0 = performance.now();
  const rows = await api(`/api/status${fresh ? "?fresh=1" : ""}`);
  for (const s of rows) {
    const dot = $(`[data-dot="${s.name}"]`);
    const kv = $(`[data-kv="${s.name}"]`);
    if (!dot) continue;
    const disk = parseInt(s.disk || "0", 10);
    dot.className = "dot " + (!s.ok ? "bad" : disk >= 90 ? "warn" : "ok");
    kv.innerHTML = !s.ok ? `<span>error</span><span>${esc(s.error)}</span>` : [
      ["omp", s.omp], ["paseo", `${s.paseo || "—"} ${s.paseo_daemon ? `(${s.paseo_daemon})` : ""}`],
      ["uv / pnpm", `${s.uv || "—"} / ${s.pnpm || "—"}`], ["node", s.node || "—"],
      ["github", `${s.gh === "ok" ? "✓ logged in" : s.gh === "keychain" ? "✓ keychain" : "✗"} · ${s.git_email || "no git email"}`],
      ["netbird", s.netbird || "—"], ["disk /", s.disk + (disk >= 90 ? " ⚠" : "")],
      ["load", s.load], ["up", s.uptime], ["ssh", `${s.ms} ms`],
    ].map(([k, v]) => `<span>${k}</span><span class="mono">${esc(v)}</span>`).join("");
  }
  $("#status-note").textContent = `updated ${new Date().toLocaleTimeString()} (${Math.round(performance.now() - t0)} ms)`;
}
$("#refresh").addEventListener("click", () => loadStatus(true));

const QUICK = [
  ["Versions", "omp --version; paseo --version; uv --version; pnpm --version; node --version 2>/dev/null; gh --version | head -1"],
  ["Disk", "df -h / /data 2>/dev/null; du -sh ~/.cache ~/.npm ~/.omp 2>/dev/null"],
  ["Top", "ps -eo pid,pcpu,pmem,etime,comm --sort=-pcpu 2>/dev/null | head -15 || ps -Ao pid,pcpu,pmem,etime,comm -r | head -15"],
  ["Paseo", "paseo daemon status"],
  ["NetBird", "netbird status"],
  ["omp models", "omp models | head -40"],
  ["Services", "systemctl --user list-units --type=service --state=running --no-legend 2>/dev/null || launchctl list | grep -iE 'paseo|omp'"],
];

function openHost(name) {
  const h = fleet.hosts.find((x) => x.name === name);
  const d = $("#drawer");
  d.hidden = false;
  d.innerHTML = `
    <div class="row"><h2 style="margin:0;flex:1">${esc(name)}</h2><button class="b" id="dclose">Close</button></div>
    <div class="muted mono">${esc(h.user)}@${esc(h.address)} · ${esc(h.platform)} · ${esc(h.groups.join(", "))}</div>
    <div class="row" style="margin:.9rem 0">${QUICK.map(([l], i) => `<button class="b" data-q="${i}">${l}</button>`).join("")}</div>
    <form class="row" id="cmdf"><input id="cmd" class="mono" style="flex:1" placeholder="command, e.g. tail -50 ~/.paseo/daemon.log">
      <label class="row muted"><input type="checkbox" id="allg"> all of ${esc(h.groups[0] || "group")}</label>
      <button class="b pri">Run</button></form>
    <pre class="log mono" id="dout" hidden></pre>`;
  const run = async (command) => {
    const hosts = $("#allg").checked ? [h.groups[0]] : [name];
    const { job } = await api("/api/exec", { method: "POST", body: { command, hosts } });
    follow(job, $("#dout"));
  };
  $("#dclose").onclick = () => (d.hidden = true);
  $$("[data-q]", d).forEach((b) => (b.onclick = () => run(QUICK[b.dataset.q][1])));
  $("#cmdf").onsubmit = (e) => { e.preventDefault(); const c = $("#cmd").value.trim(); if (c) run(c); };
  $("#cmd").focus();
}
document.addEventListener("keydown", (e) => { if (e.key === "Escape") $("#drawer").hidden = true; });

$("#mesh-btn").addEventListener("click", async () => {
  const box = $("#mesh");
  box.innerHTML = `<h2>SSH mesh</h2><pre class="log mono"></pre>`;
  const { job } = await api("/api/mesh", { method: "POST" });
  await follow(job, $("pre", box));
  const r = (await api(`/api/jobs/${job}`)).result;
  if (!r) return;
  const cols = [...r.hosts, "github"];
  box.innerHTML = `<h2>SSH mesh <span class="muted">(row → column)</span></h2><table class="mono"><tr><th></th>${cols.map((c) => `<th>${esc(c)}</th>`).join("")}</tr>
    ${r.hosts.map((src) => `<tr><th>${esc(src)}</th>${cols.map((dst) => {
      if (src === dst) return `<td class="muted">—</td>`;
      const v = r.cells[src]?.[dst] ?? "?";
      return `<td class="${v === "ok" ? "ok" : "bad"}" title="${esc(v)}">${v === "ok" ? "✓" : "✗"}</td>`;
    }).join("")}</tr>`).join("")}</table>`;
});

// ------------------------------------------------------------------ components
const planCache = {};
function renderMatrix() {
  const hosts = fleet.hosts.map((h) => h.name);
  $("#matrix").innerHTML = `<table><tr><th>component</th>${hosts.map((h) => `<th>${esc(h)}</th>`).join("")}<th></th></tr>
    ${Object.entries(fleet.components).map(([c, desc]) => `<tr>
      <th><div>${esc(c)}</div><div class="muted" style="font-weight:400;font-size:12px">${esc(desc)}</div></th>
      ${hosts.map((h) => {
        const p = planCache[c];
        if (!p) return `<td class="cell muted" data-c="${c}" data-h="${h}">·</td>`;
        if (p.failed) return `<td class="cell bad" data-c="${c}" data-h="${h}">error</td>`;
        const n = (p.plan[h] || []).length;
        return `<td class="cell ${n ? "warn" : "ok"}" data-c="${c}" data-h="${h}">${n ? `${n} change${n > 1 ? "s" : ""}` : "✓ in sync"}</td>`;
      }).join("")}
      <td class="row"><button class="b" data-plan="${c}">Plan</button><button class="b danger" data-apply="${c}">Apply…</button></td></tr>`).join("")}</table>
    <pre class="log mono" id="comp-log" hidden></pre>`;
  $$("[data-plan]").forEach((b) => (b.onclick = () => plan(b.dataset.plan)));
  $$("[data-apply]").forEach((b) => (b.onclick = () => apply(b.dataset.apply)));
  $$("td.cell").forEach((td) => (td.onclick = () => showCell(td.dataset.c, td.dataset.h)));
}

async function plan(c, hosts) {
  const { job } = await api(`/api/components/${c}/plan`, { method: "POST", body: { hosts } });
  const done = await follow(job, $("#comp-log"));
  const info = await api(`/api/jobs/${job}`);
  planCache[c] = done.status === "ok" ? { plan: info.result || {} } : { failed: true };
  renderMatrix();
  return planCache[c];
}

async function apply(c) {
  const p = planCache[c] || (await plan(c));
  const targets = Object.keys(p.plan || {});
  if (!targets.length) return alert(`${c}: nothing to change.`);
  const lines = targets.map((h) => `${h}: ${p.plan[h].join(", ")}`).join("\n");
  if (!confirm(`Apply ${c} to ${targets.join(", ")}?\n\n${lines}`)) return;
  const { job } = await api(`/api/components/${c}/apply`, { method: "POST", body: { hosts: targets } });
  await follow(job, $("#comp-log"));
  await plan(c);
}

async function showCell(c, h) {
  const d = $("#drawer");
  d.hidden = false;
  d.innerHTML = `<div class="row"><h2 style="margin:0;flex:1">${esc(c)} on ${esc(h)}</h2><button class="b" id="dclose">Close</button></div>
    <h4>Operations that would change</h4><div class="mono">${esc((planCache[c]?.plan?.[h] || ["(run Plan first, or nothing to change)"]).join("\n")).replace(/\n/g, "<br>")}</div>
    <h4>File diff</h4><pre class="log mono diff" id="cdiff">loading…</pre>`;
  $("#dclose").onclick = () => (d.hidden = true);
  try {
    const r = await api(`/api/components/${c}/diff?host=${encodeURIComponent(h)}`);
    $("#cdiff").innerHTML = r.diffs.length ? r.diffs.map((x) => colorDiff(x.diff)).join("\n") : esc(r.note || "files match");
  } catch (e) { $("#cdiff").textContent = e.message; }
}
$("#plan-all").addEventListener("click", async () => { for (const c of Object.keys(fleet.components)) await plan(c); });

// ------------------------------------------------------------------ config
let editor = null, curPath = null, models = [];
async function initConfig() {
  if (!editor) {
    editor = new EditorView({
      parent: $("#editor"),
      extensions: [basicSetup, yaml(), autocompletion({ override: [modelCompletions] }), EditorView.lineWrapping,
        ...(matchMedia("(prefers-color-scheme: dark)").matches ? [oneDark] : [])],
    });
    $("#sec-acc").innerHTML = (fleet.vault_accounts || []).map((a) => `<option>${esc(a)}</option>`).join("");
    $("#model-effort").innerHTML += EFFORTS.map((e) => `<option>${e}</option>`).join("");
    $("#prev-host").innerHTML = fleet.hosts.map((h) => `<option>${esc(h.name)}</option>`).join("");
    api("/api/catalog/models").then((m) => { models = m; $("#model-count").textContent = `(${m.length})`; renderModels(); }).catch((e) => ($("#models").textContent = e.message));
  }
  const files = await api("/api/state/tree");
  const byDir = {};
  for (const f of files) (byDir[f.includes("/") ? f.slice(0, f.lastIndexOf("/")) : ""] ||= []).push(f);
  $("#tree").innerHTML = Object.keys(byDir).sort().map((dir) => `<div class="dir">${esc(dir || "/")}</div>` +
    byDir[dir].map((f) => `<div data-f="${esc(f)}" class="${f === curPath ? "on" : ""}">&nbsp;&nbsp;${esc(f.slice(dir ? dir.length + 1 : 0))}</div>`).join("")).join("");
  $$("#tree [data-f]").forEach((el) => (el.onclick = () => openFile(el.dataset.f)));
  $("#history").innerHTML = (await api("/api/state/history")).map(esc).join("<br>");
  if (!curPath && files.includes("files/omp/all/config.yml")) openFile("files/omp/all/config.yml");
}

async function openFile(path) {
  const { content } = await api(`/api/state/file?path=${encodeURIComponent(path)}`);
  curPath = path;
  $("#cur").textContent = path;
  editor.dispatch({ changes: { from: 0, to: editor.state.doc.length, insert: content } });
  $$("#tree [data-f]").forEach((el) => el.classList.toggle("on", el.dataset.f === path));
  $("#save-msg").innerHTML = "";
}

function modelCompletions(ctx) {
  const word = ctx.matchBefore(/[\w./@:-]+/);
  if (!word || (word.from === word.to && !ctx.explicit) || !models.length) return null;
  const text = word.text;
  const colon = text.indexOf(":");
  if (colon > 0) {
    const m = models.find((x) => x.selector === text.slice(0, colon));
    if (!m) return null;
    return { from: word.from + colon + 1, options: (m.thinking.length ? m.thinking : EFFORTS).map((e) => ({ label: e, type: "keyword" })) };
  }
  if (text.length < 2) return null;
  return {
    from: word.from,
    options: models.map((m) => ({ label: m.selector, detail: `${m.kind}${m.thinking.length ? " · " + m.thinking.join("/") : ""}`, info: m.name, type: "constant" })),
    validFor: /^[\w./@-]*$/,
  };
}

function renderModels() {
  const q = $("#model-q").value.toLowerCase();
  const list = models.filter((m) => !q || m.selector.toLowerCase().includes(q) || (m.name || "").toLowerCase().includes(q)).slice(0, 200);
  $("#models").innerHTML = list.map((m) => `<div data-sel="${esc(m.selector)}" title="${esc(m.name)} · ${esc(m.kind)}"><span class="mono">${esc(m.selector)}</span> <span class="muted">${esc(m.thinking.join("/"))}</span></div>`).join("");
  $$("#models [data-sel]").forEach((el) => (el.onclick = () => {
    const eff = $("#model-effort").value;
    insert(el.dataset.sel + (eff ? `:${eff}` : ""));
  }));
}
$("#model-q").addEventListener("input", renderModels);

function insert(text) {
  const { from, to } = editor.state.selection.main;
  editor.dispatch({ changes: { from, to, insert: text }, selection: { anchor: from + text.length } });
  editor.focus();
}

$("#save").addEventListener("click", async () => {
  if (!curPath) return;
  const box = $("#save-msg");
  try {
    const r = await api("/api/state/file", { method: "PUT", body: { path: curPath, content: editor.state.doc.toString() } });
    box.innerHTML = `<div class="msg okm">Saved and committed.${r.warnings.length ? "<br>" + r.warnings.map(esc).join("<br>") : ""} Go to Components → Plan to roll it out.</div>`;
    initConfig();
  } catch (e) {
    box.innerHTML = `<div class="msg err">${(e.data?.problems || [e.message]).map(esc).join("<br>")}</div>`;
  }
});
$("#del").addEventListener("click", async () => {
  if (curPath && confirm(`Delete ${curPath}?`)) { await api(`/api/state/file?path=${encodeURIComponent(curPath)}`, { method: "DELETE" }); curPath = null; initConfig(); }
});
$("#create").addEventListener("click", async () => {
  const p = $("#newfile").value.trim();
  if (!p) return;
  await api("/api/state/file", { method: "PUT", body: { path: p, content: "" } });
  await initConfig(); openFile(p);
});
$("#sec-save").addEventListener("click", async () => {
  const title = $("#sec-title").value.trim(), value = $("#sec-val").value;
  if (!title || !value) return alert("Title and value are required.");
  const { ref } = await api("/api/secrets", { method: "POST", body: { account: $("#sec-acc").value, title, value } });
  $("#sec-val").value = "";
  insert(ref);
});
$("#prev").addEventListener("click", async () => {
  const d = $("#drawer");
  d.hidden = false;
  d.innerHTML = `<div class="row"><h2 style="margin:0;flex:1">${esc($("#prev-comp").value)} diff on ${esc($("#prev-host").value)}</h2><button class="b" id="dclose">Close</button></div><pre class="log mono diff" id="pdiff">loading…</pre>`;
  $("#dclose").onclick = () => (d.hidden = true);
  try {
    const r = await api(`/api/components/${$("#prev-comp").value}/diff?host=${encodeURIComponent($("#prev-host").value)}`);
    $("#pdiff").innerHTML = r.diffs.length ? r.diffs.map((x) => colorDiff(x.diff)).join("\n") : "files match";
  } catch (e) { $("#pdiff").textContent = e.message; }
});

// ------------------------------------------------------------------ onboarding
let onbReady = false;
async function initOnboard() {
  if (onbReady) return;
  onbReady = true;
  $("#onb-groups").innerHTML = Object.keys(fleet.groups).map((g) => `<label class="row"><input type="checkbox" name="groups" value="${esc(g)}" ${g === "personal" ? "checked" : ""}> ${esc(g)}</label>`).join("");
  $("[name=effort]").innerHTML = EFFORTS.map((e) => `<option ${e === "high" ? "selected" : ""}>${e}</option>`).join("");
  api("/api/onboarding/guide").then((g) => ($("#guide").innerHTML = marked.parse(g.markdown)));
  api("/api/catalog/models").then((m) => ($("#chat-models").innerHTML = m.filter((x) => x.kind === "chat").map((x) => `<option value="${esc(x.selector)}">`).join(""))).catch(() => {});
}
function onbData() {
  const f = new FormData($("#onb"));
  return {
    name: f.get("name"), address: f.get("address"), user: f.get("user"), platform: f.get("platform"),
    groups: f.getAll("groups"), names: String(f.get("names") || "").split(",").map((s) => s.trim()).filter(Boolean),
    notes: f.get("notes"), model: f.get("model"), effort: f.get("effort"),
  };
}
$("#onb-add").addEventListener("click", async () => {
  if (!$("#onb").reportValidity()) return;
  try { await api("/api/onboarding/hosts", { method: "POST", body: onbData() }); alert("Added to hosts.yaml."); await loadFleet(); loadStatus(); }
  catch (e) { alert(e.message); }
});
$("#onb-run").addEventListener("click", async () => {
  if (!$("#onb").reportValidity()) return;
  const { job } = await api("/api/onboarding/agent", { method: "POST", body: onbData() });
  follow(job, $("#onb-log"));
});

// ------------------------------------------------------------------ jobs
async function loadJobs() {
  const rows = await api("/api/jobs");
  $("#jobs").innerHTML = `<table><tr><th>started</th><th>job</th><th>status</th><th>time</th></tr>${rows.map((j) => `<tr data-j="${j.id}" style="cursor:pointer">
    <td class="mono">${new Date(j.started * 1000).toLocaleTimeString()}</td><td>${esc(j.title)}</td>
    <td class="${j.status === "ok" ? "ok" : j.status === "failed" ? "bad" : "warn"}">${j.status}</td>
    <td class="mono">${j.finished ? (j.finished - j.started).toFixed(1) + "s" : "…"}</td></tr>`).join("")}</table><pre class="log mono" id="jlog" hidden></pre>`;
  $$("[data-j]").forEach((tr) => (tr.onclick = async () => {
    const j = await api(`/api/jobs/${tr.dataset.j}`);
    $("#jlog").hidden = false;
    $("#jlog").textContent = j.log.join("\n");
  }));
}

// ------------------------------------------------------------------ boot
await loadFleet();
renderMatrix();
loadStatus();
showTab((location.hash || "#hosts").slice(1));
