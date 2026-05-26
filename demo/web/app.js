const map = L.map("map", { preferCanvas: true, zoomControl: true }).setView([37.535, 127.12], 12);

L.tileLayer("https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png", {
  attribution: "&copy; OpenStreetMap contributors &copy; CARTO",
}).addTo(map);

const form = document.querySelector("#routeForm");
const startInput = document.querySelector("#startInput");
const endInput = document.querySelector("#endInput");
const statusEl = document.querySelector("#status");
const summaryEl = document.querySelector("#summary");
const instructionsEl = document.querySelector("#instructions");
const showTemplates = document.querySelector("#showTemplates");

let routeLayer;
let routeHaloLayer;
let markerLayer = L.layerGroup().addTo(map);
const datasetLayers = new Map();
const datasetCache = new Map();

const datasetStyles = {
  subway_line: (feature) => ({
    color: feature.properties.line_color || "#4b5563",
    weight: 3,
    opacity: 0.5,
  }),
  braille: () => ({
    color: "#f59e0b",
    weight: 7,
    opacity: 0.32,
  }),
  braille_detail: () => ({
    color: "#92400e",
    weight: 2,
    opacity: 0.9,
    dashArray: "1 8",
    lineCap: "round",
  }),
  crosswalk: () => ({
    color: "#e11d48",
    weight: 9,
    opacity: 0.28,
  }),
};

const pointStyles = {
  subway_station: () => ({
    radius: 4,
    color: "#111827",
    weight: 1.5,
    fillColor: "#ffffff",
    fillOpacity: 0.98,
  }),
  subway_elevator: () => ({
    radius: 6,
    color: "#1d4ed8",
    weight: 2,
    fillColor: "#93c5fd",
    fillOpacity: 0.98,
  }),
  audible: () => ({
    radius: 5,
    color: "#a16207",
    weight: 2,
    fillColor: "#fde047",
    fillOpacity: 0.95,
  }),
};

function setStatus(text) {
  statusEl.textContent = text;
}

function formatNumber(value) {
  return Number(value || 0).toLocaleString("ko-KR", { maximumFractionDigits: 1 });
}

function renderSummary(summary) {
  const coverage = summary.dataset_coverage || {};
  const context = summary.route_corridor_context || {};
  const items = [
    ["출발", `${summary.start.label} (${summary.start.source})`],
    ["도착", `${summary.end.label} (${summary.end.source})`],
    ["거리", `${formatNumber(summary.total_length_m)} m`],
    ["가중 비용", formatNumber(summary.total_visual_impairment_cost)],
    ["지하철", summary.uses_subway ? `사용 (${(summary.subway_lines || []).join(", ")})` : "미사용"],
    ["환승", `${formatNumber(summary.transfer_count)}회`],
    ["edge 수", formatNumber(summary.edge_count)],
    ["edge 유형", Object.entries(summary.edge_type_counts || {}).map(([k, v]) => `${k}: ${v}`).join(" / ")],
    ["도보 데이터", `${formatNumber(coverage.walk_length_m)} m / ${formatNumber(coverage.walk_count)}개 edge`],
    ["점자블록", `${formatNumber(coverage.braille_length_m)} m / ${formatNumber(coverage.braille_edge_count)}개 edge`],
    ["횡단보도", `${formatNumber(coverage.crosswalk_length_m)} m / ${formatNumber(coverage.crosswalk_count)}개`],
    ["음향신호", `${formatNumber(coverage.audible_signal_edge_count)}개 edge / 횡단보도 대비 ${Math.round((coverage.audible_crosswalk_ratio || 0) * 100)}%`],
    ["반영 점자블록", `${formatNumber(coverage.near_braille_length_m)} m / ${formatNumber(coverage.near_braille_edge_count)}개 edge`],
    ["반영 횡단보도", `${formatNumber(coverage.near_crosswalk_length_m)} m / ${formatNumber(coverage.near_crosswalk_edge_count)}개 edge`],
    ["반영 음향신호", `${formatNumber(coverage.near_audible_signal_length_m)} m / ${formatNumber(coverage.near_audible_signal_edge_count)}개 edge`],
    ["엘리베이터 연결", `${formatNumber(coverage.elevator_connector_count)}개 / ${formatNumber(coverage.elevator_connector_length_m)} m`],
    ["지하철 이동", `${formatNumber(coverage.subway_ride_length_m)} m / ${formatNumber(coverage.subway_ride_count)}개 edge`],
    ["지하철 연결", `${formatNumber(coverage.subway_connector_length_m)} m / ${formatNumber(coverage.subway_connector_count)}개 edge`],
    ["저신뢰 데이터", `${formatNumber(coverage.low_confidence_length_m)} m / ${formatNumber(coverage.low_confidence_count)}개 edge`],
    ["주변 점자블록", `${formatNumber(context.nearby_braille_edge_count)}개 (${formatNumber(context.radius_m)}m 반경)`],
    ["주변 횡단보도", `${formatNumber(context.nearby_crosswalk_count)}개 (${formatNumber(context.radius_m)}m 반경)`],
    ["주변 음향신호", `${formatNumber(context.nearby_audible_signal_count)}개 (${formatNumber(context.radius_m)}m 반경)`],
  ];
  summaryEl.innerHTML = items.map(([key, value]) => `<dt>${key}</dt><dd>${value}</dd>`).join("");
}

