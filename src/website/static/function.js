/* ==============================================================
   Settings – match these to the Python backend
   Page text is in i18n.js (t(), fmtLocal(), LANG); loaded before this file.
   ============================================================== */
const BACKEND      = "http://localhost:8000"; // where the server runs
// When Flask serves the page (any port) the same server is used; opened as a file or
// via Live Server (port 5500/5501) BACKEND is used
const SAME_SERVER  = location.protocol.startsWith("http") && !["5500", "5501"].includes(location.port);
const API_URL      = SAME_SERVER ? "/api/compress" : `${BACKEND}/api/compress`;
const FIELD_NAME   = "file";          // name of the multipart/form-data field
const MAX_FILE_MB  = 2048;            // shown to the user + checked before upload

/* ============================================================== */
const $ = (s) => document.querySelector(s);
const fileInput = $("#fileInput"), dropZone = $("#dropZone"), list = $("#fileList");
const startBtn = $("#startBtn"), clearBtn = $("#clearBtn"), resultBody = $("#resultBody");
const emptyState = $("#emptyState");
const speakersInput = $("#speakersInput"), numSpeakersInput = $("#numSpeakersInput");
const numSpeakersLabel = $("#numSpeakersLabel"), speakerOptions = $("#speakerOptions");
const summaryInput = $("#summaryInput"), keywordsInput = $("#keywordsInput");

/* ---------- Choices: who is speaking ---------- */
function speakerChoice() {
  const n = parseInt(numSpeakersInput.value, 10);
  return { speakers: speakersInput.checked, num: speakersInput.checked && n > 0 ? n : null,
           summary: summaryInput.checked, keywords: keywordsInput.checked };
}
function updateSpeakerOptions() { numSpeakersLabel.hidden = !speakersInput.checked; }
speakersInput.addEventListener("change", updateSpeakerOptions);
updateSpeakerOptions();

$("#apiChip .is-mono").textContent = API_URL;
function setStaticTexts() {
  $("#limitHint").textContent = t("limit_hint", { gb: MAX_FILE_MB / 1024 });
}
setStaticTexts();
document.addEventListener("langchange", () => { setStaticTexts(); render(); });

/* ---------- Theme ---------- */
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

/* ---------- State ---------- */
let items = [];           // { id, file, status, progress, outBlob, outName, outSize, error }
let running = false;
let nextId = 1;
const queueHead = $("#queueHead");

const statusLabel = (status) => t("st_" + status);

// An error is { key, vars, detail } so it follows the page language; detail is text
// from the server (already in the language the request was made in).
const errText = (e) => !e ? t("unknown_error")
  : typeof e === "string" ? e
  : t(e.key, e.vars) + (e.detail ? ` ${e.detail}` : "");

// Duplicate = same file hash (worked out by the server). The name does not matter.
// The duplicate counts as removed: no download, its whole size counts as saved.
function markDuplicate(it, original) {
  it.status = "duplicate";
  it.dupOf = original.file.name;
  it.outBlob = null;
}

