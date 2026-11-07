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
  return text.replace(/\n$/, "").split("\n").map((l) => {
    const cls = l.startsWith("+++") || l.startsWith("---") ? "file" : l.startsWith("+") ? "add" : l.startsWith("-") ? "del" : l.startsWith("@@") ? "hunk" : "";
    return `<div class="${cls}">${esc(l)}</div>`;
  }).join("");
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
document.addEventListener("keydown", (e) => { if (e.key === "Escape") $("#drawer").hidden = true; });

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
    <h4>File diff</h4><div class="log mono diff" id="cdiff">loading…</div>`;
  $("#dclose").onclick = () => (d.hidden = true);
  try {
    const r = await api(`/api/components/${c}/diff?host=${encodeURIComponent(h)}`);
    $("#cdiff").innerHTML = r.diffs.length ? r.diffs.map((x) => colorDiff(x.diff)).join("") : esc(r.note || "files match");
  } catch (e) { $("#cdiff").textContent = e.message; }
}
$("#plan-all").addEventListener("click", async () => { for (const c of Object.keys(fleet.components)) await plan(c); });

// ------------------------------------------------------------------ config
// The state directory exactly as it is on prod: a file tree and one code
// editor. A bundle's overview shows which files groups and hosts override;
// a group or host file can be compared with base; Apply shows what would
// change on every host before it changes anything.
const C = {
  files: [], open: new Set(["bundles"]), bundles: [], models: [],
  view: null,     // overview | edit | review
  bundle: null,   // bundle shown in overview / review
  path: null, meta: null, saved: "", isNew: false, editor: null, diffOn: false,
};
const dark = () => (matchMedia("(prefers-color-scheme: dark)").matches ? [oneDark] : []);
const layerRel = (lid) => (lid === "base" ? "base" : `${lid.split(":")[0]}s/${lid.split(":")[1]}`);
const layerName = (lid) => (lid === "base" ? "base" : lid.split(":")[1]);
const isDirty = () => !!C.editor && C.editor.state.doc.toString() !== C.saved;
const leave = () => !isDirty() || confirm("Discard unsaved changes?");
window.addEventListener("beforeunload", (e) => { if (isDirty()) { e.preventDefault(); e.returnValue = ""; } });

function msg(html, cls = "") { $("#cmsg").innerHTML = html ? `<div class="msg ${cls}">${html}</div>` : ""; }
function dropEditor() { C.editor?.destroy(); C.editor = null; C.saved = ""; }

async function initConfig() {
  if (!C.models.length) api("/api/catalog/models").then((m) => (C.models = m)).catch(() => {});
  await Promise.all([loadTree(), loadBundleStatus()]);
  if (!C.view) {
    const first = C.bundles.find((b) => b.name === "omp") || C.bundles[0];
    if (first) openOverview(first.name);
  }
}

async function loadTree() { C.files = await api("/api/state/tree"); renderTree(); }

async function loadBundleStatus() {
  try { C.bundles = await api("/api/bundles"); } catch { return; }
  renderTree();
  const s = $("#bstat");
  if (s) s.textContent = statusText(C.bundles.find((b) => b.name === C.bundle));
}
setInterval(() => { if (!$("#tab-config").hidden) loadBundleStatus(); }, 20000);

function statusText(b) {
  if (!b?.deployable) return "";
  const p = b.plan;
  if (!p) return "checking hosts…";
  const diff = Object.keys(p.plan || {}), off = Object.keys(p.errors || {});
  return [diff.length ? `not applied on ${diff.join(", ")}` : "applied everywhere",
    off.length ? `${off.join(", ")} not reachable` : ""].filter(Boolean).join(" · ");
}

function renderTree() {
  const root = { dirs: {}, files: [] };
  for (const p of C.files) {
    let node = root;
    for (const d of p.split("/").slice(0, -1)) node = node.dirs[d] ||= { dirs: {}, files: [] };
    node.files.push(p);
  }
  const byPrefix = Object.fromEntries(C.bundles.map((b) => [b.prefix, b]));
  const rows = [];
  const walk = (node, at, depth) => {
    for (const name of Object.keys(node.dirs).sort()) {
      const p = at ? `${at}/${name}` : name;
      const b = byPrefix[p];
      const n = b ? Object.keys(b.plan?.plan || {}).length : 0;
      const on = b && C.bundle === b.name && C.view !== "edit";
      rows.push(`<div class="r dir${b ? " b" : ""}${on ? " on" : ""}" data-dir="${esc(p)}"${b ? ` data-b="${esc(b.name)}" title="${esc(b.description)}"` : ""} style="padding-left:${depth * 13 + 4}px">
        <span class="t">${C.open.has(p) ? "▾" : "▸"}</span><span class="n">${esc(name)}</span>${n ? `<span class="cnt" title="not applied on ${n} host(s)">● ${n}</span>` : ""}${depth ? `<span class="x" data-del="${esc(p)}" title="Delete this folder">×</span>` : ""}</div>`);
      if (C.open.has(p)) walk(node.dirs[name], p, depth + 1);
    }
    for (const f of node.files.sort()) {
      const on = C.view === "edit" && f === C.path;
      rows.push(`<div class="r${on ? " on" : ""}" data-f="${esc(f)}" style="padding-left:${depth * 13 + 4}px"><span class="t"></span><span class="n">${esc(f.split("/").pop())}</span><span class="x" data-del="${esc(f)}" title="Delete this file">×</span></div>`);
    }
  };
  walk(root, "", 0);
  $("#tree").innerHTML = rows.join("");
  $$("#tree [data-dir]").forEach((el) => (el.onclick = (e) => {
    const p = el.dataset.dir;
    if (el.dataset.b && !e.target.classList.contains("t")) { C.open.add(p); openOverview(el.dataset.b); return; }
    C.open.has(p) ? C.open.delete(p) : C.open.add(p);
    renderTree();
  }));
  $$("#tree [data-f]").forEach((el) => (el.onclick = () => openFile(el.dataset.f)));
  $$("#tree [data-del]").forEach((el) => (el.onclick = (e) => { e.stopPropagation(); deletePath(el.dataset.del); }));
}

function expandTo(path) {
  const parts = path.split("/");
  for (let i = 1; i < parts.length; i++) C.open.add(parts.slice(0, i).join("/"));
}

// ---- overview: which files groups and hosts override
async function openOverview(name) {
  if (!leave()) return;
  dropEditor();
  C.view = "overview"; C.bundle = name; C.path = null;
  let o;
  try { o = await api(`/api/bundles/${encodeURIComponent(name)}/overview`); }
  catch (e) { $("#chead").innerHTML = ""; $("#cbody").innerHTML = ""; msg(esc(e.message), "err"); return; }
  expandTo(`${o.prefix}/x`);
  renderTree();
  $("#chead").innerHTML = `<div class="head"><b style="font-size:1.05rem">${esc(name)}</b>
      <span class="muted mono">${esc(o.dest || "not copied to hosts; the components read it")}</span>
      <span class="grow"></span><span class="muted" id="bstat">${esc(statusText(o))}</span>
      ${o.deployable ? `<button class="b sm danger" id="tapply">Apply…</button>` : ""}</div>
    <div class="muted" style="font-size:12.5px;margin-bottom:.5rem">Click a name to edit base, a label to edit that <span class="lab group">group</span> or <span class="lab host">host</span> override${o.unit === "directory" ? " (a copy of the whole folder)" : ""}.</div>`;
  const others = (r) => r.layers.filter((l) => l !== "base");
  $("#cbody").innerHTML = `<table class="ov">${o.rows.map((r, i) => `<tr>
      <td class="mono">${r.open.base ? `<a href="#" data-open="${esc(r.open.base)}">${esc(r.name)}</a>` : `<span class="muted" title="not in base">${esc(r.name)}</span>`}</td>
      <td>${others(r).map((l) => `<a href="#" class="lab ${l.split(":")[0]}" data-open="${esc(r.open[l])}" title="${esc(l.replace(":", " "))}">${esc(layerName(l))}</a>`).join("")}${Object.entries(r.marks).map(([l, k]) => `<span class="lab mark" title="${esc(k)} in ${esc(layerRel(l))}/_layer.yml">${esc(layerName(l))}</span>`).join("")}${o.unit === "file" ? `<a href="#" class="plus muted" data-add="${i}" title="Add an override for a group or host">+</a>` : ""}</td></tr>`).join("")}</table>`;
  msg("");
  $$("#cbody [data-open]").forEach((a) => (a.onclick = (e) => { e.preventDefault(); openFile(a.dataset.open); }));
  $$("#cbody [data-add]").forEach((a) => (a.onclick = (e) => { e.preventDefault(); addOverride(a, o, o.rows[a.dataset.add]); }));
  if ($("#tapply")) $("#tapply").onclick = () => openReview(name);
}

function addOverride(anchor, o, row) {
  const have = new Set(row.layers);
  const choices = [...Object.keys(fleet.groups).map((g) => `group:${g}`), ...fleet.hosts.map((h) => `host:${h.name}`)].filter((l) => !have.has(l));
  const sel = document.createElement("select");
  sel.innerHTML = `<option value="">override for…</option>` + choices.map((l) => `<option value="${esc(l)}">${esc(l.replace(":", " "))}</option>`).join("");
  anchor.replaceWith(sel);
  sel.focus();
  sel.onchange = async () => {
    if (!sel.value) return;
    const path = `${o.prefix}/${layerRel(sel.value)}/${row.name}`;
    let prefill = "";
    if (row.merge === "keys") {
      prefill = row.name.endsWith(".json") ? "{\n}\n"
        : `# ${layerName(sel.value)} only: the keys that differ from the layers below.\n# a.b: value · list+: [add] · list-: [remove] · key!: replaces · key: ~ deletes\n`;
    } else if (row.open.base) {
      prefill = (await api(`/api/state/file?path=${encodeURIComponent(row.open.base)}`)).content;
    }
    openFile(path, prefill);
  };
}

