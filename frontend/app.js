// KSP CIP dashboard — SPA controller.
//
// Design notes:
//   * Every tab loads on activation, not on bootstrap — first paint is instant.
//   * Filter strip is contextual (visible only on tabs where filters apply).
//   * All API calls go through api() which surfaces toasts on error and
//     preserves per-request AbortController so tab-switching cancels stale
//     requests instead of racing.
//   * Tables render via renderTable(bodySel, rows, fn) — one function, one
//     truthy consistent look.

const $  = (s, root = document) => root.querySelector(s);
const $$ = (s, root = document) => Array.from(root.querySelectorAll(s));

const CLASS_COLORS = {
  "Property Crimes":     "var(--c-property)",
  "Crimes Against Body": "var(--c-body)",
  "Crimes Against Women":"var(--c-women)",
  "Economic Offences":   "var(--c-economic)",
  "Cyber Crimes":        "var(--c-cyber)",
  "Narcotic Offences":   "var(--c-narcotic)",
  "Traffic Offences":    "var(--c-traffic)",
  "Other IPC":           "var(--c-other)",
};
// Resolve CSS var to hex once — Chart.js can't parse `var(--x)`.
function cssColor(key) {
  const v = CLASS_COLORS[key] || "var(--c-other)";
  if (!v.startsWith("var(")) return v;
  const name = v.slice(4, -1).trim();
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || "#94a3b8";
}
function colorFor(className) { return cssColor(className); }

const state = {
  meta: null,
  filters: {},
  charts: {},
  maps: {},         // { geo, predict, cb }
  mapLayers: {},    // { geo: {heat, points, districts}, ... }
  currentGeoLayer: "heatmap",
  currentNetTab: "offenders",
  offenderNet: null,
  loadedTabs: new Set(),
  tabsWithFilters: new Set(["overview", "geo"]),
  aborts: {},       // per-tab AbortController
  currentTab: "overview",
};

// -------------------- fetch layer with abort + toast ----------------------
async function api(path, opts = {}) {
  const ctrl = new AbortController();
  const key = opts._key || "global";
  if (state.aborts[key]) state.aborts[key].abort();
  state.aborts[key] = ctrl;
  try {
    const r = await fetch(path, { ...opts, signal: ctrl.signal });
    if (!r.ok) throw new Error(`HTTP ${r.status} ${path}`);
    return await r.json();
  } catch (e) {
    if (e.name !== "AbortError") toast(String(e.message || e), "err");
    throw e;
  }
}
function toast(msg, kind = "info") {
  const host = $("#toast-host"); if (!host) return;
  const el = document.createElement("div");
  el.className = `toast ${kind}`; el.textContent = msg;
  host.appendChild(el);
  setTimeout(() => { el.style.opacity = "0"; setTimeout(() => el.remove(), 200); }, 4000);
}
function qs(params) {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== "" && v !== null && v !== undefined) p.append(k, v);
  }
  const s = p.toString();
  return s ? `?${s}` : "";
}
function currentFilterQS(extra = {}) { return qs({ ...state.filters, ...extra }); }

