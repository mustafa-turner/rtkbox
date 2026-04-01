"""Minimal local control server for rtkbox."""

from collections import deque
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import mimetypes
import struct
import threading
import time

import serial

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
        if self.path == "/api/receiver/tmode3":
            self._send_json(read_receiver_tmode3(self.server.app_state.load_config()))
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
            if self.path == "/api/receiver/tmode3/apply":
                mode = str(data.get("mode", "")).strip().lower()
                if mode not in {"survey", "fixed"}:
                    raise ValueError("mode must be 'survey' or 'fixed'")
                status = apply_receiver_tmode3(
                    state.load_config(),
                    mode=mode,
                    survey_min_dur=int(data.get("survey_min_dur", 600)),
                    survey_acc_limit=int(data.get("survey_acc_limit", 5000)),
                    fixed_ecef_x_m=data.get("fixed_ecef_x_m"),
                    fixed_ecef_y_m=data.get("fixed_ecef_y_m"),
                    fixed_ecef_z_m=data.get("fixed_ecef_z_m"),
                )
                state.log(f"Receiver TMODE3 set to {mode}.")
                self._send_json({"ok": True, "status": status})
                return
            if self.path == "/api/receiver/save":
                save_receiver_config(state.load_config())
                state.log("Receiver config saved to BBR/Flash.")
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


def receiver_serial_target(cfg):
    receiver_bridge = cfg.get("receiver_bridge", {})
    serial_cfg = cfg.get("serial", {})
    port = str(receiver_bridge.get("serial_port") or serial_cfg.get("port") or "/dev/ttyACM0")
    baud = int(receiver_bridge.get("baud") or serial_cfg.get("baud") or 115200)
    if not port.startswith("/dev/"):
        port = f"/dev/{port}"
    return port, baud


def ubx_checksum(data):
    ck_a = 0
    ck_b = 0
    for byte in data:
        ck_a = (ck_a + byte) & 0xFF
        ck_b = (ck_b + ck_a) & 0xFF
    return bytes([ck_a, ck_b])


def ubx_frame(msg_class, msg_id, payload=b""):
    header = bytes([msg_class, msg_id]) + struct.pack("<H", len(payload))
    return b"\xB5\x62" + header + payload + ubx_checksum(header + payload)


def read_ubx_message(ser, timeout_s=1.5):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        start = ser.read(1)
        if start != b"\xB5":
            continue
        if ser.read(1) != b"\x62":
            continue
        header = ser.read(4)
        if len(header) != 4:
            continue
        msg_class, msg_id, payload_len = header[0], header[1], struct.unpack("<H", header[2:4])[0]
        payload = ser.read(payload_len)
        checksum = ser.read(2)
        if len(payload) != payload_len or len(checksum) != 2:
            continue
        if ubx_checksum(header + payload) != checksum:
            continue
        return msg_class, msg_id, payload
    return None


def send_ubx_and_wait_ack(ser, msg_class, msg_id, payload=b"", timeout_s=1.5):
    ser.reset_input_buffer()
    ser.write(ubx_frame(msg_class, msg_id, payload))
    ser.flush()
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        message = read_ubx_message(ser, timeout_s=max(0.1, deadline - time.time()))
        if message is None:
            break
        cls, mid, pl = message
        if cls == 0x05 and mid == 0x01 and len(pl) >= 2 and pl[0] == msg_class and pl[1] == msg_id:
            return
        if cls == 0x05 and mid == 0x00 and len(pl) >= 2 and pl[0] == msg_class and pl[1] == msg_id:
            raise RuntimeError(f"Receiver NAK for UBX-{msg_class:02X}-{msg_id:02X}")
    raise RuntimeError(f"No ACK for UBX-{msg_class:02X}-{msg_id:02X}")


def poll_tmode3_payload(ser):
    ser.reset_input_buffer()
    ser.write(ubx_frame(0x06, 0x71, b""))
    ser.flush()
    deadline = time.time() + 1.5
    while time.time() < deadline:
        message = read_ubx_message(ser, timeout_s=max(0.1, deadline - time.time()))
        if message is None:
            break
        msg_class, msg_id, payload = message
        if msg_class == 0x06 and msg_id == 0x71:
            return payload
    raise RuntimeError("No response to UBX-CFG-TMODE3 poll")


