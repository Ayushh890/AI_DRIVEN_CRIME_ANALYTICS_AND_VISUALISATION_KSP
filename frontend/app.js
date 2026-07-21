// KSP CIP dashboard — SPA controller.
const $  = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));

const state = {
  meta: null,
  filters: {},
  charts: {},
  maps: {},        // { geo: L.Map, predict: L.Map, cb: L.Map }
  mapLayers: {},   // per map
  currentLayer: "heatmap",
  offenderNet: null,
  currentNetTab: "offenders",
};

const CLASS_COLORS = {
  "Property Crimes":     "#58a6ff",
  "Crimes Against Body": "#f85149",
  "Crimes Against Women":"#f0883e",
  "Economic Offences":   "#3fb950",
  "Cyber Crimes":        "#a371f7",
  "Narcotic Offences":   "#e3b341",
  "Traffic Offences":    "#7ee787",
  "Other IPC":           "#8b949e",
};
const nodeGroupColor = (cls) => CLASS_COLORS[cls] || "#8b949e";

// ----------------------------------- fetch --------------------------------------
async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error(`${path} ${r.status}`);
  return r.json();
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

// ----------------------------------- bootstrap --------------------------------------
async function bootstrap() {
  const meta = await api("/meta");
  state.meta = meta;
  populateFilters(meta);
  $("#date-range").textContent =
    `data window: ${meta.date_range.min.slice(0,10)} → ${meta.date_range.max.slice(0,10)}`;
  wireTabs();
  wireFilters();
  wireMapControls();
  wireNetTabs();
  wirePredictControls();
  wireAssistant();
  wireCrossborderTab();
  await refreshOverviewAndGeo();
  // Show LLM backend indicator.
  api("/assistant/health").then((h) => {
    $("#assistant-backend").textContent = `· backend: ${h.backend}`;
  });
}

function populateFilters(meta) {
  const dSel = $("#f-district");
  meta.districts.forEach((d) => {
    const o = document.createElement("option");
    o.value = d.id; o.textContent = d.name; dSel.appendChild(o);
  });
  const cls = new Set(meta.crime_subheads.map((s) => s.head_name));
  const clsSel = $("#f-class");
  Array.from(cls).sort().forEach((k) => {
    const o = document.createElement("option");
    o.value = k; o.textContent = k; clsSel.appendChild(o);
  });
  const sub = $("#f-subhead");
  meta.crime_subheads.forEach((s) => {
    const o = document.createElement("option");
    o.value = s.id; o.textContent = `${s.name} — ${s.head_name}`;
    sub.appendChild(o);
  });
  $("#f-from").value = meta.date_range.min.slice(0, 10);
  $("#f-to").value   = meta.date_range.max.slice(0, 10);

  // Predict tab selects.
  const predHead = $("#predict-head");
  const fcstHead = $("#fcst-head");
  meta.crime_heads.forEach((h) => {
    const o1 = document.createElement("option"); o1.value = h.id; o1.textContent = h.name; predHead.appendChild(o1);
    const o2 = document.createElement("option"); o2.value = h.id; o2.textContent = h.name; fcstHead.appendChild(o2);
  });
  const fcstDist = $("#fcst-district");
  meta.districts.forEach((d) => {
    const o = document.createElement("option"); o.value = d.id; o.textContent = d.name; fcstDist.appendChild(o);
  });
}