// -------------------- bootstrap -------------------------------------------
async function bootstrap() {
  wireStaticUI();
  try {
    const meta = await api("/meta", { _key: "meta" });
    state.meta = meta;
    populateFilters(meta);
    $("#conn-dot").className = "dot ok";
    $("#conn-text").textContent = "Connected";
    $("#data-window").textContent =
      `data: ${meta.date_range.min.slice(0,10)} → ${meta.date_range.max.slice(0,10)}`;
  } catch (e) {
    $("#conn-dot").className = "dot err";
    $("#conn-text").textContent = "Disconnected";
    return;
  }
  await switchTab("overview");
  api("/assistant/health").then((h) => {
    const backend = h.backend || "offline";
    const el = $("#assistant-backend");
    if (el) el.textContent = `backend: ${backend}`;
  }).catch(() => {});
}
function wireStaticUI() {
  // Sidebar nav
  $$(".nav-item").forEach(item => {
    item.addEventListener("click", () => switchTab(item.dataset.tab));
  });
  $("#menu-toggle")?.addEventListener("click", () => $("#sidebar").classList.toggle("open"));

  // Filter strip
  $("#f-apply").addEventListener("click", () => { readFilters(); refreshCurrentTab(true); });
  $("#f-reset").addEventListener("click", () => {
    if (!state.meta) return;
    $("#f-from").value = state.meta.date_range.min.slice(0,10);
    $("#f-to").value   = state.meta.date_range.max.slice(0,10);
    $("#f-district").value = ""; $("#f-class").value = ""; $("#f-subhead").value = "";
    readFilters(); refreshCurrentTab(true);
  });

  // Geospatial layer chips
  $$('input[name="layer"]').forEach(input => {
    input.addEventListener("change", () => {
      state.currentGeoLayer = input.value;
      $$(".chip[data-layer]").forEach(c => c.classList.toggle("active", c.dataset.layer === input.value));
      drawGeoLayer();
    });
  });

  // Predict controls
  $("#predict-refresh").addEventListener("click", loadPredictMap);
  $("#predict-horizon").addEventListener("change", loadPredictMap);
  $("#predict-head").addEventListener("change", loadPredictMap);
  $("#fcst-run").addEventListener("click", loadForecastChart);

  // Network mini-tabs
  $$(".mini-tab").forEach(t => {
    t.addEventListener("click", () => {
      $$(".mini-tab").forEach(x => x.classList.remove("active"));
      t.classList.add("active");
      state.currentNetTab = t.dataset.nettab;
      loadNetworkList();
    });
  });

  // Assistant
  $("#assistant-ask").addEventListener("click", assistantAsk);
  $("#assistant-q").addEventListener("keydown", (e) => { if (e.key === "Enter") assistantAsk(); });
  $$(".assistant-suggestions .chip").forEach(c => c.addEventListener("click", () => {
    $("#assistant-q").value = c.textContent.trim(); assistantAsk();
  }));
}
function populateFilters(meta) {
  const dSel = $("#f-district");
  meta.districts.forEach(d => { const o = document.createElement("option"); o.value = d.id; o.textContent = d.name; dSel.appendChild(o); });
  const cls = new Set(meta.crime_subheads.map(s => s.head_name));
  const clsSel = $("#f-class");
  Array.from(cls).sort().forEach(k => { const o = document.createElement("option"); o.value = k; o.textContent = k; clsSel.appendChild(o); });
  const sub = $("#f-subhead");
  meta.crime_subheads.forEach(s => { const o = document.createElement("option"); o.value = s.id; o.textContent = `${s.name} — ${s.head_name}`; sub.appendChild(o); });
  $("#f-from").value = meta.date_range.min.slice(0, 10);
  $("#f-to").value   = meta.date_range.max.slice(0, 10);
  readFilters();

  // Predict + forecast selects
  const predHead = $("#predict-head");
  const fcstHead = $("#fcst-head");
  meta.crime_heads.forEach(h => {
    predHead.appendChild(new Option(h.name, h.id));
    fcstHead.appendChild(new Option(h.name, h.id));
  });
  const fcstDist = $("#fcst-district");
  meta.districts.forEach(d => fcstDist.appendChild(new Option(d.name, d.id)));
}
function readFilters() {
  state.filters = {
    from:     $("#f-from").value || undefined,
    to:       $("#f-to").value || undefined,
    district: $("#f-district").value || undefined,
    class:    $("#f-class").value || undefined,
    subhead:  $("#f-subhead").value || undefined,
  };
}

// -------------------- tabs -----------------------------------------------
async function switchTab(id) {
  state.currentTab = id;
  $$(".nav-item").forEach(n => n.classList.toggle("active", n.dataset.tab === id));
  $$(".tab-panel").forEach(p => p.classList.toggle("active", p.dataset.panel === id));
  $("#tab-title").textContent = ({
    overview: "Overview",
    geo: "Geospatial",
    predict: "Predictive analytics",
    network: "Criminal network",
    crossborder: "Cross-border crime",
    laworder: "Law & Order",
    anomaly: "Anomalies & emerging trends",
    assistant: "AI assistant",
  })[id] || id;
  const filterStrip = $("#filter-strip");
  filterStrip.setAttribute("data-visible", state.tabsWithFilters.has(id) ? "1" : "0");
  await refreshCurrentTab(false);
}
async function refreshCurrentTab(force) {
  const id = state.currentTab;
  if (!force && state.loadedTabs.has(id) && id !== "overview" && id !== "geo") return;
  state.loadedTabs.add(id);
  if (id === "overview")    await loadOverview();
  if (id === "geo")         await loadGeoTab();
  if (id === "predict")     await loadPredictTab();
  if (id === "network")     await loadNetworkList();
  if (id === "crossborder") await loadCrossborder();
  if (id === "laworder")    await loadLawOrder();
  if (id === "anomaly")     await loadAnomaliesAndTrends();
}

