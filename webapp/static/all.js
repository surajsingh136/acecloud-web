/* ACE Hub - /all frontend */
"use strict";

const $ = (id) => document.getElementById(id);

/* ---- Sites shown on the hub. Add more websites here later. ---- */
const SITES = [
  { id: "acecloud", kind: "ace", name: "AceCloud", logo: "/logo.svg",
    desc: "Windows Spot & Linux proxy VMs", config: true, url: "/", api: "/api" },
  { id: "hostzop", kind: "hostzop", name: "Hostzop", logo: "/hostzop-logo.svg",
    desc: "Proxy & RDP server maker (API key login)", config: true, url: "/hostzop", api: "/api/hostzop" },
  { id: "excloud", kind: "excloud", name: "ExCloud", logo: "/excloud-logo.svg",
    desc: "Indian cloud proxy & RDP VMs (API key login)", config: true, url: "/excloud", api: "/api/excloud" },
  // Example for future websites:
  // { id: "other", name: "Other Tool", logo: "/logo.svg",
  //   desc: "Short description", config: false, url: "https://example.com" },
];

/* ---------------- transport (PIN aware) ---------------- */
async function J(url, opts) {
  opts = opts || {};
  opts.headers = opts.headers || {};
  const pin = localStorage.getItem("acepin");
  if (pin) opts.headers["X-Pin"] = pin;
  const r = await fetch(url, opts);
  if (r.status === 401) {
    const p = prompt("Enter website PIN:");
    if (p) { localStorage.setItem("acepin", p); return J(url, opts); }
    throw new Error("no pin");
  }
  return r.json();
}
const POST = (u, o) => J(u, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(o) });