// ----------------------------------- tabs --------------------------------------
function wireTabs() {
  $$(".tab").forEach((t) => {
    t.addEventListener("click", () => {
      $$(".tab").forEach((x) => x.classList.remove("active"));
      t.classList.add("active");
      const id = t.dataset.tab;
      $$(".panel").forEach((p) => p.classList.add("hidden"));
      $(`#tab-${id}`).classList.remove("hidden");
      window.dispatchEvent(new Event("resize"));
      if (id === "geo")         { ensureMap("geo").invalidateSize(); loadGeo(); }
      if (id === "predict")     { ensureMap("predict").invalidateSize(); loadPredictMap(); }
      if (id === "network")     { loadNetTab(); }
      if (id === "crossborder") { ensureMap("cb").invalidateSize(); loadCrossborder(); }
      if (id === "laworder")    { loadLawOrder(); }
      if (id === "anomaly")     { loadAnomaliesAndTrends(); }
    });
  });
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
function wireFilters() {
  $("#f-apply").addEventListener("click", async () => { readFilters(); await refreshOverviewAndGeo(); });
  $("#f-reset").addEventListener("click", async () => {
    $("#f-from").value = state.meta.date_range.min.slice(0,10);
    $("#f-to").value   = state.meta.date_range.max.slice(0,10);
    $("#f-district").value = ""; $("#f-class").value = ""; $("#f-subhead").value = "";
    readFilters(); await refreshOverviewAndGeo();
  });
}

async function refreshOverviewAndGeo() {
  $("#status-badge").textContent = "Loading…";
  readFilters();
  try {
    await Promise.all([loadStats(), loadGeo()]);
    $("#status-badge").textContent = "Ready";
  } catch (e) { console.error(e); $("#status-badge").textContent = "Error"; }
}

// ----------------------------------- overview --------------------------------------
async function loadStats() {
  const s = await api(`/stats${currentFilterQS()}`);
  renderKpis(s);
  renderMonthChart(s.by_month);
  renderClassChart(s.by_class);
  renderHourChart(s.by_hour);
  renderCategoryChart(s.by_category);
  renderGravityChart(s.by_gravity);
  renderStatusChart(s.by_status);
}
function renderKpis(s) {
  const violent = s.by_class.find((x) => x.class === "Crimes Against Body")?.count ?? 0;
  const cyber = s.by_class.find((x) => x.class === "Cyber Crimes")?.count ?? 0;
  const heinous = s.by_gravity.find((g) => g.gravity === "Heinous")?.count ?? 0;
  const chargesheeted = s.by_status.find((x) => x.status === "ChargeSheeted")?.count ?? 0;
  const total = s.total || 0;
  const kpis = [
    { label: "Total FIRs", value: total.toLocaleString(), sub: "matching filters" },
    { label: "Heinous", value: heinous.toLocaleString(), sub: total ? `${(heinous/total*100).toFixed(1)}% of cases` : "" },
    { label: "Chargesheeted", value: chargesheeted.toLocaleString(), sub: total ? `${(chargesheeted/total*100).toFixed(1)}% solved` : "" },
    { label: "Violent · Cyber", value: `${violent.toLocaleString()} · ${cyber.toLocaleString()}`, sub: "high-priority classes" },
  ];
  $("#kpis").innerHTML = kpis.map((k) => `
    <div class="kpi">
      <div class="label">${k.label}</div>
      <div class="value">${k.value}</div>
      <div class="sub">${k.sub}</div>
    </div>`).join("");
}
function chartOpts(extra = {}) {
  return {
    responsive: true, maintainAspectRatio: false,
    plugins: { legend: { labels: { color: "#e6edf3", font: { size: 11 } } } },
    scales: {
      x: { ticks: { color: "#8b949e" }, grid: { color: "#30363d" } },
      y: { ticks: { color: "#8b949e" }, grid: { color: "#30363d" } },
    }, ...extra,
  };
}
function makeChart(key, ctx, cfg) {
  if (state.charts[key]) state.charts[key].destroy();
  state.charts[key] = new Chart(ctx, cfg);
}
function renderMonthChart(rows) {
  makeChart("month", $("#chart-month"), {
    type: "line",
    data: { labels: rows.map(r => r.month), datasets: [{ label: "FIRs", data: rows.map(r => r.count),
      borderColor: "#f78166", backgroundColor: "rgba(247,129,102,0.2)", fill: true, tension: 0.3 }] },
    options: chartOpts(),
  });
}
function renderClassChart(rows) {
  makeChart("class", $("#chart-class"), {
    type: "doughnut",
    data: { labels: rows.map(r => r.class), datasets: [{ data: rows.map(r => r.count),
      backgroundColor: rows.map(r => nodeGroupColor(r.class)) }] },
    options: { responsive: true, maintainAspectRatio: false,
      plugins: { legend: { position: "right", labels: { color: "#e6edf3", font: { size: 10 } } } } },
  });
}
function renderHourChart(rows) {
  const data = new Array(24).fill(0);
  rows.forEach(r => data[r.hour] = r.count);
  makeChart("hour", $("#chart-hour"), {
    type: "bar",
    data: { labels: data.map((_, i) => `${String(i).padStart(2, "0")}h`),
      datasets: [{ data, backgroundColor: "#58a6ff" }] },
    options: chartOpts({ plugins: { legend: { display: false } } }),
  });
}
function renderCategoryChart(rows) {
  const top = rows.slice(0, 10);
  makeChart("category", $("#chart-category"), {
    type: "bar",
    data: { labels: top.map(r => r.label),
      datasets: [{ data: top.map(r => r.count), backgroundColor: top.map(r => nodeGroupColor(r.class)) }] },
    options: chartOpts({ indexAxis: "y", plugins: { legend: { display: false } } }),
  });
}
function renderGravityChart(rows) {
  const colors = { "Heinous": "#f85149", "Non-Heinous": "#f0883e", "Petty": "#3fb950" };
  makeChart("gravity", $("#chart-gravity"), {
    type: "bar",
    data: { labels: rows.map(r => r.gravity),
      datasets: [{ data: rows.map(r => r.count), backgroundColor: rows.map(r => colors[r.gravity] || "#8b949e") }] },
    options: chartOpts({ plugins: { legend: { display: false } } }),
  });
}
function renderStatusChart(rows) {
  const colors = { "UnderInvestigation": "#f0883e", "ChargeSheeted": "#58a6ff", "Closed": "#3fb950", "Pending Trial": "#a371f7", "PendingBeforeCourt": "#e3b341" };
  makeChart("status", $("#chart-status"), {
    type: "bar",
    data: { labels: rows.map(r => r.status),
      datasets: [{ data: rows.map(r => r.count), backgroundColor: rows.map(r => colors[r.status] || "#8b949e") }] },
    options: chartOpts({ plugins: { legend: { display: false } } }),
  });
}

// ----------------------------------- maps helper --------------------------------------
function ensureMap(key) {
  if (state.maps[key]) return state.maps[key];
  const containerId = key === "geo" ? "map" : key === "predict" ? "predict-map" : "cb-map";
  const m = L.map(containerId, { zoomControl: true }).setView([14.5, 76.0], 7);
  L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_nolabels/{z}/{x}/{y}{r}.png", {
    attribution: "&copy; OSM &copy; CARTO", subdomains: "abcd", maxZoom: 19,
  }).addTo(m);
  state.maps[key] = m;
  state.mapLayers[key] = {};
  return m;
}
function clearMapLayers(key) {
  const m = state.maps[key];
  if (!m) return;
  Object.values(state.mapLayers[key] || {}).forEach((l) => m.removeLayer(l));
  state.mapLayers[key] = {};
}

