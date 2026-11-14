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
  if (_fam) draw(_fam);        // graph colours depend on theme
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

let _chart = null, _fam = null, _orient = "TB", _clickedId = null, _selectedRoot = null;

function ensureChart() {
  if (_chart || typeof echarts === "undefined") return;
  _chart = echarts.init(document.getElementById("treeChart"));
  window.addEventListener("resize", () => _chart && _chart.resize());
  _chart.on("click", (p) => {
    if (!p.data || !p.data._id) return;
    _clickedId = p.data._id;
    openPopover(p.data);
    draw(_fam);
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
  _clickedId = null;
  $("#nodePopover").hidden = true;
  let fam;
  try {
    const resp = await fetch(`/api/tree/${rootOrDocId}`);
    if (!resp.ok) return;
    fam = await resp.json();
  } catch (e) { return; }
  _fam = fam;
  $("#graphName").textContent = fam.nodes[0] ? fam.nodes[0].name : t("vg_title");
  ensureChart();
  draw(fam);
  const s = fam.savings || {};
  $("#treeSavings").innerHTML = t("vg_saved", { saved: human(s.saved_bytes), pct: fmtLocal(s.saved_percent || 0, 1), full: human(s.full_bytes) });
}

/* ---------- graph ---------- */
/* "shortened: +0 / −11 pages" in the page language. */
function changeText(e) {
  const rel = t("rel_" + e.relation);
  return t("rel_change", { rel: rel === "rel_" + e.relation ? e.relation : rel,
                           added: e.added, removed: e.removed });
}
function nodeChange(edge, d) {
  return edge ? changeText(edge) : t(d._selected ? "added_now" : "original_version");
}

/* Build ECharts GRAPH data (nodes + links). A version family is a DAG — it can
   have multiple roots and branches — so a `tree` series (single root) dropped
   most nodes. A `graph` series shows ALL nodes and their relationships. */
function toGraph(fam) {
  const nodes = fam.nodes.map((n, i) => {
    const isNew = n.id === fam.selected, isClicked = n.id === _clickedId;
    return {
      id: String(n.id), name: shortName(n.name),
      _id: n.id, _full: n.name, _pages: n.pages, _date: n.date, _selected: isNew,
      value: n.age_rank,
      itemStyle: {
        color: isNew ? cssVar("--green") : cssVar("--primary"),
        borderColor: isClicked ? cssVar("--teal") : (isNew ? cssVar("--green") : cssVar("--primary")),
        borderWidth: isClicked ? 5 : (isNew ? 4 : 2),
        shadowBlur: isClicked ? 22 : (isNew ? 14 : 0),
        shadowColor: isClicked ? cssVar("--teal") : cssVar("--green"),
      },
      symbolSize: isClicked ? 24 : (isNew ? 20 : 15),
      label: { show: true, position: "right", color: cssVar("--text"), fontSize: 12 },
    };
  });
  const links = (fam.edges || []).map(e => ({
    source: String(e.parent), target: String(e.child),
    _rel: e.relation, _added: e.added, _removed: e.removed,
    lineStyle: { color: cssVar("--primary"), width: 2, opacity: .55, curveness: .15 },
  }));
  return { nodes, links };
}

function draw(fam) {
  ensureChart();
  if (!_chart) return;
  const g = toGraph(fam);
  _chart.setOption({
    tooltip: {
      trigger: "item",
      formatter: (p) => {
        if (p.dataType === "edge") {
          return changeText({ relation: p.data._rel, added: p.data._added, removed: p.data._removed });
        }
        const d = p.data, e = (fam.edges || []).find(x => String(x.child) === d.id);
        return `<b>${esc(d._full)}</b><br>${t("n_pages", { n: d._pages })}<br>${(d._date || "").slice(0, 10)}<br>${nodeChange(e, d)}`;
      },
    },
    series: [{
      type: "graph",
      layout: g.links.length ? "force" : "circular",
      roam: true,                              // zoom + pan
      draggable: true,
      symbol: "circle",
      edgeSymbol: ["none", "arrow"], edgeSymbolSize: 8,
      force: { repulsion: 220, edgeLength: 90, gravity: .08 },
      data: g.nodes, links: g.links,
      lineStyle: { color: cssVar("--primary"), width: 2, opacity: .55, curveness: .15 },
      emphasis: { focus: "adjacency" },
      animationDuration: 400,
    }],
  }, true);
  _chart.resize();
}

function openPopover(d) {
  const pop = $("#nodePopover");
  const e = (_fam.edges || []).find(x => x.child === d._id);
  const change = nodeChange(e, d);
  pop.innerHTML = `
    <button class="is-pop-close" id="popClose" aria-label="${t("close")}">×</button>
    <div class="is-pop-name">${esc(d._full)}</div>
    <dl class="is-kv">
      <dt>${t("k_pages")}</dt><dd>${d._pages}</dd>
      <dt>${t("k_date")}</dt><dd>${(d._date || "").slice(0, 19).replace("T", " ")}</dd>
      <dt>${t("k_change")}</dt><dd>${change}</dd>
      ${d._selected ? `<dt>${t("k_status")}</dt><dd>${t("status_added")}</dd>` : ""}
    </dl>
    <button class="is-btn is-btn--sm" id="popDl">${t("download_version")}</button>`;
  pop.hidden = false;
  $("#popClose").onclick = () => { pop.hidden = true; _clickedId = null; draw(_fam); };
  $("#popDl").onclick = () => { window.location = `/api/tree/${d._id}/download?lang=${LANG}`; };
}

/* ---------- toolbar ---------- */
function setOrient(o) {
  _orient = o;
  $("#orientTB").setAttribute("aria-pressed", o === "TB");
  $("#orientLR").setAttribute("aria-pressed", o === "LR");
  if (_fam) draw(_fam);
}
$("#orientTB").addEventListener("click", () => setOrient("TB"));
$("#orientLR").addEventListener("click", () => setOrient("LR"));
$("#treeReset").addEventListener("click", () => { if (_fam) draw(_fam); });
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
