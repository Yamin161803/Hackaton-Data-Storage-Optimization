"use strict";
// Version-tree view: stylized green/glowing data-viz tree of a document family.
// Sprout for one node; grows bigger/thicker with more + older versions.

const SVGNS = "http://www.w3.org/2000/svg";
const svg = document.getElementById("tree");
const tooltip = document.getElementById("tooltip");
const docSelect = document.getElementById("doc-select");
const dlBtn = document.getElementById("download");
let currentType = "pdf";
let selectedId = null;      // node to highlight (the just-added / selected doc)
let allDocs = [];

function human(n) {
  if (n == null) return "–";
  const u = ["B", "KB", "MB", "GB"];
  let i = 0, v = n;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return (i === 0 ? v : v.toFixed(1)) + " " + u[i];
}

function el(tag, attrs) {
  const n = document.createElementNS(SVGNS, tag);
  for (const k in attrs) n.setAttribute(k, attrs[k]);
  return n;
}

async function loadDocs() {
  const r = await fetch("/api/tree/documents");
  const d = await r.json();
  allDocs = d.documents || [];
  fillSelect();
}

function fillSelect() {
  const docs = allDocs.filter(x => x.type === currentType);
  docSelect.innerHTML = "";
  if (!docs.length) {
    const o = document.createElement("option");
    o.textContent = "(no " + currentType.toUpperCase() + " documents)";
    docSelect.appendChild(o);
    renderEmpty();
    return;
  }
  for (const doc of docs) {
    const o = document.createElement("option");
    o.value = doc.id;
    o.textContent = `${doc.name} · ${doc.pages}p · ${(doc.date || "").slice(0, 10)}`;
    docSelect.appendChild(o);
  }
  selectDoc(docs[0].id);
}

async function selectDoc(id) {
  selectedId = Number(id);
  const r = await fetch("/api/tree/" + id);
  if (!r.ok) { renderEmpty(); return; }
  const fam = await r.json();
  renderTree(fam);
  renderSavings(fam.savings);
  dlBtn.disabled = false;
  dlBtn.onclick = () => window.location = "/api/tree/" + selectedId + "/download";
}

function renderEmpty() {
  svg.innerHTML = "";
  const t = el("text", { x: 400, y: 260, fill: "rgba(215,255,233,.5)",
    "text-anchor": "middle", "font-size": 15 });
  t.textContent = "No document selected.";
  svg.appendChild(t);
}

// Layout: assign each node a depth (longest path from a root) using the edges,
// then place older (lower age_rank) nodes lower and thicker (trunk), newest on top.
function layout(fam) {
  const nodes = fam.nodes.slice().sort((a, b) => a.age_rank - b.age_rank);
  const n = nodes.length;
  const W = 800, H = 520, baseY = H - 70, topY = 90;
  const pos = {};
  nodes.forEach((node, i) => {
    // vertical: oldest at the bottom (trunk base), newest at the top
    const frac = n === 1 ? 0 : i / (n - 1);
    const y = baseY - frac * (baseY - topY);
    // horizontal: gentle zig-zag so branches fan out like a stylized tree
    const sway = n === 1 ? 0 : Math.sin(i * 1.1) * (60 + i * 14);
    const x = W / 2 + sway;
    // thickness: older (small age_rank) = thicker trunk; newest = thin twig
    const thickness = 10 + (n - 1 - i) * 3;
    // node radius grows a bit with family size (sprout -> tree)
    const radius = 9 + Math.min(n, 8) * 1.2 + (i === 0 ? 3 : 0);
    pos[node.id] = { x, y, thickness, radius, node, index: i };
  });
  return { nodes, pos, W, H, baseY };
}

