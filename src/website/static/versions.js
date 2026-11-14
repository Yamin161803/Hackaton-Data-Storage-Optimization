"use strict";
/* /versions page: left table of all version families, right a zoomable ECharts
   graph of the selected family. Click a node for info + download. */

const $ = (s) => document.querySelector(s);

/* theme toggle — dark mode is the DEFAULT on the versions page (unless the user
   has explicitly chosen light before). */
(function initTheme() {
  let saved = null;
  try { saved = localStorage.getItem("theme"); } catch (e) {}
  if (saved !== "light") document.body.classList.add("body--dark");   // default dark
})();
$("#themeBtn").addEventListener("click", () => {
  document.body.classList.toggle("body--dark");
  try { localStorage.setItem("theme", document.body.classList.contains("body--dark") ? "dark" : "light"); } catch (e) {}
  if (_hg) drawHypergraph(_hg);   // node colours depend on theme
});

function human(n) {
  if (n == null) return "–";
  const u = ["B", "KB", "MB", "GB"]; let i = 0, v = n;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return fmtLocal(v, i === 0 ? 0 : 1) + " " + u[i];
}
function esc(s) { const d = document.createElement("div"); d.textContent = s == null ? "" : s; return d.innerHTML; }
function shortName(n) { return n.length > 26 ? n.slice(0, 24) + "…" : n; }
function cssVar(n) { return getComputedStyle(document.body).getPropertyValue(n).trim(); }

let _chart = null, _fam = null, _selectedRoot = null;
let _hg = null;           // hypergraph data for the selected family

function ensureChart() {
  if (_chart || typeof echarts === "undefined") return;
  _chart = echarts.init(document.getElementById("treeChart"));
  window.addEventListener("resize", () => _chart && _chart.resize());
  _chart.on("click", (p) => {
    if (p.data) onHyperClick(p.data);
  });
}

/* ---------- left table ---------- */
async function loadFamilies() {
  const list = $("#famList");
  let fams = [];
  try { fams = (await (await fetch("/api/tree/families")).json()).families || []; }
  catch (e) { list.innerHTML = `<p class="is-field__hint">${t("fam_load_failed")}</p>`; return; }
  if (!fams.length) { list.innerHTML = `<p class="is-placeholder">${t("fam_empty")}</p>`; return; }
  list.innerHTML = fams.map(f => `
    <div class="is-fam-row" role="row" data-root="${esc(f.root)}">
      <span class="is-chip">${esc(f.source_type.toUpperCase())}</span>
      <span class="is-fam-row__name">${esc(f.name)}</span>
      <span class="is-fam-row__meta">${t("n_versions", { n: f.size })}</span>
      <span class="is-fam-row__saved">${f.saved_bytes ? "−" + human(f.saved_bytes) : ""}</span>
    </div>`).join("");
  list.onclick = (e) => { const row = e.target.closest("[data-root]"); if (row) selectFamily(row.dataset.root); };

  // pick from ?doc= deep link, else the first family
  const params = new URLSearchParams(location.search);
  const want = params.get("doc");
  selectFamily(_selectedRoot || want || fams[0].root);
}

async function selectFamily(rootOrDocId) {
  document.querySelectorAll("#famList [data-root]").forEach(el =>
    el.setAttribute("aria-selected", el.dataset.root === String(rootOrDocId)));
  _selectedRoot = String(rootOrDocId);
  $("#nodePopover").hidden = true;
  let fam, hg;
  try {
    fam = await (await fetch(`/api/tree/${rootOrDocId}`)).json();
    hg = await (await fetch(`/api/tree/${rootOrDocId}/hypergraph`)).json();
  } catch (e) { return; }
  _fam = fam;
  _hg = hg;
  $("#graphName").textContent = (fam.nodes && fam.nodes[0]) ? fam.nodes[0].name : "Version graph";
  ensureChart();
  drawHypergraph(hg);
  $("#graphHint").textContent = "Version nodes (large) connect to the slides they contain. "
    + "Shared slides link multiple versions; red links mark slides shared with other families. "
    + "Click a version for size + download.";
  const s = fam.savings || {};
  const d = s.detail || { pages: {}, images: {} };
  const slidesSaved = Math.max(0, (d.pages.full || 0) - (d.pages.stored || 0));
  const picsSaved = Math.max(0, (d.images.full || 0) - (d.images.stored || 0));
  if (picsSaved > 0) {
    $("#treeSavings").innerHTML =
      `Slides <b>−${human(slidesSaved)}</b> · Pictures <b>−${human(picsSaved)}</b> · `
      + `Total <b>−${human(s.saved_bytes)}</b> (${(s.saved_percent || 0).toFixed(1)} %)`;
  } else {
    $("#treeSavings").innerHTML =
      `Saved <b>−${human(s.saved_bytes)}</b> (${(s.saved_percent || 0).toFixed(1)} % of ${human(s.full_bytes)})`;
  }
}