// ----------------------------------- geospatial --------------------------------------
async function loadGeo() {
  ensureMap("geo");
  clearMapLayers("geo");
  if (state.currentLayer === "heatmap") await drawHeatmap();
  if (state.currentLayer === "points")   await drawPoints();
  if (state.currentLayer === "districts") await drawDistricts();
  await drawDistrictList();
}
async function drawHeatmap() {
  const h = await api(`/geo/heatmap${currentFilterQS({ cell_km: 2 })}`);
  if (!h.cells.length) return;
  const pts = h.cells.map(c => [c.lat, c.lng, c.intensity]);
  state.mapLayers.geo.heat = L.heatLayer(pts, {
    radius: 22, blur: 20, maxZoom: 10, minOpacity: 0.35,
    gradient: { 0.2: "#58a6ff", 0.5: "#f0883e", 0.8: "#f85149", 1.0: "#ffffff" },
  }).addTo(state.maps.geo);
}
async function drawPoints() {
  const p = await api(`/geo/points${currentFilterQS({ limit: 3000 })}`);
  const layer = L.layerGroup();
  p.points.forEach(pt => {
    const color = nodeGroupColor(pt.class);
    const m = L.circleMarker([pt.lat, pt.lng], {
      radius: 4, color, weight: 1, fillOpacity: 0.7, fillColor: color,
    }).bindPopup(
      `<b>${pt.fir_number}</b><br>${pt.category} · ${pt.class}<br>${pt.occurred_at.replace("T", " ")}<br>MO: ${pt.mo || "—"} · Status: ${pt.status || "—"}<br>Gravity: ${pt.gravity || "—"}`
    );
    layer.addLayer(m);
  });
  state.mapLayers.geo.points = layer.addTo(state.maps.geo);
}
async function drawDistricts() {
  const d = await api(`/geo/district-summary${currentFilterQS()}`);
  const layer = L.layerGroup();
  const maxRate = Math.max(1, ...d.districts.map(r => r.per_lakh || 0));
  d.districts.forEach(r => {
    if (!r.hq_lat) return;
    const rate = r.per_lakh || 0;
    const t = rate / maxRate;
    const radius = 8 + t * 26;
    const color = t > 0.7 ? "#f85149" : t > 0.4 ? "#f0883e" : "#58a6ff";
    layer.addLayer(L.circleMarker([r.hq_lat, r.hq_lng], {
      radius, color, weight: 1, fillOpacity: 0.55, fillColor: color,
    }).bindPopup(`<b>${r.name}</b> (${r.zone || ""})<br>${r.count.toLocaleString()} FIRs<br>${rate.toFixed(1)} per lakh`));
  });
  state.mapLayers.geo.districts = layer.addTo(state.maps.geo);
}
async function drawDistrictList() {
  const d = await api(`/geo/district-summary${currentFilterQS()}`);
  const max = Math.max(1, ...d.districts.map(r => r.count));
  $("#district-list").innerHTML = d.districts.map(r => `
    <div class="district-row" data-district="${r.id}">
      <div class="name">${r.name}</div>
      <div class="count">${r.count.toLocaleString()}</div>
      <div class="rate">${(r.per_lakh || 0).toFixed(1)}/lakh</div>
      <div class="bar"><div class="bar-fill" style="width:${r.count / max * 100}%"></div></div>
    </div>`).join("");
  $$("#district-list .district-row").forEach(row => {
    row.addEventListener("click", () => {
      $("#f-district").value = row.dataset.district;
      readFilters(); refreshOverviewAndGeo();
    });
  });
}
function wireMapControls() {
  $$("input[name=layer]").forEach(r => {
    r.addEventListener("change", async () => { state.currentLayer = r.value; loadGeo(); });
  });
}