// -------------------- OVERVIEW -------------------------------------------
async function loadOverview() {
  showKpiSkeleton();
  let s;
  try { s = await api(`/stats${currentFilterQS()}`, { _key: "stats" }); } catch { return; }
  renderKpis(s);
  makeChart("month", "chart-month", monthChartCfg(s.by_month));
  makeChart("class", "chart-class", classDonutCfg(s.by_class));
  makeChart("hour", "chart-hour", hourChartCfg(s.by_hour));
  makeChart("category", "chart-category", categoryChartCfg(s.by_category));
  makeChart("gravity", "chart-gravity", gravityChartCfg(s.by_gravity));
  makeChart("status", "chart-status", statusChartCfg(s.by_status));
}
function showKpiSkeleton() {
  if ($("#kpis").children.length) return;
  $("#kpis").innerHTML = Array.from({length: 4}, () => `
    <div class="kpi"><div class="skel" style="height:12px;width:70%"></div>
    <div class="skel" style="height:24px;width:50%;margin-top:8px"></div></div>`).join("");
}
function renderKpis(s) {
  const total = s.total || 0;
  const violent = s.by_class.find(x => x.class === "Crimes Against Body")?.count ?? 0;
  const cyber   = s.by_class.find(x => x.class === "Cyber Crimes")?.count ?? 0;
  const heinous = s.by_gravity.find(g => g.gravity === "Heinous")?.count ?? 0;
  const csed    = s.by_status.find(x => x.status === "ChargeSheeted")?.count ?? 0;
  const kpis = [
    { c: "",         label: "Total FIRs", value: fmt(total), sub: "matching filters" },
    { c: "kpi-danger", label: "Heinous",  value: fmt(heinous), sub: total ? `${pct(heinous, total)} of caseload` : "" },
    { c: "kpi-ok",     label: "Chargesheeted", value: fmt(csed), sub: total ? `${pct(csed, total)} solved` : "" },
    { c: "kpi-warn",   label: "Violent · Cyber", value: `${fmt(violent)} · ${fmt(cyber)}`, sub: "high-priority" },
  ];
  $("#kpis").innerHTML = kpis.map(k =>
    `<div class="kpi ${k.c}"><div class="label">${k.label}</div><div class="value">${k.value}</div><div class="sub">${k.sub}</div></div>`
  ).join("");
}
function chartOpts(extra = {}) {
  return {
    responsive: true, maintainAspectRatio: false,
    interaction: { mode: "index", intersect: false },
    plugins: {
      legend: { labels: { color: "#e6edf6", font: { size: 11 }, boxWidth: 12 } },
      tooltip: { backgroundColor: "#141c26", titleColor: "#e6edf6", bodyColor: "#e6edf6", borderColor: "#253044", borderWidth: 1 },
    },
    scales: {
      x: { ticks: { color: "#94a3b8", font: { size: 10 } }, grid: { color: "rgba(37,48,68,.5)" }, border: { color: "#253044" } },
      y: { ticks: { color: "#94a3b8", font: { size: 10 } }, grid: { color: "rgba(37,48,68,.5)" }, border: { color: "#253044" }, beginAtZero: true },
    },
    ...extra,
  };
}
function makeChart(key, canvasId, cfg) {
  if (state.charts[key]) state.charts[key].destroy();
  const canvas = $(`#${canvasId}`); if (!canvas) return;
  state.charts[key] = new Chart(canvas, cfg);
}
function monthChartCfg(rows) {
  return {
    type: "line",
    data: {
      labels: rows.map(r => r.month),
      datasets: [{
        label: "FIRs", data: rows.map(r => r.count),
        borderColor: "#5aa2ff", backgroundColor: "rgba(90,162,255,.15)",
        borderWidth: 2, pointRadius: 2, pointHoverRadius: 4, fill: true, tension: 0.32,
      }],
    },
    options: chartOpts({ plugins: { legend: { display: false } } }),
  };
}
function classDonutCfg(rows) {
  return {
    type: "doughnut",
    data: {
      labels: rows.map(r => r.class),
      datasets: [{ data: rows.map(r => r.count), backgroundColor: rows.map(r => colorFor(r.class)),
        borderColor: "#141c26", borderWidth: 2 }],
    },
    options: { responsive: true, maintainAspectRatio: false, cutout: "62%",
      plugins: { legend: { position: "right", labels: { color: "#e6edf6", font: { size: 10 }, boxWidth: 12, padding: 8 } } } },
  };
}
function hourChartCfg(rows) {
  const data = new Array(24).fill(0); rows.forEach(r => { data[r.hour] = r.count; });
  return {
    type: "bar",
    data: { labels: data.map((_, i) => `${String(i).padStart(2, "0")}h`),
      datasets: [{ data, backgroundColor: "#5aa2ff", borderRadius: 3, maxBarThickness: 20 }] },
    options: chartOpts({ plugins: { legend: { display: false } } }),
  };
}
function categoryChartCfg(rows) {
  const top = rows.slice(0, 10);
  return {
    type: "bar",
    data: { labels: top.map(r => r.label),
      datasets: [{ data: top.map(r => r.count), backgroundColor: top.map(r => colorFor(r.class)), borderRadius: 3 }] },
    options: chartOpts({ indexAxis: "y", plugins: { legend: { display: false } } }),
  };
}
function gravityChartCfg(rows) {
  const colors = { "Heinous": "#ef4444", "Non-Heinous": "#f59e0b", "Petty": "#22c55e" };
  return {
    type: "bar",
    data: { labels: rows.map(r => r.gravity),
      datasets: [{ data: rows.map(r => r.count), backgroundColor: rows.map(r => colors[r.gravity] || "#94a3b8"), borderRadius: 3, maxBarThickness: 40 }] },
    options: chartOpts({ plugins: { legend: { display: false } } }),
  };
}
function statusChartCfg(rows) {
  const colors = { "UnderInvestigation": "#f59e0b", "ChargeSheeted": "#5aa2ff", "Closed": "#22c55e", "Pending Trial": "#a78bfa", "PendingBeforeCourt": "#22d3ee" };
  return {
    type: "bar",
    data: { labels: rows.map(r => r.status),
      datasets: [{ data: rows.map(r => r.count), backgroundColor: rows.map(r => colors[r.status] || "#94a3b8"), borderRadius: 3, maxBarThickness: 40 }] },
    options: chartOpts({ plugins: { legend: { display: false } } }),
  };
}

