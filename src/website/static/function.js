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
const summaryInput = $("#summaryInput"), keywordsInput = $("#keywordsInput");

/* ---------- Val: vem som pratar ---------- */
function speakerChoice() {
  const n = parseInt(numSpeakersInput.value, 10);
  return { speakers: speakersInput.checked, num: speakersInput.checked && n > 0 ? n : null,
           summary: summaryInput.checked, keywords: keywordsInput.checked };
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
let selectedId = null;
let running = false;
let nextId = 1;

const STATUS_LABEL = {
  queued: "I KÖ", uploading: "SKICKAR", processing: "ARBETAR", done: "KLAR", error: "FEL"
};

function fmtBytes(n) {
  if (n == null) return "–";
  const u = ["B", "kB", "MB", "GB"]; let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(n < 10 && i > 0 ? 1 : 0)} ${u[i]}`;
}
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({ "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;" }[c]));
}

/* ---------- Lägg till filer ---------- */
function addFiles(fileList) {
  for (const f of fileList) {
    const item = { id: nextId++, file: f, status: "queued", progress: 0, choice: null };
    if (f.size > MAX_FILE_MB * 1024 * 1024) {
      item.status = "error";
      item.error = `Filen är ${fmtBytes(f.size)}. Maxstorleken är ${MAX_FILE_MB} MB.`;
    }
    items.push(item);
    if (selectedId == null) selectedId = item.id;
  }
  render();
}

fileInput.addEventListener("change", () => { addFiles(fileInput.files); fileInput.value = ""; });
dropZone.addEventListener("keydown", (e) => {
  if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fileInput.click(); }
});
["dragenter", "dragover"].forEach(ev => dropZone.addEventListener(ev, (e) => {
  e.preventDefault(); dropZone.classList.add("is-dragover");
}));
["dragleave", "drop"].forEach(ev => dropZone.addEventListener(ev, (e) => {
  e.preventDefault(); dropZone.classList.remove("is-dragover");
}));
dropZone.addEventListener("drop", (e) => { if (e.dataTransfer.files.length) addFiles(e.dataTransfer.files); });

/* ---------- Rendering ---------- */
function render() {
  list.querySelectorAll(".is-path-row").forEach(n => n.remove());
  emptyState.hidden = items.length > 0;

  for (const it of items) {
    const row = document.createElement("button");
    row.type = "button";
    row.className = "is-path-row" + (it.id === selectedId ? " is-selected" : "");
    row.setAttribute("role", "listitem");
    row.dataset.id = it.id;
    const showBar = it.status === "uploading" || it.status === "processing";
    const sizeInfo = it.status === "done"
      ? `${fmtBytes(it.file.size)} → ${fmtBytes(it.outSize)}`
      : fmtBytes(it.file.size);
    row.innerHTML = `
      <span class="is-method-badge" data-status="${it.status}">${STATUS_LABEL[it.status]}</span>
      <span class="is-row__main">
        <div class="is-row__name">${escapeHtml(it.file.name)}</div>
        <div class="is-row__meta"><span class="is-chip">${sizeInfo}</span>${
          it.status === "uploading" ? `<span>${Math.round(it.progress)} %</span>` : ""}</div>
        ${showBar ? `<div class="is-mini-bar"><div style="width:${it.status === "processing" ? 100 : it.progress}%"></div></div>` : ""}
      </span>
      <span>${it.status === "done" ? downloadIcon() : ""}</span>`;
    row.addEventListener("click", () => select(it.id));
    list.appendChild(row);
  }

  const pending = items.some(i => i.status === "queued");
  startBtn.disabled = running || !pending;
  speakerOptions.disabled = running;
  clearBtn.disabled = running || items.length === 0;
  updateSteps();
  renderResult();
}

function downloadIcon() {
  return `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="var(--green)" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 6L9 17l-5-5"/></svg>`;
}

function select(id) {
  selectedId = id;
  render();
  const row = list.querySelector(`[data-id="${id}"]`);
  if (row) { row.classList.add("is-pulse"); row.addEventListener("animationend", () => row.classList.remove("is-pulse"), { once: true }); }
}

function updateSteps() {
  let step = 1;
  if (running) step = 2;
  else if (items.some(i => i.status === "done")) step = 3;
  document.querySelectorAll(".is-step").forEach(el => {
    el.classList.toggle("is-active", Number(el.dataset.step) === step);
  });
}