// ---- editor
async function openFile(path, prefill = null) {
  if (C.view === "edit" && path === C.path) return;
  if (!leave()) return;
  let r;
  try { r = await api(`/api/state/file?path=${encodeURIComponent(path)}`); } catch (e) { return msg(esc(e.message), "err"); }
  dropEditor();
  Object.assign(C, { view: "edit", path, meta: r.meta, isNew: !!r.new, diffOn: false, bundle: r.meta?.bundle || null });
  C.saved = r.new ? "" : r.content;
  expandTo(path);
  renderTree();
  renderEditorHead();
  msg("");
  if (r.binary) { $("#cbody").innerHTML = `<p class="muted">Binary file: not shown.</p>`; $("#tsave").disabled = true; return; }
  $("#cbody").innerHTML = `<div id="editor"></div><div id="diffbox" hidden></div>`;
  C.editor = new EditorView({
    parent: $("#editor"), doc: prefill ?? r.content,
    extensions: [basicSetup, yaml(), autocompletion({ override: [modelCompletions] }), EditorView.lineWrapping,
      EditorView.updateListener.of((u) => { if (u.docChanged) $("#tdirty").hidden = !isDirty(); }), ...dark()],
  });
  $("#tdirty").hidden = !isDirty();
}

function crumbs(path, m) {
  const parts = path.split("/");
  const prefixLen = m ? m.prefix.split("/").length : 0;
  return parts.map((p, i) => (m && i === prefixLen - 1 ? `<a href="#" data-ov="${esc(m.bundle)}">${esc(p)}</a>`
    : i === parts.length - 1 ? `<b>${esc(p)}</b>` : esc(p))).join(" / ");
}