function fmtBytes(n) {
  if (n == null) return "–";
  const u = ["B", "kB", "MB", "GB"]; let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(n < 10 && i > 0 ? 1 : 0)} ${u[i]}`;
}
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({ "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;" }[c]));
}
// How much smaller: "−99.98 %" (more decimals when almost everything is gone)
function fmtSaved(orig, out) {
  if (!orig) return { text: "–", grew: false, kept: 100 };
  const saved = (1 - out / orig) * 100;
  const grew = saved < 0;
  const abs = Math.abs(saved);
  const dec = abs >= 99.9 ? 3 : abs >= 99 ? 2 : 1;
  return { text: `${grew ? "+" : "−"}${fmtLocal(abs, dec)} %`, grew,
           kept: Math.min(100, (out / orig) * 100) };
}
function fmtFactor(orig, out) {
  if (!orig || !out || out >= orig) return "";
  const f = orig / out;
  return t("factor", { f: f >= 100 ? fmtLocal(Math.round(f)) : fmtLocal(f, 1) });
}

/* ---------- Add files ---------- */
// The same extensions the server accepts (VIDEO_ENDELSER in server.py)
const VIDEO_EXT = [".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"];
const isVideo = (f) => VIDEO_EXT.some(e => f.name.toLowerCase().endsWith(e));
let skipped = null;     // { count, added }: shown in the queue box when a folder had non-videos

// files: list of File or { file, path } (path = path inside the dropped folder)
function addFiles(fileList) {
  let skip = 0, added = 0;
  for (const entry of fileList) {
    const f = entry instanceof File ? entry : entry.file;
    const path = entry instanceof File ? (f.webkitRelativePath || "") : entry.path;
    if (!isVideo(f)) { skip++; continue; }
    added++;
    const item = { id: nextId++, file: f, path, status: "queued", progress: 0, choice: null };
    if (f.size > MAX_FILE_MB * 1024 * 1024) {
      item.status = "error";
      item.error = { key: "too_big", vars: { size: fmtBytes(f.size), mb: MAX_FILE_MB } };
    }
    items.push(item);
  }
  skipped = skip ? { count: skip, added } : null;
  render();
}

// Walks a dropped folder (and its subfolders) and collects all files
async function readEntry(entry, out) {
  if (entry.isFile) {
    const file = await new Promise((res, rej) => entry.file(res, rej));
    out.push({ file, path: entry.fullPath.replace(/^\//, "") });
  } else if (entry.isDirectory) {
    const reader = entry.createReader();
    // readEntries returns only part at a time; read until it returns an empty list
    for (;;) {
      const batch = await new Promise((res, rej) => reader.readEntries(res, rej));
      if (!batch.length) break;
      for (const child of batch) await readEntry(child, out);
    }
  }
}

async function filesFromDrop(dt) {
  // Entries must be taken right in the drop event, before any await
  const entries = [...(dt.items || [])]
    .map(i => i.kind === "file" && i.webkitGetAsEntry ? i.webkitGetAsEntry() : null);
  if (!entries.length || entries.some(e => !e)) return [...dt.files];   // older browsers
  const out = [];
  for (const e of entries) {
    try { await readEntry(e, out); } catch (err) { console.warn("Could not read", e.fullPath, err); }
  }
  out.sort((a, b) => a.path.localeCompare(b.path, LOCALES[LANG]));
  return out;
}

fileInput.addEventListener("change", () => { addFiles(fileInput.files); fileInput.value = ""; });
const folderInput = $("#folderInput");
$("#folderBtn").addEventListener("click", () => folderInput.click());
folderInput.addEventListener("change", () => {
  const files = [...folderInput.files].sort((a, b) =>
    a.webkitRelativePath.localeCompare(b.webkitRelativePath, LOCALES[LANG]));
  addFiles(files); folderInput.value = "";
});
dropZone.addEventListener("keydown", (e) => {
  if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fileInput.click(); }
});
["dragenter", "dragover"].forEach(ev => dropZone.addEventListener(ev, (e) => {
  e.preventDefault(); dropZone.classList.add("is-dragover");
}));
["dragleave", "drop"].forEach(ev => dropZone.addEventListener(ev, (e) => {
  e.preventDefault(); dropZone.classList.remove("is-dragover");
}));
dropZone.addEventListener("drop", async (e) => {
  if (!e.dataTransfer.items.length && !e.dataTransfer.files.length) return;
  addFiles(await filesFromDrop(e.dataTransfer));
});

/* ---------- Rendering ---------- */
function render() {
  renderQueue();
  const pending = items.some(i => i.status === "queued");
  startBtn.disabled = running || !pending;
  speakerOptions.disabled = running;
  clearBtn.disabled = running || items.length === 0;
  updateSteps();
  renderResult();
}

// Left: all chosen videos and which one is being processed now
function renderQueue() {
  list.querySelectorAll(".is-path-row").forEach(n => n.remove());
  emptyState.hidden = items.length > 0;

  const real = items.filter(i => i.status !== "duplicate");
  const dups = items.length - real.length;
  const total = real.length;
  const finished = real.filter(i => i.status === "done" || i.status === "error").length;
  const active = items.find(i => i.status === "uploading" || i.status === "processing");
  const skippedNote = skipped
    ? t("skipped", { n: fmtLocal(skipped.count), count: skipped.count }) +
      (skipped.added ? "" : t("none_found"))
    : "";
  queueHead.hidden = items.length === 0 && !skippedNote;
  if (items.length || skippedNote) {
    const pct = total ? (finished / total) * 100 : 0;
    const label = active
      ? `<span class="is-queue-now"><span class="is-spinner" aria-hidden="true"></span>${t(active.status === "uploading" ? "q_sending" : "q_processing")} <b>${escapeHtml(active.file.name)}</b></span>`
      : `<span class="is-queue-now">${t(!total ? "q_none" : finished === total ? "q_all_done" : running ? "q_starting" : "q_ready")}</span>`;
    queueHead.innerHTML = `
      <div class="is-queue-line">${label}<span class="is-queue-count">${t("q_count", { done: finished, total })}</span></div>
      <div class="is-mini-bar"><div style="width:${pct}%"></div></div>
      ${skippedNote ? `<div class="is-queue-skip">${escapeHtml(skippedNote)}</div>` : ""}
      ${dups ? `<div class="is-queue-dups">${t("dups_removed", { n: dups, count: dups })}</div>` : ""}`;
  }

  for (const it of items) {
    const row = document.createElement("div");
    row.className = "is-path-row is-row--" + it.status;
    row.setAttribute("role", "listitem");
    row.dataset.id = it.id;
    let meta = `<span class="is-chip">${fmtBytes(it.file.size)}</span>`;
    if (it.status === "uploading") meta += `<span>${t("row_uploaded", { pct: Math.round(it.progress) })}</span>`;
    if (it.status === "processing") meta += `<span>${t("row_creating")}</span>`;
    if (it.status === "done") meta += `<span class="is-good-text">${fmtSaved(it.file.size, it.outSize).text}</span>`;
    if (it.status === "error") meta += `<span class="is-bad-text">${t("row_failed")}</span>`;
    if (it.status === "duplicate") meta += `<span class="is-bad-text">${t("row_dup", { name: escapeHtml(it.dupOf) })}</span>`;
    let bar = "";
    if (it.status === "uploading") bar = `<div class="is-mini-bar"><div style="width:${it.progress}%"></div></div>`;
    if (it.status === "processing") bar = `<div class="is-mini-bar is-indeterminate"><div></div></div>`;
    const removable = !running && it.status !== "uploading" && it.status !== "processing";
    row.innerHTML = `
      <span class="is-method-badge" data-status="${it.status}">${statusLabel(it.status)}</span>
      <span class="is-row__main">
        <div class="is-row__name" title="${escapeHtml(it.path || it.file.name)}">${it.path && it.path.includes("/")
          ? `<span class="is-row__dir">${escapeHtml(it.path.slice(0, it.path.lastIndexOf("/") + 1))}</span>` : ""}${escapeHtml(it.file.name)}</div>
        <div class="is-row__meta">${meta}</div>
        ${bar}
      </span>
      <span>${removable ? `<button class="is-x" type="button" aria-label="${t("remove_aria", { name: escapeHtml(it.file.name) })}">×</button>` : ""}</span>`;
    const x = row.querySelector(".is-x");
    if (x) x.addEventListener("click", () => { items = items.filter(i => i !== it); render(); });
    list.appendChild(row);
  }
}

function updateSteps() {
  let step = 1;
  if (running) step = 2;
  else if (items.some(i => i.status === "done")) step = 3;
  document.querySelectorAll(".is-step").forEach(el => {
    el.classList.toggle("is-active", Number(el.dataset.step) === step);
  });
}

const DL_ICON = `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 4v12M7 11l5 5 5-5M5 20h14"/></svg>`;

