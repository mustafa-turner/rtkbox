const modeSelect = document.getElementById("modeSelect");
const startButton = document.getElementById("startButton");
const stopButton = document.getElementById("stopButton");
const saveConfigButton = document.getElementById("saveConfigButton");
const statusLine = document.getElementById("statusLine");
const logBox = document.getElementById("logBox");
const saveMessage = document.getElementById("saveMessage");
const configForm = document.getElementById("configForm");
const menuButtons = document.querySelectorAll(".menu-button");
const views = document.querySelectorAll(".view");
const latValue = document.getElementById("latValue");
const lonValue = document.getElementById("lonValue");
const accValue = document.getElementById("accValue");
const surveyValue = document.getElementById("surveyValue");
const coordSource = document.getElementById("coordSource");
const mapHint = document.getElementById("mapHint");
const positionPanel = document.getElementById("positionPanel");
const controlGrid = document.querySelector(".control-grid");
const recordPanel = document.getElementById("recordPanel");
const recordFileName = document.getElementById("recordFileName");
const recordElapsed = document.getElementById("recordElapsed");
const recordSize = document.getElementById("recordSize");
const recordingsList = document.getElementById("recordingsList");
const recordingsSection = document.querySelector(".downloads");
const tmodeModeSelect = document.getElementById("tmodeModeSelect");
const applyTmodeButton = document.getElementById("applyTmodeButton");
const readTmodeButton = document.getElementById("readTmodeButton");
const saveReceiverButton = document.getElementById("saveReceiverButton");
const tmodeSurveyMinDur = document.getElementById("tmodeSurveyMinDur");
const tmodeSurveyAccLimit = document.getElementById("tmodeSurveyAccLimit");
const tmodeFixedEcefX = document.getElementById("tmodeFixedEcefX");
const tmodeFixedEcefY = document.getElementById("tmodeFixedEcefY");
const tmodeFixedEcefZ = document.getElementById("tmodeFixedEcefZ");

const MODES = ["base-local", "base-ntrip", "rover-local", "rover-ntrip", "receiver-bridge", "record", "nmea"];
const MAX_RENDERED_LOGS = 120;
let map;
let marker;
let latestStatus = { running: false, current_mode: "" };

function setField(name, value) {
  const field = configForm.elements.namedItem(name);
  if (field) {
    if (field.type === "checkbox") {
      field.checked = Boolean(value);
      return;
    }
    field.value = value ?? "";
  }
}

function getValue(name) {
  const field = configForm.elements.namedItem(name);
  if (field.type === "checkbox") {
    return field.checked;
  }
  return field.value;
}

function readConfigFromForm() {
  return {
    serial: {
      port: getValue("serial.port"),
      baud: Number(getValue("serial.baud")),
    },
    base_local: {
      bind_host: getValue("base_local.bind_host"),
      port: Number(getValue("base_local.port")),
      format: getValue("base_local.format"),
    },
    caster: {
      host: getValue("caster.host"),
      port: Number(getValue("caster.port")),
      mountpoint: getValue("caster.mountpoint"),
      user: getValue("caster.user"),
      password: getValue("caster.password"),
    },
    rover_local: {
      host: getValue("rover_local.host"),
      port: Number(getValue("rover_local.port")),
    },
    rover_ntrip: {
      scheme: getValue("rover_ntrip.scheme"),
      host: getValue("rover_ntrip.host"),
      port: Number(getValue("rover_ntrip.port")),
      mountpoint: getValue("rover_ntrip.mountpoint"),
      user: getValue("rover_ntrip.user"),
      password: getValue("rover_ntrip.password"),
    },
    receiver_bridge: {
      serial_port: getValue("receiver_bridge.serial_port"),
      baud: Number(getValue("receiver_bridge.baud")),
      bind_host: getValue("receiver_bridge.bind_host"),
      port: Number(getValue("receiver_bridge.port")),
    },
    record: {
      serial_port: getValue("record.serial_port"),
      baud: Number(getValue("record.baud")),
      output_dir: getValue("record.output_dir"),
    },
    app: {
      reconnect_delay: Number(getValue("app.reconnect_delay")),
      portal_host: getValue("app.portal_host"),
      portal_port: Number(getValue("app.portal_port")),
      startup_mode: getValue("app.startup_mode"),
      remember_last_mode: getValue("app.remember_last_mode"),
    },
  };
}