// ----------------------------------- predict --------------------------------------
function wirePredictControls() {
  $("#predict-refresh").addEventListener("click", loadPredictMap);
  $("#predict-horizon").addEventListener("change", loadPredictMap);
  $("#predict-head").addEventListener("change", loadPredictMap);
  $("#fcst-run").addEventListener("click", loadForecastChart);
}
async function loadPredictMap() {
  ensureMap("predict");
  clearMapLayers("predict");
  const head = $("#predict-head").value;
  const horizon = $("#predict-horizon").value;
  const q = qs({ horizon, ...(head ? { head } : {}) });
  const r = await api(`/predict/density-map${q}`);
  if (!r.cells.length) return;
  const pts = r.cells.map(c => [c.lat, c.lng, c.intensity]);
  state.mapLayers.predict.heat = L.heatLayer(pts, {
    radius: 24, blur: 22, maxZoom: 10, minOpacity: 0.30,
    gradient: { 0.2: "#58a6ff", 0.5: "#a371f7", 0.8: "#f0883e", 1.0: "#f85149" },
  }).addTo(state.maps.predict);
  // Top 10 predicted-hotspot markers with popups.
  const top = [...r.cells].sort((a, b) => b.predicted_next - a.predicted_next).slice(0, 10);
  const markers = L.layerGroup();
  top.forEach(c => {
    markers.addLayer(L.circleMarker([c.lat, c.lng], {
      radius: 8, color: "#f85149", weight: 2, fillOpacity: 0.4, fillColor: "#f85149",
    }).bindPopup(
      `<b>Unit ${c.unit_id}</b><br>Predicted next ${r.horizon_weeks}w: <b>${c.predicted_next}</b><br>Recent ${r.horizon_weeks}w: ${c.recent_actual}<br>Δ ${c.delta_pct == null ? "—" : c.delta_pct + "%"}`
    ));
  });
  state.mapLayers.predict.markers = markers.addTo(state.maps.predict);
}
async function loadForecastChart() {
  const did = $("#fcst-district").value;
  const hid = $("#fcst-head").value;
  if (!did || !hid) return;
  const f = await api(`/predict/district/${did}/head/${hid}?horizon=8`);
  const histLabels = f.history_weeks;
  const histVals = f.history_values;
  const fcstLabels = f.weeks;
  makeChart("fcst", $("#fcst-chart"), {
    type: "line",
    data: {
      labels: [...histLabels, ...fcstLabels],
      datasets: [
        { label: "History", data: [...histVals, ...new Array(fcstLabels.length).fill(null)],
          borderColor: "#58a6ff", backgroundColor: "rgba(88,166,255,0.15)", tension: 0.2, pointRadius: 0 },
        { label: "Forecast", data: [...new Array(histLabels.length).fill(null), ...f.point],
          borderColor: "#f78166", borderDash: [5, 5], tension: 0.2, pointRadius: 0 },
        { label: "Upper 90%", data: [...new Array(histLabels.length).fill(null), ...f.upper],
          borderColor: "rgba(247,129,102,0.4)", pointRadius: 0, borderWidth: 1 },
        { label: "Lower 90%", data: [...new Array(histLabels.length).fill(null), ...f.lower],
          borderColor: "rgba(247,129,102,0.4)", pointRadius: 0, borderWidth: 1 },
      ]
    },
    options: chartOpts({ scales: { x: { ticks: { color: "#8b949e", maxTicksLimit: 12 }, grid: { color: "#30363d" } }, y: { ticks: { color: "#8b949e" }, grid: { color: "#30363d" }, beginAtZero: true } } }),
  });
  $("#fcst-meta").textContent = `method: ${f.method} · history ${histLabels.length} weeks · forecast ${fcstLabels.length} weeks`;
}

