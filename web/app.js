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

const MODES = ["base-local", "base-ntrip", "rover-local", "rover-ntrip", "receiver-bridge", "record", "nmea"];
const MAX_RENDERED_LOGS = 120;
let map;
let marker;

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

  renderLatestPosition(rows);
  renderRecordingStatus(status.recording);
  updateRecordingsVisibility();
}

function updatePositionPanelVisibility() {
  const show = modeSelect.value === "nmea";
  positionPanel.classList.toggle("is-hidden", !show);
  controlGrid.classList.toggle("map-hidden", !show);
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

function formatDuration(totalSeconds) {
  const seconds = Math.max(0, Number(totalSeconds) || 0);
  const hours = String(Math.floor(seconds / 3600)).padStart(2, "0");
  const minutes = String(Math.floor((seconds % 3600) / 60)).padStart(2, "0");
  const secs = String(seconds % 60).padStart(2, "0");
  return `${hours}:${minutes}:${secs}`;
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

function nmeaToDecimal(raw, hemisphere, degreeDigits) {
  if (!raw || !hemisphere) {
    return null;
  }

  const degrees = Number(raw.slice(0, degreeDigits));
  const minutes = Number(raw.slice(degreeDigits));
  if (Number.isNaN(degrees) || Number.isNaN(minutes)) {
    return null;
  }

  let value = degrees + minutes / 60;
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

function renderLatestPosition(rows) {
  for (let i = rows.length - 1; i >= 0; i -= 1) {
    const position = parseNmeaPosition(rows[i]);
    if (!position || position.lat === null || position.lon === null) {
      continue;
    }

    latValue.textContent = position.lat.toFixed(6);
    lonValue.textContent = position.lon.toFixed(6);
    coordSource.textContent = position.source;
    mapHint.textContent = "Map uses OpenStreetMap tiles when internet is available.";

    if (map && marker) {
      const latLng = [position.lat, position.lon];
      marker.setLatLng(latLng);
      map.setView(latLng, 16);
    }
    return;
  }

  latValue.textContent = "-";
  lonValue.textContent = "-";
  coordSource.textContent = "-";
  mapHint.textContent = "Waiting for NMEA position...";
}

function switchView(viewName) {
  menuButtons.forEach((button) => {
    button.classList.toggle("active", button.dataset.view === viewName);
  });
  views.forEach((view) => {
    view.classList.toggle("active", view.id === `view-${viewName}`);
  });
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

loadConfig()
  .then(async () => {
    await refreshStatus();
    await refreshRecordings();
  })
  .catch((error) => {
    statusLine.textContent = `Status: failed to load portal data | ${error.message}`;
  });

window.setInterval(refreshStatus, 1000);
window.setInterval(refreshRecordings, 5000);