function renderEditorHead() {
  const m = C.meta;
  const canDiff = m && m.rel && m.layer && m.layer !== "base";
  $("#chead").innerHTML = `<div class="head">
      <span class="mono">${crumbs(C.path, m)}</span>${C.isNew ? `<span class="muted">(new)</span>` : ""}<span class="warn" id="tdirty" title="unsaved changes" hidden>●</span>
      <span class="grow"></span>
      ${canDiff ? `<button class="b sm" id="tdiff">${C.diffOn ? "Edit" : "Diff vs base"}</button>` : ""}
      <button class="b sm" id="tsec">Store a secret</button>
      ${C.isNew ? "" : `<button class="b sm" id="tdel">Delete</button>`}
      <button class="b sm pri" id="tsave" title="Save (Ctrl/⌘ S)">Save</button></div>
    <form class="pop" id="secret" hidden>
      <select id="sec-acc">${(fleet.vault_accounts || []).map((a) => `<option>${esc(a)}</option>`).join("")}</select>
      <input id="sec-title" placeholder="Title, e.g. flotilla: omp models.yml foo apiKey" style="flex:1;min-width:220px">
      <input id="sec-val" type="password" placeholder="Secret value">
      <button class="b sm">Store and insert its aside:// reference</button></form>`;
  $$("#chead [data-ov]").forEach((a) => (a.onclick = (e) => { e.preventDefault(); openOverview(a.dataset.ov); }));
  if ($("#tdiff")) $("#tdiff").onclick = toggleDiff;
  if ($("#tdel")) $("#tdel").onclick = () => deletePath(C.path);
  $("#tsave").onclick = save;
  $("#tsec").onclick = () => { $("#secret").hidden = !$("#secret").hidden; };
  $("#secret").onsubmit = storeSecret;
  $("#tdirty").hidden = !isDirty();
}