// -------------------- GEO -------------------------------------------------
function ensureMap(key, containerId, view = [14.5, 76.0, 7]) {
  if (state.maps[key]) { state.maps[key].invalidateSize(); return state.maps[key]; }
  const m = L.map(containerId, { zoomControl: true, preferCanvas: true }).setView([view[0], view[1]], view[2]);
  L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_nolabels/{z}/{x}/{y}{r}.png", {
    attribution: "&copy; OSM &copy; CARTO", subdomains: "abcd", maxZoom: 19,
  }).addTo(m);
  state.maps[key] = m; state.mapLayers[key] = {};
  setTimeout(() => m.invalidateSize(), 30);
  return m;
}
function clearMapLayers(key) {
  const m = state.maps[key]; if (!m) return;
  Object.values(state.mapLayers[key] || {}).forEach(l => m.removeLayer(l));
  state.mapLayers[key] = {};
}
async function loadGeoTab() {
  ensureMap("geo", "map");
  await drawGeoLayer();
  await drawDistrictList();
}
async function drawGeoLayer() {
  if (!state.maps.geo) return;
  clearMapLayers("geo");
  if (state.currentGeoLayer === "heatmap")   await drawHeatmap();
  if (state.currentGeoLayer === "points")    await drawPoints();
  if (state.currentGeoLayer === "districts") await drawDistrictBubbles();
}
async function drawHeatmap() {
  const h = await api(`/geo/heatmap${currentFilterQS({ cell_km: 2 })}`, { _key: "geo-heatmap" });
  if (!h.cells.length) return;
  const pts = h.cells.map(c => [c.lat, c.lng, c.intensity]);
  state.mapLayers.geo.heat = L.heatLayer(pts, {
    radius: 22, blur: 20, maxZoom: 10, minOpacity: 0.30,
    gradient: { 0.2: "#5aa2ff", 0.5: "#a78bfa", 0.8: "#f59e0b", 1.0: "#ef4444" },
  }).addTo(state.maps.geo);
}
async function drawPoints() {
  const p = await api(`/geo/points${currentFilterQS({ limit: 2500 })}`, { _key: "geo-points" });
  const layer = L.layerGroup();
  p.points.forEach(pt => {
    const color = colorFor(pt.class);
    layer.addLayer(L.circleMarker([pt.lat, pt.lng], {
      radius: 3.5, color, weight: 1, fillOpacity: 0.7, fillColor: color,
    }).bindPopup(
      `<div style="font-weight:600">${pt.fir_number}</div>` +
      `<div>${pt.category} · <span style="color:${color}">${pt.class}</span></div>` +
      `<div style="color:#94a3b8">${pt.occurred_at.replace("T", " ").slice(0,16)}</div>` +
      `<div>MO: ${pt.mo || "—"} · Status: ${pt.status || "—"} · Gravity: ${pt.gravity || "—"}</div>`
    ));
  });
  state.mapLayers.geo.points = layer.addTo(state.maps.geo);
}
async function drawDistrictBubbles() {
  const d = await api(`/geo/district-summary${currentFilterQS()}`, { _key: "geo-districts-map" });
  const layer = L.layerGroup();
  const maxRate = Math.max(1, ...d.districts.map(r => r.per_lakh || 0));
  d.districts.forEach(r => {
    if (!r.hq_lat) return;
    const rate = r.per_lakh || 0;
    const t = rate / maxRate;
    const radius = 8 + t * 26;
    const color = t > 0.7 ? "#ef4444" : t > 0.4 ? "#f59e0b" : "#5aa2ff";
    layer.addLayer(L.circleMarker([r.hq_lat, r.hq_lng], {
      radius, color, weight: 1, fillOpacity: 0.55, fillColor: color,
    }).bindPopup(
      `<div style="font-weight:600">${r.name}</div>` +
      `<div style="color:#94a3b8">${r.zone || ""} zone</div>` +
      `<div>${r.count.toLocaleString()} FIRs · ${rate.toFixed(1)}/lakh</div>`
    ));
  });
  state.mapLayers.geo.districts = layer.addTo(state.maps.geo);
}
async function drawDistrictList() {
  const d = await api(`/geo/district-summary${currentFilterQS()}`, { _key: "geo-district-list" });
  const list = $("#district-list");
  if (!d.districts.length) { list.innerHTML = `<div class="empty-state">No districts match.</div>`; return; }
  const max = Math.max(1, ...d.districts.map(r => r.count));
  list.innerHTML = d.districts.map(r => `
    <div class="district-row" data-district="${r.id}">
      <div class="name">${r.name}</div>
      <div class="count">${fmt(r.count)}</div>
      <div class="rate">${(r.per_lakh || 0).toFixed(1)}/lakh</div>
      <div class="bar"><div class="bar-fill" style="width:${r.count / max * 100}%"></div></div>
    </div>`).join("");
  $$("#district-list .district-row").forEach(row => {
    row.addEventListener("click", () => {
      $("#f-district").value = row.dataset.district;
      readFilters(); refreshCurrentTab(true);
    });
  });
}

