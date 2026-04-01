"""Minimal local control server for rtkbox."""

from collections import deque
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import mimetypes
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
            recording = dict(self.runtime.get("recording") or {}) if self.runtime.get("recording") else None
            if recording and recording.get("started_at"):
                recording["elapsed_seconds"] = int(max(0, time.time() - recording["started_at"]))
            return {
                "running": running,
                "current_mode": self.current_mode,
                "last_error": self.last_error,
                "logs": list(self.logs),
                "log_limit": LOG_LIMIT,
                "recording": recording,
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
        if self.path == "/api/recordings":
            self._send_json({"files": list_recordings(self.server.app_state.load_config())})
            return
        if self.path.startswith("/downloads/"):
            self._send_download(self.path.removeprefix("/downloads/"))
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

    def _send_download(self, name):
        path = resolve_recording_path(self.server.app_state.load_config(), name)
        if path is None or not path.is_file():
            self.send_error(404)
            return
        payload = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
        self.send_header("Last-Modified", formatdate(path.stat().st_mtime, usegmt=True))
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
        "record": {
            "serial_port": str(data["record"].get("serial_port", data["receiver_bridge"].get("serial_port", "/dev/ttyACM0"))),
            "baud": int(data["record"].get("baud", data["receiver_bridge"].get("baud", 115200))),
            "output_dir": str(data["record"].get("output_dir", "recordings")),
        },
        "app": {
            "reconnect_delay": int(data["app"]["reconnect_delay"]),
            "portal_host": str(data["app"].get("portal_host", "0.0.0.0")),
            "portal_port": int(data["app"].get("portal_port", 8080)),
            "remember_last_mode": bool(data["app"].get("remember_last_mode", True)),
            "startup_mode": str(data["app"].get("startup_mode", "last")),
            "last_mode": str(existing_app.get("last_mode", "")),
        },
    }


def recordings_dir(cfg):
    record_cfg = cfg.get("record", {})
    return Path(str(record_cfg.get("output_dir", "recordings")))


def list_recordings(cfg):
    base_dir = recordings_dir(cfg)
    if not base_dir.exists():
        return []

    files = []
    for path in sorted(base_dir.glob("*.ubx"), key=lambda item: item.stat().st_mtime, reverse=True):
        stat = path.stat()
        files.append(
            {
                "name": path.name,
                "size": stat.st_size,
                "modified": int(stat.st_mtime),
                "download_path": f"/downloads/{path.name}",
            }
        )
    return files


def resolve_recording_path(cfg, name):
    safe_name = Path(name).name
    if safe_name != name:
        return None
    path = recordings_dir(cfg) / safe_name
    try:
        path.resolve().relative_to(recordings_dir(cfg).resolve())
    except Exception:
        return None
    return path


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