async function toggleDiff() {
  C.diffOn = !C.diffOn;
  $("#tdiff").textContent = C.diffOn ? "Edit" : "Diff vs base";
  $("#editor").hidden = C.diffOn;
  const box = $("#diffbox");
  box.hidden = !C.diffOn;
  if (!C.diffOn) return C.editor.focus();
  box.innerHTML = `<p class="muted">comparing…</p>`;
  try {
    const r = await api("/api/state/compare", { method: "POST", body: { path: C.path, content: C.editor.state.doc.toString() } });
    box.innerHTML = (r.note ? `<p class="muted">${esc(r.note)}</p>` : "")
      + (r.diff ? `<div class="log mono diff">${colorDiff(r.diff)}</div>` : `<p class="muted">Same as base.</p>`);
  } catch (e) { box.innerHTML = `<div class="msg err">${esc(e.message)}</div>`; }
}

async function save() {
  if (!C.editor) return;
  const content = C.editor.state.doc.toString();
  try {
    const r = await api("/api/state/file", { method: "PUT", body: { path: C.path, content } });
    C.saved = content;
    if (C.isNew) { C.isNew = false; await loadTree(); renderEditorHead(); }
    $("#tdirty").hidden = true;
    const b = C.meta?.deployable ? C.meta.bundle : null;
    msg(`Saved.${r.warnings.length ? ` ${esc(r.warnings.join("; "))}.` : ""}${b ? ` Hosts get it when you <a href="#" id="toapply">apply ${esc(b)}</a>.` : ""}`, "okm");
    if ($("#toapply")) $("#toapply").onclick = (e) => { e.preventDefault(); openReview(b); };
    setTimeout(loadBundleStatus, 8000);
  } catch (e) { msg(esc((e.data?.problems || [e.message]).join("\n")), "err"); }
}

async function deletePath(path) {
  const isDir = !C.files.includes(path);
  const n = C.files.filter((f) => f.startsWith(path + "/")).length;
  if (!confirm(isDir ? `Delete the folder ${path} and its ${n} file(s)?` : `Delete ${path}?`)) return;
  try { await api(`/api/state/file?path=${encodeURIComponent(path)}`, { method: "DELETE" }); }
  catch (e) { return alert((e.data?.problems || [e.message]).join("\n")); }
  const hit = C.view === "edit" && (C.path === path || C.path.startsWith(path + "/"));
  await loadTree();
  if (hit) { const b = C.meta?.bundle; dropEditor(); C.view = null; if (b) openOverview(b); else { $("#chead").innerHTML = ""; $("#cbody").innerHTML = ""; } }
  else if (C.view === "overview") openOverview(C.bundle);
}

