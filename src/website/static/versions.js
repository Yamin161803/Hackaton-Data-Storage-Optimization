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
  return (i === 0 ? v : v.toFixed(1)) + " " + u[i];
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
  catch (e) { list.innerHTML = `<p class="is-field__hint">Could not load families.</p>`; return; }
  if (!fams.length) { list.innerHTML = `<p class="is-placeholder">No documents stored yet. Run a dedupe analysis first.</p>`; return; }
  list.innerHTML = fams.map(f => `
    <div class="is-fam-row" role="row" data-root="${esc(f.root)}">
      <span class="is-chip">${esc(f.source_type.toUpperCase())}</span>
      <span class="is-fam-row__name">${esc(f.name)}</span>
      <span class="is-fam-row__meta">${f.size} version${f.size === 1 ? "" : "s"}</span>
      <span class="is-fam-row__saved">${f.saved_bytes ? "−" + human(f.saved_bytes) : ""}</span>
    </div>`).join("");
  list.onclick = (e) => { const row = e.target.closest("[data-root]"); if (row) selectFamily(row.dataset.root); };

  // pick from ?doc= deep link, else the first family
  const params = new URLSearchParams(location.search);
  const want = params.get("doc");
  selectFamily(want || fams[0].root);
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
  $("#graphName").textContent = fam.nodes[0] ? fam.nodes[0].name : "Version graph";
  ensureChart();
  draw(fam);
  const s = fam.savings || {};
  $("#treeSavings").innerHTML = `Saved <b>${human(s.saved_bytes)}</b> (${(s.saved_percent || 0).toFixed(1)} % of ${human(s.full_bytes)})`;
}

/* ---------- graph ---------- */
function toTree(fam) {
  const byId = {}; fam.nodes.forEach(n => byId[n.id] = { ...n, children: [] });
  const childSet = new Set();
  for (const e of fam.edges) if (byId[e.parent] && byId[e.child]) { byId[e.parent].children.push(byId[e.child]); childSet.add(e.child); }
  const roots = fam.nodes.filter(n => !childSet.has(n.id)).map(n => byId[n.id]);
  const mk = (n) => {
    const isNew = n.id === fam.selected, isClicked = n.id === _clickedId;
    return {
      name: shortName(n.name), _id: n.id, _full: n.name, _pages: n.pages, _date: n.date, _selected: isNew,
      itemStyle: {
        color: isNew ? cssVar("--green") : cssVar("--primary"),
        borderColor: isClicked ? cssVar("--teal") : (isNew ? cssVar("--green") : cssVar("--primary")),
        borderWidth: isClicked ? 5 : (isNew ? 4 : 2),
        shadowBlur: isClicked ? 22 : (isNew ? 14 : 0),
        shadowColor: isClicked ? cssVar("--teal") : cssVar("--green"),
      },
      symbolSize: isClicked ? 18 : (isNew ? 15 : 11),
      children: (n.children || []).map(mk),
    };
  };
  return roots.map(mk);
}

function draw(fam) {
  ensureChart();
  if (!_chart) return;
  _chart.setOption({
    tooltip: {
      trigger: "item", triggerOn: "mousemove",
      formatter: (p) => {
        const d = p.data, e = (fam.edges || []).find(x => x.child === d._id);
        const change = e ? `${e.relation}: +${e.added} / −${e.removed} pages` : (d._selected ? "added now" : "original version");
        return `<b>${esc(d._full)}</b><br>${d._pages} pages<br>${(d._date || "").slice(0, 10)}<br>${change}`;
      },
    },
    series: [{
      type: "tree", data: toTree(fam), orient: _orient, roam: true, initialTreeDepth: -1, symbol: "circle",
      label: { position: _orient === "TB" ? "bottom" : "right", color: cssVar("--text"), fontSize: 12, distance: 8 },
      lineStyle: { color: cssVar("--primary"), width: 2, opacity: .5, curveness: .25 },
      expandAndCollapse: true, animationDuration: 400, top: "6%", left: "10%", right: "10%", bottom: "6%",
    }],
  }, true);
  _chart.resize();
}

function openPopover(d) {
  const pop = $("#nodePopover");
  const e = (_fam.edges || []).find(x => x.child === d._id);
  const change = e ? `${e.relation}: +${e.added} / −${e.removed} pages` : (d._selected ? "added now" : "original version");
  pop.innerHTML = `
    <button class="is-pop-close" id="popClose" aria-label="Close">×</button>
    <div class="is-pop-name">${esc(d._full)}</div>
    <dl class="is-kv">
      <dt>Pages</dt><dd>${d._pages}</dd>
      <dt>Date</dt><dd>${(d._date || "").slice(0, 19).replace("T", " ")}</dd>
      <dt>Change</dt><dd>${change}</dd>
      ${d._selected ? "<dt>Status</dt><dd>Added now</dd>" : ""}
    </dl>
    <button class="is-btn is-btn--sm" id="popDl">Download this version</button>`;
  pop.hidden = false;
  $("#popClose").onclick = () => { pop.hidden = true; _clickedId = null; draw(_fam); };
  $("#popDl").onclick = () => { window.location = `/api/tree/${d._id}/download`; };
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

loadFamilies();