function renderInstructions(instructions) {
  if (!Array.isArray(instructions) || instructions.length === 0) {
    instructionsEl.innerHTML = "<li>아직 생성된 안내 멘트가 없습니다.</li>";
    return;
  }
  instructionsEl.innerHTML = instructions
    .map((item) => `<li class="instruction-${item.type}"><strong>${item.type}</strong><span>${item.text}</span></li>`)
    .join("");
}

function edgeStyle(feature) {
  const p = feature.properties || {};
  const type = p.edge_type;
  const nearBraille = Number(p.near_braille_count || 0) > 0 || p.has_braille === true;
  const nearAudible = Number(p.near_audible_signal_count || 0) > 0 || p.has_audible_signal === true;

  if (type === "subway_ride") {
    return { color: p.line_color || "#2563eb", weight: 7, opacity: 0.9 };
  }
  if (type === "crosswalk") {
    return { color: nearAudible ? "#f59e0b" : "#e11d48", weight: 7, opacity: 0.95, dashArray: "10 5" };
  }
  if (type === "walk" && nearBraille) {
    return { color: "#d97706", weight: 7, opacity: 0.95, dashArray: "2 8", lineCap: "round" };
  }
  if (type === "walk") {
    return { color: "#047857", weight: 5, opacity: 0.85 };
  }
  if (type === "facility_connector" || type === "subway_connector") {
    return { color: "#7c3aed", weight: 5, opacity: 0.88, dashArray: "7 5" };
  }
  return { color: "#334155", weight: 4, opacity: 0.75 };
}

function edgeHaloStyle(feature) {
  const p = feature.properties || {};
  const type = p.edge_type;
  const nearBraille = Number(p.near_braille_count || 0) > 0 || p.has_braille === true;
  const nearAudible = Number(p.near_audible_signal_count || 0) > 0 || p.has_audible_signal === true;

  if (type === "subway_ride") {
    return { color: p.line_color || "#2563eb", weight: 14, opacity: 0.18 };
  }
  if (type === "crosswalk") {
    return { color: nearAudible ? "#f59e0b" : "#e11d48", weight: 15, opacity: 0.24 };
  }
  if (type === "walk" && nearBraille) {
    return { color: "#f59e0b", weight: 15, opacity: 0.28 };
  }
  if (type === "walk") {
    return { color: "#10b981", weight: 13, opacity: 0.18 };
  }
  if (type === "facility_connector" || type === "subway_connector") {
    return { color: "#8b5cf6", weight: 13, opacity: 0.2 };
  }
  return { color: "#64748b", weight: 12, opacity: 0.16 };
}

function popupProps(properties, keys) {
  return keys
    .map((key) => properties?.[key] ? `<div><strong>${key}</strong>: ${properties[key]}</div>` : "")
    .join("");
}