// ----------------------------------- network --------------------------------------
function wireNetTabs() {
  $$(".net-tab").forEach(t => {
    t.addEventListener("click", () => {
      $$(".net-tab").forEach(x => x.classList.remove("active"));
      t.classList.add("active");
      state.currentNetTab = t.dataset.nettab;
      loadNetTab();
    });
  });
}
async function loadNetTab() {
  const tab = state.currentNetTab;
  if (tab === "offenders")   await loadOffenders();
  if (tab === "communities") await loadCommunities();
  if (tab === "central")     await loadCentralFigures();
  if (tab === "mo")          await loadMoSimilar();
}
async function loadOffenders() {
  const o = await api("/offenders/top?limit=25");
  $("#offender-list").innerHTML = o.offenders.map(p => `
    <div class="offender-row" data-pid="${p.id}">
      <div class="name">${p.full_name}</div>
      <div class="stats">${p.incidents} FIRs · ${p.distinct_categories} sub-heads · ${p.district || "—"}</div>
    </div>`).join("");
  $$("#offender-list .offender-row").forEach(row => {
    row.addEventListener("click", () => {
      $$("#offender-list .offender-row").forEach(r => r.classList.remove("active"));
      row.classList.add("active");
      showOffenderNetwork(row.dataset.pid);
    });
  });
  if (o.offenders.length) {
    $("#offender-list .offender-row").classList.add("active");
    await showOffenderNetwork(o.offenders[0].id);
  }
}
async function loadCommunities() {
  const r = await api("/network/communities?top_n=200");
  $("#offender-list").innerHTML = `<div class="muted" style="padding:6px 10px">Louvain: ${r.summary.nodes} nodes, ${r.summary.edges} edges, ${r.communities.length} communities</div>` +
    r.communities.map(co => `
      <div class="community-row" data-community="${co.community_id}">
        <div class="head"><span>Community #${co.community_id}</span><span>${co.total_cases} cases</span></div>
        <div class="members">${co.size} members · <span class="sigs">${co.signature_crimes.join(" · ")}</span></div>
      </div>`).join("");
  $$("#offender-list .community-row").forEach(row => {
    row.addEventListener("click", () => showCommunityGraph(r, +row.dataset.community));
  });
  if (r.communities.length) showCommunityGraph(r, r.communities[0].community_id);
}
async function loadCentralFigures() {
  const r = await api("/network/central-figures?limit=25");
  $("#offender-list").innerHTML = r.figures.map(f => `
    <div class="offender-row" data-pid="${f.person_link_id}">
      <div class="name">${f.name}</div>
      <div class="stats">score ${f.score} · ${f.cases} cases · wdeg ${f.weighted_degree}<br>
        <span class="sigs">${f.signature.join(" · ")}</span></div>
    </div>`).join("");
  $$("#offender-list .offender-row").forEach(row => {
    row.addEventListener("click", () => {
      $$("#offender-list .offender-row").forEach(r => r.classList.remove("active"));
      row.classList.add("active");
      showOffenderNetwork(row.dataset.pid);
    });
  });
  if (r.figures.length) {
    $("#offender-list .offender-row").classList.add("active");
    await showOffenderNetwork(r.figures[0].person_link_id);
  }
}
async function loadMoSimilar() {
  const r = await api("/network/mo-similarity?top_n=60");
  $("#offender-list").innerHTML = r.pairs.slice(0, 40).map(p => `
    <div class="offender-row" data-pair="${p.a_id},${p.b_id}">
      <div class="name">${p.a_name} ↔ ${p.b_name}</div>
      <div class="stats">similarity ${p.similarity} · ${p.a_cases} & ${p.b_cases} cases</div>
    </div>`).join("");
  $$("#offender-list .offender-row").forEach(row => {
    row.addEventListener("click", () => {
      const [a, _b] = row.dataset.pair.split(",");
      showOffenderNetwork(a);
    });
  });
}
async function showOffenderNetwork(pid) {
  const g = await api(`/network/offender/${pid}`);
  const p = g.person;
  $("#graph-title").textContent = `${p.full_name} · id ${p.id}`;
  $("#offender-summary").innerHTML = `
    <div><strong>${g.summary.total_firs}</strong> FIRs</div>
    <div><strong>${g.summary.distinct_co_offenders}</strong> co-offenders</div>
    <div><strong>${g.summary.distinct_victims}</strong> victims</div>`;
  const groups = {
    offender_primary: { color: { background: "#f78166", border: "#f78166" }, font: { color: "#fff" }, size: 30 },
    offender:         { color: { background: "#f85149", border: "#f85149" }, font: { color: "#fff" } },
    victim:           { color: { background: "#3fb950", border: "#3fb950" }, font: { color: "#fff" } },
    property_crimes:  { color: { background: "#58a6ff", border: "#58a6ff" }, font: { color: "#fff" } },
    crimes_against_body: { color: { background: "#f85149", border: "#f85149" }, font: { color: "#fff" } },
    crimes_against_women: { color: { background: "#f0883e", border: "#f0883e" }, font: { color: "#fff" } },
    economic_offences: { color: { background: "#3fb950", border: "#3fb950" }, font: { color: "#fff" } },
    cyber_crimes:     { color: { background: "#a371f7", border: "#a371f7" }, font: { color: "#fff" } },
    narcotic_offences:{ color: { background: "#e3b341", border: "#e3b341" }, font: { color: "#fff" } },
    traffic_offences: { color: { background: "#7ee787", border: "#7ee787" }, font: { color: "#fff" } },
    other_ipc:        { color: { background: "#8b949e", border: "#8b949e" }, font: { color: "#fff" } },
  };
  const container = $("#graph");
  container.innerHTML = "";
  if (state.offenderNet) state.offenderNet.destroy();
  state.offenderNet = new vis.Network(container, {
    nodes: new vis.DataSet(g.graph.nodes),
    edges: new vis.DataSet(g.graph.edges),
  }, {
    layout: { improvedLayout: true },
    physics: { stabilization: { iterations: 120 }, barnesHut: { gravitationalConstant: -6000 } },
    interaction: { hover: true, tooltipDelay: 100 },
    edges: { color: { color: "#30363d", highlight: "#f78166" }, smooth: { type: "continuous" } },
    nodes: { font: { color: "#e6edf3", size: 11 }, borderWidth: 1 },
    groups,
  });
}
function showCommunityGraph(payload, commId) {
  const co = payload.communities.find(c => c.community_id === commId);
  if (!co) return;
  $("#graph-title").textContent = `Community #${co.community_id} — ${co.size} members`;
  $("#offender-summary").innerHTML = `
    <div><strong>${co.total_cases}</strong> total cases</div>
    <div>signature: <strong>${co.signature_crimes.join(", ")}</strong></div>`;
  const nodes = co.members.map(m => ({
    id: m.person_link_id, label: m.name,
    title: `${m.name} · ${m.cases} cases · wdeg ${m.weighted_degree}`,
    value: Math.log(m.cases + 1),
    group: "offender",
  }));
  // Star edges from the most-central member to the rest — quick visual.
  const center = co.members[0].person_link_id;
  const edges = co.members.slice(1).map(m => ({ from: center, to: m.person_link_id, dashes: true }));
  const container = $("#graph");
  container.innerHTML = "";
  if (state.offenderNet) state.offenderNet.destroy();
  state.offenderNet = new vis.Network(container, {
    nodes: new vis.DataSet(nodes), edges: new vis.DataSet(edges),
  }, {
    physics: { stabilization: { iterations: 100 }, barnesHut: { gravitationalConstant: -5000 } },
    edges: { color: { color: "#30363d" } },
    nodes: { font: { color: "#e6edf3", size: 11 }, borderWidth: 1, shape: "dot", scaling: { min: 6, max: 24 } },
    groups: { offender: { color: { background: "#f85149", border: "#f85149" }, font: { color: "#fff" } } },
  });
}