function renderResult() {
  const it = items.find(i => i.id === selectedId);
  if (!it) {
    resultBody.innerHTML = `<p class="is-placeholder">Välj en fil i listan för att se hur mycket den krympte. Färdiga filer går att ladda ner härifrån.</p>`;
    return;
  }
  const name = `<div class="is-mono" style="margin-bottom:16px;word-break:break-all">${escapeHtml(it.file.name)}</div>`;

  if (it.status === "error") {
    resultBody.innerHTML = name + `
      <div class="is-op-card" data-kind="error">
        <strong>Anteckningarna kunde inte skapas</strong>${escapeHtml(it.error || "Okänt fel.")}
      </div>
      <div class="is-actions"><button class="is-btn is-btn--ghost is-btn--sm" id="retryBtn" type="button">Försök igen</button></div>`;
    $("#retryBtn").addEventListener("click", () => {
      if (it.file.size > MAX_FILE_MB * 1024 * 1024) return;
      it.status = "queued"; it.error = null; it.progress = 0; render();
    });
    return;
  }

  if (it.status !== "done") {
    const msg = {
      queued: "Videon väntar. Klicka på Skapa anteckningar för att starta.",
      uploading: `Skickar filen till servern (${Math.round(it.progress)} %).`,
      processing: it.choice && it.choice.speakers
        ? "Servern gör anteckningar av videon och tar reda på vem som pratar. Det tar längre tid, ungefär lika lång tid som videon."
        : "Servern gör anteckningar av videon: bildrutor, transkribering och sammanfattning. Det tar några minuter för långa videor."
    }[it.status];
    resultBody.innerHTML = name + `
      <div class="is-stats">
        <div><div class="is-stat__num">${fmtBytes(it.file.size)}</div><div class="is-stat__label">Originalstorlek</div></div>
        <div><div class="is-stat__num">–</div><div class="is-stat__label">Komprimerad</div></div>
        <div><div class="is-stat__num">–</div><div class="is-stat__label">Besparing</div></div>
      </div>
      <div class="is-op-card" data-kind="info">${msg}</div>`;
    return;
  }

  const orig = it.file.size, out = it.outSize;
  const saved = orig > 0 ? (1 - out / orig) * 100 : 0;
  const keptPct = orig > 0 ? Math.min(100, (out / orig) * 100) : 100;
  const grew = out > orig;

  resultBody.innerHTML = name + `
    <div class="is-stats">
      <div><div class="is-stat__num">${fmtBytes(orig)}</div><div class="is-stat__label">Originalstorlek</div></div>
      <div><div class="is-stat__num">${fmtBytes(out)}</div><div class="is-stat__label">Komprimerad</div></div>
      <div><div class="is-stat__num ${grew ? "is-bad" : "is-good"}">${grew ? "+" : "−"}${Math.abs(saved).toFixed(1)} %</div><div class="is-stat__label">${grew ? "Större än originalet" : "Besparing"}</div></div>
    </div>
    <div class="is-dist-bar" role="img" aria-label="Komprimerad storlek ${keptPct.toFixed(0)} procent av originalet">
      <div style="width:${keptPct}%;background:linear-gradient(90deg,#1174E6,#00A9CE)"></div>
      <div style="width:${100 - keptPct}%;background:rgba(46,125,50,.55)"></div>
    </div>
    <div class="is-legend">
      <span><i style="background:#1174E6"></i>Kvar</span>
      <span><i style="background:#2E7D32"></i>Sparat</span>
    </div>
    <div class="is-op-card" data-kind="ok">
      <strong>${it.cached ? "Videon fanns redan – sparade anteckningar" : "Klar att ladda ner"}</strong>
      ${it.choice && it.choice.speakers && !it.warning ? "Med talare. " : ""}
      Sparas som <span class="is-mono is-token">${escapeHtml(it.outName)}</span>${it.fileId ? `<br>SHA-256 <span class="is-mono is-token" title="${escapeHtml(it.fileId)}">${escapeHtml(it.fileId.slice(0, 16))}…</span>` : ""}
    </div>
    ${it.warning ? `<div class="is-op-card" data-kind="error"><strong>Något saknas</strong>${escapeHtml(it.warning)}</div>` : ""}
    <div class="is-actions">
      <button class="is-btn" id="dlBtn" type="button">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M12 4v12M7 11l5 5 5-5M5 20h14"/></svg>
        Ladda ner
      </button>
    </div>`;
  $("#dlBtn").addEventListener("click", () => download(it));
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
    selectedId = it.id;
    await uploadOne(it);
    render();
  }
  running = false; render();
});

clearBtn.addEventListener("click", () => { items = []; selectedId = null; render(); });

render();