function datasetOptions(name) {
  if (datasetStyles[name]) {
    return {
      style: datasetStyles[name],
      onEachFeature: (feature, layer) => {
        const p = feature.properties || {};
        layer.bindPopup(popupProps(p, [
          "station_name", "지하철역명", "line_name", "line_code", "횡단보도종류",
          "보행등유무", "음향신호기설치여부", "has_braille", "보도노선명",
        ]));
      },
    };
  }
  return {
    pointToLayer: (feature, latlng) => L.circleMarker(latlng, pointStyles[name]?.(feature) || {}),
    onEachFeature: (feature, layer) => {
      const p = feature.properties || {};
      layer.bindPopup(popupProps(p, [
        "station_name", "지하철역명", "line_codes", "시군구명", "읍면동명",
        "노드 ID", "MGRNU", "STAT_CDE",
      ]));
    },
  };
}

async function loadDataset(name) {
  if (!datasetCache.has(name)) {
    setStatus(`${name} 레이어 로딩 중...`);
    const response = await fetch(`/api/dataset?name=${encodeURIComponent(name)}`, { cache: "no-store" });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || `${name} load failed`);
    datasetCache.set(name, payload);
  }
  return datasetCache.get(name);
}

async function setDatasetVisible(name, visible) {
  if (!visible) {
    const layer = datasetLayers.get(name);
    if (layer) map.removeLayer(layer);
    return;
  }
  if (!datasetLayers.has(name)) {
    const data = await loadDataset(name);
    datasetLayers.set(name, L.geoJSON(data, datasetOptions(name)));
  }
  datasetLayers.get(name).addTo(map);
  setStatus(`${name} 레이어 표시 중`);
}

function addMarkers(summary) {
  markerLayer.clearLayers();
  const start = summary.start;
  const end = summary.end;
  L.marker([start.lat, start.lon]).bindPopup(`출발: ${start.label}`).addTo(markerLayer);
  L.marker([end.lat, end.lon]).bindPopup(`도착: ${end.label}`).addTo(markerLayer);
}

async function findRoute(start, end) {
  const response = await fetch("/api/v1/routes", {
    method: "POST",
    cache: "no-store",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      origin: { query: start },
      destination: { query: end },
      profile: "visual_impairment_default",
    }),
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.detail || "route failed");
  return {
    type: "FeatureCollection",
    properties: payload.summary,
    instructions: payload.instructions,
    features: payload.geometry.features,
  };
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const start = startInput.value.trim();
  const end = endInput.value.trim();
  if (!start || !end) return;

  setStatus("경로 계산 중...");
  summaryEl.innerHTML = "";
  try {
    const route = await findRoute(start, end);
    if (routeLayer) map.removeLayer(routeLayer);
    if (routeHaloLayer) map.removeLayer(routeHaloLayer);
    routeHaloLayer = L.geoJSON(route, { style: edgeHaloStyle, interactive: false }).addTo(map);
    routeLayer = L.geoJSON(route, { style: edgeStyle }).addTo(map);
    addMarkers(route.properties);
    renderSummary(route.properties);
    renderInstructions(route.instructions || []);
    map.fitBounds(routeLayer.getBounds(), { padding: [30, 30] });
    setStatus("경로 계산 완료");
  } catch (error) {
    setStatus(`오류: ${error.message}`);
  }
});

showTemplates.addEventListener("click", async () => {
  setStatus("전체 멘트 목록 로딩 중...");
  try {
    const response = await fetch("/api/v1/instruction-templates", { cache: "no-store" });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "template load failed");
    const rows = Object.entries(payload.templates || {}).flatMap(([group, texts]) =>
      texts.map((text) => ({ type: group, text })),
    );
    renderInstructions(rows);
    setStatus("전체 멘트 목록 표시 중");
  } catch (error) {
    setStatus(`멘트 목록 오류: ${error.message}`);
  }
});

document.querySelectorAll("[data-layer]").forEach((checkbox) => {
  checkbox.addEventListener("change", async (event) => {
    const target = event.currentTarget;
    try {
      await setDatasetVisible(target.dataset.layer, target.checked);
    } catch (error) {
      target.checked = false;
      setStatus(`레이어 오류: ${error.message}`);
    }
  });
});

for (const checkbox of document.querySelectorAll("[data-layer]:checked")) {
  setDatasetVisible(checkbox.dataset.layer, true).catch((error) => setStatus(`레이어 오류: ${error.message}`));
}