// ----------------------------------- cross-border --------------------------------------
function wireCrossborderTab() { /* nothing extra */ }
async function loadCrossborder() {
  ensureMap("cb");
  clearMapLayers("cb");
  const s = await api("/cross-border/summary");
  const mv = await api("/cross-border/movements?limit=100");
  const offs = await api("/cross-border/offenders?limit=15");

  // Draw arrest-location markers.
  const locLayer = L.layerGroup();
  const max = Math.max(1, ...s.by_district.map(d => d.arrests));
  s.by_district.forEach(d => {
    if (!d.lat) return;
    const r = 6 + 20 * (d.arrests / max);
    locLayer.addLayer(L.circleMarker([d.lat, d.lng], {
      radius: r, color: "#f85149", weight: 1, fillOpacity: 0.5, fillColor: "#f85149",
    }).bindPopup(`<b>${d.district}, ${d.state}</b><br>${d.arrests} cross-border arrests`));
  });
  state.mapLayers.cb.locs = locLayer.addTo(state.maps.cb);

  // Draw home→arrest movement lines.
  const lineLayer = L.layerGroup();
  const mvMax = Math.max(1, ...mv.movements.map(m => m.movements));
  mv.movements.forEach(m => {
    const w = 1 + 4 * (m.movements / mvMax);
    lineLayer.addLayer(L.polyline([[m.home_lat, m.home_lng], [m.arrest_lat, m.arrest_lng]], {
      color: "#a371f7", weight: w, opacity: 0.55,
    }).bindPopup(`<b>${m.home_district} → ${m.arrest_district}</b> (${m.arrest_state || "—"})<br>${m.movements} movements`));
  });
  state.mapLayers.cb.lines = lineLayer.addTo(state.maps.cb);
  state.maps.cb.setView([16.0, 78.0], 6);

  // Chart: arrests by state.
  makeChart("cb_state", $("#cb-state"), {
    type: "bar",
    data: { labels: s.by_state.map(x => x.state),
      datasets: [{ data: s.by_state.map(x => x.arrests), backgroundColor: "#a371f7" }] },
    options: chartOpts({ plugins: { legend: { display: false } }, indexAxis: "y" }),
  });
  makeChart("cb_class", $("#cb-class"), {
    type: "bar",
    data: { labels: s.by_class.map(x => x.class),
      datasets: [{ data: s.by_class.map(x => x.arrests), backgroundColor: s.by_class.map(x => nodeGroupColor(x.class)) }] },
    options: chartOpts({ plugins: { legend: { display: false } }, indexAxis: "y" }),
  });

  $("#cb-offenders").innerHTML = offs.offenders.map(o => `
    <div class="row">
      <div class="name">${o.name}</div>
      <div class="n">${o.total_arrests} arrests</div>
      <div class="n">${o.distinct_arrest_states} states</div>
      <div class="states">across: ${o.states}</div>
    </div>`).join("");
}

