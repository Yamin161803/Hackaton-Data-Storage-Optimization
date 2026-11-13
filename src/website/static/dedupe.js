/* Sidan "Rensa dubbletter": kör src/dedupe/slide_dedupe.py via /api/dedupe/* (website/dedupe_api.py). */
const BACKEND = "http://localhost:8000";
// Samma server som serverade sidan; som fil eller via Live Server (5500/5501) används BACKEND
const SAME_SERVER = location.protocol.startsWith("http") && !["5500", "5501"].includes(location.port);
const API = SAME_SERVER ? "/api/dedupe" : `${BACKEND}/api/dedupe`;

const $ = (s) => document.querySelector(s);
const targetInput = $("#targetInput"), trashInput = $("#trashInput");
const moveInput = $("#moveInput"), autoDeleteInput = $("#autoDeleteInput"), fastImagesInput = $("#fastImagesInput");
const trashBox = $("#trashBox"), trashInfo = $("#trashInfo");
const runBtn = $("#runBtn"), emptyBtn = $("#emptyBtn"), resultBody = $("#resultBody");

let mode = "batch";
let running = false;
// Det användaren godkände när automatisk radering slogs på: { path, files, bytes }.
// Servern raderar bara om papperskorgen fortfarande ser ut så.
let confirmed = null;

$("#apiChip .is-mono").textContent = API;

/* ---------- Tema (samma inställning som startsidan) ---------- */
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

/* ---------- Hjälpfunktioner ---------- */
function fmtBytes(n) {
  if (n == null) return "–";
  const u = ["B", "kB", "MB", "GB", "TB"]; let i = 0;
  while (Math.abs(n) >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(n < 10 && i > 0 ? 1 : 0)} ${u[i]}`;
}
function fmtSecs(s) {
  if (s == null) return "–";
  if (s < 1) return `${Math.round(s * 1000)} ms`;
  if (s < 60) return `${s.toFixed(s < 10 ? 2 : 1)} s`;
  return `${Math.floor(s / 60)} min ${Math.round(s % 60)} s`;
}
function fmtNum(n) { return Number(n || 0).toLocaleString("sv-SE"); }
function filer(n) { return `${fmtNum(n)} ${n === 1 ? "fil" : "filer"}`; }
function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({ "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;" }[c]));
}
function remember(key, value) { try { localStorage.setItem(key, value); } catch (e) {} }
function recall(key) { try { return localStorage.getItem(key); } catch (e) { return null; } }

async function api(path, body) {
  const opts = body === undefined ? {} :
    { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  let resp;
  try { resp = await fetch(`${API}${path}`, opts); }
  catch (e) { throw Object.assign(new Error(`Kunde inte nå servern på ${API}. Kontrollera att den körs.`), { status: 0 }); }
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw Object.assign(new Error(data.detail || `Fel ${resp.status}`), { status: resp.status, data });
  return data;
}
const getTrash = () => api(`/trash?path=${encodeURIComponent(trashInput.value.trim())}`);

function setStep(n) {
  document.querySelectorAll(".is-step").forEach(el => el.classList.toggle("is-active", +el.dataset.step === n));
}

/* ---------- Bekräftelse ---------- */
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
const AUTO_TEXT = "Efter varje rensning raderas allt i papperskorgen permanent, både filerna som redan " +
                  "ligger där och de som flyttas dit. Det går inte att ångra.";
const EMPTY_TEXT = "Alla filer i papperskorgen raderas permanent. Det går inte att ångra.";

/* ---------- Papperskorgen ---------- */
let trashTimer = null;
async function refreshTrash() {
  if (!moveInput.checked) return;
  try {
    const info = await getTrash();
    trashInfo.textContent = !info.exists ? `Mappen "${info.name}" skapas vid första rensningen.`
      : info.files ? `"${info.name}" innehåller ${filer(info.files)}, ${fmtBytes(info.bytes)}.`
      : `"${info.name}" är tom.`;
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
  autoDeleteInput.checked = false;           // på först efter bekräftelse
  try {
    const info = await getTrash();
    const ok = await askConfirm({ title: "Slå på automatisk radering?", text: AUTO_TEXT,
                                  ok: "Slå på radering", info });
    setAutoDelete(ok, ok ? info : null);
  } catch (e) { trashInfo.textContent = e.message; }
});

trashInput.addEventListener("input", () => {
  if (autoDeleteInput.checked) setAutoDelete(false);   // ny mapp = ny bekräftelse
  remember("dedupe.trash", trashInput.value);
  clearTimeout(trashTimer);
  trashTimer = setTimeout(refreshTrash, 300);
});

emptyBtn.addEventListener("click", async () => {
  emptyBtn.disabled = true;
  try {
    let info = await getTrash();
    let title = "Töm papperskorgen?";
    for (;;) {
      if (!info.files) { trashInfo.textContent = `"${info.name}" är redan tom.`; return; }
      if (!await askConfirm({ title, text: EMPTY_TEXT, ok: "Radera filerna", info })) return;
      try {
        const res = await api("/empty", { path: info.path, files: info.files, bytes: info.bytes });
        if (autoDeleteInput.checked) confirmed = res.trash;
        await refreshTrash();
        trashInfo.textContent = `Raderade ${filer(res.deleted_files)}, ${fmtBytes(res.freed_bytes)} frigjort.`;
        return;
      } catch (e) {
        if (e.status !== 409 || !e.data?.trash) throw e;
        info = e.data.trash;                       // innehållet ändrades: fråga igen
        title = "Papperskorgen har ändrats. Töm den ändå?";
      }
    }
  } catch (e) {
    trashInfo.textContent = e.message;
  } finally {
    emptyBtn.disabled = false;
    refreshTrash();
  }
});

/* ---------- Val ---------- */
function updateRunLabel() {
  runBtn.textContent = !moveInput.checked ? "Analysera (inget flyttas)"
    : autoDeleteInput.checked ? "Rensa och radera" : "Rensa: flytta avvisade filer";
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

/* ---------- Mappväljare ---------- */
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
      : '<div class="is-empty">Inga undermappar.</div>';
    $("#pickerHint").textContent = `${filer(d.supported_files_here)} att jämföra direkt i den här mappen (undermappar tas också med).`;
  } catch (e) {
    if (fallback) return pickerLoad("");
    $("#pickerHint").textContent = e.message;
  }
}
document.querySelectorAll("[data-browse]").forEach(b => b.addEventListener("click", () => {
  pickerFor = document.getElementById(b.dataset.browse);
  $("#pickerTitle").textContent = pickerFor === trashInput ? "Välj papperskorg" : "Välj mapp att rensa";
  picker.showModal();
  pickerLoad(pickerFor.value.trim(), true);
}));
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

/* ---------- Kör ---------- */
runBtn.addEventListener("click", async () => {
  if (running) return;
  const body = { target: targetInput.value.trim(), move: moveInput.checked, trash: trashInput.value.trim(),
                 auto_delete: moveInput.checked && autoDeleteInput.checked, mode,
                 fast_images: fastImagesInput.checked, confirm: confirmed };
  running = true; runBtn.disabled = true; emptyBtn.disabled = true;
  setStep(2);
  const label = runBtn.textContent;
  runBtn.innerHTML = '<span class="is-btn-spinner" aria-hidden="true"></span> Arbetar…';
  const t0 = performance.now();
  resultBody.innerHTML = `<div class="is-op-card" data-kind="info"><strong>Analyserar…</strong>
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
        const ok = await askConfirm({ title: "Papperskorgen har ändrats. Radera ändå?", text: AUTO_TEXT,
                                      ok: "Rensa och radera", info: e.data.trash });
        if (!ok) { setAutoDelete(false); throw new Error("Avbrutet: automatisk radering är nu avslagen."); }
        confirmed = body.confirm = e.data.trash;
      }
    }
    if (body.auto_delete && report.web.trash_after) confirmed = report.web.trash_after;
    renderReport(report, (performance.now() - t0) / 1000);
    setStep(body.move ? 3 : 2);
  } catch (e) {
    resultBody.innerHTML = `<div class="is-op-card" data-kind="error"><strong>Det gick inte</strong>${escapeHtml(e.message)}</div>`;
    setStep(1);
  } finally {
    clearInterval(timer);
    running = false; runBtn.disabled = false; runBtn.textContent = label;
    emptyBtn.disabled = false;
    refreshTrash();
  }
});