// -------------------- PREDICT --------------------------------------------
async function loadPredictTab() {
  ensureMap("predict", "predict-map");
  await loadPredictMap();
}
async function loadPredictMap() {
  if (!state.maps.predict) return;
  clearMapLayers("predict");
  const head = $("#predict-head").value;
  const horizon = $("#predict-horizon").value;
  let r;
  try { r = await api(`/predict/density-map${qs({ horizon, ...(head ? { head } : {}) })}`, { _key: "predict-map" }); }
  catch { return; }
  if (!r.cells.length) return;
  const pts = r.cells.map(c => [c.lat, c.lng, c.intensity]);
  state.mapLayers.predict.heat = L.heatLayer(pts, {
    radius: 24, blur: 22, maxZoom: 10, minOpacity: 0.30,
    gradient: { 0.2: "#5aa2ff", 0.5: "#a78bfa", 0.8: "#f59e0b", 1.0: "#ef4444" },
  }).addTo(state.maps.predict);

  const top = [...r.cells].sort((a, b) => b.predicted_next - a.predicted_next).slice(0, 12);
  const markers = L.layerGroup();
  top.forEach(c => {
    markers.addLayer(L.circleMarker([c.lat, c.lng], {
      radius: 7, color: "#ef4444", weight: 2, fillOpacity: 0.45, fillColor: "#ef4444",
    }).bindPopup(
      `<div style="font-weight:600">Unit ${c.unit_id}</div>` +
      `<div>Predicted next ${r.horizon_weeks}w: <b>${c.predicted_next}</b></div>` +
      `<div>Recent ${r.horizon_weeks}w: ${c.recent_actual}</div>` +
      `<div style="color:#94a3b8">Δ ${c.delta_pct == null ? "—" : c.delta_pct + "%"}</div>`
    ));
  });
  state.mapLayers.predict.markers = markers.addTo(state.maps.predict);
}
async function loadForecastChart() {
  const did = $("#fcst-district").value, hid = $("#fcst-head").value;
  if (!did || !hid) { toast("Pick district and head first", "warn"); return; }
  let f;
  try { f = await api(`/predict/district/${did}/head/${hid}?horizon=8`, { _key: "forecast" }); } catch { return; }
  const hL = f.history_weeks, hV = f.history_values, fL = f.weeks;
  makeChart("fcst", "fcst-chart", {
    type: "line",
    data: {
      labels: [...hL, ...fL],
      datasets: [
        { label: "History", data: [...hV, ...new Array(fL.length).fill(null)],
          borderColor: "#5aa2ff", backgroundColor: "rgba(90,162,255,.12)", tension: 0.25, pointRadius: 0, borderWidth: 2, fill: true },
        { label: "Forecast", data: [...new Array(hL.length).fill(null), ...f.point],
          borderColor: "#f59e0b", borderDash: [5,5], tension: 0.25, pointRadius: 0, borderWidth: 2 },
        { label: "Upper 90%", data: [...new Array(hL.length).fill(null), ...f.upper],
          borderColor: "rgba(245,158,11,.4)", pointRadius: 0, borderWidth: 1 },
        { label: "Lower 90%", data: [...new Array(hL.length).fill(null), ...f.lower],
          borderColor: "rgba(245,158,11,.4)", pointRadius: 0, borderWidth: 1 },
      ]
    },
    options: chartOpts({ scales: { x: { ticks: { color: "#94a3b8", maxTicksLimit: 12, font:{size:10} }, grid: { color: "rgba(37,48,68,.5)" } }, y: { ticks: { color: "#94a3b8", font:{size:10} }, grid: { color: "rgba(37,48,68,.5)" }, beginAtZero: true } } }),
  });
  $("#fcst-meta").textContent = `method: ${f.method} · history ${hL.length} weeks · forecast ${fL.length} weeks`;
}