// ----------------------------------- law & order --------------------------------------
async function loadLawOrder() {
  const [f, ch, ct, io] = await Promise.all([
    api("/law-order/status-funnel"),
    api("/law-order/chargesheet-rate"),
    api("/law-order/court-pendency"),
    api("/law-order/io-workload"),
  ]);
  makeChart("lo_funnel", $("#lo-funnel"), {
    type: "bar",
    data: { labels: f.status.map(x => x.status),
      datasets: [{ data: f.status.map(x => x.c), backgroundColor: "#58a6ff" }] },
    options: chartOpts({ plugins: { legend: { display: false } }, indexAxis: "y" }),
  });
  $("#lo-chargesheet").innerHTML = ch.districts.slice(0, 20).map(r => `
    <div class="lo-row district">
      <div class="name">${r.district}</div>
      <div class="num">${r.total || 0}</div>
      <div class="num">${r.chargesheeted || 0}</div>
      <div class="num" style="color:var(--danger)">${r.false_cases || 0}B</div>
      <div class="num" style="color:var(--warn)">${r.pct != null ? r.pct + "%" : "—"}</div>
    </div>`).join("");
  $("#lo-court").innerHTML = ct.courts.slice(0, 15).map(r => `
    <div class="lo-row court">
      <div><div class="name">${r.court}</div><div class="sub">${r.district}</div></div>
      <div class="num">${r.pending || 0}</div>
      <div class="num">${r.total || 0}</div>
    </div>`).join("");
  $("#lo-io").innerHTML = io.officers.slice(0, 15).map(r => `
    <div class="lo-row io">
      <div><div class="name">${r.io_name}</div><div class="sub">${r.station || "—"}</div></div>
      <div class="num">${r.caseload || 0}</div>
      <div class="num" style="color:var(--warn)">${r.open_cases || 0} open</div>
    </div>`).join("");
}