// Right: list of finished videos + total at the bottom
function renderResult() {
  const done = items.filter(i => i.status === "done");
  const failed = items.filter(i => i.status === "error");
  const dups = items.filter(i => i.status === "duplicate");
  if (!done.length && !failed.length && !dups.length) {
    resultBody.innerHTML = `<p class="is-placeholder">${t("results_placeholder")}</p>`;
    return;
  }

  let html = `<div class="is-res-list" role="list">`;
  for (const it of done) {
    const s = fmtSaved(it.file.size, it.outSize);
    const factor = fmtFactor(it.file.size, it.outSize);
    html += `
      <div class="is-res-row" role="listitem">
        <div class="is-res-top">
          <div class="is-row__name" title="${escapeHtml(it.file.name)}">${escapeHtml(it.file.name)}</div>
          <span class="is-res-pct ${s.grew ? "is-bad-text" : "is-good-text"}">${s.text}</span>
        </div>
        <div class="is-res-bottom">
          <span class="is-row__meta">${fmtBytes(it.file.size)} → <b>${fmtBytes(it.outSize)}</b>${factor ? `<span class="is-chip">${factor}</span>` : ""}${it.cached ? `<span class="is-chip">${t("already_existed")}</span>` : ""}</span>
          <button class="is-btn is-btn--ghost is-btn--sm" type="button" data-dl="${it.id}" aria-label="${t("download_aria", { name: escapeHtml(it.file.name) })}">${DL_ICON}.md</button>
        </div>
        <div class="is-dist-bar is-dist-bar--thin" aria-hidden="true">
          <div style="width:${Math.max(s.kept, 0.6)}%;background:linear-gradient(90deg,#1174E6,#00A9CE)"></div>
        </div>
        ${it.fileId ? `<div class="is-res-warn">SHA-256 <span class="is-mono" title="${escapeHtml(it.fileId)}">${escapeHtml(it.fileId.slice(0, 16))}…</span></div>` : ""}
        ${it.warning ? `<div class="is-res-warn">${t("missing", { warning: escapeHtml(it.warning) })}</div>` : ""}
      </div>`;
  }
  for (const it of failed) {
    html += `
      <div class="is-res-row is-res-row--error" role="listitem">
        <div class="is-res-top">
          <div class="is-row__name" title="${escapeHtml(it.file.name)}">${escapeHtml(it.file.name)}</div>
          <span class="is-res-pct is-bad-text">${t("res_error")}</span>
        </div>
        <div class="is-res-warn">${escapeHtml(errText(it.error))}</div>
        ${it.file.size <= MAX_FILE_MB * 1024 * 1024 && !running
          ? `<div><button class="is-btn is-btn--ghost is-btn--sm" type="button" data-retry="${it.id}">${t("retry")}</button></div>` : ""}
      </div>`;
  }
  html += `</div>`;

  if (done.length || dups.length) {
    // Removed duplicates count as saved space: their whole size before, 0 after
    const dupSize = dups.reduce((a, i) => a + i.file.size, 0);
    const notesOrig = done.reduce((a, i) => a + i.file.size, 0);
    const orig = notesOrig + dupSize;
    const out = done.reduce((a, i) => a + i.outSize, 0);
    const s = fmtSaved(orig, out);
    const factor = fmtFactor(orig, out);
    html += `
      <div class="is-total">
        <div class="is-total__title">${t("total_for", { n: done.length + dups.length, count: done.length + dups.length })}</div>
        <div class="is-stats">
          <div><div class="is-stat__num">${fmtBytes(orig)}</div><div class="is-stat__label">${t("before")}</div></div>
          <div><div class="is-stat__num">${fmtBytes(out)}</div><div class="is-stat__label">${t("after")}</div></div>
          <div><div class="is-stat__num ${s.grew ? "is-bad" : "is-good"}">${s.text}</div><div class="is-stat__label">${t(s.grew ? "larger" : "smaller")}</div></div>
        </div>
        <div class="is-dist-bar" role="img" aria-label="${t("kept_aria", { pct: fmtLocal(s.kept, 2) })}">
          <div style="width:${Math.max(s.kept, 0.6)}%;background:linear-gradient(90deg,#1174E6,#00A9CE)"></div>
          <div style="flex:1;background:rgba(46,125,50,.55)"></div>
        </div>
        <div class="is-legend">
          <span><i style="background:#1174E6"></i>${t("legend_kept", { size: fmtBytes(out) })}</span>
          <span><i style="background:#2E7D32"></i>${t("legend_saved", { size: fmtBytes(Math.max(orig - out, 0)) })}</span>
          ${factor ? `<span>${factor}</span>` : ""}
        </div>
        ${dups.length ? `
        <div class="is-total__split">
          <div><span>${t("split_notes", { n: done.length, count: done.length })}</span><b>${fmtBytes(notesOrig)} → ${fmtBytes(out)}</b></div>
          <div class="is-total__dup"><span>${t("split_dups", { n: dups.length, count: dups.length })}</span><b>${fmtBytes(dupSize)} → 0 B</b></div>
        </div>` : ""}
      </div>`;
  }
  resultBody.innerHTML = html;

  resultBody.querySelectorAll("[data-dl]").forEach(b => b.addEventListener("click", () => {
    const it = items.find(i => i.id === Number(b.dataset.dl)); if (it) download(it);
  }));
  resultBody.querySelectorAll("[data-retry]").forEach(b => b.addEventListener("click", () => {
    const it = items.find(i => i.id === Number(b.dataset.retry)); if (!it) return;
    it.status = "queued"; it.error = null; it.progress = 0; render();
  }));
}