function applyConfig(config) {
  const receiverBridge = config.receiver_bridge || {};
  const record = config.record || {};
  setField("serial.port", config.serial.port);
  setField("serial.baud", config.serial.baud);
  setField("base_local.bind_host", config.base_local.bind_host);
  setField("base_local.port", config.base_local.port);
  setField("base_local.format", config.base_local.format);
  setField("caster.host", config.caster.host);
  setField("caster.port", config.caster.port);
  setField("caster.mountpoint", config.caster.mountpoint);
  setField("caster.user", config.caster.user);
  setField("caster.password", config.caster.password);
  setField("rover_local.host", config.rover_local.host);
  setField("rover_local.port", config.rover_local.port);
  setField("rover_ntrip.scheme", config.rover_ntrip.scheme);
  setField("rover_ntrip.host", config.rover_ntrip.host);
  setField("rover_ntrip.port", config.rover_ntrip.port);
  setField("rover_ntrip.mountpoint", config.rover_ntrip.mountpoint);
  setField("rover_ntrip.user", config.rover_ntrip.user);
  setField("rover_ntrip.password", config.rover_ntrip.password);
  setField("receiver_bridge.serial_port", receiverBridge.serial_port || "/dev/ttyACM0");
  setField("receiver_bridge.baud", receiverBridge.baud || 115200);
  setField("receiver_bridge.bind_host", receiverBridge.bind_host || "");
  setField("receiver_bridge.port", receiverBridge.port || 5011);
  setField("record.serial_port", record.serial_port || receiverBridge.serial_port || "/dev/ttyACM0");
  setField("record.baud", record.baud || receiverBridge.baud || 115200);
  setField("record.output_dir", record.output_dir || "recordings");
  setField("app.reconnect_delay", config.app.reconnect_delay);
  setField("app.portal_host", config.app.portal_host);
  setField("app.portal_port", config.app.portal_port);
  setField("app.startup_mode", config.app.startup_mode || "last");
  setField("app.remember_last_mode", config.app.remember_last_mode !== false);
}

async function apiGet(path) {
  const response = await fetch(path);
  const contentType = response.headers.get("content-type") || "";
  const raw = await response.text();
  let payload;

  if (contentType.includes("application/json")) {
    payload = JSON.parse(raw);
  } else {
    throw new Error(`GET ${path} returned non-JSON response`);
  }

  if (!response.ok) {
    throw new Error(`GET ${path} failed`);
  }
  return payload;
}

async function apiPost(path, data) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
  const contentType = response.headers.get("content-type") || "";
  const raw = await response.text();
  let payload = null;

  if (contentType.includes("application/json")) {
    payload = JSON.parse(raw);
  } else if (!response.ok) {
    throw new Error(`POST ${path} returned non-JSON response`);
  }

  if (!response.ok || (payload && payload.ok === false)) {
    throw new Error((payload && payload.error) || `POST ${path} failed`);
  }
  return payload;
}