// ----------------------------------- anomalies + trends --------------------------------------
async function loadAnomaliesAndTrends() {
  const [a, t] = await Promise.all([api("/anomalies"), api("/trends/emerging")]);
  const rows = a.anomalies;
  $("#anomaly-list").innerHTML = rows.length ? rows.map(r => {
    const cls = r.z_score >= 3 ? "high" : "med";
    return `<div class="anomaly-row">
      <div><div class="place">${r.district}</div><div class="cat">${r.category} · ${r.class}</div></div>
      <div class="cnt">${r.observed} <span class="muted">obs / ${r.expected} exp</span></div>
      <div class="z ${cls}">z=${r.z_score}</div></div>`;
  }).join("") : `<div class="muted">No anomalies for ${a.reference_month || "n/a"}.</div>`;

  const trows = t.trends;
  $("#trends-list").innerHTML = trows.length ? trows.map(r => {
    const g = r.growth_pct;
    const cls = g === null ? "up" : g > 20 ? "up" : g < -20 ? "down" : "flat";
    const label = g === null ? "new" : `${g > 0 ? "+" : ""}${g}%`;
    return `<div class="trend-row">
      <div><div class="cat">${r.category}</div><div class="class">${r.class}</div></div>
      <div class="num">${r.prior}</div>
      <div class="num">${r.recent}</div>
      <div class="num growth ${cls}">${label}</div></div>`;
  }).join("") : `<div class="muted">Not enough data.</div>`;
}

// ----------------------------------- assistant --------------------------------------
function wireAssistant() {
  $("#assistant-ask").addEventListener("click", assistantAsk);
  $("#assistant-q").addEventListener("keydown", (e) => { if (e.key === "Enter") assistantAsk(); });
  $$(".chip").forEach(c => c.addEventListener("click", () => {
    $("#assistant-q").value = c.textContent; assistantAsk();
  }));
}
async function assistantAsk() {
  const q = $("#assistant-q").value.trim();
  if (!q) return;
  $("#assistant-output").innerHTML = `<div class="muted">Thinking…</div>`;
  try {
    const r = await api("/assistant/ask", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question: q }),
    });
    let html = "";
    html += `<div class="explanation">${r.explanation} <span class="badge-backend">${r.backend}</span></div>`;
    html += `<pre>${escapeHtml(r.sql)}</pre>`;
    if (r.error) {
      html += `<div class="error">${escapeHtml(r.error)}</div>`;
    } else if (r.rows.length === 0) {
      html += `<div class="muted">Query returned no rows.</div>`;
    } else {
      html += `<table><thead><tr>${r.columns.map(c => `<th>${c}</th>`).join("")}</tr></thead><tbody>`;
      html += r.rows.slice(0, 200).map(row => `<tr>${row.map(v => `<td>${escapeHtml(String(v ?? "—"))}</td>`).join("")}</tr>`).join("");
      html += `</tbody></table>`;
      html += `<div class="muted" style="margin-top:6px">${r.row_count} row(s)</div>`;
    }
    $("#assistant-output").innerHTML = html;
  } catch (e) {
    $("#assistant-output").innerHTML = `<div class="error">${escapeHtml(String(e))}</div>`;
  }
}
function escapeHtml(s) {
  return s.replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;" }[c]));
}

document.addEventListener("DOMContentLoaded", bootstrap);