// -------------------- NETWORK --------------------------------------------
async function loadNetworkList() {
  const tab = state.currentNetTab;
  const list = $("#offender-list");
  list.innerHTML = `<div class="list-hint">Loading…</div>`;
  try {
    if (tab === "offenders")   await renderOffenders();
    if (tab === "communities") await renderCommunities();
    if (tab === "central")     await renderCentralFigures();
    if (tab === "mo")          await renderMoSimilar();
  } catch { /* toast surfaced */ }
}
async function renderOffenders() {
  const o = await api("/offenders/top?limit=40", { _key: "offenders" });
  const list = $("#offender-list");
  if (!o.offenders.length) { list.innerHTML = `<div class="empty-state">No repeat offenders yet.</div>`; return; }
  list.innerHTML =
    `<div class="list-hint">${o.offenders.length} repeat offenders — click to view network</div>` +
    o.offenders.map(p => `
      <div class="offender-row" data-pid="${p.id}">
        <div class="name">${escapeHtml(p.full_name)}</div>
        <div class="stats">${p.incidents} FIRs · ${p.distinct_categories} sub-heads · ${escapeHtml(p.district || "—")}</div>
      </div>`).join("");
  bindNetworkRows("[data-pid]", (row) => showOffenderNetwork(row.dataset.pid));
  autoActivateFirstRow(o.offenders.length && o.offenders[0].id);
}
async function renderCommunities() {
  const r = await api("/network/communities?top_n=200", { _key: "communities" });
  const list = $("#offender-list");
  if (!r.communities.length) { list.innerHTML = `<div class="empty-state">No communities detected.</div>`; return; }
  list.innerHTML =
    `<div class="list-hint">Louvain communities · ${r.summary.nodes} nodes / ${r.summary.edges} edges · ${r.communities.length} clusters</div>` +
    r.communities.map(co => `
      <div class="community-row" data-comm="${co.community_id}">
        <div class="head"><span>Community #${co.community_id}</span><span>${co.total_cases} cases</span></div>
        <div class="members">${co.size} members</div>
        <div class="sigs">${co.signature_crimes.map(escapeHtml).join(" · ")}</div>
      </div>`).join("");
  bindNetworkRows(".community-row", (row) => showCommunityGraph(r, +row.dataset.comm));
  const first = $$(".community-row")[0];
  if (first) { first.classList.add("active"); showCommunityGraph(r, r.communities[0].community_id); }
}
async function renderCentralFigures() {
  const r = await api("/network/central-figures?limit=30", { _key: "central" });
  const list = $("#offender-list");
  if (!r.figures.length) { list.innerHTML = `<div class="empty-state">No central figures yet.</div>`; return; }
  list.innerHTML =
    `<div class="list-hint">Ranked by centrality × case count</div>` +
    r.figures.map(f => `
      <div class="offender-row" data-pid="${f.person_link_id}">
        <div class="name">${escapeHtml(f.name)} <span class="hint">· score ${f.score}</span></div>
        <div class="stats">${f.cases} cases · weighted degree ${f.weighted_degree}</div>
        <div class="sigs">${f.signature.map(escapeHtml).join(" · ")}</div>
      </div>`).join("");
  bindNetworkRows("[data-pid]", (row) => showOffenderNetwork(row.dataset.pid));
  autoActivateFirstRow(r.figures.length && r.figures[0].person_link_id);
}
async function renderMoSimilar() {
  const r = await api("/network/mo-similarity?top_n=80", { _key: "mo" });
  const list = $("#offender-list");
  if (!r.pairs.length) { list.innerHTML = `<div class="empty-state">No MO similarities.</div>`; return; }
  list.innerHTML =
    `<div class="list-hint">MO-cosine pairs — case-linkage candidates</div>` +
    r.pairs.slice(0, 60).map(p => `
      <div class="offender-row" data-pid="${p.a_id}">
        <div class="name">${escapeHtml(p.a_name)} <span class="hint">↔</span> ${escapeHtml(p.b_name)}</div>
        <div class="stats">similarity ${p.similarity} · ${p.a_cases} & ${p.b_cases} cases</div>
      </div>`).join("");
  bindNetworkRows("[data-pid]", (row) => showOffenderNetwork(row.dataset.pid));
}
function bindNetworkRows(sel, handler) {
  $$(`#offender-list ${sel}`).forEach(row => {
    row.addEventListener("click", () => {
      $$(`#offender-list .offender-row, #offender-list .community-row`).forEach(r => r.classList.remove("active"));
      row.classList.add("active");
      handler(row);
    });
  });
}
function autoActivateFirstRow(pid) {
  const first = $("#offender-list .offender-row"); if (!first) return;
  first.classList.add("active");
  if (pid) showOffenderNetwork(pid);
}

async function showOffenderNetwork(pid) {
  let g;
  try { g = await api(`/network/offender/${pid}`, { _key: "graph" }); } catch { return; }
  const p = g.person;
  $("#graph-title").textContent = `${p.full_name} · id ${p.id}`;
  $("#offender-summary").innerHTML = `
    <span><strong>${g.summary.total_firs}</strong> FIRs</span>
    <span><strong>${g.summary.distinct_co_offenders}</strong> co-offenders</span>
    <span><strong>${g.summary.distinct_victims}</strong> victims</span>`;
  const groups = {
    offender_primary: { color: { background: "#5aa2ff", border: "#5aa2ff" }, font: { color: "#fff" }, size: 32 },
    offender:         { color: { background: "#ef4444", border: "#ef4444" }, font: { color: "#fff" } },
    victim:           { color: { background: "#22c55e", border: "#22c55e" }, font: { color: "#fff" } },
    property_crimes:  { color: { background: "#5aa2ff", border: "#5aa2ff" }, font: { color: "#fff" } },
    crimes_against_body:  { color: { background: "#ef4444", border: "#ef4444" }, font: { color: "#fff" } },
    crimes_against_women: { color: { background: "#f59e0b", border: "#f59e0b" }, font: { color: "#fff" } },
    economic_offences:{ color: { background: "#22c55e", border: "#22c55e" }, font: { color: "#fff" } },
    cyber_crimes:     { color: { background: "#a78bfa", border: "#a78bfa" }, font: { color: "#fff" } },
    narcotic_offences:{ color: { background: "#eab308", border: "#eab308" }, font: { color: "#fff" } },
    traffic_offences: { color: { background: "#22d3ee", border: "#22d3ee" }, font: { color: "#fff" } },
    other_ipc:        { color: { background: "#94a3b8", border: "#94a3b8" }, font: { color: "#fff" } },
  };
  renderVis($("#graph"), g.graph.nodes, g.graph.edges, groups);
}
function showCommunityGraph(payload, commId) {
  const co = payload.communities.find(c => c.community_id === commId);
  if (!co) return;
  $("#graph-title").textContent = `Community #${co.community_id} · ${co.size} members`;
  $("#offender-summary").innerHTML = `
    <span><strong>${co.total_cases}</strong> cases</span>
    <span>signature: <strong>${co.signature_crimes.slice(0,3).map(escapeHtml).join(", ")}</strong></span>`;
  const nodes = co.members.map(m => ({
    id: m.person_link_id, label: m.name,
    title: `${m.name} · ${m.cases} cases · wdeg ${m.weighted_degree}`,
    value: Math.log(m.cases + 1), group: "offender",
  }));
  const center = co.members[0].person_link_id;
  const edges = co.members.slice(1).map(m => ({ from: center, to: m.person_link_id, dashes: true }));
  renderVis($("#graph"), nodes, edges, {
    offender: { color: { background: "#ef4444", border: "#ef4444" }, font: { color: "#fff" } },
  });
}
function renderVis(container, nodes, edges, groups) {
  container.innerHTML = "";
  if (state.offenderNet) { try { state.offenderNet.destroy(); } catch {} state.offenderNet = null; }
  state.offenderNet = new vis.Network(container, {
    nodes: new vis.DataSet(nodes), edges: new vis.DataSet(edges),
  }, {
    layout: { improvedLayout: nodes.length < 200 },
    physics: { stabilization: { iterations: 100 }, barnesHut: { gravitationalConstant: -6500, springLength: 120 } },
    interaction: { hover: true, tooltipDelay: 100 },
    edges: { color: { color: "#334155", highlight: "#5aa2ff" }, smooth: { type: "continuous" }, width: 1 },
    nodes: { font: { color: "#e6edf6", size: 11 }, borderWidth: 1, shape: "dot", scaling: { min: 6, max: 26 } },
    groups,
  });
}

