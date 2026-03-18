"""Minimal local control server for rtkbox."""

from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import mimetypes
import subprocess
import threading
import time

from rtkbox_config import MODES, detect_local_ip, load_config, save_config
from rtkbox_modes import Runner, run_mode


WEB_DIR = Path(__file__).with_name("web")
LOG_LIMIT = 200


class AppState(Runner):
    def __init__(self, config_path):
        super().__init__()
        self.config_path = config_path
        self.logs = deque(maxlen=LOG_LIMIT)
        self.worker = None
        self.current_mode = None
        self.last_error = ""

    def log(self, message):
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        line = f"{timestamp} {message}"
        with self._lock:
            self.logs.append(line)
        print(line, flush=True)

    def snapshot(self):
        with self._lock:
            running = self.worker is not None and self.worker.is_alive()
            cfg = self.load_config()
            wifi_cfg = cfg.get("wifi", {})
            ap_cfg = cfg.get("ap", {})
            return {
                "running": running,
                "current_mode": self.current_mode,
                "last_error": self.last_error,
                "logs": list(self.logs),
                "log_limit": LOG_LIMIT,
                "wifi_status": get_wifi_status(wifi_cfg.get("interface", "wlan0")),
                "ap_status": get_wifi_status(ap_cfg.get("interface", "wlan0")),
            }

    def load_config(self):
        return load_config(self.config_path)

    def save_config(self, config):
        save_config(self.config_path, config)
        self.log("Config saved.")

    def update_last_mode(self, mode):
        cfg = self.load_config()
        app_cfg = cfg.setdefault("app", {})
        if not app_cfg.get("remember_last_mode", True):
            return
        app_cfg["last_mode"] = mode
        save_config(self.config_path, cfg)

    def start_mode(self, mode):
        with self._lock:
            if self.worker is not None and self.worker.is_alive():
                raise RuntimeError(f"mode already running: {self.current_mode}")
            self.update_last_mode(mode)
            self.stop_event = threading.Event()
            self.current_mode = mode
            self.last_error = ""
            self.worker = threading.Thread(target=self._run_mode_thread, args=(mode,), daemon=True)
            self.worker.start()
        self.log(f"Starting mode: {mode}")

    def stop_mode(self):
        self.request_stop()
        worker = self.worker
        if worker is not None and worker.is_alive():
            worker.join(timeout=3)
        with self._lock:
            self.worker = None
            self.current_mode = None
            self.process = None
        self.log("Stop requested.")

    def _run_mode_thread(self, mode):
        try:
            cfg = self.load_config()
            run_mode(mode, cfg, self)
        except Exception as exc:
            with self._lock:
                self.last_error = str(exc)
            self.log(f"Error: {exc}")
        finally:
            with self._lock:
                self.worker = None
                self.current_mode = None
                self.process = None
            self.stop_event.clear()
            self.log(f"Mode ended: {mode}")


class PortalHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send_static("index.html")
            return
        if self.path == "/app.css":
            self._send_static("app.css")
            return
        if self.path == "/app.js":
            self._send_static("app.js")
            return
        if self.path == "/api/config":
            self._send_json(self.server.app_state.load_config())
            return
        if self.path == "/api/status":
            self._send_json(self.server.app_state.snapshot())
            return
        if self.path == "/api/wifi/scan":
            interface = self.server.app_state.load_config().get("wifi", {}).get("interface", "wlan0")
            self._send_json({"networks": scan_wifi_networks(interface)})
            return
        self.send_error(404)

    def do_POST(self):
        state = self.server.app_state
        try:
            data = self._read_json()
            if self.path == "/api/config":
                state.save_config(validate_config_payload(data, state.load_config()))
                self._send_json({"ok": True})
                return
            if self.path == "/api/start":
                mode = data.get("mode", "")
                if mode not in MODES:
                    raise ValueError(f"unsupported mode: {mode}")
                state.start_mode(mode)
                self._send_json({"ok": True})
                return
            if self.path == "/api/network/mode":
                config = validate_config_payload(data, state.load_config())
                network_mode = str(data.get("network_mode", "")).strip()
                if network_mode not in {"ap", "wifi"}:
                    raise ValueError("network_mode must be 'ap' or 'wifi'")
                state.save_config(config)
                if network_mode == "ap":
                    apply_access_point(config["ap"])
                else:
                    apply_wifi_client(config["wifi"])
                self._send_json({"ok": True})
                return
            if self.path == "/api/wifi/apply":
                config = validate_config_payload(data, state.load_config())
                state.save_config(config)
                apply_wifi_client(config["wifi"])
                self._send_json({"ok": True})
                return
            if self.path == "/api/ap/apply":
                config = validate_config_payload(data, state.load_config())
                state.save_config(config)
                apply_access_point(config["ap"])
                self._send_json({"ok": True})
                return
            if self.path == "/api/stop":
                state.stop_mode()
                self._send_json({"ok": True})
                return
            self.send_error(404)
        except Exception as exc:
            state.log(f"Portal action failed: {exc}")
            with state._lock:
                state.last_error = str(exc)
            self._send_json({"ok": False, "error": str(exc)}, status=400)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8") if length else "{}"
        return json.loads(raw)

    def _send_static(self, name):
        path = WEB_DIR / name
        if not path.is_file():
            self.send_error(404)
            return
        payload = path.read_bytes()
        content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_json(self, data, status=200):
        payload = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt, *args):
        return