/* ---------- Rapport ---------- */
const STAGE_LABEL = { load: "Index", scan: "Sök filer", prefilter: "Förfilter", extract: "Läs bilder",
                      decide: "Välj filer", act: "Flytta" };

function renderReport(r, roundTrip) {
  const sp = r.space, fl = r.files, tm = r.time, w = r.web;
  const removed = sp.removed_this_run_bytes, kept = sp.kept_bytes, total = removed + kept;
  const pct = total ? (100 * removed / total) : 0;
  const rel = (p) => {
    const t = w.target.replace(/[\\/]+$/, "");
    return p.startsWith(t) ? p.slice(t.length + 1) || p : p;
  };
  const short = (s) => String(s).split(w.target.replace(/[\\/]+$/, "") + (w.target.includes("\\") ? "\\" : "/")).join("");

  let status;
  if (w.deleted) {
    status = `<div class="is-op-card" data-kind="ok"><strong>Raderat</strong>
      ${filer(w.deleted.files)} raderades från ${escapeHtml(w.trash)}: ${fmtBytes(w.deleted.bytes)} frigjort.</div>`;
  } else if (w.moved) {
    status = `<div class="is-op-card" data-kind="ok"><strong>Flyttat</strong>
      ${filer(r.moved_files)} flyttades till <span class="is-mono">${escapeHtml(w.trash)}</span>.
      Utrymmet frigörs när papperskorgen töms.</div>`;
  } else {
    status = `<div class="is-op-card" data-kind="info"><strong>Bara rapport</strong>
      Inga filer flyttades. Slå på "Flytta avvisade filer" för att rensa.</div>`;
  }
  if (!fl.found) {
    status = `<div class="is-op-card" data-kind="info"><strong>Inget att jämföra</strong>
      Mappen innehåller inga PDF-, PowerPoint- eller bildfiler.</div>`;
  }

  const stages = Object.entries(tm.stages_seconds || {});
  const maxStage = Math.max(1e-9, ...stages.map(([, s]) => s));
  const stageRows = stages.map(([k, s]) => `<div class="is-stage"><span>${STAGE_LABEL[k] || k}</span>
      <div class="is-stage__bar"><div style="width:${(100 * s / maxStage).toFixed(1)}%"></div></div>
      <span class="is-stage__val">${fmtSecs(s)}</span></div>`).join("");

  const removedRows = (r.removed || []).map(f => `<div class="is-filerow">
      <span class="is-filerow__name">${escapeHtml(rel(f.file))}</span>
      <span class="is-filerow__size">${fmtBytes(f.bytes)}</span>
      <span class="is-filerow__why">${escapeHtml(short(f.reason))}</span></div>`).join("");

  const savedLabel = w.deleted ? "frigjort (raderat)" : "sparas om avvisade filer raderas";
  resultBody.innerHTML = `
    <div class="is-stats">
      <div><div class="is-stat__num">${fmtSecs(w.total_seconds)}</div><div class="is-stat__label">körtid</div></div>
      <div><div class="is-stat__num is-good">${fmtBytes(removed)}</div><div class="is-stat__label">${savedLabel}</div></div>
      <div><div class="is-stat__num">${pct.toFixed(1)} %</div><div class="is-stat__label">av ${fmtBytes(total)}</div></div>
    </div>
    <div class="is-dist-bar" role="img" aria-label="Behållet ${fmtBytes(kept)}, avvisat ${fmtBytes(removed)}">
      <div style="width:${total ? 100 * kept / total : 0}%;background:var(--primary)"></div>
      <div style="width:${pct}%;background:var(--green)"></div>
    </div>
    <div class="is-legend"><span><i style="background:var(--primary)"></i>Behålls ${filer(sp.kept_files)}, ${fmtBytes(kept)}</span>
      <span><i style="background:var(--green)"></i>Avvisas ${filer(sp.removed_this_run_files)}, ${fmtBytes(removed)}</span></div>
    ${status}

    <h3 class="is-subhead">Filer</h3>
    <dl class="is-kv">
      <dt>Hittade</dt><dd>${fmtNum(fl.found)}</dd>
      <dt>Identiska kopior (hittade utan att öppnas)</dt><dd>${fmtNum(fl.duplicates_by_content)}</dd>
      <dt>Lästa</dt><dd>${fmtNum(fl.parsed)}</dd>
      <dt>Unika bilder</dt><dd>${fmtNum(r.slides.unique)}</dd>
      ${fl.errors ? `<dt>Kunde inte läsas</dt><dd>${fmtNum(fl.errors)}</dd>` : ""}
    </dl>

    <h3 class="is-subhead">Prestanda</h3>
    <dl class="is-kv">
      <dt>Total körtid (servern)</dt><dd>${fmtSecs(w.total_seconds)}</dd>
      <dt>Analys</dt><dd>${fmtSecs(tm.total_seconds)}</dd>
      ${w.deleted ? `<dt>Radering</dt><dd>${fmtSecs(w.deleted.seconds)}</dd>` : ""}
      <dt>Med nätverk</dt><dd>${fmtSecs(roundTrip)}</dd>
      <dt>Snitt per bild</dt><dd>${(tm.avg_ms_per_slide || 0).toFixed(2)} ms</dd>
      <dt>Läshastighet</dt><dd>${(tm.parse_mb_per_second || 0).toFixed(1)} MB/s</dd>
      ${fl.duplicates_by_content ? `<dt>Förfiltret sparade ungefär</dt><dd>${fmtSecs(tm.est_seconds_saved_by_prefilter)}</dd>` : ""}
    </dl>
    ${stageRows ? `<h3 class="is-subhead">Tid per steg</h3>${stageRows}` : ""}
    ${removedRows ? `<h3 class="is-subhead">Avvisade filer (${fmtNum(r.removed.length)})</h3><div class="is-filelist">${removedRows}</div>` : ""}
    ${(r.hints || []).map(h => `<p class="is-field__hint" style="margin-top:12px">Tips: ${escapeHtml(h)}</p>`).join("")}
  `;
}

/* ---------- Start ---------- */
(async function init() {
  updateRunLabel();
  try {
    const d = await api("/defaults");
    targetInput.value = recall("dedupe.target") || d.target;
    trashInput.value = recall("dedupe.trash") || d.trash;
    $("#extHint").textContent = `Filtyper: ${d.extensions.join(", ")}. Undermappar tas med.`;
  } catch (e) {
    resultBody.innerHTML = `<div class="is-op-card" data-kind="error"><strong>Ingen kontakt</strong>${escapeHtml(e.message)}</div>`;
  }
})();