// -------------------- CROSS-BORDER ---------------------------------------
async function loadCrossborder() {
  ensureMap("cb", "cb-map", [16.0, 78.0, 6]);
  clearMapLayers("cb");
  let s, mv, offs;
  try {
    [s, mv, offs] = await Promise.all([
      api("/cross-border/summary", { _key: "cb-summary" }),
      api("/cross-border/movements?limit=100", { _key: "cb-movements" }),
      api("/cross-border/offenders?limit=20", { _key: "cb-offenders" }),
    ]);
  } catch { return; }

  // Arrest locations
  const max = Math.max(1, ...s.by_district.map(d => d.arrests));
  const locLayer = L.layerGroup();
  s.by_district.forEach(d => {
    if (!d.lat) return;
    const r = 6 + 22 * (d.arrests / max);
    locLayer.addLayer(L.circleMarker([d.lat, d.lng], {
      radius: r, color: "#ef4444", weight: 1, fillOpacity: 0.5, fillColor: "#ef4444",
    }).bindPopup(`<b>${escapeHtml(d.district || "—")}, ${escapeHtml(d.state)}</b><br>${d.arrests} arrests`));
  });
  state.mapLayers.cb.locs = locLayer.addTo(state.maps.cb);

  // Home → arrest arcs
  const mvMax = Math.max(1, ...mv.movements.map(m => m.movements));
  const lineLayer = L.layerGroup();
  mv.movements.forEach(m => {
    if (!m.home_lat || !m.arrest_lat) return;
    const w = 1 + 3.5 * (m.movements / mvMax);
    lineLayer.addLayer(L.polyline([[m.home_lat, m.home_lng], [m.arrest_lat, m.arrest_lng]], {
      color: "#a78bfa", weight: w, opacity: 0.5,
    }).bindPopup(`<b>${escapeHtml(m.home_district || "—")} → ${escapeHtml(m.arrest_district || "—")}</b><br>${escapeHtml(m.arrest_state || "—")}<br>${m.movements} movements`));
  });
  state.mapLayers.cb.lines = lineLayer.addTo(state.maps.cb);

  // Charts
  makeChart("cb_state", "cb-state", {
    type: "bar",
    data: { labels: s.by_state.map(x => x.state),
      datasets: [{ data: s.by_state.map(x => x.arrests), backgroundColor: "#a78bfa", borderRadius: 3 }] },
    options: chartOpts({ plugins: { legend: { display: false } }, indexAxis: "y" }),
  });
  makeChart("cb_class", "cb-class", {
    type: "bar",
    data: { labels: s.by_class.map(x => x.class),
      datasets: [{ data: s.by_class.map(x => x.arrests), backgroundColor: s.by_class.map(x => colorFor(x.class)), borderRadius: 3 }] },
    options: chartOpts({ plugins: { legend: { display: false } }, indexAxis: "y" }),
  });

  // Table
  const tbody = $("#cb-offenders tbody");
  tbody.innerHTML = offs.offenders.map(o => `
    <tr>
      <td class="strong">${escapeHtml(o.name)}</td>
      <td class="num">${o.total_arrests}</td>
      <td class="num">${o.distinct_arrest_states}</td>
      <td class="mute">${escapeHtml(o.states || "")}</td>
    </tr>`).join("");
}

