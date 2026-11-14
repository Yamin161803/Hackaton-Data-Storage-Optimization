/* The "Remove duplicates" page: runs src/dedupe/slide_dedupe.py via /api/dedupe/* (website/dedupe_api.py).
   Page text is in i18n.js (t(), fmtLocal(), LANG); loaded before this file. */
const BACKEND = "http://localhost:8000";
// The server that served the page; as a file or via Live Server (5500/5501) BACKEND is used
const SAME_SERVER = location.protocol.startsWith("http") && !["5500", "5501"].includes(location.port);
const API = SAME_SERVER ? "/api/dedupe" : `${BACKEND}/api/dedupe`;

const $ = (s) => document.querySelector(s);
const targetInput = $("#targetInput"), trashInput = $("#trashInput");
const moveInput = $("#moveInput"), autoDeleteInput = $("#autoDeleteInput"), fastImagesInput = $("#fastImagesInput");
const trashBox = $("#trashBox"), trashInfo = $("#trashInfo");
const runBtn = $("#runBtn"), emptyBtn = $("#emptyBtn"), resultBody = $("#resultBody");

let mode = "batch";
let running = false;
// What the user approved when automatic deletion was turned on: { path, files, bytes }.
// The server only deletes if the trash folder still looks like that.
let confirmed = null;
let extensions = null;            // supported file types, from /defaults
let lastReport = null;            // [report, roundTrip], re-drawn when the language changes

$("#apiChip .is-mono").textContent = API;

/* ---------- Theme (same setting as the start page) ---------- */
(function initTheme() {
  let saved = null;
  try { saved = localStorage.getItem("theme"); } catch (e) {}
  const dark = saved ? saved === "dark" : window.matchMedia("(prefers-color-scheme: dark)").matches;
  document.body.classList.toggle("body--dark", dark);
})();
$("#themeBtn").addEventListener("click", () => {
  const dark = document.body.classList.toggle("body--dark");
  try { localStorage.setItem("theme", dark ? "dark" : "light"); } catch (e) {}
});