function renderStatus(status) {
  latestStatus = status || { running: false, current_mode: "" };
  const modeText = status.current_mode ? ` ${status.current_mode}` : "";
  const stateText = status.running ? "running" : "idle";
  const errorText = status.last_error ? ` | last error: ${status.last_error}` : "";
  statusLine.textContent = `Status: ${stateText}${modeText}${errorText}`;
  if (status.current_mode) {
    modeSelect.value = status.current_mode;
  }
  updatePositionPanelVisibility();

  const rows = status.logs.slice(-MAX_RENDERED_LOGS);
  const nearBottom = logBox.scrollTop + logBox.clientHeight >= logBox.scrollHeight - 20;
  logBox.textContent = rows.join("\n");
  if (nearBottom || !status.running) {
    logBox.scrollTop = logBox.scrollHeight;
  }

  renderRecordingStatus(status.recording);
  updateRecordingsVisibility();

  const runtime = status.receiver_runtime || null;
  const modeIsNmea = modeSelect.value === "nmea";
  const preferNmea = modeIsNmea && (!runtime || !runtime.available || runtime.stale);
  if (preferNmea && renderLatestNmeaPosition(rows)) {
    return;
  }
  if (runtime) {
    renderReceiverRuntime(runtime);
  }
}

function updatePositionPanelVisibility() {
  positionPanel.classList.remove("is-hidden");
  controlGrid.classList.remove("map-hidden");
}

function updateRecordingsVisibility() {
  const show = modeSelect.value === "record";
  recordingsSection.classList.toggle("is-hidden", !show);
}

function renderRecordingStatus(recording) {
  const show = modeSelect.value === "record" || Boolean(recording);
  recordPanel.classList.toggle("is-hidden", !show);

  if (!recording) {
    recordFileName.textContent = "-";
    recordElapsed.textContent = "00:00:00";
    recordSize.textContent = "0 B";
    return;
  }

  recordFileName.textContent = recording.name || recording.path || "-";
  recordElapsed.textContent = formatDuration(recording.elapsed_seconds || 0);
  recordSize.textContent = formatBytes(recording.bytes_written || 0);
}

function setSaveMessage(text, isError = false) {
  saveMessage.textContent = text;
  saveMessage.style.color = isError ? "#a32914" : "";
}

async function loadConfig() {
  const config = await apiGet("/api/config");
  applyConfig(config);
}

async function refreshStatus() {
  try {
    const status = await apiGet("/api/status");
    renderStatus(status);
  } catch (error) {
    statusLine.textContent = `Status: portal error | ${error.message}`;
  }
}

async function refreshRecordings() {
  if (modeSelect.value !== "record") {
    return;
  }
  try {
    const payload = await apiGet("/api/recordings");
    renderRecordings(payload.files || []);
  } catch (error) {
    recordingsList.textContent = `Failed to load recordings: ${error.message}`;
  }
}

async function saveConfig() {
  try {
    await apiPost("/api/config", readConfigFromForm());
    setSaveMessage("Config saved.");
    return true;
  } catch (error) {
    setSaveMessage(error.message, true);
    return false;
  }
}

async function startMode() {
  try {
    const saved = await saveConfig();
    if (!saved) {
      return;
    }
    await apiPost("/api/start", { mode: modeSelect.value });
    setSaveMessage(`Started ${modeSelect.value}.`);
    await refreshStatus();
    await refreshRecordings();
  } catch (error) {
    setSaveMessage(error.message, true);
  }
}

async function stopMode() {
  try {
    await apiPost("/api/stop", {});
    setSaveMessage("Stop requested.");
    await refreshStatus();
    await refreshRecordings();
  } catch (error) {
    setSaveMessage(error.message, true);
  }
}

