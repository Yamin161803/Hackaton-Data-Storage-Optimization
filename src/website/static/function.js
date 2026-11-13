/* ==============================================================
   Inställningar – anpassa efter er Python-backend
   ============================================================== */
const BACKEND      = "http://localhost:8000"; // adressen där app.py körs
// Serveras sidan av Flask (port 8000) används relativ adress, annars full adress
const API_URL      = (location.origin === BACKEND || location.port === "8000")
                     ? "/api/compress"
                     : `${BACKEND}/api/compress`;
const FIELD_NAME   = "file";          // namnet på fältet i multipart/form-data
const MAX_FILE_MB  = 2048;            // visas för användaren + kontrolleras före uppladdning

/* ============================================================== */
const $ = (s) => document.querySelector(s);
const fileInput = $("#fileInput"), dropZone = $("#dropZone"), list = $("#fileList");
const startBtn = $("#startBtn"), clearBtn = $("#clearBtn"), resultBody = $("#resultBody");
const emptyState = $("#emptyState");
const speakersInput = $("#speakersInput"), numSpeakersInput = $("#numSpeakersInput");
const numSpeakersLabel = $("#numSpeakersLabel"), speakerOptions = $("#speakerOptions");

/* ---------- Val: vem som pratar ---------- */
function speakerChoice() {
  const n = parseInt(numSpeakersInput.value, 10);
  return { speakers: speakersInput.checked, num: speakersInput.checked && n > 0 ? n : null };
}
function updateSpeakerOptions() { numSpeakersLabel.hidden = !speakersInput.checked; }
speakersInput.addEventListener("change", updateSpeakerOptions);
updateSpeakerOptions();

$("#apiChip .is-mono").textContent = API_URL;
$("#limitHint").textContent = `Videofiler (mp4, mov, mkv, webm, avi, m4v), upp till ${MAX_FILE_MB / 1024} GB per fil`;

/* ---------- Tema ---------- */
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

/* ---------- Tillstånd ---------- */
let items = [];           // { id, file, status, progress, outBlob, outName, outSize, error }
let running = false;
let nextId = 1;
const queueHead = $("#queueHead");

const STATUS_LABEL = {
  queued: "I KÖ", uploading: "SKICKAR", processing: "ARBETAR", done: "KLAR", error: "FEL",
  duplicate: "DUBBLETT"
};

// Dubblett = samma video-id (hash av videons innehåll, räknas ut av servern).
// Namnet spelar ingen roll. Dubbletten räknas som borttagen: ingen nedladdning,
// hela storleken räknas som sparad i totalen.
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
// Hur mycket mindre: "−99,98 %" (fler decimaler när nästan allt försvann)
function fmtSaved(orig, out) {
  if (!orig) return { text: "–", grew: false, kept: 100 };
  const saved = (1 - out / orig) * 100;
  const grew = saved < 0;
  const abs = Math.abs(saved);
  const dec = abs >= 99.9 ? 3 : abs >= 99 ? 2 : 1;
  return { text: `${grew ? "+" : "−"}${abs.toFixed(dec).replace(".", ",")} %`, grew,
           kept: Math.min(100, (out / orig) * 100) };
}
function fmtFactor(orig, out) {
  if (!orig || !out || out >= orig) return "";
  const f = orig / out;
  return `${f >= 100 ? Math.round(f).toLocaleString("sv-SE") : f.toFixed(1).replace(".", ",")}× mindre`;
}

/* ---------- Lägg till filer ---------- */
// Samma ändelser som servern tar emot (VIDEO_ENDELSER i server.py)
const VIDEO_EXT = [".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"];
const isVideo = (f) => VIDEO_EXT.some(e => f.name.toLowerCase().endsWith(e));
let skippedNote = "";   // visas i kö-rutan när en mapp innehöll annat än videor

// files: lista av File eller { file, path } (path = sökväg i mappen som släpptes)
function addFiles(fileList) {
  let skipped = 0, added = 0;
  for (const entry of fileList) {
    const f = entry instanceof File ? entry : entry.file;
    const path = entry instanceof File ? (f.webkitRelativePath || "") : entry.path;
    if (!isVideo(f)) { skipped++; continue; }
    added++;
    const item = { id: nextId++, file: f, path, status: "queued", progress: 0, choice: null };
    if (f.size > MAX_FILE_MB * 1024 * 1024) {
      item.status = "error";
      item.error = `Filen är ${fmtBytes(f.size)}. Maxstorleken är ${MAX_FILE_MB} MB.`;
    }
    items.push(item);
  }
  skippedNote = skipped
    ? `${skipped} ${skipped === 1 ? "fil" : "filer"} som inte är video hoppades över` +
      (added ? "" : " – inga videor hittades")
    : "";
  render();
}