/* ---------- Helpers ---------- */
function fmtBytes(n) {
  if (n == null) return "–";
  const u = ["B", "kB", "MB", "GB", "TB"]; let i = 0;
  while (Math.abs(n) >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(n < 10 && i > 0 ? 1 : 0)} ${u[i]}`;
}
function fmtSecs(s) {
  if (s == null) return "–";
  if (s < 1) return `${Math.round(s * 1000)} ms`;
  if (s < 60) return `${fmtLocal(s, s < 10 ? 2 : 1)} s`;
  return `${Math.floor(s / 60)} min ${Math.round(s % 60)} s`;
}
function fmtNum(n) { return fmtLocal(n); }
function filer(n) { return t("files", { n: fmtNum(n), count: n }); }
function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({ "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;" }[c]));
}
function remember(key, value) { try { localStorage.setItem(key, value); } catch (e) {} }
function recall(key) { try { return localStorage.getItem(key); } catch (e) { return null; } }

async function api(path, body) {
  const headers = { "X-Lang": LANG };                // server messages in the page language
  const opts = body === undefined ? { headers } :
    { method: "POST", headers: { ...headers, "Content-Type": "application/json" },
      body: JSON.stringify(body) };
  let resp;
  try { resp = await fetch(`${API}${path}`, opts); }
  catch (e) { throw Object.assign(new Error(t("unreachable", { url: API })), { status: 0 }); }
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw Object.assign(new Error(data.detail || t("error_status", { status: resp.status })),
                                     { status: resp.status, data });
  return data;
}
const getTrash = () => api(`/trash?path=${encodeURIComponent(trashInput.value.trim())}`);

function setStep(n) {
  document.querySelectorAll(".is-step").forEach(el => el.classList.toggle("is-active", +el.dataset.step === n));
}

/* ---------- Confirmation ---------- */
const confirmDialog = $("#confirmDialog");
function askConfirm({ title, text, ok, info }) {
  $("#confirmTitle").textContent = title;
  $("#confirmText").textContent = text;
  $("#confirmFolder").textContent = info.name;
  $("#confirmFiles").textContent = filer(info.files);
  $("#confirmSize").textContent = fmtBytes(info.bytes);
  $("#confirmPath").textContent = info.path;
  $("#confirmOk").textContent = ok;
  confirmDialog.returnValue = "";
  confirmDialog.showModal();
  return new Promise(resolve =>
    confirmDialog.addEventListener("close", () => resolve(confirmDialog.returnValue === "ok"), { once: true }));
}


/* ---------- Trash folder ---------- */
let trashTimer = null;
async function refreshTrash() {
  if (!moveInput.checked) return;
  try {
    const info = await getTrash();
    trashInfo.textContent = !info.exists ? t("trash_created", { name: info.name })
      : info.files ? t("trash_contains", { name: info.name, files: filer(info.files), size: fmtBytes(info.bytes) })
      : t("trash_empty", { name: info.name });
    emptyBtn.disabled = !info.files;
  } catch (e) {
    trashInfo.textContent = e.message;
    emptyBtn.disabled = true;
  }
}

function setAutoDelete(on, info = null) {
  autoDeleteInput.checked = on;
  confirmed = on ? info : null;
  updateRunLabel();
}

autoDeleteInput.addEventListener("change", async () => {
  if (!autoDeleteInput.checked) { setAutoDelete(false); return; }
  autoDeleteInput.checked = false;           // on only after confirmation
  try {
    const info = await getTrash();
    const ok = await askConfirm({ title: t("auto_title"), text: t("auto_text"),
                                  ok: t("auto_ok"), info });
    setAutoDelete(ok, ok ? info : null);
  } catch (e) { trashInfo.textContent = e.message; }
});

trashInput.addEventListener("input", () => {
  if (autoDeleteInput.checked) setAutoDelete(false);   // new folder = new confirmation
  remember("dedupe.trash", trashInput.value);
  clearTimeout(trashTimer);
  trashTimer = setTimeout(refreshTrash, 300);
});

emptyBtn.addEventListener("click", async () => {
  emptyBtn.disabled = true;
  try {
    let info = await getTrash();
    let title = t("empty_title");
    for (;;) {
      if (!info.files) { trashInfo.textContent = t("trash_already_empty", { name: info.name }); return; }
      if (!await askConfirm({ title, text: t("empty_text"), ok: t("delete_files"), info })) return;
      try {
        const res = await api("/empty", { path: info.path, files: info.files, bytes: info.bytes });
        if (autoDeleteInput.checked) confirmed = res.trash;
        await refreshTrash();
        trashInfo.textContent = t("deleted_freed", { files: filer(res.deleted_files), size: fmtBytes(res.freed_bytes) });
        return;
      } catch (e) {
        if (e.status !== 409 || !e.data?.trash) throw e;
        info = e.data.trash;                       // the contents changed: ask again
        title = t("changed_empty");
      }
    }
  } catch (e) {
    trashInfo.textContent = e.message;
  } finally {
    emptyBtn.disabled = false;
    refreshTrash();
  }
});

/* ---------- Choices ---------- */
function updateRunLabel() {
  runBtn.textContent = t(!moveInput.checked ? "run_report"
    : autoDeleteInput.checked ? "run_delete" : "run_move");
  runBtn.classList.toggle("is-btn--danger", moveInput.checked && autoDeleteInput.checked);
}
moveInput.addEventListener("change", () => {
  trashBox.hidden = !moveInput.checked;
  if (!moveInput.checked && autoDeleteInput.checked) setAutoDelete(false);
  updateRunLabel();
  refreshTrash();
});
targetInput.addEventListener("input", () => remember("dedupe.target", targetInput.value));
document.querySelectorAll("[data-mode]").forEach(b => b.addEventListener("click", () => {
  mode = b.dataset.mode;
  document.querySelectorAll("[data-mode]").forEach(x => x.setAttribute("aria-pressed", String(x === b)));
}));

/* ---------- Folder picker ---------- */
const picker = $("#pickerDialog"), pickerList = $("#pickerList"), pickerPath = $("#pickerPath");
const FOLDER_ICON = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>';
let pickerFor = null, pickerCurrent = null, pickerParent = null;

async function pickerLoad(path, fallback = false) {
  try {
    const d = await api(`/browse?path=${encodeURIComponent(path)}`);
    pickerCurrent = d.path; pickerParent = d.parent;
    pickerPath.value = d.path;
    $("#pickerUp").disabled = !d.parent;
    const chips = [...d.shortcuts, ...d.roots.map(r => ({ name: r, path: r }))];
    $("#pickerChips").innerHTML = chips.map(c =>
      `<button type="button" data-path="${escapeHtml(c.path)}">${escapeHtml(c.name)}</button>`).join("");
    pickerList.innerHTML = d.dirs.length
      ? d.dirs.map(x => `<button class="is-picker__item" type="button" role="listitem" data-path="${escapeHtml(x.path)}">${FOLDER_ICON}<span>${escapeHtml(x.name)}</span></button>`).join("")
      : `<div class="is-empty">${t("no_subfolders")}</div>`;
    $("#pickerHint").textContent = t("picker_hint", { files: filer(d.supported_files_here) });
  } catch (e) {
    if (fallback) return pickerLoad("");
    $("#pickerHint").textContent = e.message;
  }
}
document.querySelectorAll("[data-browse]").forEach(b => b.addEventListener("click", () => {
  pickerFor = document.getElementById(b.dataset.browse);
  setPickerTitle();
  picker.showModal();
  pickerLoad(pickerFor.value.trim(), true);
}));
function setPickerTitle() {
  $("#pickerTitle").textContent = t(pickerFor === trashInput ? "pick_trash" : "pick_target");
}
pickerList.addEventListener("click", e => { const b = e.target.closest("[data-path]"); if (b) pickerLoad(b.dataset.path); });
$("#pickerChips").addEventListener("click", e => { const b = e.target.closest("[data-path]"); if (b) pickerLoad(b.dataset.path); });
$("#pickerUp").addEventListener("click", () => pickerParent && pickerLoad(pickerParent));
$("#pickerGo").addEventListener("click", () => pickerLoad(pickerPath.value));
pickerPath.addEventListener("keydown", e => { if (e.key === "Enter") { e.preventDefault(); pickerLoad(pickerPath.value); } });
$("#pickerCancel").addEventListener("click", () => picker.close());
$("#pickerChoose").addEventListener("click", () => {
  if (pickerCurrent && pickerFor) {
    pickerFor.value = pickerCurrent;
    pickerFor.dispatchEvent(new Event("input"));
  }
  picker.close();
});

/* ---------- Run ---------- */
runBtn.addEventListener("click", async () => {
  if (running) return;
  const body = { target: targetInput.value.trim(), move: moveInput.checked, trash: trashInput.value.trim(),
                 auto_delete: moveInput.checked && autoDeleteInput.checked, mode,
                 fast_images: fastImagesInput.checked, confirm: confirmed };
  running = true; runBtn.disabled = true; emptyBtn.disabled = true;
  setStep(2);
  runBtn.innerHTML = `<span class="is-btn-spinner" aria-hidden="true"></span> ${t("working")}`;
  const t0 = performance.now();
  lastReport = null;
  resultBody.innerHTML = `<div class="is-op-card" data-kind="info"><strong>${t("analysing")}</strong>
    <span class="is-mono">${escapeHtml(body.target)}</span><br><span id="elapsed">0.0 s</span></div>`;
  const timer = setInterval(() => {
    const el = $("#elapsed"); if (el) el.textContent = `${((performance.now() - t0) / 1000).toFixed(1)} s`;
  }, 100);
  try {
    let report;
    for (;;) {
      try { report = await api("/run", body); break; }
      catch (e) {
        if (!(e.status === 409 && e.data?.trash && body.auto_delete)) throw e;
        clearInterval(timer);
        const ok = await askConfirm({ title: t("changed_delete"), text: t("auto_text"),
                                      ok: t("run_delete"), info: e.data.trash });
        if (!ok) { setAutoDelete(false); throw new Error(t("cancelled")); }
        confirmed = body.confirm = e.data.trash;
      }
    }
    if (body.auto_delete && report.web.trash_after) confirmed = report.web.trash_after;
    lastReport = [report, (performance.now() - t0) / 1000];
    renderReport(...lastReport);
    setStep(body.move ? 3 : 2);
  } catch (e) {
    resultBody.innerHTML = `<div class="is-op-card" data-kind="error"><strong>${t("failed")}</strong>${escapeHtml(e.message)}</div>`;
    setStep(1);
  } finally {
    clearInterval(timer);
    running = false; runBtn.disabled = false; updateRunLabel();
    emptyBtn.disabled = false;
    refreshTrash();
  }
});

/* ---------- Report ---------- */
const stageLabel = (k) => { const s = t("stage_" + k); return s === "stage_" + k ? k : s; };

function renderReport(r, roundTrip) {
  const sp = r.space, fl = r.files, tm = r.time, w = r.web;
  const removed = sp.removed_this_run_bytes, kept = sp.kept_bytes, total = removed + kept;
  const pct = total ? (100 * removed / total) : 0;
  const rel = (p) => {
    const base = w.target.replace(/[\\/]+$/, "");
    return p.startsWith(base) ? p.slice(base.length + 1) || p : p;
  };
  const short = (s) => String(s).split(w.target.replace(/[\\/]+$/, "") + (w.target.includes("\\") ? "\\" : "/")).join("");

  let status;
  if (w.deleted) {
    status = `<div class="is-op-card" data-kind="ok"><strong>${t("deleted_title")}</strong>
      ${t("deleted_text", { files: filer(w.deleted.files), trash: escapeHtml(w.trash), size: fmtBytes(w.deleted.bytes) })}</div>`;
  } else if (w.moved) {
    status = `<div class="is-op-card" data-kind="ok"><strong>${t("moved_title")}</strong>
      ${t("moved_text", { files: filer(r.moved_files), trash: `<span class="is-mono">${escapeHtml(w.trash)}</span>` })}</div>`;
  } else {
    status = `<div class="is-op-card" data-kind="info"><strong>${t("report_only_title")}</strong>
      ${escapeHtml(t("report_only_text"))}</div>`;
  }
  if (!fl.found) {
    status = `<div class="is-op-card" data-kind="info"><strong>${t("nothing_title")}</strong>
      ${t("nothing_text")}</div>`;
  }

  const stages = Object.entries(tm.stages_seconds || {});
  const maxStage = Math.max(1e-9, ...stages.map(([, s]) => s));
  const stageRows = stages.map(([k, s]) => `<div class="is-stage"><span>${stageLabel(k)}</span>
      <div class="is-stage__bar"><div style="width:${(100 * s / maxStage).toFixed(1)}%"></div></div>
      <span class="is-stage__val">${fmtSecs(s)}</span></div>`).join("");

  const removedRows = (r.removed || []).map(f => `<div class="is-filerow">
      <span class="is-filerow__name">${escapeHtml(rel(f.file))}</span>
      <span class="is-filerow__size">${fmtBytes(f.bytes)}</span>
      <span class="is-filerow__why">${escapeHtml(short(f.reason))}</span></div>`).join("");

  const savedLabel = t(w.deleted ? "saved_deleted" : "saved_if");
  resultBody.innerHTML = `
    <div class="is-stats">
      <div><div class="is-stat__num">${fmtSecs(w.total_seconds)}</div><div class="is-stat__label">${t("run_time")}</div></div>
      <div><div class="is-stat__num is-good">${fmtBytes(removed)}</div><div class="is-stat__label">${savedLabel}</div></div>
      <div><div class="is-stat__num">${fmtLocal(pct, 1)} %</div><div class="is-stat__label">${t("of_total", { size: fmtBytes(total) })}</div></div>
    </div>
    <div class="is-dist-bar" role="img" aria-label="${t("dist_aria", { kept: fmtBytes(kept), removed: fmtBytes(removed) })}">
      <div style="width:${total ? 100 * kept / total : 0}%;background:var(--primary)"></div>
      <div style="width:${pct}%;background:var(--green)"></div>
    </div>
    <div class="is-legend"><span><i style="background:var(--primary)"></i>${t("kept_legend", { files: filer(sp.kept_files), size: fmtBytes(kept) })}</span>
      <span><i style="background:var(--green)"></i>${t("rejected_legend", { files: filer(sp.removed_this_run_files), size: fmtBytes(removed) })}</span></div>
    ${status}

    <h3 class="is-subhead">${t("c_files")}</h3>
    <dl class="is-kv">
      <dt>${t("k_found")}</dt><dd>${fmtNum(fl.found)}</dd>
      <dt>${t("k_identical")}</dt><dd>${fmtNum(fl.duplicates_by_content)}</dd>
      <dt>${t("k_parsed")}</dt><dd>${fmtNum(fl.parsed)}</dd>
      <dt>${t("k_unique")}</dt><dd>${fmtNum(r.slides.unique)}</dd>
      ${fl.errors ? `<dt>${t("k_unreadable")}</dt><dd>${fmtNum(fl.errors)}</dd>` : ""}
    </dl>

    <h3 class="is-subhead">${t("performance")}</h3>
    <dl class="is-kv">
      <dt>${t("k_total_server")}</dt><dd>${fmtSecs(w.total_seconds)}</dd>
      <dt>${t("k_analysis")}</dt><dd>${fmtSecs(tm.total_seconds)}</dd>
      ${w.deleted ? `<dt>${t("k_deletion")}</dt><dd>${fmtSecs(w.deleted.seconds)}</dd>` : ""}
      <dt>${t("k_network")}</dt><dd>${fmtSecs(roundTrip)}</dd>
      <dt>${t("k_avg")}</dt><dd>${fmtLocal(tm.avg_ms_per_slide, 2)} ms</dd>
      <dt>${t("k_speed")}</dt><dd>${fmtLocal(tm.parse_mb_per_second, 1)} MB/s</dd>
      ${fl.duplicates_by_content ? `<dt>${t("k_prefilter")}</dt><dd>${fmtSecs(tm.est_seconds_saved_by_prefilter)}</dd>` : ""}
    </dl>
    ${stageRows ? `<h3 class="is-subhead">${t("per_stage")}</h3>${stageRows}` : ""}
    ${removedRows ? `<h3 class="is-subhead">${t("rejected_files", { n: fmtNum(r.removed.length) })}</h3><div class="is-filelist">${removedRows}</div>` : ""}
    ${(r.hints || []).map(h => `<p class="is-field__hint" style="margin-top:12px">${t("tip", { hint: escapeHtml(h) })}</p>`).join("")}
  `;
}

/* ---------- Language switch: redo everything built in JS ---------- */
function setExtHint() {
  $("#extHint").textContent = extensions ? t("ext_hint_types", { types: extensions.join(", ") })
                                         : t("ext_hint");
}
document.addEventListener("langchange", () => {
  if (!running) updateRunLabel();
  setExtHint();
  refreshTrash();
  if (pickerFor) setPickerTitle();
  if (lastReport) renderReport(...lastReport);
});

/* ---------- Start ---------- */
(async function init() {
  updateRunLabel();
  try {
    const d = await api("/defaults");
    targetInput.value = recall("dedupe.target") || d.target;
    trashInput.value = recall("dedupe.trash") || d.trash;
    extensions = d.extensions;
    setExtHint();
  } catch (e) {
    resultBody.innerHTML = `<div class="is-op-card" data-kind="error"><strong>${t("no_contact")}</strong>${escapeHtml(e.message)}</div>`;
  }
})();