function renderTree(fam) {
  svg.innerHTML = "";
  const { nodes, pos, W, baseY } = layout(fam);
  const n = nodes.length;

  // glow filter
  const defs = el("defs", {});
  defs.innerHTML =
    '<filter id="glow" x="-80%" y="-80%" width="260%" height="260%">' +
    '<feGaussianBlur stdDeviation="5" result="b"/>' +
    '<feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>';
  svg.appendChild(defs);

  // ground / sprout soil line
  svg.appendChild(el("line", { x1: W / 2 - 60, y1: baseY + 18, x2: W / 2 + 60,
    y2: baseY + 18, stroke: "rgba(56,255,166,.25)", "stroke-width": 2 }));

  // edges: draw as organic branches (older parent -> newer child)
  if (n === 1) {
    // single node = sprout: a short stem from the soil to the node
    const p = pos[nodes[0].id];
    svg.appendChild(el("path", {
      d: `M ${W / 2} ${baseY + 18} Q ${W / 2} ${(baseY + p.y) / 2} ${p.x} ${p.y}`,
      stroke: "var(--trunk)", "stroke-width": 8, fill: "none",
      "stroke-linecap": "round"
    }));
  } else {
    // trunk from soil to oldest node
    const root = pos[nodes[0].id];
    svg.appendChild(el("path", {
      d: `M ${W / 2} ${baseY + 18} L ${root.x} ${root.y}`,
      stroke: "var(--trunk)", "stroke-width": root.thickness + 2, fill: "none",
      "stroke-linecap": "round"
    }));
    for (const e of fam.edges) {
      const a = pos[e.parent], b = pos[e.child];
      if (!a || !b) continue;
      const midY = (a.y + b.y) / 2;
      svg.appendChild(el("path", {
        d: `M ${a.x} ${a.y} C ${a.x} ${midY}, ${b.x} ${midY}, ${b.x} ${b.y}`,
        stroke: "var(--edge)", "stroke-width": Math.max(3, a.thickness - 4),
        fill: "none", "stroke-linecap": "round"
      }));
    }
  }

  // nodes (glowing leaves/orbs)
  for (const node of nodes) {
    const p = pos[node.id];
    const isSel = node.id === fam.selected;
    const g = el("g", { class: "node", style: "cursor:pointer" });
    // halo
    g.appendChild(el("circle", { cx: p.x, cy: p.y, r: p.radius + (isSel ? 10 : 5),
      fill: isSel ? "rgba(56,255,166,.25)" : "rgba(56,255,166,.08)" }));
    // core
    g.appendChild(el("circle", {
      cx: p.x, cy: p.y, r: p.radius,
      fill: isSel ? "var(--glow)" : "var(--glow-soft)",
      stroke: "var(--glow)", "stroke-width": isSel ? 3 : 1,
      filter: isSel ? "url(#glow)" : ""
    }));
    // label
    const lbl = el("text", { x: p.x + p.radius + 8, y: p.y + 4,
      fill: "var(--text)", "font-size": 11 });
    lbl.textContent = node.name.length > 22 ? node.name.slice(0, 20) + "…" : node.name;
    g.appendChild(lbl);

    g.addEventListener("mousemove", (ev) => showTip(ev, node, fam));
    g.addEventListener("mouseleave", () => tooltip.style.opacity = 0);
    g.addEventListener("click", () => selectDoc(node.id));
    svg.appendChild(g);
  }
}

function edgeFor(fam, childId) {
  return fam.edges.find(e => e.child === childId);
}

function showTip(ev, node, fam) {
  const e = edgeFor(fam, node.id);
  const change = e ? `${e.relation}: +${e.added} / −${e.removed} pages vs parent`
                   : (node.is_root ? "root version (no parent)" : "no recorded parent");
  tooltip.innerHTML =
    `<div class="t-name">${node.name}</div>` +
    `<div class="t-row">${node.pages} pages · ${(node.date || "").slice(0, 19).replace("T", " ")}</div>` +
    `<div class="t-row">${change}</div>` +
    `<div class="t-row">${node.id === fam.selected ? "◉ just added / selected" : "click to select"}</div>`;
  const wrap = svg.getBoundingClientRect();
  tooltip.style.left = (ev.clientX - wrap.left + 14) + "px";
  tooltip.style.top = (ev.clientY - wrap.top + 14) + "px";
  tooltip.style.opacity = 1;
  // update side panel
  document.getElementById("node-info").innerHTML =
    `<b style="color:var(--glow)">${node.name}</b><br>${node.pages} pages<br>` +
    `${(node.date || "").slice(0, 19).replace("T", " ")}<br>${change}`;
}

function renderSavings(s) {
  if (!s) return;
  document.getElementById("saved-pct").textContent = (s.saved_percent || 0) + "%";
  document.getElementById("saved-bytes").textContent = human(s.saved_bytes);
  document.getElementById("full-bytes").textContent = human(s.full_bytes);
  document.getElementById("stored-bytes").textContent = human(s.stored_bytes);
  const d = s.detail || { pages: {}, images: {} };
  document.getElementById("pages-detail").textContent =
    human(d.pages.full) + " → " + human(d.pages.stored);
  document.getElementById("images-detail").textContent =
    human(d.images.full) + " → " + human(d.images.stored);
}

// tabs (PDF / PPTX separation)
document.querySelectorAll(".tab").forEach(tab => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach(t => t.classList.remove("active"));
    tab.classList.add("active");
    currentType = tab.dataset.type;
    fillSelect();
  });
});
docSelect.addEventListener("change", () => selectDoc(docSelect.value));

loadDocs();