function download(it) {
  const url = URL.createObjectURL(it.outBlob);
  const a = document.createElement("a");
  a.href = url; a.download = it.outName;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}

/* ---------- Upload ---------- */
function filenameFromHeader(header) {
  if (!header) return null;
  const star = /filename\*\s*=\s*(?:UTF-8'')?([^;]+)/i.exec(header);
  if (star) { try { return decodeURIComponent(star[1].trim().replace(/^"|"$/g, "")); } catch (e) {} }
  const plain = /filename\s*=\s*"?([^";]+)"?/i.exec(header);
  return plain ? plain[1].trim() : null;
}

function uploadOne(it) {
  return new Promise((resolve) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", API_URL);
    xhr.setRequestHeader("X-Lang", LANG);       // server messages in the page language
    xhr.responseType = "blob";

    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) { it.progress = (e.loaded / e.total) * 100; render(); }
    };
    xhr.upload.onload = () => { it.status = "processing"; render(); };

    xhr.onload = async () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        const blob = xhr.response;
        it.outBlob = blob;
        it.outSize = blob.size;
        it.outName = filenameFromHeader(xhr.getResponseHeader("Content-Disposition"))
                     || it.file.name.replace(/\.[^.]+$/, "") + ".md";
        it.fileId = xhr.getResponseHeader("X-File-Id");
        it.cached = xhr.getResponseHeader("X-Cached") === "1";
        const warn = xhr.getResponseHeader("X-Warning");
        it.warning = warn ? decodeURIComponent(warn) : "";
        it.status = "done";
        // Same file hash as a video already done in the list = duplicate
        const original = it.fileId && items.find(i => i !== it && i.status === "done" && i.fileId === it.fileId);
        if (original) markDuplicate(it, original);
      } else {
        let detail = "";
        try {
          const txt = await xhr.response.text();
          try { const j = JSON.parse(txt); detail = j.detail || j.error || j.message || txt; }
          catch (e) { detail = txt; }
        } catch (e) {}
        it.status = "error";
        it.error = { key: "server_status", vars: { status: xhr.status },
                     detail: detail ? String(detail).slice(0, 300) : "" };
      }
      resolve();
    };
    xhr.onerror = () => {
      it.status = "error";
      it.error = { key: "unreachable_upload", vars: { url: API_URL } };
      resolve();
    };

    const fd = new FormData();
    fd.append(FIELD_NAME, it.file, it.file.name);
    it.choice = speakerChoice();               // the choice when the file is sent
    fd.append("speakers", it.choice.speakers ? "1" : "0");
    if (it.choice.num) fd.append("num_speakers", String(it.choice.num));
    fd.append("summary", it.choice.summary ? "1" : "0");
    fd.append("keywords", it.choice.keywords ? "1" : "0");
    it.status = "uploading"; it.progress = 0; render();
    xhr.send(fd);
  });
}

startBtn.addEventListener("click", async () => {
  running = true; render();
  for (const it of items) {
    if (it.status !== "queued") continue;
    await uploadOne(it);
    render();
  }
  running = false; render();
});

clearBtn.addEventListener("click", () => { items = []; skipped = null; render(); });

render();