function renderTmodeStatus(status) {
  if (status.mode === "fixed" || status.mode === "survey") {
    tmodeModeSelect.value = status.mode;
  }
  tmodeSurveyMinDur.value = status.survey_min_dur_s ?? tmodeSurveyMinDur.value;
  tmodeSurveyAccLimit.value = status.survey_acc_limit_0_1mm ?? tmodeSurveyAccLimit.value;
  if (typeof status.ecef_x_m === "number") {
    tmodeFixedEcefX.value = Number(status.ecef_x_m).toFixed(4);
  }
  if (typeof status.ecef_y_m === "number") {
    tmodeFixedEcefY.value = Number(status.ecef_y_m).toFixed(4);
  }
  if (typeof status.ecef_z_m === "number") {
    tmodeFixedEcefZ.value = Number(status.ecef_z_m).toFixed(4);
  }

  const lines = [
    `Serial: ${status.serial_port || "-"} @ ${status.baud || "-"}`,
    `Mode: ${status.mode || "-"}`,
    `Version: ${status.version ?? "-"}`,
    `LLA flag: ${status.lla ? "on" : "off"}`,
    `ECEF X: ${Number(status.ecef_x_m || 0).toFixed(4)} m`,
    `ECEF Y: ${Number(status.ecef_y_m || 0).toFixed(4)} m`,
    `ECEF Z: ${Number(status.ecef_z_m || 0).toFixed(4)} m`,
    `Fixed Pos Acc: ${status.fixed_pos_acc_0_1mm ?? "-"} (0.1mm)`,
    `Survey Min Dur: ${status.survey_min_dur_s ?? "-"} s`,
    `Survey Acc Limit: ${status.survey_acc_limit_0_1mm ?? "-"} (0.1mm)`,
  ];
  setSaveMessage(`TMODE3: ${lines.join(" | ")}`);
}

async function readTmodeStatus() {
  try {
    const status = await apiGet("/api/receiver/tmode3");
    renderTmodeStatus(status);
    setSaveMessage("Receiver TMODE3 status loaded.");
  } catch (error) {
    setSaveMessage(error.message, true);
  }
}

async function setTmode(mode) {
  try {
    const payload = {
      mode,
      survey_min_dur: Number(tmodeSurveyMinDur.value || 600),
      survey_acc_limit: Number(tmodeSurveyAccLimit.value || 5000),
    };
    if (mode === "fixed") {
      const x = Number(tmodeFixedEcefX.value);
      const y = Number(tmodeFixedEcefY.value);
      const z = Number(tmodeFixedEcefZ.value);
      if (Number.isNaN(x) || Number.isNaN(y) || Number.isNaN(z)) {
        throw new Error("Enter valid fixed ECEF X/Y/Z values.");
      }
      payload.fixed_ecef_x_m = x;
      payload.fixed_ecef_y_m = y;
      payload.fixed_ecef_z_m = z;
    }
    const result = await apiPost("/api/receiver/tmode3/apply", payload);
    if (result && result.status) {
      renderTmodeStatus(result.status);
    } else {
      await readTmodeStatus();
    }
    setSaveMessage(`TMODE3 updated to ${mode}.`);
  } catch (error) {
    setSaveMessage(error.message, true);
  }
}

async function saveReceiverConfig() {
  try {
    await apiPost("/api/receiver/save", {});
    setSaveMessage("Receiver config saved to BBR/Flash.");
  } catch (error) {
    setSaveMessage(error.message, true);
  }
}

function formatDuration(totalSeconds) {
  const seconds = Math.max(0, Number(totalSeconds) || 0);
  const hours = String(Math.floor(seconds / 3600)).padStart(2, "0");
  const minutes = String(Math.floor((seconds % 3600) / 60)).padStart(2, "0");
  const secs = String(seconds % 60).padStart(2, "0");
  return `${hours}:${minutes}:${secs}`;
}

function formatSurveyDurationCompact(totalSeconds) {
  const seconds = Math.max(0, Number(totalSeconds) || 0);
  const minutes = Math.floor(seconds / 60);
  const hours = Math.floor(minutes / 60);
  const days = Math.floor(hours / 24);
  const years = Math.floor(days / 365);

  if (years > 0) {
    const remDays = days % 365;
    return `${years}y ${remDays}d`;
  }
  if (days > 0) {
    const remHours = hours % 24;
    return `${days}d ${remHours}h`;
  }
  if (hours > 0) {
    const remMinutes = minutes % 60;
    return `${hours}h ${remMinutes}m`;
  }
  return `${minutes}m`;
}