def validate_config_payload(data, existing=None):
    if not isinstance(data, dict):
        raise ValueError("config payload must be an object")

    existing = existing or {}
    existing_app = existing.get("app", {})

    return {
        "serial": {
            "port": str(data["serial"]["port"]),
            "baud": int(data["serial"]["baud"]),
        },
        "base_local": {
            "bind_host": str(data["base_local"].get("bind_host", "")),
            "port": int(data["base_local"]["port"]),
            "format": str(data["base_local"].get("format", "")),
        },
        "caster": {
            "host": str(data["caster"]["host"]),
            "port": int(data["caster"]["port"]),
            "mountpoint": str(data["caster"]["mountpoint"]),
            "user": str(data["caster"].get("user", "")),
            "password": str(data["caster"].get("password", "")),
        },
        "rover_local": {
            "host": str(data["rover_local"]["host"]),
            "port": int(data["rover_local"]["port"]),
        },
        "rover_ntrip": {
            "scheme": str(data["rover_ntrip"]["scheme"]),
            "host": str(data["rover_ntrip"]["host"]),
            "port": int(data["rover_ntrip"]["port"]),
            "mountpoint": str(data["rover_ntrip"]["mountpoint"]),
            "user": str(data["rover_ntrip"].get("user", "")),
            "password": str(data["rover_ntrip"].get("password", "")),
        },
        "receiver_bridge": {
            "bind_host": str(data["receiver_bridge"].get("bind_host", "")),
            "port": int(data["receiver_bridge"]["port"]),
            "serial_port": str(data["receiver_bridge"].get("serial_port", "/dev/ttyACM0")),
            "baud": int(data["receiver_bridge"].get("baud", 115200)),
        },
        "app": {
            "reconnect_delay": int(data["app"]["reconnect_delay"]),
            "portal_host": str(data["app"].get("portal_host", "0.0.0.0")),
            "portal_port": int(data["app"].get("portal_port", 8080)),
            "remember_last_mode": bool(data["app"].get("remember_last_mode", True)),
            "startup_mode": str(data["app"].get("startup_mode", "last")),
            "last_mode": str(existing_app.get("last_mode", "")),
        },
        "ap": {
            "interface": str(data["ap"].get("interface", "wlan0")),
            "connection_name": str(data["ap"].get("connection_name", "rtkbox-ap")),
            "ssid": str(data["ap"].get("ssid", "RTKbox")),
            "password": str(data["ap"].get("password", "")),
            "address": str(data["ap"].get("address", "10.42.0.1/24")),
        },
        "wifi": {
            "interface": str(data["wifi"].get("interface", "wlan0")),
            "connection_name": str(data["wifi"].get("connection_name", "rtkbox-client")),
            "ssid": str(data["wifi"].get("ssid", "")),
            "password": str(data["wifi"].get("password", "")),
        },
    }