def parse_tmode3_payload(payload):
    if len(payload) < 32:
        raise RuntimeError("Invalid UBX-CFG-TMODE3 response payload length")
    version = payload[0]
    flags = struct.unpack("<H", payload[2:4])[0]
    mode_code = flags & 0xFF
    mode = {0: "disabled", 1: "survey", 2: "fixed"}.get(mode_code, f"unknown({mode_code})")
    lla = bool(flags & 0x100)

    x_cm, y_cm, z_cm = struct.unpack("<iii", payload[4:16])
    x_hp, y_hp, z_hp = struct.unpack("<bbb", payload[16:19])
    fixed_pos_acc, svin_min_dur, svin_acc_limit = struct.unpack("<III", payload[20:32])

    return {
        "version": int(version),
        "mode": mode,
        "flags": int(flags),
        "lla": lla,
        "ecef_x_m": (x_cm / 100.0) + (x_hp / 10000.0),
        "ecef_y_m": (y_cm / 100.0) + (y_hp / 10000.0),
        "ecef_z_m": (z_cm / 100.0) + (z_hp / 10000.0),
        "fixed_pos_acc_0_1mm": int(fixed_pos_acc),
        "survey_min_dur_s": int(svin_min_dur),
        "survey_acc_limit_0_1mm": int(svin_acc_limit),
    }


def meters_to_cm_and_hp(meters):
    total_0_1mm = int(round(float(meters) * 10000.0))
    cm = int(total_0_1mm / 100)
    hp = total_0_1mm - (cm * 100)
    if hp < -99:
        hp = -99
    if hp > 99:
        hp = 99
    return cm, hp


def build_tmode3_payload_for_mode(
    existing_payload,
    mode,
    survey_min_dur,
    survey_acc_limit,
    fixed_ecef_x_m=None,
    fixed_ecef_y_m=None,
    fixed_ecef_z_m=None,
):
    payload = bytearray(existing_payload)
    if len(payload) < 40:
        payload = bytearray(payload.ljust(40, b"\x00"))

    flags = struct.unpack("<H", payload[2:4])[0]
    flags = flags & 0xFE00
    if mode == "survey":
        flags |= 1
        payload[24:28] = struct.pack("<I", max(1, survey_min_dur))
        payload[28:32] = struct.pack("<I", max(1, survey_acc_limit))
    elif mode == "fixed":
        flags |= 2
        if fixed_ecef_x_m is not None and fixed_ecef_y_m is not None and fixed_ecef_z_m is not None:
            x_cm, x_hp = meters_to_cm_and_hp(fixed_ecef_x_m)
            y_cm, y_hp = meters_to_cm_and_hp(fixed_ecef_y_m)
            z_cm, z_hp = meters_to_cm_and_hp(fixed_ecef_z_m)
            payload[4:8] = struct.pack("<i", x_cm)
            payload[8:12] = struct.pack("<i", y_cm)
            payload[12:16] = struct.pack("<i", z_cm)
            payload[16:17] = struct.pack("<b", x_hp)
            payload[17:18] = struct.pack("<b", y_hp)
            payload[18:19] = struct.pack("<b", z_hp)
    else:
        raise ValueError("mode must be 'survey' or 'fixed'")
    payload[2:4] = struct.pack("<H", flags)
    return bytes(payload)


def read_receiver_tmode3(cfg):
    port, baud = receiver_serial_target(cfg)
    with serial.Serial(port=port, baudrate=baud, timeout=0.2) as ser:
        payload = poll_tmode3_payload(ser)
    status = parse_tmode3_payload(payload)
    status["serial_port"] = port
    status["baud"] = baud
    return status


def apply_receiver_tmode3(
    cfg,
    mode,
    survey_min_dur=600,
    survey_acc_limit=5000,
    fixed_ecef_x_m=None,
    fixed_ecef_y_m=None,
    fixed_ecef_z_m=None,
):
    if mode == "fixed":
        provided = [fixed_ecef_x_m, fixed_ecef_y_m, fixed_ecef_z_m]
        if any(value is not None for value in provided) and not all(value is not None for value in provided):
            raise ValueError("fixed mode requires all of fixed_ecef_x_m, fixed_ecef_y_m, fixed_ecef_z_m")
    port, baud = receiver_serial_target(cfg)
    with serial.Serial(port=port, baudrate=baud, timeout=0.2) as ser:
        existing = poll_tmode3_payload(ser)
        payload = build_tmode3_payload_for_mode(
            existing,
            mode,
            survey_min_dur,
            survey_acc_limit,
            fixed_ecef_x_m=fixed_ecef_x_m,
            fixed_ecef_y_m=fixed_ecef_y_m,
            fixed_ecef_z_m=fixed_ecef_z_m,
        )
        send_ubx_and_wait_ack(ser, 0x06, 0x71, payload)
        confirmed = poll_tmode3_payload(ser)
    status = parse_tmode3_payload(confirmed)
    status["serial_port"] = port
    status["baud"] = baud
    return status


def save_receiver_config(cfg):
    port, baud = receiver_serial_target(cfg)
    save_mask = 0x0000FFFF
    payload = struct.pack("<IIIB", 0, save_mask, 0, 0x03)
    with serial.Serial(port=port, baudrate=baud, timeout=0.2) as ser:
        send_ubx_and_wait_ack(ser, 0x06, 0x09, payload)


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