// -------------------- LAW & ORDER ----------------------------------------
async function loadLawOrder() {
  let f, ch, ct, io;
  try {
    [f, ch, ct, io] = await Promise.all([
      api("/law-order/status-funnel", { _key: "lo-funnel" }),
      api("/law-order/chargesheet-rate", { _key: "lo-cs" }),
      api("/law-order/court-pendency", { _key: "lo-court" }),
      api("/law-order/io-workload", { _key: "lo-io" }),
    ]);
  } catch { return; }

  makeChart("lo_funnel", "lo-funnel", {
    type: "bar",
    data: { labels: f.status.map(x => x.status),
      datasets: [{ data: f.status.map(x => x.c), backgroundColor: "#5aa2ff", borderRadius: 3 }] },
    options: chartOpts({ plugins: { legend: { display: false } }, indexAxis: "y" }),
  });

  $("#lo-chargesheet tbody").innerHTML = ch.districts.slice(0, 20).map(r => `
    <tr>
      <td class="strong">${escapeHtml(r.district)}</td>
      <td class="num">${fmt(r.total || 0)}</td>
      <td class="num">${fmt(r.chargesheeted || 0)}</td>
      <td class="num" style="color:var(--danger)">${fmt(r.false_cases || 0)}</td>
      <td class="num pill-cell"><span class="pill ${csClass(r.pct)}">${r.pct != null ? r.pct + "%" : "—"}</span></td>
    </tr>`).join("");
  $("#lo-court tbody").innerHTML = ct.courts.slice(0, 15).map(r => `
    <tr>
      <td class="strong">${escapeHtml(r.court)}</td>
      <td class="mute">${escapeHtml(r.district)}</td>
      <td class="num">${fmt(r.pending || 0)}</td>
      <td class="num mute">${fmt(r.total || 0)}</td>
    </tr>`).join("");
  $("#lo-io tbody").innerHTML = io.officers.slice(0, 15).map(r => `
    <tr>
      <td class="strong">${escapeHtml(r.io_name)}</td>
      <td class="mute">${escapeHtml(r.station || "—")}</td>
      <td class="num">${fmt(r.caseload || 0)}</td>
      <td class="num" style="color:var(--warn)">${fmt(r.open_cases || 0)}</td>
    </tr>`).join("");
}
function csClass(p) { if (p == null) return "flat"; if (p >= 60) return "down"; if (p >= 40) return "info"; return "warn"; }

// -------------------- ANOMALIES & TRENDS ---------------------------------
async function loadAnomaliesAndTrends() {
  let a, t;
  try {
    [a, t] = await Promise.all([api("/anomalies", { _key: "anom" }), api("/trends/emerging", { _key: "trends" })]);
  } catch { return; }
  const at = $("#anomaly-list tbody");
  at.innerHTML = a.anomalies.length ? a.anomalies.map(r => {
    const zc = r.z_score >= 3 ? "up" : "warn";
    return `<tr>
      <td class="strong">${escapeHtml(r.district)}</td>
      <td>${escapeHtml(r.category)} <span class="hint">· ${escapeHtml(r.class)}</span></td>
      <td class="num">${r.observed}</td>
      <td class="num mute">${r.expected}</td>
      <td class="num pill-cell"><span class="pill ${zc}">z=${r.z_score}</span></td>
    </tr>`;
  }).join("") : `<tr><td colspan="5" class="empty-state">No anomalies for ${a.reference_month || "n/a"}.</td></tr>`;

  const tt = $("#trends-list tbody");
  tt.innerHTML = t.trends.length ? t.trends.map(r => {
    const g = r.growth_pct;
    const cls = g === null ? "up" : g > 20 ? "up" : g < -20 ? "down" : "flat";
    const label = g === null ? "new" : `${g > 0 ? "+" : ""}${g}%`;
    return `<tr>
      <td class="strong">${escapeHtml(r.category)}</td>
      <td class="mute">${escapeHtml(r.class)}</td>
      <td class="num">${r.prior}</td>
      <td class="num">${r.recent}</td>
      <td class="num pill-cell"><span class="pill ${cls}">${label}</span></td>
    </tr>`;
  }).join("") : `<tr><td colspan="5" class="empty-state">Not enough data.</td></tr>`;
}

// -------------------- ASSISTANT ------------------------------------------
async function assistantAsk() {
  const q = $("#assistant-q").value.trim(); if (!q) return;
  const out = $("#assistant-output");
  out.innerHTML = `<div class="hint">Thinking…</div>`;
  let r;
  try {
    r = await api("/assistant/ask", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question: q }), _key: "assistant",
    });
  } catch { return; }
  const backendCls = r.backend === "offline" ? "offline" : "online";
  let html = `
    <div class="assistant-meta">
      <span class="badge-backend ${backendCls}">${escapeHtml(r.backend)}</span>
      <span class="explanation">${escapeHtml(r.explanation || "")}</span>
    </div>
    <pre>${escapeHtml(r.sql)}</pre>`;
  if (r.error) {
    html += `<div class="error">${escapeHtml(r.error)}</div>`;
  } else if (r.rows.length === 0) {
    html += `<div class="hint">Query returned no rows.</div>`;
  } else {
    html += `<div style="max-height:420px; overflow:auto"><table class="data-table">
      <thead><tr>${r.columns.map(c => `<th>${escapeHtml(c)}</th>`).join("")}</tr></thead>
      <tbody>${r.rows.slice(0, 200).map(row =>
        `<tr>${row.map(v => `<td>${escapeHtml(String(v ?? "—"))}</td>`).join("")}</tr>`).join("")}</tbody>
      </table></div>
      <div class="row-count">${r.row_count} row(s)</div>`;
  }
  out.innerHTML = html;
  $("#assistant-backend").textContent = `backend: ${r.backend}`;
}

// -------------------- utils ----------------------------------------------
function fmt(n)  { return Number(n).toLocaleString(); }
function pct(a,b){ return b ? (a / b * 100).toFixed(1) + "%" : "—"; }
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({ "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;" }[c]));
}

document.addEventListener("DOMContentLoaded", bootstrap);
