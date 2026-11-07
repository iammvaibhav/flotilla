import { EditorView, basicSetup } from "https://esm.sh/codemirror@6.0.1";
import { yaml } from "https://esm.sh/@codemirror/lang-yaml@6.1.1";
import { autocompletion } from "https://esm.sh/@codemirror/autocomplete@6.18.1";
import { oneDark } from "https://esm.sh/@codemirror/theme-one-dark@6.1.2";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
const EFFORTS = ["off", "minimal", "low", "medium", "high", "xhigh", "max", "auto"];
const ago = (t) => { const s = Math.round(Date.now() / 1000 - t); return s < 90 ? `${s}s ago` : s < 5400 ? `${Math.round(s / 60)} min ago` : `${Math.round(s / 3600)} h ago`; };

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "content-type": "application/json" }, ...opts,
    body: opts.body && typeof opts.body !== "string" ? JSON.stringify(opts.body) : opts.body,
  });
  if (res.status === 401) { location.href = "/login"; throw new Error("login"); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw Object.assign(new Error(data.detail || data.error || (data.problems || []).join("\n") || res.statusText), { data, status: res.status });
  return data;
}

/** Stream a job's log into `pre`; resolve with the job summary. */
function follow(jobId, pre) {
  pre.hidden = false;
  pre.textContent = "";
  return new Promise((resolve) => {
    const es = new EventSource(`/api/jobs/${jobId}/stream`);
    es.onmessage = (e) => { pre.textContent += JSON.parse(e.data) + "\n"; pre.scrollTop = pre.scrollHeight; };
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
  if (!$(`#tab-${name}`)) name = "hosts";
  $$("nav button").forEach((b) => b.classList.toggle("on", b.dataset.tab === name));
  $$("main > section").forEach((s) => (s.hidden = s.id !== `tab-${name}`));
  ({ jobs: loadJobs, config: initConfig }[name] || (() => {}))();
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
document.addEventListener("keydown", (e) => { if (e.key === "Escape") { $("#drawer").hidden = true; closeMenu(); } });

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
// One bundle at a time: its files (or items), and for one file either the
// file each host ends up with (with where each key comes from), the diff
// against a host, or one layer's file in an editor.
const C = { list: [], b: null, file: null, view: "compiled", host: null, item: null, models: [] };

const layerKind = (id) => id === "base" ? "base" : id.split(":")[0];
const layerShort = (id) => id === "base" ? "base" : id.split(":")[1];
const chip = (id, extra = "") => `<span class="chip ${layerKind(id)} ${extra}" title="${esc(id)}">${esc(layerShort(id))}</span>`;
const layerDir = (id) => id === "base" ? "base" : `${id.split(":")[0]}s/${id.split(":")[1]}`;
const statePath = (b, layer, rel) => b.name === "settings" ? `settings/${layerDir(layer)}/${rel}` : `bundles/${b.name}/${layerDir(layer)}/${rel}`;
const allLayers = () => ["base", ...Object.keys(fleet.groups).map((g) => `group:${g}`), ...fleet.hosts.map((h) => `host:${h.name}`)];

function planBadge(b) {
  if (!b.deployable) return `<span class="pill">settings</span>`;
  const p = b.plan;
  if (!p) return `<span class="pill">not planned</span>`;
  const n = Object.keys(p.plan || {}).length, e = Object.keys(p.errors || {}).length;
  return [n ? `<span class="pill warn" title="${esc(Object.keys(p.plan).join(", "))}">${n} out of date</span>` : "",
    e ? `<span class="pill bad" title="${esc(Object.entries(p.errors).map(([h, m]) => `${h}: ${m}`).join("\n"))}">${e} offline</span>` : "",
    !n && !e ? `<span class="pill ok">in sync</span>` : ""].join(" ");
}

async function initConfig() {
  if (!C.models.length) api("/api/catalog/models").then((m) => (C.models = m)).catch(() => {});
  await loadBundles();
  loadHistory();
  if (!C.b) openBundle((C.list.find((b) => b.name === "omp") || C.list[0]).name);
}

async function loadBundles() {
  C.list = await api("/api/bundles");
  $("#blist").innerHTML = C.list.map((b) => `<div class="bi ${C.b?.name === b.name ? "on" : ""}" data-b="${esc(b.name)}">
      <div class="nm"><span>${esc(b.name === "settings" ? "Flotilla settings" : b.name)}</span></div>
      <div class="ds muted mono">${esc(b.dest || "used by components")}</div>
      <div style="margin-top:.25rem">${planBadge(b)}</div></div>`).join("");
  $$("#blist [data-b]").forEach((el) => (el.onclick = () => openBundle(el.dataset.b)));
}

async function loadHistory() {
  $("#history").innerHTML = (await api("/api/state/history")).map(esc).join("<br>");
}

async function openBundle(name, keepFile = false) {
  C.b = await api(`/api/bundles/${encodeURIComponent(name)}`);
  if (!keepFile) { C.file = null; C.item = null; C.view = "compiled"; }
  C.host ||= fleet.controller;
  $$("#blist [data-b]").forEach((el) => el.classList.toggle("on", el.dataset.b === name));
  renderBundle();
}

function renderBundle() {
  const b = C.b;
  const p = b.plan;
  $("#bundle").innerHTML = `
    <div class="row" style="align-items:baseline">
      <h2 style="margin:0">${esc(b.name === "settings" ? "Flotilla settings" : b.name)}</h2>
      <span class="muted">${esc(b.description)}</span>
      <span style="flex:1"></span>
      ${b.deployable ? `<button class="b" id="bplan">Plan</button><button class="b danger" id="bapply">Apply…</button>` : ""}
    </div>
    <div class="muted" style="margin:.3rem 0 .2rem">
      ${b.deployable ? `Goes to <span class="mono">${esc(b.dest)}</span> on every host.` : "Not copied to hosts: the components read these."}
      Layers: ${chip("base")} → groups → hosts; the most specific wins.
      ${b.owned.length ? `Owned folders (extra files deleted): <span class="mono">${esc(b.owned.join(", "))}</span>.` : ""}
      ${b.after_apply ? `After apply it runs <span class="mono" title="${esc(b.after_apply)}">a script</span> (see bundle.yaml).` : ""}
    </div>
    ${b.deployable ? `<div class="muted" style="font-size:12.5px">${p ? `Planned ${ago(p.at)}: ${planBadge(b)} ${Object.entries(p.plan || {}).map(([h, ops]) => `<span class="mono">${esc(h)}</span> (${ops.length})`).join(", ")}` : "Not planned yet."}</div>` : ""}
    ${b.readme ? `<details class="panel readme" style="margin-top:.7rem"><summary>How this bundle works</summary>${marked.parse(b.readme)}</details>` : ""}
    <pre class="log mono" id="blog" hidden></pre>
    <div id="bbody" style="margin-top:.8rem"></div>
    <div id="fview"></div>`;
  if (b.deployable) { $("#bplan").onclick = () => bundlePlan(); $("#bapply").onclick = () => bundleApply(); }
  if (b.unit === "directory") renderItems(); else renderFiles(b.files);
  if (C.file) renderFile();
}

function renderFiles(files, title = "") {
  const hosts = fleet.hosts.map((h) => h.name);
  $("#bbody").innerHTML = `${title}<table class="files" style="width:100%">
    <tr><th>File on the host</th><th>Layers</th>${hosts.map((h) => `<th class="mono" style="font-size:11.5px">${esc(h)}</th>`).join("")}</tr>
    ${files.map((f) => `<tr data-rel="${esc(f.rel)}" class="${C.file === f.rel ? "on" : ""}">
      <td><span class="mono">${esc(f.out)}</span>${f.out !== f.rel ? ` <span class="muted" style="font-size:11.5px">from ${esc(f.rel)}</span>` : ""}
        <div class="muted" style="font-size:11.5px">${f.merge === "keys" ? "merged by key" : "whole file"}</div></td>
      <td>${f.layers.map((l) => chip(l)).join("")}</td>
      ${hosts.map((h) => { const src = C.b.hosts[h]?.files?.[f.out]; return `<td style="text-align:center">${src ? src.filter((s) => s !== "base").map((s) => chip(s)).join("") || `<span class="muted">base</span>` : `<span class="muted">—</span>`}</td>`; }).join("")}
    </tr>`).join("")}</table>
    <div class="row" style="margin-top:.5rem"><input id="newrel" class="mono" placeholder="new file, e.g. rules/foo.md" style="width:280px">
      <select id="newlayer">${allLayers().map((l) => `<option value="${esc(l)}">${esc(l)}</option>`).join("")}</select>
      <button class="b" id="newfile">Add file</button></div>`;
  $$("#bbody [data-rel]").forEach((tr) => (tr.onclick = () => { C.file = tr.dataset.rel; C.view = "compiled"; renderBundle(); $("#fview").scrollIntoView({ behavior: "smooth" }); }));
  $("#newfile").onclick = () => {
    const rel = $("#newrel").value.trim();
    if (!rel) return;
    C.file = rel; C.view = `layer:${$("#newlayer").value}`; renderBundle();
  };
}

function renderItems() {
  const b = C.b, hosts = fleet.hosts.map((h) => h.name);
  const items = new Set(Object.keys(b.marks));
  for (const f of b.files) items.add(f.rel.split("/")[0]);
  const rows = [...items].sort();
  $("#bbody").innerHTML = `<p class="muted" style="margin:.2rem 0 .5rem">One folder per item. A cell shows the layer whose copy that host gets; click it to skip or delete it there. Click a name for its files.</p>
    <table class="items" style="width:100%"><tr><th>${rows.length} items</th>${hosts.map((h) => `<th class="mono">${esc(h)}</th>`).join("")}</tr>
    ${rows.map((it) => `<tr><td><a href="#" data-item="${esc(it)}" style="color:inherit">${esc(it)}</a></td>${hosts.map((h) => {
      const hp = b.hosts[h] || {};
      const src = hp.items?.[it];
      const cell = src ? chip(src) : hp.skipped?.includes(it) ? `<span class="chip skip">skip</span>` : hp.delete?.includes(it) ? `<span class="chip delete">delete</span>` : `<span class="muted">—</span>`;
      return `<td class="c" data-it="${esc(it)}" data-h="${esc(h)}">${cell}</td>`;
    }).join("")}</tr>`).join("")}</table>`;
  $$("#bbody [data-item]").forEach((a) => (a.onclick = (e) => { e.preventDefault(); showItem(a.dataset.item); }));
  $$("#bbody td.c").forEach((td) => (td.onclick = (e) => itemMenu(e, td.dataset.it, td.dataset.h)));
}

function showItem(item) {
  C.item = item;
  const files = C.b.files.filter((f) => f.rel === item || f.rel.startsWith(item + "/"));
  const box = document.createElement("div");
  $("#fview").innerHTML = `<h2>${esc(item)}</h2>`;
  $("#fview").appendChild(box);
  box.innerHTML = `<table class="files" style="width:100%"><tr><th>File</th><th>Layers</th></tr>${files.map((f) => `<tr data-rel="${esc(f.rel)}"><td class="mono">${esc(f.rel)}</td><td>${f.layers.map((l) => chip(l)).join("")}</td></tr>`).join("")}</table>`;
  $$("[data-rel]", box).forEach((tr) => (tr.onclick = () => {
    const f = files.find((x) => x.rel === tr.dataset.rel);
    C.file = f.rel; C.view = `layer:${f.layers[f.layers.length - 1]}`; renderFile();
  }));
}

function closeMenu() { $$(".menu").forEach((m) => m.remove()); }
document.addEventListener("click", (e) => { if (!e.target.closest(".menu") && !e.target.closest("td.c")) closeMenu(); });

function itemMenu(e, item, host) {
  closeMenu();
  const h = fleet.hosts.find((x) => x.name === host);
  const marks = C.b.marks[item] || {};
  const opts = [[`Skip on ${host}`, `host:${host}`, "skip"], [`Delete on ${host}`, `host:${host}`, "delete"],
    ...h.groups.map((g) => [`Skip for group ${g}`, `group:${g}`, "skip"])];
  const mine = ["base", ...h.groups.map((g) => `group:${g}`), `host:${host}`];
  for (const [lid, kind] of Object.entries(marks)) if (mine.includes(lid)) opts.push([`Undo ${kind} in ${lid}`, lid, null]);
  const m = document.createElement("div");
  m.className = "menu";
  m.innerHTML = `<div class="h">${esc(item)} on ${esc(host)}</div>` + opts.map(([label], i) => `<div data-o="${i}">${esc(label)}</div>`).join("");
  m.style.left = `${e.pageX}px`; m.style.top = `${e.pageY}px`;
  document.body.appendChild(m);
  $$("[data-o]", m).forEach((el) => (el.onclick = async () => {
    const [, layer, state] = opts[el.dataset.o];
    closeMenu();
    try { await api(`/api/bundles/${C.b.name}/item`, { method: "POST", body: { layer, item, state } }); }
    catch (err) { return alert(err.message); }
    await openBundle(C.b.name, true); loadBundles(); loadHistory();
  }));
}

function fileInfo(rel) { return C.b.files.find((f) => f.rel === rel) || { rel, out: rel, layers: [], merge: "whole" }; }

function renderFile() {
  C.seq = (C.seq || 0) + 1;
  const f = fileInfo(C.file);
  const hostSel = `<select id="fhost">${fleet.hosts.map((h) => `<option ${h.name === C.host ? "selected" : ""}>${esc(h.name)}</option>`).join("")}</select>`;
  const layerTabs = f.layers.map((l) => `<span class="t ${C.view === `layer:${l}` ? "on" : ""}" data-v="layer:${esc(l)}">edit ${chip(l)}</span>`).join("");
  const missing = allLayers().filter((l) => !f.layers.includes(l));
  const box = $("#fview");
  box.innerHTML = `<h2 class="mono" style="margin-bottom:0">${esc(f.out)}</h2>
    <div class="tabs">
      <span class="t ${C.view === "compiled" ? "on" : ""}" data-v="compiled">What a host gets</span>
      ${C.b.deployable ? `<span class="t ${C.view === "diff" ? "on" : ""}" data-v="diff">Diff on host</span>` : ""}
      ${["compiled", "diff"].includes(C.view) ? hostSel : ""}
      <span style="width:1rem"></span>${layerTabs}
      <select id="addlayer"><option value="">+ override file for…</option>${missing.map((l) => `<option value="${esc(l)}">${esc(l)}</option>`).join("")}</select>
    </div>
    <div id="fbody"></div>`;
  $$(".tabs [data-v]", box).forEach((t) => (t.onclick = () => { C.view = t.dataset.v; renderFile(); }));
  if ($("#fhost")) $("#fhost").onchange = (e) => { C.host = e.target.value; renderFile(); };
  $("#addlayer").onchange = (e) => { if (e.target.value) { C.view = `layer:${e.target.value}`; renderFile(); } };
  if (C.view === "compiled") showCompiled(f);
  else if (C.view === "diff") showDiff();
  else showLayer(f, C.view.slice(6));
}

async function showCompiled(f) {
  const body = $("#fbody"), seq = C.seq;
  body.innerHTML = `<div class="muted">loading…</div>`;
  let r;
  try { r = await api(`/api/bundles/${C.b.name}/compiled?host=${encodeURIComponent(C.host)}&file=${encodeURIComponent(f.out)}`); }
  catch (e) { if (seq === C.seq) body.innerHTML = `<div class="msg err">${esc(e.message)}</div>`; return; }
  if (seq !== C.seq) return;   // a newer render replaced this view
  const own = r.own_keys.length ? `<p class="muted">Flotilla sets only <span class="mono">${esc(r.own_keys.join(", "))}</span> in this file; the rest of it on the host is left alone.</p>` : "";
  if (!r.explain) {
    body.innerHTML = `${own}<p>${esc(C.host)} gets this whole file from ${chip(r.sources[r.sources.length - 1])}.</p><pre class="log mono">${esc(r.text)}</pre>`;
    return;
  }
  body.innerHTML = `${own}<div class="row muted" style="margin-bottom:.4rem">${r.explain.length} keys for <b>${esc(C.host)}</b>. Rows tinted: set by a group or host override.
      <span style="flex:1"></span><label class="row"><input type="checkbox" id="raw"> show the file</label></div>
    <div id="ovslot"></div>
    <table class="explain" style="width:100%"><tr><th>Key</th><th>Value</th><th>Set by</th><th></th></tr>
    ${r.explain.map((row, i) => { const over = row.sources.some((s) => !s.startsWith("base")); return `<tr class="${over ? "ovr" : ""}">
      <td class="k">${esc(row.key)}</td><td class="v">${esc(short(row.value))}</td>
      <td>${row.sources.map((s) => { const op = /[+-]$/.test(s) ? s.slice(-1) : ""; return chip(op ? s.slice(0, -1) : s) + (op ? `<span class="muted">${op}</span>` : ""); }).join(" ")}</td>
      <td style="white-space:nowrap"><button class="b sm" data-ov="${i}">Override…</button>
        ${row.sources.filter((s) => !s.startsWith("base")).map((s) => s.replace(/[+-]$/, "")).filter((s, j, a) => a.indexOf(s) === j).map((s) => `<button class="b sm" data-clr="${i}" data-l="${esc(s)}" title="remove from ${esc(s)}">clear ${esc(layerShort(s))}</button>`).join("")}</td></tr>`; }).join("")}
    </table><pre class="log mono" id="rawtext" hidden>${esc(r.text)}</pre>`;
  $("#raw", body).onchange = (e) => ($("#rawtext", body).hidden = !e.target.checked);
  $$("[data-ov]", body).forEach((b) => (b.onclick = () => overrideBox(f, r.explain[b.dataset.ov])));
  $$("[data-clr]", body).forEach((b) => (b.onclick = async () => {
    const row = r.explain[b.dataset.clr];
    if (!confirm(`Remove the ${row.key} override from ${b.dataset.l}?`)) return;
    try { await api(`/api/bundles/${C.b.name}/override`, { method: "POST", body: { layer: b.dataset.l, file: f.rel, key: row.key, clear: true } }); }
    catch (e) { return alert(e.message); }
    await refreshBundle();
  }));
}

const short = (v) => { const s = typeof v === "string" ? v : JSON.stringify(v); return s.length > 220 ? s.slice(0, 220) + "…" : s; };

function overrideBox(f, row) {
  const h = fleet.hosts.find((x) => x.name === C.host);
  const targets = [...h.groups.map((g) => `group:${g}`), `host:${h.name}`,
    ...allLayers().filter((l) => l !== "base" && !h.groups.map((g) => `group:${g}`).includes(l) && l !== `host:${h.name}`)];
  const isList = Array.isArray(row.value);
  $("#ovslot").innerHTML = `<div class="ovbox">
    <b style="grid-column:1/-1" class="mono">${esc(row.key)}</b>
    <label>For</label><select id="ovl">${targets.map((l) => `<option value="${esc(l)}">${esc(l)}</option>`).join("")}</select>
    <label>Change</label><select id="ovop">
      <option value="">set this value</option>
      ${isList ? `<option value="+">add these items to the list</option><option value="-">remove these items from the list</option>` : ""}
      <option value="!">replace whole (no merge)</option></select>
    <label>Value (YAML)</label><textarea id="ovv">${esc(JSON.stringify(row.value, null, 1))}</textarea>
    <span></span><div class="row"><button class="b pri" id="ovsave">Save override</button><button class="b" id="ovcancel">Cancel</button>
      <span class="muted">Writes only this key into ${esc(f.rel)} of that layer. Use an aside:// reference for secrets.</span></div>
    <div id="ovmsg" style="grid-column:1/-1"></div></div>`;
  $("#ovop").onchange = (e) => { if (["+", "-"].includes(e.target.value)) $("#ovv").value = "[]"; };
  $("#ovcancel").onclick = () => ($("#ovslot").innerHTML = "");
  $("#ovsave").onclick = async () => {
    try {
      await api(`/api/bundles/${C.b.name}/override`, { method: "POST", body: { layer: $("#ovl").value, file: f.rel, key: row.key, op: $("#ovop").value, value: $("#ovv").value } });
    } catch (e) { $("#ovmsg").innerHTML = `<div class="msg err">${esc(e.message)}</div>`; return; }
    await refreshBundle();
  };
}

async function refreshBundle() {
  await openBundle(C.b.name, true);
  loadBundles(); loadHistory();
}

async function showDiff() {
  const body = $("#fbody");
  body.innerHTML = `<pre class="log mono diff">loading…</pre>`;
  try {
    const r = await api(`/api/bundles/${C.b.name}/diff?host=${encodeURIComponent(C.host)}`);
    $("pre", body).innerHTML = r.diffs.length ? r.diffs.map((x) => colorDiff(x.diff)).join("\n") : esc(r.note || `${C.host} already has what this bundle puts there.`);
  } catch (e) { $("pre", body).textContent = e.message; }
}

let editor = null;
async function showLayer(f, layer) {
  const path = statePath(C.b, layer, f.rel);
  const body = $("#fbody"), seq = C.seq;
  const { content, new: isNew } = await api(`/api/state/file?path=${encodeURIComponent(path)}`);
  if (seq !== C.seq) return;   // a newer render replaced this view
  body.innerHTML = `<div class="edit"><div>
      <div class="row" style="margin-bottom:.4rem"><span class="mono muted">${esc(path)}</span>${isNew ? `<span class="pill">new file</span>` : ""}<span style="flex:1"></span>
        ${isNew ? "" : `<button class="b" id="fdel">Delete this file</button>`}<button class="b pri" id="fsave">Save</button></div>
      <div class="muted" style="font-size:12.5px;margin-bottom:.4rem">${layer === "base" ? "The full file for every host." : f.merge === "keys" ? "Only the keys this layer changes: <span class=mono>a.b: value</span>, <span class=mono>list+: [x]</span> adds, <span class=mono>list-: [x]</span> removes, <span class=mono>key!:</span> replaces, <span class=mono>key: ~</span> deletes." : "Replaces the whole file for this layer's hosts."}</div>
      <div id="editor"></div><div id="fmsg"></div></div>
    <div>
      <div class="panel"><h4>Models</h4><input id="model-q" placeholder="Search models…" style="width:100%">
        <div class="row" style="margin:.4rem 0"><select id="model-effort"><option value="">no effort</option>${EFFORTS.map((e) => `<option>${e}</option>`).join("")}</select><span class="muted">click to insert</span></div>
        <div class="models" id="models"></div></div>
      <div class="panel"><h4>Store a secret</h4><div style="display:grid;gap:.4rem">
        <select id="sec-acc">${(fleet.vault_accounts || []).map((a) => `<option>${esc(a)}</option>`).join("")}</select>
        <input id="sec-title" placeholder="Title (e.g. flotilla: omp models.yml foo apiKey)">
        <input id="sec-val" type="password" placeholder="Secret value">
        <button class="b" id="sec-save">Store in aside-vault and insert reference</button></div></div>
    </div></div>`;
  editor = new EditorView({
    parent: $("#editor"), doc: content,
    extensions: [basicSetup, yaml(), autocompletion({ override: [modelCompletions] }), EditorView.lineWrapping,
      ...(matchMedia("(prefers-color-scheme: dark)").matches ? [oneDark] : [])],
  });
  renderModels();
  if (C.flash) { $("#fmsg").innerHTML = C.flash; C.flash = ""; }
  $("#model-q").oninput = renderModels;
  $("#fsave").onclick = async () => {
    try {
      const r = await api("/api/state/file", { method: "PUT", body: { path, content: editor.state.doc.toString() } });
      C.flash = `<div class="msg okm">Saved and committed. ${r.warnings.map(esc).join("<br>")} Re-planning in the background; Plan or Apply above to roll it out.</div>`;
      await openBundle(C.b.name, true); loadBundles(); loadHistory();
    } catch (e) { $("#fmsg").innerHTML = `<div class="msg err">${esc(e.message)}</div>`; }
  };
  if ($("#fdel")) $("#fdel").onclick = async () => {
    if (!confirm(`Delete ${path}?`)) return;
    await api(`/api/state/file?path=${encodeURIComponent(path)}`, { method: "DELETE" });
    C.view = "compiled"; await openBundle(C.b.name, true); loadBundles(); loadHistory();
  };
  $("#sec-save").onclick = async () => {
    const title = $("#sec-title").value.trim(), value = $("#sec-val").value;
    if (!title || !value) return alert("Title and value are required.");
    const { ref } = await api("/api/secrets", { method: "POST", body: { account: $("#sec-acc").value, title, value } });
    $("#sec-val").value = "";
    insert(ref);
  };
}

function modelCompletions(ctx) {
  const word = ctx.matchBefore(/[\w./@:-]+/);
  if (!word || (word.from === word.to && !ctx.explicit) || !C.models.length) return null;
  const text = word.text;
  const colon = text.indexOf(":");
  if (colon > 0) {
    const m = C.models.find((x) => x.selector === text.slice(0, colon));
    if (!m) return null;
    return { from: word.from + colon + 1, options: (m.thinking.length ? m.thinking : EFFORTS).map((e) => ({ label: e, type: "keyword" })) };
  }
  if (text.length < 2) return null;
  return {
    from: word.from,
    options: C.models.map((m) => ({ label: m.selector, detail: `${m.kind}${m.thinking.length ? " · " + m.thinking.join("/") : ""}`, info: m.name, type: "constant" })),
    validFor: /^[\w./@-]*$/,
  };
}

function renderModels() {
  const q = ($("#model-q")?.value || "").toLowerCase();
  const list = C.models.filter((m) => !q || m.selector.toLowerCase().includes(q) || (m.name || "").toLowerCase().includes(q)).slice(0, 200);
  $("#models").innerHTML = list.map((m) => `<div data-sel="${esc(m.selector)}" title="${esc(m.name)} · ${esc(m.kind)}"><span class="mono">${esc(m.selector)}</span> <span class="muted">${esc(m.thinking.join("/"))}</span></div>`).join("") || `<span class="muted">loading the catalog…</span>`;
  $$("#models [data-sel]").forEach((el) => (el.onclick = () => {
    const eff = $("#model-effort").value;
    insert(el.dataset.sel + (eff ? `:${eff}` : ""));
  }));
}

function insert(text) {
  const { from, to } = editor.state.selection.main;
  editor.dispatch({ changes: { from, to, insert: text }, selection: { anchor: from + text.length } });
  editor.focus();
}

async function bundlePlan() {
  const { job } = await api(`/api/bundles/${C.b.name}/plan`, { method: "POST", body: {} });
  await follow(job, $("#blog"));
  await refreshBundle();
}

async function bundleApply() {
  const { job } = await api(`/api/bundles/${C.b.name}/plan`, { method: "POST", body: {} });
  await follow(job, $("#blog"));
  const info = await api(`/api/jobs/${job}`);
  const plan = info.result?.plan || {};
  const targets = Object.keys(plan);
  if (!targets.length) { await refreshBundle(); return alert(`${C.b.name}: every reachable host is in sync.`); }
  const lines = targets.map((h) => `${h}:\n  ${plan[h].join("\n  ")}`).join("\n");
  if (!confirm(`Apply ${C.b.name} to ${targets.join(", ")}?\n\n${lines}`)) return;
  const run = await api(`/api/bundles/${C.b.name}/apply`, { method: "POST", body: { hosts: targets } });
  await follow(run.job, $("#blog"));
  await refreshBundle();
}

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