// Går igenom en släppt mapp (och dess undermappar) och samlar alla filer
async function readEntry(entry, out) {
  if (entry.isFile) {
    const file = await new Promise((res, rej) => entry.file(res, rej));
    out.push({ file, path: entry.fullPath.replace(/^\//, "") });
  } else if (entry.isDirectory) {
    const reader = entry.createReader();
    // readEntries ger bara en del åt gången; läs tills den returnerar en tom lista
    for (;;) {
      const batch = await new Promise((res, rej) => reader.readEntries(res, rej));
      if (!batch.length) break;
      for (const child of batch) await readEntry(child, out);
    }
  }
}

async function filesFromDrop(dt) {
  // Entries måste hämtas direkt i drop-händelsen, innan något await
  const entries = [...(dt.items || [])]
    .map(i => i.kind === "file" && i.webkitGetAsEntry ? i.webkitGetAsEntry() : null);
  if (!entries.length || entries.some(e => !e)) return [...dt.files];   // äldre webbläsare
  const out = [];
  for (const e of entries) {
    try { await readEntry(e, out); } catch (err) { console.warn("Kunde inte läsa", e.fullPath, err); }
  }
  out.sort((a, b) => a.path.localeCompare(b.path, "sv"));
  return out;
}

fileInput.addEventListener("change", () => { addFiles(fileInput.files); fileInput.value = ""; });
const folderInput = $("#folderInput");
$("#folderBtn").addEventListener("click", () => folderInput.click());
folderInput.addEventListener("change", () => {
  const files = [...folderInput.files].sort((a, b) =>
    a.webkitRelativePath.localeCompare(b.webkitRelativePath, "sv"));
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

// Vänster: alla valda videor och vilken som bearbetas just nu
function renderQueue() {
  list.querySelectorAll(".is-path-row").forEach(n => n.remove());
  emptyState.hidden = items.length > 0;

  const real = items.filter(i => i.status !== "duplicate");
  const dups = items.length - real.length;
  const total = real.length;
  const finished = real.filter(i => i.status === "done" || i.status === "error").length;
  const active = items.find(i => i.status === "uploading" || i.status === "processing");
  queueHead.hidden = items.length === 0 && !skippedNote;
  if (items.length || skippedNote) {
    const pct = total ? (finished / total) * 100 : 0;
    const label = active
      ? `<span class="is-queue-now"><span class="is-spinner" aria-hidden="true"></span>${active.status === "uploading" ? "Skickar" : "Bearbetar"} <b>${escapeHtml(active.file.name)}</b></span>`
      : `<span class="is-queue-now">${!total ? "Inga videor att bearbeta" : finished === total ? "Alla videor är klara" : running ? "Startar…" : "Redo att starta"}</span>`;
    queueHead.innerHTML = `
      <div class="is-queue-line">${label}<span class="is-queue-count">${finished} av ${total} bearbetade</span></div>
      <div class="is-mini-bar"><div style="width:${pct}%"></div></div>
      ${skippedNote ? `<div class="is-queue-skip">${escapeHtml(skippedNote)}</div>` : ""}
      ${dups ? `<div class="is-queue-dups">${dups} ${dups === 1 ? "dubblett togs bort" : "dubbletter togs bort"}</div>` : ""}`;
  }

  for (const it of items) {
    const row = document.createElement("div");
    row.className = "is-path-row is-row--" + it.status;
    row.setAttribute("role", "listitem");
    row.dataset.id = it.id;
    let meta = `<span class="is-chip">${fmtBytes(it.file.size)}</span>`;
    if (it.status === "uploading") meta += `<span>${Math.round(it.progress)} % uppladdat</span>`;
    if (it.status === "processing") meta += `<span>Skapar anteckningar…</span>`;
    if (it.status === "done") meta += `<span class="is-good-text">${fmtSaved(it.file.size, it.outSize).text}</span>`;
    if (it.status === "error") meta += `<span class="is-bad-text">Misslyckades</span>`;
    if (it.status === "duplicate") meta += `<span class="is-bad-text">Samma hash som ${escapeHtml(it.dupOf)} – borttagen</span>`;
    let bar = "";
    if (it.status === "uploading") bar = `<div class="is-mini-bar"><div style="width:${it.progress}%"></div></div>`;
    if (it.status === "processing") bar = `<div class="is-mini-bar is-indeterminate"><div></div></div>`;
    const removable = !running && it.status !== "uploading" && it.status !== "processing";
    row.innerHTML = `
      <span class="is-method-badge" data-status="${it.status}">${STATUS_LABEL[it.status]}</span>
      <span class="is-row__main">
        <div class="is-row__name" title="${escapeHtml(it.path || it.file.name)}">${it.path && it.path.includes("/")
          ? `<span class="is-row__dir">${escapeHtml(it.path.slice(0, it.path.lastIndexOf("/") + 1))}</span>` : ""}${escapeHtml(it.file.name)}</div>
        <div class="is-row__meta">${meta}</div>
        ${bar}
      </span>
      <span>${removable ? `<button class="is-x" type="button" aria-label="Ta bort ${escapeHtml(it.file.name)}">×</button>` : ""}</span>`;
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

// Höger: lista över färdiga videor + total längst ner
function renderResult() {
  const done = items.filter(i => i.status === "done");
  const failed = items.filter(i => i.status === "error");
  const dups = items.filter(i => i.status === "duplicate");
  if (!done.length && !failed.length && !dups.length) {
    resultBody.innerHTML = `<p class="is-placeholder">Färdiga videor visas här med hur mycket mindre de blev. Längst ner står den totala besparingen.</p>`;
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
          <span class="is-row__meta">${fmtBytes(it.file.size)} → <b>${fmtBytes(it.outSize)}</b>${factor ? `<span class="is-chip">${factor}</span>` : ""}${it.cached ? `<span class="is-chip">fanns redan</span>` : ""}</span>
          <button class="is-btn is-btn--ghost is-btn--sm" type="button" data-dl="${it.id}" aria-label="Ladda ner anteckningar för ${escapeHtml(it.file.name)}">${DL_ICON}.md</button>
        </div>
        <div class="is-dist-bar is-dist-bar--thin" aria-hidden="true">
          <div style="width:${Math.max(s.kept, 0.6)}%;background:linear-gradient(90deg,#1174E6,#00A9CE)"></div>
        </div>
        ${it.warning ? `<div class="is-res-warn">Utan talare: ${escapeHtml(it.warning)}</div>` : ""}
      </div>`;
  }
  for (const it of failed) {
    html += `
      <div class="is-res-row is-res-row--error" role="listitem">
        <div class="is-res-top">
          <div class="is-row__name" title="${escapeHtml(it.file.name)}">${escapeHtml(it.file.name)}</div>
          <span class="is-res-pct is-bad-text">Fel</span>
        </div>
        <div class="is-res-warn">${escapeHtml(it.error || "Okänt fel.")}</div>
        ${it.file.size <= MAX_FILE_MB * 1024 * 1024 && !running
          ? `<div><button class="is-btn is-btn--ghost is-btn--sm" type="button" data-retry="${it.id}">Försök igen</button></div>` : ""}
      </div>`;
  }
  html += `</div>`;

  if (done.length || dups.length) {
    // Borttagna dubbletter räknas som sparat utrymme: hela storleken före, 0 efter
    const dupSize = dups.reduce((a, i) => a + i.file.size, 0);
    const notesOrig = done.reduce((a, i) => a + i.file.size, 0);
    const orig = notesOrig + dupSize;
    const out = done.reduce((a, i) => a + i.outSize, 0);
    const s = fmtSaved(orig, out);
    const factor = fmtFactor(orig, out);
    html += `
      <div class="is-total">
        <div class="is-total__title">Totalt för ${done.length + dups.length} ${done.length + dups.length === 1 ? "video" : "videor"}</div>
        <div class="is-stats">
          <div><div class="is-stat__num">${fmtBytes(orig)}</div><div class="is-stat__label">Före</div></div>
          <div><div class="is-stat__num">${fmtBytes(out)}</div><div class="is-stat__label">Efter</div></div>
          <div><div class="is-stat__num ${s.grew ? "is-bad" : "is-good"}">${s.text}</div><div class="is-stat__label">${s.grew ? "Större" : "Mindre"}</div></div>
        </div>
        <div class="is-dist-bar" role="img" aria-label="Kvar: ${s.kept.toFixed(2)} procent av originalet">
          <div style="width:${Math.max(s.kept, 0.6)}%;background:linear-gradient(90deg,#1174E6,#00A9CE)"></div>
          <div style="flex:1;background:rgba(46,125,50,.55)"></div>
        </div>
        <div class="is-legend">
          <span><i style="background:#1174E6"></i>Kvar ${fmtBytes(out)}</span>
          <span><i style="background:#2E7D32"></i>Sparat ${fmtBytes(Math.max(orig - out, 0))}</span>
          ${factor ? `<span>${factor}</span>` : ""}
        </div>
        ${dups.length ? `
        <div class="is-total__split">
          <div><span>${done.length} ${done.length === 1 ? "video" : "videor"} → anteckningar</span><b>${fmtBytes(notesOrig)} → ${fmtBytes(out)}</b></div>
          <div class="is-total__dup"><span>${dups.length} ${dups.length === 1 ? "dubblett borttagen" : "dubbletter borttagna"}</span><b>${fmtBytes(dupSize)} → 0 B</b></div>
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

/* ---------- Uppladdning ---------- */
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
        // Samma video-id (hash) som en video som redan är klar i listan = dubblett
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
        it.error = `Servern svarade med status ${xhr.status}.` + (detail ? ` ${String(detail).slice(0, 300)}` : "");
      }
      resolve();
    };
    xhr.onerror = () => {
      it.status = "error";
      it.error = `Kunde inte nå servern på ${API_URL}. Kontrollera att backend körs och att CORS tillåter den här sidan.`;
      resolve();
    };

    const fd = new FormData();
    fd.append(FIELD_NAME, it.file, it.file.name);
    it.choice = speakerChoice();               // valet när filen skickas
    fd.append("speakers", it.choice.speakers ? "1" : "0");
    if (it.choice.num) fd.append("num_speakers", String(it.choice.num));
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

clearBtn.addEventListener("click", () => { items = []; skippedNote = ""; render(); });

render();