function formatBytes(size) {
  const value = Number(size) || 0;
  if (value < 1024) {
    return `${value} B`;
  }
  if (value < 1024 * 1024) {
    return `${(value / 1024).toFixed(1)} KB`;
  }
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

function formatAgeCompact(seconds) {
  const value = Math.max(0, Number(seconds) || 0);
  if (value < 1) {
    return "now";
  }
  if (value < 60) {
    return `${Math.floor(value)}s ago`;
  }
  if (value < 3600) {
    return `${Math.floor(value / 60)}m ago`;
  }
  return `${Math.floor(value / 3600)}h ago`;
}

function renderRecordings(files) {
  recordingsList.innerHTML = "";

  if (!files.length) {
    recordingsList.textContent = "No recordings yet.";
    return;
  }

  files.forEach((file) => {
    const item = document.createElement("div");
    item.className = "recording-item";

    const meta = document.createElement("div");
    meta.className = "recording-meta";

    const name = document.createElement("div");
    name.className = "recording-name";
    name.textContent = file.name;

    const detail = document.createElement("div");
    detail.className = "recording-detail";
    detail.textContent = `${formatBytes(file.size)} | ${new Date(file.modified * 1000).toLocaleString()}`;

    const link = document.createElement("a");
    link.className = "recording-link";
    link.href = file.download_path;
    link.textContent = "Download";

    meta.appendChild(name);
    meta.appendChild(detail);
    item.appendChild(meta);
    item.appendChild(link);
    recordingsList.appendChild(item);
  });
}

function renderReceiverRuntime(runtime) {
  if (!runtime.available) {
    latValue.textContent = "-";
    lonValue.textContent = "-";
    accValue.textContent = "-";
    surveyValue.textContent = "-";
    coordSource.textContent = "receiver cache";
    mapHint.textContent = runtime.message
      ? `Receiver unavailable: ${runtime.message}`
      : "Waiting for receiver position...";
    return;
  }

  const lat = Number(runtime.lat_deg);
  const lon = Number(runtime.lon_deg);
  const acc = Number(runtime.h_acc_m);
  const mode = runtime.tmode_mode || "unknown";
  const svinActive = Boolean(runtime.svin_active);
  const svinValid = Boolean(runtime.svin_valid);
  const svinAcc = Number(runtime.svin_accuracy_m);
  const svinDur = Number(runtime.svin_duration_s || 0);

  latValue.textContent = Number.isFinite(lat) ? lat.toFixed(7) : "-";
  lonValue.textContent = Number.isFinite(lon) ? lon.toFixed(7) : "-";
  accValue.textContent = Number.isFinite(acc) ? `${acc.toFixed(3)} m` : "-";
  coordSource.textContent = runtime.stale ? "receiver cache (stale)" : "receiver cache";

  if (mode === "survey") {
    const partAcc = Number.isFinite(svinAcc) ? `${svinAcc.toFixed(3)} m` : "-";
    const partDur = formatSurveyDurationCompact(svinDur);
    surveyValue.textContent = svinActive
      ? `surveying (${partDur}, acc ${partAcc})`
      : svinValid
        ? `survey complete (${partDur}, acc ${partAcc})`
        : "survey pending";
  } else if (mode === "fixed") {
    surveyValue.textContent = "fixed mode";
  } else {
    surveyValue.textContent = mode;
  }

  if (Number.isFinite(lat) && Number.isFinite(lon) && map && marker) {
    const latLng = [lat, lon];
    marker.setLatLng(latLng);
    map.setView(latLng, 16);
    const ageText = formatAgeCompact(runtime.age_s);
    if (runtime.stale) {
      mapHint.textContent = runtime.message
        ? `Showing cached receiver position (${ageText}). ${runtime.message}`
        : `Showing cached receiver position (${ageText}).`;
    } else {
      mapHint.textContent = `Receiver position from centralized runtime (${ageText}).`;
    }
  } else {
    mapHint.textContent = "Receiver position unavailable.";
  }
}

function nmeaToDecimal(raw, hemisphere, degreeDigits) {
  if (!raw || !hemisphere) {
    return null;
  }

  const degrees = Number(raw.slice(0, degreeDigits));
  const minutes = Number(raw.slice(degreeDigits));
  if (Number.isNaN(degrees) || Number.isNaN(minutes)) {
    return null;
  }

  let value = degrees + (minutes / 60);
  if (hemisphere === "S" || hemisphere === "W") {
    value *= -1;
  }
  return value;
}

function parseNmeaPosition(line) {
  const start = line.indexOf("$");
  if (start === -1) {
    return null;
  }

  const sentence = line.slice(start);
  if (!sentence.startsWith("$")) {
    return null;
  }

  const parts = sentence.split(",");
  const type = parts[0];

  if (type.endsWith("GGA")) {
    return {
      lat: nmeaToDecimal(parts[2], parts[3], 2),
      lon: nmeaToDecimal(parts[4], parts[5], 3),
      source: type.slice(1),
    };
  }

  if (type.endsWith("RMC")) {
    return {
      lat: nmeaToDecimal(parts[3], parts[4], 2),
      lon: nmeaToDecimal(parts[5], parts[6], 3),
      source: type.slice(1),
    };
  }

  return null;
}

function renderLatestNmeaPosition(rows) {
  for (let i = rows.length - 1; i >= 0; i -= 1) {
    const position = parseNmeaPosition(rows[i]);
    if (!position || position.lat === null || position.lon === null) {
      continue;
    }

    latValue.textContent = position.lat.toFixed(7);
    lonValue.textContent = position.lon.toFixed(7);
    accValue.textContent = "-";
    surveyValue.textContent = "nmea mode";
    coordSource.textContent = position.source;
    mapHint.textContent = "Position from NMEA stream.";

    if (map && marker) {
      const latLng = [position.lat, position.lon];
      marker.setLatLng(latLng);
      map.setView(latLng, 16);
    }
    return true;
  }
  return false;
}

function initMap() {
  if (!window.L) {
    mapHint.textContent = "Map library unavailable. Coordinates still update below.";
    return;
  }

  map = window.L.map("map").setView([0, 0], 2);
  window.L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  }).addTo(map);
  marker = window.L.marker([0, 0]).addTo(map);
}