/* ---------------- ui helpers ---------------- */
function toast(m) { const t = $("toast"); t.textContent = m; t.style.display = "block"; clearTimeout(t._h); t._h = setTimeout(() => t.style.display = "none", 1900); }
function hide(id) { $(id).style.display = "none"; }
function esc(s) { return String(s == null ? "" : s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }

/* ---------------- hub ---------------- */
function renderSites() {
  $("sites").innerHTML = SITES.map(s => `
    <div class="card" onclick="openSite('${s.id}')">
      <img class="logo" src="${s.logo}" alt="${esc(s.name)}">
      <div class="cname">${esc(s.name)}</div>
      <div class="cdesc">${esc(s.desc)}</div>
      <span class="chip ${s.config ? "" : "go"}">${s.config ? "Configure &amp; run" : "Open"}</span>
    </div>`).join("");
}

function openSite(id) {
  const s = SITES.find(x => x.id === id);
  if (!s) return;
  if (s.config) openConfig(s);
  else window.location.href = s.url;
}

function addPlaceholder() { toast("Coming soon - more websites will be added here."); }

/* ---------------- account config modal ---------------- */
let EDIT_ID = "";
let ACC = {};
let DETECT = null;
let CUR = SITES[0];

async function openConfig(site) {
  CUR = site;
  $("cfgTitle").textContent = site.name + " - Configure";
  const lg = $("cfgLogo");
  if (lg) lg.src = site.logo;
  resetForm();
  $("cfg").style.display = "flex";
  await loadAccounts();
}

function applyKind() {
  const k = (CUR && CUR.kind) || "ace";
  $("fieldsAce").style.display = k === "ace" ? "" : "none";
  $("fieldsKey").style.display = k === "ace" ? "none" : "";
  $("fieldsHz").style.display = k === "hostzop" ? "" : "none";
  $("fieldsEx").style.display = k === "excloud" ? "" : "none";
}

function resetForm() {
  EDIT_ID = "";
  DETECT = null;
  applyKind();
  $("formTitle").textContent = "Add account";
  ["alabel", "aemail", "apass", "akey", "akey2", "anote", "aproj", "anetmum", "anetnoi",
   "ahzbill", "ahzloc", "aexzone", "aexsub"].forEach(i => { if ($(i)) $(i).value = ""; });
  $("aregion").value = "ap-south-mum-2";
  $("detectBox").style.display = "none";
  $("ause").checked = true;
}

async function loadAccounts() {
  let d;
  try { d = await J(CUR.api + "/accounts"); }
  catch (e) { $("acclist").innerHTML = '<div class="err">PIN required.</div>'; return; }
  ACC = {};
  const list = d.accounts || [];
  list.forEach(a => { ACC[a.id] = a; });
  const keySite = CUR.kind !== "ace";
  $("acclist").innerHTML =
    '<table class="acct"><thead><tr><th>Use</th><th>Label</th><th>' +
    (keySite ? "API key" : "Email") + '</th><th></th></tr></thead><tbody>' +
    list.map(a => `
      <tr class="${a.id === d.active ? "act" : ""}">
        <td>${a.id === d.active ? '<span class="dot" title="active"></span>'
             : `<button class="mini" onclick="activate('${a.id}')">Use</button>`}</td>
        <td>${esc(a.label)}${a.editable ? "" : ' <span class="ro">(read-only)</span>'}</td>
        <td>${esc(keySite ? ((a.has_key || a.apikey) ? "set" : "-") : (a.email || "-"))}</td>
        <td class="rowbtns">
          ${a.editable ? `<button class="mini" onclick="editAccount('${a.id}')">Edit</button>
          <button class="mini del" onclick="delAccount('${a.id}')">Del</button>` : ""}
          <button class="mini go" onclick="window.location.href='${CUR.url}'">Open</button>
        </td>
      </tr>`).join("") +
    "</tbody></table>";
  const act = list.find(a => a.id === d.active) || {};
  $("activeInfo").textContent = "Active account: " +
    (keySite ? (act.label || act.id || "-") : (act.email || "-")) +
    (act.label && !keySite ? " (" + act.label + ")" : "");
}

async function detect() {
  if (CUR.kind !== "ace") return;
  const email = $("aemail").value.trim();
  const password = $("apass").value;
  if (!email || !password) { toast("Enter email and password first"); return; }
  toast("Detecting...");
  let d;
  try { d = await POST("/api/accounts/detect", { email, password }); }
  catch (e) { toast("Failed"); return; }
  if (!d.ok) { toast(d.msg || "Detect failed"); return; }
  DETECT = d;
  const projs = d.projects || [];
  $("detectBox").style.display = "block";
  $("projSel").innerHTML = projs.map(p =>
    `<option value="${esc(p.id)}">${esc(p.name)} (${esc(String(p.id).slice(0, 8))}...)</option>`).join("");
  if (projs[0]) $("aproj").value = projs[0].id;
  renderNets();
  toast("Details detected");
}

const NETMAP = { "ap-south-mum-2": "anetmum", "ap-south-noi-1": "anetnoi" };
function renderNets() {
  if (!DETECT) return;
  $("aproj").value = $("projSel").value;
  const proj = (DETECT.projects || []).find(p => p.id === $("projSel").value) || {};
  const nets = DETECT.networks || {};
  const regions = proj.regions || Object.keys(nets);
  $("netBox").innerHTML = regions.map(r => {
    const list = nets[r] || [];
    return `<label>Network (${esc(r)})</label><select data-region="${esc(r)}" onchange="netChanged('${esc(r)}',this.value)">` +
      list.map(n => `<option value="${esc(n.id)}">${esc(n.name)}${n.external ? " (external)" : ""}</option>`).join("") +
      `</select>`;
  }).join("");
  regions.forEach(r => {
    const sel = $("netBox").querySelector(`select[data-region="${r}"]`);
    const inp = NETMAP[r];
    if (inp) $(inp).value = sel ? sel.value : "";
  });
  $("detectBox").style.display = "block";
}
function netChanged(region, val) {
  const inp = NETMAP[region];
  if (inp) $(inp).value = val;
}

async function saveAccount() {
  let body;
  if (CUR.kind === "ace") {
    body = {
      id: EDIT_ID, label: $("alabel").value.trim(), email: $("aemail").value.trim(),
      password: $("apass").value, api_key: $("akey").value.trim(),
      note: $("anote").value.trim(), use: $("ause").checked,
      region: $("aregion").value, project_id: $("aproj").value.trim(),
      net_mumbai: $("anetmum").value.trim(), net_noida: $("anetnoi").value.trim(),
    };
    if (!EDIT_ID && (!body.email || !body.password)) { toast("Email and password required"); return; }
  } else if (CUR.kind === "hostzop") {
    body = {
      id: EDIT_ID, label: $("alabel").value.trim(), apikey: $("akey2").value.trim(),
      billing_account_id: $("ahzbill").value.trim(), location: $("ahzloc").value.trim(),
      note: $("anote").value.trim(), use: $("ause").checked,
    };
    if (!EDIT_ID && !body.apikey) { toast("API key required"); return; }
  } else {
    body = {
      id: EDIT_ID, label: $("alabel").value.trim(), apikey: $("akey2").value.trim(),
      zone_id: $("aexzone").value.trim(), subnet_id: $("aexsub").value.trim(),
      note: $("anote").value.trim(), use: $("ause").checked,
    };
    if (!EDIT_ID && !body.apikey) { toast("API key required"); return; }
  }
  let d;
  try { d = await POST(CUR.api + "/accounts/add", body); }
  catch (e) { toast("Failed"); return; }
  if (d.ok) { toast("Saved"); resetForm(); loadAccounts(); }
  else toast(d.msg || "Failed");
}

function editAccount(id) {
  const a = ACC[id];
  if (!a) return;
  EDIT_ID = id;
  DETECT = null;
  applyKind();
  $("formTitle").textContent = "Edit account";
  $("alabel").value = a.label || "";
  $("anote").value = a.note || "";
  if (CUR.kind === "ace") {
    $("aemail").value = a.email || "";
    $("apass").value = "";
    $("akey").value = a.api_key || "";
    $("aregion").value = a.region || "ap-south-mum-2";
    $("aproj").value = a.project_id || "";
    $("anetmum").value = a.net_mumbai || "";
    $("anetnoi").value = a.net_noida || "";
    $("detectBox").style.display = "none";
  } else if (CUR.kind === "hostzop") {
    $("akey2").value = "";
    $("ahzbill").value = a.billing_account_id || "";
    $("ahzloc").value = a.location || "";
  } else {
    $("akey2").value = "";
    $("aexzone").value = a.zone_id || "";
    $("aexsub").value = a.subnet_id || "";
  }
  $("cfg").scrollTop = 0;
}

async function delAccount(id) {
  if (!confirm("Delete this account?")) return;
  const d = await POST(CUR.api + "/accounts/delete", { id });
  toast(d.ok ? "Deleted" : (d.msg || "Failed"));
  loadAccounts();
}

async function activate(id) {
  const d = await POST(CUR.api + "/accounts/activate", { id });
  toast(d.ok ? "Active account switched" : (d.msg || "Failed"));
  loadAccounts();
}

renderSites();