def run_nmcli(args):
    result = subprocess.run(
        ["/usr/bin/nmcli"] + args,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        if not detail:
            detail = f"nmcli failed with exit code {result.returncode}"
        raise RuntimeError(detail)
    return result


def run_nmcli_sudo(args):
    result = subprocess.run(
        ["sudo", "/usr/bin/nmcli"] + args,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        if not detail:
            detail = f"nmcli failed with exit code {result.returncode}"
        raise RuntimeError(detail)
    return result


def apply_wifi_client(wifi_cfg):
    interface = wifi_cfg.get("interface", "wlan0")
    connection_name = wifi_cfg.get("connection_name", "rtkbox-client")
    ssid = wifi_cfg.get("ssid", "").strip()
    password = wifi_cfg.get("password", "")

    if not ssid:
        raise ValueError("wifi.ssid is required")

    subprocess.run(
        ["sudo", "/usr/bin/nmcli", "connection", "delete", connection_name],
        check=False,
        capture_output=True,
        text=True,
    )

    connect_cmd = [
        "device",
        "wifi",
        "connect",
        ssid,
        "ifname",
        interface,
        "name",
        connection_name,
    ]
    if password:
        connect_cmd.extend(["password", password])

    run_nmcli_sudo(connect_cmd)
    run_nmcli_sudo(["connection", "modify", connection_name, "connection.autoconnect", "yes"])
    run_nmcli_sudo(["connection", "modify", connection_name, "connection.interface-name", interface])


def apply_access_point(ap_cfg):
    interface = ap_cfg.get("interface", "wlan0")
    connection_name = ap_cfg.get("connection_name", "rtkbox-ap")
    ssid = ap_cfg.get("ssid", "").strip()
    password = ap_cfg.get("password", "")
    address = ap_cfg.get("address", "10.42.0.1/24")

    if not ssid:
        raise ValueError("ap.ssid is required")
    if len(password) < 8:
        raise ValueError("ap.password must be at least 8 characters")

    subprocess.run(
        ["sudo", "/usr/bin/nmcli", "connection", "delete", connection_name],
        check=False,
        capture_output=True,
        text=True,
    )

    run_nmcli_sudo(
        [
            "connection",
            "add",
            "type",
            "wifi",
            "ifname",
            interface,
            "con-name",
            connection_name,
            "autoconnect",
            "yes",
            "ssid",
            ssid,
        ]
    )
    run_nmcli_sudo(
        [
            "connection",
            "modify",
            connection_name,
            "802-11-wireless.mode",
            "ap",
            "802-11-wireless.band",
            "bg",
            "802-11-wireless-security.key-mgmt",
            "wpa-psk",
            "802-11-wireless-security.psk",
            password,
            "ipv4.method",
            "shared",
            "ipv4.addresses",
            address,
            "ipv6.method",
            "disabled",
            "connection.interface-name",
            interface,
        ]
    )
    run_nmcli_sudo(["connection", "up", connection_name])


def get_wifi_status(interface):
    try:
        result = run_nmcli(
            [
                "-t",
                "-f",
                "GENERAL.STATE,GENERAL.CONNECTION,IP4.ADDRESS",
                "device",
                "show",
                interface,
            ]
        )
    except Exception:
        return {"interface": interface, "state": "unknown", "connection": "", "address": ""}

    state = ""
    connection = ""
    address = ""
    for line in result.stdout.splitlines():
        if line.startswith("GENERAL.STATE:"):
            state = line.split(":", 1)[1]
        elif line.startswith("GENERAL.CONNECTION:"):
            connection = line.split(":", 1)[1]
        elif line.startswith("IP4.ADDRESS[1]:"):
            address = line.split(":", 1)[1]

    return {
        "interface": interface,
        "state": state,
        "connection": connection,
        "address": address,
    }


def scan_wifi_networks(interface):
    subprocess.run(
        ["/usr/bin/nmcli", "device", "wifi", "rescan", "ifname", interface],
        check=False,
        capture_output=True,
        text=True,
    )

    result = run_nmcli(
        [
            "-m",
            "multiline",
            "-f",
            "IN-USE,SSID,SIGNAL,SECURITY",
            "device",
            "wifi",
            "list",
            "ifname",
            interface,
        ]
    )

    networks = []
    current = {}
    for line in result.stdout.splitlines():
        if not line.strip():
            if current.get("ssid"):
                networks.append(current)
            current = {}
            continue
        key, value = line.split(":", 1)
        current[key.strip().lower().replace("-", "_")] = value.strip()

    if current.get("ssid"):
        networks.append(current)

    deduped = {}
    for entry in networks:
        ssid = entry.get("ssid", "").strip()
        if not ssid:
            continue
        signal = int(entry.get("signal", "0") or 0)
        item = {
            "ssid": ssid,
            "signal": signal,
            "security": entry.get("security", ""),
            "in_use": entry.get("in_use", "") == "*",
        }
        existing = deduped.get(ssid)
        if existing is None or item["signal"] > existing["signal"]:
            deduped[ssid] = item

    return sorted(
        deduped.values(),
        key=lambda item: (not item["in_use"], -item["signal"], item["ssid"].lower()),
    )


def run_portal(config_path):
    cfg = load_config(config_path)
    app_cfg = cfg.get("app", {})
    host = app_cfg.get("portal_host", "0.0.0.0")
    port = int(app_cfg.get("portal_port", 8080))

    state = AppState(config_path)
    httpd = ThreadingHTTPServer((host, port), PortalHandler)
    httpd.app_state = state

    print(f"Portal listening on http://{host}:{port}", flush=True)
    print(f"Try from Wi-Fi clients: http://{detect_local_ip()}:{port}", flush=True)
    print("Captive portal redirect itself must be handled by the Pi AP/network setup.", flush=True)

    startup_mode = resolve_startup_mode(cfg)
    if startup_mode:
        try:
            state.start_mode(startup_mode)
            state.log(f"Autostart enabled. Started mode: {startup_mode}")
        except Exception as exc:
            state.log(f"Autostart failed: {exc}")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        state.stop_mode()
        httpd.server_close()
        print("Portal stopped.", flush=True)


def resolve_startup_mode(cfg):
    app_cfg = cfg.get("app", {})
    startup_mode = str(app_cfg.get("startup_mode", "")).strip()
    if not startup_mode:
        return ""
    if startup_mode == "last":
        startup_mode = str(app_cfg.get("last_mode", "")).strip()
    if startup_mode in MODES:
        return startup_mode
    return ""