function switchView(viewName) {
  menuButtons.forEach((button) => {
    button.classList.toggle("active", button.dataset.view === viewName);
  });
  views.forEach((view) => {
    view.classList.toggle("active", view.id === `view-${viewName}`);
  });

  refreshRecordings();
}

menuButtons.forEach((button) => {
  button.addEventListener("click", () => switchView(button.dataset.view));
});

MODES.forEach((mode) => {
  const option = document.createElement("option");
  option.value = mode;
  option.textContent = mode;
  modeSelect.appendChild(option);
});

initMap();
updatePositionPanelVisibility();
updateRecordingsVisibility();
saveConfigButton.addEventListener("click", saveConfig);
startButton.addEventListener("click", startMode);
stopButton.addEventListener("click", stopMode);
modeSelect.addEventListener("change", updatePositionPanelVisibility);
modeSelect.addEventListener("change", updateRecordingsVisibility);
readTmodeButton.addEventListener("click", readTmodeStatus);
applyTmodeButton.addEventListener("click", () => setTmode(tmodeModeSelect.value));
saveReceiverButton.addEventListener("click", saveReceiverConfig);

loadConfig()
  .then(async () => {
    await refreshStatus();
    await refreshRecordings();
    await readTmodeStatus();
  })
  .catch((error) => {
    statusLine.textContent = `Status: failed to load portal data | ${error.message}`;
  });

window.setInterval(refreshStatus, 1000);
window.setInterval(refreshRecordings, 5000);