/* ---------- hypergraph view (slides + versions, fair-share sizing) ---------- */
function drawHypergraph(hg) {
  ensureChart();
  if (!_chart) return;
  const prim = cssVar("--primary"), green = cssVar("--green"), teal = cssVar("--teal"), text = cssVar("--text");

  // Fixed two-row layout: versions on a top horizontal line, slides on a
  // bottom horizontal line. ECharts layout:"none" uses our x/y coordinates.
  const W = 1000, TOP_Y = 90, BOT_Y = 360;
  const nodes = [];

  const nv = hg.versions.length;
  hg.versions.forEach((v, i) => {
    const x = nv === 1 ? W / 2 : 60 + (W - 120) * i / (nv - 1);
    nodes.push({
      id: "v:" + v.id, name: shortName(v.name), category: 0,
      x, y: TOP_Y, fixed: true,
      symbolSize: 30, symbol: "roundRect",
      _kind: "version", _id: v.id, _full: v.name,
      _original: v.original_bytes, _fair: v.fairshare_bytes,
      itemStyle: { color: green, borderColor: teal,
        borderWidth: v.id === String(hg.selected) ? 5 : 2 },
      label: { show: true, color: text, fontSize: 11, position: "top" },
    });
  });

  const ns = hg.slides.length;
  hg.slides.forEach((sl, i) => {
    const shared = sl.used_by > 1;
    const x = ns === 1 ? W / 2 : 60 + (W - 120) * i / (ns - 1);
    nodes.push({
      id: "s:" + sl.hash, name: String(i + 1),        // index label 1,2,3,4...
      _idx: i + 1,
      category: 1, x, y: BOT_Y, fixed: true,
      symbolSize: 22, symbol: "circle",
      _kind: "slide", _hash: sl.hash, _size: sl.size, _used: sl.used_by,
      _external: sl.external_uses || 0,
      itemStyle: { color: shared ? teal : prim, opacity: shared ? 1 : .8 },
      label: { show: true, color: "#06130d", fontSize: 11, fontWeight: "bold", position: "inside" },
    });
  });

  // Red "bridge" square below the slide row: any slide also used by a document
  // OUTSIDE this family connects to it (shares a version with another cluster).
  const bridged = hg.slides
    .map((sl, i) => ({ ...sl, _idx: i + 1 }))
    .filter(sl => (sl.external_uses || 0) > 0);
  const links = hg.edges.map(e => ({
    source: "v:" + e.version, target: "s:" + e.slide,
    lineStyle: { color: prim, width: 1.5, opacity: .4, curveness: 0 },
  }));
  const hasBridge = bridged.length > 0;
  if (hasBridge) {
    nodes.push({
      id: "bridge", name: "shared elsewhere", category: 2,
      x: W / 2, y: BOT_Y + 170, fixed: true,
      symbolSize: 36, symbol: "rect",
      _kind: "bridge", _count: bridged.length,
      _links: bridged.map(sl => ({ idx: sl._idx, external: sl.external_uses })),
      itemStyle: { color: "#C62828", borderColor: "#ff6b6b", borderWidth: 2 },
      label: { show: true, color: "#C62828", fontSize: 11, fontWeight: "bold", position: "bottom" },
    });
    for (const sl of bridged) {
      links.push({
        source: "s:" + sl.hash, target: "bridge",
        lineStyle: { color: "#C62828", width: 2, opacity: .75, curveness: 0 },
      });
    }
  }

  // Only advertise "Shared elsewhere" in the legend when such slides exist.
  const legendData = hasBridge ? ["Versions", "Slides", "Shared elsewhere"]
                               : ["Versions", "Slides"];
  const categories = hasBridge
    ? [{ name: "Versions" }, { name: "Slides" }, { name: "Shared elsewhere" }]
    : [{ name: "Versions" }, { name: "Slides" }];

  _chart.setOption({
    tooltip: { show: false },
    legend: [{ data: legendData, textStyle: { color: text }, top: 6 }],
    series: [{
      type: "graph", layout: "none", roam: true,
      categories: categories,
      data: nodes, links: links,
      emphasis: { focus: "adjacency" }, animationDuration: 300,
    }],
  }, true);
  _chart.resize();
}