async function storeSecret(e) {
  e.preventDefault();
  const title = $("#sec-title").value.trim(), value = $("#sec-val").value;
  if (!title || !value) return alert("Title and value are required.");
  try {
    const { ref } = await api("/api/secrets", { method: "POST", body: { account: $("#sec-acc").value, title, value } });
    $("#sec-val").value = "";
    $("#secret").hidden = true;
    const { from, to } = C.editor.state.selection.main;
    C.editor.dispatch({ changes: { from, to, insert: ref }, selection: { anchor: from + ref.length } });
    C.editor.focus();
  } catch (err) { alert(err.message); }
}

document.addEventListener("keydown", (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key === "s" && C.view === "edit" && !$("#tab-config").hidden) { e.preventDefault(); save(); }
});

$("#newbtn").onclick = () => {
  const f = $("#newform");
  f.hidden = !f.hidden;
  if (f.hidden) return;
  const at = C.view === "edit" && C.path ? C.path.slice(0, C.path.lastIndexOf("/") + 1)
    : C.bundle ? `${C.bundles.find((b) => b.name === C.bundle)?.prefix || ""}/` : "";
  $("#newpath").value = at;
  $("#newpath").focus();
};
$("#newform").onsubmit = (e) => {
  e.preventDefault();
  const p = $("#newpath").value.trim().replace(/^\/+/, "");
  if (!p || p.endsWith("/")) return;
  $("#newform").hidden = true;
  openFile(p);
};
$("#newpath").onkeydown = (e) => { if (e.key === "Escape") $("#newform").hidden = true; };

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

// ---- apply: what changes on every host, then apply
async function openReview(name) {
  if (!leave()) return;
  dropEditor();
  Object.assign(C, { view: "review", bundle: name, path: null });
  renderTree();
  msg("");
  $("#chead").innerHTML = `<div class="head"><b style="font-size:1.05rem">Apply ${esc(name)}</b><span class="grow"></span><button class="b sm" id="rback">Back</button></div>`;
  $("#rback").onclick = () => openOverview(name);
  $("#cbody").innerHTML = `<p class="muted">Checking every host…</p>`;
  let r;
  try { r = await api(`/api/bundles/${encodeURIComponent(name)}/review`, { method: "POST" }); }
  catch (e) { $("#cbody").innerHTML = ""; return msg(esc(e.message), "err"); }
  if (C.view !== "review" || C.bundle !== name) return;
  const hosts = Object.keys(r.hosts), off = Object.entries(r.errors);
  $("#cbody").innerHTML = hosts.map((h) => `<h2 class="mono" style="margin:1rem 0 .3rem">${esc(h)}</h2>
      <div class="muted mono">${r.hosts[h].ops.map(esc).join("<br>")}</div>
      ${r.hosts[h].diffs.length ? `<div class="log mono diff">${r.hosts[h].diffs.map((d) => colorDiff(d.diff)).join("")}</div>` : ""}`).join("")
    + (r.in_sync.length ? `<p class="muted">Already applied: ${esc(r.in_sync.join(", "))}</p>` : "")
    + (off.length ? `<p class="bad">Not reachable: ${off.map(([h, m]) => `${esc(h)} (${esc(m.slice(0, 90))})`).join(", ")}</p>` : "")
    + (hosts.length ? `<div class="row" style="margin-top:1rem"><button class="b danger" id="rgo">Apply to ${esc(hosts.join(", "))}</button></div>` : `<p>Nothing to apply.</p>`)
    + `<pre class="log mono" id="rlog" hidden></pre>`;
  loadBundleStatus();
  if (!hosts.length) return;
  $("#rgo").onclick = async () => {
    $("#rgo").disabled = true;
    const { job } = await api(`/api/bundles/${encodeURIComponent(name)}/apply`, { method: "POST", body: { hosts } });
    const done = await follow(job, $("#rlog"));
    msg(done.status === "ok" ? `Applied ${esc(name)} to ${esc(hosts.join(", "))}.` : "Apply failed; see the log above.", done.status === "ok" ? "okm" : "err");
    setTimeout(loadBundleStatus, 4000);
  };
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
window.addEventListener("hashchange", () => showTab((location.hash || "#hosts").slice(1)));