function onHyperClick(d) {
  if (d._kind === "version") {
    const pop = $("#nodePopover");
    pop.innerHTML = `
      <button class="is-pop-close" id="popClose" aria-label="Close">×</button>
      <div class="is-pop-name">${esc(d._full)}</div>
      <dl class="is-kv">
        <dt>Original size</dt><dd>${human(d._original)}</dd>
        <dt>Fair-share stored</dt><dd>${human(d._fair)}</dd>
      </dl>
      <button class="is-btn is-btn--sm" id="popDl">Download this version</button>`;
    pop.hidden = false;
    $("#popClose").onclick = () => { pop.hidden = true; };
    $("#popDl").onclick = () => { window.location = `/api/tree/${d._id}/download`; };
  } else if (d._kind === "slide") {
    const pop = $("#nodePopover");
    pop.innerHTML = `
      <button class="is-pop-close" id="popClose" aria-label="Close">×</button>
      <div class="is-pop-name">Slide ${d._idx}</div>
      <dl class="is-kv">
        <dt>Size</dt><dd>${human(d._size)}</dd>
        <dt>Used by (this family)</dt><dd>${d._used} version${d._used === 1 ? "" : "s"}</dd>
        <dt>Shared elsewhere</dt><dd>${d._external} outside doc${d._external === 1 ? "" : "s"}</dd>
      </dl>`;
    pop.hidden = false;
    $("#popClose").onclick = () => { pop.hidden = true; };
  } else if (d._kind === "bridge") {
    const pop = $("#nodePopover");
    const rows = (d._links || []).map(l =>
      `<dt>Slide ${l.idx}</dt><dd>${l.external} outside doc${l.external === 1 ? "" : "s"}</dd>`).join("");
    pop.innerHTML = `
      <button class="is-pop-close" id="popClose" aria-label="Close">×</button>
      <div class="is-pop-name" style="color:#C62828">Shared elsewhere</div>
      <p class="is-field__hint">${d._count} slide${d._count === 1 ? "" : "s"} in this family also appear in documents outside it:</p>
      <dl class="is-kv">${rows}</dl>`;
    pop.hidden = false;
    $("#popClose").onclick = () => { pop.hidden = true; };
  }
}

/* ---------- toolbar ---------- */
$("#treeReset").addEventListener("click", () => { if (_hg) drawHypergraph(_hg); });
$("#treeFull").addEventListener("click", () => {
  const card = document.querySelector(".is-graph-card");
  if (document.fullscreenElement) document.exitFullscreen(); else card.requestFullscreen && card.requestFullscreen();
  setTimeout(() => _chart && _chart.resize(), 150);
});
document.addEventListener("fullscreenchange", () => setTimeout(() => _chart && _chart.resize(), 150));

/* language switch: rebuild what JS wrote (the static texts are done by i18n.js) */
document.addEventListener("langchange", loadFamilies);   // keeps the selected family

$("#graphName").textContent = t("vg_title");
loadFamilies();
